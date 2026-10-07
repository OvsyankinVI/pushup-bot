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


def format_summary(members: list[dict], reported: set[int], report_date: date,
                   kind: str = 'today', pushup_totals: dict[int, int] | None = None) -> str:
    titles = {
        'today': '🏋️ Сегодня',
        'evening': '🏋️ Промежуточная сводка',
        'midnight': f'🏁 Итоги {report_date.day} {MONTHS[report_date.month - 1]}',
    }
    lines = [titles[kind], '']
    pushup_totals = pushup_totals or {}
    if not members:
        lines.append('Пока нет активных участников. Зарегистрируйтесь через /join.')
    count = 0
    for member in members:
        done = member['telegram_user_id'] in reported
        count += done
        total = pushup_totals.get(member['telegram_user_id'], 0)
        lines.append(f"{'✅' if done else '❌'} {member['display_name']} — {total} отж.")
    lines.extend(['', f'Отчитались: {count}/{len(members)}',
                  f'🏋️ Всего отжиманий: {sum(pushup_totals.values())}'])
    if kind != 'today':
        lines.extend(['', 'Наличие кружка не гарантирует наличие в нём отжиманий 😏'])
        if count < len(members):
            lines.extend(['', '👀 Кажется, кто-то филонит.' if kind == 'evening'
                          else '💸 Кажется, у нас появились должники.'])
    return '\n'.join(lines)
