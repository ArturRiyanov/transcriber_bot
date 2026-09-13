import os
import tempfile
import shutil
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from config import TELEGRAM_TOKEN, cleanup_temp_files
from audio_utils import extract_audio_from_video, get_audio_duration, estimate_time
from transcription import transcribe_audio, segments_to_text, diarization_pipeline
from deepseek_client import improve_text, analyze_interview, extract_speakers_and_roles
from conference import join_conference, stop_conference, active_sessions, check_auto_stop
from docx_builder import create_docx
from storage import (
    save_entry, list_entries, get_entry,
    get_file_path, delete_entry, get_stats, _safe_filename
)

cleanup_temp_files()


# ---------- Меню ----------
def get_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📁 Мои отчёты", callback_data="menu_reports")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _auto_stop_watcher(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(active_sessions.keys()):
        try:
            stopped = await check_auto_stop(chat_id)
            if stopped:
                print(f"[AutoStop] Сессия {chat_id} остановлена")
        except Exception as e:
            print(f"[AutoStop] Ошибка: {e}")


async def send_long_message(update: Update, text: str):
    limit = 4000
    if len(text) <= limit:
        await update.message.reply_text(text)
        return
    parts = [text[i:i+limit] for i in range(0, len(text), limit)]
    for idx, part in enumerate(parts, 1):
        await update.message.reply_text(f"Часть {idx}/{len(parts)}:\n\n{part}")


# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎙️ Транскрибатор аудио, видео и конференций.\n\n"
        "Что я умею:\n"
        "• Отправьте голосовое, аудио или видео — пришлю расшифровку с анализом.\n"
        "• Отправьте ссылку на Яндекс.Телемост — подключусь, запишу и пришлю отчёт.\n\n"
        "Меню:"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Главное меню:", reply_markup=get_main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 Справка:\n\n"
        "🔹 Отправьте аудио / видео / голосовое — получите транскрипцию и отчёт.\n"
        "🔹 Отправьте ссылку telemost.yandex.ru — бот подключится к конференции.\n"
        "🔹 /stop — принудительно остановить запись конференции.\n"
        "🔹 /menu — главное меню.\n\n"
        "📁 Все отчёты и аудиозаписи сохраняются в личной картотеке."
    )
    await update.message.reply_text(text)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sessions:
        await update.message.reply_text("Нет активной сессии конференции.")
        return
    await stop_conference(chat_id, update)


