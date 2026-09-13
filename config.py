import os
import shutil
import tempfile
import time
from pathlib import Path

# ---------- Автозагрузка .env ----------
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=True)
except ImportError:
    pass

# ---------- Telegram ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в .env")

# ---------- DeepSeek ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY not set")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# ---------- Whisper ----------
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru")

# float16 — максимальная точность на GPU
_default_compute = "float16" if WHISPER_DEVICE == "cuda" else "int8"
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", _default_compute)

# Контекст-подсказка для Whisper (термины, имена). До 448 токенов.
WHISPER_INITIAL_PROMPT = os.getenv("WHISPER_INITIAL_PROMPT", "").strip()

# Длинные файлы режем по N минут
CHUNK_MINUTES = int(os.getenv("CHUNK_MINUTES", "30"))

# ---------- Оценка времени ----------
WHISPER_SPEED_FACTOR = float(os.getenv("WHISPER_SPEED_FACTOR", "0.15"))
DIARIZATION_FACTOR = float(os.getenv("DIARIZATION_FACTOR", "0.1"))

# ---------- Диаризация (pyannote) ----------
DIARIZATION_MODEL = os.getenv(
    "DIARIZATION_MODEL",
    "pyannote/speaker-diarization-community-1"
)
# 0 = автоопределение. Если знаете точно — поставьте число (2 для интервью 1-на-1)
DIARIZATION_NUM_SPEAKERS = int(os.getenv("DIARIZATION_NUM_SPEAKERS", "0"))
DIARIZATION_MIN_SPEAKERS = int(os.getenv("DIARIZATION_MIN_SPEAKERS", "1"))
DIARIZATION_MAX_SPEAKERS = int(os.getenv("DIARIZATION_MAX_SPEAKERS", "4"))
# Реплики короче этой длительности присваиваются окружающему спикеру
DIARIZATION_MIN_DURATION = float(os.getenv("DIARIZATION_MIN_DURATION", "1.0"))
# Склейка соседних реплик одного спикера при зазоре < GAP
DIARIZATION_GAP = float(os.getenv("DIARIZATION_GAP", "0.5"))

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

# ---------- Простой режим ----------
# Запись не сохраняется в картотеку и не создаёт DOCX, если:
# - это не интервью И
#   (длительность <= SIMPLE_MAX_DURATION_SEC ИЛИ текст <= SIMPLE_MAX_CHARS).
# Пользователь может сохранить такую запись кнопкой в чате.
SIMPLE_MAX_DURATION_SEC = int(os.getenv("SIMPLE_MAX_DURATION_SEC", "30"))
SIMPLE_MAX_CHARS = int(os.getenv("SIMPLE_MAX_CHARS", "150"))

# Аудио короче этого порога не отправляется на классификацию в DeepSeek
# (экономия времени и денег — короткие «алло, привет» не нуждаются в анализе)
SKIP_CLASSIFY_SEC = int(os.getenv("SKIP_CLASSIFY_SEC", "10"))

# ---------- Картотека ----------
STORAGE_DIR = os.getenv(
    "STORAGE_DIR",
    str(Path.home() / "transcriber_bot" / "storage")
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