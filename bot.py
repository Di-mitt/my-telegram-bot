# bot.py
from __future__ import annotations

import os
import threading
import asyncio
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

# -------------------- логирование --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# -------------------- окружение ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask --------------------------
app_flask = Flask(__name__)

# Сюда положим PTB-приложение и флаг готовности
application: Optional[Application] = None
ptb_ready = threading.Event()


# -------------------- handlers -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# -------------------- запуск PTB в потоке -------------
def _ptb_thread() -> None:
    """
    Отдельный поток с собственным event loop:
    - initialize() / start()
    - выставляем webhook ПОСЛЕ старта
    - держим приложение живым
    """
    global application
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def runner():
        global application
        application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        # регистрируем handlers
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        # полноценный lifecycle вручную
        await application.initialize()
        await application.start()

        # только теперь ставим вебхук
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)

        # даём Flask знать, что можно принимать апдейты
        ptb_ready.set()

        # держим приложение «вечно»
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(runner())
    except Exception:
        log.exception("PTB thread crashed")
    finally:
        try:
            loop.run_until_complete(application.stop())  # type: ignore[arg-type]
            loop.run_until_complete(application.shutdown())  # type: ignore[arg-type]
        except Exception:
            pass
        loop.close()


# Стартуем поток PTB один раз при загрузке модуля
_t = threading.Thread(target=_ptb_thread, name="ptb-thread", daemon=True)
_t.start()


# -------------------- Flask routes --------------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    # Простой healthcheck
    return ("OK", 200)


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_receiver():
    # проверяем секрет Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    # если PTB ещё не готов — отвечаем 200, чтобы Telegram не ретраил,
    # но апдейт пропускаем (почти сразу станет готов).
    if not ptb_ready.is_set():
        log.warning("Received update, but PTB not ready yet")
        return ("ok", 200)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON in webhook")
        return ("ok", 200)

    if not data:
        return ("ok", 200)

    # прокидываем апдейт в очередь PTB
    try:
        upd = Update.de_json(data, application.bot)  # type: ignore[union-attr]
        application.update_queue.put_nowait(upd)     # type: ignore[union-attr]
    except Exception:
        log.exception("Failed to enqueue update")

    return ("ok", 200)
    return "ok", 200
    
