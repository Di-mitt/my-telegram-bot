# bot.py
from __future__ import annotations

import os
import logging
import asyncio
from threading import Thread
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

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # пример: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask --------------------
app_flask = Flask(__name__)

# -------------------- PTB application + event loop в отдельной нити --------------------
app_tg: Optional[Application] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def _loop_runner(loop: asyncio.AbstractEventLoop) -> None:
    """Запускаем новый event loop в отдельной нити."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _ptb_init_and_start(application: Application) -> None:
    """Инициализация PTB и установка вебхука (в том же loop)."""
    await application.initialize()
    await application.start()

    # Ставим вебхук
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")


def _ensure_started() -> None:
    """Создаём PTB app и loop один раз при первом обращении."""
    global app_tg, _loop

    if app_tg is not None and _loop is not None:
        return

    # 1) PTB app
    app_tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)  # последовательная обработка
        .build()
    )

    # Handlers
    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Привет! Я на связи 🤖")

    async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message and update.message.text:
            await update.message.reply_text(f"Вы написали: {update.message.text}")

    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # 2) Отдельный event loop в фоновой нити
    _loop = asyncio.new_event_loop()
    thread = Thread(target=_loop_runner, args=(_loop,), daemon=True)
    thread.start()

    # 3) Запускаем PTB внутри этого loop
    fut = asyncio.run_coroutine_threadsafe(_ptb_init_and_start(app_tg), _loop)
    # ждать не обязательно; если хотите — можно fut.result(timeout=10)


# -------------------- Flask routes --------------------
@app_flask.route("/", methods=["GET"])
def health():
    _ensure_started()
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    _ensure_started()

    # Проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
        log.info("Webhook JSON: %s", data)

        update = Update.de_json(data, app_tg.bot)  # type: ignore[arg-type]

        # Отдаём обработку сразу в PTB в его loop
        asyncio.run_coroutine_threadsafe(app_tg.process_update(update), _loop)  # type: ignore[arg-type]

        # Важно: всегда отвечаем 200 быстро, чтобы TG не пытался ретраить
        return "ok", 200
    except Exception:
        log.exception("Error in webhook handler")
        return "ok", 200


# -------------------- gunicorn entry --------------------
# Procfile должен указывать:
# web: gunicorn bot:app_flask
#
# Переменные окружения на Render:
# BOT_TOKEN=<твой_токен>
# APP_URL=https://my-telegram-bot-cr3q.onrender.com
# WEBHOOK_SECRET=mySecret_2025
