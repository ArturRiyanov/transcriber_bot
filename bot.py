import os
import asyncio
import time
import psutil
import shutil
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
AUDIO_FILE = "recording.wav"
TXT_FILE = "transcript.txt"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Отправь ссылку на конференцию Яндекс.Телемост.\n"
        "Когда встреча закончится — отправь /stop.\n"
        "Я пришлю аудиозапись и расшифровку."
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
        
        # Запускаем браузер (без указания PULSE_SINK, звук пойдёт в системный sink по умолчанию)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
            ]
        )
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

        # Запуск ffmpeg – записываем с системного монитора по умолчанию
        await update.message.reply_text("🎙️ Запускаю ffmpeg...")
        ffmpeg_cmd = (
            f"ffmpeg -f pulse -i default.monitor "  # <-- ИСПРАВЛЕНО
            f"-acodec pcm_s16le -ar 16000 -ac 1 -y {AUDIO_FILE} 2> ffmpeg_error.log"
        )
        ffmpeg_process = await asyncio.create_subprocess_shell(
            ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd()
        )

        await asyncio.sleep(2)
        if os.path.exists(AUDIO_FILE) and os.path.getsize(AUDIO_FILE) > 0:
            await update.message.reply_text("✅ ffmpeg запущен, файл записи создан.")
        else:
            error_log = ""
            if os.path.exists("ffmpeg_error.log"):
                with open("ffmpeg_error.log", "r") as f:
                    error_log = f.read()[:500]
            await update.message.reply_text(f"⚠️ ffmpeg не создал файл. Ошибка: {error_log if error_log else 'неизвестна'}")

        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'ffmpeg': ffmpeg_process,
            'start_time': datetime.now(),
            'update': update,
            'cmd': ffmpeg_cmd
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
    if chat_id not in active_sessions:
        if update and notify:
            await update.message.reply_text("❌ Нет активной сессии для остановки.")
        return False

    session = active_sessions[chat_id]
    if update is None:
        update = session.get('update')

    errors = []
    log_msgs = []

    # Остановка ffmpeg через psutil
    try:
        ffmpeg_proc = session.get('ffmpeg')
        if ffmpeg_proc:
            if update:
                await update.message.reply_text("⏹️ Останавливаю ffmpeg...")
            parent = psutil.Process(ffmpeg_proc.pid)
            children = parent.children(recursive=True)
            procs_to_kill = [parent] + children
            for p in procs_to_kill:
                try:
                    p.terminate()
                except psutil.NoSuchProcess:
                    pass
            gone, alive = psutil.wait_procs(procs_to_kill, timeout=5)
            for p in alive:
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
            log_msgs.append("✅ ffmpeg остановлен.")
    except Exception as e:
        errors.append(f"ffmpeg stop error: {e}")

    # Закрытие браузера
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

    if session.get('start_time'):
        duration = datetime.now() - session['start_time']
        log_msgs.append(f"⏱️ Длительность: {duration.seconds//60} мин {duration.seconds%60} сек")

    del active_sessions[chat_id]

    # Проверка файла
    if not os.path.exists(AUDIO_FILE) or os.path.getsize(AUDIO_FILE) == 0:
        errors.append("Файл записи не найден или пуст.")
        if update and notify:
            await update.message.reply_text("⚠️ Аудиофайл не найден или пуст. Запись не удалась.")
            if os.path.exists("ffmpeg_error.log"):
                with open("ffmpeg_error.log", "r") as f:
                    error_text = f.read()
                await update.message.reply_text(f"📄 Лог ошибок ffmpeg:\n{error_text[:500]}")
        return False, None, errors

    size = os.path.getsize(AUDIO_FILE)
    log_msgs.append(f"📁 Размер файла: {size} байт")

    # Транскрипция
    try:
        if update and notify:
            await update.message.reply_text("🧠 Начинаю транскрипцию...")
        
        # Проверим, что файл не пустой и содержит звук
        if os.path.getsize(AUDIO_FILE) < 1000:  # меньше 1 КБ — почти наверняка пусто
            await update.message.reply_text("⚠️ Аудиофайл слишком маленький (вероятно, тишина). Проверьте звук в конференции.")
        
        segments, info = model.transcribe(AUDIO_FILE, beam_size=5)
        transcription = " ".join([seg.text for seg in segments])
        log_msgs.append("✅ Транскрипция завершена.")

        # Сохраняем текст в файл
        with open(TXT_FILE, "w", encoding="utf-8") as f:
            f.write(transcription)

        # Сохраняем копии с датой на сервере
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_saved = f"recording_{timestamp}.wav"
        txt_saved = f"transcript_{timestamp}.txt"
        shutil.copy(AUDIO_FILE, audio_saved)
        shutil.copy(TXT_FILE, txt_saved)

        if update and notify:
            # Отправляем логи
            for msg in log_msgs:
                await update.message.reply_text(msg)
            if errors:
                await update.message.reply_text(f"⚠️ Ошибки:\n{chr(10).join(errors)}")

            # Отправляем аудиофайл
            if os.path.exists(AUDIO_FILE) and os.path.getsize(AUDIO_FILE) > 0:
                with open(AUDIO_FILE, "rb") as f:
                    await update.message.reply_audio(
                        audio=f,
                        filename="recording.wav",
                        caption="🎧 Аудиозапись встречи"
                    )

            # Отправляем текстовый файл (всегда)
            if os.path.exists(TXT_FILE):
                with open(TXT_FILE, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename="transcript.txt",
                        caption="📝 Расшифровка встречи"
                    )

            # Если транскрипция пустая – предупреждаем
            if not transcription.strip():
                await update.message.reply_text("⚠️ Внимание: расшифровка пуста. Возможно, в записи нет речи или аудио слишком тихое.")

            # Удаляем временные файлы (копии с датой остаются на сервере)
            os.remove(AUDIO_FILE)
            os.remove(TXT_FILE)

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
    # Обработка загруженных аудио (для совместимости)
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
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("stop", stop))

app.run_polling(close_loop=False)