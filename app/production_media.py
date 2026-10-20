"""Production Mini App media archive. Video I/O happens outside the pose worker."""
import asyncio
import logging
import os
import tempfile
from datetime import timedelta
from app.summaries import today_moscow

logger = logging.getLogger(__name__)
BUCKET = 'pushup-production-media'


def _set_status(db, attempt_id, status, path=None):
    fields = {'video_status': status}
    if path is not None:
        fields['video_storage_path'] = path
    db.client.table('pushup_attempts').update(fields).eq('id', attempt_id).execute()


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
            with open(local, 'rb') as source:
                db.client.storage.from_(BUCKET).upload(
                    path, source, {'content-type': 'video/mp4', 'upsert': 'true'})
            _set_status(db, aid, 'ready', path)
        await asyncio.to_thread(upload)
    except Exception:
        logger.exception('Production video archive failed attempt=%s', aid)
        await asyncio.to_thread(_set_status, db, aid, 'failed')
    finally:
        if local:
            try:
                os.unlink(local)
            except OSError:
                pass


def _pending(db, cutoff):
    return (db.client.table('pushup_attempts')
            .select('id,chat_id,telegram_file_id,report_date')
            .gte('report_date', cutoff.isoformat())
            .in_('status', ['accepted', 'rejected', 'uncertain'])
            .eq('video_status', 'pending')
            .not_.is_('telegram_file_id', 'null')
            .order('id').limit(5).execute().data)


def _expired(db, cutoff):
    return (db.client.table('pushup_attempts')
            .select('id,video_storage_path')
            .lt('report_date', cutoff.isoformat())
            .not_.is_('video_storage_path', 'null')
            .limit(50).execute().data)


def _cleanup(db, rows):
    for row in rows:
        path = row['video_storage_path']
        db.client.storage.from_(BUCKET).remove([path])
        db.client.table('pushup_attempts').update({
            'video_status': 'expired', 'video_storage_path': None
        }).eq('id', row['id']).eq('video_storage_path', path).execute()


async def media_loop(bot, db):
    """Independent low-concurrency archival loop; failures never block results."""
    while True:
        try:
            cutoff = today_moscow() - timedelta(days=4)
            expired = await asyncio.to_thread(_expired, db, cutoff)
            if expired:
                await asyncio.to_thread(_cleanup, db, expired)
            pending = await asyncio.to_thread(_pending, db, cutoff)
            for attempt in pending:
                await _archive_one(bot, db, attempt)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Staging media loop error')
        await asyncio.sleep(30)
