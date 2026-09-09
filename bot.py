import os
import requests
import subprocess
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

# ---------- Конфигурация ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY environment variable not set")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
model = WhisperModel("base", device="cpu", compute_type="int8")

# ---------- Вспомогательные функции ----------
def extract_audio_from_video(video_path: str, audio_path: str) -> bool:
    """Извлекает аудио из видео в WAV (16 кГц, моно) с помощью ffmpeg."""
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn",                # без видео
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y", audio_path
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg error] {e.stderr}")
        return False

def improve_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text

    prompt = (
        "Ты — профессиональный корректор транскрипций. Исправь все ошибки в тексте, "
        "расставь знаки препинания, заглавные буквы, сделай текст грамотным и читаемым. "
        "Если слово неразборчиво — попробуй восстановить по контексту. "
        "Не добавляй лишней информации, только исправляй.\n\n"
        f"Транскрипция:\n{text}"
    )

    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — помощник, исправляющий транскрипции речи."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 1500
    }

    try:
        response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[DeepSeek Error] {e}")
        return f"⚠️ Ошибка DeepSeek: {str(e)}"

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я транскрибирую аудио и видео.\n"
        "Отправь мне голосовое, аудиофайл или видео – я пришлю расшифровку и улучшенный текст."
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Определяем тип вложения
    attachment = update.message.voice or update.message.audio or update.message.video
    if not attachment:
        await update.message.reply_text("❌ Не удалось найти медиа.")
        return

    await update.message.reply_text("⏳ Скачиваю файл...")

    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'
    raw_path = f"temp_media.{file_ext}"
    await file.download_to_drive(raw_path)

    # Если это видео – извлекаем аудио
    if update.message.video:
        await update.message.reply_text("🎬 Извлекаю аудио из видео...")
        audio_path = "temp_audio.wav"
        if not extract_audio_from_video(raw_path, audio_path):
            await update.message.reply_text("❌ Не удалось извлечь аудио. Проверьте, что установлен ffmpeg.")
            os.remove(raw_path)
            return
        os.remove(raw_path)   # видео больше не нужно
    else:
        # Для аудио или голосового сразу используем файл
        # Но может быть .ogg, .m4a и т.п. – лучше конвертировать в WAV для единообразия
        audio_path = "temp_audio.wav"
        if not extract_audio_from_video(raw_path, audio_path):
            # если не получилось конвертировать, пробуем использовать как есть
            audio_path = raw_path
        else:
            os.remove(raw_path)

    await update.message.reply_text("🎧 Распознаю речь...")

    try:
        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            language='ru',
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False
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
        # Удаляем временные файлы
        if os.path.exists(raw_path):
            os.remove(raw_path)
        if os.path.exists(audio_path) and audio_path != raw_path:
            os.remove(audio_path)

if __name__ == "__main__":
    print("[LOG] Запуск бота с поддержкой видео...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling()