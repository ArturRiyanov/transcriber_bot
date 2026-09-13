import json
import re
import requests
from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_URL, IMPROVE_TEXT,
    ANALYSIS_ENABLED, CANDIDATE_NAME, POSITION_NAME
)


def _call_deepseek(messages: list, model_name: str = "deepseek-chat",
                   max_tokens: int = 8000, timeout: int = 300) -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens
    }
    response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    msg = data["choices"][0]["message"]
    return (msg.get("content") or "").strip()


def classify_content(transcript_text: str) -> dict:
    """
    Определяет тип контента транскрипта.
    Возвращает {"type": ..., "confidence": float, "reason": str}.
    Типы: interview | meeting | lecture | monologue | dialogue | other
    """
    if not transcript_text or len(transcript_text.strip()) < 50:
        return {"type": "other", "confidence": 0.0,
                "reason": "Слишком короткий текст"}

    prompt = f"""Проанализируй транскрипт и определи его тип. Отвечай ТОЛЬКО валидным JSON.

ДОПУСТИМЫЕ ТИПЫ:
- "interview" — интервью или собеседование. Один или несколько интервьюеров задают вопросы кандидату или респонденту, оценивают его опыт, навыки, мотивацию.
- "meeting" — рабочая встреча, планёрка, совещание, обсуждение рабочих вопросов в команде.
- "lecture" — лекция, доклад, выступление, презентация для аудитории.
- "monologue" — монолог, начитка, озвучка, один говорящий без диалога.
- "dialogue" — бытовой или иной диалог, не подходящий под предыдущие категории.
- "other" — ничего из перечисленного.

ПРИЗНАКИ ИНТЕРВЬЮ:
- один участник задаёт вопросы о биографии, опыте, навыках другого
- второй развёрнуто отвечает о себе
- типичные фразы: "расскажите о себе", "ваш опыт", "почему ушли", "как вы видите", "какие у вас планы"

ВАЖНО:
- Если запись НЕ похожа на интервью или собеседование — не выбирай "interview" только потому, что говорящих несколько.
- Обычная рабочая встреча — это "meeting", а не "interview".
- Одиночная начитка или озвучка — это "monologue".

ФОРМАТ ОТВЕТА:
{{"type": "interview", "confidence": 0.85, "reason": "краткое обоснование"}}

ТРАНСКРИПТ:
{transcript_text[:6000]}"""

    messages = [
        {"role": "system", "content": "Ты — классификатор транскриптов. Отвечай строго валидным JSON."},
        {"role": "user", "content": prompt}
    ]

    raw = ""
    try:
        raw = _call_deepseek(messages, "deepseek-chat",
                             max_tokens=500, timeout=120)
    except Exception as e:
        print(f"[DeepSeek] classify error: {e}")

    if not raw:
        return {"type": "other", "confidence": 0.0,
                "reason": "Ошибка классификации"}

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None

    if not isinstance(data, dict):
        return {"type": "other", "confidence": 0.0,
                "reason": "Не удалось распарсить ответ"}

    t = (data.get("type") or "other").lower().strip()
    if t not in ("interview", "meeting", "lecture",
                 "monologue", "dialogue", "other"):
        t = "other"

    try:
        conf = float(data.get("confidence") or 0.0)
    except Exception:
        conf = 0.0

    return {
        "type": t,
        "confidence": conf,
        "reason": (data.get("reason") or "").strip()
    }


def improve_text(text: str) -> str:
    if not IMPROVE_TEXT:
        print("[DeepSeek] Улучшение отключено")
        return text
    if not text or len(text.strip()) < 5:
        return text

    prompt = (
        "Ты — корректор транскрипций. Исправь ТОЛЬКО орфографию и пунктуацию. "
        "НЕ меняй порядок слов. Сохрани метки [SPEAKER_XX] как есть.\n\n"
        f"Транскрипция:\n{text}"
    )
    messages = [
        {"role": "system", "content": "Ты — корректор транскрипций."},
        {"role": "user", "content": prompt}
    ]
    try:
        r = _call_deepseek(messages, "deepseek-reasoner")
        if r:
            return r
    except Exception as e:
        print(f"[DeepSeek] Reasoner error: {e}")
    try:
        r = _call_deepseek(messages, "deepseek-chat")
        if r:
            return r
    except Exception as e:
        print(f"[DeepSeek] Chat error: {e}")
    return text


