import re
import warnings
import tempfile
import shutil
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"TensorFloat-32 \(TF32\) has been disabled.*"
)
warnings.filterwarnings(
    "ignore",
    message=r".*degrees of freedom is <= 0.*"
)

from faster_whisper import WhisperModel

from config import (
    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
    WHISPER_LANGUAGE, WHISPER_INITIAL_PROMPT,
    CHUNK_MINUTES, HUGGINGFACE_TOKEN,
    DIARIZATION_MODEL, DIARIZATION_NUM_SPEAKERS,
    DIARIZATION_MIN_SPEAKERS, DIARIZATION_MAX_SPEAKERS,
    DIARIZATION_MIN_DURATION, DIARIZATION_GAP,
)
from audio_utils import get_audio_duration, split_audio

# Порог вероятности, ниже которого слово считаем «неразборчивым»
UNCERTAIN_WORD_PROB = 0.45
# Не больше этого числа сомнительных слов отдаём в пометки
MAX_UNCERTAIN_WORDS = 25

model = WhisperModel(
    WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE
)

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
            DIARIZATION_MODEL, token=HUGGINGFACE_TOKEN,
        )
        device = torch.device("cuda" if WHISPER_DEVICE == "cuda" else "cpu")
        diarization_pipeline.to(device)
        print(f"[LOG] Диаризация: {DIARIZATION_MODEL} на {device}")
    except Exception as e:
        print(f"[WARN] Не загрузилась {DIARIZATION_MODEL}: {e}")
        try:
            diarization_pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=HUGGINGFACE_TOKEN,
            )
            device = torch.device("cuda" if WHISPER_DEVICE == "cuda" else "cpu")
            diarization_pipeline.to(device)
            print("[LOG] Фолбэк на speaker-diarization-3.1")
        except Exception as e2:
            print(f"[WARN] Диаризация не загружена: {e2}")
            diarization_pipeline = None


# ================= ДИАРИЗАЦИЯ =================

def _extract_annotation(output):
    if hasattr(output, "speaker_diarization"):
        return output.speaker_diarization
    if hasattr(output, "itertracks"):
        return output
    if hasattr(output, "annotation"):
        return output.annotation
    return output


def _prune_ghost_speakers(segments, total_duration: float,
                          min_share: float = 0.10):
    if not segments or total_duration <= 0:
        return segments

    durations = defaultdict(float)
    for s, e, spk in segments:
        durations[spk] += (e - s)

    threshold = total_duration * min_share
    main_speakers = {spk for spk, d in durations.items() if d >= threshold}
    if not main_speakers:
        main_speakers = {max(durations, key=durations.get)}

    cleaned = []
    for i, (s, e, spk) in enumerate(segments):
        if spk in main_speakers:
            cleaned.append((s, e, spk))
            continue
        left = next((segments[j][2] for j in range(i - 1, -1, -1)
                     if segments[j][2] in main_speakers), None)
        right = next((segments[j][2] for j in range(i + 1, len(segments))
                      if segments[j][2] in main_speakers), None)
        replacement = left or right or next(iter(main_speakers))
        cleaned.append((s, e, replacement))

    return cleaned


def merge_short_speaker_segments(segments, total_duration: float = 0.0,
                                 min_duration: float = None,
                                 gap: float = None):
    if not segments:
        return segments

    if min_duration is None:
        min_duration = DIARIZATION_MIN_DURATION
    if gap is None:
        gap = DIARIZATION_GAP

    segments = sorted(segments, key=lambda x: x[0])
    segments = [s for s in segments if (s[1] - s[0]) >= 0.15]
    if not segments:
        return segments

    merged = []
    for start, end, spk in segments:
        if merged:
            p_start, p_end, p_spk = merged[-1]
            if p_spk == spk and start - p_end < gap:
                merged[-1] = (p_start, end, p_spk)
                continue
        merged.append((start, end, spk))

    if len(merged) >= 3:
        cleaned = [merged[0]]
        for i in range(1, len(merged) - 1):
            start, end, spk = merged[i]
            duration = end - start
            left_spk = cleaned[-1][2]
            right_spk = merged[i + 1][2]
            if (duration < min_duration
                    and left_spk == right_spk
                    and spk != left_spk):
                p_start, _, _ = cleaned[-1]
                cleaned[-1] = (p_start, end, left_spk)
            else:
                cleaned.append(merged[i])
        cleaned.append(merged[-1])
        merged = cleaned

    if total_duration > 0:
        merged = _prune_ghost_speakers(merged, total_duration, min_share=0.10)

    return merged


