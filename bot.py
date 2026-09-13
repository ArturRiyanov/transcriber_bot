import os
import time
import asyncio
import html as html_lib
import re as re_lib
import tempfile
import shutil
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from config import (
    TELEGRAM_TOKEN, cleanup_temp_files,
    SIMPLE_MAX_DURATION_SEC, SIMPLE_MAX_CHARS, SKIP_CLASSIFY_SEC,
)
from audio_utils import extract_audio_from_video, get_audio_duration, estimate_time
from transcription import (
    transcribe_audio, segments_to_text, text_to_segments,
    diarization_pipeline, get_all_uncertain_words,
)
from deepseek_client import (
    polish_text, analyze_interview,
    extract_speakers_and_roles, resplit_by_speakers,
    classify_content,
)
from conference import join_conference, stop_conference, active_sessions, check_auto_stop
from docx_builder import create_docx, _resolve_speaker_label
from storage import (
    save_entry, list_entries, get_entry,
    get_file_path, delete_entry, get_stats, _safe_filename
)

cleanup_temp_files()

pending_results: dict = {}


TYPE_LABELS = {
    "interview": "Интервью / собеседование",
    "meeting": "Рабочая встреча",
    "lecture": "Лекция / доклад",
    "monologue": "Монолог",
    "dialogue": "Диалог",
    "other": "Прочее",
    "media": "Медиа",
}

SINGLE_SPEAKER_TYPES = {"monologue", "lecture"}


# ---------- Анимированный статус ----------
class AnimatedStatus:
    def __init__(self, message, initial_text: str = "Обработка"):
        self.message = message
        self.base_text = initial_text
        self._running = False
        self._task = None
        self._lock = asyncio.Lock()
        self._dots = 0

    async def _edit(self):
        text = self.base_text + "." * self._dots
        async with self._lock:
            try:
                await self.message.edit_text(text)
            except Exception:
                pass

    async def _loop(self):
        self._running = True
        try:
            while self._running:
                self._dots = (self._dots % 3) + 1
                await self._edit()
                await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[AnimatedStatus] Ошибка: {e}")

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def set_base(self, text: str):
        self.base_text = text
        self._dots = 1
        await self._edit()

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def delete(self):
        await self.stop()
        try:
            await self.message.delete()
        except Exception:
            pass


# ---------- Утилиты ----------
def force_single_speaker(segments):
    if not segments:
        return segments

    parts = []
    start = None
    end = None
    for s in segments:
        text = (s.get('text') or "").strip()
        if not text:
            continue
        parts.append(text)
        if start is None:
            start = s.get('start', 0.0)
        end = s.get('end', 0.0)

    merged = " ".join(parts).strip()
    if not merged:
        return segments

    return [{
        'speaker': None,
        'text': merged,
        'start': start if start is not None else 0.0,
        'end': end if end is not None else 0.0,
        'uncertain': [],
    }]


def pick_name_from_speakers(speakers_info: dict):
    if not speakers_info:
        return None, None

    cand = speakers_info.get("candidate_speaker")
    if cand and cand in speakers_info:
        info = speakers_info[cand]
        if isinstance(info, dict) and info.get("name"):
            return info["name"], info.get("position") or ""

    for spk, info in speakers_info.items():
        if spk in ("candidate_speaker", "interviewer_speaker"):
            continue
        if isinstance(info, dict) and info.get("name"):
            return info["name"], info.get("position") or ""

    return None, None


_UNWRAP_RE = re_lib.compile(r'\[\[(.*?)\]\]')


def unwrap_uncertain(text: str) -> str:
    """Убирает пометки [[...]] для чистого текста в чате."""
    return _UNWRAP_RE.sub(r'\1', text)


