# bot.py
from __future__ import annotations

import os
import logging
import asyncio
import threading
import signal
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
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- окружение --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Env BOT_TOKEN and APP_URL must be set")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask --------------------
app_flask = Flask(__name__)

# -------------------- PTB state --------------------
_app: Optional[Application] = None
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

_app_ready = threading.Event()   # PTB инициализирован и запущен
_stopping = threading.Event()    # процесс сворачивается, апдейты не принимаем «в буфер»

# -------------------- handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# -------------------- helpers --------------------
def _enqueue_update_safe(data: dict) -> None:
    """Положить Update в очередь PTB из стороннего потока (Flask)."""
    upd = Update.de_json(data, _app.bot)  # type: ignore[arg-type]
    fut = asyncio.run_coroutine_threadsafe(_app.update_queue.put(upd), _loop)  # type: ignore[union-attr]
    fut.result(timeout=1.0)

# -------------------- PTB background thread --------------------
async def _ptb_main() -> None:
    """Создаёт и запускает PTB; ставит вебхук; держит цикл живым."""
    global _app

    log.info("PTB: building application...")
    _app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    await _app.initialize()
    await _app.start()

    # отмечаем готовность СРАЗУ после старта ядра
    _app_ready.set()

    # только теперь ставим вебхук
    log.info("PTB: setting webhook to %s", WEBHOOK_URL)
    await _app.bot.delete_webhook(drop_pending_updates=True)
    await _app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("PTB: webhook is set")

    # держим живым, пока не попросят остановиться
    await asyncio.Event().wait()

def _ptb_thread_runner() -> None:
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ptb_main())
    except Exception:
        log.exception("PTB thread crashed")
    finally:
        try:
            if _app:
                _loop.run_until_complete(_app.stop())
        except Exception:
            pass
        try:
            _loop.close()
        except Exception:
            pass

# стартуем PTB при импорте модуля (когда gunicorn воркер поднимается)
threading.Thread(target=_ptb_thread_runner, name="ptb-loop", daemon=True).start()

# -------------------- graceful shutdown --------------------
def _on_term(signum, frame):
    _stopping.set()
    log.info("Got signal %s, stopping gracefully...", signum)

signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)

# -------------------- Flask routes --------------------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    if _stopping.is_set():
        # Инстанс сворачивается — подтверждаем 200, чтобы TG не ретрайл,
        # и не буферим (буфер в памяти всё равно потеряется при рестарте).
        return "ok", 200

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON in webhook")
        return "ok", 200
    if not data:
        return "ok", 200

    # Если PTB уже поднят — кладём сразу
    if _app is not None and _app_ready.is_set():
        try:
            _enqueue_update_safe(data)
        except Exception:
            log.exception("enqueue failed")
        return "ok", 200

    # Иначе подождём до 5 сек старта PTB
    if _app_ready.wait(timeout=5.0) and _app is not None:
        try:
            _enqueue_update_safe(data)
        except Exception:
            log.exception("enqueue after wait failed")
        return "ok", 200

    # Если не успели подняться — не буферим (иначе потеряем при рестарте),
    # просто подтверждаем 200 и логируем.
    log.warning("Dropped update while PTB not ready (no-buffer mode)")
    return "ok", 200

# -------------------- локальный запуск --------------------
if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
