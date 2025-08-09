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

# -------------------- Логирование --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- Переменные окружения --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # напр.: https://my-telegram-bot-xxxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Нужно задать переменные окружения BOT_TOKEN и APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Глобалы --------------------
app_flask = Flask(__name__)

app_tg: Optional[Application] = None
_ptb_ready = threading.Event()  # флаг «PTB полностью готов и вебхук установлен»

# -------------------- Handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# -------------------- PTB запуск в отдельном потоке --------------------
async def _ptb_main() -> None:
    """Создаём и запускаем PTB-приложение и выставляем вебхук."""
    global app_tg

    # 1) Создаём приложение и регистрируем хендлеры
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # 2) Инициализируем и запускаем приложение (без .run_* helpers)
    await application.initialize()
    await application.start()

    # 3) Выставляем вебхук только ПОСЛЕ старта PTB
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set to %s", WEBHOOK_URL)

    # 4) Помечаем «бот готов»
    app_tg = application
    _ptb_ready.set()

    # 5) Держим цикл живым
    await asyncio.Event().wait()


def _ptb_thread_runner() -> None:
    """Точка входа фонового потока для PTB."""
    try:
        asyncio.run(_ptb_main())
    except Exception:
        log.exception("PTB thread crashed")


# Стартуем PTB-поток СРАЗУ при импорте модуля — до того, как Telegram начнёт слать апдейты
_thread = threading.Thread(target=_ptb_thread_runner, daemon=True, name="ptb-thread")
_thread.start()

# -------------------- Flask маршруты --------------------
@app_flask.get("/")
def health() -> tuple[str, int]:
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook_handler():
    """Приём апдейтов от Telegram (Flask)."""
    # Проверяем секрет
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        # Неправильный секрет — отвечаем 403 (это нормально видеть в логах)
        abort(403)

    # JSON апдейта
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200

    log.info("Webhook JSON: %s", data)

    # Если PTB ещё не успел подняться — просто возвращаем 200 и не паникуем
    if not _ptb_ready.is_set() or app_tg is None:
        log.warning("Получено обновление, но приложение PTB ещё не собрано")
        return "ok", 200

    try:
        # Превращаем JSON в Update и пихаем в очередь PTB
        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
    except Exception:
        log.exception("Ошибка при помещении апдейта в очередь PTB")

    return "ok", 200


# -------------------- Локальный запуск (не нужен на Render) --------------------
if __name__ == "__main__":
    # Локально можно гонять так:
    #   export BOT_TOKEN=... APP_URL=http://localhost:8080
    #   python bot.py
    # и слать тестовые POST-запросы на /webhook/<secret>
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
