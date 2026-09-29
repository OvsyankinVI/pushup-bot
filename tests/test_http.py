import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.telegram_bot import update_failed
from main import app


@pytest.fixture
def client():
    app.state.settings = SimpleNamespace(telegram_webhook_secret='test-webhook',
                                        cron_secret='test-cron', telegram_chat_id=-100)
    app.state.telegram = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),
                                         process_update=AsyncMock())
    app.state.db = MagicMock()
    app.state.db.summary_data.return_value = ([], set())
    app.state.update_lock = asyncio.Lock()
    # No lifespan: these tests must never connect to Telegram or Supabase.
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


def test_health(client):
    assert client.get('/').json() == {'status': 'ok'}


@pytest.mark.parametrize('path, header', [('/telegram/webhook', 'X-Telegram-Bot-Api-Secret-Token'),
                                         ('/cron/evening', 'X-Cron-Secret'),
                                         ('/cron/midnight', 'X-Cron-Secret')])
def test_secrets_required(client, path, header):
    assert client.post(path, json={'update_id': 1}).status_code == 403
    assert client.post(path, headers={header: 'wrong'}, json={'update_id': 1}).status_code == 403
    app.state.telegram.process_update.assert_not_awaited()
    app.state.db.summary_data.assert_not_called()


def test_invalid_update(client):
    assert client.post('/telegram/webhook', json=[], headers={
        'X-Telegram-Bot-Api-Secret-Token': 'test-webhook'}).status_code == 400


def test_webhook_waits_for_processing(client):
    response = client.post('/telegram/webhook', json={'update_id': 1}, headers={
        'X-Telegram-Bot-Api-Secret-Token': 'test-webhook'})
    assert response.status_code == 200
    app.state.telegram.process_update.assert_awaited_once()


def test_handler_error_is_retryable(client):
    async def fail(update):
        update_failed.set(True)
    app.state.telegram.process_update.side_effect = fail
    assert client.post('/telegram/webhook', json={'update_id': 1}, headers={
        'X-Telegram-Bot-Api-Secret-Token': 'test-webhook'}).status_code == 503


def test_cron_previous_day(client, monkeypatch):
    from datetime import date, datetime
    from app.summaries import midnight_report_date
    monkeypatch.setattr('main.midnight_report_date', lambda: midnight_report_date(
        datetime.fromisoformat('2026-09-29T21:00:00+00:00')))
    app.state.db.summary_data.return_value = (
        [{'telegram_user_id': 1, 'display_name': 'Влад'}], set())
    response = client.post('/cron/midnight', headers={'X-Cron-Secret': 'test-cron'})
    assert response.json()['report_date'] == '2026-09-29'
    app.state.db.summary_data.assert_called_once_with(-100, date(2026, 9, 29))
    assert 'Итоги 29 сентября' in app.state.telegram.bot.send_message.call_args.kwargs['text']


def test_cron_without_chat(client):
    app.state.settings.telegram_chat_id = None
    assert client.post('/cron/evening', headers={'X-Cron-Secret': 'test-cron'}).status_code == 503


def test_database_error_is_private(client):
    app.state.db.summary_data.side_effect = RuntimeError('sensitive-test-value')
    response = client.post('/cron/evening', headers={'X-Cron-Secret': 'test-cron'})
    assert response.status_code == 503
    assert 'sensitive-test-value' not in response.text


@pytest.mark.parametrize('kind, reported, warning, sent', [
    ('evening', {1, 2}, None, True),
    ('evening', {1}, '👀 Кажется, кто-то филонит.', True),
    ('midnight', {1, 2}, None, False),
    ('midnight', {1}, '💸 Кажется, у нас появились должники.', True),
])
def test_cron_summary_delivery(client, kind, reported, warning, sent):
    app.state.db.summary_data.return_value = ([
        {'telegram_user_id': 1, 'display_name': 'Влад'},
        {'telegram_user_id': 2, 'display_name': 'Никита'},
    ], reported)
    response = client.post(f'/cron/{kind}', headers={'X-Cron-Secret': 'test-cron'})
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    app.state.db.summary_data.assert_called_once()
    send = app.state.telegram.bot.send_message
    if not sent:
        send.assert_not_awaited()
        return
    send.assert_awaited_once()
    text = send.call_args.kwargs['text']
    assert '✅ Влад — кружок есть' in text
    assert ('✅ Никита — кружок есть' if 2 in reported else '❌ Никита — кружка нет') in text
    assert 'Наличие кружка не гарантирует наличие в нём отжиманий 😏' in text
    if warning:
        assert warning in text
    else:
        assert 'филонит' not in text and 'должники' not in text


def test_midnight_reads_fresh_reports_after_evening(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr('main.today_moscow', lambda: date(2026, 9, 29))
    monkeypatch.setattr('main.midnight_report_date', lambda: date(2026, 9, 29))
    members = [{'telegram_user_id': 1, 'display_name': 'Влад'},
               {'telegram_user_id': 2, 'display_name': 'Никита'}]
    app.state.db.summary_data.side_effect = [(members, {1}), (members, {1, 2})]
    headers = {'X-Cron-Secret': 'test-cron'}
    assert client.post('/cron/evening', headers=headers).status_code == 200
    send = app.state.telegram.bot.send_message
    send.assert_awaited_once()
    assert '👀 Кажется, кто-то филонит.' in send.call_args.kwargs['text']
    send.reset_mock()
    assert client.post('/cron/midnight', headers=headers).status_code == 200
    send.assert_not_awaited()
    assert app.state.db.summary_data.call_count == 2
    for call in app.state.db.summary_data.call_args_list:
        assert call.args == (-100, date(2026, 9, 29))


def test_midnight_empty_group_is_success_without_message(client):
    assert client.post('/cron/midnight', headers={'X-Cron-Secret': 'test-cron'}).status_code == 200
    app.state.db.summary_data.assert_called_once()
    app.state.telegram.bot.send_message.assert_not_awaited()
