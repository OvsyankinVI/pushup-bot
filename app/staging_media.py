"""Staging-only media archive. Video I/O happens outside the pose worker."""
import asyncio
import logging
import os
import tempfile
import json
import sys
import time
from datetime import datetime, timezone
from datetime import timedelta
from app.summaries import today_moscow

logger = logging.getLogger(__name__)
BUCKET = 'pushup-staging-media'


def _set_status(db, attempt_id, status, path=None):
    fields = {'video_status': status}
    if path is not None:
        fields['video_storage_path'] = path
    db.client.table(db.attempt_table).update(fields).eq('id', attempt_id).execute()


async def _archive_one(bot, db, attempt):
    aid = attempt['id']
    path = f"videos/{attempt['chat_id']}/{aid}.mp4"
    local = None
    try:
        f = await bot.get_file(attempt['telegram_file_id'])
        with tempfile.NamedTemporaryFile(prefix='archive_', suffix='.mp4', delete=False) as handle:
            local = handle.name
        await f.download_to_drive(custom_path=local)
        def upload():
            import httpx
            for upload_attempt in range(1, 5):
                try:
                    with open(local, 'rb') as source:
                        db.client.storage.from_(BUCKET).upload(
                            path, source, {'content-type': 'video/mp4', 'upsert': 'true'})
                    break
                except httpx.TransportError:
                    if upload_attempt == 4:
                        raise
                    logger.warning('Staging original upload transport retry attempt=%s try=%s', aid, upload_attempt, exc_info=True)
                    time.sleep(min(2 ** upload_attempt, 8))
            _set_status(db, aid, 'ready', path)
            db.client.table(db.attempt_table).update({'overlay_queued_at':datetime.now(timezone.utc).isoformat()}).eq('id',aid).is_('overlay_queued_at','null').execute()
        await asyncio.to_thread(upload)
    except Exception:
        logger.exception('Staging video archive failed attempt=%s', aid)
        await asyncio.to_thread(_set_status, db, aid, 'failed')
    finally:
        if local:
            try:
                os.unlink(local)
            except OSError:
                pass


def _pending(db, cutoff):
    return (db.client.table(db.attempt_table)
            .select('id,chat_id,telegram_file_id,report_date')
            .gte('report_date', cutoff.isoformat())
            .in_('status', ['accepted', 'rejected', 'uncertain'])
            .eq('video_status', 'pending')
            .not_.is_('telegram_file_id', 'null')
            .order('id').limit(5).execute().data)


def _expired(db, cutoff):
    return (db.client.table(db.attempt_table)
            .select('id,video_storage_path,overlay_video_path')
            .lt('report_date', cutoff.isoformat())
            .not_.is_('video_storage_path', 'null')
            .limit(50).execute().data)


def _cleanup(db, rows):
    for row in rows:
        path = row['video_storage_path']
        db.client.storage.from_(BUCKET).remove([p for p in (path,row.get('overlay_video_path')) if p])
        db.client.table(db.attempt_table).update({
            'video_status': 'expired', 'video_storage_path': None,
            'overlay_video_status':'expired','overlay_video_path':None
        }).eq('id', row['id']).eq('video_storage_path', path).execute()



def _overlay_candidates(db, cutoff):
    return (db.client.table(db.attempt_table)
            .select('id,video_storage_path')
            .gte('report_date',cutoff.isoformat())
            .eq('video_status','ready')
            .eq('overlay_video_status','pending')
            .not_.is_('video_storage_path','null')
            .order('id').limit(1).execute().data)


