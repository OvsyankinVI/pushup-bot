from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')


def today_moscow(now: datetime | None = None) -> date:
    if now is not None and now.tzinfo is None:
        raise ValueError('Требуется datetime с часовым поясом')
    return (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date()


def midnight_report_date(now: datetime | None = None) -> date:
    return today_moscow(now) - timedelta(days=1)


MEDALS = ('🥇', '🥈', '🥉')


def leaderboard(members: list[dict], totals: dict[int, int]) -> list[tuple[dict, int, int | None]]:
    """Competition ranking: equal scores share a place; zero scores are unranked."""
    ordered = sorted(
        members,
        key=lambda member: (
            -(totals.get(member['telegram_user_id'], 0) or 0),
            member['display_name'].casefold(),
            member['telegram_user_id'],
        ),
    )
    result = []
    last_score = None
    place = 0
    for member in ordered:
        score = max(0, int(totals.get(member['telegram_user_id'], 0) or 0))
        if score > 0:
            if score != last_score:
                place += 1
            last_score = score
            result.append((member, score, place))
        else:
            result.append((member, 0, None))
    return result


def rank_for_user(members: list[dict], totals: dict[int, int], user_id: int) -> int | None:
    return next((place for member, _, place in leaderboard(members, totals)
                 if member['telegram_user_id'] == user_id), None)


def promotion_message(before: int | None, after: int | None) -> str | None:
    """Only congratulate when entering a medal tier or improving its rank."""
    if after is None or after > 3 or (before is not None and before <= after):
        return None
    messages = {
        1: '🥇 Ты на первом месте сегодня! Пока никто не отжался больше тебя 😎',
        2: '🥈 Поздравляю, ты на втором месте сегодня! Так держать 💪',
        3: '🥉 Поздравляю, ты на третьем месте сегодня! Есть шанс подняться выше 💪',
    }
    return messages[after]


def format_summary(members: list[dict], reported: set[int], report_date: date,
                   kind: str = 'today', pushup_totals: dict[int, int] | None = None) -> str:
    titles = {
        'today': '🏋️ Сегодня',
        'evening': '🏋️ Промежуточная сводка',
        'midnight': f'🏁 Итоги {report_date.day} {MONTHS[report_date.month - 1]}',
    }
    lines = [titles[kind], '']
    totals = pushup_totals or {}
    ranking = leaderboard(members, totals)
    if not members:
        lines.append('Пока нет активных участников. Зарегистрируйтесь через /join.')
    for member, score, place in ranking:
        icon = MEDALS[place - 1] if place is not None and place <= 3 else ('✅' if score else '❌')
        suffix = f' — {score} отж.' if score else ''
        lines.append(f"{icon} {member['display_name']}{suffix}")
    completed = sum(score > 0 for _, score, _ in ranking)
    total = sum(score for _, score, _ in ranking)
    lines.extend(['', f'Отжались: {completed}/{len(members)}',
                  f'🏋️ Всего отжиманий: {total}'])
    # Penalty reminders retain the original video-submission criterion.
    if kind != 'today' and len(reported.intersection(
            {member['telegram_user_id'] for member in members})) < len(members):
        lines.extend(['', '👀 Кажется, кто-то филонит.' if kind == 'evening'
                      else '💸 Кажется, у нас появились должники.'])
    return '\n'.join(lines)
