import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from supabase import create_client
from telegram import Update

from app.admin_alerts import send_admin_alert
from app.analysis_worker import worker_loop
from app.config import Settings
from app.database import Database
from app.safe_logging import log_exception
from app.summaries import format_summary, midnight_report_date, today_moscow
from app.telegram_bot import build_application, update_failed
from app.miniapp import router as miniapp_router
from app.sick_leave import router as sick_leave_router, deliver_sick_leave_events
from app.production_media import media_loop

logger = logging.getLogger(__name__)
# HTTP client URLs can contain bot tokens. Never enable their request logs.
for name in ('httpx', 'httpcore', 'telegram', 'supabase', 'postgrest'):
    logging.getLogger(name).setLevel(logging.CRITICAL)


@asynccontextmanager
async def lifespan(app):
    settings = Settings.from_env()
    try:
        db = Database(create_client(settings.supabase_url, settings.supabase_key))
        telegram = build_application(settings, db)
        async with telegram:  # initialize / shutdown
            if telegram.post_init is not None:
                await telegram.post_init(telegram)
            await telegram.start()
            app.state.settings = settings
            app.state.db = db
            app.state.telegram = telegram
            app.state.update_lock = asyncio.Lock()
            app.state.sick_leave_delivery_lock = asyncio.Lock()
            analysis_lock = asyncio.Lock()
            media_task = (asyncio.create_task(media_loop(telegram.bot, db, analysis_lock))
                          if settings.pushup_media_archive_enabled else None)
            logger.info('Media archive enabled=%s overlay diagnostics enabled=%s',
                        settings.pushup_media_archive_enabled,
                        settings.pushup_overlay_diagnostics_enabled)
            worker_task = None
            if settings.pushup_analysis_enabled:
                worker_task = asyncio.create_task(worker_loop(telegram, settings, db, analysis_lock))
            try:
                yield
            finally:
                if media_task:
                    media_task.cancel()
                    try:
                        await media_task
                    except asyncio.CancelledError:
                        pass
                if worker_task:
                    worker_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                await telegram.stop()
    except Exception as exc:
        log_exception(logger, 'Application lifecycle failed', exc, settings)
        raise RuntimeError('Application lifecycle failed; check configuration and connectivity') from None


app = FastAPI(lifespan=lifespan)
app.include_router(miniapp_router)
app.include_router(sick_leave_router)


def check_secret(provided: str | None, expected: str):
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(403, 'Forbidden')


@app.get('/')
async def health():
    return {'status': 'ok'}


@app.post('/telegram/webhook')
async def webhook(request: Request,
                  token: str | None = Header(None, alias='X-Telegram-Bot-Api-Secret-Token')):
    settings = request.app.state.settings
    if settings.telegram_webhook_secret:
        check_secret(token, settings.telegram_webhook_secret)
    try:
        data = await request.json()
        if not isinstance(data, dict) or type(data.get('update_id')) is not int:
            raise ValueError('Invalid update')
        update = Update.de_json(data, request.app.state.telegram.bot)
        logger.info('Telegram webhook received update_id=%s chat_id=%s kind=%s',
                    update.update_id,
                    update.effective_chat.id if update.effective_chat else None,
                    'command' if update.effective_message and update.effective_message.text and update.effective_message.text.startswith('/') else 'other')
    except Exception:
        raise HTTPException(400, 'Invalid Telegram update') from None
    # PTB expects sequential processing by default. Finish before acknowledging;
    # failed DB/API operations produce 503 so Telegram can retry delivery.
    async with request.app.state.update_lock:
        marker = update_failed.set(False)
        try:
            await request.app.state.telegram.process_update(update)
            if update_failed.get():
                raise HTTPException(503, 'Update processing failed')
            logger.info('Telegram webhook processed update_id=%s', update.update_id)
        except HTTPException:
            raise
        except Exception as exc:
            log_exception(logger, 'Webhook failed', exc, settings)
            raise HTTPException(503, 'Update processing failed') from None
        finally:
            update_failed.reset(marker)
    return {'status': 'ok'}


