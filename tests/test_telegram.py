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
    assert message.reply_text.await_count == expected
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
