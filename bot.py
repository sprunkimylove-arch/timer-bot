import os
import asyncio
import json
import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import List

# ===== мини-HTTP сервер, чтобы хостинг видел открытый порт =====
import threading
from flask import Flask

http_app = Flask(__name__)

@http_app.get("/")
def health():
    return "ok"  # healthcheck

def run_http():
    # Koyeb/другие PaaS передают порт через переменную PORT
    port = int(os.getenv("PORT", "8080"))
    # запускаем без debug, в отдельном потоке
    http_app.run(host="0.0.0.0", port=port)
# ===============================================================

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, User
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ФИКС ДЛЯ PYTHON 3.14 (event loop) ----------
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# -------------------------------------------------------

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("8292909315:AAFmaZJr1CNWZhT91JSQgNKLaE0LbMXyKPg")
# BOT_TOKEN = "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА"   # если тестируешь локально, раскомментируй

PIN_TIMER = True      # пинить таймер (боту нужны права на закрепление)
SILENT_PIN = True     # пинить без уведомления
SUBS_FILE = Path("subs.json")  # файл с подписчиками /notifyme

# ====== ПОДПИСКИ (оповещение «всех») ======
# структура: { str(chat_id): set(user_id) }
_SUBS: dict[str, set[int]] = defaultdict(set)

def _load_subs():
    global _SUBS
    if SUBS_FILE.exists():
        try:
            data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            _SUBS = defaultdict(set, {k: set(v) for k, v in data.items()})
        except Exception as e:
            logging.warning("subs.json не прочитан: %s", e)

def _save_subs():
    try:
        SUBS_FILE.write_text(
            json.dumps({k: list(v) for k, v in _SUBS.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logging.warning("subs.json не сохранён: %s", e)

def _chat_key(chat_id: int) -> str:
    return str(chat_id)

def subscribe_user(chat_id: int, user_id: int) -> int:
    key = _chat_key(chat_id)
    _SUBS[key].add(user_id)
    _save_subs()
    return len(_SUBS[key])

def unsubscribe_user(chat_id: int, user_id: int) -> int:
    key = _chat_key(chat_id)
    _SUBS[key].discard(user_id)
    _save_subs()
    return len(_SUBS[key])

def get_subscribers(chat_id: int) -> List[int]:
    return list(_SUBS.get(_chat_key(chat_id), set()))

# ====== UI ======
def duration_keypad() -> InlineKeyboardMarkup:
    # 10 и 20 — в одной строке; 30 — отдельной строкой (широкой)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕐 10 мин", callback_data="timer_10"),
         InlineKeyboardButton("🕒 20 мин", callback_data="timer_20")],
        [InlineKeyboardButton("⏱️ 30 мин", callback_data="timer_30")],
    ])

def countdown_keypad() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Стоп таймера", callback_data="timer_stop")]])

def mention(u: User) -> str:
    return f"@{u.username}" if u.username \
        else f"<a href=\"tg://user?id={u.id}\">{u.first_name}</a>"

def mention_id(uid: int, name: str = "пользователь") -> str:
    return f"<a href=\"tg://user?id={uid}\">{name}</a>"

# ====== Состояние чата (один таймер на чат) ======
def job_name(chat_id: int) -> str:
    return f"timer_{chat_id}"

def has_active_timer(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.chat_data.get("active_timer"))

def set_active_timer(context: ContextTypes.DEFAULT_TYPE, owner_id: int, message_id: int):
    context.chat_data["active_timer"] = {"owner_id": owner_id, "message_id": message_id}

def clear_active_timer(context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("active_timer", None)

# ====== Команды ======
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Выбери длительность. В конце пингую того, кто запустил❗️",
        reply_markup=duration_keypad()
    )

async def notifyme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    n = subscribe_user(chat_id, user.id)
    await update.effective_message.reply_text(f"🔔 Подписал тебя на оповещения. Подписчиков: {n}")

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    n = unsubscribe_user(chat_id, user.id)
    await update.effective_message.reply_text(f"🔕 Отключил тебе оповещения. Подписчиков осталось: {n}")

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    act = context.chat_data.get("active_timer")
    if not act:
        await update.message.reply_text("Сейчас активного таймера нет.")
        return
    if act["owner_id"] != user_id:
        await update.message.reply_text("Остановить может только тот, кто запускал таймер.")
        return

    jq = context.application.job_queue
    if jq:
        for j in jq.get_jobs_by_name(job_name(chat_id)):
            j.schedule_removal()

    # анпин
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=act["message_id"])
    except Exception as e:
        logging.info("Не удалось анпинить при /cancel: %s", e)

    clear_active_timer(context)
    await update.message.reply_text("🛑 Твой таймер остановлен.")

