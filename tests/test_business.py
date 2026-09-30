from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.database import Database
from app.summaries import format_summary, midnight_report_date, today_moscow


@pytest.mark.parametrize('instant, expected', [
    ('2026-09-29T20:59:59+00:00', date(2026, 9, 29)),
    ('2026-09-29T21:00:00+00:00', date(2026, 9, 30)),
])
def test_moscow_date(instant, expected):
    assert today_moscow(datetime.fromisoformat(instant)) == expected


@pytest.mark.parametrize('instant, expected', [
    ('2026-09-29T21:00:00+00:00', date(2026, 9, 29)),
    ('2026-01-01T00:00:00+03:00', date(2025, 12, 31)),
    ('2024-03-01T00:00:00+03:00', date(2024, 2, 29)),
])
def test_midnight_previous_day(instant, expected):
    assert midnight_report_date(datetime.fromisoformat(instant)) == expected


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        today_moscow(datetime(2026, 1, 1))


MEMBERS = [dict(telegram_user_id=1, display_name='Влад'),
           dict(telegram_user_id=2, display_name='Никита')]


@pytest.mark.parametrize('kind', ['today', 'evening', 'midnight'])
def test_everyone_reported(kind):
    text = format_summary(MEMBERS, {1, 2, 99}, date(2026, 9, 29), kind)
    assert 'Отчитались: 2/2' in text
    assert '❌' not in text
    assert 'филонит' not in text and 'должники' not in text


@pytest.mark.parametrize('kind, warning', [('evening', 'филонит'), ('midnight', 'должники')])
def test_missing_report(kind, warning):
    text = format_summary(MEMBERS, {1}, date(2026, 9, 29), kind)
    assert '❌ Никита — кружка нет' in text
    assert 'Отчитались: 1/2' in text and warning in text
    assert 'Наличие кружка' in text
    if kind == 'midnight':
        assert '🏁 Итоги 29 сентября' in text


def test_empty_group():
    text = format_summary([], set(), date(2026, 9, 29), 'evening')
    assert 'Пока нет активных участников' in text
    assert 'филонит' not in text


def test_report_conflict_and_schema():
    client = MagicMock()
    db = Database(client)
    from types import SimpleNamespace
    client.table.return_value.upsert.return_value.execute.side_effect = [
        SimpleNamespace(data=[{'id': 1}]), SimpleNamespace(data=[])]
    assert db.record(-100, 1, date(2026, 9, 29)) is True
    assert db.record(-100, 1, date(2026, 9, 29)) is False
    client.table.return_value.upsert.assert_called_with(
        {'chat_id': -100, 'telegram_user_id': 1, 'report_date': '2026-09-29'},
        on_conflict='chat_id,telegram_user_id,report_date', ignore_duplicates=True,
        returning='representation')
    sql = (Path(__file__).parents[1] / 'sql/schema.sql').read_text()
    assert 'unique (chat_id, telegram_user_id, report_date)' in sql
    assert 'unique (chat_id, telegram_user_id)' in sql


def test_repeat_join_refreshes_identity():
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.return_value.data = []
    assert Database(client).join(-100, 1, None, 'Новое имя') is False
    client.table.return_value.update.assert_called_once_with(
        {'username': None, 'display_name': 'Новое имя', 'active': True})
