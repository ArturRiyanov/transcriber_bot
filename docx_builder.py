import re
from datetime import datetime
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from config import SPEAKER_NAMES


TITLE_BY_TYPE = {
    "interview": "Отчёт по интервью",
    "meeting": "Протокол встречи",
    "lecture": "Транскрипция лекции",
    "monologue": "Транскрипция записи",
    "dialogue": "Транскрипция диалога",
    "other": "Транскрипция записи",
    "media": "Транскрипция записи",
}


def _resolve_speaker_label(speaker_raw: str, speakers_info: dict) -> str:
    if not speaker_raw:
        return "Говорящий"

    if speakers_info and speaker_raw in speakers_info:
        info = speakers_info[speaker_raw]
        if isinstance(info, dict):
            name = (info.get("name") or "").strip()
            role = (info.get("role") or "").strip()
            position = (info.get("position") or "").strip()

            parts = []
            if name:
                parts.append(name)
            if role:
                parts.append(role)
            if position and position.lower() not in ("не указана", "не указано", ""):
                parts.append(position)

            if parts:
                if name:
                    if len(parts) > 1:
                        return f"{name} ({', '.join(parts[1:])})"
                    return name
                return ", ".join(parts)

    if speaker_raw in SPEAKER_NAMES:
        return SPEAKER_NAMES[speaker_raw]

    return speaker_raw


# Распознаём:
# 1) [SPEAKER_XX] — невербалка/ремарка в квадратных скобках
# 2) [[...]] — пометка сомнения из DeepSeek
_TOKEN_RE = re.compile(r'(\[\[[^\]]+\]\]|\[[^\[\]]+\])')


def _add_speaker_replica(doc, speaker_label: str, text: str):
    p = doc.add_paragraph()

    run_speaker = p.add_run(f"{speaker_label}: ")
    run_speaker.bold = True
    run_speaker.font.size = Pt(11)
    run_speaker.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

    for part in _TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith('[[!') and part.endswith(']]'):
            continue  # на всякий случай, если попадётся что-то вроде [[!x]]
        if part.startswith('[[') and part.endswith(']]'):
            # Пометка сомнения — серый курсив
            inner = part[2:-2]
            run = p.add_run(inner)
            run.italic = True
            run.font.color.rgb = RGBColor(0x9C, 0x27, 0xB0)
        elif part.startswith('[') and part.endswith(']'):
            run = p.add_run(part)
            run.italic = True
            run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        else:
            p.add_run(part)


def _add_markdown_section(doc, markdown_text: str):
    for raw_line in markdown_text.split('\n'):
        line = raw_line.rstrip()
        if not line.strip():
            doc.add_paragraph('')
            continue
        if line.startswith('### '):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith('## '):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith('# '):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.lstrip().startswith(('- ', '* ')):
            indent = len(line) - len(line.lstrip())
            text = line.lstrip()[2:].strip()
            p = doc.add_paragraph(text, style='List Bullet')
            if indent >= 2:
                p.paragraph_format.left_indent = Pt(20)
        elif re.match(r'^\s*\d+\.\s', line):
            text = re.sub(r'^\s*\d+\.\s', '', line)
            doc.add_paragraph(text, style='List Number')
        else:
            p = doc.add_paragraph()
            for i, part in enumerate(line.split('**')):
                run = p.add_run(part)
                if i % 2 == 1:
                    run.bold = True


def create_docx(segments: list, output_path: str,
                meeting_url: str = "", analysis_text: str = "",
                candidate_name: str = "", position: str = "",
                speakers_info: dict = None,
                content_type: str = "interview"):
    speakers_info = speakers_info or {}
    is_interview = (content_type == "interview")

    if is_interview:
        if (not candidate_name or candidate_name in ("Кандидат", "")) and speakers_info:
            cand_spk = speakers_info.get("candidate_speaker")
            if cand_spk and cand_spk in speakers_info:
                info = speakers_info[cand_spk]
                if isinstance(info, dict) and info.get("name"):
                    candidate_name = info["name"]

        if (not position or position in ("", "Не указана")) and speakers_info:
            cand_spk = speakers_info.get("candidate_speaker")
            if cand_spk and cand_spk in speakers_info:
                info = speakers_info[cand_spk]
                if isinstance(info, dict) and info.get("position"):
                    position = info["position"]

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(11)

    title_text = TITLE_BY_TYPE.get(content_type, "Транскрипция записи")
    title = doc.add_heading(title_text, 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if is_interview and candidate_name and candidate_name != "Кандидат":
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f'Кандидат: {candidate_name}')
        run.bold = True
        run.font.size = Pt(14)

    if is_interview and position:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f'Позиция: {position}').font.size = Pt(12)

    if meeting_url:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f'Ссылка на встречу: {meeting_url}').font.size = Pt(9)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f'Дата: {datetime.now().strftime("%d.%m.%Y %H:%M")}').font.size = Pt(10)

    if is_interview and speakers_info:
        participants = []
        for spk, info in speakers_info.items():
            if spk in ("candidate_speaker", "interviewer_speaker"):
                continue
            if not isinstance(info, dict):
                continue
            label = _resolve_speaker_label(spk, speakers_info)
            participants.append(label)
        if participants:
            doc.add_paragraph('')
            doc.add_heading('Участники', level=2)
            for label in participants:
                doc.add_paragraph(label, style='List Bullet')

    if analysis_text:
        doc.add_page_break()
        doc.add_heading('Аналитический отчёт по кандидату', level=1)
        _add_markdown_section(doc, analysis_text)

    doc.add_page_break()
    heading_text = 'Транскрипция диалога' if is_interview else 'Транскрипция'
    doc.add_heading(heading_text, level=1)

    # Пояснение про пометки
    p = doc.add_paragraph()
    run = p.add_run(
        "Пометки: серым курсивом выделены места, где возможна ошибка "
        "распознавания или оговорка."
    )
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    prev_speaker = None
    for seg in segments:
        speaker_raw = seg.get('speaker')
        speaker_label = _resolve_speaker_label(speaker_raw, speakers_info)
        if prev_speaker is not None and speaker_raw != prev_speaker:
            doc.add_paragraph('')
        _add_speaker_replica(doc, speaker_label, seg['text'])
        prev_speaker = speaker_raw

    doc.save(output_path)