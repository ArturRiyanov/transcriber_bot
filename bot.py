import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"

model = WhisperModel("base", device="cpu", compute_type="int8")

# Хранилище активных браузеров: {chat_id: {'browser': ..., 'context': ..., 'page': ...}}
active_sessions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь ссылку на конференцию Яндекс.Телемост (или голосовое для транскрибации). Когда закончишь — отправь /stop.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    url = update.message.text.strip()

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("Это не ссылка на Яндекс.Телемост. Проверь, пожалуйста.")
        return

    # Если уже есть активная сессия — закроем старую
    if chat_id in active_sessions:
        await stop_session(chat_id)

    await update.message.reply_text("Подключаюсь к конференции... Это может занять 20-30 секунд.")

    async with async_playwright() as p:
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

        try:
            await page.goto(url, wait_until="load", timeout=60000)
            await page.wait_for_timeout(5000)

            # Логика входа
            name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
            if name_input:
                await name_input.fill("Transcriber Bot")
                await page.wait_for_timeout(1000)

            join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
            if join_button:
                await join_button.click()
                await page.wait_for_timeout(5000)

            # Сохраняем сессию
            active_sessions[chat_id] = {'browser': browser, 'context': context, 'page': page}

            # Отправляем подтверждение
            screenshot = await page.screenshot()
            await update.message.reply_photo(photo=screenshot, caption="Я вошёл в конференцию! Остаюсь здесь, пока ты не отправишь /stop.")

        except Exception as e:
            await update.message.reply_text(f"Ошибка подключения: {e}")
            await browser.close()
            if chat_id in active_sessions:
                del active_sessions[chat_id]

async def stop_session(chat_id):
    """Закрываем браузер для конкретного чата"""
    if chat_id in active_sessions:
        session = active_sessions[chat_id]
        await session['browser'].close()
        del active_sessions[chat_id]
        return True
    return False

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    closed = await stop_session(chat_id)
    if closed:
        await update.message.reply_text("Я вышел из конференции. Спасибо!")
    else:
        await update.message.reply_text("Нет активной конференции для остановки.")

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