# ---------- Обработка медиа ----------
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attachment = update.message.voice or update.message.audio or update.message.video
    if not attachment:
        await update.message.reply_text("Не удалось найти медиафайл.")
        return

    user_id = update.effective_user.id
    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'

    tmp_dir = tempfile.mkdtemp(prefix="transcriber_")
    raw_path = os.path.join(tmp_dir, f"media.{file_ext}")
    audio_path = os.path.join(tmp_dir, "audio.wav")

    try:
        await file.download_to_drive(raw_path)

        if update.message.video or file_ext.lower() not in ("wav",):
            if not extract_audio_from_video(raw_path, audio_path):
                audio_path = raw_path
        else:
            audio_path = raw_path

        duration = get_audio_duration(audio_path)
        if duration > 0:
            est = estimate_time(duration, diarization_pipeline is not None)
            await update.message.reply_text(
                f"Файл получен. Длительность: {int(duration // 60)} мин {int(duration % 60)} сек.\n"
                f"Обработка займёт {est}."
            )

        await update.message.reply_text("Распознаю речь...")
        segments = transcribe_audio(audio_path)
        if not segments:
            await update.message.reply_text("Речь не обнаружена.")
            return

        transcript_text = segments_to_text(segments)
        improved = improve_text(transcript_text)

        speakers_info = {}
        try:
            speakers_info = extract_speakers_and_roles(transcript_text)
        except Exception as e:
            print(f"[Speakers] Ошибка: {e}")

        # Fallback
        real = {s.get('speaker') for s in segments if s.get('speaker')}
        ds = [k for k in speakers_info.keys() if k not in ("candidate_speaker", "interviewer_speaker")]
        if len(real) <= 1 and len(ds) >= 2:
            try:
                from deepseek_client import resplit_by_speakers
                from transcription import text_to_segments
                resplit_text = resplit_by_speakers(transcript_text, speakers_info)
                new_segments = text_to_segments(resplit_text)
                if new_segments and any(s.get('speaker') for s in new_segments):
                    segments = new_segments
            except Exception as e:
                print(f"[Fallback] {e}")

        effective_candidate = "Аудио"
        effective_position = ""
        if speakers_info:
            cand = speakers_info.get("candidate_speaker")
            if cand and cand in speakers_info:
                info = speakers_info[cand]
                if isinstance(info, dict):
                    if info.get("name"):
                        effective_candidate = info["name"]
                    if info.get("position"):
                        effective_position = info["position"]

        # Анализ (если длинное)
        analysis = ""
        if duration >= 120:
            await update.message.reply_text("Формирую аналитический отчёт...")
            analysis = analyze_interview(improved, speakers_info=speakers_info)

        # DOCX
        docx_path = os.path.join(tmp_dir, "report.docx")
        create_docx(
            segments=segments,
            output_path=docx_path,
            meeting_url="",
            analysis_text=analysis,
            candidate_name=effective_candidate,
            position=effective_position,
            speakers_info=speakers_info
        )

        # Сохраняем в картотеку
        save_entry(
            user_id=user_id,
            source_audio=audio_path,
            report_path=docx_path,
            candidate_name=effective_candidate,
            position=effective_position or "Медиа",
            duration=duration,
            num_speakers=len({s.get('speaker') for s in segments if s.get('speaker')}),
            kind="media"
        )

        # Красивое имя
        safe_cand = _safe_filename(effective_candidate or "Аудио")
        safe_pos = _safe_filename(effective_position) if effective_position else "Медиа"
        date_part = datetime.now().strftime("%Y-%m-%d_%H-%M")
        base_name = f"{safe_cand}_{safe_pos}_{date_part}"

        # Отправка DOCX
        with open(docx_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"{base_name}.docx",
                caption="Отчёт: транскрипция + анализ"
            )

        # Отправка текста для быстрого просмотра
        await send_long_message(update, improved)

        await update.message.reply_text(
            "✅ Сохранено в картотеке. Откройте /menu → Мои отчёты."
        )

    except Exception as e:
        print(f"[Error] {e}")
        await update.message.reply_text(f"Ошибка обработки: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("Это не ссылка на Яндекс.Телемост.")
        return

    if chat_id in active_sessions:
        await update.message.reply_text("Уже есть активная сессия. Отправьте /stop.")
        return

    await update.message.reply_text("Подключаюсь к конференции...")
    await join_conference(chat_id, url, update)


# ---------- Callback-кнопки ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu_reports":
        await show_reports(query, user_id)

    elif data == "menu_stats":
        stats = get_stats(user_id)
        mins = stats["total_duration_sec"] // 60
        secs = stats["total_duration_sec"] % 60
        text = (
            f"📊 Статистика:\n\n"
            f"• Всего записей: {stats['total']}\n"
            f"• Общая длительность: {mins} мин {secs} сек"
        )
        kb = [[InlineKeyboardButton("🔙 Назад", callback_data="menu_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_help":
        text = (
            "📖 Справка:\n\n"
            "🔹 Отправьте аудио/видео/голосовое — получите отчёт.\n"
            "🔹 Отправьте ссылку telemost.yandex.ru — бот запишет конференцию.\n"
            "🔹 /stop — остановить запись.\n\n"
            "Все отчёты сохраняются в картотеке."
        )
        kb = [[InlineKeyboardButton("🔙 Назад", callback_data="menu_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_back":
        await query.message.edit_text("📋 Главное меню:", reply_markup=get_main_menu())

    elif data.startswith("rep_"):
        entry_id = data[4:]
        await show_entry_detail(query, user_id, entry_id)

    elif data.startswith("dl_docx_"):
        entry_id = data[8:]
        await send_file(query, user_id, entry_id, kind="report")

    elif data.startswith("dl_audio_"):
        entry_id = data[9:]
        await send_file(query, user_id, entry_id, kind="audio")

    elif data.startswith("del_"):
        entry_id = data[4:]
        if delete_entry(user_id, entry_id):
            await query.answer("Удалено")
            await show_reports(query, user_id)
        else:
            await query.answer("Не найдено")


async def show_reports(query, user_id: int):
    entries = list_entries(user_id)
    if not entries:
        kb = [[InlineKeyboardButton("🔙 Назад", callback_data="menu_back")]]
        await query.message.edit_text(
            "📁 Картотека пуста.\nОтправьте аудио или ссылку на Телемост.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    keyboard = []
    for e in entries[:10]:
        label = f"📄 {e['candidate']}"
        if e.get('position'):
            label += f" — {e['position']}"
        label += f" ({e['datetime']})"
        if len(label) > 64:
            label = label[:61] + "..."
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rep_{e['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_back")])

    await query.message.edit_text(
        f"📁 Ваши отчёты ({len(entries)}):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_entry_detail(query, user_id: int, entry_id: str):
    entry = get_entry(user_id, entry_id)
    if not entry:
        await query.answer("Запись не найдена")
        return

    mins = entry["duration_sec"] // 60
    secs = entry["duration_sec"] % 60

    text = (
        f"📋 {entry['candidate']}\n"
        f"Должность: {entry.get('position') or '—'}\n"
        f"Дата: {entry['datetime']}\n"
        f"Длительность: {mins} мин {secs} сек\n"
        f"Спикеров: {entry.get('num_speakers', 0)}"
    )

    keyboard = []
    if entry.get("report_file"):
        keyboard.append([InlineKeyboardButton("📄 Скачать DOCX", callback_data=f"dl_docx_{entry_id}")])
    if entry.get("audio_file"):
        keyboard.append([InlineKeyboardButton("🎧 Скачать аудио", callback_data=f"dl_audio_{entry_id}")])
    keyboard.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{entry_id}")])
    keyboard.append([InlineKeyboardButton("🔙 К списку", callback_data="menu_reports")])

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def send_file(query, user_id: int, entry_id: str, kind: str):
    path = get_file_path(user_id, entry_id, kind=kind)
    if not path:
        await query.answer("Файл не найден")
        return

    entry = get_entry(user_id, entry_id)
    filename = os.path.basename(path)
    caption = f"{entry.get('candidate', '')} — {entry.get('position', '')}"

    try:
        if kind == "report":
            with open(path, "rb") as f:
                await query.message.reply_document(
                    document=f, filename=filename, caption=caption
                )
        else:
            with open(path, "rb") as f:
                await query.message.reply_audio(
                    audio=f, filename=filename, caption=caption
                )
    except Exception as e:
        await query.message.reply_text(f"Ошибка отправки: {e}")


# ---------- Запуск ----------
if __name__ == "__main__":
    from config import WHISPER_MODEL, WHISPER_DEVICE, ANALYSIS_ENABLED, STORAGE_DIR
    print(f"[LOG] Запуск (Whisper {WHISPER_MODEL}/{WHISPER_DEVICE}, "
          f"анализ={'вкл' if ANALYSIS_ENABLED else 'выкл'}, storage={STORAGE_DIR})...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"telemost\.yandex\.ru"),
        handle_link
    ))

    if app.job_queue:
        app.job_queue.run_repeating(_auto_stop_watcher, interval=3, first=5)
    else:
        print("[WARN] JobQueue недоступен. Установите: pip install 'python-telegram-bot[job-queue]'")

    print("[LOG] Бот запущен.")
    app.run_polling()