from datetime import date


class Database:
    """Synchronous supabase-py calls; callers use asyncio.to_thread."""

    def __init__(self, client):
        self.client = client

    def member(self, chat_id: int, user_id: int):
        rows = (self.client.table('members').select('*').eq('chat_id', chat_id)
                .eq('telegram_user_id', user_id).limit(1).execute().data)
        return rows[0] if rows else None

    def join(self, chat_id, user_id, username, display_name) -> bool:
        # INSERT ON CONFLICT DO NOTHING makes concurrent registrations safe.
        data = dict(chat_id=chat_id, telegram_user_id=user_id, username=username,
                    display_name=display_name, active=True)
        inserted = (self.client.table('members').upsert(
            data, on_conflict='chat_id,telegram_user_id', ignore_duplicates=True)
            .execute().data)
        if not inserted:
            (self.client.table('members').update(dict(username=username,
                display_name=display_name, active=True)).eq('chat_id', chat_id)
                .eq('telegram_user_id', user_id).execute())
        return bool(inserted)

    def record(self, chat_id: int, user_id: int, day: date) -> bool:
        # Only the INSERT winner returns a row; conflicts return an empty list.
        inserted = (self.client.table('daily_reports').upsert(
            dict(chat_id=chat_id, telegram_user_id=user_id, report_date=day.isoformat()),
            on_conflict='chat_id,telegram_user_id,report_date', ignore_duplicates=True,
            returning='representation')
            .execute().data)
        return bool(inserted)

    def members(self, chat_id: int) -> list[dict]:
        return (self.client.table('members').select('*').eq('chat_id', chat_id)
                .eq('active', True).order('display_name').execute().data)

    def summary_data(self, chat_id: int, day: date):
        members = self.members(chat_id)
        rows = (self.client.table('daily_reports').select('telegram_user_id')
                .eq('chat_id', chat_id).eq('report_date', day.isoformat()).execute().data)
        return members, {row['telegram_user_id'] for row in rows}
