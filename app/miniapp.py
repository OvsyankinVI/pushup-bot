import asyncio
import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Request, Query
from fastapi.responses import FileResponse
BUCKET = 'pushup-production-media'
from app.summaries import today_moscow

router = APIRouter()


@router.get('/miniapp/assets/{asset_name}')
async def miniapp_asset(asset_name: str):
    allowed = {'medals.svg', 'pushup-athlete.svg', 'character-light.webp', 'character-dark.webp', 'medal-gold.webp', 'medal-silver.webp', 'medal-bronze.webp', 'crown.webp'}
    if asset_name not in allowed:
        raise HTTPException(404, 'Asset not found')
    asset_path = Path(__file__).resolve().parent.parent / 'miniapp' / 'assets' / asset_name
    return FileResponse(asset_path, media_type='image/webp' if asset_name.endswith('.webp') else 'image/svg+xml', headers={'Cache-Control': 'public, max-age=3600'})

@router.get('/miniapp')
async def page():
    return FileResponse(Path(__file__).resolve().parent.parent / 'miniapp' / 'index.html', headers={'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0', 'Pragma': 'no-cache', 'Expires': '0'})

def telegram_user(raw, token):
    try:
        fields = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
        signature = fields.pop('hash')
        signed = '\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
        key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('signature')
        if abs(time.time() - int(fields['auth_date'])) > 86400:
            raise ValueError('expired')
        return int(json.loads(fields['user'])['id'])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, 'Открой приложение через Telegram') from None


def authenticate(request, authorization):
    raw = (authorization or '').removeprefix('tma ').strip()
    if not raw or len(raw) > 8192:
        raise HTTPException(401, 'Открой приложение через Telegram')
    uid = telegram_user(raw, request.app.state.settings.telegram_bot_token)
    chat_id = request.app.state.settings.telegram_chat_id
    if chat_id is None:
        raise HTTPException(503, 'Не настроен чат Telegram')
    return uid, chat_id, request.app.state.db


def format_attempt(row):
    stamp = row.get('created_at')
    row['time'] = (datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                   .astimezone(ZoneInfo('Europe/Moscow')).strftime('%H:%M')) if stamp else '—'
    row['manual_adjustment'] = int(row.get('manual_adjustment') or 0)
    row['pushup_count'] = int(row.get('pushup_count') or 0)
    return row


