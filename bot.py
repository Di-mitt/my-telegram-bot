# bot.py
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from flask import Flask, abort, request
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask --------------------
app_flask = Flask(__name__)

# Глобальные объекты PTB
app_tg: Optional[Application] = None
app_ready = threading.Event()  # флаг «бот готов принимать апдейты»

# -------------------- handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# -------------------- PTB runner (без run_webhook) --------------------
async def _ptb_main() -> None:
    """Запускаем PTB так, чтобы он принимал апдейты из очереди update_queue."""
    global app_tg

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg = application

    # регистрируем хендлеры
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # инициализация и старт без poll/webhook (мы сами кладём апдейты в очередь)
    await application.initialize()
    await application.start()

    # ставим вебхук для Telegram (пусть шлёт к нам на Flask)
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")

    # помечаем, что бот готов, и «спим», чтобы цикл жил
    app_ready.set()
    while True:
        await asyncio.sleep(3600)

def _ptb_thread_runner() -> None:
    """Запуск PTB в отдельном потоке с собственным event loop."""
    try:
        asyncio.run(_ptb_main())
    except Exception:
        log.exception("PTB runner crashed")

# Запускаем PTB сразу при импорте (когда gunicorn импортирует bot:app_flask)
threading.Thread(target=_ptb_thread_runner, daemon=True).start()

# -------------------- Flask routes --------------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        log.warning("Webhook 403: wrong secret")
        return abort(403)

    # Парсим JSON и прокидываем в PTB
    try:
        data = request.get_json(force=True, silent=False)
        log.info("Webhook JSON: %s", data)

        if not data:
            log.warning("Empty JSON in webhook")
            return "ok", 200

        if not app_ready.is_set() or app_tg is None:
            # Бывает в первые секунды после деплоя — просто не теряем апдейт
            log.warning(
                "Buffered update while bot not ready (queue=%s)",
                0 if app_tg is None else app_tg.update_queue.qsize(),
            )
            return "ok", 200

        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
        log.info("Webhook: update queued")
        return "ok", 200

    except Exception:
        # Всегда отвечаем 200, чтобы Telegram не видел 500 и не забивал очередь ретраями
        log.exception("Error in webhook_handler")
        return "ok", 200
