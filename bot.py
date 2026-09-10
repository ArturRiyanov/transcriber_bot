import os
import requests
import subprocess
import tempfile
import shutil
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

# ---------- Импорт для диаризации ----------
try:
    from pyannote.audio import Pipeline
    import torch
    DIARIZATION_AVAILABLE = True
except ImportError:
    DIARIZATION_AVAILABLE = False
    print("[WARN] pyannote.audio не установлен, диаризация недоступна")

# ---------- Конфигурация ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY environment variable not set")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# Модель Whisper: small (для GPU можно заменить на large-v3 через WHISPER_MODEL)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")  # на GPU будет "cuda"
model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")

# ---------- Очистка мусора при старте ----------
def cleanup_temp_files():
    """Удаляет все временные файлы в рабочей папке."""
    patterns = ["temp_*", "*.webm", "*.wav", "*.ogg", "*.mp4", "*.mp3", "*.m4a"]
    count = 0
    for pattern in patterns:
        for f in Path(".").glob(pattern):
            try:
                if f.is_file():
                    f.unlink()
                    count += 1
            except Exception as e:
                print(f"[Cleanup] Не удалось удалить {f}: {e}")
    if count:
        print(f"[Cleanup] Удалено файлов: {count}")

cleanup_temp_files()

# ---------- Инициализация диаризации ----------
diarization_pipeline = None
if DIARIZATION_AVAILABLE:
    try:
        diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization",
            use_auth_token=os.getenv("HUGGINGFACE_TOKEN", None)
        )
        device = torch.device("cuda" if WHISPER_DEVICE == "cuda" else "cpu")
        diarization_pipeline.to(device)
        print(f"[LOG] Диаризация инициализирована на {device}")
    except Exception as e:
        print(f"[WARN] Не удалось загрузить диаризацию: {e}")
        diarization_pipeline = None

# ---------- Вспомогательные функции ----------
def extract_audio_from_video(video_path: str, audio_path: str) -> bool:
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        "-y", audio_path
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg Error] {e.stderr}")
        return False

def perform_diarization(audio_path: str):
    if diarization_pipeline is None:
        return None
    try:
        diarization = diarization_pipeline(audio_path)
        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append((turn.start, turn.end, speaker))
        return segments
    except Exception as e:
        print(f"[Diarization Error] {e}")
        return None

def improve_text(text: str) -> str:
    """Отправляет текст в DeepSeek Reasoner для максимального качества."""
    if not text or len(text.strip()) < 5:
        return text

    prompt = (
        "Ты — профессиональный корректор транскрипций с диаризацией. "
        "Ниже дан текст, полученный автоматическим распознаванием речи с определением говорящих. "
        "В нём могут быть ошибки распознавания, отсутствовать пунктуация, искажены слова.\n\n"
        "Твоя задача:\n"
        "1. Исправить все ошибки распознавания, восстановив смысл по контексту.\n"
        "2. Расставить знаки препинания и заглавные буквы.\n"
        "3. Сохранить метки говорящих (SPEAKER_01, SPEAKER_02 и т.д.) и оформить их единообразно.\n"
        "4. Разбить текст на абзацы по смене говорящего.\n"
        "5. НЕ добавлять информацию, которой нет в оригинале. Если слово неразборчиво — оставь [неразборчиво].\n"
        "6. НЕ менять смысл реплик.\n\n"
        f"Транскрипция:\n{text}"
    )

    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-reasoner",   # самая мощная модель
        "messages": [
            {"role": "system", "content": "Ты — эксперт по коррекции транскрипций речи с диаризацией. Отвечай только исправленным текстом, без пояснений."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,             # для детерминизма
        "max_tokens": 4000
    }

    try:
        response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=180)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        print("[DeepSeek Error] Timeout после 180 сек")
        return "⚠️ DeepSeek не ответил вовремя. Попробуйте снова или используйте более короткое аудио."
    except Exception as e:
        print(f"[DeepSeek Error] {e}")
        return f"⚠️ Ошибка DeepSeek: {str(e)}"

