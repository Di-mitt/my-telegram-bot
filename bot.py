# bot.py
import os
import sys
import json
import logging
import threading
import atexit
import asyncio
from typing import Optional

from flask import Flask, request, jsonify, Response

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# -----------------------------------------------------------------------------
# Конфиг и логирование
# -----------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mySecret_2025").strip()

if not BOT_TOKEN:
    log.error("Environment variable BOT_TOKEN is not set")
    # Развертывание на Render: если нет токена — падаем, чтобы было заметно
    raise SystemExit(1)

if not APP_URL:
    log.warning("APP_URL is not set; using http://0.0.0.0:10000 for local runs")
    APP_URL = "http://0.0.0.0:10000"

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -----------------------------------------------------------------------------
# Глобальные объекты PTB и его loop/флаги
# -----------------------------------------------------------------------------
_ptb_loop: Optional[asyncio.AbstractEventLoop] = None
_ptb_app: Optional[Application] = None
_ptb_ready = threading.Event()          # True когда PTB полностью готов
_ptb_stop_event: Optional[asyncio.Event] = None

# -----------------------------------------------------------------------------
# Хэндлеры бота
# -----------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я на связи 🤖\n"
        "Напиши что-нибудь — я повторю.\n"
        "Команды: /start, /help"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Доступные команды:\n"
        "/start — приветствие\n"
        "/help — эта подсказка"
    )

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")

# -----------------------------------------------------------------------------
# Функции старта/остановки PTB (в отдельном потоке и отдельном event loop)
# -----------------------------------------------------------------------------
async def _ptb_async_main():
    """
    Запускается внутри отдельного event loop.
    Создает Application, добавляет хэндлеры, стартует, ставит вебхук и ждет стопа.
    """
    global _ptb_app, _ptb_stop_event

    log.info("PTB: building application...")
    app = Application.builder().token(BOT_TOKEN).build()

    # Хэндлеры
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Инициализация/старт
    await app.initialize()
    await app.start()

    # Ставим вебхук только ПОСЛЕ старта PTB
    try:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("PTB: webhook is set -> %s", WEBHOOK_URL)
    except Exception:
        log.exception("PTB: failed to set webhook")

    # Сигнализируем Flask, что можем принимать апдейты
    _ptb_ready.set()
    _ptb_app = app

    # Ждем сигнала на остановку
    _ptb_stop_event = asyncio.Event()
    await _ptb_stop_event.wait()

    # Аккуратное завершение
    log.info("PTB: stopping application...")
    try:
        await app.stop()
        await app.shutdown()
        await app.post_stop()
    except Exception:
        log.exception("PTB: error while stopping")

def _ptb_thread_runner():
    """
    Цель фонового потока: создать loop и выполнить _ptb_async_main.
    """
    global _ptb_loop
    loop = asyncio.new_eventLoop() if hasattr(asyncio, "new_eventLoop") else asyncio.new_event_loop()
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
        log.info("PTB: event loop closed")

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
# Flask-приложение
# -----------------------------------------------------------------------------
app_flask = Flask(__name__)

@app_flask.get("/")
def root():
    return Response("ok", status=200, mimetype="text/plain")

@app_flask.get("/health")
def health():
    return jsonify(
        status="ok",
        ptb_ready=_ptb_ready.is_set(),
        webhook_url=WEBHOOK_URL,
    )

@app_flask.post(WEBHOOK_PATH)
def telegram_webhook():
    # Проверка секрета
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret != WEBHOOK_SECRET:
        log.warning("Webhook: secret mismatch")
        return ("", 403)

    # Если PTB еще не готов — ничего не делаем (Telegram потом ретрайнет)
    if not _ptb_ready.is_set() or _ptb_app is None or _ptb_loop is None:
        log.warning("Webhook: got update while PTB not ready (no-buffer mode)")
        return ("", 200)

    # JSON апдейта
    data = request.get_json(silent=True, force=True) or {}
    try:
        update = Update.de_json(data, _ptb_app.bot)
    except Exception:
        log.exception("Webhook: failed to parse update JSON")
        return ("", 200)

    # Передаем апдейт в PTB внутри его event loop
    try:
        fut = asyncio.run_coroutine_threadsafe(_ptb_app.process_update(update), _ptb_loop)
        # не блокируемся надолго; ошибок ждать не нужно
        _ = fut.result(timeout=0.5) if _ptb_loop.is_running() else None
    except Exception:
        # Даже если тут ошибка — для Telegram лучше ответить 200,
        # чтобы он не засыпал наш endpoint повторными ретраями.
        log.exception("Webhook: error while scheduling update in PTB")

    return ("", 200)

# Ручной ресет вебхука (опционально)
@app_flask.post("/admin/reset_webhook")
def reset_webhook():
    if not _ptb_ready.is_set() or _ptb_app is None or _ptb_loop is None:
        return jsonify(ok=False, error="PTB not ready"), 503

    async def _reset():
        await _ptb_app.bot.delete_webhook()
        await _ptb_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        return True

    try:
        fut = asyncio.run_coroutine_threadsafe(_reset(), _ptb_loop)
        fut.result(timeout=5)
        return jsonify(ok=True, url=WEBHOOK_URL)
    except Exception as e:
        log.exception("admin/reset_webhook failed")
        return jsonify(ok=False, error=str(e)), 500

# -----------------------------------------------------------------------------
# Запуск PTB в фоне при загрузке модуля
# -----------------------------------------------------------------------------
_ptb_thread = start_ptb_background()

# Чистое завершение при остановке воркера
@atexit.register
def _graceful_shutdown():
    stop_ptb_background()
