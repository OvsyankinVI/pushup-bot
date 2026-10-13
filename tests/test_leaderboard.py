from datetime import date

from app.summaries import format_summary, leaderboard, promotion_message, rank_for_user


def people():
    return [
        {'telegram_user_id': 1, 'display_name': 'Vlad'},
        {'telegram_user_id': 2, 'display_name': 'Vika'},
        {'telegram_user_id': 3, 'display_name': 'Dima'},
        {'telegram_user_id': 4, 'display_name': 'Pasha'},
        {'telegram_user_id': 5, 'display_name': 'Serezha'},
        {'telegram_user_id': 6, 'display_name': 'Alex'},
    ]


def test_shared_medals_and_alphabetical_zeroes():
    totals = {1: 30, 2: 30, 3: 28, 4: 27, 5: 26}
    ranked = leaderboard(people(), totals)
    assert [r[2] for r in ranked] == [1, 1, 2, 3, 4, None]
    text = format_summary(people(), set(), date(2026, 10, 13), pushup_totals=totals)
    assert '🥇 Vika — 30 отж.' in text
    assert '🥇 Vlad — 30 отж.' in text
    assert '🥈 Dima — 28 отж.' in text
    assert '🥉 Pasha — 27 отж.' in text
    assert '✅ Serezha — 26 отж.' in text
    assert '❌ Alex' in text
    assert 'Отжались: 5/6' in text
    assert 'Всего отжиманий: 141' in text
    assert 'Наличие кружка' not in text


def test_no_pushups_is_alphabetical():
    ranked = leaderboard(people(), {})
    assert [r[0]['display_name'] for r in ranked] == sorted(
        [m['display_name'] for m in people()], key=str.casefold)
    assert all(place is None for _, _, place in ranked)


def test_promotion_only_on_rank_improvement():
    assert 'первом месте' in promotion_message(None, 1)
    assert 'втором месте' in promotion_message(None, 2)
    assert 'третьем месте' in promotion_message(None, 3)
    assert promotion_message(1, 1) is None
    assert promotion_message(2, 2) is None
    assert promotion_message(1, 2) is None
    assert promotion_message(None, 4) is None
    assert 'первом месте' in promotion_message(3, 1)


def test_rank_for_user():
    assert rank_for_user(people(), {1: 20, 2: 20, 3: 10}, 2) == 1
    assert rank_for_user(people(), {}, 2) is None


def test_submission_reminder_stays_based_on_video_presence():
    text = format_summary(people(), {1, 2, 3, 4, 5, 6}, date(2026, 10, 13),
                          kind='midnight', pushup_totals={1: 3})
    assert 'должники' not in text
    assert 'Отжались: 1/6' in text
