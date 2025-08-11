# bot.py
from __future__ import annotations
import os
import logging
import asyncio
import threading
import signal
import time
from collections import deque
from typing import Optional, Deque, Tuple, Dict, Any

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

# ===== ЛОГИ =====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ===== ENV =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("BOT_TOKEN и APP_URL обязательны")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ===== Flask =====
app_flask = Flask(__name__)

# ===== PTB STATE =====
_app: Optional[Application] = None
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_app_ready = threading.Event()
_stopping = threading.Event()

# Буфер
BUF_MAX = 100
BUF_TTL = 60
_buffer: Deque[Tuple[float, Dict[str, Any]]] = deque()
_buffer_lock = threading.Lock()

# ===== HANDLERS =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен и готов к работе ✅")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# ===== BUFFER HELPERS =====
def _enqueue_update_safe(data: dict):
    upd = Update.de_json(data, _app.bot)  # type: ignore
    fut = asyncio.run_coroutine_threadsafe(_app.update_queue.put(upd), _loop)  # type: ignore
    fut.result(timeout=2.0)

def _buffer_push(data: dict):
    ts = time.time()
    with _buffer_lock:
        while _buffer and ts - _buffer[0][0] > BUF_TTL:
            _buffer.popleft()
        if len(_buffer) >= BUF_MAX:
            _buffer.popleft()
        _buffer.append((ts, data))
        log.info("Buffered update (total=%d)", len(_buffer))

def _buffer_flush_if_ready() -> int:
    if not (_app and _app_ready.is_set()):
        return 0
    flushed = 0
    now = time.time()
    with _buffer_lock:
        items = [(t, d) for t, d in list(_buffer) if now - t <= BUF_TTL]
        _buffer.clear()
    for _, data in items:
        try:
            _enqueue_update_safe(data)
            flushed += 1
        except Exception:
            log.exception("Ошибка при отправке апдейта из буфера")
    if flushed:
        log.info("Flushed %d buffered updates", flushed)
    return flushed

def _buffer_flusher_thread():
    while not _stopping.is_set():
        if _app_ready.wait(timeout=0.5):
            _buffer_flush_if_ready()
        time.sleep(0.5)

# ===== PTB MAIN =====
async def _ptb_main():
    global _app
    log.info("PTB: building application...")
    _app = ApplicationBuilder().token(BOT_TOKEN).build()
    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    await _app.initialize()
    await _app.start()
    _app_ready.set()

    log.info("PTB: setting webhook to %s", WEBHOOK_URL)
    await _app.bot.delete_webhook(drop_pending_updates=True)
    await _app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("PTB: webhook is set")

    # сразу сливаем буфер
    _buffer_flush_if_ready()

    await asyncio.Event().wait()

def _ptb_thread_runner():
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ptb_main())
    except Exception:
        log.exception("PTB thread crashed")
    finally:
        if _app:
            try:
                _loop.run_until_complete(_app.stop())
            except Exception:
                pass
        _loop.close()

threading.Thread(target=_ptb_thread_runner, daemon=True).start()
threading.Thread(target=_buffer_flusher_thread, daemon=True).start()

# ===== SHUTDOWN =====
def _on_term(signum, frame):
    _stopping.set()
    log.info("Получен сигнал %s, останавливаем...", signum)

signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)

# ===== ROUTES =====
@app_flask.get("/")
def health():
    return "OK", 200

@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)
    if _stopping.is_set():
        return "ok", 200

    try:
        data = request.get_json(force=True)
    except Exception:
        log.exception("Bad JSON")
        return "ok", 200
    if not data:
        return "ok", 200

    if _app_ready.is_set():
        try:
            _enqueue_update_safe(data)
        except Exception:
            log.exception("enqueue failed")
        return "ok", 200
    else:
        _buffer_push(data)
        return "ok", 200

# ===== LOCAL RUN =====
if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
