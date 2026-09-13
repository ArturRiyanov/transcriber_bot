import re
import tempfile
import shutil
from pathlib import Path
from faster_whisper import WhisperModel

from config import WHISPER_MODEL, WHISPER_DEVICE, CHUNK_MINUTES, HUGGINGFACE_TOKEN
from audio_utils import get_audio_duration, split_audio

model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")

try:
    from pyannote.audio import Pipeline
    import torch
    DIARIZATION_AVAILABLE = True
except ImportError:
    DIARIZATION_AVAILABLE = False
    print("[WARN] pyannote.audio не установлен")

diarization_pipeline = None
if DIARIZATION_AVAILABLE:
    try:
        diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=HUGGINGFACE_TOKEN
        )
        device = torch.device("cuda" if WHISPER_DEVICE == "cuda" else "cpu")
        diarization_pipeline.to(device)
        print(f"[LOG] Диаризация инициализирована на {device}")
    except Exception as e:
        print(f"[WARN] Диаризация не загружена: {e}")
        diarization_pipeline = None


def perform_diarization(audio_path: str):
    if diarization_pipeline is None:
        return None
    try:
        diarization = diarization_pipeline(audio_path)
        return [(t.start, t.end, spk) for t, _, spk in diarization.itertracks(yield_label=True)]
    except Exception as e:
        print(f"[Diarization Error] {e}")
        return None


def merge_transcription_with_diarization(transcription_segments, diarization_segments, time_offset: float = 0.0):
    if not diarization_segments:
        text = " ".join(seg.text.strip() for seg in transcription_segments)
        return [{
            'speaker': None,
            'text': text,
            'start': (transcription_segments[0].start + time_offset) if transcription_segments else 0.0,
            'end': (transcription_segments[-1].end + time_offset) if transcription_segments else 0.0,
        }]

    intervals = list(diarization_segments)
    raw = []
    for seg in transcription_segments:
        best_speaker, max_overlap = None, 0
        for s_start, s_end, speaker in intervals:
            overlap = max(0, min(seg.end, s_end) - max(seg.start, s_start))
            if overlap > max_overlap:
                max_overlap, best_speaker = overlap, speaker
        raw.append({
            'speaker': best_speaker,
            'text': seg.text.strip(),
            'start': seg.start + time_offset,
            'end': seg.end + time_offset,
        })

    grouped = []
    for item in raw:
        if grouped and grouped[-1]['speaker'] == item['speaker']:
            grouped[-1]['text'] += " " + item['text']
            grouped[-1]['end'] = item['end']
        else:
            grouped.append(dict(item))
    return grouped


def segments_to_text(segments: list) -> str:
    if not segments:
        return ""
    if all(s['speaker'] is None for s in segments):
        return " ".join(s['text'] for s in segments)
    return "\n".join(f"[{s['speaker'] or 'SPEAKER_UNKNOWN'}] {s['text']}" for s in segments)


def text_to_segments(text: str) -> list:
    """Парсит '[SPEAKER_XX] текст' в список сегментов."""
    if not text:
        return []

    pattern = re.compile(r'\[(SPEAKER_\w+)\]\s*(.+?)(?=\n\[SPEAKER_\w+\]|\Z)', re.DOTALL)
    matches = pattern.findall(text)

    if not matches:
        return [{'speaker': None, 'text': text.strip(), 'start': 0.0, 'end': 0.0}]

    segments = []
    for speaker, content in matches:
        content = content.strip().replace('\n', ' ')
        if content:
            segments.append({
                'speaker': speaker,
                'text': content,
                'start': 0.0,
                'end': 0.0,
            })
    return segments


def _transcribe_single(path: str, time_offset: float = 0.0) -> list:
    segments, _ = model.transcribe(
        path, beam_size=5, language='ru', temperature=0.0,
        vad_filter=True, word_timestamps=True,
        condition_on_previous_text=False
    )
    segs = list(segments)
    diar = perform_diarization(path) if diarization_pipeline else None
    return merge_transcription_with_diarization(segs, diar, time_offset)


def transcribe_audio(audio_path: str) -> list:
    duration = get_audio_duration(audio_path)
    print(f"[LOG] Длительность аудио: {duration:.1f} сек")

    if duration < CHUNK_MINUTES * 60 * 1.5:
        return _transcribe_single(audio_path, time_offset=0.0)

    print(f"[LOG] Длинное аудио, чанки по {CHUNK_MINUTES} мин")
    tmp_dir = tempfile.mkdtemp(prefix="chunks_")
    try:
        chunks = split_audio(audio_path, CHUNK_MINUTES * 60, tmp_dir)
        print(f"[LOG] Чанков: {len(chunks)}")
        all_segments = []
        offset = 0.0
        for i, chunk in enumerate(chunks, 1):
            print(f"[LOG] Обработка чанка {i}/{len(chunks)}")
            chunk_duration = get_audio_duration(chunk)
            segs = _transcribe_single(chunk, time_offset=offset)
            all_segments.extend(segs)
            offset += chunk_duration
        return all_segments
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)