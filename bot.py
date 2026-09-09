import os
import requests
import subprocess
import tempfile
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

# ---------- Импорт для диаризации ----------
try:
    from pyannote.audio import Pipeline
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

# Модель Whisper: выбираем small для лучшего качества (если памяти мало – переключите на base)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # можно заменить на "base" через переменную
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

# ---------- Инициализация диаризации (если доступна) ----------
diarization_pipeline = None
if DIARIZATION_AVAILABLE:
    try:
        # Используем предобученную модель pyannote
        diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization",
            use_auth_token=os.getenv("HUGGINGFACE_TOKEN", None)  # если требуется токен
        )
        # Отправляем на CPU
        diarization_pipeline.to(torch.device("cpu"))
        print("[LOG] Диаризация инициализирована")
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
    except subprocess.CalledProcessError:
        return False

def perform_diarization(audio_path: str):
    """Возвращает список сегментов с метками говорящих."""
    if diarization_pipeline is None:
        return None
    try:
        diarization = diarization_pipeline(audio_path)
        # Преобразуем в список (start, end, speaker)
        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append((turn.start, turn.end, speaker))
        return segments
    except Exception as e:
        print(f"[Diarization Error] {e}")
        return None

def improve_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text

    prompt = (
        "Ты — профессиональный корректор транскрипций. Исправь все ошибки, расставь знаки препинания, "
        "заглавные буквы, сделай текст грамотным и читаемым. Если в тексте есть обозначения говорящих "
        "(например, 'SPEAKER_01:'), сохрани их, но оформи красиво.\n\n"
        f"Транскрипция:\n{text}"
    )

    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — корректор транскрипций речи с диаризацией."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 2000
    }

    try:
        response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[DeepSeek Error] {e}")
        return f"⚠️ Ошибка DeepSeek: {str(e)}"

def merge_transcription_with_diarization(transcription_segments, diarization_segments):
    """
    Накладывает временные метки транскрипции на диаризацию.
    transcription_segments – список от Whisper (с start, end, text)
    diarization_segments – список от pyannote (start, end, speaker)
    Возвращает строку с текстом, разбитую по говорящим.
    """
    if not diarization_segments:
        return " ".join(seg.text for seg in transcription_segments)

    # Преобразуем диаризацию в интервалы
    speaker_intervals = []
    for start, end, speaker in diarization_segments:
        speaker_intervals.append((start, end, speaker))

    # Для каждого сегмента транскрипции определяем основного говорящего
    result_parts = []
    for seg in transcription_segments:
        seg_start = seg.start
        seg_end = seg.end
        # Ищем пересечение с диаризацией
        best_speaker = None
        max_overlap = 0
        for s_start, s_end, speaker in speaker_intervals:
            overlap = max(0, min(seg_end, s_end) - max(seg_start, s_start))
            if overlap > max_overlap:
                max_overlap = overlap
                best_speaker = speaker
        if best_speaker:
            result_parts.append(f"[{best_speaker}] {seg.text}")
        else:
            result_parts.append(seg.text)

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
    raw_path = f"temp_media.{file_ext}"
    await file.download_to_drive(raw_path)

    # Конвертируем в WAV
    audio_path = "temp_audio.wav"
    if update.message.video:
        await update.message.reply_text("🎬 Извлекаю аудио из видео...")
        if not extract_audio_from_video(raw_path, audio_path):
            await update.message.reply_text("❌ Не удалось извлечь аудио. Проверьте ffmpeg.")
            os.remove(raw_path)
            return
        os.remove(raw_path)
    else:
        if not extract_audio_from_video(raw_path, audio_path):
            # если не получилось, используем исходный файл
            audio_path = raw_path
        else:
            os.remove(raw_path)

    await update.message.reply_text("🎧 Распознаю речь...")

    try:
        # Транскрипция Whisper
        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            language='ru',
            temperature=0.0,
            vad_filter=True,
            word_timestamps=True,   # для точной привязки к диаризации
            condition_on_previous_text=False
        )
        transcription_segments = list(segments)  # материализуем

        # Диаризация (если доступна)
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

        # Отправляем сырой текст (с метками говорящих)
        await update.message.reply_text(f"📝 Сырая расшифровка:\n\n{raw_text_with_speakers[:4000]}")  # обрезаем для Telegram

        # Улучшаем через DeepSeek
        await update.message.reply_text("🔄 Улучшаю текст через DeepSeek...")
        improved = improve_text(raw_text_with_speakers)
        await update.message.reply_text(f"✨ Улучшенный текст:\n\n{improved[:4000]}")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")
    finally:
        for path in [raw_path, audio_path]:
            if os.path.exists(path):
                os.remove(path)

if __name__ == "__main__":
    print("[LOG] Запуск бота с поддержкой диаризации...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling()