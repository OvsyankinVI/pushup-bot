from datetime import date, datetime, timezone


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

    def create_pushup_attempt(self, chat_id: int, user_id: int, day: date,
                              message_id: int):
        data = dict(chat_id=chat_id, telegram_user_id=user_id,
                    report_date=day.isoformat(), telegram_message_id=message_id,
                    status='processing')
        rows = (self.client.table('pushup_attempts').upsert(
            data, on_conflict='chat_id,telegram_message_id', ignore_duplicates=True,
            returning='representation').execute().data)
        return rows[0] if rows else None

    def enqueue_pushup_attempt(self, chat_id: int, user_id: int, day: date,
                               message_id: int, file_id: str):
        data = dict(chat_id=chat_id, telegram_user_id=user_id,
                    report_date=day.isoformat(), telegram_message_id=message_id,
                    telegram_file_id=file_id, status='processing')
        rows = (self.client.table('pushup_attempts').upsert(
            data, on_conflict='chat_id,telegram_message_id', ignore_duplicates=True,
            returning='representation').execute().data)
        return rows[0] if rows else None

    def claim_next_pushup_attempt(self):
        rows = self.client.rpc('claim_next_pushup_attempt').execute().data
        return rows[0] if rows else None

    def retry_or_fail_pushup_attempt(self, attempt_id: int, error: str,
                                     max_attempts: int = 3):
        rows = (self.client.table('pushup_attempts')
                .select('attempt_count,status').eq('id', attempt_id)
                .limit(1).execute().data)
        if not rows or rows[0]['status'] != 'processing':
            return []
        exhausted = int(rows[0].get('attempt_count') or 0) >= max_attempts
        data = {
            'last_error': error[:1000],
            'processing_started_at': None,
        }
        if exhausted:
            data.update({
                'status': 'failed',
                'rejection_reason': 'retry_limit_exceeded',
                'processed_at': datetime.now(timezone.utc).isoformat(),
            })
        return (self.client.table('pushup_attempts').update(data)
                .eq('id', attempt_id).eq('status', 'processing').execute().data)

    def finish_pushup_attempt(self, chat_id: int, message_id: int, status: str,
                              rejection_reason: str | None = None):
        data = dict(status=status, rejection_reason=rejection_reason,
                    processed_at=datetime.now(timezone.utc).isoformat())
        return (self.client.table('pushup_attempts').update(data)
                .eq('chat_id', chat_id).eq('telegram_message_id', message_id)
                .execute().data)

    def daily_pushup_total(self, chat_id: int, user_id: int, day: date) -> int:
        rows=(self.client.table('pushup_attempts').select('pushup_count')
              .eq('chat_id',chat_id).eq('telegram_user_id',user_id)
              .eq('report_date',day.isoformat()).eq('status','accepted').execute().data)
        return sum(int(row.get('pushup_count') or 0) for row in rows)

    def pushup_totals(self, chat_id: int, day: date) -> dict[int, int]:
        rows = (self.client.table('pushup_attempts')
                .select('telegram_user_id,pushup_count')
                .eq('chat_id', chat_id).eq('report_date', day.isoformat())
                .eq('status', 'accepted').execute().data)
        totals = {}
        for row in rows:
            user_id = row['telegram_user_id']
            totals[user_id] = totals.get(user_id, 0) + int(row.get('pushup_count') or 0)
        return totals

    def result_replies_enabled(self, chat_id: int) -> bool:
        rows = (self.client.table('chat_settings')
                .select('pushup_result_replies_enabled').eq('chat_id', chat_id)
                .limit(1).execute().data)
        return True if not rows else bool(rows[0]['pushup_result_replies_enabled'])

    def set_result_replies_enabled(self, chat_id: int, enabled: bool):
        return (self.client.table('chat_settings').upsert({
                    'chat_id': chat_id,
                    'pushup_result_replies_enabled': enabled,
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }, on_conflict='chat_id').execute().data)

    def members(self, chat_id: int) -> list[dict]:
        return (self.client.table('members').select('*').eq('chat_id', chat_id)
                .eq('active', True).order('display_name').execute().data)

    def summary_data(self, chat_id: int, day: date):
        members = self.members(chat_id)
        rows = (self.client.table('daily_reports').select('telegram_user_id')
                .eq('chat_id', chat_id).eq('report_date', day.isoformat()).execute().data)
        return members, {row['telegram_user_id'] for row in rows}, self.pushup_totals(chat_id, day)
