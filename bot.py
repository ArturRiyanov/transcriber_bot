import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"

model = WhisperModel("base", device="cpu", compute_type="int8")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь ссылку на конференцию Яндекс.Телемост (или голосовое для транскрибации)")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("Это не ссылка на Яндекс.Телемост. Проверь, пожалуйста.")
        return

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

            # --- Логика входа в Телемост ---
            # 1. Ищем поле ввода имени (input) и вводим имя
            name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
            if name_input:
                await name_input.fill("Transcriber Bot")
                await page.wait_for_timeout(1000)

            # 2. Ищем кнопку "Подключиться" или "Войти" и кликаем
            join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
            if join_button:
                await join_button.click()
                await page.wait_for_timeout(5000)
            else:
                # Если кнопка не найдена, возможно, это страница с "Принять условия" или что-то похожее
                # Попробуем найти любую основную кнопку в центре
                any_button = await page.query_selector("button[type='submit'], button.btn-primary, button:has-text('Продолжить')")
                if any_button:
                    await any_button.click()
                    await page.wait_for_timeout(5000)

            # 3. Скриншот после попытки входа (может быть комната ожидания или уже конференция)
            screenshot = await page.screenshot()
            await update.message.reply_photo(photo=screenshot, caption="Я попытался войти в конференцию. Если попал в комнату ожидания, организатор должен меня впустить.")

        except Exception as e:
            await update.message.reply_text(f"Ошибка подключения: {e}")
        finally:
            await browser.close()

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
app.add_handler(MessageHandler(filters.COMMAND, start))
app.run_polling()