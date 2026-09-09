import os
import asyncio
import psutil
import shutil
import subprocess
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
AUDIO_WEBM = "recording.webm"
AUDIO_WAV = "recording.wav"
TXT_FILE = "transcript.txt"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}

# ---------- JavaScript для записи через Web Audio ----------
JS_START_RECORDING = """
async function startRecording() {
    try {
        // 1. Создаём аудио-контекст
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        // 2. Создаём destination для получения потока
        const dest = audioCtx.createMediaStreamDestination();
        // 3. Находим все медиа-элементы
        const mediaElements = document.querySelectorAll('video, audio');
        let connected = 0;
        for (let el of mediaElements) {
            try {
                const source = audioCtx.createMediaElementSource(el);
                source.connect(dest);
                connected++;
            } catch (e) {
                // Элемент не поддерживает createMediaElementSource (например, уже используется)
            }
        }
        // 4. Если есть глобальный AudioContext (Web Audio), пытаемся подключиться к нему
        if (window._audioContext) {
            try {
                // Создаём gain-узел для тихого микширования
                const gain = audioCtx.createGain();
                gain.gain.value = 1;
                // Подключаем существующий контекст к нашему (это сложно, но мы можем просто использовать его поток)
                // Вместо этого, если есть существующий MediaStream из веб-конференции, можно подключить его напрямую
                // Но проще считать, что звук идёт через медиа-элементы
            } catch (e) {}
        }
        if (connected === 0) {
            // Если не удалось подключить ни одного элемента, пробуем захватить системный звук через getUserMedia
            // Но это может попросить микрофон, что не то же самое
            // Вместо этого создаём фиктивный источник тишины, чтобы не было ошибки
            // Но лучше выдать ошибку
            throw new Error('Не найдено аудио-элементов для захвата');
        }
        // 5. Получаем поток из destination
        const stream = dest.stream;
        // 6. Запускаем MediaRecorder
        const options = { mimeType: 'audio/webm;codecs=opus' };
        let recorder;
        try {
            recorder = new MediaRecorder(stream, options);
        } catch (e) {
            recorder = new MediaRecorder(stream);
        }
        const chunks = [];
        recorder.ondataavailable = e => chunks.push(e.data);
        recorder.onstop = () => {
            const blob = new Blob(chunks, { type: 'audio/webm' });
            window._recordingBlob = blob;
            window._recordingComplete = true;
        };
        recorder.start();
        window._recorder = recorder;
        window._chunks = chunks;
        window._recordingComplete = false;
        // Сохраняем контекст для остановки
        window._audioCtx = audioCtx;
        return { success: true, message: 'Запись аудио через Web Audio запущена, подключено: ' + connected };
    } catch (err) {
        return { success: false, message: err.message };
    }
}
"""

JS_STOP_RECORDING = """
function stopRecordingAndGetData() {
    return new Promise((resolve) => {
        if (!window._recorder) {
            resolve({ success: false, message: 'Рекордер не найден' });
            return;
        }
        window._recorder.onstop = () => {
            const blob = window._recordingBlob;
            if (!blob) {
                resolve({ success: false, message: 'Blob не создан' });
                return;
            }
            const reader = new FileReader();
            reader.onloadend = () => {
                const base64data = reader.result.split(',')[1];
                resolve({ success: true, data: base64data, size: blob.size });
            };
            reader.readAsDataURL(blob);
        };
        window._recorder.stop();
        if (window._recorder.stream) {
            window._recorder.stream.getTracks().forEach(track => track.stop());
        }
        // Закрываем аудио-контекст
        if (window._audioCtx) {
            window._audioCtx.close();
        }
    });
}
"""

# ---------- Вспомогательная функция ----------
def convert_webm_to_wav(webm_path, wav_path):
    cmd = [
        "ffmpeg", "-i", webm_path,
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y", wav_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion error: {result.stderr}")
    return wav_path

