"""Sick leave management API for Telegram Mini App.

All reads and writes are scoped to the configured Telegram chat. Dates are inclusive.
"""
import asyncio
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from app.miniapp import authenticate

router = APIRouter()
log = logging.getLogger(__name__)


def _active_member(db, chat_id, uid):
    member = db.member(chat_id, uid)
    if not member or not member.get('active'):
        raise HTTPException(403, 'Участник не зарегистрирован')
    return member


def _doctor(db, chat_id, uid):
    _active_member(db, chat_id, uid)
    rows = (db.client.table('sick_leave_doctors').select('telegram_user_id')
            .eq('chat_id', chat_id).eq('telegram_user_id', uid).limit(1).execute().data)
    if not rows:
        raise HTTPException(403, 'Создавать больничные могут только врачи')


def _dates(payload):
    try:
        start = date.fromisoformat(payload['starts_on'])
        end = date.fromisoformat(payload['ends_on'])
        if end < start:
            raise ValueError()
        return start.isoformat(), end.isoformat()
    except (KeyError, ValueError, TypeError):
        raise HTTPException(400, 'Укажите корректные даты начала и окончания') from None


def _load(db, chat_id, leave_id):
    rows = (db.client.table('sick_leaves').select('*').eq('chat_id', chat_id)
            .eq('id', leave_id).is_('deleted_at', 'null').limit(1).execute().data)
    if not rows:
        raise HTTPException(404, 'Больничный не найден')
    return rows[0]


def _event_text(db, event):
    before = event.get('before_state') or {}
    after = event.get('after_state') or {}
    state = after or before
    patient = db.member(event['chat_id'], state['patient_id'])
    actor = db.member(event['chat_id'], event['actor_id'])
    name = (patient or {}).get('display_name') or str(state['patient_id'])
    doctor = (actor or {}).get('display_name') or str(event['actor_id'])
    def period(item):
        return f"{item['starts_on']} — {item['ends_on']}"
    kind = event['event_type']
    if kind == 'created':
        return f"🏥 Открыт больняк!\n🤒 Пациент: {name}\n📅 {period(after)}\n👨‍⚕️ Врач: {doctor}"
    if kind == 'updated':
        return f"🩺 Больняк изменён!\n🤒 Пациент: {name}\n📅 Было: {period(before)}\n📅 Стало: {period(after)}\n👨‍⚕️ Врач: {doctor}"
    if kind == 'deleted':
        return f"❌ Больняк аннулирован!\n🤒 Пациент: {name}\n📅 {period(before)}\n👨‍⚕️ Аннулировал: {doctor}"
    return f"🎉 {name} наконец-то выздоровел и возвращается к отжиманиям! 🏋️"


async def deliver_sick_leave_events(request, chat_id: int, event_ids=None):
    """Retryable outbox. Sequential delivery; a crash between send and ack may duplicate."""
    lock = getattr(request.app.state, 'sick_leave_delivery_lock', None)
    if lock is None:
        raise RuntimeError('Sick leave delivery lock is not initialized')
    async with lock:
        return await _deliver_sick_leave_events_locked(request, chat_id, event_ids)


async def _deliver_sick_leave_events_locked(request, chat_id: int, event_ids=None):
    db = request.app.state.db
    def load():
        query = (db.client.table('sick_leave_events').select('*')
                 .eq('chat_id', chat_id).is_('delivered_at', 'null')
                 .order('id').limit(50))
        if event_ids is not None:
            query = query.in_('id', event_ids)
        return query.execute().data
    events = await asyncio.to_thread(load)
    sent = 0
    for event in events:
        if event['event_type'] == 'recovered':
            def still_recovered():
                rows = (db.client.table('sick_leaves').select('ends_on,deleted_at,patient_id')
                        .eq('id', event['sick_leave_id']).eq('chat_id', chat_id)
                        .limit(1).execute().data)
                if not rows or rows[0]['deleted_at']:
                    return False
                from app.summaries import today_moscow
                from datetime import timedelta
                yesterday = (today_moscow() - timedelta(days=1)).isoformat()
                if rows[0]['ends_on'] != yesterday:
                    return False
                # A new or extended leave may cover the patient's return day.
                current = db.sick_leaves_for_day(chat_id, today_moscow())
                return int(rows[0]['patient_id']) not in current
            if not await asyncio.to_thread(still_recovered):
                # Do not send stale recovery notices after extensions/deletions.
                await asyncio.to_thread(lambda: db.client.table('sick_leave_events')
                    .update({'delivered_at': datetime.now(timezone.utc).isoformat()})
                    .eq('id', event['id']).execute())
                continue
        text = await asyncio.to_thread(_event_text, db, event)
        try:
            await request.app.state.telegram.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception('Sick leave outbox delivery failed: event=%s', event['id'])
            await asyncio.to_thread(lambda: db.client.table('sick_leave_events')
                .update({'delivery_attempts': event['delivery_attempts'] + 1})
                .eq('id', event['id']).execute())
            break
        await asyncio.to_thread(lambda: db.client.table('sick_leave_events')
            .update({'delivered_at': datetime.now(timezone.utc).isoformat(),
                     'delivery_attempts': event['delivery_attempts'] + 1})
            .eq('id', event['id']).execute())
        sent += 1
    return sent


