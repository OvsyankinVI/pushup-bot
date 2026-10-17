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
                   kind: str = 'today', pushup_totals: dict[int, int] | None = None,
                   manual_totals: dict[int, int] | None = None) -> str:
    titles = {
        'today': '🏋️ Сегодня',
        'evening': '🏋️ Промежуточная сводка',
        'midnight': f'🏁 Итоги {report_date.day} {MONTHS[report_date.month - 1]}',
    }
    lines = [titles[kind], '']
    totals = pushup_totals or {}
    ranking = leaderboard(members, totals)
    manual_totals = manual_totals or {}
    if not members:
        lines.append('Пока нет активных участников. Зарегистрируйтесь через /join.')
    for member, score, place in ranking:
        icon = MEDALS[place - 1] if place is not None and place <= 3 else ('✅' if score else '❌')
        manual = manual_totals.get(member['telegram_user_id'], 0)
        suffix = f' — {score-manual} (+{manual}) отж.' if manual else (f' — {score} отж.' if score else '')
        lines.append(f"{icon} {member['display_name']}{suffix}")
    completed = sum(score > 0 for _, score, _ in ranking)
    total = sum(score for _, score, _ in ranking)
    manual_sum = sum(manual_totals.get(m['telegram_user_id'], 0) for m in members)
    lines.extend(['', f'Отжались: {completed}/{len(members)}',
                  f'🏋️ Всего отжиманий: {total-manual_sum} (+{manual_sum}) = {total}' if manual_sum else f'🏋️ Всего отжиманий: {total}'])
    # Penalty reminders retain the original video-submission criterion.
    if kind != 'today' and len(reported.intersection(
            {member['telegram_user_id'] for member in members})) < len(members):
        lines.extend(['', '👀 Кажется, кто-то филонит.' if kind == 'evening'
                      else '💸 Кажется, у нас появились должники.'])
    return '\n'.join(lines)


def weekly_summary(members: list[dict], totals: dict[int, int], week_start: date,
                   manual_totals: dict[int, int] | None = None) -> str:
    manual_totals = manual_totals or {}
    end = week_start + timedelta(days=6)
    lines = [f'🏆 Итоги недели {week_start:%d.%m}–{end:%d.%m}', '']
    for member, score, place in leaderboard(members, totals):
        if not score:
            continue
        icon = MEDALS[place - 1] if place <= 3 else '✅'
        manual = manual_totals.get(member['telegram_user_id'], 0)
        lines.append(f"{icon} {member['display_name']} — {score-manual} (+{manual}) отж." if manual else f"{icon} {member['display_name']} — {score} отж.")
    if len(lines) == 2:
        lines.append('Пока нет засчитанных отжиманий.')
    week_total = sum(max(0, int(v)) for v in totals.values())
    week_manual = sum(manual_totals.values())
    lines.extend(['', f'🏋️ Всего за неделю: {week_total-week_manual} (+{week_manual}) = {week_total}' if week_manual else f'🏋️ Всего за неделю: {week_total}'])
    return '\n'.join(lines)


def rival_challenge(members: list[dict], totals: dict[int, int], user_id: int) -> str | None:
    ranked = leaderboard(members, totals)
    current = next(((score, place) for m, score, place in ranked
                    if m['telegram_user_id'] == user_id), None)
    if current is None or current[0] <= 0 or current[1] == 1:
        return None
    better = [(m, score) for m, score, _ in ranked if score > current[0]]
    if not better:
        return None
    rival, score = min(better, key=lambda item: (item[1], item[0]['display_name'].casefold()))
    return f"🎯 Ещё {score - current[0] + 1} отж. — и обгонишь {rival['display_name']}!"


def overtaken_message(before: dict[int, int], after: dict[int, int],
                      members: list[dict], user_id: int) -> str | None:
    """Only notify the previous sole leader if a new leader strictly surpasses them."""
    old_leaders = [uid for uid, score in before.items() if score > 0 and score == max(before.values())]
    new_score = after.get(user_id, 0)
    if len(old_leaders) != 1 or old_leaders[0] == user_id:
        return None
    former_id = old_leaders[0]
    if new_score <= before[former_id] or sum(v == new_score for v in after.values()) != 1:
        return None
    names = {m['telegram_user_id']: m['display_name'] for m in members}
    if former_id not in names or user_id not in names:
        return None
    return (f"👀 {names[former_id]}, тебя обогнали! "
            f"Ещё {new_score - after.get(former_id, 0) + 1} отж. — и вернёшь первое место!")
