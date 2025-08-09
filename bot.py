# bot.py
from __future__ import annotations

import os
import json
import logging
import asyncio
import threading
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

# -------------------- logging --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- env --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")                    # e.g. https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask app --------------------
app_flask = Flask(__name__)

# -------------------- PTB globals --------------------
_app: Optional[Application] = None
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_ready = threading.Event()  # станет True, когда PTB запустится и вебхук будет установлен


# -------------------- handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# -------------------- PTB startup in background loop --------------------
async def _ptb_init_and_run() -> None:
    """Создаём Application, запускаем его (без сигналов) и ставим вебхук."""
    global _app

    log.info("PTB: building application...")
    _app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # handlers
    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Старт без сигнальных хендлеров
    await _app.initialize()
    await _app.start()

    # Вебхук после старта
    log.info("PTB: setting webhook to %s", WEBHOOK_URL)
    await _app.bot.delete_webhook(drop_pending_updates=True)
    await _app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("PTB: webhook is set")

    # Сообщаем Flask, что можно принимать апдейты
    _ready.set()

    # держим луп живым
    await asyncio.Event().wait()


def _ptb_thread_worker() -> None:
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ptb_init_and_run())
    except Exception:
        log.exception("PTB thread crashed")
    finally:
        try:
            _loop.run_until_complete(_app.stop()) if _app else None
        except Exception:
            pass
        try:
            _loop.close()
        except Exception:
            pass


# Стартуем PTB в отдельном потоке сразу при импорте модуля (для gunicorn worker’а).
_thread = threading.Thread(target=_ptb_thread_worker, name="ptb-loop", daemon=True)
_thread.start()


# -------------------- Flask routes --------------------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    # Проверка секрета
    secret_hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_hdr != WEBHOOK_SECRET:
        return abort(403)

    # Если PTB ещё не готов, просто подтверждаем, чтобы Телеграм не ретраил
    if not _ready.is_set():
        log.warning("Received update, but PTB is not ready yet (buffered)")
        return "ok", 200

    try:
        # JSON апдейта
        data = request.get_json(force=True, silent=False)
        if not data:
            return "ok", 200

        # Преобразуем в Update и кладём в очередь PTB из его event-loop’а
        upd = Update.de_json(data, _app.bot)

        fut = asyncio.run_coroutine_threadsafe(_app.update_queue.put(upd), _loop)
        # не блокируем ответ, но на случай исключения логируем
        try:
            fut.result(timeout=0.5)
        except Exception:
            log.exception("Failed to enqueue update")

        return "ok", 200

    except Exception:
        log.exception("Error in webhook_receiver")
        return "ok", 200


# -------------------- local run (не используется на Render) --------------------
if __name__ == "__main__":
    # Локально можно запустить Flask для health и webhook-приёма
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
