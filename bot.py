import os
import logging
import json
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TIMEZONE = "America/Guatemala"
DB_PATH = "reminders.db"

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN no está configurado")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no está configurado")

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

LIST_PHRASES = [
    "lista",
    "mis recordatorios",
    "muéstrame",
    "muestrame",
    "ver recordatorios",
    "qué tengo",
    "que tengo",
    "pendientes",
    "mis alertas",
    "ver mis",
    "show",
    "listar",
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            time TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_reminder(chat_id: int, task: str, time: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, task, time) VALUES (?, ?, ?)",
        (chat_id, task, time),
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()
    return reminder_id


def get_reminder(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_done(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()
    conn.close()


def delete_reminder(reminder_id: int):
    mark_done(reminder_id)


def get_pending(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM reminders
        WHERE chat_id = ? AND done = 0
        ORDER BY time ASC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def is_list_request(text: str) -> bool:
    text_lower = text.lower().strip()
    return any(phrase in text_lower for phrase in LIST_PHRASES)


def remove_reminder_jobs(reminder_id: int):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"remind_{reminder_id}"):
            job.remove()


async def extract_reminder(text: str) -> dict | None:
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    prompt = f"""
Eres un asistente que extrae recordatorios de mensajes en español.

Fecha y hora actual: {now}
Zona horaria: Guatemala, America/Guatemala, UTC-6.

Extrae del mensaje:
- "task": la acción breve que se debe recordar
- "time": fecha y hora exacta en formato ISO 8601: YYYY-MM-DDTHH:MM:00

Reglas:
- Si dice "en 1 minuto", suma 1 minuto a la hora actual.
- Si dice "en 2 minutos", suma 2 minutos.
- Si dice "mañana", usa la fecha de mañana.
- Si dice "a las 3", interpreta según contexto como 3 PM si parece horario diurno.
- Si no hay hora clara, devuelve "time": null.
- Responde SOLO JSON válido.
- No uses backticks.
- No agregues explicación.

Mensaje: "{text}"

Formato:
{{"task":"...", "time":"YYYY-MM-DDTHH:MM:00"}}
"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
        )

    data = response.json()

    if response.status_code != 200:
        logger.error(f"OpenAI error {response.status_code}: {data}")
        return None

    raw = data["choices"][0]["message"]["content"].strip()

    try:
        result = json.loads(raw)
    except Exception as e:
        logger.error(f"Error parseando JSON: {e} | Respuesta: {raw}")
        return None

    if result.get("task") and result.get("time"):
        return result

    return None


async def transcribe_audio(file_path: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as audio_file:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data={
                    "model": "whisper-1",
                    "language": "es",
                },
                files={
                    "file": ("audio.ogg", audio_file, "audio/ogg"),
                },
            )

    data = response.json()

    if response.status_code != 200:
        logger.error(f"OpenAI audio error {response.status_code}: {data}")
        raise Exception("Error transcribiendo audio")

    return data["text"]


async def send_reminder_job(app, chat_id: int, reminder_id: int, task: str):
    reminder = get_reminder(reminder_id)

    if not reminder or reminder["done"]:
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder_id}"),
            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"del_{reminder_id}"),
        ]
    ]

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *Recordatorio:* {task}\n\n¡Presiona Completado cuando termines!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    next_time = datetime.now(pytz.timezone(TIMEZONE)) + timedelta(minutes=5)

    scheduler.add_job(
        send_reminder_job,
        "date",
        run_date=next_time,
        args=[app, chat_id, reminder_id, task],
        id=f"remind_{reminder_id}_{next_time.timestamp()}",
        misfire_grace_time=120,
    )


async def create_reminder_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
):
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
            "❌ No pude entender la hora.\n\n"
            "Intenta: *'Recuérdame en 10 minutos tomar agua'*\n\n"
            "O escribe *'lista'* para ver tus recordatorios.",
            parse_mode="Markdown",
        )
        return

    tz = pytz.timezone(TIMEZONE)
    run_date = datetime.fromisoformat(data["time"])

    if run_date.tzinfo is None:
        run_date = tz.localize(run_date)

    now = datetime.now(tz)

    if run_date <= now:
        await update.message.reply_text(
            "❌ Esa hora ya pasó. Intenta con una hora futura.",
        )
        return

    reminder_id = save_reminder(chat_id, data["task"], run_date.isoformat())

    scheduler.add_job(
        send_reminder_job,
        "date",
        run_date=run_date,
        args=[context.application, chat_id, reminder_id, data["task"]],
        id=f"remind_{reminder_id}",
        misfire_grace_time=120,
    )

    formatted = run_date.strftime("%d/%m/%Y a las %I:%M %p")

    await update.message.reply_text(
        f"✅ *Recordatorio guardado:*\n\n"
        f"📌 {data['task']}\n"
        f"🕐 {formatted}",
        parse_mode="Markdown",
    )


async def show_reminders(update: Update, chat_id: int):
    reminders = get_pending(chat_id)

    if not reminders:
        await update.message.reply_text("📭 No tienes recordatorios pendientes.")
        return

    tz = pytz.timezone(TIMEZONE)

    await update.message.reply_text("📋 *Tus recordatorios pendientes:*", parse_mode="Markdown")

    for reminder in reminders:
        dt = datetime.fromisoformat(reminder["time"])

        if dt.tzinfo is None:
            dt = tz.localize(dt)

        formatted = dt.strftime("%d/%m/%Y a las %I:%M %p")

        keyboard = [
            [
                InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder['id']}"),
                InlineKeyboardButton("🗑️ Eliminar", callback_data=f"del_{reminder['id']}"),
            ]
        ]

        await update.message.reply_text(
            f"📌 *{reminder['task']}*\n🕐 {formatted}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name if user and user.first_name else "!"

    await update.message.reply_text(
        f"👋 ¡Hola {first_name}! Soy tu asistente de recordatorios.\n\n"
        "Puedes escribirme o mandarme un audio, por ejemplo:\n"
        "• *Recuérdame llamar a mi esposa a las 3pm*\n"
        "• *Recuérdame en 10 minutos tomar agua*\n"
        "• *Mañana a las 9am tengo reunión*\n\n"
        "Para ver tus recordatorios escribe:\n"
        "• *lista*\n"
        "• *mis recordatorios*\n\n"
        "Te avisaré cada 5 minutos hasta que marques ✅ Completado.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if is_list_request(text):
        await show_reminders(update, chat_id)
        return

    await create_reminder_from_text(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await update.message.reply_text("🎤 Transcribiendo tu audio...")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/voice_{chat_id}.ogg"

    await file.download_to_drive(file_path)

    try:
        text = await transcribe_audio(file_path)

        await update.message.reply_text(
            f"📝 Entendí: _{text}_",
            parse_mode="Markdown",
        )

        if is_list_request(text):
            await show_reminders(update, chat_id)
            return

        await create_reminder_from_text(update, context, text)

    except Exception as e:
        logger.error(f"Error audio: {e}")
        await update.message.reply_text("❌ No pude procesar el audio. Intenta de nuevo.")


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    reminder_id = int(data.split("_")[1])

    reminder = get_reminder(reminder_id)

    if not reminder:
        await query.edit_message_text("❌ Ese recordatorio ya no existe.")
        return

    if data.startswith("done_"):
        mark_done(reminder_id)
        remove_reminder_jobs(reminder_id)
        await query.edit_message_text("✅ ¡Recordatorio completado!")

    elif data.startswith("del_"):
        delete_reminder(reminder_id)
        remove_reminder_jobs(reminder_id)
        await query.edit_message_text("🗑️ Recordatorio eliminado.")


def restore_pending_reminders(app: Application):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM reminders
        WHERE done = 0
        """
    ).fetchall()
    conn.close()

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    for row in rows:
        reminder = dict(row)
        run_date = datetime.fromisoformat(reminder["time"])

        if run_date.tzinfo is None:
            run_date = tz.localize(run_date)

        if run_date <= now:
            run_date = now + timedelta(seconds=10)

        scheduler.add_job(
            send_reminder_job,
            "date",
            run_date=run_date,
            args=[app, reminder["chat_id"], reminder["id"], reminder["task"]],
            id=f"remind_{reminder['id']}",
            replace_existing=True,
            misfire_grace_time=120,
        )

    logger.info(f"🔁 Recordatorios restaurados: {len(rows)}")


def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lista", lambda update, context: show_reminders(update, update.effective_chat.id)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_buttons, pattern=r"^(done|del)_\d+$"))

    scheduler.start()
    restore_pending_reminders(app)

    logger.info("✅ Bot iniciado correctamente")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
