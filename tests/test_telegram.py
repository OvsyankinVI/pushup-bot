import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler

from app.telegram_bot import build_application


@pytest.mark.parametrize('chat_type', ['private', 'group', 'supergroup'])
def test_myid(chat_type):
    application = build_application(SimpleNamespace(telegram_bot_token='123:test'), MagicMock())
    handler = next(h for h in application.handlers[0]
                   if isinstance(h, CommandHandler) and 'myid' in h.commands)
    message = SimpleNamespace(sender_chat=None, reply_text=AsyncMock())
    update = SimpleNamespace(message=message,
                             effective_chat=SimpleNamespace(id=-100, type=chat_type),
                             effective_user=SimpleNamespace(id=123456789))

    asyncio.run(handler.callback(update, None))

    message.reply_text.assert_awaited_once_with('🆔 Ваш Telegram ID: 123456789')


@pytest.mark.parametrize('active, registered, expected', [(True, True, 2),
                                                         (False, True, 0),
                                                         (True, False, 0)])
def test_video_handler(active, registered, expected):
    db = MagicMock()
    db.member.return_value = {'active': active} if registered else None
    db.record.side_effect = [True, False]
    application = build_application(SimpleNamespace(telegram_bot_token='123:test'), db)
    handler = next(h for h in application.handlers[0] if isinstance(h, MessageHandler))
    message = SimpleNamespace(sender_chat=None, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=-100),
                             effective_user=SimpleNamespace(id=42, is_bot=False,
                                                           username=None, full_name='Влад'))

    async def run():
        await handler.callback(update, None)
        await handler.callback(update, None)

    asyncio.run(run())
    assert db.record.call_count == expected
    assert message.reply_text.await_count == (1 if expected else 0)
    if expected:
        assert message.reply_text.call_args.args[0] == (
            '✅ Влад кружок зафиксирован, но отжимания ли там? Я не знаю 🤨')
        assert db.record.call_args.args[:2] == (-100, 42)


@pytest.mark.parametrize('chat_type, update_kind, accepted', [
    ('private', 'message', False), ('group', 'message', True),
    ('supergroup', 'message', True), ('channel', 'channel_post', False),
    ('group', 'edited_message', False),
])
def test_video_filter(chat_type, update_kind, accepted):
    application = build_application(SimpleNamespace(telegram_bot_token='123:test'), MagicMock())
    handler = next(h for h in application.handlers[0] if isinstance(h, MessageHandler))
    update = Update.de_json({'update_id': 1, update_kind: {
        'message_id': 1, 'date': 0, 'chat': {'id': -100, 'type': chat_type},
        'from': {'id': 42, 'is_bot': False, 'first_name': 'Влад'},
        'video_note': {'file_id': 'test', 'file_unique_id': 'test', 'length': 10, 'duration': 1},
    }}, application.bot)
    assert bool(handler.check_update(update)) is accepted


def test_lifecycle(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    settings = SimpleNamespace(supabase_url='https://example.invalid', supabase_key='test')
    telegram = AsyncMock()
    monkeypatch.setattr(main.Settings, 'from_env', lambda: settings)
    monkeypatch.setattr(main, 'create_client', MagicMock())
    monkeypatch.setattr(main, 'build_application', lambda *_: telegram)
    with TestClient(main.app) as client:
        assert client.get('/').status_code == 200
        telegram.__aenter__.assert_awaited_once()
        telegram.start.assert_awaited_once()
        telegram.stop.assert_not_awaited()
    telegram.stop.assert_awaited_once()
    telegram.__aexit__.assert_awaited_once()


@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('username, name', [(None, 'Влад'), ('vlad', '@vlad')])
def test_video_atomic_insert_and_next_moscow_day(monkeypatch, concurrent, username, name):
    from datetime import datetime
    from threading import Lock
    from app.database import Database
    from app.summaries import today_moscow

    rows = set()
    lock = Lock()
    client = MagicMock()

    def upsert(data, **options):
        assert options == dict(on_conflict='chat_id,telegram_user_id,report_date',
                               ignore_duplicates=True, returning='representation')
        key = (data['chat_id'], data['telegram_user_id'], data['report_date'])

        def execute():
            # Simulate the atomic UNIQUE / DO NOTHING database contract.
            with lock:
                if key in rows:
                    return SimpleNamespace(data=[])
                rows.add(key)
                return SimpleNamespace(data=[data])
        return SimpleNamespace(execute=execute)

    client.table.return_value.upsert.side_effect = upsert
    db = Database(client)
    db.member = MagicMock(return_value={'active': True})
    application = build_application(SimpleNamespace(telegram_bot_token='123:test'), db)
    handler = next(h for h in application.handlers[0] if isinstance(h, MessageHandler))
    message = SimpleNamespace(sender_chat=None, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=-100),
                             effective_user=SimpleNamespace(id=42, is_bot=False,
                                                           username=username, full_name='Влад'))
    instant = datetime.fromisoformat('2026-09-29T20:59:59+00:00')
    monkeypatch.setattr('app.telegram_bot.today_moscow', lambda: today_moscow(instant))

    async def run():
        nonlocal instant
        if concurrent:
            await asyncio.gather(handler.callback(update, None), handler.callback(update, None))
        else:
            await handler.callback(update, None)
            message.reply_text.assert_awaited_once()
            await handler.callback(update, None)
        assert len(rows) == 1
        message.reply_text.assert_awaited_once_with(
            f'✅ {name} кружок зафиксирован, но отжимания ли там? Я не знаю 🤨')
        message.reply_text.reset_mock()
        await handler.callback(update, None)
        message.reply_text.assert_not_awaited()
        instant = datetime.fromisoformat('2026-09-29T21:00:00+00:00')
        await handler.callback(update, None)
        message.reply_text.assert_awaited_once()
        assert rows == {(-100, 42, '2026-09-29'), (-100, 42, '2026-09-30')}

    asyncio.run(run())
