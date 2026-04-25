import os
import logging
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import json

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TIMEZONE = "America/Guatemala"

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN no está configurado")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no está configurado")

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Base de datos simple en memoria + archivo
import sqlite3

DB_PATH = "reminders.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            time TEXT NOT NULL,
            done INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_reminder(chat_id, task, time):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO reminders (chat_id, task, time) VALUES (?, ?, ?)", (chat_id, task, time))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def get_reminder(rid):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def mark_done(rid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
    conn.commit()
    conn.close()

def get_pending(chat_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM reminders WHERE chat_id = ? AND done = 0 ORDER BY time ASC", (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── OpenAI via httpx (sin cliente oficial) ───────────────────────────────────

async def extract_reminder(text: str) -> dict | None:
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    prompt = f"""Eres un asistente que extrae recordatorios de mensajes en español.
La fecha y hora actual es: {now} (Guatemala, UTC-6).

Del siguiente mensaje extrae:
- "task": qué debe recordar el usuario
- "time": la hora en formato ISO 8601 (YYYY-MM-DDTHH:MM:00)

Si no hay hora clara, devuelve null para "time".
Responde SOLO con JSON sin texto extra ni backticks.

Mensaje: "{text}"
"""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0
            }
        )
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        try:
            result = json.loads(raw)
            if result.get("time") and result.get("task"):
                return result
        except Exception:
            pass
    return None


async def transcribe_audio(file_path: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as f:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data={"model": "whisper-1", "language": "es"},
                files={"file": ("audio.ogg", f, "audio/ogg")}
            )
        return response.json()["text"]


# ── Jobs ─────────────────────────────────────────────────────────────────────

async def send_reminder_job(app, chat_id, reminder_id, task):
    r = get_reminder(reminder_id)
    if r and not r["done"]:
        keyboard = [[InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder_id}")]]
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"🔔 *Recordatorio:* {task}\n\n¡Presiona Completado cuando termines!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        next_time = datetime.now(pytz.timezone(TIMEZONE)) + timedelta(minutes=5)
        scheduler.add_job(
            send_reminder_job, "date", run_date=next_time,
            args=[app, chat_id, reminder_id, task],
            id=f"remind_{reminder_id}_{next_time.timestamp()}",
            misfire_grace_time=120
        )


# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot de recordatorios.\n\n"
        "Escríbeme o mándame un audio, por ejemplo:\n"
        "• *'Recuérdame llamar a mi esposa a las 3pm'*\n"
        "• *'Mañana a las 9am tengo reunión'*\n\n"
        "Te avisaré cada 5 minutos hasta que marques ✅ Completado.",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Procesando tu recordatorio...")

    try:
        data = await extract_reminder(text)
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        await update.message.reply_text("❌ Error conectando con la IA. Intenta de nuevo.")
        return

    if not data:
        await update.message.reply_text(
            "❌ No pude entender la hora. Intenta: 'Recuérdame a las 3pm llamar a Juan'"
        )
        return

    tz = pytz.timezone(TIMEZONE)
    run_date = datetime.fromisoformat(data["time"])
    if run_date.tzinfo is None:
        run_date = tz.localize(run_date)

    reminder_id = save_reminder(chat_id, data["task"], run_date.isoformat())
    scheduler.add_job(
        send_reminder_job, "date", run_date=run_date,
        args=[context.application, chat_id, reminder_id, data["task"]],
        id=f"remind_{reminder_id}",
        misfire_grace_time=120
    )

    formatted = run_date.strftime("%d/%m/%Y a las %I:%M %p")
    await update.message.reply_text(
        f"✅ *Recordatorio guardado:*\n\n📌 {data['task']}\n🕐 {formatted}",
        parse_mode="Markdown"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🎤 Transcribiendo tu audio...")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/voice_{chat_id}.ogg"
    await file.download_to_drive(file_path)
    try:
        text = await transcribe_audio(file_path)
        await update.message.reply_text(f"📝 Entendí: _{text}_", parse_mode="Markdown")
        update.message.text = text
        await handle_text(update, context)
    except Exception as e:
        logger.error(f"Error audio: {e}")
        await update.message.reply_text("❌ No pude procesar el audio. Intenta de nuevo.")


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reminder_id = int(query.data.split("_")[1])
    mark_done(reminder_id)
    for job in scheduler.get_jobs():
        if job.id.startswith(f"remind_{reminder_id}"):
            job.remove()
    await query.edit_message_text("✅ ¡Recordatorio completado!", parse_mode="Markdown")


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reminders = get_pending(chat_id)
    if not reminders:
        await update.message.reply_text("📭 No tienes recordatorios pendientes.")
        return
    tz = pytz.timezone(TIMEZONE)
    msg = "📋 *Tus recordatorios pendientes:*\n\n"
    for r in reminders:
        dt = datetime.fromisoformat(r["time"])
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        msg += f"• {r['task']} — {dt.strftime('%d/%m %I:%M %p')}\n"
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
    logger.info("✅ Bot iniciado correctamente")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
