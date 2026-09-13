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
    raise ValueError(
        "TELEGRAM_TOKEN не задан.\n"
        "Создайте файл .env рядом с config.py со строкой:\n"
        "  TELEGRAM_TOKEN=ваш_токен_из_BotFather"
    )

# ---------- Администраторы ----------
def _parse_admin_ids():
    raw = os.getenv("ADMIN_IDS", "")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids

ADMIN_IDS = _parse_admin_ids()

# ---------- DeepSeek ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError(
        "DEEPSEEK_API_KEY не задан. Добавьте в .env:\n"
        "  DEEPSEEK_API_KEY=ваш_ключ"
    )
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# ---------- Whisper ----------
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None
CHUNK_MINUTES = int(os.getenv("CHUNK_MINUTES", "30"))
CHUNK_OVERLAP_SECONDS = int(os.getenv("CHUNK_OVERLAP_SECONDS", "2"))

# float16 — максимальная точность, int8_float16 — компромисс
_default_compute = "float16" if WHISPER_DEVICE == "cuda" else "int8"
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", _default_compute)

# Список терминов/имён, которые часто встречаются в ваших записях.
# Максимум ~448 токенов. Разделяйте пробелами, как обычный текст.
WHISPER_INITIAL_PROMPT = os.getenv("WHISPER_INITIAL_PROMPT", "").strip()

# ---------- Диаризация (pyannote) ----------
# Рекомендуется: pyannote/speaker-diarization-community-1 (точнее 3.1)
# Fallback:      pyannote/speaker-diarization-3.1
DIARIZATION_MODEL = os.getenv(
    "DIARIZATION_MODEL",
    "pyannote/speaker-diarization-community-1"
)
# num_speakers=0 означает "определить автоматически".
# Если точно знаете число спикеров — задайте, качество будет выше.
DIARIZATION_NUM_SPEAKERS = int(os.getenv("DIARIZATION_NUM_SPEAKERS", "0"))
DIARIZATION_MIN_SPEAKERS = int(os.getenv("DIARIZATION_MIN_SPEAKERS", "1"))
DIARIZATION_MAX_SPEAKERS = int(os.getenv("DIARIZATION_MAX_SPEAKERS", "4"))
# Минимальная длительность реплики, чтобы считать её самостоятельной
DIARIZATION_MIN_DURATION = float(os.getenv("DIARIZATION_MIN_DURATION", "1.0"))
# Зазор между репликами одного спикера для склейки
DIARIZATION_GAP = float(os.getenv("DIARIZATION_GAP", "0.5"))

# ---------- Оценка времени ----------
WHISPER_SPEED_FACTOR = float(os.getenv("WHISPER_SPEED_FACTOR", "0.15"))
DIARIZATION_FACTOR = float(os.getenv("DIARIZATION_FACTOR", "0.1"))
DEEPSEEK_FACTOR = float(os.getenv("DEEPSEEK_FACTOR", "0.05"))
ANALYSIS_FACTOR = float(os.getenv("ANALYSIS_FACTOR", "0.15"))

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
    str(Path.home() / "transcriber_bot" / "storage")
)

# ---------- TTL ----------
PENDING_TTL_SECONDS = int(os.getenv("PENDING_TTL_SECONDS", "3600"))


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
    tmp_root = Path(tempfile.gettempdir())
    prefixes = ("transcriber_", "chunks_", "conference_")
    count = 0
    now = time.time()
    for prefix in prefixes:
        for d in tmp_root.glob(f"{prefix}*"):
            try:
                if now - d.stat().st_mtime < 6 * 3600:
                    continue
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink()
                count += 1
            except Exception:
                pass
    if count:
        print(f"[Cleanup] Удалено временных объектов: {count}")