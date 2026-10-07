import asyncio
import logging
import os
import tempfile
from contextvars import ContextVar

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.pose_analysis import analyze_pose_visibility
from app.pushup_classification import classify_pushup_attempt
from app.safe_logging import log_exception
from app.summaries import format_summary, today_moscow
from app.world_pose_analysis import analyze_world_landmarks

logger=logging.getLogger(__name__);update_failed=ContextVar('update_failed',default=False)
def is_admin(user_id,settings)->bool:return settings.admin_telegram_id is not None and user_id==settings.admin_telegram_id

def build_application(settings,db):
    application=Application.builder().token(settings.telegram_bot_token).updater(None).job_queue(None).build()
    async def group_only(update):
        if not update.message:return False
        if update.effective_chat.type not in ('group','supergroup'):await update.message.reply_text('Эта команда доступна только в группе.');return False
        return True
    async def start(update,context):await update.message.reply_text('🏋️ Бот отжиманий на связи!')
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
        day=today_moscow();rows,reported=await asyncio.to_thread(db.summary_data,update.effective_chat.id,day);await update.message.reply_text(format_summary(rows,reported,day))
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
        if (getattr(update.message,'forward_origin',None) is not None
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
        await asyncio.to_thread(db.record,chat_id,user.id,day)
        if settings.pushup_analysis_enabled:
            await asyncio.to_thread(
                db.enqueue_pushup_attempt,chat_id,user.id,day,
                update.message.message_id,update.message.video_note.file_id)
    async def error_handler(update,context):update_failed.set(True);log_exception(logger,'Telegram update failed',context.error,settings)
    for command,callback in [('start',start),('join',join),('members',members),('today',today),('chatid',chatid),('myid',myid)]:application.add_handler(CommandHandler(command,callback,filters=filters.UpdateType.MESSAGE))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.VIDEO_NOTE,video_note));application.add_error_handler(error_handler);return application
