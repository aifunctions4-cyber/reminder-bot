import os
import logging
import json
import sqlite3
import re
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

REMINDER_LIST_PHRASES = [
    "mis recordatorios",
    "ver recordatorios",
    "qué tengo pendiente",
    "que tengo pendiente",
    "recordatorios pendientes",
    "mis alertas",
]

LISTS_PHRASES = [
    "listas",
    "mis listas",
    "muéstrame mis listas",
    "muestrame mis listas",
    "mostrar mis listas",
    "ver mis listas",
]


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()

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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS list_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (list_id) REFERENCES lists (id)
        )
        """
    )

    # Migraciones simples para bases de datos existentes.
    existing_columns = [row[1] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()]

    if "repeat" not in existing_columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT DEFAULT 'none'")

    if "active" not in existing_columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN active INTEGER DEFAULT 1")

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# REMINDERS DB
# ─────────────────────────────────────────────────────────────────────────────

def save_reminder(chat_id: int, task: str, time: str, repeat: str = "none") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, task, time, repeat, active) VALUES (?, ?, ?, ?, 1)",
        (chat_id, task, time, repeat),
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()
    return reminder_id


def get_reminder(reminder_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_done(reminder_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()
    conn.close()


def deactivate_reminder(reminder_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE reminders SET active = 0, done = 1 WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()
    conn.close()


def update_reminder_time(reminder_id: int, new_time: str):
    conn = get_conn()
    conn.execute(
        "UPDATE reminders SET time = ? WHERE id = ?",
        (new_time, reminder_id),
    )
    conn.commit()
    conn.close()


def get_pending_reminders(chat_id: int):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM reminders
        WHERE chat_id = ? AND done = 0 AND active = 1
        ORDER BY time ASC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def remove_reminder_jobs(reminder_id: int):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"remind_{reminder_id}"):
            job.remove()


# ─────────────────────────────────────────────────────────────────────────────
# LISTS DB
# ─────────────────────────────────────────────────────────────────────────────

def create_list(chat_id: int, name: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO lists (chat_id, name) VALUES (?, ?)",
        (chat_id, name.strip()),
    )
    conn.commit()
    list_id = cur.lastrowid
    conn.close()
    return list_id


def get_lists(chat_id: int):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM lists
        WHERE chat_id = ?
        ORDER BY created_at DESC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_list_by_id(list_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM lists WHERE id = ?",
        (list_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_list_by_name(chat_id: int, name: str):
    name_clean = name.strip().lower()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM lists WHERE chat_id = ?",
        (chat_id,),
    ).fetchall()
    conn.close()

    for row in rows:
        if row["name"].strip().lower() == name_clean:
            return dict(row)

    for row in rows:
        if name_clean in row["name"].strip().lower() or row["name"].strip().lower() in name_clean:
            return dict(row)

    return None


def delete_list(list_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    conn.commit()
    conn.close()


def add_item_to_list(list_id: int, item: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO list_items (list_id, item) VALUES (?, ?)",
        (list_id, item.strip()),
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_items(list_id: int):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM list_items
        WHERE list_id = ?
        ORDER BY created_at ASC
        """,
        (list_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def toggle_item(item_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT completed FROM list_items WHERE id = ?",
        (item_id,),
    ).fetchone()

    if not row:
        conn.close()
        return

    new_value = 0 if row["completed"] else 1

    conn.execute(
        "UPDATE list_items SET completed = ? WHERE id = ?",
        (new_value, item_id),
    )
    conn.commit()
    conn.close()


def delete_item(item_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# TEXT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    return text.lower().strip()


def is_reminder_list_request(text: str) -> bool:
    text_lower = normalize(text)
    return any(phrase in text_lower for phrase in REMINDER_LIST_PHRASES)


def is_lists_request(text: str) -> bool:
    text_lower = normalize(text)
    return any(phrase in text_lower for phrase in LISTS_PHRASES)


def is_create_list_request(text: str) -> bool:
    text_lower = normalize(text)
    patterns = [
        "crear una lista nueva",
        "crear lista nueva",
        "nueva lista",
        "quiero crear una lista",
        "crear una lista",
    ]
    return any(pattern in text_lower for pattern in patterns)


def parse_add_item_request(text: str):
    """
    Supported examples:
    - agregar leche a la lista de supermercado
    - agrega pan a la lista supermercado
    - añade huevos a la lista de compras
    """
    text_clean = text.strip()

    pattern = re.compile(
        r"^(agregar|agrega|añadir|añade|poner|pon)\s+(.+?)\s+a\s+la\s+lista(?:\s+de)?\s+(.+)$",
        re.IGNORECASE,
    )

    match = pattern.match(text_clean)

    if not match:
        return None

    item = match.group(2).strip().strip('"').strip("'")
    list_name = match.group(3).strip().strip('"').strip("'")

    if not item or not list_name:
        return None

    return item, list_name


def split_items(text: str):
    """
    Guarda items aunque el usuario los escriba:
    - con comas: leche, pan, huevos
    - con saltos de línea
    - sin comas: leche pan huevos
    - por audio: "leche pan huevos"
    """
    cleaned = text.strip()

    cleaned = re.sub(
        r"^(agrega|agregar|añade|añadir|pon|poner|mete|meter|incluye|incluir)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    if re.search(r"[,\n;]+", cleaned):
        raw_parts = re.split(r"[,\n;]+", cleaned)
    else:
        raw_parts = cleaned.split()

    items = []
    for part in raw_parts:
        item = part.strip().strip("-").strip("•").strip(".").strip()
        if item:
            items.append(item)

    return items


# ─────────────────────────────────────────────────────────────────────────────
# OPENAI
# ─────────────────────────────────────────────────────────────────────────────

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
- "repeat": "none" o "daily"

Reglas:
- Si dice "en 1 minuto", suma 1 minuto a la hora actual.
- Si dice "en 2 minutos", suma 2 minutos.
- Si dice "mañana", usa la fecha de mañana.
- Si dice "todos los días", "cada día", "diario", "diariamente" o "todos los dias", usa repeat: "daily".
- Si dice "todos los días a las 8pm", usa la próxima fecha a las 20:00.
- Si dice "a las 8 de la noche", interpreta 20:00.
- Si dice "a las 8 pm", interpreta 20:00.
- Si dice "a las 8 am", interpreta 08:00.
- Si dice "a las 3", interpreta según contexto como 3 PM si parece horario diurno.
- Si no hay hora clara, devuelve "time": null.
- Si el mensaje parece de listas, compras o items sin hora, devuelve "time": null.
- Responde SOLO JSON válido.
- No uses backticks.
- No agregues explicación.

Mensaje: "{text}"

Formato:
{{"task":"...", "time":"YYYY-MM-DDTHH:MM:00", "repeat":"none"}}
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
        if result.get("repeat") not in ["none", "daily"]:
            result["repeat"] = "none"
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


# ─────────────────────────────────────────────────────────────────────────────
# REMINDERS
# ─────────────────────────────────────────────────────────────────────────────

def get_next_daily_time(dt: datetime) -> datetime:
    return dt + timedelta(days=1)


def schedule_reminder_job(app, chat_id: int, reminder_id: int, task: str, run_date: datetime):
    scheduler.add_job(
        send_reminder_job,
        "date",
        run_date=run_date,
        args=[app, chat_id, reminder_id, task],
        id=f"remind_{reminder_id}",
        replace_existing=True,
        misfire_grace_time=120,
    )


async def send_reminder_job(app, chat_id: int, reminder_id: int, task: str):
    reminder = get_reminder(reminder_id)

    if not reminder or reminder.get("done") or not reminder.get("active", 1):
        return

    repeat = reminder.get("repeat", "none")

    keyboard = [
        [
            InlineKeyboardButton("✅ Completado", callback_data=f"done_{reminder_id}"),
            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"delrem_{reminder_id}"),
        ]
    ]

    repeat_text = "\n🔁 Se repetirá todos los días." if repeat == "daily" else ""

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *Recordatorio:* {task}{repeat_text}\n\n¡Presiona Completado cuando termines!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Si no lo marcas completado, te seguirá insistiendo cada 5 minutos.
    next_time = datetime.now(pytz.timezone(TIMEZONE)) + timedelta(minutes=5)

    scheduler.add_job(
        send_reminder_job,
        "date",
        run_date=next_time,
        args=[app, chat_id, reminder_id, task],
        id=f"remind_{reminder_id}_{next_time.timestamp()}",
        misfire_grace_time=120,
    )


async def create_reminder_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
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
            "❌ No pude entender una hora para recordatorio.\n\n"
            "Ejemplos:\n"
            "• *Recuérdame en 10 minutos tomar agua*\n"
            "• *Recuérdame mañana a las 9 llamar al doctor*\n"
            "• *Recuérdame todos los días a las 8pm dar la pastilla*\n\n"
            "Para listas puedes decir:\n"
            "• *crear una lista nueva*\n"
            "• *agregar leche a la lista de supermercado*\n"
            "• *muéstrame mis listas*",
            parse_mode="Markdown",
        )
        return

    tz = pytz.timezone(TIMEZONE)
    run_date = datetime.fromisoformat(data["time"])

    if run_date.tzinfo is None:
        run_date = tz.localize(run_date)

    now = datetime.now(tz)

    repeat = data.get("repeat", "none")

    if run_date <= now:
        if repeat == "daily":
            run_date = get_next_daily_time(run_date)
        else:
            await update.message.reply_text("❌ Esa hora ya pasó. Intenta con una hora futura.")
            return

    reminder_id = save_reminder(chat_id, data["task"], run_date.isoformat(), repeat)

    schedule_reminder_job(
        context.application,
        chat_id,
        reminder_id,
        data["task"],
        run_date,
    )

    formatted = run_date.strftime("%d/%m/%Y a las %I:%M %p")
    repeat_text = "\n🔁 Se repetirá todos los días." if repeat == "daily" else ""

    await update.message.reply_text(
        f"✅ *Recordatorio guardado:*\n\n"
        f"📌 {data['task']}\n"
        f"🕐 {formatted}"
        f"{repeat_text}",
        parse_mode="Markdown",
    )


async def show_reminders(update: Update, chat_id: int):
    reminders = get_pending_reminders(chat_id)

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
                InlineKeyboardButton("🗑️ Eliminar", callback_data=f"delrem_{reminder['id']}"),
            ]
        ]

        repeat_text = "\n🔁 Todos los días" if reminder.get("repeat") == "daily" else ""

        await update.message.reply_text(
            f"📌 *{reminder['task']}*\n🕐 {formatted}{repeat_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


def restore_pending_reminders(app: Application):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM reminders
        WHERE done = 0 AND active = 1
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
            if reminder.get("repeat") == "daily":
                while run_date <= now:
                    run_date = get_next_daily_time(run_date)
                update_reminder_time(reminder["id"], run_date.isoformat())
            else:
                run_date = now + timedelta(seconds=10)

        schedule_reminder_job(
            app,
            reminder["chat_id"],
            reminder["id"],
            reminder["task"],
            run_date,
        )

    logger.info(f"🔁 Recordatorios restaurados: {len(rows)}")


# ─────────────────────────────────────────────────────────────────────────────
# LISTS
# ─────────────────────────────────────────────────────────────────────────────

async def ask_list_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_list_name"] = True
    await update.message.reply_text(
        "📝 Perfecto. ¿Qué nombre quieres ponerle a la lista?\n\n"
        "Ejemplo: *Supermercado*, *Trabajo*, *Viaje*, *Compras*",
        parse_mode="Markdown",
    )


async def create_list_from_name(update: Update, context: ContextTypes.DEFAULT_TYPE, list_name: str):
    chat_id = update.effective_chat.id
    list_name = list_name.strip()

    if len(list_name) < 2:
        await update.message.reply_text("❌ El nombre es muy corto. Escribe otro nombre.")
        return

    list_id = create_list(chat_id, list_name)
    context.user_data["waiting_for_list_name"] = False
    context.user_data["adding_items_to_list_id"] = list_id
    context.user_data["adding_items_to_list_name"] = list_name

    await update.message.reply_text(
        f"✅ Lista creada: *{list_name}*\n\n"
        "Ahora escribe los items como quieras: con comas, sin comas, por mensaje o por audio.\n\n"
        "Ejemplos:\n"
        "*leche, pan, papel, frutas, verduras*\n"
        "*leche pan papel frutas verduras*\n\n"
        "Cuando termines, escribe *listo*.",
        parse_mode="Markdown",
    )


async def handle_new_list_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await create_list_from_name(update, context, update.message.text)


async def handle_add_item(update: Update, text: str):
    chat_id = update.effective_chat.id
    parsed = parse_add_item_request(text)

    if not parsed:
        return False

    item, list_name = parsed
    found_list = find_list_by_name(chat_id, list_name)

    if not found_list:
        await update.message.reply_text(
            f"❌ No encontré una lista llamada *{list_name}*.\n\n"
            f"Escribe *listas* para ver tus listas o *crear una lista nueva*.",
            parse_mode="Markdown",
        )
        return True

    add_item_to_list(found_list["id"], item)

    await update.message.reply_text(
        f"✅ Agregado a *{found_list['name']}*:\n📌 {item}",
        parse_mode="Markdown",
    )
    return True



async def handle_items_for_current_list(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    list_id = context.user_data.get("adding_items_to_list_id")
    list_name = context.user_data.get("adding_items_to_list_name")

    if not list_id:
        return False

    text_lower = text.strip().lower()

    if text_lower in ["listo", "terminar", "finalizar", "ya", "hecho"]:
        context.user_data.pop("adding_items_to_list_id", None)
        context.user_data.pop("adding_items_to_list_name", None)
        await update.message.reply_text(
            f"✅ Perfecto. Terminé de agregar items a *{list_name}*.\n\n"
            "Escribe *muéstrame mis listas* para verla.",
            parse_mode="Markdown",
        )
        return True

    items = split_items(text)

    if not items:
        await update.message.reply_text(
            "❌ No pude detectar items. Intenta: *leche pan papel* o *leche, pan, papel*",
            parse_mode="Markdown",
        )
        return True

    for item in items:
        add_item_to_list(list_id, item)

    items_text = "\n".join([f"• {item}" for item in items])

    await update.message.reply_text(
        f"✅ Agregado a *{list_name}*:\n{items_text}\n\n"
        "Puedes seguir enviando items o escribir *listo*.",
        parse_mode="Markdown",
    )
    return True


async def show_lists(update: Update, chat_id: int):
    lists = get_lists(chat_id)

    if not lists:
        await update.message.reply_text(
            "📭 No tienes listas todavía.\n\n"
            "Para crear una, escribe:\n*crear una lista nueva*",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("📋 *Tus listas:*", parse_mode="Markdown")

    for user_list in lists:
        items = get_items(user_list["id"])

        if not items:
            items_text = "_Sin items todavía._"
        else:
            lines = []
            for item in items:
                status = "✅" if item["completed"] else "⬜"
                lines.append(f"{status} {item['item']}")
            items_text = "\n".join(lines)

        keyboard = [
            [
                InlineKeyboardButton("👁️ Ver/editar", callback_data=f"viewlist_{user_list['id']}"),
                InlineKeyboardButton("🗑️ Eliminar lista", callback_data=f"dellist_{user_list['id']}"),
            ]
        ]

        await update.message.reply_text(
            f"🗂️ *{user_list['name']}*\n\n{items_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_single_list(query, list_id: int):
    user_list = get_list_by_id(list_id)

    if not user_list:
        await query.edit_message_text("❌ Esa lista ya no existe.")
        return

    items = get_items(list_id)

    if not items:
        text = (
            f"🗂️ *{user_list['name']}*\n\n"
            "_Sin items todavía._\n\n"
            f"Para agregar algo escribe:\n"
            f"*agregar leche a la lista de {user_list['name']}*"
        )
        keyboard = [
            [InlineKeyboardButton("🗑️ Eliminar lista", callback_data=f"dellist_{list_id}")]
        ]
    else:
        lines = [f"🗂️ *{user_list['name']}*\n"]
        keyboard = []

        for item in items:
            status = "✅" if item["completed"] else "⬜"
            lines.append(f"{status} {item['item']}")
            keyboard.append(
                [
                    InlineKeyboardButton(f"{status} {item['item'][:20]}", callback_data=f"toggleitem_{item['id']}_{list_id}"),
                    InlineKeyboardButton("🗑️", callback_data=f"delitem_{item['id']}_{list_id}"),
                ]
            )

        keyboard.append([InlineKeyboardButton("🗑️ Eliminar lista", callback_data=f"dellist_{list_id}")])
        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name if user and user.first_name else ""

    await update.message.reply_text(
        f"👋 ¡Hola {first_name}! Soy PingMyMind.\n\n"
        "Puedo ayudarte con:\n\n"
        "🔔 *Recordatorios*\n"
        "• Recuérdame en 10 minutos tomar agua\n"
        "• Recuérdame mañana a las 9 llamar al doctor\n\n"
        "📋 *Listas*\n"
        "• crear una lista nueva\n"
        "• agregar leche a la lista de supermercado\n"
        "• muéstrame mis listas\n\n"
        "También puedes mandarme notas de voz.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if context.user_data.get("waiting_for_list_name"):
        await handle_new_list_name(update, context)
        return

    if await handle_items_for_current_list(update, context, text):
        return

    if is_create_list_request(text):
        await ask_list_name(update, context)
        return

    if await handle_add_item(update, text):
        return

    if is_lists_request(text):
        await show_lists(update, chat_id)
        return

    if is_reminder_list_request(text):
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

        if context.user_data.get("waiting_for_list_name"):
            await create_list_from_name(update, context, text)
            return

        if await handle_items_for_current_list(update, context, text):
            return

        if is_create_list_request(text):
            await ask_list_name(update, context)
            return

        if await handle_add_item(update, text):
            return

        if is_lists_request(text):
            await show_lists(update, chat_id)
            return

        if is_reminder_list_request(text):
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

    if data.startswith("done_"):
        reminder_id = int(data.split("_")[1])
        reminder = get_reminder(reminder_id)
        remove_reminder_jobs(reminder_id)

        if reminder and reminder.get("repeat") == "daily" and reminder.get("active", 1):
            tz = pytz.timezone(TIMEZONE)
            current_time = datetime.fromisoformat(reminder["time"])
            if current_time.tzinfo is None:
                current_time = tz.localize(current_time)

            next_daily = get_next_daily_time(current_time)
            now = datetime.now(tz)

            while next_daily <= now:
                next_daily = get_next_daily_time(next_daily)

            update_reminder_time(reminder_id, next_daily.isoformat())
            schedule_reminder_job(
                context.application,
                reminder["chat_id"],
                reminder_id,
                reminder["task"],
                next_daily,
            )

            await query.edit_message_text(
                f"✅ ¡Recordatorio completado por hoy!\n\n"
                f"🔁 Te volveré a recordar mañana."
            )
        else:
            mark_done(reminder_id)
            await query.edit_message_text("✅ ¡Recordatorio completado!")

        return

    if data.startswith("delrem_"):
        reminder_id = int(data.split("_")[1])
        deactivate_reminder(reminder_id)
        remove_reminder_jobs(reminder_id)
        await query.edit_message_text("🗑️ Recordatorio eliminado.")
        return

    if data.startswith("viewlist_"):
        list_id = int(data.split("_")[1])
        await show_single_list(query, list_id)
        return

    if data.startswith("dellist_"):
        list_id = int(data.split("_")[1])
        delete_list(list_id)
        await query.edit_message_text("🗑️ Lista eliminada.")
        return

    if data.startswith("toggleitem_"):
        parts = data.split("_")
        item_id = int(parts[1])
        list_id = int(parts[2])
        toggle_item(item_id)
        await show_single_list(query, list_id)
        return

    if data.startswith("delitem_"):
        parts = data.split("_")
        item_id = int(parts[1])
        list_id = int(parts[2])
        delete_item(item_id)
        await show_single_list(query, list_id)
        return


def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("listas", lambda update, context: show_lists(update, update.effective_chat.id)))
    app.add_handler(CommandHandler("lista", lambda update, context: show_lists(update, update.effective_chat.id)))
    app.add_handler(CommandHandler("recordatorios", lambda update, context: show_reminders(update, update.effective_chat.id)))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    scheduler.start()
    restore_pending_reminders(app)

    logger.info("✅ Bot iniciado correctamente")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
