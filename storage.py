import os
import re
import json
import shutil
from datetime import datetime
from pathlib import Path

from config import STORAGE_DIR


def _safe_filename(name: str) -> str:
    """Убирает недопустимые символы для файловой системы."""
    if not name:
        return "Без_имени"
    cleaned = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name).strip()
    return cleaned or "Без_имени"


def get_user_dir(user_id: int) -> Path:
    user_dir = Path(STORAGE_DIR) / str(user_id)
    (user_dir / "audio").mkdir(parents=True, exist_ok=True)
    (user_dir / "reports").mkdir(parents=True, exist_ok=True)
    return user_dir


def _load_index(user_id: int) -> list:
    index_path = get_user_dir(user_id) / "index.json"
    if not index_path.exists():
        return []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(user_id: int, data: list):
    index_path = get_user_dir(user_id) / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_entry(user_id: int, source_audio: str = None, report_path: str = None,
               candidate_name: str = "Кандидат", position: str = "",
               duration: float = 0.0, num_speakers: int = 0,
               meeting_url: str = "", kind: str = "interview") -> dict:
    """
    Сохраняет запись в картотеку пользователя.
    kind='interview' — отчёт с конференции (аудио + DOCX).
    kind='media' — обработанный пользовательский файл (аудио + DOCX).
    Возвращает метаданные записи.
    """
    user_dir = get_user_dir(user_id)
    ts = datetime.now()
    ts_id = ts.strftime("%Y%m%d_%H%M%S")
    ts_human = ts.strftime("%d.%m.%Y %H:%M")

    safe_cand = _safe_filename(candidate_name or "Кандидат")
    safe_pos = _safe_filename(position) if position else "Без_должности"
    date_part = ts.strftime("%Y-%m-%d_%H-%M")

    base_name = f"{safe_cand}_{safe_pos}_{date_part}"

    audio_filename = f"{base_name}.wav"
    report_filename = f"{base_name}.docx"

    audio_dst = user_dir / "audio" / audio_filename
    report_dst = user_dir / "reports" / report_filename

    audio_saved = False
    if source_audio and os.path.exists(source_audio):
        try:
            shutil.copy(source_audio, audio_dst)
            audio_saved = True
        except Exception as e:
            print(f"[Storage] Не удалось скопировать аудио: {e}")

    report_saved = False
    if report_path and os.path.exists(report_path):
        try:
            shutil.copy(report_path, report_dst)
            report_saved = True
        except Exception as e:
            print(f"[Storage] Не удалось скопировать отчёт: {e}")

    entry = {
        "id": ts_id,
        "datetime": ts_human,
        "candidate": candidate_name or "Кандидат",
        "position": position or "",
        "duration_sec": int(duration),
        "num_speakers": int(num_speakers),
        "meeting_url": meeting_url,
        "kind": kind,
        "audio_file": audio_filename if audio_saved else "",
        "report_file": report_filename if report_saved else "",
    }

    index = _load_index(user_id)
    index.insert(0, entry)
    _save_index(user_id, index)

    print(f"[Storage] Запись сохранена: {base_name}")
    return entry


def list_entries(user_id: int) -> list:
    return _load_index(user_id)


def get_entry(user_id: int, entry_id: str) -> dict:
    for e in _load_index(user_id):
        if e["id"] == entry_id:
            return e
    return {}


def get_file_path(user_id: int, entry_id: str, kind: str = "report") -> str:
    """kind='report' или 'audio'. Возвращает путь или пустую строку."""
    user_dir = get_user_dir(user_id)
    entry = get_entry(user_id, entry_id)
    if not entry:
        return ""
    if kind == "report":
        filename = entry.get("report_file", "")
        folder = "reports"
    else:
        filename = entry.get("audio_file", "")
        folder = "audio"
    if not filename:
        return ""
    path = user_dir / folder / filename
    return str(path) if path.exists() else ""


def delete_entry(user_id: int, entry_id: str) -> bool:
    user_dir = get_user_dir(user_id)
    index = _load_index(user_id)
    for i, entry in enumerate(index):
        if entry["id"] == entry_id:
            for folder, key in (("reports", "report_file"), ("audio", "audio_file")):
                filename = entry.get(key, "")
                if filename:
                    f = user_dir / folder / filename
                    if f.exists():
                        try:
                            f.unlink()
                        except Exception:
                            pass
            index.pop(i)
            _save_index(user_id, index)
            return True
    return False


def get_stats(user_id: int) -> dict:
    entries = _load_index(user_id)
    total_duration = sum(e.get("duration_sec", 0) for e in entries)
    return {
        "total": len(entries),
        "total_duration_sec": total_duration,
    }