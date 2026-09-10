import os
import requests
import subprocess
import tempfile
import shutil
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel

try:
    from pyannote.audio import Pipeline
    import torch
    DIARIZATION_AVAILABLE = True
except ImportError:
    DIARIZATION_AVAILABLE = False
    print("[WARN] pyannote.audio не установлен")

# ---------- Конфигурация ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY not set")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
CHUNK_MINUTES = int(os.getenv("CHUNK_MINUTES", "10"))

# Коэффициенты для оценки времени (в секундах на секунду аудио)
WHISPER_SPEED_FACTOR = float(os.getenv("WHISPER_SPEED_FACTOR", "2.5"))
DIARIZATION_FACTOR = float(os.getenv("DIARIZATION_FACTOR", "1.0"))

model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")

# ---------- Очистка при старте ----------
def cleanup_temp_files():
    patterns = ["temp_*", "*.webm", "*.wav", "*.ogg", "*.mp4", "*.mp3", "*.m4a", "chunk_*"]
    count = 0
    for pattern in patterns:
        for f in Path(".").glob(pattern):
            try:
                if f.is_file():
                    f.unlink()
                    count += 1
            except Exception:
                pass
    if count:
        print(f"[Cleanup] Удалено файлов: {count}")

cleanup_temp_files()

# ---------- Диаризация ----------
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
        print(f"[WARN] Диаризация не загружена: {e}")

# ---------- Вспомогательные функции ----------
def extract_audio_from_video(video_path: str, audio_path: str) -> bool:
    cmd = ["ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-y", audio_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg Error] {e.stderr}")
        return False

def get_audio_duration(audio_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0

def split_audio(audio_path: str, chunk_seconds: int, out_dir: str) -> list:
    pattern = os.path.join(out_dir, "chunk_%03d.wav")
    cmd = ["ffmpeg", "-i", audio_path, "-f", "segment",
           "-segment_time", str(chunk_seconds), "-c", "copy", "-y", pattern]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    chunks = sorted(Path(out_dir).glob("chunk_*.wav"))
    return [str(c) for c in chunks]

def perform_diarization(audio_path: str):
    if diarization_pipeline is None:
        return None
    try:
        diarization = diarization_pipeline(audio_path)
        return [(t.start, t.end, spk) for t, _, spk in diarization.itertracks(yield_label=True)]
    except Exception as e:
        print(f"[Diarization Error] {e}")
        return None

def call_deepseek(text: str, model_name: str = "deepseek-reasoner") -> str:
    prompt = (
        "Ты — профессиональный корректор транскрипций с диаризацией. "
        "Исправь все ошибки распознавания речи, расставь знаки препинания, заглавные буквы. "
        "Сохрани метки говорящих (SPEAKER_01, SPEAKER_02 и т.д.) и оформи их единообразно. "
        "Разбей текст на абзацы по смене говорящего. "
        "НЕ добавляй информацию, которой нет. Если слово неразборчиво — оставь [неразборчиво]. "
        "НЕ меняй смысл реплик.\n\n"
        f"Транскрипция:\n{text}"
    )
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "Ты — эксперт по коррекции транскрипций. Отвечай только исправленным текстом."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 8000
    }
    response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=300)
    response.raise_for_status()
    data = response.json()
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()
    return content

def improve_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text

    try:
        result = call_deepseek(text, "deepseek-reasoner")
        if result:
            return result
        print("[DeepSeek] Reasoner вернул пусто, пробую chat")
    except requests.exceptions.Timeout:
        print("[DeepSeek] Reasoner timeout, пробую chat")
    except Exception as e:
        print(f"[DeepSeek] Reasoner error: {e}, пробую chat")

    try:
        result = call_deepseek(text, "deepseek-chat")
        if result:
            return result
    except Exception as e:
        print(f"[DeepSeek] Chat error: {e}")

    return text

def merge_transcription_with_diarization(transcription_segments, diarization_segments):
    if not diarization_segments:
        return " ".join(seg.text for seg in transcription_segments)
    intervals = list(diarization_segments)
    result_parts = []
    for seg in transcription_segments:
        best_speaker, max_overlap = None, 0
        for s_start, s_end, speaker in intervals:
            overlap = max(0, min(seg.end, s_end) - max(seg.start, s_start))
            if overlap > max_overlap:
                max_overlap, best_speaker = overlap, speaker
        if best_speaker:
            result_parts.append(f"[{best_speaker}] {seg.text.strip()}")
        else:
            result_parts.append(seg.text.strip())
    return "\n".join(result_parts)

