import os
import asyncio
import psutil
import shutil
import subprocess
import base64
import json
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from faster_whisper import WhisperModel
from playwright.async_api import async_playwright

# ⚠️ ЗАМЕНИТЕ НА НОВЫЙ ТОКЕН ОТ @BotFather
TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"
AUDIO_WEBM = "recording.webm"
AUDIO_WAV = "recording.wav"
TXT_FILE = "transcript.txt"

model = WhisperModel("base", device="cpu", compute_type="int8")
active_sessions = {}

# ---------- JavaScript для записи через WebRTC ----------
JS_START_RECORDING = """
async function startRecording() {
    try {
        function findAllAudioTracks() {
            const tracks = [];
            const pcCandidates = [];
            for (let key in window) {
                try {
                    const obj = window[key];
                    if (obj && typeof obj === 'object') {
                        if (typeof obj.getReceivers === 'function') {
                            pcCandidates.push(obj);
                        }
                        if (Array.isArray(obj)) {
                            for (let item of obj) {
                                if (item && typeof item === 'object' && typeof item.getReceivers === 'function') {
                                    pcCandidates.push(item);
                                }
                            }
                        }
                    }
                } catch(e) {}
            }
            for (let pc of pcCandidates) {
                try {
                    const receivers = pc.getReceivers ? pc.getReceivers() : [];
                    for (let receiver of receivers) {
                        if (receiver.track && receiver.track.kind === 'audio') {
                            tracks.push(receiver.track);
                        }
                    }
                } catch(e) {}
            }
            const mediaElements = document.querySelectorAll('video, audio');
            for (let el of mediaElements) {
                try {
                    if (el.srcObject && el.srcObject instanceof MediaStream) {
                        const audioTracks = el.srcObject.getAudioTracks();
                        for (let track of audioTracks) {
                            if (!tracks.includes(track)) {
                                tracks.push(track);
                            }
                        }
                    }
                } catch(e) {}
            }
            if (window.telemost && window.telemost.peerConnection) {
                try {
                    const pc = window.telemost.peerConnection;
                    if (typeof pc.getReceivers === 'function') {
                        const receivers = pc.getReceivers();
                        for (let receiver of receivers) {
                            if (receiver.track && receiver.track.kind === 'audio' && !tracks.includes(receiver.track)) {
                                tracks.push(receiver.track);
                            }
                        }
                    }
                } catch(e) {}
            }
            return tracks;
        }

        const audioTracks = findAllAudioTracks();
        if (audioTracks.length === 0) {
            throw new Error('Не найдено аудио-треков ни из WebRTC, ни из медиа-элементов');
        }
        const liveTracks = audioTracks.filter(t => t.readyState === 'live');
        if (liveTracks.length === 0) {
            throw new Error('Все найденные аудио-треки неактивны (readyState !== live)');
        }
        const stream = new MediaStream(liveTracks);
        if (stream.getAudioTracks().length === 0) {
            throw new Error('Не удалось создать MediaStream с живыми треками');
        }
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
        window._stream = stream;
        return { success: true, message: 'Запись запущена, треков: ' + liveTracks.length };
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
        if (window._stream) {
            window._stream.getTracks().forEach(track => track.stop());
        }
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
    const foundPCs = [];
    for (let key in window) {
        try {
            const obj = window[key];
            if (obj && typeof obj === 'object') {
                if (typeof obj.getReceivers === 'function') {
                    const receivers = obj.getReceivers ? obj.getReceivers() : [];
                    foundPCs.push({
                        key: key,
                        receivers: receivers.map(r => ({
                            kind: r.track ? r.track.kind : 'unknown',
                            state: r.track ? r.track.readyState : 'unknown',
                            enabled: r.track ? r.track.enabled : 'unknown'
                        }))
                    });
                }
                if (Array.isArray(obj)) {
                    for (let item of obj) {
                        if (item && typeof item === 'object' && typeof item.getReceivers === 'function') {
                            const receivers = item.getReceivers ? item.getReceivers() : [];
                            foundPCs.push({
                                key: key + '[' + obj.indexOf(item) + ']',
                                receivers: receivers.map(r => ({
                                    kind: r.track ? r.track.kind : 'unknown',
                                    state: r.track ? r.track.readyState : 'unknown',
                                    enabled: r.track ? r.track.enabled : 'unknown'
                                }))
                            });
                        }
                    }
                }
            }
        } catch(e) {}
    }
    info.foundPCs = foundPCs;

    const mediaElements = document.querySelectorAll('video, audio');
    info.mediaElementsCount = mediaElements.length;
    info.mediaElements = [];
    for (let el of mediaElements) {
        const item = { tag: el.tagName, hasSrcObject: !!el.srcObject };
        if (el.srcObject) {
            try {
                item.tracks = el.srcObject.getTracks().map(t => ({
                    kind: t.kind,
                    state: t.readyState,
                    enabled: t.enabled
                }));
            } catch(e) {
                item.tracksError = e.message;
            }
        }
        info.mediaElements.push(item);
    }

    const knownKeys = ['telemost', 'Telemost', 'conference', 'webrtc', 'peerConnection'];
    for (let key of knownKeys) {
        if (window[key]) {
            info[key] = typeof window[key] === 'object' ? 'exists' : window[key];
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
    text = json.dumps(result, indent=2, ensure_ascii=False)
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
    print(f"[LOG] Получена ссылка: {url}")

    if "telemost.yandex.ru" not in url:
        await update.message.reply_text("❌ Это не ссылка на Яндекс.Телемост.")
        return

    if chat_id in active_sessions:
        await update.message.reply_text("⏳ Закрываю старую сессию...")
        await stop_session(chat_id, update=update, notify=False)

    await update.message.reply_text("🔄 Подключаюсь к конференции...")

    try:
        print("[LOG] Запуск Playwright...")
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

        print("[LOG] Открываю страницу...")
        await update.message.reply_text("🌐 Открываю страницу...")
        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        print("[LOG] Ввожу имя...")
        await update.message.reply_text("✏️ Ввожу имя...")
        name_input = await page.query_selector("input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']")
        if name_input:
            await name_input.fill("🤖 Запись встречи (Transcriber)")
            await page.wait_for_timeout(1000)

        print("[LOG] Пытаюсь войти...")
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

        # ✅ СОХРАНЯЕМ СЕССИЮ СРАЗУ ПОСЛЕ ВХОДА
        active_sessions[chat_id] = {
            'playwright': p,
            'browser': browser,
            'context': context,
            'page': page,
            'start_time': datetime.now(),
            'update': update,
            'recording_started': False,   # пока запись не запущена
        }

        # Теперь запускаем запись
        print("[LOG] Запуск записи...")
        await update.message.reply_text("🎙️ Запускаю запись аудио через WebRTC...")
        result = await page.evaluate(JS_START_RECORDING)
        print(f"[LOG] Результат записи: {result}")

        if not result.get("success"):
            await update.message.reply_text(f"❌ Не удалось начать запись: {result.get('message')}")
            # Сессия уже сохранена, поэтому /debug будет работать
            screenshot = await page.screenshot()
            await update.message.reply_photo(
                photo=screenshot,
                caption="❌ Запись не удалась, но страница открыта. Используйте /debug для диагностики."
            )
            return

        # Если запись успешна, отмечаем это
        active_sessions[chat_id]['recording_started'] = True
        await update.message.reply_text("✅ Запись аудио запущена.")

        screenshot = await page.screenshot()
        await update.message.reply_photo(
            photo=screenshot,
            caption="✅ Я вошёл и начал запись.\n⏹️ Для остановки — /stop."
        )

    except Exception as e:
        print(f"[LOG] Ошибка: {e}")
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

    # Если запись не была запущена, просто закрываем
    if not session.get('recording_started', False):
        if update:
            await update.message.reply_text("ℹ️ Запись не была запущена, закрываю сессию.")
        # Закрываем браузер и удаляем сессию
        try:
            await session['page'].close()
        except: pass
        try:
            await session['context'].close()
        except: pass
        try:
            await session['browser'].close()
        except: pass
        try:
            await session['playwright'].stop()
        except: pass
        del active_sessions[chat_id]
        return False, None, ["Запись не была запущена"]

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
    print("[LOG] Запуск бота...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("debug", debug))
    print("[LOG] Бот запущен, начинаю polling...")
    app.run_polling(close_loop=False)