# bot.py
from __future__ import annotations

import os
import logging
import threading
import asyncio
import signal
from typing import Optional, List, Dict, Any

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

# ---------- логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ---------- переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # обязателен
APP_URL = os.getenv("APP_URL")      # обязателен: https://....onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- глобальные объекты ----------
app_flask = Flask(__name__)

app_tg: Optional[Application] = None
_ptb_ready: bool = False
_buffer: List[Dict[str, Any]] = []
_buffer_lock = threading.Lock()


# ---------- PTB handlers ----------
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


# ---------- вспомогательные ----------
def _push_to_buffer(data: Dict[str, Any]) -> None:
    """Кладём апдейт в буфер, пока PTB не поднялся."""
    with _buffer_lock:
        _buffer.append(data)
        if len(_buffer) > 200:
            # защищаемся от бесконечного роста
            _buffer.pop(0)
        log.warning("Received update, but PTB is not ready yet (buffer=%d)", len(_buffer))

async def _drain_buffer() -> None:
    """Пересылаем накопленные апдейты в PTB, когда ядро готово."""
    global _buffer
    if not app_tg:
        return
    with _buffer_lock:
        pending = _buffer
        _buffer = []
    if not pending:
        return
    log.info("Draining buffered updates: %d", len(pending))
    for data in pending:
        try:
            upd = Update.de_json(data, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
        except Exception:
            log.exception("Failed to forward buffered update")


# ---------- Flask routes ----------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    # проверяем секрет из заголовка
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON in webhook")
        return "ok", 200

    if not data:
        return "ok", 200

    log.info("Webhook JSON: %s", data)

    # если PTB ещё не готов — в буфер
    if not _ptb_ready or not app_tg:
        _push_to_buffer(data)
        return "ok", 200

    # иначе сразу в очередь PTB
    try:
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue update")

    return "ok", 200


# ---------- запуск PTB в фоне ----------
async def _ptb_main() -> None:
    global app_tg, _ptb_ready

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # инициализация PTB без run_webhook / run_polling
    await application.initialize()
    await application.start()

    # выставляем вебхук
    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")

    # ядро готово — сохраняем ссылку и сливаем буфер
    app_tg = application
    _ptb_ready = True
    await _drain_buffer()

    # держим задачу живой до сигнала остановки
    stop_event = asyncio.Event()

    def _on_term(*_):
        try:
            stop_event.set()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_term)
        except NotImplementedError:
            # на Windows сигналов может не быть
            pass

    await stop_event.wait()

    # корректное завершение
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        log.exception("Error during PTB shutdown")


def _bg_runner():
    # отдельный event loop для PTB
    try:
        asyncio.run(_ptb_main())
    except Exception:
        log.exception("PTB application crashed")


# стартуем фон сразу при импортe модуля (gunicorn импортирует модуль для wsgi)
_thread = threading.Thread(target=_bg_runner, name="ptb-runner", daemon=True)
_thread.start()

# ---------- конец файла ----------