def render_for_chat(segments, speakers_info, improved_text: str) -> str:
    speakers_info = speakers_info or {}
    speakers = {s.get('speaker') for s in segments if s.get('speaker')}

    if len(speakers) <= 1:
        joined = " ".join((s.get('text') or "").strip()
                          for s in segments).strip()
        return unwrap_uncertain(joined or improved_text)

    parts = []
    for seg in segments:
        text = (seg.get('text') or "").strip()
        if not text:
            continue
        label = _resolve_speaker_label(seg.get('speaker'), speakers_info)
        parts.append(f"{label}: {unwrap_uncertain(text)}")

    return "\n\n".join(parts) if parts else unwrap_uncertain(improved_text)


# ---------- Меню ----------
def get_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Мои отчёты", callback_data="menu_reports")],
        [InlineKeyboardButton("Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("Как пользоваться", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _auto_stop_watcher(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(active_sessions.keys()):
        try:
            stopped = await check_auto_stop(chat_id)
            if stopped:
                print(f"[AutoStop] Сессия {chat_id} остановлена")
        except Exception as e:
            print(f"[AutoStop] Ошибка: {e}")


async def send_long_message(update: Update, text: str):
    limit = 4000
    if len(text) <= limit:
        await update.message.reply_text(text)
        return
    parts = [text[i:i+limit] for i in range(0, len(text), limit)]
    for idx, part in enumerate(parts, 1):
        await update.message.reply_text(f"Часть {idx}/{len(parts)}:\n\n{part}")


async def send_copyable_text(update: Update, text: str):
    limit = 3500
    escaped = html_lib.escape(text)
    if len(escaped) <= limit:
        await update.message.reply_text(
            f"<code>{escaped}</code>",
            parse_mode="HTML"
        )
        return

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()

    for chunk in chunks:
        escaped = html_lib.escape(chunk)
        await update.message.reply_text(
            f"<code>{escaped}</code>",
            parse_mode="HTML"
        )


# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Транскрибатор аудио, видео и конференций.\n\n"
        "Возможности:\n"
        "1. Транскрибация файла. Отправьте голосовое, аудио или видео — "
        "пришлю расшифровку. Бот определяет тип записи: интервью, "
        "рабочая встреча, лекция, монолог или диалог. Для интервью и "
        "собеседований дополнительно формируется аналитический отчёт "
        "по кандидату.\n\n"
        "2. Конференция. Отправьте ссылку telemost.yandex.ru. Бот "
        "подключится, запишет встречу с отключёнными камерой и микрофоном "
        "и пришлёт отчёт по завершении.\n\n"
        "Меню:"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=get_main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Как пользоваться.\n\n"
        "1. Транскрибация файла.\n"
        "Отправьте аудио, видео или голосовое сообщение. Бот распознает "
        "речь, определит тип контента и сформирует отчёт в формате DOCX. "
        "Для интервью и собеседований дополнительно выполняется "
        "аналитическая оценка кандидата.\n\n"
        "Исправляются окончания, склонения, согласования. Места, где "
        "распознавание было неуверенным, помечаются — в DOCX они "
        "выделены серым курсивом.\n\n"
        "Если говорящие представились, в выводе используются их имена.\n\n"
        "Короткие записи выводятся в чат без создания файлов.\n\n"
        "2. Конференция.\n"
        "Отправьте ссылку telemost.yandex.ru. Бот подключится к встрече, "
        "запишет её и по завершении пришлёт расшифровку и отчёт.\n\n"
        "3. Картотека.\n"
        "Все расшифровки, отчёты и аудиозаписи сохраняются в личной "
        "картотеке пользователя.\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/menu — главное меню\n"
        "/stop — остановить запись конференции\n"
        "/help — эта справка"
    )
    await update.message.reply_text(text)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sessions:
        await update.message.reply_text("Нет активной сессии конференции.")
        return
    await stop_conference(chat_id, update)


# ---------- Обработка медиа ----------
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attachment = update.message.voice or update.message.audio or update.message.video
    if not attachment:
        await update.message.reply_text("Не удалось найти медиафайл.")
        return

    user_id = update.effective_user.id
    file = await attachment.get_file()
    file_ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'bin'

    tmp_dir = tempfile.mkdtemp(prefix="transcriber_")
    raw_path = os.path.join(tmp_dir, f"media.{file_ext}")
    audio_path = os.path.join(tmp_dir, "audio.wav")

    status_msg = await update.message.reply_text("Обработка.")
    status = AnimatedStatus(status_msg, "Обработка")
    status.start()

    try:
        await file.download_to_drive(raw_path)

        if update.message.video or file_ext.lower() not in ("wav",):
            ok = await asyncio.to_thread(
                extract_audio_from_video, raw_path, audio_path
            )
            if not ok:
                audio_path = raw_path
        else:
            audio_path = raw_path

        duration = await asyncio.to_thread(get_audio_duration, audio_path)

        await status.set_base("Распознаю речь")
        segments = await asyncio.to_thread(transcribe_audio, audio_path)
        if not segments:
            await status.set_base("Речь не обнаружена")
            await status.stop()
            return

        transcript_text = segments_to_text(segments)

        # Собираем сомнительные слова из Whisper
        uncertain_words = get_all_uncertain_words(segments)
        if uncertain_words:
            print(f"[LOG] Неуверенных слов: {len(uncertain_words)} "
                  f"({uncertain_words[:10]})")

        # Полируем текст: морфология + пометки
        await status.set_base("Правлю текст")
        improved = await asyncio.to_thread(
            polish_text, transcript_text, uncertain_words
        )

        # ---------- Классификация ----------
        if duration <= SKIP_CLASSIFY_SEC:
            classification = {"type": "other", "confidence": 1.0,
                              "reason": "Короткая запись"}
            content_type = "other"
            is_interview = False
        else:
            await status.set_base("Определяю тип записи")
            classification = {"type": "other", "confidence": 0.0, "reason": ""}
            try:
                classification = await asyncio.to_thread(
                    classify_content, transcript_text
                )
            except Exception as e:
                print(f"[Classify] Ошибка: {e}")

            content_type = classification.get("type", "other")
            is_interview = (content_type == "interview")

            print(f"[LOG] Тип контента: {content_type} "
                  f"(confidence={classification.get('confidence')}, "
                  f"reason={classification.get('reason')})")

        # Принудительное объединение для монологов/лекций
        if content_type in SINGLE_SPEAKER_TYPES:
            before = len(segments)
            segments = force_single_speaker(segments)
            print(f"[LOG] Тип {content_type}: объединено "
                  f"{before} сегментов в один говорящий")

        # Имена говорящих
        speakers_info = {}
        if duration > SKIP_CLASSIFY_SEC and len(transcript_text.strip()) >= 50:
            await status.set_base("Определяю говорящих")
            try:
                speakers_info = await asyncio.to_thread(
                    extract_speakers_and_roles, transcript_text
                )
            except Exception as e:
                print(f"[Speakers] Ошибка: {e}")

        # Fallback для интервью: диаризация 1, DeepSeek 2+
        if is_interview:
            real = {s.get('speaker') for s in segments if s.get('speaker')}
            ds = [k for k in speakers_info.keys()
                  if k not in ("candidate_speaker", "interviewer_speaker")]
            if len(real) <= 1 and len(ds) >= 2:
                try:
                    resplit_text = await asyncio.to_thread(
                        resplit_by_speakers, transcript_text, speakers_info
                    )
                    new_segments = text_to_segments(resplit_text)
                    if new_segments and any(s.get('speaker') for s in new_segments):
                        segments = new_segments
                except Exception as e:
                    print(f"[Fallback] Ошибка: {e}")

        # Имя/должность для файла
        effective_candidate = "Медиа"
        effective_position = ""

        picked_name, picked_pos = pick_name_from_speakers(speakers_info)
        if picked_name:
            effective_candidate = picked_name
            if picked_pos:
                effective_position = picked_pos

        num_speakers = len(
            {s.get('speaker') for s in segments if s.get('speaker')}
        )

        chat_text = render_for_chat(segments, speakers_info, improved)

        # ---------- Простой режим ----------
        is_simple = (
            not is_interview
            and (duration <= SIMPLE_MAX_DURATION_SEC
                 or len(improved) <= SIMPLE_MAX_CHARS)
        )

        if is_simple:
            pending_results[user_id] = {
                "_ts": time.time(),
                "segments": segments,
                "transcript_text": transcript_text,
                "improved_text": improved,
                "chat_text": chat_text,
                "speakers_info": speakers_info,
                "duration": duration,
                "num_speakers": num_speakers,
                "content_type": content_type,
                "classification": classification,
                "is_interview": False,
                "audio_path": audio_path,
                "tmp_dir": tmp_dir,
                "effective_candidate": effective_candidate,
                "effective_position": effective_position,
                "has_analysis": False,
            }

            await status.delete()
            await send_copyable_text(update, chat_text)

            await update.message.reply_text(
                "Нажмите на текст выше, чтобы скопировать его в буфер обмена.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Сохранить в картотеку",
                        callback_data="save_simple"
                    )]
                ])
            )
            return

        # ---------- Обычный режим ----------
        analysis = ""
        if is_interview:
            await status.set_base("Формирую аналитический отчёт")
            analysis = await asyncio.to_thread(
                analyze_interview, improved, speakers_info
            )
        else:
            await status.set_base("Формирую отчёт")

        docx_path = os.path.join(tmp_dir, "report.docx")
        await asyncio.to_thread(
            create_docx,
            segments=segments,
            output_path=docx_path,
            meeting_url="",
            analysis_text=analysis,
            candidate_name=effective_candidate,
            position=effective_position,
            speakers_info=speakers_info,
            content_type=content_type,
        )

        entry = await asyncio.to_thread(
            save_entry,
            user_id=user_id,
            source_audio=audio_path,
            report_path=docx_path,
            candidate_name=effective_candidate,
            position=effective_position,
            duration=duration,
            num_speakers=num_speakers,
            kind=content_type,
        )

        pending_results[user_id] = {
            "_ts": time.time(),
            "segments": segments,
            "transcript_text": transcript_text,
            "improved_text": improved,
            "chat_text": chat_text,
            "speakers_info": speakers_info,
            "duration": duration,
            "num_speakers": num_speakers,
            "content_type": content_type,
            "classification": classification,
            "is_interview": is_interview,
            "entry_id": entry["id"],
            "audio_path": audio_path,
            "tmp_dir": tmp_dir,
            "docx_path": docx_path,
            "effective_candidate": effective_candidate,
            "effective_position": effective_position,
            "has_analysis": bool(analysis),
            "analysis_text": analysis,
        }

        safe_cand = _safe_filename(effective_candidate or "Запись")
        safe_pos = _safe_filename(effective_position) if effective_position else ""
        date_part = datetime.now().strftime("%Y-%m-%d_%H-%M")
        parts = [p for p in (safe_cand, safe_pos, date_part) if p]
        base_name = "_".join(parts)

        await status.delete()

        with open(docx_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"{base_name}.docx",
                caption=f"Отчёт. Тип записи: "
                        f"{TYPE_LABELS.get(content_type, content_type)}"
            )

        await send_copyable_text(update, chat_text)

        await update.message.reply_text(
            "Нажмите на текст выше, чтобы скопировать его.\n\n"
            "Отчёт сохранён в картотеке. Раздел «Мои отчёты» в /menu."
        )

    except Exception as e:
        print(f"[Error] {e}")
        await status.set_base(f"Ошибка обработки: {e}")
        await status.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("Это не ссылка на Яндекс.Телемост.")
        return

    if chat_id in active_sessions:
        await update.message.reply_text(
            "Уже есть активная сессия. Отправьте /stop для завершения текущей."
        )
        return

    await update.message.reply_text("Подключаюсь к конференции...")
    await join_conference(chat_id, url, update)


