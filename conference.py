import base64
import tempfile
import shutil
import os
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

from audio_utils import extract_audio_from_video, get_audio_duration
from transcription import transcribe_audio, segments_to_text, text_to_segments
from deepseek_client import (
    improve_text, analyze_interview,
    extract_speakers_and_roles, resplit_by_speakers
)
from docx_builder import create_docx
from storage import save_entry, _safe_filename
from config import (
    ANALYSIS_ENABLED, CANDIDATE_NAME, POSITION_NAME,
    SEND_AUDIO, MAX_AUDIO_SIZE_MB
)

active_sessions = {}

# ---------- JavaScript ----------
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
                        if (typeof obj.getReceivers === 'function') pcCandidates.push(obj);
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
                        if (receiver.track && receiver.track.kind === 'audio') tracks.push(receiver.track);
                    }
                } catch(e) {}
            }
            const mediaElements = document.querySelectorAll('video, audio');
            for (let el of mediaElements) {
                try {
                    if (el.srcObject && el.srcObject instanceof MediaStream) {
                        const audioTracks = el.srcObject.getAudioTracks();
                        for (let track of audioTracks) {
                            if (!tracks.includes(track)) tracks.push(track);
                        }
                    }
                } catch(e) {}
            }
            return tracks;
        }
        const audioTracks = findAllAudioTracks();
        if (audioTracks.length === 0) throw new Error('Не найдено аудио-треков');
        const liveTracks = audioTracks.filter(t => t.readyState === 'live');
        if (liveTracks.length === 0) throw new Error('Все аудио-треки неактивны');
        const stream = new MediaStream(liveTracks);
        let recorder;
        try {
            recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
        } catch (e) {
            recorder = new MediaRecorder(stream);
        }
        const chunks = [];
        recorder.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
        recorder.onstop = () => { window._chunks = chunks; window._recordingComplete = true; };
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
        if (window._stream) window._stream.getTracks().forEach(track => track.stop());
        setTimeout(() => {
            const chunks = window._chunks || [];
            if (chunks.length === 0) {
                resolve({ success: false, message: 'Нет данных' });
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

JS_DISABLE_CAMERA_MIC = """
(function() {
    function clickByKeywords(keywords) {
        const buttons = document.querySelectorAll('button, [role="button"]');
        for (const btn of buttons) {
            const combined = (
                (btn.getAttribute('aria-label') || '') + ' ' +
                (btn.getAttribute('title') || '') + ' ' +
                (btn.innerText || '')
            ).toLowerCase();
            for (const kw of keywords) {
                if (combined.includes(kw)) { btn.click(); return true; }
            }
        }
        return false;
    }
    clickByKeywords(['выключить камеру', 'отключить камеру', 'camera off', 'turn off camera', 'видео выкл']);
    clickByKeywords(['выключить микрофон', 'отключить микрофон', 'mute microphone', 'mic off', 'микрофон выкл']);
    document.querySelectorAll('video, audio').forEach(el => { el.muted = true; });
})();
"""

JS_CHECK_CONFERENCE_ENDED = """
(function() {
    const bodyText = (document.body.innerText || '').toLowerCase();
    const url = window.location.href.toLowerCase();
    const endedKeywords = [
        'встреча завершена', 'встреча закончена', 'встреча окончена',
        'конференция завершена', 'звонок завершён', 'звонок завершен',
        'вы вышли из встречи', 'вы покинули встречу',
        'meeting ended', 'call ended'
    ];
    for (const kw of endedKeywords) {
        if (bodyText.includes(kw)) return { ended: true, reason: kw };
    }
    if (!url.includes('telemost.yandex.ru') || url.endsWith('telemost.yandex.ru/')) {
        return { ended: true, reason: 'url_redirect' };
    }
    return { ended: false };
})();
"""


async def _monitor_conference(chat_id: int, page, update):
    print(f"[Monitor] Запущен для чата {chat_id}")
    try:
        while True:
            await asyncio.sleep(5)
            if chat_id not in active_sessions:
                return
            try:
                result = await page.evaluate(JS_CHECK_CONFERENCE_ENDED)
                if result.get("ended"):
                    print(f"[Monitor] Конференция завершена: {result.get('reason')}")
                    try:
                        await update.message.reply_text(
                            "Конференция завершена. Останавливаю запись и обрабатываю..."
                        )
                    except Exception:
                        pass
                    session = active_sessions.get(chat_id)
                    if session:
                        session['auto_stop'] = True
                    return
            except Exception as e:
                print(f"[Monitor] Ошибка: {e}")
                session = active_sessions.get(chat_id)
                if session:
                    session['auto_stop'] = True
                return
    except asyncio.CancelledError:
        return


async def join_conference(chat_id: int, url: str, update):
    print(f"[LOG] Подключение к конференции: {url}")
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage", "--no-sandbox",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--mute-audio", "--disable-features=AudioServiceOutOfProcess",
        ]
    )
    context = await browser.new_context(
        permissions=["microphone", "camera"],
        viewport={"width": 1280, "height": 720}
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(5000)

        name_input = await page.query_selector(
            "input[placeholder*='имя'], input[placeholder*='Ваше'], input[type='text']"
        )
        if name_input:
            await name_input.fill("Transcriber Bot")
            await page.wait_for_timeout(1000)

        try:
            await page.evaluate(JS_DISABLE_CAMERA_MIC)
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[LOG] Отключение камеры/мика: {e}")

        join_button = await page.query_selector(
            "button:has-text('Подключиться'), button:has-text('Войти'), button:has-text('Присоединиться')"
        )
        if join_button:
            await join_button.click()
            await page.wait_for_timeout(5000)

        try:
            await page.evaluate(JS_DISABLE_CAMERA_MIC)
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        active_sessions[chat_id] = {
            'playwright': p, 'browser': browser, 'context': context,
            'page': page, 'url': url, 'start_time': datetime.now(),
            'update': update, 'auto_stop': False, 'monitor_task': None,
        }

        result = await page.evaluate(JS_START_RECORDING)
        if not result.get("success"):
            await update.message.reply_text(f"Не удалось начать запись: {result.get('message')}")
            return False

        monitor_task = asyncio.create_task(_monitor_conference(chat_id, page, update))
        active_sessions[chat_id]['monitor_task'] = monitor_task

        await update.message.reply_text(
            "Подключился к конференции и начал запись.\n\n"
            "Камера и микрофон отключены.\n"
            "Запись остановится автоматически при завершении конференции или по /stop."
        )
        return True
    except Exception as e:
        print(f"[LOG] Ошибка подключения: {e}")
        await update.message.reply_text(f"Ошибка подключения: {e}")
        return False


async def stop_conference(chat_id: int, update=None):
    if chat_id not in active_sessions:
        if update:
            await update.message.reply_text("Нет активной сессии.")
        return

    session = active_sessions[chat_id]
    page = session['page']
    url = session.get('url', '')

    monitor_task = session.get('monitor_task')
    if monitor_task and not monitor_task.done():
        try:
            monitor_task.cancel()
        except Exception:
            pass

    if update:
        await update.message.reply_text("Останавливаю запись...")

    try:
        result = await page.evaluate(JS_STOP_RECORDING)
    except Exception as e:
        result = {"success": False, "message": str(e)}

    for key in ('page', 'context', 'browser'):
        try:
            await session[key].close()
        except Exception:
            pass
    try:
        await session['playwright'].stop()
    except Exception:
        pass

    if chat_id in active_sessions:
        del active_sessions[chat_id]

    if not result.get("success"):
        if update:
            await update.message.reply_text(f"Ошибка остановки записи: {result.get('message')}")
        return

    audio_base64 = result.get("data")
    if not audio_base64:
        if update:
            await update.message.reply_text("Аудио-данные не получены.")
        return

    tmp_dir = tempfile.mkdtemp(prefix="conference_")
    webm_path = os.path.join(tmp_dir, "recording.webm")
    wav_path = os.path.join(tmp_dir, "recording.wav")

    try:
        with open(webm_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))

        if update:
            await update.message.reply_text("Конвертирую аудио...")

        if not extract_audio_from_video(webm_path, wav_path):
            if update:
                await update.message.reply_text("Ошибка конвертации аудио.")
            return

        duration = get_audio_duration(wav_path)

        if update:
            await update.message.reply_text("Распознаю речь и определяю говорящих...")

        segments = transcribe_audio(wav_path)
        if not segments:
            if update:
                await update.message.reply_text("Речь не обнаружена.")
            return

        transcript_text = segments_to_text(segments)
        improved_text = improve_text(transcript_text)

        speakers_info = {}
        if update:
            await update.message.reply_text("Анализирую участников...")
        try:
            speakers_info = extract_speakers_and_roles(transcript_text)
        except Exception as e:
            print(f"[Speakers] Ошибка: {e}")

        real_speakers = {s.get('speaker') for s in segments if s.get('speaker')}
        num_real = len(real_speakers)
        deepseek_speakers = [
            k for k in speakers_info.keys()
            if k not in ("candidate_speaker", "interviewer_speaker")
        ]
        num_deepseek = len(deepseek_speakers)

        print(f"[LOG] Спикеров: диаризация={num_real}, DeepSeek={num_deepseek}")

        if num_real <= 1 and num_deepseek >= 2:
            print("[Fallback] Переразбиваю текст по смысловым репликам...")
            if update:
                await update.message.reply_text("Разбиваю диалог по репликам...")
            try:
                resplit_text = resplit_by_speakers(transcript_text, speakers_info)
                new_segments = text_to_segments(resplit_text)
                if new_segments and any(s.get('speaker') for s in new_segments):
                    segments = new_segments
            except Exception as e:
                print(f"[Fallback] Ошибка: {e}")

        effective_candidate = CANDIDATE_NAME
        effective_position = POSITION_NAME
        if speakers_info:
            cand_spk = speakers_info.get("candidate_speaker")
            if cand_spk and cand_spk in speakers_info:
                info = speakers_info[cand_spk]
                if isinstance(info, dict):
                    if info.get("name"):
                        effective_candidate = info["name"]
                    if info.get("position"):
                        effective_position = info["position"]

        analysis = ""
        if ANALYSIS_ENABLED:
            if update:
                await update.message.reply_text("Формирую аналитический отчёт (3-5 минут)...")
            analysis = analyze_interview(improved_text, speakers_info=speakers_info)

        docx_path = os.path.join(tmp_dir, "interview_report.docx")
        create_docx(
            segments=segments,
            output_path=docx_path,
            meeting_url=url,
            analysis_text=analysis,
            candidate_name=effective_candidate,
            position=effective_position,
            speakers_info=speakers_info
        )

        # ---------- Сохраняем в картотеку ----------
        num_speakers_final = len({s.get('speaker') for s in segments if s.get('speaker')})
        entry = save_entry(
            user_id=chat_id,
            source_audio=wav_path,
            report_path=docx_path,
            candidate_name=effective_candidate,
            position=effective_position,
            duration=duration,
            num_speakers=num_speakers_final,
            meeting_url=url,
            kind="interview"
        )

        # ---------- Красивое имя файла для отправки ----------
        safe_cand = _safe_filename(effective_candidate or "Кандидат")
        safe_pos = _safe_filename(effective_position) if effective_position else "Без_должности"
        date_part = datetime.now().strftime("%Y-%m-%d_%H-%M")
        base_name = f"{safe_cand}_{safe_pos}_{date_part}"

        # ---------- Отправка аудио ----------
        if SEND_AUDIO and update:
            size_mb = os.path.getsize(wav_path) / (1024 * 1024)
            if size_mb <= MAX_AUDIO_SIZE_MB:
                try:
                    with open(wav_path, "rb") as f:
                        await update.message.reply_audio(
                            audio=f,
                            filename=f"{base_name}.wav",
                            caption=f"Аудиозапись: {effective_candidate} — {effective_position}",
                            title="Интервью",
                            performer="Transcriber Bot"
                        )
                except Exception as e:
                    print(f"[Error] Отправка аудио: {e}")
            else:
                await update.message.reply_text(
                    f"Аудио {size_mb:.1f} МБ > {MAX_AUDIO_SIZE_MB} МБ. "
                    "Скачайте из картотеки через /menu."
                )

        # ---------- Отправка DOCX ----------
        if update:
            with open(docx_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"{base_name}.docx",
                    caption="Отчёт: транскрипция + анализ кандидата"
                )

            await update.message.reply_text(
                "✅ Отчёт сохранён в картотеке.\n"
                "Откройте /menu → Мои отчёты, чтобы скачать позже."
            )

    except Exception as e:
        print(f"[Error] {e}")
        if update:
            await update.message.reply_text(f"Ошибка обработки: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def check_auto_stop(chat_id: int):
    session = active_sessions.get(chat_id)
    if session and session.get('auto_stop'):
        update = session.get('update')
        if update:
            await stop_conference(chat_id, update)
            return True
    return False