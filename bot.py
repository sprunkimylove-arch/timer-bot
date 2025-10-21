import os
import asyncio
import logging
import threading
from datetime import datetime, timedelta

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("timer-bot")

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # в Replit добавим в Secrets
if not BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения BOT_TOKEN не задана. "
        "Задай её в Replit ➜ Secrets (ключ BOT_TOKEN)."
    )

# Таймеры по чатам: chat_id -> dict(...)
RUNNING = {}  # { chat_id: {"owner_id": int, "owner_name": str, "until": datetime, "msg_id": int, "pin_id": int} }

# ---------- KEEP-ALIVE ДЛЯ REPLIT ----------
# Replit может засыпать. Этот микросервер можно будить пингами (например, UptimeRobot).
app = Flask(__name__)

@app.get("/")
def health():
    return "ok"

def run_keepalive():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_keepalive, daemon=True).start()

# ---------- ХЕЛПЕРЫ ----------
def fmt_remaining(until: datetime) -> str:
    now = datetime.utcnow()
    if until <= now:
        return "00:00"
    left = until - now
    m, s = divmod(int(left.total_seconds()), 60)
    return f"{m:02d}:{s:02d}"

def timer_active(chat_id: int) -> bool:
    info = RUNNING.get(chat_id)
    if not info:
        return False
    return info["until"] > datetime.utcnow()

def clear_timer(chat_id: int):
    RUNNING.pop(chat_id, None)

def main_keyboard() -> InlineKeyboardMarkup:
    # ряд 1: 10 и 20 минут, ряд 2: одна широкая 30
    kb = [
        [
            InlineKeyboardButton("🕒 10 минут", callback_data="start_10"),
            InlineKeyboardButton("🕓 20 минут", callback_data="start_20"),
        ],
        [
            InlineKeyboardButton("🕕 30 минут", callback_data="start_30"),
        ],
        [
            InlineKeyboardButton("⛔ Стоп", callback_data="stop"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

async def send_or_edit_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, msg_id: int | None) -> int:
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, parse_mode=ParseMode.HTML
            )
            return msg_id
        except Exception:
            pass
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    return msg.message_id

# ---------- JOBS ----------
async def tick(context: ContextTypes.DEFAULT_TYPE):
    """Обновление сообщения каждую минуту."""
    chat_id = context.job.chat_id
    info = RUNNING.get(chat_id)
    if not info:
        return

    if datetime.utcnow() >= info["until"]:
        # Завершение
        user_mention = info["owner_name"]
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ <b>Выплату окончил!</b>\n{user_mention}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning("send finish msg: %s", e)

        # Снимаем пин
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=info["pin_id"])
        except Exception:
            # иногда пин уже снят
            pass

        clear_timer(chat_id)
        return

    # Обновить текст таймера
    remain = fmt_remaining(info["until"])
    text = f"⏱ <b>Таймер</b>\nОсталось: <b>{remain}</b>\nЗапустил: {info['owner_name']}\n\nВыбери длительность. В конце пингую того, кто запустил❗️"
    try:
        new_id = await send_or_edit_timer(context, chat_id, text, info["msg_id"])
        info["msg_id"] = new_id
    except Exception as e:
        log.warning("tick edit: %s", e)

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Привет! Я таймер-бот.\n\nВыбери длительность. В конце пингую того, кто запустил❗️",
        reply_markup=main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    user = query.from_user
    mention = user.mention_html()

    data = query.data or ""

    if data == "stop":
        info = RUNNING.get(chat_id)
        if not info:
            await query.edit_message_reply_markup(reply_markup=main_keyboard())
            await context.bot.send_message(chat_id, "Таймер не запущен.")
            return

        # Только владелец или админ группы (если надо — можно расширить) — но для простоты только владелец:
        if info["owner_id"] != user.id:
            await context.bot.send_message(chat_id, "Остановить может только тот, кто запускал.")
            return

        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=info["pin_id"])
        except Exception:
            pass

        clear_timer(chat_id)
        await context.bot.send_message(chat_id, f"✅ <b>Выплату окончил!</b>\n{mention}", parse_mode=ParseMode.HTML)
        return

    # Старт
    if timer_active(chat_id):
        # уже идёт — не даём второй
        await context.bot.send_message(chat_id, "⛔ Уже идёт таймер. Сначала останови текущий.")
        return

    minutes = 0
    if data == "start_10":
        minutes = 10
    elif data == "start_20":
        minutes = 20
    elif data == "start_30":
        minutes = 30

    if minutes <= 0:
        return

    until = datetime.utcnow() + timedelta(minutes=minutes)

    # первичное сообщение таймера
    remain = fmt_remaining(until)
    text = f"⏱ <b>Таймер</b>\nОсталось: <b>{remain}</b>\nЗапустил: {mention}\n\nВыбери длительность. В конце пингую того, кто запустил❗️"
    timer_msg = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # пин без сервисного уведомления
    pin_id = timer_msg.message_id
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=pin_id, disable_notification=True)
    except Exception as e:
        log.warning("pin failed: %s", e)

    RUNNING[chat_id] = {
        "owner_id": user.id,
        "owner_name": mention,
        "until": until,
        "msg_id": timer_msg.message_id,
        "pin_id": pin_id,
    }

    # поставить job — обновление каждую минуту
    context.job_queue.run_repeating(
        tick,
        interval=60,
        first=60,
        chat_id=chat_id,
        name=f"timer_{chat_id}",
    )

    # кнопки под стартовым сообщением
    try:
        await timer_msg.edit_reply_markup(reply_markup=main_keyboard())
    except Exception:
        pass

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — меню с кнопками\n"
        "Кнопками запускаешь на 10/20/30 минут.\n"
        "⛔ Стоп — останавливает текущий таймер (может только тот, кто запускал)."
    )

# ---------- MAIN ----------
async def main():
    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(handle_buttons))

    log.info("Bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=None)  # polling
    await application.updater.wait()
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