# ---------- Callback-кнопки ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "save_simple":
        pending = pending_results.get(user_id)
        if not pending:
            await query.answer("Результат устарел.")
            return

        await query.answer("Сохраняю...")
        try:
            docx_path = os.path.join(pending["tmp_dir"], "report.docx")
            await asyncio.to_thread(
                create_docx,
                segments=pending["segments"],
                output_path=docx_path,
                meeting_url="",
                analysis_text="",
                candidate_name=pending.get("effective_candidate", "Медиа"),
                position=pending.get("effective_position", ""),
                speakers_info=pending.get("speakers_info", {}),
                content_type=pending.get("content_type", "other"),
            )

            entry = await asyncio.to_thread(
                save_entry,
                user_id=user_id,
                source_audio=pending["audio_path"],
                report_path=docx_path,
                candidate_name=pending.get("effective_candidate", "Медиа"),
                position=pending.get("effective_position", ""),
                duration=pending["duration"],
                num_speakers=pending["num_speakers"],
                kind=pending.get("content_type", "other"),
            )

            pending["entry_id"] = entry["id"]
            pending["docx_path"] = docx_path
            pending["has_analysis"] = False

            await query.message.edit_text(
                "Сохранено в картотеке. Раздел «Мои отчёты» в /menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Удалить из картотеки",
                        callback_data="res_delete_simple"
                    )]
                ])
            )
        except Exception as e:
            print(f"[SaveSimple] Ошибка: {e}")
            await query.message.reply_text(f"Ошибка сохранения: {e}")
        return

    if data == "res_delete_simple":
        pending = pending_results.get(user_id)
        if not pending or not pending.get("entry_id"):
            await query.answer("Нечего удалять.")
            return
        if delete_entry(user_id, pending["entry_id"]):
            await query.answer("Удалено")
            await query.message.edit_text(
                "Удалено из картотеки.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Сохранить в картотеку",
                        callback_data="save_simple"
                    )]
                ])
            )
            pending.pop("entry_id", None)
            pending.pop("docx_path", None)
        else:
            await query.answer("Не найдено")
        return

    await query.answer()

    if data == "menu_reports":
        await show_reports(query, user_id)

    elif data == "menu_stats":
        stats = get_stats(user_id)
        mins = stats["total_duration_sec"] // 60
        secs = stats["total_duration_sec"] % 60
        text = (
            "Статистика.\n\n"
            f"Всего записей: {stats['total']}\n"
            f"Общая длительность: {mins} мин {secs} сек"
        )
        kb = [[InlineKeyboardButton("Назад", callback_data="menu_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_help":
        text = (
            "Краткая справка.\n\n"
            "Отправьте аудио, видео или голосовое сообщение — получите "
            "расшифровку и отчёт.\n\n"
            "Бот исправляет окончания и склонения. Неуверенные места "
            "помечаются — в DOCX они выделены серым курсивом.\n\n"
            "Если говорящие представились, в выводе используются их имена.\n\n"
            "Короткие записи выводятся в чат без создания файлов.\n\n"
            "Отправьте ссылку telemost.yandex.ru — бот запишет конференцию "
            "и пришлёт отчёт по завершении.\n\n"
            "Команда /stop завершает активную запись конференции."
        )
        kb = [[InlineKeyboardButton("Назад", callback_data="menu_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_back":
        await query.message.edit_text("Главное меню:", reply_markup=get_main_menu())

    elif data.startswith("rep_"):
        entry_id = data[4:]
        await show_entry_detail(query, user_id, entry_id)

    elif data.startswith("dl_docx_"):
        entry_id = data[8:]
        await send_file(query, user_id, entry_id, kind="report")

    elif data.startswith("dl_audio_"):
        entry_id = data[9:]
        await send_file(query, user_id, entry_id, kind="audio")

    elif data.startswith("del_"):
        entry_id = data[4:]
        if delete_entry(user_id, entry_id):
            await query.answer("Удалено")
            await show_reports(query, user_id)
        else:
            await query.answer("Не найдено")


async def show_reports(query, user_id: int):
    entries = list_entries(user_id)
    if not entries:
        kb = [[InlineKeyboardButton("Назад", callback_data="menu_back")]]
        await query.message.edit_text(
            "Картотека пуста. Отправьте аудио, видео или ссылку на Телемост.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    keyboard = []
    for e in entries[:10]:
        label = f"{e['candidate']}"
        if e.get('position'):
            label += f" — {e['position']}"
        label += f" ({e['datetime']})"
        if len(label) > 64:
            label = label[:61] + "..."
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rep_{e['id']}")])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu_back")])

    await query.message.edit_text(
        f"Ваши отчёты ({len(entries)}):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_entry_detail(query, user_id: int, entry_id: str):
    entry = get_entry(user_id, entry_id)
    if not entry:
        await query.answer("Запись не найдена")
        return

    mins = entry["duration_sec"] // 60
    secs = entry["duration_sec"] % 60
    kind = entry.get('kind') or 'media'
    kind_label = TYPE_LABELS.get(kind, kind)

    text = (
        f"Запись.\n"
        f"Название: {entry['candidate']}\n"
        f"Должность: {entry.get('position') or '—'}\n"
        f"Дата: {entry['datetime']}\n"
        f"Длительность: {mins} мин {secs} сек\n"
        f"Спикеров: {entry.get('num_speakers', 0)}\n"
        f"Тип: {kind_label}"
    )

    keyboard = []
    if entry.get("report_file"):
        keyboard.append([InlineKeyboardButton("Скачать DOCX", callback_data=f"dl_docx_{entry_id}")])
    if entry.get("audio_file"):
        keyboard.append([InlineKeyboardButton("Скачать аудио", callback_data=f"dl_audio_{entry_id}")])
    keyboard.append([InlineKeyboardButton("Удалить", callback_data=f"del_{entry_id}")])
    keyboard.append([InlineKeyboardButton("К списку", callback_data="menu_reports")])

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def send_file(query, user_id: int, entry_id: str, kind: str):
    path = get_file_path(user_id, entry_id, kind=kind)
    if not path:
        await query.answer("Файл не найден")
        return

    entry = get_entry(user_id, entry_id)
    filename = os.path.basename(path)
    caption = f"{entry.get('candidate', '')} — {entry.get('position', '')}".strip(" —")

    try:
        if kind == "report":
            with open(path, "rb") as f:
                await query.message.reply_document(
                    document=f, filename=filename, caption=caption
                )
        else:
            with open(path, "rb") as f:
                await query.message.reply_audio(
                    audio=f, filename=filename, caption=caption
                )
    except Exception as e:
        await query.message.reply_text(f"Ошибка отправки: {e}")


# ---------- Запуск ----------
if __name__ == "__main__":
    from config import WHISPER_MODEL, WHISPER_DEVICE, ANALYSIS_ENABLED, STORAGE_DIR
    print(f"[LOG] Запуск (Whisper {WHISPER_MODEL}/{WHISPER_DEVICE}, "
          f"анализ={'вкл' if ANALYSIS_ENABLED else 'выкл'}, storage={STORAGE_DIR})...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"telemost\.yandex\.ru"),
        handle_link
    ))

    if app.job_queue:
        app.job_queue.run_repeating(_auto_stop_watcher, interval=3, first=5)
    else:
        print("[WARN] JobQueue недоступен. Установите: pip install 'python-telegram-bot[job-queue]'")

    print("[LOG] Бот запущен.")
    app.run_polling()