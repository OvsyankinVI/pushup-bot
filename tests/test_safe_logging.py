import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

from postgrest.exceptions import APIError

from app.database import Database
from app.safe_logging import log_exception
from app.telegram_bot import build_application, update_failed


def test_diagnostics_redact_secrets_and_chained_tracebacks(caplog, monkeypatch):
    secrets = dict(telegram_bot_token='123:fake-token-secret', supabase_key='fake/key+secret',
                   telegram_webhook_secret='webhook-test-secret', cron_secret='cron-test-secret')
    monkeypatch.setenv('OTHER_PASSWORD', 'other-password-value')
    settings = SimpleNamespace(**secrets)
    try:
        try:
            raise ValueError('other-password-value')
        except ValueError as cause:
            raise RuntimeError(' '.join(secrets.values()) + ' ' + quote(secrets['supabase_key'], safe='')
                               + ' Authorization: Bearer unknown-credential') from cause
    except RuntimeError as error:
        log_exception(logging.getLogger('test'), 'Failure', error, settings)
    assert 'RuntimeError' in caplog.text and 'ValueError' in caplog.text
    assert 'Traceback' in caplog.text and 'test_diagnostics_redact' in caplog.text
    for value in [*secrets.values(), 'other-password-value', 'unknown-credential',
                  quote(secrets['supabase_key'], safe='')]:
        assert value not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_join_apierror_reaches_error_handler_with_database_diagnostics(caplog):
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.side_effect = APIError({
        'code': '42501', 'message': 'permission denied for table members',
        'hint': 'Check backend role', 'details': 'apikey=unknown-secret',
    })
    settings = SimpleNamespace(telegram_bot_token='123:test')
    application = build_application(settings, Database(client))

    async def run():
        marker = update_failed.set(False)
        try:
            try:
                await asyncio.to_thread(Database(client).join, -100, 42, None, 'Test')
            except APIError as error:
                await application.process_error(update=None, error=error)
            assert update_failed.get() is True
        finally:
            update_failed.reset(marker)

    asyncio.run(run())
    assert 'APIError' in caplog.text
    assert '42501' in caplog.text
    assert 'permission denied for table members' in caplog.text
    assert 'database.py' in caplog.text and 'join' in caplog.text
    assert 'unknown-secret' not in caplog.text
