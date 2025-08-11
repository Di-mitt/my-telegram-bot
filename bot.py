# bot.py
import os
import sys
import json
import logging
import threading
import atexit
import asyncio
from typing import Optional, Deque, Tuple
from collections import deque

from flask import Flask, request, jsonify, Response

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# -----------------------------------------------------------------------------
# ЛОГИ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")

# -----------------------------------------------------------------------------
# КОНФИГ
# -----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mySecret_2025").strip()

if not BOT_TOKEN:
    log.error("Environment variable BOT_TOKEN is not set")
    raise SystemExit(1)

if not APP_URL:
    log.warning("APP_URL not set; fallback http://0.0.0.0:10000 (local only)")
    APP_URL = "http://0.0.0.0:10000"

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -----------------------------------------------------------------------------
# PTB: LOOP/APP/ФЛАГИ
# -----------------------------------------------------------------------------
_ptb_loop: Optional[asyncio.AbstractEventLoop] = None
_ptb_app: Optional[Application] = None
_ptb_ready = threading.Event()
_ptb_stop_event: Optional[asyncio.Event] = None

# -----------------------------------------------------------------------------
# БУФЕР ВЕБХУКА (чтобы не терять апдейты при старте)
# -----------------------------------------------------------------------------
# ограничим буфер по кол-ву, чтобы не съесть память, если что-то пойдет не так
BUFFER_MAX = int(os.environ.get("WEBHOOK_BUFFER_MAX", "50"))
_buffer_lock = threading.Lock()
_buffer: Deque[Tuple[dict, float]] = deque()  # (raw_json, timestamp)

def _buffer_push(data: dict):
    with _buffer_lock:
        while len(_buffer) >= BUFFER_MAX:
            _buffer.popleft()
        _buffer.append((data, asyncio.get_event_loop_policy().time()))
        log.warning("Webhook buffered (total=%s)", len(_buffer))

def _buffer_drain():
    """Вызываем уже ПОСЛЕ того, как PTB готов; переносит накопленные апдейты в PTB."""
    if not (_ptb_ready.is_set() and _ptb_app and _ptb_loop):
        return
    drained = 0
    while True:
        with _buffer_lock:
            if not _buffer:
                break
            data, ts = _buffer.popleft()
        try:
            upd = Update.de_json(data, _ptb_app.bot)
            asyncio.run_coroutine_threadsafe(_ptb_app.process_update(upd), _ptb_loop)
            drained += 1
        except Exception:
            log.exception("buffer drain: failed to schedule update")
    if drained:
        log.info("Webhook buffer drained: %s updates flushed", drained)

# -----------------------------------------------------------------------------
# ХЭНДЛЕРЫ БОТА
# -----------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я на связи 🤖\nНапиши что-нибудь — я повторю.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды: /start, /help")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")

# -----------------------------------------------------------------------------
# PTB ASYNC MAIN (в отдельном event loop + поток)
# -----------------------------------------------------------------------------
async def _ptb_async_main():
    global _ptb_app, _ptb_stop_event

    log.info("PTB: building application...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    await app.initialize()
    await app.start()

    # Ставим вебхук после старта
    try:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("PTB: webhook set -> %s", WEBHOOK_URL)
    except Exception:
        log.exception("PTB: failed to set webhook")

    # Готово — разрешаем принимать апдейты
    _ptb_app = app
    _ptb_ready.set()

    # Сразу пробуем слить буфер (если пришло что-то во время старта)
    try:
        _buffer_drain()
    except Exception:
        log.exception("buffer drain on ready failed")

    _ptb_stop_event = asyncio.Event()
    await _ptb_stop_event.wait()

    log.info("PTB: stopping...")
    try:
        await app.stop()
        await app.shutdown()
        await app.post_stop()
    except Exception:
        log.exception("PTB: stop error")

def _ptb_thread_runner():
    global _ptb_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _ptb_loop = loop
    try:
        loop.run_until_complete(_ptb_async_main())
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        log.info("PTB: loop closed")

def start_ptb_background():
    t = threading.Thread(target=_ptb_thread_runner, name="ptb-runner", daemon=True)
    t.start()
    return t

def stop_ptb_background():
    if _ptb_loop and _ptb_stop_event:
        try:
            _ptb_loop.call_soon_threadsafe(_ptb_stop_event.set)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# FLASK
# -----------------------------------------------------------------------------
app_flask = Flask(__name__)

@app_flask.get("/")
def root():
    return Response("ok", status=200, mimetype="text/plain")

@app_flask.get("/health")
def health():
    with _buffer_lock:
        buf_size = len(_buffer)
    return jsonify(
        status="ok",
        ptb_ready=_ptb_ready.is_set(),
        webhook_url=WEBHOOK_URL,
        buffer_size=buf_size,
        buffer_max=BUFFER_MAX,
    )

@app_flask.post(WEBHOOK_PATH)
def telegram_webhook():
    # проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != WEBHOOK_SECRET:
        log.warning("Webhook: secret mismatch")
        return ("", 403)

    data = request.get_json(silent=True, force=True) or {}

    # если PTB не готов — кладем в буфер и 200
    if not _ptb_ready.is_set() or _ptb_app is None or _ptb_loop is None:
        log.warning("Webhook: got update while PTB not ready -> buffer")
        _buffer_push(data)
        return ("", 200)

    # PTB готов — сразу отдаем
    try:
        update = Update.de_json(data, _ptb_app.bot)
        asyncio.run_coroutine_threadsafe(_ptb_app.process_update(update), _ptb_loop)
    except Exception:
        log.exception("Webhook: failed to schedule update")
    return ("", 200)

# админка: ручной ресет вебхука и слив буфера
@app_flask.post("/admin/reset_webhook")
def reset_webhook():
    if not (_ptb_ready.is_set() and _ptb_app and _ptb_loop):
        return jsonify(ok=False, error="PTB not ready"), 503

    async def _reset():
        await _ptb_app.bot.delete_webhook()
        await _ptb_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)

    try:
        fut = asyncio.run_coroutine_threadsafe(_reset(), _ptb_loop)
        fut.result(timeout=5)
        return jsonify(ok=True, url=WEBHOOK_URL)
    except Exception as e:
        log.exception("reset_webhook failed")
        return jsonify(ok=False, error=str(e)), 500

@app_flask.post("/admin/flush_buffer")
def flush_buffer():
    try:
        _buffer_drain()
        with _buffer_lock:
            left = len(_buffer)
        return jsonify(ok=True, left=left)
    except Exception as e:
        log.exception("manual buffer flush failed")
        return jsonify(ok=False, error=str(e)), 500

# -----------------------------------------------------------------------------
# СТАРТ PTB В ФОНЕ
# -----------------------------------------------------------------------------
_ptb_thread = start_ptb_background()

@atexit.register
def _graceful_shutdown():
    stop_ptb_background()
