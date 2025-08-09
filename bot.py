# bot.py
from __future__ import annotations

import os
import logging
import asyncio
import threading
from collections import deque
from typing import Optional, Deque, Dict, Any

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

# -------------------- переменные окружения --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # напр.: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask-приложение --------------------
app_flask = Flask(__name__)

# -------------------- PTB объекты/состояние --------------------
app_tg: Optional[Application] = None
_ready_evt: asyncio.Event = asyncio.Event()
_buffer: Deque[Dict[str, Any]] = deque(maxlen=100)  # временный буфер апдейтов


# -------------------- handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Привет! Я на связи 🤖")
    except Exception:
        log.exception("Error in /start handler")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.message and update.message.text:
            await update.message.reply_text(f"Вы написали: {update.message.text}")
    except Exception:
        log.exception("Error in echo handler")


# -------------------- вспомогательное --------------------
def _flush_buffer_safe() -> None:
    """Пробуем «слить» накопленные апдейты в очередь PTB."""
    global app_tg
    if not (_ready_evt.is_set() and app_tg and app_tg.update_queue):
        return
    pushed = 0
    while _buffer:
        data = _buffer.popleft()
        try:
            upd = Update.de_json(data, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
            pushed += 1
        except Exception:
            log.exception("Failed to push buffered update")
    if pushed:
        log.info("Flushed %d buffered update(s) to PTB", pushed)


async def _runner() -> None:
    """
    Запускаем ядро PTB в «вебхук»-режиме: сам сервер у нас Flask/Gunicorn,
    а PTB — только обработчик апдейтов.
    """
    global app_tg

    # 1) Собираем приложение
    app_tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # 2) Инициализируем и стартуем PTB
    await app_tg.initialize()
    await app_tg.start()

    # 3) Сразу считаем PTB готовым и сливаем буфер
    _ready_evt.set()
    _flush_buffer_safe()

    # 4) Выставляем (пере-)вебхук у Telegram
    try:
        # не чистим pending, чтобы не потерять то, что уже пришло
        await app_tg.bot.delete_webhook(drop_pending_updates=False)
        await app_tg.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
        )
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")

    # 5) Держим задачу живой, пока процесс живёт
    log.info("PTB application is up")
    while True:
        await asyncio.sleep(3600)


def _start_ptb_background() -> None:
    """Запускаем _runner() в отдельном потоке с собственным событийным циклом."""
    def _target():
        try:
            asyncio.run(_runner())
        except Exception:
            log.exception("PTB runner crashed")

    th = threading.Thread(target=_target, daemon=True)
    th.start()


# -------------------- Flask routes --------------------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook_handler():
    # Проверка секрета от Telegram
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Invalid JSON in webhook")
        return "ok", 200

    if not data:
        return "ok", 200

    # Если PTB уже готов — сразу отправляем в его очередь,
    # иначе складываем во временный буфер.
    if _ready_evt.is_set() and app_tg and app_tg.update_queue:
        try:
            upd = Update.de_json(data, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
        except Exception:
            log.exception("Failed to enqueue update")
    else:
        _buffer.append(data)
        log.warning("Received update, but PTB is not ready yet (buffer=%d)", len(_buffer))

    return "ok", 200


# -------------------- entrypoint --------------------
# При импорте модуля (когда gunicorn поднимает воркер) — запускаем PTB в фоне.
_start_ptb_background()

# Ничего не блокируем — gunicorn будет брать объект app_flask
# из этого модуля: `gunicorn bot:app_flask`
