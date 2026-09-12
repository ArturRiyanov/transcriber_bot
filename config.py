import os
from pathlib import Path

# ---------- Telegram ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE")

# ---------- DeepSeek ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY not set")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# ---------- Whisper ----------
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
CHUNK_MINUTES = int(os.getenv("CHUNK_MINUTES", "10"))

# ---------- Оценка времени ----------
WHISPER_SPEED_FACTOR = float(os.getenv("WHISPER_SPEED_FACTOR", "0.15"))
DIARIZATION_FACTOR = float(os.getenv("DIARIZATION_FACTOR", "0.1"))

# ---------- Hugging Face ----------
HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN")

# ---------- Улучшение DeepSeek ----------
IMPROVE_TEXT = os.getenv("IMPROVE_TEXT", "false").lower() == "true"

# ---------- Анализ интервью ----------
ANALYSIS_ENABLED = os.getenv("ANALYSIS_ENABLED", "true").lower() == "true"
CANDIDATE_NAME = os.getenv("CANDIDATE_NAME", "Кандидат")
POSITION_NAME = os.getenv("POSITION_NAME", "")

# ---------- Отправка аудио ----------
SEND_AUDIO = os.getenv("SEND_AUDIO", "true").lower() == "true"
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "45"))

# ---------- Картотека ----------
STORAGE_DIR = os.getenv(
    "STORAGE_DIR",
    "/mnt/c/Users/riano/OneDrive/Desktop/transcriber_bot/storage"
)

# ---------- Имена спикеров (fallback) ----------
def _parse_speaker_names():
    raw = os.getenv("SPEAKER_NAMES", "")
    mapping = {}
    if raw:
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                mapping[k.strip()] = v.strip()
    return mapping

SPEAKER_NAMES = _parse_speaker_names()

# ---------- Очистка временных файлов ----------
def cleanup_temp_files():
    patterns = ["temp_*", "chunk_*"]
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
        print(f"[Cleanup] Удалено временных файлов: {count}")