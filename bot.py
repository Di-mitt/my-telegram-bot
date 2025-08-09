# bot.py
from __future__ import annotations

import os
import threading
import logging
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

# ---------- Логи ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- Переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ---------- Flask (WSGI) ----------
app_flask = Flask(__name__)

# Глобальная ссылка на PTB-приложение
app_tg: Optional[Application] = None


# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я живу на Render и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ---------- Вебхук: health и приём апдейтов ----------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Проверяем секрет
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json(force=True, silent=True)
    if not data:
        log.warning("Empty JSON in webhook")
        return "ok", 200

    if not app_tg:
        # Редкий случай, когда поток ещё не успел стартовать.
        # Ответим 200 — Telegram сам попробует повторить.
        log.error("app_tg is not initialized yet")
        return "ok", 200

    try:
        upd = Update.de_json(data, app_tg.bot)
        # ВАЖНО: кладём апдейт в очередь БЕЗ доп. проверок готовности —
        # PTB обработает его, как только будет готов.
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue update")

    return "ok", 200


# ---------- Фоновый запуск PTB под gunicorn ----------
def _run_bot() -> None:
    """Функция запуска бота в отдельном потоке."""
    global app_tg
    try:
        log.info("Starting PTB application…")
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Регистрируем хендлеры
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        app_tg = application  # делимся ссылкой с Flask

        # Ставим вебхук ВО ВРЕМЯ старта приложения
        async def on_startup(app: Application) -> None:
            log.info("Setting webhook: %s", WEBHOOK_URL)
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            log.info("Webhook is set")

        # Блокирующий цикл PTB — внутри отдельного потока
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            on_startup=[on_startup],
        )
    except Exception:
        log.exception("PTB application crashed")


# Стартуем бот при импорте модуля (когда gunicorn поднимает app_flask)
threading.Thread(target=_run_bot, name="ptb-runner", daemon=True).start()
