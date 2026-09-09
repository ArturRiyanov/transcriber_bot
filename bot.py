import os
import asyncio
import subprocess
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
AUDIO_FILE = "recording.wav"  # временный файл для записи

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}  # chat_id -> {playwright, browser, context, page, ffmpeg_process}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь ссылку на конференцию Яндекс.Телемост. Когда закончишь — отправь /stop.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    url = update.message.text.strip()

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("Это не ссылка на Яндекс.Телемост. Проверь, пожалуйста.")
        return

    if chat_id in active_sessions:
        await stop_session(chat_id)

    await update.message.reply_text("Подключаюсь к конференции... Это может занять 20-30 секунд.")

    try:
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-sandbox",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            # Указываем вывод звука в виртуальное устройство (pulseaudio)
            "--alsa-output-device=virtual_sink",
            "--audio-output-channels=2"
        ])
        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        # Ввод имени
        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("🤖 Запись встречи (Transcriber)")
            await page.wait_for_timeout(1000)

        join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        # ===== ЗАПУСК ЗАПИСИ через ffmpeg =====
        # Запись с виртуального устройства в WAV (можно MP3, но WAV быстрее обрабатывается)
        # Используем arecord или ffmpeg: ffmpeg -f pulse -i virtual_sink.monitor -acodec pcm_s16le recording.wav
        ffmpeg_cmd = [
            "ffmpeg",
            "-f", "pulse",
            "-i", "virtual_sink.monitor",   # монитор виртуального устройства (захват звука)
            "-acodec", "pcm_s16le",
            "-ar", "16000",                 # частота для Whisper
            "-ac", "1",                     # моно
            "-y",                           # перезаписать файл
            AUDIO_FILE
        ]
        ffmpeg_process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Сохраняем сессию вместе с процессом записи
        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'ffmpeg': ffmpeg_process
        }

        screenshot = await page.screenshot()
        await update.message.reply_photo(
            photo=screenshot,
            caption="✅ Я вошёл в конференцию и начал запись аудио. Участники видят меня в списке. Для остановки отправьте /stop."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка подключения: {e}")
        if chat_id in active_sessions:
            await stop_session(chat_id)
        else:
            try:
                await browser.close()
            except:
                pass
            try:
                await p.stop()
            except:
                pass

async def stop_session(chat_id):
    """Останавливаем запись, закрываем браузер, транскрибируем и отправляем результат"""
    if chat_id not in active_sessions:
        return False

    session = active_sessions[chat_id]
    errors = []

    # 1. Останавливаем ffmpeg (отправляем SIGTERM)
    try:
        ffmpeg = session.get('ffmpeg')
        if ffmpeg:
            ffmpeg.terminate()
            await ffmpeg.wait()  # ждём завершения
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

    del active_sessions[chat_id]

    # 3. Транскрибируем записанный файл (если он существует)
    if os.path.exists(AUDIO_FILE) and os.path.getsize(AUDIO_FILE) > 0:
        try:
            segments, info = model.transcribe(AUDIO_FILE, beam_size=5)
            transcription = " ".join([segment.text for segment in segments])
            # Сохраняем текст в файл для отправки (если длинный)
            with open("transcript.txt", "w", encoding="utf-8") as f:
                f.write(transcription)
            # Возвращаем результат для отправки пользователю
            return True, transcription, errors
        except Exception as e:
            errors.append(f"transcription: {e}")
            return True, None, errors
    else:
        errors.append("Аудиофайл не найден или пуст")
        return True, None, errors

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        success, transcription, errors = await stop_session(chat_id)
        if success:
            if transcription:
                # Отправляем текст (если длинный, то файлом)
                if len(transcription) > 4000:
                    await update.message.reply_document(
                        document=open("transcript.txt", "rb"),
                        caption="📝 Расшифровка встречи (файл)"
                    )
                else:
                    await update.message.reply_text(f"📝 Расшифровка:\n\n{transcription}")
                # Удаляем временные файлы
                os.remove(AUDIO_FILE)
                os.remove("transcript.txt")
            else:
                await update.message.reply_text("⚠️ Запись выполнена, но транскрипция не удалась.")
            if errors:
                await update.message.reply_text(f"⚠️ Были ошибки: {', '.join(errors)}")
            else:
                await update.message.reply_text("✅ Я вышел из конференции, запись обработана.")
        else:
            await update.message.reply_text("❌ Нет активной конференции для остановки.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка при обработке: {e}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Оставляем старую обработку голосовых (для загруженных файлов)
    await update.message.reply_text("Секунду, слушаю и переписываю...")
    file = await update.message.effective_attachment.get_file()
    path = "temp_audio." + file.file_path.split(".")[-1]
    await file.download_to_drive(path)
    segments, info = model.transcribe(path)
    text = " ".join([segment.text for segment in segments])
    await update.message.reply_text(text)
    os.remove(path)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["start"]), start))
app.add_handler(MessageHandler(filters.COMMAND & filters.Text(["stop"]), stop))
app.run_polling()