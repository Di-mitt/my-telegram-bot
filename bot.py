# bot.py
from __future__ import annotations

import os
import asyncio
import logging
import threading
from typing import Optional

from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters,
)

# ---------- логирование ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- конфиг из окружения ----------
BOT_TOKEN = "8407972541:AAEXRIny7RMduH-sE8j5ooTMapqt2eMByd8"
APP_URL = os.getenv("APP_URL")  # пример: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = "mySecret_2025"

if not APP_URL:
    raise RuntimeError("Set env var APP_URL on Render")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- Flask-приложение, которое видит gunicorn ----------
app_flask = Flask(__name__)

# Telegram Application и признак готовности
app_tg: Optional[Application] = None
_bot_ready = threading.Event()


# ---------- handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ---------- служебные маршруты ----------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    """Приём апдейтов от Telegram через Flask."""
    # Дожидаемся запуска бота (до 5 секунд), чтобы не словить "not initialized"
    if not _bot_ready.wait(timeout=5):
        log.error("Webhook got request, but bot is not ready yet")
        return "ok", 200  # отвечаем 200, чтобы TG не считал это 500

    # Проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
        if not data:
            return "ok", 200

        assert app_tg is not None
        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
        return "ok", 200
    except Exception:
        log.exception("Error in webhook_handler")
        return "ok", 200


# ---------- запуск Telegram-приложения в фоне ----------
async def _async_start_bot():
    """Асинхронная инициализация и запуск PTB без собственного веб-сервера."""
    global app_tg

    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Инициализируем и запускаем обработчики очереди
    await app_tg.initialize()
    await app_tg.start()

    # Ставим вебхук (сначала удалим старый)
    await app_tg.bot.delete_webhook(drop_pending_updates=True)
    await app_tg.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set to %s", WEBHOOK_URL)

    _bot_ready.set()

    # Держим задачу живой (PTB сам слушает update_queue)
    await asyncio.Event().wait()


def _start_bot_in_thread():
    asyncio.run(_async_start_bot())


# Стартуем фонового обработчика при импорте модуля (когда gunicorn его грузит)
threading.Thread(target=_start_bot_in_thread, daemon=True).start()
