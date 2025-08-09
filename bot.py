# bot.py
from __future__ import annotations

import os
import json
import time
import asyncio
import logging
import threading
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

# -------------------- переменные окружения --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # обязательно
APP_URL = os.getenv("APP_URL")      # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask --------------------
app_flask = Flask(__name__)

# -------------------- Глобалы PTB --------------------
app_tg: Optional[Application] = None
app_ready = threading.Event()  # ставим, как только собран Application


# -------------------- handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Привет! Я на связи 🤖")
    except Exception:
        log.exception("start_cmd failed")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.message and update.message.text:
            await update.message.reply_text(f"Вы написали: {update.message.text}")
    except Exception:
        log.exception("echo failed")


# -------------------- PTB main --------------------
async def _ptb_main() -> None:
    """
    Создаём и запускаем PTB-приложение.
    Делаем set_webhook, но Flask принимает апдейты сам.
    """
    global app_tg

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg = application

    # Сразу помечаем «готов», чтобы вебхук мог класть апдейты в очередь,
    # даже если PTB ещё стартует — они будут обработаны, как только PTB поднимется.
    app_ready.set()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Инициализация и старт PTB
    await application.initialize()
    await application.start()

    # Сброс и установка вебхука с секретом
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")

    # Держим цикл живым
    while True:
        await asyncio.sleep(3600)


def _runner() -> None:
    """Запуск корутины PTB в отдельном потоке."""
    try:
        asyncio.run(_ptb_main())
    except Exception:
        log.exception("PTB runner crashed")


# Запускаем PTB при загрузке модуля (gunicorn импортирует bot:app_flask)
_thread = threading.Thread(target=_runner, name="ptb-runner", daemon=True)
_thread.start()

# -------------------- Flask routes --------------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Проверяем секрет в заголовке
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    # Получаем JSON апдейта
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Invalid JSON in webhook")
        return "ok", 200

    # Логируем кратко (полезно для отладки)
    try:
        log.info("Webhook JSON: %s", json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

    # Если PTB Application уже создан — кладём апдейт в очередь
    if app_tg is not None:
        try:
            upd = Update.de_json(data, app_tg.bot)
            # NB: put_nowait на asyncio.Queue из другого потока обычно ок для PTB.
            app_tg.update_queue.put_nowait(upd)
        except Exception:
            log.exception("Failed to enqueue update")
    else:
        log.warning("Received update but PTB Application is not built yet")

    # Всегда отвечаем 200, чтобы Telegram не считал это ошибкой
    return "ok", 200
