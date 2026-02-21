# ==========================================
# HABIT TRACKER TELEGRAM BOT
# python-telegram-bot v20+
# ==========================================

import os
import sqlite3
import logging
from datetime import datetime, date, time, timedelta
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# ==========================================
# ENV
# ==========================================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE
# ==========================================

DB = "habits.db"


def db():
    return sqlite3.connect(DB)


def init_db():
    conn = db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS activities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        description TEXT,
        period INTEGER DEFAULT 1,
        reminder_time TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activity_id INTEGER,
        log_date TEXT
    )
    """)

    # Проверка и добавление недостающих колонок (без вывода в консоль)
    c.execute("PRAGMA table_info(activities)")
    columns = [column[1] for column in c.fetchall()]

    if 'period' not in columns:
        c.execute("ALTER TABLE activities ADD COLUMN period INTEGER DEFAULT 1")
        c.execute("UPDATE activities SET period = 1 WHERE period IS NULL")

    if 'reminder_time' not in columns:
        c.execute("ALTER TABLE activities ADD COLUMN reminder_time TEXT")

    if 'reminders_per_day' in columns:
        c.execute("""
        CREATE TABLE activities_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            description TEXT,
            period INTEGER DEFAULT 1,
            reminder_time TEXT
        )
        """)
        c.execute("""
        INSERT INTO activities_new(id, user_id, name, description, period, reminder_time)
        SELECT id, user_id, name, description, period, reminder_time FROM activities
        """)
        c.execute("DROP TABLE activities")
        c.execute("ALTER TABLE activities_new RENAME TO activities")

    conn.commit()
    conn.close()


# ==========================================
# HELPERS
# ==========================================

def get_user_id(tg_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (tg_id,))
    conn.commit()
    c.execute("SELECT id FROM users WHERE telegram_id=?", (tg_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def get_activities(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM activities WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_activity_by_id(activity_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM activities WHERE id=?", (activity_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_last_log_date(activity_id):
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT log_date FROM logs WHERE activity_id=? ORDER BY log_date DESC LIMIT 1",
        (activity_id,)
    )
    row = c.fetchone()
    conn.close()
    return date.fromisoformat(row[0]) if row else None


def add_log(activity_id):
    conn = db()
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute(
        "SELECT * FROM logs WHERE activity_id=? AND log_date=?",
        (activity_id, today)
    )
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO logs(activity_id, log_date) VALUES(?,?)",
            (activity_id, today)
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


# ==========================================
# STREAK LOGIC
# ==========================================

def calculate_streak(activity_id, period):
    if period is None:
        period = 1

    logs = []
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT log_date FROM logs WHERE activity_id=? ORDER BY log_date DESC",
        (activity_id,)
    )
    rows = c.fetchall()
    conn.close()

    for x in rows:
        try:
            logs.append(date.fromisoformat(x[0]))
        except:
            continue

    if not logs:
        return 0, 0, None, "нет данных"

    # Текущая серия
    current = 1
    for i in range(len(logs) - 1):
        diff = (logs[i] - logs[i + 1]).days
        if diff <= period:
            current += 1
        else:
            break

    # Максимальная серия
    max_streak = 1
    temp = 1
    for i in range(len(logs) - 1):
        diff = (logs[i] - logs[i + 1]).days
        if diff <= period:
            temp += 1
            max_streak = max(max_streak, temp)
        else:
            temp = 1

    last = logs[0]
    days_since = (date.today() - last).days
    if days_since <= period:
        status = "✅ в норме"
    else:
        overdue = days_since - period
        status = f"❌ просрочено ({overdue} дн)"

    return current, max_streak, last, status


# ==========================================
# REMINDER SYSTEM (ПЕРЕРАБОТАНО)
# ==========================================

def remove_reminder_jobs(activity_id, context):
    """Удаляет все запланированные напоминания для активности."""
    job_name = f"reminder_{activity_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()


def schedule_next_reminder(activity_id, context):
    """
    Планирует следующее напоминание для активности.
    Вызывается после отметки, при создании активности и при перезапуске бота.
    """
    activity = get_activity_by_id(activity_id)
    if not activity:
        return

    user_id, period, reminder_time_str = activity[1], activity[4], activity[5]
    if not reminder_time_str:
        return  # напоминание не настроено

    # Получаем telegram_id пользователя
    conn = db()
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    if not user:
        return
    chat_id = user[0]

    # Дата последней отметки (или None)
    last_date = get_last_log_date(activity_id)
    today = date.today()

    if last_date:
        next_date = last_date + timedelta(days=period)
    else:
        # Нет отметок – напомним через period дней от сегодня
        next_date = today + timedelta(days=period)

    # Если дата уже прошла – переносим на сегодня (напоминаем о просрочке)
    if next_date < today:
        next_date = today

    # Парсим время
    try:
        reminder_time = datetime.strptime(reminder_time_str, "%H:%M").time()
    except ValueError:
        return

    # Собираем datetime для запуска
    run_datetime = datetime.combine(next_date, reminder_time)
    now = datetime.now()

    # Если время уже прошло сегодня – переносим на завтра
    if run_datetime <= now:
        run_datetime += timedelta(days=1)

    # Удаляем старые задания и создаём новое
    remove_reminder_jobs(activity_id, context)
    context.job_queue.run_once(
        send_reminder,
        when=run_datetime,
        data=(chat_id, activity_id),
        name=f"reminder_{activity_id}"
    )
    logger.info(f"Напоминание для активности {activity_id} запланировано на {run_datetime}")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет одно напоминание."""
    job = context.job
    chat_id, activity_id = job.data
    activity = get_activity_by_id(activity_id)
    if not activity:
        return

    activity_name = activity[2]
    period = activity[4] if activity[4] is not None else 1
    cur, mx, last, status = calculate_streak(activity_id, period)

    message = (
        f"⏰ <b>Напоминание!</b>\n\n"
        f"🏷 Активность: <b>{activity_name}</b>\n"
        f"📅 Статус: {status}\n"
        f"🔥 Текущая серия: {cur} дней\n\n"
        f"Не забудьте отметить выполнение сегодня!\n"
        f"Используйте кнопку '✅ Отметить сегодня' в главном меню."
    )

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания: {e}")


