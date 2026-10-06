import asyncio
import logging
import os
import tempfile
from contextvars import ContextVar

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.pose_analysis import analyze_pose_visibility
from app.safe_logging import log_exception
from app.summaries import format_summary, today_moscow

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
        if not await group_only(update): return
        user = update.effective_user
        if not user or user.is_bot or update.message.sender_chat:
            await update.message.reply_text('Отправьте /join от своего личного аккаунта.'); return
        created = await asyncio.to_thread(db.join, update.effective_chat.id, user.id, user.username, user.full_name)
        await update.message.reply_text('✅ Вы зарегистрированы!' if created else 'Вы уже зарегистрированы. Данные обновлены.')

    async def members(update, context):
        if not await group_only(update): return
        rows = await asyncio.to_thread(db.members, update.effective_chat.id)
        text = ('👥 Участники:\n\n' + '\n'.join(f"• {m['display_name']}" for m in rows) + f'\n\nВсего: {len(rows)}') if rows else 'Участников пока нет. Используйте /join.'
        await update.message.reply_text(text)

    async def today(update, context):
        if not await group_only(update): return
        day = today_moscow(); rows, reported = await asyncio.to_thread(db.summary_data, update.effective_chat.id, day)
        await update.message.reply_text(format_summary(rows, reported, day))

    async def chatid(update, context):
        if not await group_only(update): return
        if settings.admin_telegram_id is None:
            await update.message.reply_text('Администратор не настроен: ADMIN_TELEGRAM_ID.')
        elif update.message.sender_chat or not update.effective_user or not is_admin(update.effective_user.id, settings):
            await update.message.reply_text('Команда доступна только администратору бота.')
        else:
            await update.message.reply_text(str(update.effective_chat.id))

    async def inspect_video_note(update, context, user_id, day):
        chat_id = update.effective_chat.id; message_id = update.message.message_id
        attempt = None; temp_path = None
        try:
            attempt = await asyncio.to_thread(db.create_pushup_attempt, chat_id, user_id, day, message_id)
            if not attempt: return
            telegram_file = await context.bot.get_file(update.message.video_note.file_id)
            with tempfile.NamedTemporaryFile(prefix='pushup_', suffix='.mp4', delete=False) as f:
                temp_path = f.name
            await telegram_file.download_to_drive(custom_path=temp_path)
            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
                raise ValueError('Downloaded video note is empty')

            metrics = await asyncio.to_thread(analyze_pose_visibility, temp_path)
            confidence = round(metrics['usable_ratio'], 4)
            candidates = metrics.get('candidates', [])
            signal_diag = ';'.join(
                f"{c['name']}={c['count']}/{c['amplitude']:.3f}/{c['quality']:.3f}"
                for c in candidates)
            reason = (
                f"multi_signal:selected={metrics.get('selected_signal','none')},"
                f"agreement={metrics.get('signal_agreement',0):.3f};"
                f"sampled={metrics['sampled_frames']},pose={metrics['pose_frames']},"
                f"usable={metrics['usable_frames']};signals:{signal_diag}")
            await asyncio.to_thread(db.finish_pushup_attempt, chat_id, message_id, 'uncertain', reason)
            await asyncio.to_thread(
                lambda: db.client.table('pushup_attempts').update({
                    'confidence': confidence, 'pushup_count': metrics['pushup_count']})
                .eq('chat_id', chat_id).eq('telegram_message_id', message_id).execute())
            logger.info('Multi-signal push-up count chat_id=%s message_id=%s count=%s selected=%s agreement=%.3f signals=%s',
                        chat_id, message_id, metrics['pushup_count'], metrics.get('selected_signal'),
                        metrics.get('signal_agreement', 0), signal_diag)
        except Exception as exc:
            if attempt:
                try:
                    await asyncio.to_thread(db.finish_pushup_attempt, chat_id, message_id, 'failed', 'pose_analysis_error')
                except Exception as db_exc:
                    log_exception(logger, 'Push-up attempt failure update failed', db_exc, settings)
            log_exception(logger, 'Push-up pose analysis failed', exc, settings)
        finally:
            if temp_path:
                try: os.remove(temp_path)
                except FileNotFoundError: pass
                except OSError as exc: log_exception(logger, 'Temporary video cleanup failed', exc, settings)

    async def video_note(update, context):
        user = update.effective_user
        if not user or user.is_bot or update.message.sender_chat: return
        day = today_moscow(); member = await asyncio.to_thread(db.member, update.effective_chat.id, user.id)
        if not member or not member['active']: return
        await asyncio.to_thread(db.record, update.effective_chat.id, user.id, day)
        if settings.pushup_analysis_enabled:
            await inspect_video_note(update, context, user.id, day)

    async def error_handler(update, context):
        update_failed.set(True); log_exception(logger, 'Telegram update failed', context.error, settings)

    for command, callback in [('start', start), ('join', join), ('members', members), ('today', today), ('chatid', chatid), ('myid', myid)]:
        application.add_handler(CommandHandler(command, callback, filters=filters.UpdateType.MESSAGE))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.VIDEO_NOTE, video_note))
    application.add_error_handler(error_handler)
    return application
