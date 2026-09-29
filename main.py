import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from supabase import create_client
from telegram import Update

from app.config import Settings
from app.database import Database
from app.safe_logging import log_exception
from app.summaries import format_summary, midnight_report_date, today_moscow
from app.telegram_bot import build_application, update_failed

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
            await telegram.start()
            app.state.settings = settings
            app.state.db = db
            app.state.telegram = telegram
            app.state.update_lock = asyncio.Lock()
            try:
                yield
            finally:
                await telegram.stop()
    except Exception as exc:
        log_exception(logger, 'Application lifecycle failed', exc, settings)
        raise RuntimeError('Application lifecycle failed; check configuration and connectivity') from None


app = FastAPI(lifespan=lifespan)


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
        members, reported = await asyncio.to_thread(
            request.app.state.db.summary_data, settings.telegram_chat_id, day)
        if kind == 'midnight' and all(
                member['telegram_user_id'] in reported for member in members):
            return {'status': 'ok', 'report_date': day.isoformat()}
        await request.app.state.telegram.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=format_summary(members, reported, day, kind))
    except Exception as exc:
        log_exception(logger, 'Summary failed', exc, settings)
        raise HTTPException(503, 'Summary delivery failed') from None
    return {'status': 'ok', 'report_date': day.isoformat()}


@app.post('/cron/evening')
async def evening(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    return await send_summary(request, secret, 'evening')


@app.post('/cron/midnight')
async def midnight(request: Request, secret: str | None = Header(None, alias='X-Cron-Secret')):
    return await send_summary(request, secret, 'midnight')
