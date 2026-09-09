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

        # Ввод имени
        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("Transcriber Bot")
            await page.wait_for_timeout(1000)

        join_button = await page.query_selector("button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')")
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        # ========== ОТПРАВКА СООБЩЕНИЯ В ЧАТ (улучшенная) ==========
        try:
            # Ждём, пока появится чат (любой элемент с редактируемым полем)
            await page.wait_for_selector(
                "textarea, input[type='text'], div[contenteditable='true'], [role='textbox']",
                timeout=15000
            )
            # Ищем поле ввода с помощью JavaScript (более надёжно)
            message_text = "🤖 Этот бот записывает аудио для создания транскрипции встречи. Пожалуйста, подтвердите своё согласие на запись. Если вы против, просто скажите – я завершу сессию."
            sent = await page.evaluate(f"""
                async () => {{
                    // Поиск поля ввода
                    let input = document.querySelector('textarea') ||
                                document.querySelector('input[type="text"]') ||
                                document.querySelector('[contenteditable="true"]') ||
                                document.querySelector('[role="textbox"]');
                    if (!input) return false;

                    // Устанавливаем текст
                    if (input.tagName === 'DIV' && input.contentEditable === 'true') {{
                        input.innerText = `{message_text}`;
                    }} else {{
                        input.value = `{message_text}`;
                    }}

                    // Ищем кнопку отправки
                    let sendBtn = document.querySelector('button[aria-label*="отправить"]') ||
                                  document.querySelector('button[aria-label*="Send"]') ||
                                  document.querySelector('button[type="submit"]') ||
                                  document.querySelector('button:has-text("Отправить")') ||
                                  document.querySelector('button:has-text("Send")');
                    if (sendBtn) {{
                        sendBtn.click();
                        return true;
                    }} else {{
                        // Если кнопки нет, имитируем нажатие Enter
                        const enterEvent = new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', which: 13}});
                        input.dispatchEvent(enterEvent);
                        return true;
                    }}
                }}
            """)
            if sent:
                await page.wait_for_timeout(2000)
            else:
                # Если JS не сработал, пробуем старый метод с селекторами
                chat_input = await page.query_selector("textarea, input[type='text'], div[contenteditable='true']")
                if chat_input:
                    await chat_input.fill(message_text)
                    await page.wait_for_timeout(1000)
                    send_button = await page.query_selector("button[aria-label*='отправить'], button[type='submit']")
                    if send_button:
                        await send_button.click()
                    else:
                        await chat_input.press("Enter")
        except Exception as e:
            # Если не удалось, просто логируем (в Telegram не отправляем, чтобы не сбивать пользователя)
            print(f"Не удалось отправить сообщение: {e}")

        # Сохраняем сессию
        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page
        }

        screenshot = await page.screenshot()
        await update.message.reply_photo(photo=screenshot, caption="Я вошёл в конференцию и попытался отправить сообщение о записи. Остаюсь здесь, пока ты не отправишь /stop.")

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