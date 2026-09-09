import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"

# Модель tiny – самая быстрая, используем VAD для пропуска тишины
model = WhisperModel("tiny", device="cpu", compute_type="int8")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я транскрибирую аудио.\n"
        "Отправь мне голосовое сообщение, аудиофайл или видео – я пришлю расшифровку текста."
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Определяем тип вложения
    attachment = (update.message.voice or update.message.audio or update.message.video)
    if not attachment:
        await update.message.reply_text("❌ Не удалось найти аудио.")
        return

    await update.message.reply_text("🎧 Секунду, распознаю речь...")

    # Скачиваем файл
    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'
    path = f"temp_audio.{file_ext}"
    await file.download_to_drive(path)

    try:
        # Транскрипция с оптимизированными параметрами
        segments, info = model.transcribe(
            path,
            beam_size=3,
            language='ru',
            temperature=0.0,
            vad_filter=True
        )
        text = " ".join(seg.text for seg in segments)

        if not text.strip():
            await update.message.reply_text("⚠️ Расшифровка пуста. Возможно, в аудио нет речи или оно слишком тихое.")
        else:
            await update.message.reply_text(f"📝 Расшифровка:\n\n{text}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка транскрипции: {e}")
    finally:
        # Удаляем временный файл
        if os.path.exists(path):
            os.remove(path)

if __name__ == "__main__":
    print("[LOG] Запуск бота (только транскрипция)...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling()