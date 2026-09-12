import os
import subprocess
from pathlib import Path


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
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-c", "copy",
        "-y", pattern
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    chunks = sorted(Path(out_dir).glob("chunk_*.wav"))
    return [str(c) for c in chunks]


def estimate_time(duration_sec: float, has_diarization: bool) -> str:
    from config import WHISPER_SPEED_FACTOR, DIARIZATION_FACTOR
    total_sec = duration_sec * WHISPER_SPEED_FACTOR
    if has_diarization:
        total_sec += duration_sec * DIARIZATION_FACTOR
    total_sec += 60
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