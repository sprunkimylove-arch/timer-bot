import os
import asyncio
import logging
import threading
from datetime import timedelta

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("timer-bot")

# ----------------- FLASK HEALTHCHECK -----------------
# НУЖНО для Render: Gunicorn поднимет этот app, чтобы был открытый порт
app = Flask(__name__)

@app.get("/")
def health():
    return "OK"

# ----------------- НАСТРОЙКИ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # т.к. на хостинге задаём через Envs
if not BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения BOT_TOKEN не задана. "
        "Задай её в Render → Environment → Add Environment Variable."
    )

# Кнопки 2 сверху + 1 широкой снизу
def main_menu():
    kb = [
        [
            InlineKeyboardButton("⏱ 10 минут", callback_data="start_10"),
            InlineKeyboardButton("⏱ 20 минут", callback_data="start_20"),
        ],
        [InlineKeyboardButton("⏱ 30 минут", callback_data="start_30")],
        [InlineKeyboardButton("🛑 Стоп", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(kb)

# Имя джоба по чату и юзеру
def job_name(chat_id: int) -> str:
    return f"chat_{chat_id}"

# Сохранение id закреплённого сообщения, чтобы потом снять пин
PIN_KEY = "pinned_msg_id"
# Кто запустил таймер
STARTER_KEY = "starter_id"
# id сообщения с таймером, которое мы правим
TIMER_MSG_KEY = "timer_msg_id"
# осталось минут
LEFT_MIN_KEY = "left_min"

# ----------------- ХЕНДЛЕРЫ -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Выбери длительность. В конце пингую того, кто запустил❗️"
    await update.message.reply_text(text, reply_markup=main_menu())

async def stop_active_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, send_final: bool):
    """Снять все регистрационные данные, остановить job, распинить, почистить"""
    jq = context.application.job_queue
    # стопим задачу
    for j in list(jq.get_jobs_by_name(job_name(chat_id)) or []):
        j.schedule_removal()

    # распинить
    data = context.chat_data
    pinned_id = data.get(PIN_KEY)
    if pinned_id:
        try:
            await context.bot.unpin_chat_message(chat_id, pinned_id)
        except Exception:
            pass
        data[PIN_KEY] = None

    # финальная надпись (опционально)
    if send_final:
        starter_id = data.get(STARTER_KEY)
        if starter_id:
            await context.bot.send_message(
                chat_id,
                f"Выплату окончил! <a href='tg://user?id={starter_id}'>‎</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    # чистим метки
    for k in (STARTER_KEY, TIMER_MSG_KEY, LEFT_MIN_KEY):
        context.chat_data[k] = None

async def pick_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие на любые кнопки"""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    data = query.data

    # нажатие "Стоп"
    if data == "stop":
        await stop_active_timer(context, chat_id, send_final=True)
        # обновить кнопки
        await query.edit_message_text(
            "Выбери длительность. В конце пингую того, кто запустил❗️",
            reply_markup=main_menu(),
        )
        return

    # Проверяем, есть ли уже активный таймер
    active = context.application.job_queue.get_jobs_by_name(job_name(chat_id))
    if active:
        await query.answer("Уже тикает таймер в этом чате. Сначала нажми «Стоп».", show_alert=True)
        return

    # Сколько минут
    minutes_map = {"start_10": 10, "start_20": 20, "start_30": 30}
    minutes = minutes_map.get(data, 0)
    if minutes == 0:
        return

    # кто запустил
    starter = query.from_user
    context.chat_data[STARTER_KEY] = starter.id
    context.chat_data[LEFT_MIN_KEY] = minutes

    # Создаём/обновляем сообщение таймера
    text = f"⏳ Осталось: <b>{minutes} мин</b>\nЗапустил: <a href='tg://user?id={starter.id}'>‎</a>"
    if context.chat_data.get(TIMER_MSG_KEY):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
            timer_msg_id = query.message.message_id
        except Exception:
            # если не получилось отредактировать — отправим новое
            m = await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
            timer_msg_id = m.message_id
    else:
        m = await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
        timer_msg_id = m.message_id

    context.chat_data[TIMER_MSG_KEY] = timer_msg_id

    # Пин без уведомлений
    try:
        res = await context.bot.pin_chat_message(chat_id, timer_msg_id, disable_notification=True)
        # pinChatMessage возвращает True. pinned_id у нас наш timer_msg_id
        context.chat_data[PIN_KEY] = timer_msg_id
    except Exception:
        context.chat_data[PIN_KEY] = None

    # старт job — раз в минуту обновляем
    context.application.job_queue.run_repeating(
        callback=every_minute_tick,
        interval=60,
        first=60,
        name=job_name(chat_id),
        data={"chat_id": chat_id},
    )

async def every_minute_tick(context: ContextTypes.DEFAULT_TYPE):
    """Ежеминутный тикер: уменьшаем минуты, обновляем сообщение, финиш"""
    chat_id = context.job.data["chat_id"]

    left = context.chat_data.get(LEFT_MIN_KEY) or 0
    left = max(0, left - 1)
    context.chat_data[LEFT_MIN_KEY] = left

    # Обновим сообщение
    timer_msg_id = context.chat_data.get(TIMER_MSG_KEY)
    starter_id = context.chat_data.get(STARTER_KEY)
    try:
        if timer_msg_id:
            if left > 0:
                txt = f"⏳ Осталось: <b>{left} мин</b>\nЗапустил: <a href='tg://user?id={starter_id}'>‎</a>"
                await context.bot.edit_message_text(
                    txt, chat_id=chat_id, message_id=timer_msg_id,
                    parse_mode=ParseMode.HTML, reply_markup=main_menu()
                )
            else:
                # Финиш: распин, финальное сообщение, остановка джоба
                await stop_active_timer(context, chat_id, send_final=True)
    except Exception as e:
        log.warning("Edit/pin/unpin failed: %s", e)

# ----------------- ТЕЛЕГРАМ-БОТ -----------------
async def tg_main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .arbitrary_callback_data(True)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(pick_timer))

    # Запуск long-polling
    log.info("Starting Telegram polling…")
    await application.initialize()
    await application.start()
    try:
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Держим фоново бесконечно
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

def _run_tg_in_thread():
    """Запускаем Telegram-петлю в отдельном потоке,
    чтобы Gunicorn держал HTTP-приложение (Flask), а бот работал параллельно."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(tg_main())

# Стартуем поток сразу при импорте модуля (важно для gunicorn)
threading.Thread(target=_run_tg_in_thread, daemon=True).start()

# ----------------- ЛОКАЛЬНЫЙ СТАРТ -----------------
if __name__ == "__main__":
    # Для локального теста:
    # 1) python bot.py — запустит Flask на 8080 и бота в фоне
    # 2) В боте отправь /start
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