def extract_speakers_and_roles(transcript_text: str) -> dict:
    """Извлекает имена, роли и должности спикеров из текста интервью."""
    if not transcript_text or len(transcript_text.strip()) < 50:
        return {}

    prompt = f"""Проанализируй транскрипцию интервью и извлеки информацию о каждом говорящем.

ТРАНСКРИПЦИЯ:
{transcript_text}

ЗАДАЧА:
1. Найди все упоминания участников диалога.
2. Для каждого участника определи:
   - Имя — если он или она представился (Меня зовут..., Я — ..., обращение по имени).
   - Должность или профессию — если упоминается.
   - Роль в интервью: "Кандидат" или "Интервьюер".
3. Присвой каждому участнику метку SPEAKER_00, SPEAKER_01, ...
4. Если имя не названо — оставь пустую строку.

ВАЖНО: отвечай ТОЛЬКО валидным JSON без пояснений и markdown-обёрток.

ФОРМАТ ОТВЕТА:
{{
  "SPEAKER_00": {{"name": "...", "role": "...", "position": "..."}},
  "SPEAKER_01": {{"name": "...", "role": "...", "position": "..."}},
  "candidate_speaker": "SPEAKER_XX",
  "interviewer_speaker": "SPEAKER_YY"
}}
"""

    messages = [
        {"role": "system", "content": "Ты — аналитик интервью. Отвечай строго валидным JSON."},
        {"role": "user", "content": prompt}
    ]

    raw = ""
    try:
        print("[DeepSeek] Извлекаю имена и роли спикеров...")
        raw = _call_deepseek(messages, "deepseek-reasoner", max_tokens=2000, timeout=300)
    except Exception as e:
        print(f"[DeepSeek] extract reasoner error: {e}")

    if not raw:
        try:
            raw = _call_deepseek(messages, "deepseek-chat", max_tokens=2000, timeout=300)
        except Exception as e:
            print(f"[DeepSeek] extract chat error: {e}")

    if not raw:
        return {}

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
        print(f"[DeepSeek] Спикеры: {data}")
        return data
    except json.JSONDecodeError as e:
        print(f"[DeepSeek] JSON parse error: {e}\nОтвет: {raw[:500]}")
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        return {}


def resplit_by_speakers(text: str, speakers_info: dict) -> str:
    """
    Разбивает монолитный текст на реплики с метками [SPEAKER_XX] по СМЫСЛУ.
    Применяется, когда диаризация нашла 1 голос, но в тексте диалог за двоих.
    """
    if not text or not speakers_info:
        return text

    known = []
    for spk, info in speakers_info.items():
        if spk in ("candidate_speaker", "interviewer_speaker"):
            continue
        if isinstance(info, dict):
            name = info.get("name") or ""
            role = info.get("role") or ""
            position = info.get("position") or ""
            label = spk
            if name:
                label += f" ({name}"
                if role:
                    label += f", {role}"
                if position:
                    label += f", {position}"
                label += ")"
            known.append(label)

    if not known:
        return text

    speakers_list = "\n".join(f"  - {s}" for s in known)

    prompt = f"""Ниже дан текст интервью БЕЗ разметки спикеров. Разбей его на реплики и присвой каждую реплику одному из известных спикеров.

ИЗВЕСТНЫЕ СПИКЕРЫ:
{speakers_list}

ПРАВИЛА:
1. Используй ТОЛЬКО метки SPEAKER_XX из списка выше.
2. Каждая реплика — с новой строки в формате: [SPEAKER_XX] текст реплики
3. Определяй спикера по СМЫСЛУ:
   - Кто задаёт вопросы — Интервьюер.
   - Кто отвечает, представляется, рассказывает о себе — Кандидат.
   - Фразы Меня зовут X, Я — X относятся к тому спикеру, чьё имя упомянуто.
4. НЕ меняй текст реплик. НЕ добавляй и НЕ удаляй слова.
5. Если один спикер говорит несколько предложений подряд — это одна реплика.
6. Отвечай ТОЛЬКО размеченным текстом, без пояснений.

ТЕКСТ:
{text}
"""

    messages = [
        {"role": "system", "content": "Ты — эксперт по разметке диалогов. Разбиваешь текст на реплики спикеров."},
        {"role": "user", "content": prompt}
    ]

    try:
        print("[DeepSeek] Разбиваю текст по спикерам (fallback)...")
        result = _call_deepseek(messages, "deepseek-reasoner", max_tokens=8000, timeout=300)
        if result:
            return result
    except Exception as e:
        print(f"[DeepSeek] resplit reasoner error: {e}")

    try:
        result = _call_deepseek(messages, "deepseek-chat", max_tokens=8000, timeout=300)
        if result:
            return result
    except Exception as e:
        print(f"[DeepSeek] resplit chat error: {e}")

    return text


