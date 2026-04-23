import os
import asyncio
import logging
from datetime import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI
import json
from db import init_db, save_reminder, get_pending_reminders, mark_done, get_reminder_by_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TIMEZONE = "America/Guatemala"

openai_client = OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# ── Extraer recordatorio con IA ──────────────────────────────────────────────

def extract_reminder(text: str) -> dict | None:
    now = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    prompt = f"""
Eres un asistente que extrae recordatorios de mensajes en español.
La fecha y hora actual es: {now} (zona horaria Guatemala, UTC-6).

Del siguiente mensaje extrae:
- "task": qué debe recordar el usuario (descripción breve)
- "time": la hora/fecha en formato ISO 8601 (YYYY-MM-DDTHH:MM:00)

Si no hay una hora clara, devuelve null.
Responde SOLO con JSON, sin texto extra, sin backticks.

Mensaje: "{text}"
"""
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    raw = response.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
        if data.get("time") and data.get("task"):
            return data
    except Exception:
        pass
    return None


# ── Transcribir audio ────────────────────────────────────────────────────────

async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="es"
        )
    return transcript.text


# ── Enviar recordatorio ──────────────────────────────────────────────────────

async def send_reminder(app: Application, chat_id: int, reminder_id: int, task: str):
    keyboard = [[InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await app.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *Recordatorio:* {task}\n\nPresiona el botón cuando lo hayas completado.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    # Re-agendar en 5 minutos si no se completa
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=datetime.now(pytz.timezone(TIMEZONE)).replace(second=0, microsecond=0).__class__.now(pytz.timezone(TIMEZONE)).replace(second=0),
        args=[app, chat_id, reminder_id, task],
        id=f"retry_{reminder_id}_{datetime.now().timestamp()}",
        misfire_grace_time=60
    )


async def send_reminder_job(app, chat_id, reminder_id, task):
    """Job que se ejecuta y luego reagenda cada 5 min hasta completar."""
    from db import get_reminder_by_id
    r = get_reminder_by_id(reminder_id)
    if r and not r["done"]:
        keyboard = [[InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"🔔 *Recordatorio:* {task}\n\n¡Presiona Completado cuando termines!",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        # Reagendar en 5 minutos
        from datetime import timedelta
        next_time = datetime.now(pytz.timezone(TIMEZONE)) + timedelta(minutes=5)
        job_id = f"remind_{reminder_id}_{next_time.timestamp()}"
        scheduler.add_job(
            send_reminder_job,
            "date",
            run_date=next_time,
            args=[app, chat_id, reminder_id, task],
            id=job_id,
            misfire_grace_time=120
        )


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot de recordatorios.\n\n"
        "Puedes escribirme o mandarme un audio, por ejemplo:\n"
        "• *'Recuérdame llamar a mi esposa a las 3pm'*\n"
        "• *'Mañana a las 9am tengo reunión con el doctor'*\n\n"
        "Cuando llegue la hora, te avisaré cada 5 minutos hasta que marques ✅ Completado.",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    await update.message.reply_text("⏳ Procesando tu recordatorio...")

    data = extract_reminder(text)
    if not data:
        await update.message.reply_text(
            "❌ No pude entender la hora del recordatorio. "
            "Intenta ser más específico, por ejemplo: 'Recuérdame a las 3pm llamar a Juan'."
        )
        return

    tz = pytz.timezone(TIMEZONE)
    run_date = datetime.fromisoformat(data["time"])
    if run_date.tzinfo is None:
        run_date = tz.localize(run_date)

    reminder_id = save_reminder(chat_id, data["task"], run_date.isoformat())
    job_id = f"remind_{reminder_id}"

    scheduler.add_job(
        send_reminder_job,
        "date",
        run_date=run_date,
        args=[context.application, chat_id, reminder_id, data["task"]],
        id=job_id,
        misfire_grace_time=120
    )

    formatted = run_date.strftime("%d/%m/%Y a las %I:%M %p")
    await update.message.reply_text(
        f"✅ Recordatorio guardado:\n\n"
        f"📌 *{data['task']}*\n"
        f"🕐 {formatted}",
        parse_mode="Markdown"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🎤 Escuché tu audio, transcribiendo...")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/voice_{chat_id}.ogg"
    await file.download_to_drive(file_path)

    try:
        text = await transcribe_audio(file_path)
        await update.message.reply_text(f"📝 Entendí: _{text}_", parse_mode="Markdown")
        # Reusar el handler de texto
        update.message.text = text
        await handle_text(update, context)
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        await update.message.reply_text("❌ No pude procesar el audio. Intenta de nuevo.")


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    reminder_id = int(query.data.split("_")[1])
    mark_done(reminder_id)

    # Cancelar jobs pendientes de este recordatorio
    for job in scheduler.get_jobs():
        if job.id.startswith(f"remind_{reminder_id}"):
            job.remove()

    await query.edit_message_text(
        f"✅ ¡Listo! Recordatorio completado.\n\n_{query.message.text}_",
        parse_mode="Markdown"
    )


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reminders = get_pending_reminders(chat_id)
    if not reminders:
        await update.message.reply_text("📭 No tienes recordatorios pendientes.")
        return

    msg = "📋 *Tus recordatorios pendientes:*\n\n"
    tz = pytz.timezone(TIMEZONE)
    for r in reminders:
        dt = datetime.fromisoformat(r["time"])
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        formatted = dt.strftime("%d/%m/%Y %I:%M %p")
        msg += f"• {r['task']} — {formatted}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lista", list_reminders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_done, pattern=r"^done_\d+$"))

    scheduler.start()
    logger.info("Bot iniciado ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