# ====== Тик таймера ======
async def timer_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    data["remaining"] -= 1
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    owner = data["user"]

    if data["remaining"] > 0:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"⏳ Осталось: <b>{data['remaining']}</b> мин.",
                parse_mode=ParseMode.HTML,
                reply_markup=countdown_keypad()
            )
        except Exception as e:
            logging.warning("Не удалось отредактировать сообщение таймера: %s", e)
            job.schedule_removal()
            clear_active_timer(context)
    else:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⏰ Время вышло!")
        except Exception:
            pass
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logging.info("Не удалось анпинить по окончании: %s", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Время вышло, {mention(owner)}!",
            parse_mode=ParseMode.HTML
        )
        job.schedule_removal()
        clear_active_timer(context)

# ====== Старт через кнопки ======
async def pick_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user = query.from_user

    # запрет параллельных таймеров
    act = context.chat_data.get("active_timer")
    if act and act["owner_id"] != user.id:
        await query.answer("Уже идёт таймер другого пользователя. Дождись окончания.", show_alert=True)
        return

    m = {"timer_10": 10, "timer_20": 20, "timer_30": 30}
    duration = m.get(query.data, 10)

    # удалим предыдущие job'ы (если мой же)
    jq = context.application.job_queue
    if jq:
        for j in jq.get_jobs_by_name(job_name(chat_id)):
            j.schedule_removal()

    # сообщение таймера
    first_text = f"⏳ Осталось: <b>{duration}</b> мин."
    try:
        await query.edit_message_text(first_text, parse_mode=ParseMode.HTML, reply_markup=countdown_keypad())
        msg_id = query.message.message_id
    except Exception:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=first_text, parse_mode=ParseMode.HTML, reply_markup=countdown_keypad()
        )
        msg_id = sent.message_id

    # пин
    if PIN_TIMER:
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg_id, disable_notification=SILENT_PIN)
        except Exception as e:
            logging.info("Не смог закрепить (нет прав?): %s", e)

    # запомним активный таймер
    set_active_timer(context, owner_id=user.id, message_id=msg_id)

    # уведомим подписчиков
    subs = get_subscribers(chat_id)
    if subs:
        CHUNK = 15
        for i in range(0, len(subs), CHUNK):
            part = subs[i:i+CHUNK]
            text = f"🔔 Таймер запущен {mention(user)} — оповещаю подписанных:\n" + \
                   " ".join(mention_id(uid) for uid in part)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    # планируем тики
    if not jq:
        raise RuntimeError('JobQueue не инициализирован. Установи: pip install "python-telegram-bot[job-queue]==21.6"')

    jq.run_repeating(
        timer_tick,
        interval=timedelta(minutes=1),
        first=timedelta(minutes=1),
        name=job_name(chat_id),
        data={"remaining": duration, "chat_id": chat_id, "message_id": msg_id, "user": user},
        chat_id=chat_id,
        user_id=user.id
    )

# ====== Стоп кнопкой ======
async def stop_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    msg_id = query.message.message_id
    clicker = query.from_user

    act = context.chat_data.get("active_timer")
    if not act or act["message_id"] != msg_id:
        await query.answer("Таймер уже завершён или не найден.", show_alert=True)
        return
    if act["owner_id"] != clicker.id:
        await query.answer("Остановить может только тот, кто запускал таймер.", show_alert=True)
        return

    jq = context.application.job_queue
    if jq:
        for j in jq.get_jobs_by_name(job_name(chat_id)):
            j.schedule_removal()

    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logging.info("Не удалось анпинить при стопе: %s", e)

    try:
        await query.edit_message_text("🛑 Таймер остановлен.")
    except Exception:
        pass

    clear_active_timer(context)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Выплату окончил! {mention(clicker)}",
        parse_mode=ParseMode.HTML
    )
    await query.answer("Таймер остановлен.")

# ====== Чистим сервисные «pinned message» ======
async def delete_pin_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.effective_message.delete()
    except Exception as e:
        logging.info("Не смог удалить сервисное pinned-сообщение: %s", e)

# ====== MAIN ======
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана. "
                           "Либо задайте её, либо вставьте токен строкой в коде.")

    # поднимаем мини-HTTP сервер в фоне (для healthcheck на хостинге)
    threading.Thread(target=run_http, daemon=True).start()

    _load_subs()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("notifyme", notifyme_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(pick_timer, pattern=r"^timer_(10|20|30)$"))
    app.add_handler(CallbackQueryHandler(stop_button, pattern=r"^timer_stop$"))

    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, delete_pin_service))

    print("✅ Бот запущен. Жду апдейтов...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
