# bot.py
import os
import json
import time
import logging
import threading
import atexit
import asyncio
from collections import deque
from typing import Deque, Tuple, Any

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

# ----------------------- ЛОГИ -----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ----------------------- ENV ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
APP_URL = os.getenv("APP_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is not set")
if not APP_URL:
    raise RuntimeError("APP_URL is not set")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ----------------------- Flask ----------------------
app_flask = Flask(__name__)

# -------------------- PTB runtime -------------------
_application: Application | None = None
_ptb_loop: asyncio.AbstractEventLoop | None = None
_ptb_ready: bool = False

# буфер апдейтов (если пришли раньше, чем запустился PTB)
_buffer_max = 50
_buffer: Deque[Tuple[dict, float]] = deque(maxlen=_buffer_max)
_buffer_lock = threading.Lock()

# событие для аккуратной остановки PTB
_stop_event: asyncio.Event | None = None


# --------------- Хэндлеры бота ----------------------
async def cmd_start(update: Update, _):
    await update.effective_chat.send_message("Привет! Я на связи 🤖")

async def any_text(update: Update, _):
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")


# --------------- Работа с буфером -------------------
def _buffer_push(data: dict):
    with _buffer_lock:
        _buffer.append((data, time.monotonic()))
        log.warning("Webhook: got update while PTB not ready -> buffer (total=%d)", len(_buffer))

async def _buffer_drain():
    """Слить накопленные апдейты в PTB после старта."""
    global _application
    if not _application:
        return
    drained = 0
    while True:
        with _buffer_lock:
            if not _buffer:
                break
            data, _ts = _buffer.popleft()
        try:
            upd = Update.de_json(data, _application.bot)
            await _application.process_update(upd)
            drained += 1
        except Exception as e:
            log.exception("Buffer drain failed: %s", e)
    if drained:
        log.info("Buffer drained: %d updates", drained)


# --------------- PTB поток / цикл -------------------
async def _ptb_start():
    """Инициализация и запуск PTB в отдельном event loop."""
    global _application, _ptb_ready, _stop_event

    log.info("PTB: building application...")
    _application = Application.builder().token(BOT_TOKEN).build()

    # Роуты/хэндлеры
    _application.add_handler(CommandHandler("start", cmd_start))
    _application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    # Инициализация/старт без собственного веб-сервера PTB —
    # мы сами принимаем HTTP во Flask и кидаем апдейты в process_update.
    await _application.initialize()
    await _application.start()

    # Вебхук ставим руками (его будет дергать Telegram -> Flask)
    try:
        await _application.bot.delete_webhook(drop_pending_updates=False)
        await _application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("PTB: webhook set -> %s", WEBHOOK_URL)
    except Exception as e:
        log.exception("Failed to set webhook: %s", e)

    _ptb_ready = True
    await _buffer_drain()

    _stop_event = asyncio.Event()
    await _stop_event.wait()

    # Остановка
    try:
        await _application.stop()
        await _application.shutdown()
    except Exception:
        log.exception("PTB stop/shutdown error")


def _ptb_thread():
    """Отдельный поток с собственным asyncio loop."""
    global _ptb_loop
    _ptb_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ptb_loop)
    try:
        _ptb_loop.run_until_complete(_ptb_start())
    finally:
        _ptb_loop.run_until_complete(asyncio.sleep(0))
        _ptb_loop.close()


# Стартуем PTB-поток сразу при импорте модуля (когда gunicorn грузит воркер)
_ptb_thread_handle = threading.Thread(target=_ptb_thread, name="ptb-thread", daemon=True)
_ptb_thread_handle.start()


def _ptb_shutdown():
    """Вызывается при завершении процесса (gunicorn SIGTERM)."""
    global _ptb_loop, _stop_event
    if _ptb_loop and _stop_event:
        try:
            _ptb_loop.call_soon_threadsafe(_stop_event.set)
        except Exception:
            pass

atexit.register(_ptb_shutdown)

# -------------------- Flask routes -------------------
@app_flask.get("/")
def index():
    return "ok"

@app_flask.get("/health")
def health():
    return jsonify(
        status="ok",
        ptb_ready=_ptb_ready,
        buffer_size=len(_buffer),
        buffer_max=_buffer_max,
        webhook_url=WEBHOOK_URL,
    )

@app_flask.post(WEBHOOK_PATH)
def telegram_webhook():
    # Проверка секрета (Telegram присылает этот заголовок)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != WEBHOOK_SECRET:
        return "forbidden", 403

    # Получаем апдейт
    data = request.get_json(silent=True, force=True) or {}

    # Если PTB ещё не готов — кладём в буфер
    if not _ptb_ready or not _application or not _ptb_loop:
        _buffer_push(data)
        return "ok", 200

    # Пробрасываем апдейт в PTB non-blocking
    try:
        upd = Update.de_json(data, _application.bot)
        asyncio.run_coroutine_threadsafe(_application.process_update(upd), _ptb_loop)
    except Exception as e:
        log.exception("Webhook update processing failed: %s", e)

    return "ok", 200