def transcribe_audio(audio_path: str) -> str:
    duration = get_audio_duration(audio_path)
    print(f"[LOG] Длительность аудио: {duration:.1f} сек")

    if duration < CHUNK_MINUTES * 60 * 1.5:
        segments, _ = model.transcribe(
            audio_path, beam_size=5, language='ru', temperature=0.0,
            vad_filter=True, word_timestamps=True, condition_on_previous_text=False
        )
        segs = list(segments)
        diar = perform_diarization(audio_path) if diarization_pipeline else None
        return merge_transcription_with_diarization(segs, diar) if diar \
               else " ".join(s.text for s in segs)

    print(f"[LOG] Длинное аудио, чанки по {CHUNK_MINUTES} мин")
    tmp_dir = tempfile.mkdtemp(prefix="chunks_")
    try:
        chunks = split_audio(audio_path, CHUNK_MINUTES * 60, tmp_dir)
        print(f"[LOG] Чанков: {len(chunks)}")
        full_text_parts = []
        for i, chunk in enumerate(chunks, 1):
            print(f"[LOG] Обработка чанка {i}/{len(chunks)}")
            segments, _ = model.transcribe(
                chunk, beam_size=5, language='ru', temperature=0.0,
                vad_filter=True, word_timestamps=True, condition_on_previous_text=False
            )
            segs = list(segments)
            diar = perform_diarization(chunk) if diarization_pipeline else None
            if diar:
                full_text_parts.append(merge_transcription_with_diarization(segs, diar))
            else:
                full_text_parts.append(" ".join(s.text for s in segs))
        return "\n".join(full_text_parts)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def estimate_time(duration_sec: float) -> str:
    """Возвращает строку с оценкой времени обработки."""
    total_sec = duration_sec * WHISPER_SPEED_FACTOR
    if diarization_pipeline is not None:
        total_sec += duration_sec * DIARIZATION_FACTOR
    total_sec += 90  # запас на DeepSeek

    minutes = int(total_sec // 60)
    if minutes < 1:
        return "менее 1 минуты"
    if minutes < 60:
        return f"около {minutes} мин"
    hours = minutes // 60
    remainder = minutes % 60
    if remainder == 0:
        return f"около {hours} ч"
    return f"около {hours} ч {remainder} мин"

async def send_long_message(update: Update, text: str):
    """Отправляет длинный текст частями (Telegram лимит 4096)."""
    limit = 4000
    if len(text) <= limit:
        await update.message.reply_text(text)
        return
    parts = [text[i:i+limit] for i in range(0, len(text), limit)]
    for idx, part in enumerate(parts, 1):
        await update.message.reply_text(f"Часть {idx}/{len(parts)}:\n\n{part}")

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Транскрибатор аудио и видео с определением говорящих.\n"
        "Отправьте голосовое сообщение, аудиофайл или видео. "
        "Бот вернёт готовую расшифровку."
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attachment = update.message.voice or update.message.audio or update.message.video
    if not attachment:
        await update.message.reply_text("Не удалось найти медиафайл.")
        return

    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'

    tmp_dir = tempfile.mkdtemp(prefix="transcriber_")
    raw_path = os.path.join(tmp_dir, f"media.{file_ext}")
    audio_path = os.path.join(tmp_dir, "audio.wav")

    try:
        await file.download_to_drive(raw_path)

        # Извлекаем аудио
        if update.message.video or file_ext.lower() not in ("wav",):
            if not extract_audio_from_video(raw_path, audio_path):
                audio_path = raw_path
        else:
            audio_path = raw_path

        # Оценка времени
        duration = get_audio_duration(audio_path)
        if duration > 0:
            est = estimate_time(duration)
            await update.message.reply_text(
                f"Файл получен. Длительность: {int(duration // 60)} мин {int(duration % 60)} сек.\n"
                f"Обработка займёт {est}. Дождитесь ответа — я пришлю готовый текст."
            )
        else:
            await update.message.reply_text(
                "Файл получен. Обработка займёт некоторое время. Дождитесь ответа."
            )

        # Транскрипция
        raw_text = transcribe_audio(audio_path)
        if not raw_text.strip():
            await update.message.reply_text("Речь не обнаружена.")
            return

        # Улучшение
        improved = improve_text(raw_text)

        # Отправляем финальный результат
        await send_long_message(update, improved)

    except Exception as e:
        print(f"[Error] {e}")
        await update.message.reply_text(f"Ошибка обработки: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    print(f"[LOG] Запуск (Whisper {WHISPER_MODEL}/{WHISPER_DEVICE}, reasoner+chat)...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    print("[LOG] Бот запущен.")
    app.run_polling()