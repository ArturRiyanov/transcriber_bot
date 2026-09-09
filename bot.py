import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

# ---------- Конфигурация ----------
TELEGRAM_TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY environment variable not set")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# Модель Whisper (tiny – быстрая)
model = WhisperModel("tiny", device="cpu", compute_type="int8")

# ---------- Функция улучшения текста через DeepSeek ----------
def improve_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text

    prompt = (
        "Ты — профессиональный корректор. Исправь ошибки в расшифровке речи, "
        "расставь знаки препинания, заглавные буквы, сделай текст грамотным и читаемым. "
        "Сохрани смысл дословно, не добавляй и не убирай информацию.\n\n"
        f"Текст для исправления:\n{text}"
    )

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — помощник, исправляющий расшифровки речи."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 2000
    }

    try:
        response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        improved = data["choices"][0]["message"]["content"].strip()
        return improved
    except Exception as e:
        print(f"[DeepSeek Error] {e}")
        return text

# ---------- Команды бота ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я транскрибирую аудио и улучшаю текст через нейросеть.\n"
        "Отправь голосовое, аудио или видео – я пришлю расшифровку и исправленный вариант."
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attachment = (update.message.voice or update.message.audio or update.message.video)
    if not attachment:
        await update.message.reply_text("❌ Не удалось найти аудио.")
        return

    await update.message.reply_text("🎧 Распознаю речь...")

    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'
    path = f"temp_audio.{file_ext}"
    await file.download_to_drive(path)

    try:
        segments, info = model.transcribe(
            path,
            beam_size=3,
            language='ru',
            temperature=0.0,
            vad_filter=True
        )
        raw_text = " ".join(seg.text for seg in segments)

        if not raw_text.strip():
            await update.message.reply_text("⚠️ Речь не обнаружена.")
            return

        await update.message.reply_text(f"📝 Сырая расшифровка:\n\n{raw_text}")
        await update.message.reply_text("🔄 Улучшаю текст через DeepSeek...")
        improved = improve_text(raw_text)
        await update.message.reply_text(f"✨ Улучшенный текст:\n\n{improved}")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

if __name__ == "__main__":
    print("[LOG] Запуск бота с улучшением через DeepSeek...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling()