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
from app.summaries import today_moscow

router = APIRouter()

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

@router.get('/api/miniapp')
async def data(request: Request, authorization: str | None = Header(None), selected_date: str | None = Query(None)):
    raw = (authorization or '').removeprefix('tma ').strip()
    if not raw or len(raw)>8192:
        raise HTTPException(401,'Открой приложение через Telegram')
    user_id = telegram_user(raw,request.app.state.settings.telegram_bot_token)
    chat_id = request.app.state.settings.telegram_chat_id
    if chat_id is None:
        raise HTTPException(503,'Не настроен тестовый чат')
    db = request.app.state.db
    day = today_moscow()
    try:
        chosen = date.fromisoformat(selected_date) if selected_date else day
    except ValueError:
        raise HTTPException(400, 'Неверная дата') from None
    if chosen > day:
        raise HTTPException(400, 'Нельзя выбрать будущую дату')
    def load():
        members, reported, today = db.summary_data(chat_id,day)
        member = next((m for m in members if int(m['telegram_user_id'])==user_id),None)
        if member is None:
            return None
        attempts = (db.client.table(db.attempt_table)
                    .select('id,pushup_count,created_at,processed_at')
                    .eq('chat_id',chat_id).eq('telegram_user_id',user_id)
                    .eq('report_date',chosen.isoformat()).eq('status','accepted')
                    .order('created_at').execute().data)
        # Telegram submission time, converted to Moscow time for display.
        for attempt in attempts:
            stamp = attempt.get('created_at')
            attempt['time'] = datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%H:%M') if stamp else '—'
        week = db.weekly_pushup_totals(chat_id,day-timedelta(days=day.weekday()))
        stats = db.personal_stats(chat_id,user_id,day)
        totals = {}
        monthly = {}
        daily = {}
        offset = 0
        while True:
            rows=(db.client.table(db.attempt_table)
                  .select('telegram_user_id,report_date,pushup_count')
                  .eq('chat_id',chat_id).eq('status','accepted')
                  .order('id').range(offset,offset+499).execute().data)
            for row in rows:
                uid=int(row['telegram_user_id'])
                n=max(0,int(row.get('pushup_count') or 0))
                totals[uid]=totals.get(uid,0)+n
                if row['report_date'][:7] == day.isoformat()[:7]:
                    monthly[uid]=monthly.get(uid,0)+n
                if uid==user_id:
                    date=row['report_date']
                    daily[date]=daily.get(date,0)+n
            if len(rows)<500: break
            offset+=500
        return {'user':{'id':user_id,'name':member['display_name'],'username':member.get('username')},
                'date':day.isoformat(),'selected_date':chosen.isoformat(),'attempts':attempts,'selected_total':sum(max(0,int(a.get('pushup_count') or 0)) for a in attempts),'members':[{'id':int(m['telegram_user_id']),
                'name':m['display_name'],'reported':int(m['telegram_user_id']) in reported}
                for m in members],'today':today,'week':week,'month':monthly,'all':totals,'stats':stats,'daily':daily}
    result=await asyncio.to_thread(load)
    if result is None:
        raise HTTPException(403,'Сначала зарегистрируйся в тестовом чате через /join')
    return result
