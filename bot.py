# bot.py
from __future__ import annotations

import asyncio
import logging
import os
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

# =============== ЛОГИ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# =============== ENV ===================
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# =============== FLASK =================
app_flask = Flask(__name__)

# PTB app и флажок готовности
app_tg: Optional[Application] = None
_app_ready = asyncio.Event()


# =============== HANDLERS ==============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖\nНапиши мне что-нибудь!")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — проверка, что бот жив\n"
        "/help — эта подсказка\n\n"
        "И просто напиши текст — отвечу 😉"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = update.message.text.strip()
    await update.message.reply_text(f"Ты сказал: “{txt}”. Я тебя слышу 👂")


# =============== PTB RUNNER ============
async def _ptb_runner(application: Application) -> None:
    """Инициализация и запуск PTB в фоне (без собственного HTTP-сервера)."""
    await application.initialize()
    await application.start()
    _app_ready.set()  # теперь готовы принимать апдейты из вебхука
    log.info("PTB application is up")

    # держим задачу «вечно»
    await asyncio.Event().wait()


def _start_ptb_in_thread() -> None:
    """Создать Application, навесить хендлеры и запустить на отдельном потоке."""
    global app_tg

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Хендлеры
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app_tg = application

    # фоновая петля
    def _run():
        asyncio.run(_ptb_runner(application))

    threading.Thread(target=_run, daemon=True, name="ptb-runner").start()


# =============== WEBHOOK SETUP =========
async def _ensure_webhook_once() -> None:
    """Сбрасываем старый вебхук и ставим новый (асинхронно, один раз при старте)."""
    if not app_tg:
        return
    try:
        await app_tg.bot.delete_webhook(drop_pending_updates=True)
        await app_tg.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")


def _kick_webhook_setup() -> None:
    """Запускаем установку вебхука отдельно, чтобы не блокировать импорт."""
    async def _runner():
        # подождём готовности PTB, чтобы был bot-сессия
        await _app_ready.wait()
        await _ensure_webhook_once()

    threading.Thread(target=lambda: asyncio.run(_runner()), daemon=True).start()


# =============== FLASK ROUTES ==========
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook_entry():
    """Приём апдейта от Telegram + защита по секретному заголовку."""
    # 1) Проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    # 2) JSON апдейта
    data = request.get_json(silent=True)
    if not data:
        return "ok", 200

    # 3) Если PTB ещё не готов — просто подождём, но ответим 200,
    #    чтобы Телеграм не считал это ошибкой.
    if not _app_ready.is_set():
        log.warning("Получено обновление, но приложение PTB ещё не собрано")
        return "ok", 200

    # 4) Кладём апдейт в очередь PTB
    try:
        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
    except Exception:
        log.exception("Failed to push update to PTB")
    return "ok", 200


# =============== ENTRYPOINT ============
# Запускаем PTB и ставим вебхук при импортe модуля (когда Gunicorn поднимает приложение)
_start_ptb_in_thread()
_kick_webhook_setup()
