import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}

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
            "--use-fake-ui-for-media-stream"
        ])
        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("Transcriber Bot")
            await page.wait_for_timeout(1000)

        join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page
        }

        screenshot = await page.screenshot()
        await update.message.reply_photo(photo=screenshot, caption="Я вошёл в конференцию! Остаюсь здесь, пока ты не отправишь /stop.")

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
    if chat_id not in active_sessions:
        return False

    session = active_sessions[chat_id]
    errors = []

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

    if errors:
        raise Exception("; ".join(errors))
    return True

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        closed = await stop_session(chat_id)
        if closed:
            await update.message.reply_text("✅ Я вышел из конференции.")
        else:
            await update.message.reply_text("❌ Нет активной конференции для остановки.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка при выходе: {e}\nПопробуйте перезапустить бота вручную.")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
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