import asyncio
import hashlib
import json
import signal
import sys
import logging
import os
import tempfile
import time
from datetime import date, datetime, timezone

from app.admin_alerts import send_admin_alert
from app.pushup_diagnostics import format_diagnostics
from app.pose_analysis import analyze_pose_visibility
from app.pushup_classification import classify_pushup_attempt
from app.summaries import rank_for_user, promotion_message, rival_challenge, overtaken_message
from app.safe_logging import log_exception
from app.world_pose_analysis import analyze_world_landmarks

logger = logging.getLogger(__name__)

def _memory_mb():
    """Linux process RSS and high-water RSS; no additional dependencies."""
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            fields = {}
            for line in f:
                if line.startswith(('VmRSS:', 'VmHWM:')):
                    key, value = line.split(':', 1)
                    fields[key] = round(int(value.strip().split()[0]) / 1024, 1)
            return fields.get('VmRSS', 0), fields.get('VmHWM', 0)
    except (OSError, ValueError, IndexError):
        return 0, 0


async def _sample_memory(attempt_id, readings):
    while True:
        rss, hwm = _memory_mb()
        readings.append(rss)
        logger.info('Pushup resource sample attempt=%s rss_mb=%.1f hwm_mb=%.1f', attempt_id, rss, hwm)
        await asyncio.sleep(1)




async def _isolated_pose_analysis(video_path, attempt_id, timeout=120):
    """Run one CPU-heavy video in a separate process, reaping it even on timeout."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'app.pose_subprocess', video_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    peak_child_mb = 0.0
    async def monitor():
        nonlocal peak_child_mb
        while proc.returncode is None:
            try:
                with open(f'/proc/{proc.pid}/status', encoding='utf-8') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            peak_child_mb = max(peak_child_mb, int(line.split()[1])/1024)
                            break
            except (OSError, ValueError, IndexError):
                pass
            await asyncio.sleep(.5)
    task = asyncio.create_task(monitor())
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f'Pose analyzer exceeded {timeout}s') from exc
        if proc.returncode:
            raise RuntimeError(f'Pose analyzer exited {proc.returncode}: {stderr.decode(errors="replace")[-1200:]}')
        return json.loads(stdout)
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info('Pushup isolated child attempt=%s child_peak_rss_mb=%.1f child_exit=%s',
                    attempt_id, peak_child_mb, proc.returncode)


async def process_attempt(application, settings, db, attempt):
    attempt_id = attempt['id']
    chat_id = attempt['chat_id']
    user_id = attempt['telegram_user_id']
    message_id = attempt['telegram_message_id']
    day = attempt['report_date']
    temp_path = None
    started = time.monotonic()
    rss_start, _ = _memory_mb()
    readings = []
    sampler = asyncio.create_task(_sample_memory(attempt_id, readings))
    logger.info('Pushup resource start attempt=%s rss_mb=%.1f', attempt_id, rss_start)
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

        analysis_started = time.monotonic()
        metrics = await _isolated_pose_analysis(temp_path, attempt_id)
        logger.info('Pushup analysis finished attempt=%s analysis_sec=%.2f rss_mb=%.1f', attempt_id, time.monotonic()-analysis_started, _memory_mb()[0])
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

        medal_before = None
        best_before = 0
        totals_before = {}
        members_before = []
        if status == 'accepted' and count > 0:
            members_before = await asyncio.to_thread(db.members, chat_id)
            totals_before = await asyncio.to_thread(db.pushup_totals, chat_id, date.fromisoformat(day))
            medal_before = rank_for_user(members_before, totals_before, user_id)
            best_before = await asyncio.to_thread(db.personal_best, chat_id, user_id)

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

        medal_notice = None
        reward_notices = []
        if status == 'accepted' and count > 0:
            members_after = await asyncio.to_thread(db.members, chat_id)
            totals_after = await asyncio.to_thread(db.pushup_totals, chat_id, date.fromisoformat(day))
            medal_after = rank_for_user(members_after, totals_after, user_id)
            medal_notice = promotion_message(medal_before, medal_after)
            if count > best_before:
                reward_notices.append(
                    f"🏆 Новый личный рекорд за подход: {count} отж.!"
                    + (f" Предыдущий — {best_before}." if best_before else " Первый рекорд!"))
            takeover = overtaken_message(totals_before, totals_after, members_after, user_id)
            if takeover:
                reward_notices.append(takeover)
            target = rival_challenge(members_after, totals_after, user_id)
            if target:
                reward_notices.append(target)

        if settings.pushup_diagnostics_enabled and chat_id == settings.telegram_chat_id:
            report = format_diagnostics(attempt_id, message_id, metrics, classification, time.monotonic()-started)
            for chunk in report:
                await application.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id)
            notices = ([medal_notice] if medal_notice else []) + reward_notices[:2]
            if notices:
                await application.bot.send_message(chat_id=chat_id, text='\n'.join(notices), reply_to_message_id=message_id)

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
            notices = ([medal_notice] if medal_notice else []) + reward_notices[:2]
            if notices:
                text += '\n\n' + '\n'.join(notices)
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
        sampler.cancel()
        try:
            await sampler
        except asyncio.CancelledError:
            pass
        rss_end, hwm = _memory_mb()
        logger.info('Pushup resource end attempt=%s duration_sec=%.2f rss_start_mb=%.1f rss_end_mb=%.1f rss_sample_peak_mb=%.1f process_hwm_mb=%.1f', attempt_id, time.monotonic()-started, rss_start, rss_end, max(readings, default=0), hwm)
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
