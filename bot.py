# bot.py
from __future__ import annotations
import os
import json
import logging
import asyncio
import threading

from flask import Flask, request, abort

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler,
    ContextTypes, filters,
)

# ---------- логирование ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- Flask ----------
app_flask = Flask(__name__)

# ---------- PTB app + loop/thread ----------
app_tg: Application | None = None
tg_loop: asyncio.AbstractEventLoop | None = None
tg_ready = threading.Event()   # поднимем, когда PTB запустится

# ---------- handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Привет! Я на связи 🤖")
    except Exception:
        log.exception("Error in /start")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.message and update.message.text:
            await update.message.reply_text(f"Вы написали: {update.message.text}")
    except Exception:
        log.exception("Error in echo")


# ---------- PTB thread ----------
async def _ptb_async_start(application: Application) -> None:
    """Инициализация и запуск PTB без собственного веб-сервера."""
    await application.initialize()
    await application.start()
    log.info("PTB application is up")

    # Ставим вебхук (сбрасываем на всякий случай старый)
    await application.bot.delete_webhook(drop_pending_updates=False)
    await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set to %s", WEBHOOK_URL)

    # Сигнализируем Flask, что можно принимать апдейты
    tg_ready.set()

    # держим цикл живым
    await asyncio.Event().wait()

def _ptb_thread_fn() -> None:
    global tg_loop, app_tg
    tg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(tg_loop)

    app_tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # handlers
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    try:
        tg_loop.run_until_complete(_ptb_async_start(app_tg))
    except Exception:
        log.exception("PTB thread crashed")


# Стартуем поток PTB при импортe модуля (т.е. при старте gunicorn)
threading.Thread(target=_ptb_thread_fn, name="ptb-thread", daemon=True).start()


# ---------- Flask routes ----------
@app_flask.route("/", methods=["GET"])
def health():
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # проверяем секрет
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
        log.info("Webhook JSON: %s", json.dumps(data, ensure_ascii=False))

        # если PTB ещё не готов — сохранять негде; просто отвечаем 200,
        # чтобы Telegram не считал это ошибкой, и ждём следующего апдейта
        if not tg_ready.is_set() or not app_tg or not tg_loop:
            log.warning("Received update, but PTB not ready yet")
            return "ok", 200

        # конвертация и безопасная передача в PTB из другого потока
        update = Update.de_json(data, app_tg.bot)
        fut = asyncio.run_coroutine_threadsafe(
            app_tg.update_queue.put(update), tg_loop
        )
        fut.result(timeout=2)  # чтоб видеть исключения сразу в логах

        return "ok", 200
    except Exception:
        log.exception("Error in webhook_handler")
        return "ok", 200


# ---------- gunicorn entry ----------
# Procfile: web: gunicorn bot:app_flask
    
