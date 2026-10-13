from datetime import date
from app.summaries import rival_challenge, overtaken_message, weekly_summary


MEMBERS = [
    {'telegram_user_id': 1, 'display_name': 'Vlad'},
    {'telegram_user_id': 2, 'display_name': 'Dima'},
    {'telegram_user_id': 3, 'display_name': 'Pasha'},
]


def test_rival_target():
    assert 'Ещё 6 отж.' in rival_challenge(MEMBERS, {1: 20, 2: 25, 3: 40}, 1)
    assert rival_challenge(MEMBERS, {1: 50, 2: 25}, 1) is None


def test_overtake_only_for_unique_new_leader():
    assert 'тебя обогнали' in overtaken_message(
        {1: 30, 2: 20}, {1: 30, 2: 35}, MEMBERS, 2)
    assert overtaken_message({1: 30, 2: 20}, {1: 30, 2: 30}, MEMBERS, 2) is None
    assert overtaken_message({1: 30, 2: 20}, {1: 35, 2: 20}, MEMBERS, 1) is None


def test_weekly_totals():
    text = weekly_summary(MEMBERS, {1: 100, 2: 80, 3: 80}, date(2026, 10, 12))
    assert '🥇 Vlad — 100 отж.' in text
    assert '🥈 Dima — 80 отж.' in text
    assert '🥈 Pasha — 80 отж.' in text
    assert 'Всего за неделю: 260' in text