# ---------- Команды бота ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Привет! Отправь ссылку на конференцию Яндекс.Телемост.\n"
        "Когда встреча закончится — отправь /stop.\n"
        "Я запишу аудио через Web Audio API и пришлю расшифровку."
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

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--enable-audio",
                "--use-fake-device-for-media-stream",  # помогает с audio-устройствами
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

        # Включаем звук на странице
        await page.evaluate("""
            document.querySelectorAll('video, audio').forEach(el => el.muted = false);
            document.querySelectorAll('[aria-label*="sound" i], [aria-label*="mute" i]').forEach(el => el.click());
        """)

        await update.message.reply_text("🎙️ Запускаю запись аудио через Web Audio...")
        result = await page.evaluate(JS_START_RECORDING)
        if not result.get("success"):
            await update.message.reply_text(f"❌ Не удалось начать запись: {result.get('message')}")
            await browser.close()
            await p.stop()
            return

        await update.message.reply_text("✅ Запись аудио запущена.")

        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'start_time': datetime.now(),
            'update': update,
            'recording_started': True
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
        return False, None, ["Сессия не найдена"]

    session = active_sessions[chat_id]
    if update is None:
        update = session.get('update')

    errors = []
    log_msgs = []

    try:
        if update:
            await update.message.reply_text("⏹️ Останавливаю запись...")
        page = session['page']
        result = await page.evaluate(JS_STOP_RECORDING)
        if not result.get("success"):
            errors.append(f"Ошибка остановки записи: {result.get('message')}")
        else:
            audio_base64 = result.get("data")
            if audio_base64:
                with open(AUDIO_WEBM, "wb") as f:
                    f.write(base64.b64decode(audio_base64))
                log_msgs.append(f"✅ Аудио получено, размер: {result.get('size')} байт")
                if update:
                    await update.message.reply_text("🔄 Конвертирую аудио в WAV...")
                convert_webm_to_wav(AUDIO_WEBM, AUDIO_WAV)
                os.remove(AUDIO_WEBM)
                log_msgs.append("✅ Конвертация завершена")
            else:
                errors.append("Аудио-данные не получены")
    except Exception as e:
        errors.append(f"Ошибка при остановке записи: {e}")

    # Закрытие браузера и Playwright
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

    # Проверка аудиофайла
    if not os.path.exists(AUDIO_WAV) or os.path.getsize(AUDIO_WAV) == 0:
        errors.append("Аудиофайл не создан или пуст.")
        if update and notify:
            await update.message.reply_text("⚠️ Аудиофайл не создан. Запись не удалась.")
        return False, None, errors

    size = os.path.getsize(AUDIO_WAV)
    log_msgs.append(f"📁 Размер аудио: {size} байт")

    # Транскрипция
    try:
        if update and notify:
            await update.message.reply_text("🧠 Начинаю транскрипцию...")

        if os.path.getsize(AUDIO_WAV) < 1000:
            await update.message.reply_text("⚠️ Аудиофайл очень маленький (вероятно, тишина). Проверьте звук в конференции.")

        segments, info = model.transcribe(AUDIO_WAV, beam_size=5)
        transcription = " ".join([seg.text for seg in segments])
        log_msgs.append("✅ Транскрипция завершена.")

        with open(TXT_FILE, "w", encoding="utf-8") as f:
            f.write(transcription)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_saved = f"recording_{timestamp}.wav"
        txt_saved = f"transcript_{timestamp}.txt"
        shutil.copy(AUDIO_WAV, audio_saved)
        shutil.copy(TXT_FILE, txt_saved)

        if update and notify:
            for msg in log_msgs:
                await update.message.reply_text(msg)
            if errors:
                await update.message.reply_text(f"⚠️ Ошибки:\n{chr(10).join(errors)}")

            if os.path.exists(AUDIO_WAV) and os.path.getsize(AUDIO_WAV) > 0:
                with open(AUDIO_WAV, "rb") as f:
                    await update.message.reply_audio(
                        audio=f,
                        filename="recording.wav",
                        caption="🎧 Аудиозапись встречи"
                    )

            if os.path.exists(TXT_FILE):
                with open(TXT_FILE, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename="transcript.txt",
                        caption="📝 Расшифровка встречи"
                    )

            if not transcription.strip():
                await update.message.reply_text("⚠️ Внимание: расшифровка пуста. Возможно, в записи нет речи или аудио слишком тихое.")

            os.remove(AUDIO_WAV)
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
    await update.message.reply_text("🎧 Секунду...")
    file = await update.message.effective_attachment.get_file()
    path = "temp_audio." + file.file_path.split(".")[-1]
    await file.download_to_drive(path)
    segments, info = model.transcribe(path)
    text = " ".join([seg.text for seg in segments])
    await update.message.reply_text(f"📝 Расшифровка:\n\n{text}")
    os.remove(path)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.run_polling(close_loop=False)