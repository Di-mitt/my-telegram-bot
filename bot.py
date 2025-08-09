# bot.py
from __future__ import annotations

import os
import json
import logging
import asyncio
import threading
from collections import deque
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

# ---------- logging ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")                    # e.g. https://<name>.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- Flask ----------
app_flask = Flask(__name__)

# ---------- PTB globals ----------
_app: Optional[Application] = None
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

_ready = threading.Event()      # ставим True после старта PTB и set_webhook
_buf_lock = threading.Lock()
_buf: deque[dict] = deque(maxlen=500)  # буфер «сырых» апдейтов

# ---------- handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# ---------- helpers ----------
def _enqueue_update_dict(data: dict) -> None:
    """Кладём Update в очередь PTB из чужого потока."""
    upd = Update.de_json(data, _app.bot)  # type: ignore[arg-type]
    fut = asyncio.run_coroutine_threadsafe(_app.update_queue.put(upd), _loop)  # type: ignore[union-attr]
    try:
        fut.result(timeout=0.5)
    except Exception:
        log.exception("Failed to enqueue update")

async def _drain_buffer() -> None:
    """Сливаем накопленные апдейты в PTB (в его event loop)."""
    drained = 0
    while True:
        with _buf_lock:
            if not _buf:
                break
            data = _buf.popleft()
        try:
            await _app.update_queue.put(Update.de_json(data, _app.bot))  # type: ignore[arg-type]
            drained += 1
        except Exception:
            log.exception("Failed to drain one buffered update")
    if drained:
        log.info("Drained %d buffered update(s) into PTB", drained)

# ---------- PTB startup (background thread + its own loop) ----------
async def _ptb_init_and_run() -> None:
    """Создаём Application, запускаем, ставим вебхук, отмечаем готовность и ждём вечно."""
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

    log.info("PTB: setting webhook to %s", WEBHOOK_URL)
    await _app.bot.delete_webhook(drop_pending_updates=True)
    await _app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("PTB: webhook is set")

    _ready.set()
    await _drain_buffer()

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
            if _app:
                _loop.run_until_complete(_app.stop())
        except Exception:
            pass
        try:
            _loop.close()
        except Exception:
            pass

# стартуем PTB при импорте модуля (когда gunicorn поднимает воркер)
threading.Thread(target=_ptb_thread_worker, name="ptb-loop", daemon=True).start()

# ---------- Flask routes ----------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    # проверка секрета
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON in webhook")
        return "ok", 200
    if not data:
        return "ok", 200

    # если PTB ещё стартует — ждём до 5с; если не успел — буферим
    if not _ready.is_set():
        if _ready.wait(timeout=5.0):
            # только что стал готов — сперва промоем буфер, затем положим текущий апдейт
            try:
                asyncio.run_coroutine_threadsafe(_drain_buffer(), _loop).result(timeout=1)
            except Exception:
                log.exception("drain after wait-ready failed")
            try:
                _enqueue_update_dict(data)
            except Exception:
                log.exception("enqueue after wait-ready failed")
            return "ok", 200

        with _buf_lock:
            _buf.append(data)
        log.warning("Buffered update while PTB not ready (queue=%d)", len(_buf))
        return "ok", 200

    # PTB уже готов — на всякий случай лениво промываем буфер и отправляем текущий апдейт
    try:
        if _buf:  # ленивый drain, если что-то застряло
            try:
                asyncio.run_coroutine_threadsafe(_drain_buffer(), _loop).result(timeout=1)
            except Exception:
                log.exception("lazy drain failed")
        _enqueue_update_dict(data)
    except Exception:
        log.exception("Error enqueuing update")

    return "ok", 200

# ---------- local run (dev) ----------
if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