async def send_summary(request, secret, kind):
    settings = request.app.state.settings
    check_secret(secret, settings.cron_secret)
    if settings.telegram_chat_id is None:
        raise HTTPException(503, 'TELEGRAM_CHAT_ID is not configured')
    day = midnight_report_date() if kind == 'midnight' else today_moscow()
    try:
        members, reported, totals = await asyncio.to_thread(
            request.app.state.db.summary_data, settings.telegram_chat_id, day)
        norm = await asyncio.to_thread(request.app.state.db.daily_norm, settings.telegram_chat_id)
        sick = await asyncio.to_thread(request.app.state.db.sick_leaves_for_day, settings.telegram_chat_id, day)
        if kind == 'midnight' and all(
                member['telegram_user_id'] in sick or totals.get(member['telegram_user_id'], 0) >= norm for member in members):
            return {'status': 'ok', 'report_date': day.isoformat()}
        manual = await asyncio.to_thread(request.app.state.db.manual_totals, settings.telegram_chat_id, day)
        await request.app.state.telegram.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=format_summary(members, reported, day, kind, totals, manual, daily_norm=norm, sick_leaves=sick))
    except Exception as exc:
        log_exception(logger, 'Summary failed', exc, settings)
        label = 'вечерней сводки (21:00)' if kind == 'evening' else 'ночной сводки (00:00)'
        await send_admin_alert(
            request.app.state.telegram, settings,
            f"🚨 Ошибка {label}\n"
            f"📅 Дата отчёта: {day.isoformat()}\n"
            f"❌ Ошибка: {type(exc).__name__}")
        raise HTTPException(503, 'Summary delivery failed') from None
    return {'status': 'ok', 'report_date': day.isoformat()}


@app.post('/cron/evening')
async def evening(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    return await send_summary(request, secret, 'evening')


@app.post('/cron/midnight')
async def midnight(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    return await send_summary(request, secret, 'midnight')


@app.post('/cron/sick-leave-recovery')
async def sick_leave_recovery(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    """Run at 09:00 Europe/Moscow. An expired leave returns on the following day."""
    from datetime import timedelta
    check_secret(secret, request.app.state.settings.cron_secret)
    chat_id = request.app.state.settings.telegram_chat_id
    if chat_id is None:
        raise HTTPException(503, 'TELEGRAM_CHAT_ID is not configured')
    from datetime import datetime
    from app.summaries import MOSCOW
    now = datetime.now(MOSCOW)
    if now.hour != 9:
        return {'status': 'skipped', 'reason': 'outside_09_moscow_window'}
    db = request.app.state.db
    today = today_moscow(now)
    yesterday = (today - timedelta(days=1)).isoformat()
    def load():
        return (db.client.table('sick_leaves').select('id,patient_id,starts_on,ends_on')
                .eq('chat_id', chat_id).is_('deleted_at', 'null')
                .eq('ends_on', yesterday).execute().data)
    leaves = await asyncio.to_thread(load)
    created = 0
    for leave in leaves:
        def prepare():
            existing = (db.client.table('sick_leave_events').select('id')
                        .eq('sick_leave_id', leave['id']).eq('event_type', 'recovered')
                        .limit(1).execute().data)
            if existing:
                return False
            db.client.table('sick_leave_events').insert({
                'sick_leave_id': leave['id'], 'chat_id': chat_id,
                'actor_id': leave['patient_id'], 'event_type': 'recovered',
                'after_state': leave}).execute()
            return True
        if await asyncio.to_thread(prepare):
            created += 1
    sent = await deliver_sick_leave_events(request, chat_id)
    return {'status': 'ok', 'queued_recoveries': created, 'delivered_events': sent}


@app.post('/cron/sick-leave-notifications')
async def sick_leave_notifications(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    check_secret(secret, request.app.state.settings.cron_secret)
    chat_id = request.app.state.settings.telegram_chat_id
    if chat_id is None:
        raise HTTPException(503, 'TELEGRAM_CHAT_ID is not configured')
    sent = await deliver_sick_leave_events(request, chat_id)
    return {'status': 'ok', 'delivered_events': sent}
