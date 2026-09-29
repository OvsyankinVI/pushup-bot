import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    supabase_url: str
    supabase_key: str = field(repr=False)
    cron_secret: str = field(repr=False)
    telegram_webhook_secret: str = field(default='', repr=False)
    admin_telegram_id: int | None = None
    telegram_chat_id: int | None = None

    @classmethod
    def from_env(cls):
        load_dotenv()

        def required(name):
            value = os.getenv(name, '').strip()
            if not value:
                raise ValueError(f'Не задана переменная {name}')
            return value

        def optional_id(name):
            value = os.getenv(name, '').strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                raise ValueError(f'{name} должна быть целым числом') from None

        secret = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
        if secret and not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
            raise ValueError('Некорректный формат TELEGRAM_WEBHOOK_SECRET')
        return cls(
            telegram_bot_token=required('TELEGRAM_BOT_TOKEN'),
            supabase_url=required('SUPABASE_URL'),
            supabase_key=required('SUPABASE_KEY'),
            cron_secret=required('CRON_SECRET'),
            telegram_webhook_secret=secret,
            admin_telegram_id=optional_id('ADMIN_TELEGRAM_ID'),
            telegram_chat_id=optional_id('TELEGRAM_CHAT_ID'),
        )
