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
active_sessions = {}

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
        await update.message.reply_text("❌ Это не ссылка на Яндекс.Телемост.")
        return

    if chat_id in active_sessions:
        await update.message.reply_text("⏳ Закрываю старую сессию...")
        await stop_session(chat_id, update=update, notify=False)

    await update.message.reply_text("🔄 Подключаюсь к конференции...")

    try:
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

        await update.message.reply_text("🌐 Открываю страницу...")
        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        await update.message.reply_text("✏️ Ввожу имя...")
        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("🤖 Запись встречи (Transcriber)")
            await page.wait_for_timeout(1000)

        await update.message.reply_text("🚪 Пытаюсь войти...")
        join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        await update.message.reply_text("🎙️ Запускаю ffmpeg...")
        ffmpeg_cmd = [
            "ffmpeg",
            "-f", "pulse",
            "-i", "virtual_sink.monitor",
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

        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'ffmpeg': ffmpeg_process,
            'start_time': datetime.now(),
            'update': update  # сохраняем для отправки логов
        }

        screenshot = await page.screenshot()
        await update.message.reply_photo(
            photo=screenshot,
            caption="✅ Я вошёл и начал запись.\n⏹️ Для остановки — /stop."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        if chat_id in active_sessions:
            await stop_session(chat_id, update=update, notify=False)
        else:
            try:
                await browser.close()
            except:
                pass
            try:
                await p.stop()
            except:
                pass

async def stop_session(chat_id, update=None, notify=True):
    """Останавливаем ffmpeg, закрываем браузер, транскрибируем и отправляем результат."""
    if chat_id not in active_sessions:
        if update and notify:
            await update.message.reply_text("❌ Нет активной сессии для остановки.")
        return False

    session = active_sessions[chat_id]
    if update is None:
        update = session.get('update')  # берём сохранённый update

    errors = []
    log_msgs = []

    # 1. Останавливаем ffmpeg с таймаутом
    try:
        ffmpeg = session.get('ffmpeg')
        if ffmpeg:
            if update:
                await update.message.reply_text("⏹️ Останавливаю ffmpeg...")
            ffmpeg.terminate()
            try:
                # Ждём завершения до 10 секунд
                stdout, stderr = await asyncio.wait_for(ffmpeg.communicate(), timeout=10.0)
                if stderr:
                    errors.append(f"ffmpeg stderr: {stderr.decode()[:200]}")
                log_msgs.append("ffmpeg завершён.")
            except asyncio.TimeoutError:
                # Если не завершился, убиваем принудительно
                if update:
                    await update.message.reply_text("⚠️ ffmpeg не завершился за 10 сек, убиваю...")
                ffmpeg.kill()
                stdout, stderr = await ffmpeg.communicate()
                errors.append("ffmpeg убит по таймауту")
    except Exception as e:
        errors.append(f"ffmpeg error: {e}")

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

    # 3. Длительность
    if session.get('start_time'):
        duration = datetime.now() - session['start_time']
        log_msgs.append(f"⏱️ Длительность: {duration.seconds//60} мин {duration.seconds%60} сек")

    del active_sessions[chat_id]

    # 4. Проверяем файл записи
    if os.path.exists(AUDIO_FILE):
        size = os.path.getsize(AUDIO_FILE)
        log_msgs.append(f"📁 Размер файла: {size} байт")
        if size == 0:
            errors.append("Файл записи пуст.")
            if update and notify:
                await update.message.reply_text("⚠️ Аудиофайл пуст. Запись не удалась.")
            return False, None, errors
    else:
        errors.append("Файл записи не найден.")
        if update and notify:
            await update.message.reply_text("⚠️ Аудиофайл не найден. Запись не удалась.")
        return False, None, errors

    # 5. Транскрипция
    try:
        if update and notify:
            await update.message.reply_text("🧠 Начинаю транскрипцию...")
        segments, info = model.transcribe(AUDIO_FILE, beam_size=5)
        transcription = " ".join([seg.text for seg in segments])
        log_msgs.append("✅ Транскрипция завершена.")

        txt_file = "transcript.txt"
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(transcription)

        if update and notify:
            # Отправляем все логи
            for msg in log_msgs:
                await update.message.reply_text(msg)
            if errors:
                await update.message.reply_text(f"⚠️ Ошибки:\n{chr(10).join(errors)}")
            # Отправляем расшифровку
            if len(transcription) > 4000:
                await update.message.reply_document(
                    document=open(txt_file, "rb"),
                    caption="📝 Расшифровка (файл)"
                )
            else:
                await update.message.reply_text(f"📝 Расшифровка:\n\n{transcription}")
            # Чистим файлы
            os.remove(AUDIO_FILE)
            os.remove(txt_file)
        return True, transcription, errors
    except Exception as e:
        errors.append(f"transcription: {e}")
        if update and notify:
            await update.message.reply_text(f"⚠️ Ошибка транскрипции: {e}")
        return True, None, errors

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Останавливаю сессию...")
    try:
        success, transcription, errors = await stop_session(chat_id, update=update, notify=True)
        if not success:
            await update.message.reply_text("❌ Не удалось остановить сессию.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Критическая ошибка: {e}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обработка загруженных аудио (оставляем для совместимости)
    await update.message.reply_text("🎧 Секунду...")
    file = await update.message.effective_attachment.get_file()
    path = "temp_audio." + file.file_path.split(".")[-1]
    await file.download_to_drive(path)
    segments, info = model.transcribe(path)
    text = " ".join([seg.text for seg in segments])
    await update.message.reply_text(f"📝 Расшифровка:\n\n{text}")
    os.remove(path)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["start"]), start))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["stop"]), stop))
app.run_polling()