import asyncio
import logging
from contextvars import ContextVar

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.safe_logging import log_exception
from app.summaries import display_user, format_summary, today_moscow

logger = logging.getLogger(__name__)
update_failed = ContextVar('update_failed', default=False)


def is_admin(user_id, settings) -> bool:
    return settings.admin_telegram_id is not None and user_id == settings.admin_telegram_id


def build_application(settings, db):
    application = (Application.builder().token(settings.telegram_bot_token)
                   .updater(None).job_queue(None).build())

    async def group_only(update):
        if not update.message:
            return False
        if update.effective_chat.type not in ('group', 'supergroup'):
            await update.message.reply_text('Эта команда доступна только в группе.')
            return False
        return True

    async def start(update, context):
        await update.message.reply_text('🏋️ Бот отжиманий на связи!')

    async def myid(update, context):
        if update.effective_user is None or update.message.sender_chat:
            return
        await update.message.reply_text(f'🆔 Ваш Telegram ID: {update.effective_user.id}')

    async def join(update, context):
        if not await group_only(update):
            return
        user = update.effective_user
        if not user or user.is_bot or update.message.sender_chat:
            await update.message.reply_text('Отправьте /join от своего личного аккаунта.')
            return
        created = await asyncio.to_thread(db.join, update.effective_chat.id,
                                         user.id, user.username, user.full_name)
        await update.message.reply_text('✅ Вы зарегистрированы!' if created
                                        else 'Вы уже зарегистрированы. Данные обновлены.')

    async def members(update, context):
        if not await group_only(update):
            return
        rows = await asyncio.to_thread(db.members, update.effective_chat.id)
        text = ('👥 Участники:\n\n' + '\n'.join(f"• {m['display_name']}" for m in rows)
                + f'\n\nВсего: {len(rows)}') if rows else 'Участников пока нет. Используйте /join.'
        await update.message.reply_text(text)

    async def today(update, context):
        if not await group_only(update):
            return
        day = today_moscow()
        rows, reported = await asyncio.to_thread(db.summary_data, update.effective_chat.id, day)
        await update.message.reply_text(format_summary(rows, reported, day))

    async def chatid(update, context):
        if not await group_only(update):
            return
        if settings.admin_telegram_id is None:
            await update.message.reply_text('Администратор не настроен: ADMIN_TELEGRAM_ID.')
        elif (update.message.sender_chat or not update.effective_user
              or not is_admin(update.effective_user.id, settings)):
            await update.message.reply_text('Команда доступна только администратору бота.')
        else:
            await update.message.reply_text(str(update.effective_chat.id))

    async def video_note(update, context):
        user = update.effective_user
        if not user or user.is_bot or update.message.sender_chat:
            return
        # Capture processing date before any I/O; server timezone is irrelevant.
        day = today_moscow()
        member = await asyncio.to_thread(db.member, update.effective_chat.id, user.id)
        if not member or not member['active']:
            return
        await asyncio.to_thread(db.record, update.effective_chat.id, user.id, day)
        name = display_user(user.username, user.full_name)
        await update.message.reply_text(
            f'✅ {name} кружок зафиксирован, но отжимания ли там? Я не знаю 🤨')

    async def error_handler(update, context):
        update_failed.set(True)
        log_exception(logger, 'Telegram update failed', context.error, settings)

    for command, callback in [('start', start), ('join', join), ('members', members),
                              ('today', today), ('chatid', chatid), ('myid', myid)]:
        application.add_handler(CommandHandler(command, callback, filters=filters.UpdateType.MESSAGE))
    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.VIDEO_NOTE, video_note))
    application.add_error_handler(error_handler)
    return application
