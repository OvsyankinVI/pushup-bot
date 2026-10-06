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
from app.world_pose_analysis import analyze_world_pose

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
    async def inspect_video_note(update,context,user_id,day):
        chat_id=update.effective_chat.id;message_id=update.message.message_id;attempt=None;temp_path=None
        try:
            attempt=await asyncio.to_thread(db.create_pushup_attempt,chat_id,user_id,day,message_id)
            if not attempt:return
            telegram_file=await context.bot.get_file(update.message.video_note.file_id)
            with tempfile.NamedTemporaryFile(prefix='pushup_',suffix='.mp4',delete=False) as f:temp_path=f.name
            await telegram_file.download_to_drive(custom_path=temp_path)
            if not os.path.isfile(temp_path) or os.path.getsize(temp_path)<=0:raise ValueError('Downloaded video note is empty')
            metrics=await asyncio.to_thread(analyze_pose_visibility,temp_path);world=await asyncio.to_thread(analyze_world_pose,temp_path);metrics['world_geometry']=world
            classification=classify_pushup_attempt(metrics);status=classification['status'];class_reason=classification['reason'];final_count=int(classification.get('count') or 0);confidence=round(metrics['usable_ratio'],4);candidates=metrics.get('candidates',[]);geometry=metrics.get('geometry',{})
            signal_diag=';'.join(f"{c['name']}={c['count']}/{c['amplitude']:.3f}/{c['quality']:.3f}" for c in candidates);geometry_diag=f"horizontal={geometry.get('horizontal_ratio',0):.3f},vertical={geometry.get('vertical_ratio',0):.3f},straight={geometry.get('straight_body_ratio',0):.3f},pushup_pose={geometry.get('pushup_pose_ratio',0):.3f},tilt={geometry.get('median_torso_tilt_deg')},bodyline={geometry.get('median_body_line_deg')}"
            world_diag=f"world_horizontal={world.get('horizontal_ratio',0):.3f},world_vertical={world.get('vertical_ratio',0):.3f},world_straight={world.get('straight_body_ratio',0):.3f},world_pushup={world.get('pushup_pose_ratio',0):.3f},world_verticality={world.get('median_torso_verticality')},world_bodyline={world.get('median_body_line_deg')},gated_segments={world.get('gated_segments',0)},gated_ratio={world.get('gated_ratio',0):.3f},gated_count={world.get('gated_pushup_count',0)}"
            reason=f"classifier={class_reason};geometry:{geometry_diag};world:{world_diag};multi_signal:selected={metrics.get('selected_signal','none')},agreement={metrics.get('signal_agreement',0):.3f};sampled={metrics['sampled_frames']},pose={metrics['pose_frames']},usable={metrics['usable_frames']};signals:{signal_diag}"
            await asyncio.to_thread(db.finish_pushup_attempt,chat_id,message_id,status,reason);await asyncio.to_thread(lambda:db.client.table('pushup_attempts').update({'confidence':confidence,'pushup_count':final_count}).eq('chat_id',chat_id).eq('telegram_message_id',message_id).execute())
            if settings.pushup_results_enabled:
                status_label={'accepted':'✅ Отжимания подтверждены','rejected':'❌ Отжимания не обнаружены','uncertain':'⚠️ Не удалось уверенно определить'}.get(status,status);signal_lines='\n'.join(f"• {c['name']}: {c['count']} (ампл. {c['amplitude']:.2f}, кач. {c['quality']:.0%})" for c in candidates)
                tilt=geometry.get('median_torso_tilt_deg');bodyline=geometry.get('median_body_line_deg');tilt_text=f'{tilt:.0f}°' if tilt is not None else '—';bodyline_text=f'{bodyline:.0f}°' if bodyline is not None else '—';world_v=world.get('median_torso_verticality');world_line=world.get('median_body_line_deg');world_v_text=f'{world_v:.2f}' if world_v is not None else '—';world_line_text=f'{world_line:.0f}°' if world_line is not None else '—'
                segments=world.get('segment_details',[])
                def seg_text(s):
                    mv=s.get('median_verticality');p75=s.get('p75_verticality');er=s.get('elbow_range');mc=s.get('motion_coupling');strict=s.get('strict_horizontal_ratio');pre=s.get('pre_verticality');post=s.get('post_verticality');kr=s.get('knee_range_deg');lkr=s.get('left_knee_range_deg');rkr=s.get('right_knee_range_deg');lv=s.get('left_leg_visibility');rv=s.get('right_leg_visibility');rl=s.get('reliable_leg_sides');hr=s.get('hip_y_range');ks=s.get('knee_elbow_sync');flag='' if s.get('counted',True) else ' [не считается]';coupling=f'{mc:.2f}' if mc is not None else '—';strict_text=f'{strict:.0%}' if strict is not None else '—';pre_text=f'{pre:.2f}' if pre is not None else '—';post_text=f'{post:.2f}' if post is not None else '—';knee_text=f'{kr:.0f}°' if kr is not None else '—';hip_text=f'{hr:.2f}' if hr is not None else '—';knee_sync=f'{ks:.2f}' if ks is not None else '—';left_knee_text=f'{lkr:.0f}°' if lkr is not None else '—';right_knee_text=f'{rkr:.0f}°' if rkr is not None else '—';left_vis=f'{lv:.0%}' if lv is not None else '—';right_vis=f'{rv:.0%}' if rv is not None else '—';reliable_legs=str(rl) if rl is not None else '—'
                    return f"{s['start_s']:.1f}–{s['end_s']:.1f}с: {s['count']} | v50 {mv:.2f} | v75 {p75:.2f} | elbow {er:.0f}° | sync {coupling} | strict {strict_text} | pre {pre_text} | post {post_text} | knee {knee_text} (L {left_knee_text}/R {right_knee_text}) | leg-vis L {left_vis}/R {right_vis} | reliable {reliable_legs}/2 | hip {hip_text} | knee-sync {knee_sync}{flag}" if mv is not None and p75 is not None else f"{s['start_s']:.1f}–{s['end_s']:.1f}с: {s['count']} | sync {coupling} | strict {strict_text} | pre {pre_text} | post {post_text} | knee {knee_text} (L {left_knee_text}/R {right_knee_text}) | leg-vis L {left_vis}/R {right_vis} | reliable {reliable_legs}/2 | hip {hip_text} | knee-sync {knee_sync}{flag}"
                segment_text='; '.join(seg_text(s) for s in segments[:5]) or 'нет'
                await update.message.reply_text('🧪 Анализ кружка\n\n'+f"🔎 Решение: {status_label}\n🧠 Причина: {class_reason}\n🏋️ Итоговый счёт: {final_count}\n👤 Качество позы: {confidence:.0%}\n🤝 Согласованность сигналов: {metrics.get('signal_agreement',0):.0%}\n🎯 Выбранный сигнал: {metrics.get('selected_signal','none')}\n🎞 Кадры: {metrics['usable_frames']}/{metrics['sampled_frames']} пригодны\n\n"+f"⏱ 3D temporal:\n• горизонтальных сегментов: {world.get('gated_segments',0)}\n• кадров внутри: {world.get('gated_frames',0)}/{world.get('sampled_frames',0)}\n• доля видео: {world.get('gated_ratio',0):.0%}\n• циклов внутри сегментов: {world.get('gated_pushup_count',0)}\n• сегменты: {segment_text}\n\n"+f"🌐 3D-геометрия:\n• world-кадры: {world.get('world_frames',0)}/{world.get('sampled_frames',0)}\n• горизонтально: {world.get('horizontal_ratio',0):.0%}\n• вертикально: {world.get('vertical_ratio',0):.0%}\n• прямой корпус: {world.get('straight_body_ratio',0):.0%}\n• push-up поза: {world.get('pushup_pose_ratio',0):.0%}\n• verticality: {world_v_text}\n• линия тела: {world_line_text}\n\n"+f"📐 2D (только диагностика):\n• горизонтально: {geometry.get('horizontal_ratio',0):.0%}\n• вертикально: {geometry.get('vertical_ratio',0):.0%}\n• прямой корпус: {geometry.get('straight_body_ratio',0):.0%}\n• push-up поза: {geometry.get('pushup_pose_ratio',0):.0%}\n• наклон торса: {tilt_text}\n• линия тела: {bodyline_text}\n\nСигналы:\n{signal_lines}")
            if status=='accepted':
                    daily_total=await asyncio.to_thread(db.daily_pushup_total,chat_id,user_id,day)
                    await update.message.reply_text(f"🏋️ Отжиманий: {final_count}\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {daily_total}")
                elif status=='rejected':
                    await update.message.reply_text(f"❌ Отжимания не засчитаны\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {await asyncio.to_thread(db.daily_pushup_total,chat_id,user_id,day)}")
                else:
                    await update.message.reply_text(f"⚠️ Не удалось уверенно распознать отжимания\n🎯 Качество распознавания: {confidence:.0%}\n📊 Всего за сегодня: {await asyncio.to_thread(db.daily_pushup_total,chat_id,user_id,day)}")
            logger.info('Push-up classification chat_id=%s message_id=%s status=%s reason=%s count=%s geometry=%s world=%s selected=%s agreement=%.3f signals=%s',chat_id,message_id,status,class_reason,final_count,geometry_diag,world_diag,metrics.get('selected_signal'),metrics.get('signal_agreement',0),signal_diag)
        except Exception as exc:
            if attempt:
                try:await asyncio.to_thread(db.finish_pushup_attempt,chat_id,message_id,'failed','pose_analysis_error')
                except Exception as db_exc:log_exception(logger,'Push-up attempt failure update failed',db_exc,settings)
            log_exception(logger,'Push-up pose analysis failed',exc,settings)
        finally:
            if temp_path:
                try:os.remove(temp_path)
                except FileNotFoundError:pass
                except OSError as exc:log_exception(logger,'Temporary video cleanup failed',exc,settings)
    async def video_note(update,context):
        user=update.effective_user
        if not user or user.is_bot or update.message.sender_chat:return
        day=today_moscow();member=await asyncio.to_thread(db.member,update.effective_chat.id,user.id)
        if not member or not member['active']:return
        await asyncio.to_thread(db.record,update.effective_chat.id,user.id,day)
        if settings.pushup_analysis_enabled:await inspect_video_note(update,context,user.id,day)
    async def error_handler(update,context):update_failed.set(True);log_exception(logger,'Telegram update failed',context.error,settings)
    for command,callback in [('start',start),('join',join),('members',members),('today',today),('chatid',chatid),('myid',myid)]:application.add_handler(CommandHandler(command,callback,filters=filters.UpdateType.MESSAGE))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.VIDEO_NOTE,video_note));application.add_error_handler(error_handler);return application