def merge_transcription_with_diarization(transcription_segments, diarization_segments):
    if not diarization_segments:
        return " ".join(seg.text for seg in transcription_segments)

    speaker_intervals = list(diarization_segments)
    result_parts = []

    for seg in transcription_segments:
        seg_start, seg_end = seg.start, seg.end
        best_speaker = None
        max_overlap = 0
        for s_start, s_end, speaker in speaker_intervals:
            overlap = max(0, min(seg_end, s_end) - max(seg_start, s_start))
            if overlap > max_overlap:
                max_overlap = overlap
                best_speaker = speaker
        if best_speaker:
            result_parts.append(f"[{best_speaker}] {seg.text.strip()}")
        else:
            result_parts.append(seg.text.strip())

    return "\n".join(result_parts)

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Я транскрибирую аудио и видео с определением говорящих.\n"
        "Отправь мне голосовое, аудиофайл или видео."
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attachment = update.message.voice or update.message.audio or update.message.video
    if not attachment:
        await update.message.reply_text("❌ Не удалось найти медиа.")
        return

    await update.message.reply_text("⏳ Скачиваю файл...")

    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'

    # Работаем во временной папке
    tmp_dir = tempfile.mkdtemp(prefix="transcriber_")
    raw_path = os.path.join(tmp_dir, f"media.{file_ext}")
    audio_path = os.path.join(tmp_dir, "audio.wav")

    await file.download_to_drive(raw_path)

    try:
        # Извлекаем аудио (для видео и для не-WAV аудио)
        if update.message.video or file_ext.lower() not in ("wav",):
            if update.message.video:
                await update.message.reply_text("🎬 Извлекаю аудио из видео...")
            if extract_audio_from_video(raw_path, audio_path):
                # успех — используем audio_path
                pass
            else:
                # не удалось — используем исходный файл
                audio_path = raw_path
        else:
            audio_path = raw_path

        await update.message.reply_text("🎧 Распознаю речь...")

        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            language='ru',
            temperature=0.0,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False
        )
        transcription_segments = list(segments)

        # Диаризация
        diarization_result = None
        if DIARIZATION_AVAILABLE and diarization_pipeline is not None:
            await update.message.reply_text("🗣️ Определяю говорящих...")
            diarization_result = perform_diarization(audio_path)

        # Сборка текста
        if diarization_result:
            raw_text_with_speakers = merge_transcription_with_diarization(transcription_segments, diarization_result)
        else:
            raw_text_with_speakers = " ".join(seg.text for seg in transcription_segments)

        if not raw_text_with_speakers.strip():
            await update.message.reply_text("⚠️ Речь не обнаружена.")
            return

        # Сырая расшифровка (обрезаем для Telegram)
        await update.message.reply_text(f"📝 Сырая расшифровка:\n\n{raw_text_with_speakers[:4000]}")

        # Улучшение через DeepSeek Reasoner
        await update.message.reply_text("🧠 Улучшаю текст через DeepSeek Reasoner (это может занять 1-2 минуты)...")
        improved = improve_text(raw_text_with_speakers)

        # Telegram ограничивает 4096 символов — режем при необходимости
        if len(improved) <= 4000:
            await update.message.reply_text(f"✨ Улучшенный текст:\n\n{improved}")
        else:
            # Отправляем частями
            parts = [improved[i:i+4000] for i in range(0, len(improved), 4000)]
            for idx, part in enumerate(parts):
                header = f"✨ Улучшенный текст (часть {idx+1}/{len(parts)}):\n\n"
                await update.message.reply_text(f"{header}{part}")

    except Exception as e:
        print(f"[Error] {e}")
        await update.message.reply_text(f"⚠️ Ошибка: {e}")
    finally:
        # Гарантированная очистка всей временной папки
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            print(f"[Cleanup] Удалена папка {tmp_dir}")
        except Exception as e:
            print(f"[Cleanup Error] {e}")

if __name__ == "__main__":
    print(f"[LOG] Запуск бота (Whisper {WHISPER_MODEL} на {WHISPER_DEVICE}, DeepSeek Reasoner)...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling()