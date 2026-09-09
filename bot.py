import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from faster_whisper import WhisperModel

TOKEN = "8401430343:AAGWyxI_6x6kVtjtDL36NMn4f0oILhTZMUE"

model = WhisperModel("base", device="cpu", compute_type="int8")

async def start(update, context):
    await update.message.reply_text("Привет! Кидай голосовое или короткое видео (до 20МБ) - я перепишу в текст.")

async def handle_audio(update, context):
    await update.message.reply_text("Секунду, слушаю и переписываю...")
    file = await update.message.effective_attachment.get_file()
    path = "temp_audio." + file.file_path.split(".")[-1]
    await file.download_to_drive(path)
    segments, info = model.transcribe(path)
    text = " ".join([segment.text for segment in segments])
    await update.message.reply_text(text)
    os.remove(path)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, handle_audio))
app.add_handler(MessageHandler(filters.COMMAND, start))
app.run_polling()