async def _notify(request, chat_id, db, event, before, after, actor):
    # Audit trigger creates outbox event in the same database transaction.
    # Attempt immediate delivery; failed sends remain queued for cron retry.
    try:
        await deliver_sick_leave_events(request, chat_id)
        return True
    except Exception:
        log.exception('Sick leave event queued but immediate delivery failed')
        return False


@router.get('/api/miniapp/sick-leaves')
async def list_sick_leaves(request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    def load():
        _active_member(db, chat_id, uid)
        doctors = (db.client.table('sick_leave_doctors').select('telegram_user_id')
                   .eq('chat_id', chat_id).execute().data)
        leaves = (db.client.table('sick_leaves')
                  .select('id,patient_id,starts_on,ends_on,created_by,updated_at')
                  .eq('chat_id', chat_id).is_('deleted_at', 'null')
                  .order('starts_on', desc=True).execute().data)
        return {'doctors': [int(r['telegram_user_id']) for r in doctors],
                'leaves': leaves, 'is_doctor': uid in {int(r['telegram_user_id']) for r in doctors}}
    return await asyncio.to_thread(load)


@router.put('/api/miniapp/sick-leaves/doctors')
async def set_doctors(request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    if uid != request.app.state.settings.admin_telegram_id:
        raise HTTPException(403, 'Назначать врачей может только администратор')
    payload = await request.json()
    ids = payload.get('doctor_ids')
    if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int for i in ids):
        raise HTTPException(400, 'doctor_ids должен быть массивом идентификаторов участников')
    ids = list(dict.fromkeys(ids))
    def save():
        _active_member(db, chat_id, uid)
        members = {int(m['telegram_user_id']) for m in db.members(chat_id)}
        if not set(ids) <= members:
            raise HTTPException(400, 'Можно назначать только активных участников')
        existing = (db.client.table('sick_leave_doctors').select('telegram_user_id')
                    .eq('chat_id', chat_id).execute().data)
        current = {int(row['telegram_user_id']) for row in existing}
        additions = set(ids) - current
        removals = current - set(ids)
        # Insert first so an insertion error never removes existing doctors.
        if additions:
            db.client.table('sick_leave_doctors').insert([
                {'chat_id': chat_id, 'telegram_user_id': doctor, 'appointed_by': uid}
                for doctor in sorted(additions)]).execute()
        if removals:
            (db.client.table('sick_leave_doctors').delete().eq('chat_id', chat_id)
             .in_('telegram_user_id', list(removals)).execute())
    await asyncio.to_thread(save)
    return {'doctor_ids': ids}


@router.post('/api/miniapp/sick-leaves')
async def create_sick_leave(request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    payload = await request.json()
    start, end = _dates(payload)
    patient_id = payload.get('patient_id')
    if type(patient_id) is not int:
        raise HTTPException(400, 'Выберите пациента')
    def save():
        _doctor(db, chat_id, uid)
        actor = _active_member(db, chat_id, uid)
        _active_member(db, chat_id, patient_id)
        rows = db.client.table('sick_leaves').insert({
            'chat_id': chat_id, 'patient_id': patient_id, 'starts_on': start,
            'ends_on': end, 'created_by': uid}).execute().data
        return actor, rows[0]
    try:
        actor, row = await asyncio.to_thread(save)
    except HTTPException:
        raise
    except Exception:
        log.exception('Sick leave creation failed')
        raise HTTPException(409, 'Не удалось создать больничный. Проверьте пересечение дат') from None
    notified = await _notify(request, chat_id, db, 'created', None, row, actor)
    return {'leave': row, 'notified': notified}


@router.patch('/api/miniapp/sick-leaves/{leave_id}')
async def update_sick_leave(leave_id: int, request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    payload = await request.json()
    start, end = _dates(payload)
    def save():
        _doctor(db, chat_id, uid)
        actor = _active_member(db, chat_id, uid)
        old = _load(db, chat_id, leave_id)
        if (old['starts_on'], old['ends_on']) == (start, end):
            return actor, old, old
        rows = (db.client.table('sick_leaves')
                .update({'starts_on': start, 'ends_on': end, 'updated_by': uid,
                         'updated_at': datetime.now(timezone.utc).isoformat()})
                .eq('chat_id', chat_id).eq('id', leave_id).is_('deleted_at', 'null')
                .execute().data)
        return actor, old, rows[0]
    try:
        actor, old, row = await asyncio.to_thread(save)
    except HTTPException:
        raise
    except Exception:
        log.exception('Sick leave update failed')
        raise HTTPException(409, 'Не удалось изменить больничный. Проверьте пересечение дат') from None
    notified = True if old == row else await _notify(request, chat_id, db, 'updated', old, row, actor)
    return {'leave': row, 'notified': notified}


@router.delete('/api/miniapp/sick-leaves/{leave_id}')
async def delete_sick_leave(leave_id: int, request: Request, authorization: str | None = Header(None)):
    uid, chat_id, db = authenticate(request, authorization)
    def save():
        _doctor(db, chat_id, uid)
        actor = _active_member(db, chat_id, uid)
        old = _load(db, chat_id, leave_id)
        db.client.table('sick_leaves').update({
            'deleted_at': datetime.now(timezone.utc).isoformat(), 'deleted_by': uid}).eq('chat_id', chat_id).eq('id', leave_id).execute()
        return actor, old
    actor, old = await asyncio.to_thread(save)
    notified = await _notify(request, chat_id, db, 'deleted', old, None, actor)
    return {'ok': True, 'notified': notified}
