# bot.py
from __future__ import annotations

import os
import logging
from typing import Optional
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- настройка логов --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- переменные окружения --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask-приложение --------------------
app_flask = Flask(__name__)

# -------------------- Telegram Application --------------------
app_tg: Application = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .build()
)

# -------------------- handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# Регистрируем обработчики
app_tg.add_handler(CommandHandler("start", start_cmd))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

# -------------------- webhook lifecycle --------------------
async def on_startup(application: Application) -> None:
    log.info("Setting webhook to %s", WEBHOOK_URL)
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook is set")

# -------------------- Flask routes --------------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
        if not data:
            log.warning("Empty JSON in webhook")
            return "ok", 200

        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
        return "ok", 200
    except Exception:
        log.exception("Error in webhook_handler")
        return "ok", 200

# -------------------- Запуск Telegram App в фоне --------------------
import threading
def run_tg():
    app_tg.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        on_startup=[on_startup],
    )

threading.Thread(target=run_tg, daemon=True).start()