def perform_diarization(audio_path: str):
    if diarization_pipeline is None:
        return None
    try:
        kwargs = {}
        if DIARIZATION_NUM_SPEAKERS > 0:
            kwargs["num_speakers"] = DIARIZATION_NUM_SPEAKERS
        else:
            kwargs["min_speakers"] = DIARIZATION_MIN_SPEAKERS
            kwargs["max_speakers"] = DIARIZATION_MAX_SPEAKERS

        total_duration = get_audio_duration(audio_path)
        output = diarization_pipeline(audio_path, **kwargs)
        annotation = _extract_annotation(output)

        if not hasattr(annotation, "itertracks"):
            print(f"[Diarization] Не удалось извлечь annotation из "
                  f"{type(output).__name__}")
            return None

        raw = [
            (t.start, t.end, spk)
            for t, _, spk in annotation.itertracks(yield_label=True)
        ]
        print(f"[LOG] Сырых интервалов: {len(raw)}")

        cleaned = merge_short_speaker_segments(
            raw, total_duration=total_duration
        )
        speakers = sorted({s[2] for s in cleaned})
        print(f"[LOG] После постобработки: {len(cleaned)} интервалов, "
              f"спикеров: {len(speakers)}")
        return cleaned
    except Exception as e:
        import traceback
        print(f"[Diarization Error] {e}")
        traceback.print_exc()
        return None


def merge_transcription_with_diarization(
    transcription_segments, diarization_segments, time_offset: float = 0.0
):
    """
    Возвращает сегменты вида:
    {'speaker':..., 'text':..., 'start':..., 'end':..., 'uncertain': [слово, ...]}
    """
    if not diarization_segments:
        parts = []
        uncertain_all = []
        for seg in transcription_segments:
            text = seg.text.strip()
            if text:
                parts.append(text)
            uncertain_all.extend(_collect_uncertain_words(seg))

        text = " ".join(parts)
        return [{
            'speaker': None,
            'text': text,
            'start': (transcription_segments[0].start + time_offset)
                     if transcription_segments else 0.0,
            'end': (transcription_segments[-1].end + time_offset)
                   if transcription_segments else 0.0,
            'uncertain': uncertain_all[:MAX_UNCERTAIN_WORDS],
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
            'uncertain': _collect_uncertain_words(seg),
        })

    grouped = []
    for item in raw:
        if grouped and grouped[-1]['speaker'] == item['speaker']:
            grouped[-1]['text'] += " " + item['text']
            grouped[-1]['end'] = item['end']
            grouped[-1]['uncertain'].extend(item.get('uncertain', []))
        else:
            grouped.append(dict(item))

    for g in grouped:
        g['uncertain'] = g.get('uncertain', [])[:MAX_UNCERTAIN_WORDS]

    return grouped


def _collect_uncertain_words(seg):
    """Возвращает список слов сегмента с низкой probability."""
    words = getattr(seg, "words", None) or []
    out = []
    for w in words:
        try:
            prob = getattr(w, "probability", None)
            if prob is None:
                continue
            if prob < UNCERTAIN_WORD_PROB:
                word = (getattr(w, "word", "") or "").strip()
                if word:
                    out.append(word)
        except Exception:
            continue
    return out


# ================= ТЕКСТ <-> СЕГМЕНТЫ =================

def segments_to_text(segments: list) -> str:
    if not segments:
        return ""
    speakers = {s['speaker'] for s in segments if s['speaker']}
    if len(speakers) <= 1:
        return " ".join(s['text'] for s in segments)
    return "\n".join(
        f"[{s['speaker'] or 'SPEAKER_UNKNOWN'}] {s['text']}" for s in segments
    )


def get_all_uncertain_words(segments: list) -> list:
    """Объединяет списки сомнительных слов со всех сегментов."""
    seen = []
    for s in segments:
        for w in (s.get('uncertain') or []):
            if w not in seen:
                seen.append(w)
    return seen[:MAX_UNCERTAIN_WORDS]


_SPEAKER_LINE_RE = re.compile(
    r'^\s*\[(?P<spk>SPEAKER_\w+)\]\s*(?P<text>.*)$',
    re.MULTILINE
)


def text_to_segments(text: str) -> list:
    if not text:
        return []
    lines = text.split('\n')
    segments = []
    current = None
    for line in lines:
        m = _SPEAKER_LINE_RE.match(line)
        if m:
            if current:
                segments.append(current)
            current = {
                'speaker': m.group('spk'),
                'text': m.group('text').strip(),
                'start': 0.0,
                'end': 0.0,
                'uncertain': [],
            }
        else:
            stripped = line.strip()
            if not stripped:
                continue
            if current is None:
                current = {
                    'speaker': None,
                    'text': stripped,
                    'start': 0.0,
                    'end': 0.0,
                    'uncertain': [],
                }
            else:
                current['text'] = (current['text'] + " " + stripped).strip()
    if current:
        segments.append(current)
    if not segments:
        return [{'speaker': None, 'text': text.strip(),
                 'start': 0.0, 'end': 0.0, 'uncertain': []}]
    return [s for s in segments if s['text']]


# ================= ТРАНСКРИБАЦИЯ =================

def _transcribe_single(path: str, time_offset: float = 0.0) -> list:
    prompt = WHISPER_INITIAL_PROMPT or None
    segments, _ = model.transcribe(
        path,
        beam_size=5,
        language=WHISPER_LANGUAGE,
        temperature=0.0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=prompt,
        compression_ratio_threshold=3.5,
        log_prob_threshold=-1.5,
        no_speech_threshold=0.6,
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