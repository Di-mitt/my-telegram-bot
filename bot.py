# bot.py
from __future__ import annotations

import json
import logging
import os
import threading
import time
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

# ================== ЛОГИ ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ================== ENV ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # обязателен
APP_URL = os.getenv("APP_URL")      # обязателен, например https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set environment vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ================== Flask ==================
app_flask = Flask(__name__)

# ================== PTB (python-telegram-bot) ==================
application: Optional[Application] = None
ptb_ready = threading.Event()  # флаг «PTB запущен и вебхук установлен»


# --- handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


def _run_ptb() -> None:
    """Запуск PTB в отдельном потоке:
    1) инициализация
    2) старт
    3) установка вебхука
    4) выставление ptb_ready
    """
    global application
    try:
        log.info("PTB thread: building Application...")
        application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .build()
        )

        # регистрируем обработчики
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        # запускаем ядро PTB (без собственного веб-сервера)
        log.info("PTB thread: initialize & start...")
        application.initialize()
        application.start()
        log.info("PTB thread: Application started")

        # выставим вебхук только ПОСЛЕ старта ядра PTB
        log.info("PTB thread: set webhook -> %s", WEBHOOK_URL)
        # сбросить старый
        application.bot.delete_webhook(drop_pending_updates=True).result()
        # установить новый
        application.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
        ).result()

        # теперь PTB готов принимать апдейты
        ptb_ready.set()
        log.info("PTB thread: webhook set. Ready to accept updates.")

        # держим поток «живым», пока процесс не завершится
        # (PTB внутри крутит свои фоновые задачи)
        while True:
            time.sleep(60)

    except Exception:
        log.exception("PTB thread crashed")


# Запускаем PTB сразу при импорте модуля (когда gunicorn загружает приложение Flask)
threading.Thread(target=_run_ptb, name="ptb-runner", daemon=True).start()


# ================== Flask routes ==================
@app_flask.route("/", methods=["GET"])
def health():
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    """Приём апдейтов от Telegram. Ждём готовности PTB и только потом отдаём апдейт в PTB."""
    # проверим секретный заголовок
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    # подождём PTB (до 20 сек). Telegram будет терпеливо ждать ответ ~10 сек, это ок.
    if not ptb_ready.wait(timeout=20):
        log.warning("Received update, but PTB not ready yet")
        # даём 200, чтобы Telegram не заспамил ретраями (апдейт всё равно скоро придёт ещё раз)
        return "ok", 200

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        # некорректный JSON — отвечаем 200, чтобы TG не долбил ретраями
        log.exception("Invalid JSON in webhook")
        return "ok", 200

    if not data:
        return "ok", 200

    # Преобразуем JSON в Update и кладём в очередь PTB
    try:
        upd = Update.de_json(data, application.bot)  # type: ignore[arg-type]
        application.update_queue.put_nowait(upd)     # type: ignore[union-attr]
    except Exception:
        # не валим вебхук, всегда отвечаем 200
        log.exception("Failed to enqueue update")
        return "ok", 200

    return "ok", 200
    
