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

# ⚠️ ЗАМЕНИТЕ НА НОВЫЙ ТОКЕН (скомпрометирован старый!)
TOKEN = "НОВЫЙ_ТОКЕН_ОТ_BOTFATHER"
AUDIO_WEBM = "recording.webm"
AUDIO_WAV = "recording.wav"
TXT_FILE = "transcript.txt"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}

# ---------- JavaScript для записи через WebRTC ----------
JS_START_RECORDING = """
async function startRecording() {
    try {
        // Функция поиска всех аудио-треков из всех RTCPeerConnection
        function getAllAudioTracks() {
            const tracks = [];
            // Способ 1: если есть глобальный массив peerConnections
            if (window.peerConnections && Array.isArray(window.peerConnections)) {
                for (let pc of window.peerConnections) {
                    try {
                        const receivers = pc.getReceivers ? pc.getReceivers() : [];
                        for (let receiver of receivers) {
                            if (receiver.track && receiver.track.kind === 'audio') {
                                tracks.push(receiver.track);
                            }
                        }
                    } catch (e) {}
                }
            }
            // Способ 2: если есть глобальный объект конференции (Яндекс.Телемост)
            if (window.telemost && window.telemost.peerConnection) {
                const pc = window.telemost.peerConnection;
                try {
                    const receivers = pc.getReceivers ? pc.getReceivers() : [];
                    for (let receiver of receivers) {
                        if (receiver.track && receiver.track.kind === 'audio') {
                            tracks.push(receiver.track);
                        }
                    }
                } catch (e) {}
            }
            // Способ 3: прямой перебор глобальных объектов (можно добавить при необходимости)
            return tracks;
        }

        const audioTracks = getAllAudioTracks();
        if (audioTracks.length === 0) {
            throw new Error('Не найдено аудио-треков из WebRTC');
        }
        // Создаём MediaStream из найденных треков
        const stream = new MediaStream(audioTracks);
        // Проверяем, что треки живы
        const liveTracks = stream.getAudioTracks().filter(t => t.readyState === 'live');
        if (liveTracks.length === 0) {
            throw new Error('Все аудио-треки неактивны (readyState !== live)');
        }
        // Запускаем MediaRecorder
        const options = { mimeType: 'audio/webm;codecs=opus' };
        let recorder;
        try {
            recorder = new MediaRecorder(stream, options);
        } catch (e) {
            recorder = new MediaRecorder(stream);
        }
        const chunks = [];
        recorder.ondataavailable = e => {
            if (e.data.size > 0) chunks.push(e.data);
        };
        recorder.onstop = () => {
            window._chunks = chunks;
            window._recordingComplete = true;
        };
        recorder.start(1000);
        window._recorder = recorder;
        window._recordingComplete = false;
        window._stream = stream; // сохраняем для остановки
        return { success: true, message: 'Запись WebRTC аудио запущена, треков: ' + liveTracks.length };
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
        const recorder = window._recorder;
        recorder.stop();
        // Останавливаем треки
        if (window._stream) {
            window._stream.getTracks().forEach(track => track.stop());
        }
        // Ждём завершения onstop
        setTimeout(() => {
            const chunks = window._chunks || [];
            if (chunks.length === 0) {
                resolve({ success: false, message: 'Нет данных для создания blob' });
                return;
            }
            const blob = new Blob(chunks, { type: 'audio/webm' });
            const reader = new FileReader();
            reader.onloadend = () => {
                const base64data = reader.result.split(',')[1];
                resolve({ success: true, data: base64data, size: blob.size });
            };
            reader.readAsDataURL(blob);
        }, 1500);
    });
}
"""

# ---------- Диагностика WebRTC ----------
JS_DEBUG_WEBRTC = """
function debugWebRTC() {
    const info = {};
    // Проверяем, есть ли window.peerConnections
    if (window.peerConnections && Array.isArray(window.peerConnections)) {
        info.peerConnectionsCount = window.peerConnections.length;
        info.receivers = [];
        for (let pc of window.peerConnections) {
            try {
                const receivers = pc.getReceivers ? pc.getReceivers() : [];
                for (let receiver of receivers) {
                    if (receiver.track) {
                        info.receivers.push({
                            kind: receiver.track.kind,
                            state: receiver.track.readyState,
                            enabled: receiver.track.enabled
                        });
                    }
                }
            } catch (e) {}
        }
    }
    // Проверяем наличие telemost
    if (window.telemost) {
        info.telemost = 'найден';
        if (window.telemost.peerConnection) {
            info.telemostPC = 'найден';
            try {
                const receivers = window.telemost.peerConnection.getReceivers ? window.telemost.peerConnection.getReceivers() : [];
                info.telemostReceivers = [];
                for (let receiver of receivers) {
                    if (receiver.track) {
                        info.telemostReceivers.push({
                            kind: receiver.track.kind,
                            state: receiver.track.readyState,
                            enabled: receiver.track.enabled
                        });
                    }
                }
            } catch (e) {}
        }
    }
    // Проверяем DOM-элементы
    const mediaElements = document.querySelectorAll('video, audio');
    info.mediaElementsCount = mediaElements.length;
    info.mediaElementsWithSrcObject = 0;
    for (let el of mediaElements) {
        if (el.srcObject && el.srcObject instanceof MediaStream) {
            info.mediaElementsWithSrcObject++;
        }
    }
    return info;
}
"""

# ---------- Команда диагностики ----------
async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sessions:
        await update.message.reply_text("❌ Нет активной сессии. Сначала отправьте ссылку.")
        return
    page = active_sessions[chat_id]['page']
    result = await page.evaluate(JS_DEBUG_WEBRTC)
    # Преобразуем результат в читаемый текст
    import json
    text = json.dumps(result, indent=2, ensure_ascii=False)
    # Если текст слишком длинный, обрежем
    if len(text) > 4000:
        text = text[:4000] + "..."
    await update.message.reply_text(f"📊 Диагностика WebRTC:\n<pre>{text}</pre>", parse_mode='HTML')

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
        "Я запишу аудио через WebRTC и пришлю расшифровку.\n"
        "Также доступна команда /debug для диагностики WebRTC."
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
                "--use-fake-device-for-media-stream",
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

        # Включаем звук на странице (на случай, если muted)
        await page.evaluate("""
            document.querySelectorAll('video, audio').forEach(el => el.muted = false);
            document.querySelectorAll('[aria-label*="sound" i], [aria-label*="mute" i]').forEach(el => el.click());
        """)

        await update.message.reply_text("🎙️ Запускаю запись аудио через WebRTC...")
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
    app.add_handler(CommandHandler("debug", debug))
    app.run_polling(close_loop=False)