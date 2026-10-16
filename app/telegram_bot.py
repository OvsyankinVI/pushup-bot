import asyncio
import logging
import os
import tempfile
from contextvars import ContextVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update, BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.pose_analysis import analyze_pose_visibility
from app.pushup_classification import classify_pushup_attempt
from app.safe_logging import log_exception
from app.summaries import format_summary, today_moscow, weekly_summary
from app.world_pose_analysis import analyze_world_landmarks

logger=logging.getLogger(__name__);update_failed=ContextVar('update_failed',default=False)
def is_admin(user_id,settings)->bool:return settings.admin_telegram_id is not None and user_id==settings.admin_telegram_id

def build_application(settings,db):
    application=Application.builder().token(settings.telegram_bot_token).updater(None).job_queue(None).build()
    async def group_only(update):
        if not update.message:return False
        if update.effective_chat.type not in ('group','supergroup'):await update.message.reply_text('Эта команда доступна только в группе.');return False
        return True
    async def start(update,context):
        await update.message.reply_text('🏋️ Бот отжиманий на связи!')
        if update.effective_chat.type == 'private':
            await update.message.reply_text('Mini App:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💪 Открыть', web_app=WebAppInfo(url='https://pushup-bot-staging.onrender.com/miniapp?v=2'))]]))
    async def app_command(update, context):
        if not await group_only(update):
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('🚀 Открыть Отжиматор',
                url='https://t.me/otshumator_test_bot/miniapp')
        ]])
        await update.message.reply_text(
            '💪 Отжиматор\nРейтинг участников и личная статистика.',
            reply_markup=keyboard)

    async def myid(update,context):
        if update.effective_user is not None and not update.message.sender_chat:await update.message.reply_text(f'🆔 Ваш Telegram ID: {update.effective_user.id}')
    async def join(update,context):
        if not await group_only(update):return
        user=update.effective_user
        if not user or user.is_bot or update.message.sender_chat:await update.message.reply_text('Отправьте /join от своего личного аккаунта.');return
        created=await asyncio.to_thread(db.join,update.effective_chat.id,user.id,user.username,user.full_name);await update.message.reply_text('✅ Вы зарегистрированы!' if created else 'Вы уже зарегистрированы. Данные обновлены.')
    async def members(update,context):
        if not await group_only(update):return
        rows=await asyncio.to_thread(db.members,update.effective_chat.id);text=('👥 Участники:\n\n'+'\n'.join(f"• {m['display_name']}" for m in rows)+f'\n\nВсего: {len(rows)}') if rows else 'Участников пока нет. Используйте /join.';await update.message.reply_text(text)
    async def today(update,context):
        if not await group_only(update):return
        day=today_moscow();rows,reported,totals=await asyncio.to_thread(db.summary_data,update.effective_chat.id,day);await update.message.reply_text(format_summary(rows,reported,day,pushup_totals=totals))
    async def week(update,context):
        if not await group_only(update):return
        day=today_moscow()
        from datetime import timedelta
        start=day-timedelta(days=day.weekday())
        members=await asyncio.to_thread(db.members,update.effective_chat.id)
        totals=await asyncio.to_thread(db.weekly_pushup_totals,update.effective_chat.id,start)
        await update.message.reply_text(weekly_summary(members,totals,start))
    async def stats(update, context):
        if not await group_only(update):return
        user = update.effective_user
        if not user or user.is_bot or update.message.sender_chat:return
        member = await asyncio.to_thread(db.member, update.effective_chat.id, user.id)
        if not member or not member.get('active'):
            await update.message.reply_text('Сначала зарегистрируйтесь через /join.')
            return
        values = await asyncio.to_thread(db.personal_stats, update.effective_chat.id, user.id, today_moscow())
        await update.message.reply_text(
            f"🏆 Достижения: {member['display_name']}\n\n"
            f"💪 Всего отжиманий: {values['total']}\n"
            f"📅 За текущую неделю: {values['this_week']}\n"
            f"🔥 Рекорд за неделю: {values['best_week']}\n"
            f"🥇 Рекорд за один кружок: {values['best_single']}")

    async def results_on(update,context):
        if not await group_only(update):return
        if update.message.sender_chat or not update.effective_user or not is_admin(update.effective_user.id,settings):
            await update.message.reply_text('Команда доступна только администратору бота.');return
        await asyncio.to_thread(db.set_result_replies_enabled,update.effective_chat.id,True)
        await update.message.reply_text('🔔 Ответы на кружки включены.')

    async def results_off(update,context):
        if not await group_only(update):return
        if update.message.sender_chat or not update.effective_user or not is_admin(update.effective_user.id,settings):
            await update.message.reply_text('Команда доступна только администратору бота.');return
        await asyncio.to_thread(db.set_result_replies_enabled,update.effective_chat.id,False)
        await update.message.reply_text('🔕 Ответы на кружки выключены. Отжимания продолжают считаться.')

    async def chatid(update,context):
        if not await group_only(update):return
        if settings.admin_telegram_id is None:await update.message.reply_text('Администратор не настроен: ADMIN_TELEGRAM_ID.')
        elif update.message.sender_chat or not update.effective_user or not is_admin(update.effective_user.id,settings):await update.message.reply_text('Команда доступна только администратору бота.')
        else:await update.message.reply_text(str(update.effective_chat.id))
    async def video_note(update,context):
        user=update.effective_user
        if not user or user.is_bot or update.message.sender_chat:return
        # Only original video notes sent in this group are eligible.
        # Telegram exposes both modern and legacy forwarding metadata depending
        # on Bot API / python-telegram-bot versions.
        if not settings.pushup_allow_forwarded and (getattr(update.message,'forward_origin',None) is not None
                or getattr(update.message,'forward_date',None) is not None
                or getattr(update.message,'forward_from',None) is not None
                or getattr(update.message,'forward_from_chat',None) is not None):
            if settings.pushup_results_enabled:
                await update.message.reply_text(
                    '↪️ Пересланный кружок не засчитывается. Отправьте новый кружок прямо в эту группу.')
            return
        day=today_moscow();chat_id=update.effective_chat.id
        member=await asyncio.to_thread(db.member,chat_id,user.id)
        if not member or not member['active']:return
        # Reused Telegram file IDs indicate the same video, even if the
        # forwarder hid the original sender. Check before daily report credit.
        if settings.pushup_analysis_enabled and not settings.pushup_allow_forwarded:
            already_seen = await asyncio.to_thread(
                db.previously_seen_video, chat_id,
                update.message.video_note.file_id, update.message.message_id)
            if already_seen:
                await update.message.reply_text(
                    '♻️ Этот кружок уже присылали в чат. Повторные видео не засчитываются — запиши новый кружок!')
                return
        if settings.pushup_analysis_enabled:
            attempt = await asyncio.to_thread(
                db.enqueue_pushup_attempt,chat_id,user.id,day,
                update.message.message_id,update.message.video_note.file_id)
            if attempt is None:
                # Telegram webhook retries must not grant additional daily credit.
                return
        await asyncio.to_thread(db.record,chat_id,user.id,day)
    async def error_handler(update,context):update_failed.set(True);log_exception(logger,'Telegram update failed',context.error,settings)
    for command,callback in [('start',start),('app',app_command),('join',join),('members',members),('today',today),('week',week),('stats',stats),('results_on',results_on),('results_off',results_off),('chatid',chatid),('myid',myid)]:application.add_handler(CommandHandler(command,callback,filters=filters.UpdateType.MESSAGE))
    async def setup_commands():
        await application.bot.set_my_commands([
            BotCommand('app', 'Открыть Отжиматор'),
            BotCommand('join', 'Присоединиться'),
            BotCommand('today', 'Рейтинг за сегодня'),
            BotCommand('week', 'Рейтинг за неделю'),
            BotCommand('stats', 'Мои достижения'),
            BotCommand('members', 'Участники'),
        ], scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands([
            BotCommand('start', 'Запустить бота'),
            BotCommand('myid', 'Мой Telegram ID'),
        ], scope=BotCommandScopeAllPrivateChats())
    application.post_init = lambda app: setup_commands()
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.VIDEO_NOTE,video_note));application.add_error_handler(error_handler);return application