def analyze_interview(transcript_text: str, speakers_info: dict = None) -> str:
    if not ANALYSIS_ENABLED:
        return ""
    if not transcript_text or len(transcript_text.strip()) < 100:
        return ""

    speakers_context = ""
    if speakers_info:
        speakers_context = "\n\nИНФОРМАЦИЯ О СПИКЕРАХ:\n"
        for spk, info in speakers_info.items():
            if spk in ("candidate_speaker", "interviewer_speaker"):
                continue
            if isinstance(info, dict):
                speakers_context += (
                    f"- {spk}: имя={info.get('name') or 'не названо'}, "
                    f"роль={info.get('role') or 'не определена'}, "
                    f"должность={info.get('position') or 'не указана'}\n"
                )
        cand = speakers_info.get("candidate_speaker")
        if cand:
            speakers_context += f"\nКандидат — это {cand}.\n"

    prompt = f"""Ты — опытный HR-эксперт и технический интервьюер. Проведи объективный анализ интервью и составь подробный отчёт по кандидату.

ИНФОРМАЦИЯ:
- Кандидат: {CANDIDATE_NAME}
- Позиция: {POSITION_NAME}
{speakers_context}

ЗАДАЧА:
1. Определи, кто из говорящих является кандидатом, а кто — интервьюером.
2. Проведи детальный анализ профессиональных и личностных качеств кандидата.
3. Используй настоящие имена спикеров, если они определены.

СТРУКТУРА ОТЧЁТА:

ОБЩАЯ ИНФОРМАЦИЯ
- Длительность интервью
- Количество участников
- Кто определён как кандидат (имя + должность)

КРАТКОЕ РЕЗЮМЕ

ТЕХНИЧЕСКИЕ И ПРОФЕССИОНАЛЬНЫЕ ЗНАНИЯ (оценка 1-10)

КОММУНИКАТИВНЫЕ НАВЫКИ (оценка 1-10)

ЛИЧНОСТНЫЕ КАЧЕСТВА (оценка 1-10)

ОБЪЕКТИВНОЕ МНЕНИЕ
- Итоговая оценка (1-10)
- Рекомендация: "Нанять" / "Пригласить на следующий этап" / "Отказать"

КРАСНЫЕ ФЛАГИ
СИЛЬНЫЕ СТОРОНЫ
РЕКОМЕНДАЦИИ

ТРЕБОВАНИЯ:
- Будь объективен
- Опирайся ТОЛЬКО на факты
- Приводи цитаты кандидата
- Если информации недостаточно — укажи это

ТРАНСКРИПЦИЯ:
{transcript_text}"""

    messages = [
        {"role": "system", "content": "Ты — опытный HR-эксперт."},
        {"role": "user", "content": prompt}
    ]
    try:
        r = _call_deepseek(messages, "deepseek-reasoner", max_tokens=8000, timeout=600)
        if r:
            return r
    except Exception as e:
        print(f"[DeepSeek] Анализ reasoner error: {e}")
    try:
        r = _call_deepseek(messages, "deepseek-chat", max_tokens=8000, timeout=600)
        if r:
            return r
    except Exception as e:
        print(f"[DeepSeek] Анализ chat error: {e}")
    return "Не удалось сформировать аналитический отчёт."