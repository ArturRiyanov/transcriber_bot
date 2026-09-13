"""
Общий пайплайн обработки транскрипта.
Определяет тип контента, применяет интервью-специфичную логику
только когда это действительно интервью / собеседование.
"""
import os
from transcription import (
    transcribe_audio, segments_to_text, text_to_segments,
    diarization_pipeline
)
from deepseek_client import (
    improve_text, analyze_interview,
    extract_speakers_and_roles, resplit_by_speakers,
    classify_content
)
from docx_builder import create_docx
from audio_utils import get_audio_duration


def process_audio(
    audio_path: str,
    *,
    docx_path: str,
    meeting_url: str = "",
    default_candidate: str = "Медиа",
    default_position: str = "",
    run_analysis: bool = False,
    analysis_enabled: bool = False,
    log_prefix: str = "[Pipeline]",
) -> dict:
    duration = get_audio_duration(audio_path)

    segments = transcribe_audio(audio_path)
    if not segments:
        raise RuntimeError("Речь не обнаружена")

    transcript_text = segments_to_text(segments)
    improved = improve_text(transcript_text)

    # Классификация типа контента
    classification = {"type": "other", "confidence": 0.0, "reason": ""}
    classification_failed = False
    try:
        classification = classify_content(transcript_text)
        if classification.get("reason") == "Ошибка классификации":
            classification_failed = True
    except Exception as e:
        classification_failed = True
        print(f"{log_prefix} classify error: {e}")

    content_type = classification.get("type", "other")
    is_interview = (content_type == "interview")
    print(f"{log_prefix} тип контента: {content_type} "
          f"(confidence={classification.get('confidence')}, "
          f"reason={classification.get('reason')})")

    # Интервью-специфичная логика
    speakers_info = {}
    if is_interview:
        try:
            speakers_info = extract_speakers_and_roles(transcript_text)
        except Exception as e:
            print(f"{log_prefix} speakers error: {e}")

        real_speakers = {s.get('speaker') for s in segments if s.get('speaker')}
        ds_speakers = [
            k for k in speakers_info.keys()
            if k not in ("candidate_speaker", "interviewer_speaker")
        ]
        if len(real_speakers) <= 1 and len(ds_speakers) >= 2:
            print(f"{log_prefix} fallback resplit: real={len(real_speakers)} "
                  f"ds={len(ds_speakers)}")
            try:
                resplit = resplit_by_speakers(transcript_text, speakers_info)
                new_segments = text_to_segments(resplit)
                if new_segments and any(s.get('speaker') for s in new_segments):
                    segments = new_segments
            except Exception as e:
                print(f"{log_prefix} fallback error: {e}")

    effective_candidate = default_candidate
    effective_position = default_position
    if is_interview and speakers_info:
        cand = speakers_info.get("candidate_speaker")
        if cand and cand in speakers_info:
            info = speakers_info[cand]
            if isinstance(info, dict):
                if info.get("name"):
                    effective_candidate = info["name"]
                if info.get("position"):
                    effective_position = info["position"]

    num_speakers = len(
        {s.get('speaker') for s in segments if s.get('speaker')}
    )

    # Анализ — только для интервью
    analysis_text = ""
    if run_analysis and analysis_enabled and is_interview:
        try:
            analysis_text = analyze_interview(improved, speakers_info=speakers_info)
        except Exception as e:
            print(f"{log_prefix} analysis error: {e}")

    # Имена для DOCX передаём только для интервью
    docx_candidate = effective_candidate if is_interview else ""
    docx_position = effective_position if is_interview else ""

    create_docx(
        segments=segments,
        output_path=docx_path,
        meeting_url=meeting_url,
        analysis_text=analysis_text,
        candidate_name=docx_candidate,
        position=docx_position,
        speakers_info=speakers_info if is_interview else {},
        content_type=content_type,
    )

    # TXT рядом с DOCX
    txt_path = os.path.splitext(docx_path)[0] + ".txt"
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(improved)
    except Exception as e:
        print(f"{log_prefix} txt save error: {e}")
        txt_path = ""

    return {
        "segments": segments,
        "transcript_text": transcript_text,
        "improved_text": improved,
        "speakers_info": speakers_info,
        "duration": duration,
        "num_speakers": num_speakers,
        "content_type": content_type,
        "classification": classification,
        "classification_failed": classification_failed,
        "is_interview": is_interview,
        "kind": content_type,
        "docx_path": docx_path,
        "txt_path": txt_path,
        "analysis_text": analysis_text,
        "effective_candidate": effective_candidate,
        "effective_position": effective_position,
    }