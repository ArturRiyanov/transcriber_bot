import os
import asyncio
import subprocess
import time
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
AUDIO_FILE = "recording.wav"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}  # chat_id -> {playwright, browser, context, page, ffmpeg, start_time}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Отправь ссылку на конференцию Яндекс.Телемост.\n"
        "Когда встреча закончится — отправь /stop.\n"
        "Я буду присылать статусы каждого шага."
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    url = update.message.text.strip()

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("❌ Это не ссылка на Яндекс.Телемост. Проверь, пожалуйста.")
        return

    if chat_id in active_sessions:
        await update.message.reply_text("⏳ Уже есть активная сессия. Закрываю старую...")
        await stop_session(chat_id, notify=False)  # без отправки транскрипции, просто закрыть

    await update.message.reply_text("🔄 Подключаюсь к конференции... Это может занять 20-30 секунд.")

    try:
        # Запуск Playwright
        await update.message.reply_text("📡 Запускаю браузер...")
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-sandbox",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
        ])
        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        await update.message.reply_text("🌐 Открываю страницу конференции...")
        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        # Ввод имени
        await update.message.reply_text("✏️ Ввожу имя участника...")
        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("🤖 Запись встречи (Transcriber)")
            await page.wait_for_timeout(1000)

        # Нажатие кнопки входа
        await update.message.reply_text("🚪 Пытаюсь войти в конференцию...")
        join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        # ===== ЗАПУСК ЗАПИСИ через ffmpeg =====
        await update.message.reply_text("🎙️ Запускаю запись аудио (ffmpeg)...")
        ffmpeg_cmd = [
            "ffmpeg",
            "-f", "pulse",
            "-i", "virtual_sink.monitor",   # захват звука с виртуального устройства
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-y",
            AUDIO_FILE
        ]
        ffmpeg_process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Сохраняем сессию
        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'ffmpeg': ffmpeg_process,
            'start_time': datetime.now()
        }

        # Делаем скриншот и отправляем подтверждение
        screenshot = await page.screenshot()
        await update.message.reply_photo(
            photo=screenshot,
            caption="✅ Я вошёл в конференцию и начал запись аудио.\n"
                    "📌 Участники видят меня в списке.\n"
                    "⏹️ Для остановки записи отправьте /stop."
        )

    except Exception as e:
        error_msg = f"❌ Ошибка на этапе подключения:\n{str(e)}"
        await update.message.reply_text(error_msg)
        # Закрываем всё, что успели открыть
        if chat_id in active_sessions:
            await stop_session(chat_id, notify=False)
        else:
            try:
                await browser.close()
            except:
                pass
            try:
                await p.stop()
            except:
                pass

async def stop_session(chat_id, notify=True):
    """Останавливаем запись, закрываем браузер, транскрибируем и отправляем результат"""
    if chat_id not in active_sessions:
        return False

    session = active_sessions[chat_id]
    errors = []
    log_messages = []

    # 1. Останавливаем ffmpeg
    try:
        ffmpeg = session.get('ffmpeg')
        if ffmpeg:
            ffmpeg.terminate()
            stdout, stderr = await ffmpeg.communicate()
            if stderr:
                errors.append(f"ffmpeg stderr: {stderr.decode()[:200]}")
            log_messages.append("⏹️ Запись остановлена.")
    except Exception as e:
        errors.append(f"ffmpeg stop: {e}")

    # 2. Закрываем браузер и Playwright
    try:
        await session['page'].close()
    except Exception as e:
        errors.append(f"page.close(): {e}")
    try:
        await session['context'].close()
    except Exception as e:
        errors.append(f"context.close(): {e}")
    try:
        await session['browser'].close()
    except Exception as e:
        errors.append(f"browser.close(): {e}")
    try:
        await session['playwright'].stop()
    except Exception as e:
        errors.append(f"playwright.stop(): {e}")

    start_time = session.get('start_time')
    if start_time:
        duration = datetime.now() - start_time
        log_messages.append(f"⏱️ Длительность записи: {duration.seconds//60} мин {duration.seconds%60} сек")

    del active_sessions[chat_id]

    # 3. Проверяем файл записи
    if os.path.exists(AUDIO_FILE) and os.path.getsize(AUDIO_FILE) > 0:
        file_size = os.path.getsize(AUDIO_FILE)
        log_messages.append(f"📁 Файл записи: {file_size} байт")
        try:
            # 4. Транскрипция
            log_messages.append("🧠 Начинаю транскрипцию через Whisper...")
            segments, info = model.transcribe(AUDIO_FILE, beam_size=5)
            transcription = " ".join([segment.text for segment in segments])
            log_messages.append("✅ Транскрипция завершена.")

            # Сохраняем текст в файл для отправки
            txt_file = "transcript.txt"
            with open(txt_file, "w", encoding="utf-8") as f:
                f.write(transcription)

            # Отправляем результат
            if notify:
                # Отправляем статусы в чат
                for msg in log_messages:
                    await update.message.reply_text(msg)
                if errors:
                    await update.message.reply_text(f"⚠️ Были ошибки:\n{chr(10).join(errors)}")

                # Отправляем расшифровку
                if len(transcription) > 4000:
                    await update.message.reply_document(
                        document=open(txt_file, "rb"),
                        caption="📝 Расшифровка встречи (файл)"
                    )
                else:
                    await update.message.reply_text(f"📝 Расшифровка:\n\n{transcription}")

                # Чистим временные файлы
                os.remove(AUDIO_FILE)
                os.remove(txt_file)

            return True, transcription, errors

        except Exception as e:
            errors.append(f"transcription: {e}")
            if notify:
                await update.message.reply_text(f"⚠️ Ошибка транскрипции: {e}")
            return True, None, errors
    else:
        errors.append("Аудиофайл не найден или пуст")
        if notify:
            await update.message.reply_text("⚠️ Запись не удалась: аудиофайл отсутствует.")
        return True, None, errors

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Останавливаю сессию, пожалуйста, подождите...")
    try:
        success, transcription, errors = await stop_session(chat_id, notify=True)
        if not success:
            await update.message.reply_text("❌ Нет активной конференции для остановки.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Критическая ошибка при остановке: {e}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обработка загруженных аудиофайлов (оставляем для совместимости)
    await update.message.reply_text("🎧 Секунду, слушаю и переписываю...")
    file = await update.message.effective_attachment.get_file()
    path = "temp_audio." + file.file_path.split(".")[-1]
    await file.download_to_drive(path)
    segments, info = model.transcribe(path)
    text = " ".join([segment.text for segment in segments])
    await update.message.reply_text(f"📝 Расшифровка:\n\n{text}")
    os.remove(path)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["start"]), start))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["stop"]), stop))
app.run_polling()