@router.get('/api/miniapp')
async def data(request: Request, authorization: str | None = Header(None),
               selected_date: str | None = Query(None), participant_id: int | None = Query(None)):
    user_id, chat_id, db = authenticate(request, authorization)
    day = today_moscow()
    try:
        chosen = date.fromisoformat(selected_date) if selected_date else day
    except ValueError:
        raise HTTPException(400, 'Неверная дата') from None
    if chosen > day:
        raise HTTPException(400, 'Нельзя выбрать будущую дату')

    def load():
        members, reported, _ = db.summary_data(chat_id, day)
        ids = {int(m['telegram_user_id']) for m in members}
        if user_id not in ids:
            return None
        viewed_id = participant_id if participant_id in ids else user_id
        rows = []
        offset = 0
        while True:
            batch = (db.client.table('pushup_attempts')
                     .select('id,telegram_user_id,report_date,pushup_count,manual_adjustment,created_at,video_storage_path,video_status')
                     .eq('chat_id', chat_id).eq('status', 'accepted')
                     .order('id').range(offset, offset + 499).execute().data)
            rows.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        from collections import defaultdict
        from datetime import timedelta
        base_all, manual_all = defaultdict(int), defaultdict(int)
        base_month, manual_month = defaultdict(int), defaultdict(int)
        base_week, manual_week = defaultdict(int), defaultdict(int)
        base_selected, manual_selected = defaultdict(int), defaultdict(int)
        daily, daily_manual, weeks = defaultdict(int), defaultdict(int), defaultdict(int)
        best_single = 0
        for a in rows:
            uid = int(a['telegram_user_id'])
            n = max(0, int(a.get('pushup_count') or 0))
            extra = max(0, int(a.get('manual_adjustment') or 0))
            d = date.fromisoformat(a['report_date'])
            base_all[uid] += n
            manual_all[uid] += extra
            if d.strftime('%Y-%m') == day.strftime('%Y-%m'):
                base_month[uid] += n
                manual_month[uid] += extra
            if d >= day - timedelta(days=day.weekday()) and d <= day:
                base_week[uid] += n
                manual_week[uid] += extra
            if d == chosen:
                base_selected[uid] += n
                manual_selected[uid] += extra
            if uid == user_id:
                daily[d.isoformat()] += n
                daily_manual[d.isoformat()] += extra
                week_start = d - timedelta(days=d.weekday())
                weeks[week_start] += n + extra
                best_single = max(best_single, n + extra)
        photo_rows = (db.client.table('member_photos').select('telegram_user_id,storage_path')
                      .eq('chat_id', chat_id).execute().data)
        photo_urls = {}
        for item in photo_rows:
            try:
                signed = db.client.storage.from_(BUCKET).create_signed_url(item['storage_path'], 3600)
                photo_urls[str(item['telegram_user_id'])] = signed.get('signedURL') or signed.get('signedUrl')
            except Exception:
                pass
        def attempts_for(uid):
            return [format_attempt(dict(a)) for a in rows
                    if int(a['telegram_user_id']) == uid and a['report_date'] == chosen.isoformat()]
        def video_links(items):
            for a in items:
                if a['report_date'] < (day - timedelta(days=4)).isoformat():
                    a['video_status'] = 'expired'
                elif a.get('video_status') == 'ready' and a.get('video_storage_path'):
                    try:
                        video_path = (a.get('overlay_video_path') if a.get('overlay_video_status') == 'ready' else None) or a['video_storage_path']
                        signed = db.client.storage.from_(BUCKET).create_signed_url(video_path, 900)
                        a['video_annotated'] = bool(a.get('overlay_video_status') == 'ready' and a.get('overlay_video_path'))
                        a['video_url'] = signed.get('signedURL') or signed.get('signedUrl')
                        if not a['video_url']:
                            a['video_status'] = 'failed'
                    except Exception:
                        a['video_status'] = 'failed'
                a.pop('video_storage_path', None)
                a.pop('overlay_video_path', None)
            return items
        own_attempts = video_links(attempts_for(user_id))
        viewed_attempts = video_links(attempts_for(viewed_id))
        return {
            'user': {'id': user_id, 'name': next(m['display_name'] for m in members if int(m['telegram_user_id']) == user_id),
                     'username': next((m.get('username') for m in members if int(m['telegram_user_id']) == user_id), None)},
            'date': day.isoformat(), 'selected_date': chosen.isoformat(),
            'attempts': own_attempts, 'viewed_attempts': viewed_attempts, 'viewed_id': viewed_id,
            'selected_total': base_selected[user_id] + manual_selected[user_id],
            'selected_base': base_selected[user_id], 'selected_manual': manual_selected[user_id],
            'photo_urls': photo_urls,
            'members': [{'id': int(m['telegram_user_id']), 'name': m['display_name'],
                         'reported': int(m['telegram_user_id']) in reported} for m in members],
            'today': dict(base_selected), 'today_manual': dict(manual_selected),
            'week': dict(base_week), 'week_manual': dict(manual_week),
            'month': dict(base_month), 'month_manual': dict(manual_month),
            'all': dict(base_all), 'all_manual': dict(manual_all),
            'stats': {'total': base_all[user_id] + manual_all[user_id],
                      'this_week': base_week[user_id] + manual_week[user_id],
                      'best_week': max(weeks.values(), default=0), 'best_single': best_single},
            'daily': dict(daily), 'daily_manual': dict(daily_manual)
        }

    result = await asyncio.to_thread(load)
    if result is None:
        raise HTTPException(403, 'Сначала зарегистрируйся в чате через /join')
    return result