def _render_overlay(db, row):
    """Separate child process; no pose inference and no bot interaction."""
    aid=row['id']
    # Do not compete with recognition for memory.
    active=(db.client.table(db.attempt_table).select('id')
            .eq('status','processing').limit(1).execute().data)
    if active:return
    diagnostic=(db.client.table('staging_pushup_overlay_diagnostics')
                .select('trace,classification').eq('attempt_id',aid).limit(1).execute().data)
    if not diagnostic:
        db.client.table(db.attempt_table).update({'overlay_video_status':'unavailable'}).eq('id',aid).execute()
        return
    import subprocess
    started=datetime.now(timezone.utc)
    start_clock=time.monotonic()
    db.client.table(db.attempt_table).update({
        'overlay_started_at':started.isoformat()
    }).eq('id',aid).execute()
    with tempfile.TemporaryDirectory(prefix='overlay_') as folder:
        src=os.path.join(folder,'original.mp4')
        dst=os.path.join(folder,'annotated.mp4')
        info=os.path.join(folder,'trace.json')
        original=db.client.storage.from_(BUCKET).download(row['video_storage_path'])
        with open(src,'wb') as f:f.write(original)
        del original
        with open(info,'w',encoding='utf-8') as f:json.dump(diagnostic[0],f)
        proc=subprocess.run([sys.executable,'-m','app.overlay_render',src,dst,info],
                            capture_output=True,text=True,timeout=120,check=True)
        if not os.path.isfile(dst) or os.path.getsize(dst)==0:
            raise RuntimeError('Empty annotated video')
        target=f"annotated/{aid}.mp4"
        # Transient HTTP/2 transport errors must not permanently lose an overlay.
        for upload_attempt in range(1, 4):
            try:
                with open(dst, 'rb') as f:
                    db.client.storage.from_(BUCKET).upload(
                        target, f, {'content-type': 'video/mp4', 'upsert': 'true'})
                break
            except Exception:
                if upload_attempt == 3:
                    raise
                logger.warning('Staging overlay upload retry attempt=%s try=%s', aid, upload_attempt, exc_info=True)
                time.sleep(2 * upload_attempt)
        db.client.table(db.attempt_table).update({
            'overlay_video_path':target,'overlay_video_status':'ready',
            'overlay_error':None,
            'overlay_finished_at':datetime.now(timezone.utc).isoformat(),
            'overlay_render_seconds':round(time.monotonic()-start_clock,3)
        }).eq('id',aid).execute()
        logger.info('Staging overlay complete attempt=%s result=%s',aid,proc.stdout.strip())


async def _process_overlay(db, cutoff, analysis_lock=None):
    rows=await asyncio.to_thread(_overlay_candidates,db,cutoff)
    for row in rows:
        try:
            if analysis_lock is None:
                await asyncio.to_thread(_render_overlay,db,row)
            elif not analysis_lock.locked():
                async with analysis_lock:
                    # Prefer queued recognitions; no new MediaPipe job is claimed during rendering.
                    queued = await asyncio.to_thread(lambda: db.client.table(db.attempt_table).select('id').eq('status','pending').limit(1).execute().data)
                    if queued:
                        return
                    await asyncio.to_thread(_render_overlay,db,row)
            else:
                return
        except Exception as exc:
            logger.exception('Staging overlay render failed attempt=%s', row['id'])
            # Supabase Storage can briefly fail with httpx/httpcore transport errors
            # even after the upload's local retries. Requeue at most twice more.
            import httpx
            if isinstance(exc, httpx.TransportError):
                def retry_transport():
                    state = (db.client.table(db.attempt_table)
                             .select('overlay_error').eq('id', row['id'])
                             .limit(1).execute().data)
                    previous = (state[0].get('overlay_error') or '') if state else ''
                    count = int(previous.rsplit('_', 1)[-1]) if previous.startswith('transport_retry_') else 0
                    if count < 2:
                        db.client.table(db.attempt_table).update({
                            'overlay_video_status': 'pending',
                            'overlay_finished_at': None,
                            'overlay_error': f'transport_retry_{count + 1}'
                        }).eq('id', row['id']).execute()
                        logger.warning('Requeued staging overlay after transport error attempt=%s retry=%s', row['id'], count + 1)
                        return
                    db.client.table(db.attempt_table).update({
                        'overlay_video_status': 'failed',
                        'overlay_finished_at': datetime.now(timezone.utc).isoformat(),
                        'overlay_error': 'transport_retries_exhausted'
                    }).eq('id', row['id']).execute()
                await asyncio.to_thread(retry_transport)
            else:
                await asyncio.to_thread(lambda: db.client.table(db.attempt_table).update({
                    'overlay_video_status':'failed',
                    'overlay_finished_at':datetime.now(timezone.utc).isoformat(),
                    'overlay_error':'render_failed_see_logs'
                }).eq('id',row['id']).execute())


async def media_loop(bot, db, analysis_lock=None):
    """Independent low-concurrency archival loop; failures never block results."""
    while True:
        try:
            cutoff = today_moscow() - timedelta(days=4)
            expired = await asyncio.to_thread(_expired, db, cutoff)
            if expired:
                await asyncio.to_thread(_cleanup, db, expired)
            await _process_overlay(db, cutoff, analysis_lock)
            pending = await asyncio.to_thread(_pending, db, cutoff)
            for attempt in pending:
                await _archive_one(bot, db, attempt)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Staging media loop error')
        await asyncio.sleep(30)
