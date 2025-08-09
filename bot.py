# bot.py
from __future__ import annotations

import os
import asyncio
import logging
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

# ============ Логи ============
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ============ ENV ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ============ Flask ============
app_flask = Flask(__name__)

# PTB-приложение (создаём/запускаем ниже, в отдельном потоке)
app_tg: Optional[Application] = None

# Флаг “PTB готов” и буфер входящих апдейтов на время старта
_ready_evt = threading.Event()
_buffer = deque(maxlen=100)


# ============ Handlers ============
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


# ============ “Движок” PTB без собственного веб-сервера ============
def _run_ptb_in_background() -> None:
    """
    Готовит Application, запускает его (initialize/start) в собственном event loop
    и ставит webhook на наш Flask-роут WEBHOOK_URL. Работает в отдельном потоке.
    """
    async def _runner():
        global app_tg

        app_tg = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .build()
        )

        # Регистрация обработчиков
        app_tg.add_handler(CommandHandler("start", start_cmd))
        app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        # Инициализация и запуск PTB (без run_webhook / run_polling)
        await app_tg.initialize()
        await app_tg.start()

        # Выставляем webhook у Telegram на наш Flask-URL
        # (PTB сам ничего не слушает по HTTP — это делает Flask+gunicorn)
        try:
            # Сначала удалим старый
            await app_tg.bot.delete_webhook(drop_pending_updates=False)
            # Затем поставим новый со скрытым заголовком-подписью
            await app_tg.bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET,
            )
            log.info("Webhook set to %s", WEBHOOK_URL)
        except Exception:
            log.exception("Failed to set webhook")

        # Отмечаем готовность и сливаем буфер
        _ready_evt.set()
        _flush_buffer_safe()

        # Держим приложение “вечно” запущенным
        await asyncio.Event().wait()

    # Запускаем собственный event loop PTB
    asyncio.run(_runner())


def _flush_buffer_safe() -> None:
    """Отправляет накопленные JSON-апдейты в PTB-очередь, если всё готово."""
    if not (_ready_evt.is_set() and app_tg and app_tg.update_queue):
        return

    while _buffer:
        data = _buffer.popleft()
        try:
            upd = Update.de_json(data, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
        except Exception:
            log.exception("Failed to push buffered update")


# ============ Flask routes ============
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Защита: проверяем секрет из заголовка
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    # Получаем JSON апдейта
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200

    # Лог для отладки (можно закомментить)
    log.info("Webhook JSON: %s", data)

    # Если PTB ещё стартует — в буфер
    if not _ready_evt.is_set() or not app_tg or not app_tg.update_queue:
        log.warning("Received update, but PTB not ready yet")
        _buffer.append(data)
        return "ok", 200

    # Преобразуем и отправляем в очередь PTB
    try:
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to push update to PTB queue")

    return "ok", 200


# ============ Старт фонового потока PTB ============
# Запускаем движок PTB сразу при импорте модуля (до старта gunicorn воркера)
_thread = threading.Thread(target=_run_ptb_in_background, daemon=True)
_thread.start()
