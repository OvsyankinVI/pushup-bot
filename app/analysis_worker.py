import asyncio
import logging
import os
import tempfile
from datetime import date, datetime, timezone

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
    try:
        telegram_file = await application.bot.get_file(attempt['telegram_file_id'])
        with tempfile.NamedTemporaryFile(prefix='pushup_', suffix='.mp4', delete=False) as f:
            temp_path = f.name
        await telegram_file.download_to_drive(custom_path=temp_path)
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
            raise ValueError('Downloaded video note is empty')

        metrics = await asyncio.to_thread(
            analyze_pose_visibility, temp_path, 6.0, analyze_world_landmarks)
        world = metrics['world_geometry']
        classification = classify_pushup_attempt(metrics)
        status = classification['status']
        reason = classification['reason']
        count = int(classification.get('count') or 0)
        confidence = round(metrics['usable_ratio'], 4)

        await asyncio.to_thread(
            lambda: db.client.table('pushup_attempts').update({
                'status': status,
                'pushup_count': count,
                'confidence': confidence,
                'rejection_reason': reason,
                'processed_at': datetime.now(timezone.utc).isoformat(),
                'processing_started_at': None,
                'last_error': None,
            }).eq('id', attempt_id).execute())

        if settings.pushup_results_enabled:
            total = await asyncio.to_thread(
                db.daily_pushup_total, chat_id, user_id,
                date.fromisoformat(day))
            if status == 'accepted':
                text = f"🏋️ Отжиманий: {count}\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {total}"
            elif status == 'rejected':
                text = f"❌ Отжимания не засчитаны\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {total}"
            else:
                text = f"⚠️ Не удалось уверенно распознать отжимания\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {total}"
            await application.bot.send_message(
                chat_id=chat_id, text=text, reply_to_message_id=message_id)

        logger.info('Push-up worker completed attempt_id=%s status=%s count=%s gated_count=%s',
                    attempt_id,status,count,world.get('gated_pushup_count',0))
    except Exception as exc:
        try:
            await asyncio.to_thread(db.retry_or_fail_pushup_attempt, attempt_id, type(exc).__name__)
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