def schedule_all_reminders(application):
    """Планирует напоминания для всех активностей при старте бота."""
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id FROM activities
        WHERE reminder_time IS NOT NULL AND reminder_time != ''
    """)
    activities = c.fetchall()
    conn.close()

    for (activity_id,) in activities:
        schedule_next_reminder(activity_id, application)


# ==========================================
# KEYBOARD
# ==========================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["✅ Отметить сегодня", "📋 Мои активности"],
            ["➕ Добавить активность", "🗑 Удалить"],
            ["🔄 Перезапуск", "❌ Отмена"]
        ],
        resize_keyboard=True
    )


# ==========================================
# START / RESET / CANCEL
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user_id(update.effective_user.id)
    await update.message.reply_text(
        "👋 Habit Tracker готов к работе\n\n"
        "📌 Используйте кнопки ниже для управления привычками\n"
        "⏰ Напоминания приходят ровно через N дней после последней отметки в заданное время!",
        reply_markup=main_keyboard()
    )


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 Перезапуск выполнен",
        reply_markup=main_keyboard()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Действие отменено",
        reply_markup=main_keyboard()
    )


# ==========================================
# ADD ACTIVITY (Conversation)
# ==========================================

NAME, DESC, PERIOD, REMINDER_TIME = range(4)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите название новой активности:",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await cancel(update, context)
        return ConversationHandler.END
    context.user_data["name"] = update.message.text
    await update.message.reply_text(
        "Введите описание активности (или '-' если описание не нужно):",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await cancel(update, context)
        return ConversationHandler.END
    context.user_data["desc"] = update.message.text
    await update.message.reply_text(
        "Введите период активности в днях (например, 1 для ежедневной, 7 для еженедельной):",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return PERIOD


async def add_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await cancel(update, context)
        return ConversationHandler.END
    try:
        period = int(update.message.text)
        if period <= 0:
            await update.message.reply_text("Период должен быть положительным числом. Попробуйте еще раз:")
            return PERIOD
        if period > 365:
            await update.message.reply_text("Период не должен превышать 365 дней. Попробуйте еще раз:")
            return PERIOD
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите целое число (например: 1, 7, 30):")
        return PERIOD

    context.user_data["period"] = period

    keyboard = ReplyKeyboardMarkup(
        [
            ["07:00", "08:00", "09:00"],
            ["12:00", "18:00", "20:00"],
            ["Пропустить", "❌ Отмена"]
        ],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "⏰ Введите время для ежедневного напоминания в формате ЧЧ:ММ (например, 09:30):\n\n"
        "Или выберите из предложенных вариантов:\n"
        "• 'Пропустить' - если не хотите настраивать напоминание\n\n"
        "📌 Напоминание будет приходить через указанное количество дней после последней отметки.",
        reply_markup=keyboard
    )
    return REMINDER_TIME


async def add_reminder_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await cancel(update, context)
        return ConversationHandler.END

    if update.message.text == "Пропустить":
        context.user_data["reminder_time"] = None
        return await save_activity(update, context)

    try:
        reminder_time = update.message.text
        datetime.strptime(reminder_time, "%H:%M")
        hours, minutes = map(int, reminder_time.split(":"))
        if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
            raise ValueError
        context.user_data["reminder_time"] = reminder_time
        return await save_activity(update, context)
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ (например, 09:30) "
            "или выберите 'Пропустить':"
        )
        return REMINDER_TIME


async def save_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_user_id(update.effective_user.id)
    name = context.user_data.get("name", "")
    desc = context.user_data.get("desc", "-")
    period = context.user_data.get("period", 1)
    reminder_time = context.user_data.get("reminder_time")

    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            """INSERT INTO activities(user_id,name,description,period,reminder_time) 
               VALUES(?,?,?,?,?)""",
            (uid, name, desc, period, reminder_time)
        )
        activity_id = c.lastrowid
        conn.commit()
        conn.close()

        # Если указано время – планируем первое напоминание
        if reminder_time:
            schedule_next_reminder(activity_id, context.application)

        message = f"✅ Активность '{name}' успешно добавлена!\n"
        message += f"📅 Период: {period} дней\n"
        if reminder_time:
            message += f"⏰ Напоминание: через {period} дн(ей) после отметки в {reminder_time}\n"
        else:
            message += "⏰ Напоминание: не настроено\n"
        message += "\nИспользуйте кнопку '✅ Отметить сегодня', чтобы отметить выполнение."

        await update.message.reply_text(message, reply_markup=main_keyboard())
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка сохранения активности: {e}")
        await update.message.reply_text(
            f"❌ Произошла ошибка при добавлении активности.",
            reply_markup=main_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END


# ==========================================
# LIST ACTIVITIES
# ==========================================

async def list_activities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_user_id(update.effective_user.id)
    activities = get_activities(uid)

    if not activities:
        await update.message.reply_text(
            "📭 У вас пока нет активностей.\n"
            "Добавьте первую активность с помощью кнопки '➕ Добавить активность'"
        )
        return

    text = "📋 Мои активности:\n\n"
    for a in activities:
        period = a[4] if a[4] is not None else 1
        cur, mx, last, status = calculate_streak(a[0], period)
        last_date = last.strftime("%d.%m.%Y") if last else "никогда"
        desc_text = f"📝 {a[3]}\n" if a[3] and a[3] != '-' else ""
        reminder_info = f"⏰ Напоминание: через {period} дн(ей) в {a[5]}\n" if a[5] else ""

        text += (
            f"🏷 <b>{a[2]}</b>\n"
            f"{desc_text}"
            f"📅 Период: каждые <b>{period}</b> дней\n"
            f"{reminder_info}"
            f"🔥 Текущая серия: <b>{cur}</b> дней (макс: {mx})\n"
            f"📊 Последняя отметка: {last_date}\n"
            f"📈 Статус: {status}\n"
            f"────────────────────\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


# ==========================================
# MARK TODAY
# ==========================================

async def mark_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_user_id(update.effective_user.id)
    activities = get_activities(uid)

    if not activities:
        await update.message.reply_text("У вас пока нет активностей для отметки.")
        return

    keyboard = []
    today_date = date.today()
    for a in activities:
        period = a[4] if a[4] is not None else 1
        cur, mx, last, status = calculate_streak(a[0], period)
        days_since = (today_date - last).days if last else 999
        button_text = f"{a[2]}"
        if days_since == 0:
            button_text = f"✅ {a[2]}"
        elif days_since > period:
            button_text = f"❌ {a[2]}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"log_{a[0]}")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])

    await update.message.reply_text(
        f"✅ Отметить выполнение на сегодня ({today_date.strftime('%d.%m.%Y')}):\n\n"
        "✅ - уже отмечено сегодня\n"
        "❌ - просрочено",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_to_main":
        await query.edit_message_text("Возвращаемся в главное меню...", reply_markup=None)
        await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return

    aid = int(query.data.split("_")[1])
    activity = get_activity_by_id(aid)
    if not activity:
        await query.edit_message_text("❌ Активность не найдена", reply_markup=None)
        return

    activity_name, period = activity[2], activity[4] or 1
    success = add_log(aid)

    if success:
        # Перепланируем напоминание с новой датой последней отметки
        schedule_next_reminder(aid, context.application)

        uid = get_user_id(query.from_user.id)
        activities = get_activities(uid)
        keyboard = []
        today_date = date.today()
        for a in activities:
            cur_period = a[4] if a[4] is not None else 1
            cur, mx, last, status = calculate_streak(a[0], cur_period)
            days_since = (today_date - last).days if last else 999
            button_text = f"{a[2]}"
            if days_since == 0:
                button_text = f"✅ {a[2]}"
            elif days_since > cur_period:
                button_text = f"❌ {a[2]}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"log_{a[0]}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])

        await query.edit_message_text(
            f"✅ Отметка для '{activity_name}' добавлена!\n"
            f"Период: {period} дней\n\n"
            f"Выберите следующую активность:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await query.answer("✅ Эта активность уже отмечена на сегодня!", show_alert=True)


# ==========================================
# DELETE ACTIVITY
# ==========================================

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_user_id(update.effective_user.id)
    activities = get_activities(uid)

    if not activities:
        await update.message.reply_text(
            "📭 У вас нет активностей для удаления.\n"
            "Добавьте активности с помощью кнопки '➕ Добавить активность'"
        )
        return

    keyboard = []
    for a in activities:
        period = a[4] if a[4] is not None else 1
        reminder_info = f" (⏰ {a[5]})" if a[5] else ""
        button_text = f"{a[2]} - каждые {period} дн{reminder_info}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"del_{a[0]}")])

    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="delete_back")])

    await update.message.reply_text(
        "🗑 <b>Удаление активности</b>\n\n"
        "Выберите активность для удаления:\n"
        "<i>Внимание: удалятся все связанные отметки и напоминания!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    aid = int(query.data.split("_")[1])

    try:
        conn = db()
        c = conn.cursor()
        c.execute("SELECT name, period, reminder_time FROM activities WHERE id=?", (aid,))
        result = c.fetchone()
        if not result:
            await query.edit_message_text("❌ Активность не найдена", reply_markup=None)
            return

        activity_name, period, reminder_time = result
        c.execute("SELECT COUNT(*) FROM logs WHERE activity_id=?", (aid,))
        logs_count = c.fetchone()[0]

        c.execute("DELETE FROM logs WHERE activity_id=?", (aid,))
        c.execute("DELETE FROM activities WHERE id=?", (aid,))
        conn.commit()
        conn.close()

        # Удаляем все напоминания для этой активности
        remove_reminder_jobs(aid, context.application)

        message = (
            f"🗑 <b>Активность удалена</b>\n\n"
            f"🏷 <b>Название:</b> {activity_name}\n"
            f"📅 <b>Период:</b> {period} дней\n"
        )
        if reminder_time:
            message += f"⏰ <b>Напоминание было настроено:</b> {reminder_time}\n"
        message += f"📊 <b>Удалено отметок:</b> {logs_count}\n\n"
        message += "<i>Активность и все связанные данные успешно удалены.</i>"

        await query.edit_message_text(message, parse_mode="HTML", reply_markup=None)
        await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())

    except Exception as e:
        logger.error(f"Ошибка удаления активности: {e}")
        await query.edit_message_text(
            "❌ <b>Ошибка при удалении</b>\n\n"
            "Повторите попытку позже.",
            parse_mode="HTML",
            reply_markup=None
        )


async def delete_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Удаление отменено", reply_markup=None)
    await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())


# ==========================================
# ERROR HANDLER (исправлен)
# ==========================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла внутренняя ошибка. Разработчик уже уведомлён, попробуйте позже.",
                reply_markup=main_keyboard()
            )
    except:
        pass


# ==========================================
# MAIN
# ==========================================

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ConversationHandler для добавления активности
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить активность$"), add_start)
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
            PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_period)],
            REMINDER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_time)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel)
        ],
    )

    # Обработчики команд и кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🔄 Перезапуск$"), restart))
    app.add_handler(MessageHandler(filters.Regex("^📋 Мои активности$"), list_activities))
    app.add_handler(MessageHandler(filters.Regex("^✅ Отметить сегодня$"), mark_today))
    app.add_handler(MessageHandler(filters.Regex("^🗑 Удалить$"), delete_menu))
    app.add_handler(conv_handler)

    # Callback-обработчики
    app.add_handler(CallbackQueryHandler(log_callback, pattern="^log_"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(log_callback, pattern="^back_to_main"))
    app.add_handler(CallbackQueryHandler(delete_back_callback, pattern="^delete_back"))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    # Планируем все напоминания перед запуском
    schedule_all_reminders(app)

    logger.info("Бот запущен...")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")


if __name__ == "__main__":
    main()