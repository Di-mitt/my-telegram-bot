# bot.py
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Deque, Dict, Optional

from flask import Flask, request, abort

from telegram import Update, Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------- логирование -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ----------------- env -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # обязателен
APP_URL = os.getenv("APP_URL")      # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ----------------- Flask app -----------------
app_flask = Flask(__name__)

# ----------------- PTB app + состояние -----------------
app_tg: Optional[Application] = None
_ptb_ready: bool = False

# Буфер входящих апдейтов, пока PTB не готов
BUFFER_MAX = 200
buffered_updates: Deque[Dict] = deque(maxlen=BUFFER_MAX)


# ----------------- handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ----------------- служебное: запуск PTB в фоне -----------------
async def _runner() -> None:
    """
    Поднимаем PTB без его веб-сервера (он нам не нужен — вебхуки принимает Flask).
    После старта ставим вебхук и помечаем приложение как готовое.
    Затем «проглатываем» буфер ранних апдейтов.
    """
    global app_tg, _ptb_ready

    app_tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # регистрация хендлеров
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Инициализация и запуск PTB
    await app_tg.initialize()
    await app_tg.start()
    log.info("PTB application is up")

    # Ставим вебхук c секретом (и очищаем старые)
    await app_tg.bot.delete_webhook(drop_pending_updates=False)
    await app_tg.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set to %s", WEBHOOK_URL)

    # Отмечаем готовность и дренируем буфер
    _ptb_ready = True
    await _drain_buffer()

    # Держим задачу живой
    await asyncio.Event().wait()


async def _drain_buffer() -> None:
    """Преобразуем накопленные JSON → Update и отправляем в очередь PTB."""
    if not app_tg:
        return
    drained = 0
    while buffered_updates:
        raw = buffered_updates.popleft()
        try:
            upd = Update.de_json(raw, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
            drained += 1
        except Exception:  # на всякий случай не роняем обработку
            log.exception("Failed to inject buffered update")
    if drained:
        log.info("Drained %s buffered update(s)", drained)


def _ensure_ptb_background_started() -> None:
    """Стартуем фоновую задачу PTB один раз при первом HTTP-запросе."""
    if getattr(_ensure_ptb_background_started, "_started", False):
        return
    loop = asyncio.new_event_loop()

    def _bg():
        try:
            loop.run_until_complete(_runner())
        finally:
            loop.close()

    import threading
    t = threading.Thread(target=_bg, name="ptb-runner", daemon=True)
    t.start()
    setattr(_ensure_ptb_background_started, "_started", True)


# ----------------- Flask routes -----------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    # Запускаем PTB при первом обращении (Render делает HEAD/GET на health)
    _ensure_ptb_background_started()
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook() -> tuple[str, int]:
    # гарантируем запуск PTB (на всякий)
    _ensure_ptb_background_started()

    # Проверяем секрет
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json(silent=True, force=True) or {}
    log.info("Webhook JSON: %s", data)

    # Если PTB ещё стартует — буферизуем
    if not _ptb_ready or not app_tg:
        buffered_updates.append(data)
        log.warning("Received update while PTB not ready yet (buffer=%d)", len(buffered_updates))
        return "ok", 200

    # Иначе отправляем сразу в очередь PTB
    try:
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue update")
    return "ok", 200


# ----------------- локальный запуск -----------------
if __name__ == "__main__":
    # Для локальных тестов: Flask + фоновый PTB
    _ensure_ptb_background_started()
    app_flask.run("0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    
