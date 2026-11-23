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


async def _notify(request, chat_id, db, event, before, after, actor):
    patient_id = (after or before)['patient_id']
    patient = await asyncio.to_thread(db.member, chat_id, patient_id)
    name = (patient or {}).get('display_name') or str(patient_id)
    actor_name = actor['display_name']
    def period(row):
        return f"{row['starts_on']} — {row['ends_on']}"
    if event == 'created':
        message = f'🏥 Открыт больняк!\n🤒 Пациент: {name}\n📅 {period(after)}\n👨‍⚕️ Врач: {actor_name}'
    elif event == 'updated':
        message = f'🩺 Больняк изменён!\n🤒 Пациент: {name}\n📅 Было: {period(before)}\n📅 Стало: {period(after)}\n👨‍⚕️ Врач: {actor_name}'
    else:
        message = f'❌ Больняк аннулирован!\n🤒 Пациент: {name}\n📅 {period(before)}\n👨‍⚕️ Аннулировал: {actor_name}'
    try:
        await request.app.state.telegram.bot.send_message(chat_id=chat_id, text=message)
        return True
    except Exception:
        log.exception('Sick leave notification failed')
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
        db.client.table('sick_leave_doctors').delete().eq('chat_id', chat_id).execute()
        if ids:
            db.client.table('sick_leave_doctors').insert([
                {'chat_id': chat_id, 'telegram_user_id': doctor, 'appointed_by': uid}
                for doctor in ids]).execute()
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
