# bot.py
from __future__ import annotations

import os
import time
import threading
import asyncio
import logging
from typing import Optional

from flask import Flask, request, abort
from telegram import Update, Bot
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# ---------- Логи ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- Переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # напр.: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- Flask (WSGI) ----------
app_flask = Flask(__name__)
app_tg: Optional[Application] = None


# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я живу на Render и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ---------- Webhook endpoints ----------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json(force=True, silent=True)
    if not data:
        log.warning("Empty JSON in webhook")
        return "ok", 200

    if not app_tg:
        # крайне короткое «окно», когда PTB ещё не поднялся;
        # отвечаем 200 — Telegram сам ретрайнет
        log.error("app_tg is not initialized yet")
        return "ok", 200

    try:
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue update")

    return "ok", 200


# ---------- Фоновый запуск PTB ----------
async def _set_webhook_once():
    """Ставит webhook через Bot API (вне PTB цикла)."""
    bot = Bot(BOT_TOKEN)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    finally:
        # закрываем httpx-сессию
        await bot.session.close()

def _set_webhook_later():
    # ждём пару секунд, чтобы run_webhook успел начать слушать порт
    time.sleep(2)
    try:
        asyncio.run(_set_webhook_once())
    except Exception:
        log.exception("Failed to set webhook")

def _run_bot() -> None:
    global app_tg
    try:
        log.info("Starting PTB application…")
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        app_tg = application

        # Запускаем отложенную установку вебхука параллельно
        threading.Thread(target=_set_webhook_later, daemon=True).start()

        # В PTB 21.x у run_webhook нет on_startup — просто поднимаем приёмник
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            url_path=WEBHOOK_PATH,
            # webhook_url и secret_token тут не указываем — мы их ставим отдельно
        )
    except Exception:
        log.exception("PTB application crashed")

# Стартуем PTB в отдельном потоке, когда gunicorn импортирует модуль
threading.Thread(target=_run_bot, name="ptb-runner", daemon=True).start()

# Стартуем бот при импорте модуля (когда gunicorn поднимает app_flask)
threading.Thread(target=_run_bot, name="ptb-runner", daemon=True).start()
