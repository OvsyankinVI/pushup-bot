import logging

from app.safe_logging import log_exception

logger = logging.getLogger(__name__)


async def send_admin_alert(application, settings, text: str):
    """Best-effort alert. Alert delivery must never break the primary flow."""
    if settings.admin_telegram_id is None:
        logger.warning('Admin alert skipped: ADMIN_TELEGRAM_ID is not configured')
        return
    try:
        await application.bot.send_message(
            chat_id=settings.admin_telegram_id,
            text=text[:4000],
        )
    except Exception as exc:
        log_exception(logger, 'Admin alert delivery failed', exc, settings)
