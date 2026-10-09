import asyncio
import hashlib
import logging
import os
import tempfile
import time
from datetime import date, datetime, timezone

from app.admin_alerts import send_admin_alert
from app.pushup_diagnostics import format_diagnostics
from app.pose_analysis import analyze_pose_visibility
from app.pushup_classification import classify_pushup_attempt
from app.safe_logging import log_exception
from app.world_pose_analysis import analyze_world_landmarks

logger = logging.getLogger(__name__)


async def process_attempt(application, settings, db, attempt):
    attempt_id = attempt['id']
    chat_id = attempt['chat_id']
    user_id = attempt['telegram_user_id']
    message_id = attempt['telegram_message_id']
    day = attempt['report_date']
    temp_path = None
    started = time.monotonic()
    try:
        telegram_file = await application.bot.get_file(attempt['telegram_file_id'])
        with tempfile.NamedTemporaryFile(prefix='pushup_', suffix='.mp4', delete=False) as f:
            temp_path = f.name
        await telegram_file.download_to_drive(custom_path=temp_path)
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
            raise ValueError('Downloaded video note is empty')

        with open(temp_path, 'rb') as video_file:
            media_digest = hashlib.sha256(video_file.read()).hexdigest()
        media_size = os.path.getsize(temp_path)

        metrics = await asyncio.to_thread(
            analyze_pose_visibility, temp_path, 6.0, analyze_world_landmarks)
        world = metrics['world_geometry']
        classification = classify_pushup_attempt(metrics)
        status = classification['status']
        reason = classification['reason']
        count = int(classification.get('count') or 0)
        confidence = round(metrics['usable_ratio'], 4)
        front = metrics.get('front_cycles') or {}
        logger.info(
            'Pushup diagnostic attempt=%s msg=%s digest=%s size=%s frames=%s '
            'pose=%s usable=%s left=%s right=%s paired=%s temporal=%s '
            'horizontal=%s vertical=%s status=%s reason=%s count=%s',
            attempt_id, message_id, media_digest, media_size,
            metrics.get('sampled_frames'), metrics.get('pose_ratio'),
            metrics.get('usable_ratio'), (front.get('left') or {}).get('count'),
            (front.get('right') or {}).get('count'),
            (front.get('paired') or {}).get('count'),
            world.get('gated_pushup_count'), world.get('horizontal_ratio'),
            world.get('vertical_ratio'), status, reason, count,
        )

        await asyncio.to_thread(
            lambda: db.client.table(db.attempt_table).update({
                'status': status,
                'pushup_count': count,
                'confidence': confidence,
                'rejection_reason': reason,
                'processed_at': datetime.now(timezone.utc).isoformat(),
                'processing_started_at': None,
                'last_error': None,
            }).eq('id', attempt_id).execute())

        if settings.pushup_diagnostics_enabled and chat_id == settings.telegram_chat_id:
            report = format_diagnostics(attempt_id, message_id, metrics, classification, time.monotonic()-started)
            for chunk in report:
                await application.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id)

        replies_enabled = await asyncio.to_thread(db.result_replies_enabled, chat_id)
        if settings.pushup_results_enabled and replies_enabled and not settings.pushup_diagnostics_enabled:
            total = await asyncio.to_thread(
                db.daily_pushup_total, chat_id, user_id,
                date.fromisoformat(day))
            if status == 'accepted':
                text = f"✅ Засчитано: {count} отж.\n📊 Всего за сегодня: {total}"
            elif status == 'rejected':
                text = f"❌ Отжимания не засчитаны\n📊 Всего за сегодня: {total}"
            else:
                text = f"⚠️ Не удалось уверенно распознать отжимания\n📊 Всего за сегодня: {total}"
            await application.bot.send_message(
                chat_id=chat_id, text=text, reply_to_message_id=message_id)

        logger.info('Push-up worker completed attempt_id=%s status=%s count=%s gated_count=%s',
                    attempt_id,status,count,world.get('gated_pushup_count',0))
    except Exception as exc:
        try:
            rows = await asyncio.to_thread(
                db.retry_or_fail_pushup_attempt, attempt_id, type(exc).__name__)
            if rows and rows[0].get('status') == 'failed':
                member = await asyncio.to_thread(db.member, chat_id, user_id)
                name = (member or {}).get('display_name') or str(user_id)
                await send_admin_alert(
                    application, settings,
                    "🚨 Не удалось обработать кружок после 3 попыток\n"
                    f"👤 {name}\n"
                    f"💬 Chat ID: {chat_id}\n"
                    f"🆔 Message ID: {message_id}\n"
                    f"❌ Ошибка: {type(exc).__name__}")
        except Exception as db_exc:
            log_exception(logger,'Push-up queue failure update failed',db_exc,settings)
        log_exception(logger,'Push-up queue processing failed',exc,settings)
    finally:
        if temp_path:
            try: os.remove(temp_path)
            except FileNotFoundError: pass
            except OSError as exc: log_exception(logger,'Temporary video cleanup failed',exc,settings)


async def worker_loop(application, settings, db):
    while True:
        try:
            attempt = await asyncio.to_thread(db.claim_next_pushup_attempt)
            if attempt:
                await process_attempt(application,settings,db,attempt)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception(logger,'Push-up worker loop failed',exc,settings)
        await asyncio.sleep(2)