@router.post('/api/miniapp/adjustment')
async def adjust(request: Request, authorization: str | None = Header(None)):
    user_id, chat_id, db = authenticate(request, authorization)
    try:
        payload = await request.json()
        attempt_id = payload.get('attempt_id')
        adjustment = payload.get('manual_adjustment')
        if type(attempt_id) is not int or type(adjustment) is not int or not (0 <= adjustment <= 30):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, 'Корректировка должна быть целым числом от 0 до 30') from None

    def save():
        if not db.member(chat_id, user_id):
            raise ValueError('Участник не найден')
        rows = (db.client.table('pushup_attempts')
                .select('manual_adjustment').eq('id', attempt_id).eq('chat_id', chat_id)
                .eq('telegram_user_id', user_id).eq('status', 'accepted').limit(1).execute().data)
        if not rows:
            raise ValueError('Можно корректировать только свои засчитанные подходы')
        previous = int(rows[0].get('manual_adjustment') or 0)
        updated = db.client.rpc('update_pushup_adjustment', {
            'p_attempt_id': attempt_id, 'p_chat_id': chat_id,
            'p_user_id': user_id, 'p_new_adjustment': adjustment
        }).execute().data
        if not updated:
            raise ValueError('Не удалось обновить подход')
        return previous, updated[0]
    try:
        previous, row = await asyncio.to_thread(save)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    notified = True
    if previous != adjustment:
        member = await asyncio.to_thread(db.member, chat_id, user_id)
        name = (member or {}).get('display_name') or str(user_id)
        bot_count = int(row['bot_count'])
        day = row['report_day']
        text = (f"✏️ {name} изменил(а) корректировку подхода за {day}\n"
                f"🤖 Бот: {bot_count} отж.\n"
                f"📝 Вручную: +{previous} → +{adjustment}\n"
                f"💪 Теперь: {bot_count} (+{adjustment}) = {bot_count + adjustment}")
        try:
            await request.app.state.telegram.bot.send_message(
                chat_id=chat_id, text=text, reply_to_message_id=int(row['telegram_message']))
        except Exception:
            try:
                await request.app.state.telegram.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                notified = False
    return {'ok': True, 'notified': notified, 'manual_adjustment': adjustment}


@router.put('/api/miniapp/avatar')
async def upload_avatar(request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if mime not in ('image/jpeg', 'image/png', 'image/webp'):
        raise HTTPException(415, 'Используй JPG, PNG или WebP')
    if request.headers.get('content-length') and int(request.headers['content-length']) > 2_000_000:
        raise HTTPException(413, 'Фото должно быть меньше 2 МБ')
    payload = await request.body()
    if not payload or len(payload) > 2_000_000:
        raise HTTPException(413, 'Фото должно быть меньше 2 МБ')
    if not (payload.startswith(b'\xff\xd8\xff') if mime == 'image/jpeg' else
            payload.startswith(b'\x89PNG\r\n\x1a\n') if mime == 'image/png' else
            payload.startswith(b'RIFF') and payload[8:12] == b'WEBP'):
        raise HTTPException(400, 'Неверный формат изображения')
    def save():
        if not db.member(chat_id, uid):
            raise ValueError('Участник не найден')
        suffix = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[mime]
        path = f'avatars/{chat_id}/{uid}/{int(time.time()*1000)}.{suffix}'
        db.client.storage.from_(BUCKET).upload(path, payload, {'content-type': mime})
        old = (db.client.table('member_photos').select('storage_path')
               .eq('chat_id', chat_id).eq('telegram_user_id', uid).limit(1).execute().data)
        db.client.table('member_photos').upsert({
            'chat_id': chat_id, 'telegram_user_id': uid, 'storage_path': path,
            'updated_at': datetime.now(ZoneInfo('UTC')).isoformat()
        }, on_conflict='chat_id,telegram_user_id').execute()
        if old and old[0]['storage_path'] != path:
            try:
                db.client.storage.from_(BUCKET).remove([old[0]['storage_path']])
            except Exception:
                pass
        signed = db.client.storage.from_(BUCKET).create_signed_url(path, 3600)
        return signed.get('signedURL') or signed.get('signedUrl')
    try:
        url = await asyncio.to_thread(save)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    return {'ok': True, 'photo_url': url}
