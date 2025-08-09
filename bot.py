# bot.py
from __future__ import annotations

import atexit
import logging
import os
import threading
from queue import Queue, Empty
from typing import Optional

from flask import Flask, request, abort

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== ЛОГИ =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ===================== ENV =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # напр.: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ===================== ГЛОБАЛЬНОЕ СОСТОЯНИЕ =====================
app_flask = Flask(__name__)

_ptb_app: Optional[Application] = None
_ptb_ready = threading.Event()          # ядро PTB готово принимать апдейты
_stop_event = threading.Event()         # сигнал на мягкую остановку
_buffer: "Queue[dict]" = Queue()        # буфер входящих апдейтов (пока PTB стартует)


# ===================== PTB HANDLERS =====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(update.message.text)


# ===================== СЛУЖЕБНОЕ =====================
def _drain_buffer() -> None:
    """Слить буфер накопленных апдейтов в очередь PTB."""
    global _ptb_app
    if not _ptb_app:
        return
    drained = 0
    while True:
        try:
            json_obj = _buffer.get_nowait()
        except Empty:
            break
        try:
            upd = Update.de_json(json_obj, _ptb_app.bot)
            _ptb_app.update_queue.put_nowait(upd)
            drained += 1
        except Exception:
            log.exception("Failed to enqueue buffered update")
    if drained:
        log.info("Buffered updates delivered: %s", drained)


def _ptb_runner() -> None:
    """
    Фоновый поток с собственным async loop для PTB:
    - инициализирует приложение
    - ставит вебхук
    - помечает готовность и сливает буфер
    - держит процесс живым до сигнала остановки
    """
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _main():
        global _ptb_app

        # Создаём PTB Application
        _ptb_app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .build()
        )

        # Хендлеры
        _ptb_app.add_handler(CommandHandler("start", start_cmd))
        _ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        # Старт PTB (без блокировки)
        await _ptb_app.initialize()
        await _ptb_app.start()

        # Ставим (или пере-ставляем) вебхук
        try:
            await _ptb_app.bot.delete_webhook(drop_pending_updates=False)
            await _ptb_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            log.info("Webhook set to %s", WEBHOOK_URL)
        except Exception:
            log.exception("Failed to set webhook")

        # Теперь готовы принимать апдейты из Flask
        _ptb_ready.set()
        _drain_buffer()

        # Держим цикл живым, пока не придёт сигнал остановки
        while not _stop_event.is_set():
            await asyncio.sleep(0.5)

        # Мягкая остановка
        try:
            await _ptb_app.stop()
            await _ptb_app.shutdown()
        except Exception:
            log.exception("Error during PTB shutdown")

    try:
        loop.run_until_complete(_main())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


# Запускаем PTB-поток при импортe модуля (когда gunicorn поднимает воркера)
_ptb_thread = threading.Thread(target=_ptb_runner, name="ptb-runner", daemon=True)
_ptb_thread.start()


@atexit.register
def _on_exit():
    # Сигнал на остановку PTB и ожидание потока
    _stop_event.set()
    if _ptb_thread.is_alive():
        _ptb_thread.join(timeout=5)


# ===================== FLASK ROUTES =====================
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook() -> tuple[str, int]:
    # Проверяем секрет (Telegram присылает его в заголовке)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        # Не наш запрос — 403
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON")
        return "ok", 200

    if not data:
        return "ok", 200

    # Если PTB ещё поднимается — кладём апдейт в буфер,
    # чтобы не терять сообщение во время рестартов воркера.
    if not _ptb_ready.is_set():
        _buffer.put_nowait(data)
        log.warning("Received update, but PTB is not ready yet (buffer=%d)", _buffer.qsize())
        return "ok", 200

    # PTB уже готов — сразу отдаём апдейт в очередь
    try:
        if _ptb_app is not None:
            upd = Update.de_json(data, _ptb_app.bot)
            _ptb_app.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue update")

    return "ok", 200
