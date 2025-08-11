# bot.py
# -*- coding: utf-8 -*-
"""
Render + Gunicorn + Flask + python-telegram-bot (v21.x)
Надёжный вебхук с буферизацией и корректным завершением воркера.

Переменные окружения (с дефолтами):
- TELEGRAM_TOKEN      : токен бота (обязательно)
- BASE_URL            : публичный https URL сервиса (напр. https://my-telegram-bot-xxxx.onrender.com)
- WEBHOOK_PATH        : хвост пути вебхука, по умолчанию "mySecret_2025"
- WEBHOOK_SECRET      : секрет для заголовка X-Telegram-Bot-Api-Secret-Token (по умолчанию как WEBHOOK_PATH)
- PORT                : порт сервера (Render задаёт сам), по умолчанию 10000
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from typing import Deque, Optional

from flask import Flask, jsonify, request
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

# ----------------------- Конфиг / Логирование -----------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

BASE_URL = os.getenv("BASE_URL", "").strip()  # напр. https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "mySecret_2025").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", WEBHOOK_PATH).strip()
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_URL = f"{BASE_URL.rstrip('/')}/webhook/{WEBHOOK_PATH}"

# ----------------------- Глобальное состояние PTB -----------------------

app_flask = Flask(__name__)

_app_tg: Optional[Application] = None
_loop_tg = None  # type: Optional[asyncio.AbstractEventLoop]  # hint: создаётся в треде
_ready_evt = threading.Event()  # PTB инициализирован и готов принимать апдейты
_stop_evt = threading.Event()   # просим PTB остановиться

_buffer: Deque[dict] = deque()  # временное хранилище апдейтов до готовности PTB
_BUFFER_MAX = int(os.getenv("BUFFER_MAX", "1000"))

# ----------------------- Хэндлеры бота -----------------------

async def cmd_start(update: Update, _ctx):
    user = update.effective_user
    name = (user.full_name if user else "друг")
    await update.effective_chat.send_message(
        f"Привет, {name}! Я на связи 🤖\n"
        f"Отправь любое сообщение — повторю его обратно.",
        parse_mode=ParseMode.HTML,
    )

async def echo(update: Update, _ctx):
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты сказал: <code>{update.message.text}</code>", parse_mode=ParseMode.HTML)

# ----------------------- Сервисные функции -----------------------

def _submit_update_json(data: dict) -> None:
    """Преобразовать JSON апдейта в Update и безопасно отдать PTB."""
    global _app_tg, _loop_tg
    if not (_app_tg and _loop_tg and _ready_evt.is_set()):
        return

    try:
        upd = Update.de_json(data, _app_tg.bot)
    except Exception as e:
        log.exception("Bad update JSON, skip: %s", e)
        return

    # process_update — корутина; исполняем её внутри event-loop PTB
    import asyncio
    fut = asyncio.run_coroutine_threadsafe(_app_tg.process_update(upd), _loop_tg)
    # не блокируем — но подвешиваем обработку исключений (иначе молча потеряем)
    def _done(_f):
        try:
            _f.result()
        except Exception:
            log.exception("PTB process_update failed")
    fut.add_done_callback(_done)

def _drain_buffer(tag: str) -> int:
    """Слить накопившиеся апдейты в PTB. Возвращает кол-во слитых."""
    drained = 0
    while _ready_evt.is_set() and _buffer:
        data = _buffer.popleft()
        _submit_update_json(data)
        drained += 1
    if drained:
        log.info("Buffer drain (%s): flushed %d update(s)", tag, drained)
    return drained

# ----------------------- Запуск/останов PTB в отдельном треде -----------------------

def _ptb_thread():
    """Бэкграунд-тред: собственный event-loop + запуск PTB Application."""
    import asyncio

    global _app_tg, _loop_tg

    try:
        _loop_tg = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop_tg)

        _app_tg = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .concurrent_updates(True)  # распараллеливание обработок
            .build()
        )

        # маршруты
        _app_tg.add_handler(CommandHandler("start", cmd_start))
        _app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        async def _async_bootstrap():
            await _app_tg.initialize()
            await _app_tg.start()
            log.info("PTB: initialized & started")
            # ставим вебхук (на всякий случай ещё здесь)
            if BASE_URL:
                try:
                    await _app_tg.bot.delete_webhook(drop_pending_updates=False)
                    await _app_tg.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
                    log.info("PTB: webhook confirmed at %s", WEBHOOK_URL)
                except Exception:
                    log.exception("PTB: set_webhook failed (background)")

        _loop_tg.run_until_complete(_async_bootstrap())

        _ready_evt.set()
        # сразу после старта — слить всё, что накопилось
        _drain_buffer("startup")

        # держим луп живым до сигнала на останов
        while not _stop_evt.is_set():
            _loop_tg.run_until_complete(asyncio.sleep(0.2))

        # Аккуратная остановка
        async def _async_shutdown():
            try:
                await _app_tg.stop()
            finally:
                await _app_tg.shutdown()

        _loop_tg.run_until_complete(_async_shutdown())
        log.info("PTB: stopped")

    except Exception:
        log.exception("PTB thread crashed")
    finally:
        try:
            import asyncio
            if _loop_tg and _loop_tg.is_running():
                _loop_tg.stop()
        except Exception:
            pass

# Запускаем PTB-тред максимально рано
_t = threading.Thread(target=_ptb_thread, name="ptb-thread", daemon=True)
_t.start()

# ----------------------- Flask: маршруты -----------------------

@app_flask.get("/")
def health():
    # просто "живой" ответ для Render
    return "OK", 200

@app_flask.post(f"/webhook/{WEBHOOK_PATH}")
def webhook():
    # Проверяем секретный заголовок Telegram
    hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if hdr != WEBHOOK_SECRET:
        return "forbidden", 403

    try:
        data = request.get_json(force=True, silent=False)  # гарантированно словарь
    except Exception:
        log.exception("Webhook: bad JSON")
        return "bad json", 400

    # Если PTB уже готов — сразу отдаём в него
    if _ready_evt.is_set():
        _submit_update_json(data)
        # На случай, если прямо сейчас перешли в ready — добьём буфер
        _drain_buffer("webhook")
        return jsonify(ok=True)

    # Иначе — буферизуем (ограничиваем рост)
    if len(_buffer) >= _BUFFER_MAX:
        # старое выбрасываем, чтобы не разрастаться до бесконечности
        _buffer.popleft()
    _buffer.append(data)
    log.warning("Buffered update while PTB not ready (queue=%d)", len(_buffer))
    return jsonify(ok=True)

# ----------------------- Установка вебхука при старте воркера -----------------------

def _set_webhook_once():
    """Поставить вебхук сразу же при старте воркера gunicorn."""
    if not BASE_URL:
        log.warning("BASE_URL is empty — webhook will not be set automatically")
        return
    try:
        import httpx
        # Удаляем и ставим заново (надёжнее при частых рестартах)
        with httpx.Client(timeout=10.0) as cl:
            cl.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
            r = cl.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                json={"url": WEBHOOK_URL, "secret_token": WEBHOOK_SECRET},
            )
            r.raise_for_status()
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook via HTTP")

# Ставим вебхук синхронно — это быстро, чтобы Telegram не слал в старое место
_set_webhook_once()

# ----------------------- Корректное завершение -----------------------

def _graceful_shutdown(reason: str):
    """Вызывается при SIGTERM/atexit: дожимаем буфер и аккуратно гасим PTB."""
    log.info("Shutdown requested (%s). Flushing buffer & stopping PTB ...", reason)
    # Сначала помечаем, что пора завершаться
    _stop_evt.set()
    # Попробуем слить буфер (если PTB уже успел стартовать)
    _drain_buffer("shutdown")
    # Немного подождём, чтобы PTB успел обработать уже отправленное
    t0 = time.time()
    while _ready_evt.is_set() and (time.time() - t0) < 2.0 and _buffer:
        time.sleep(0.05)
    try:
        _t.join(timeout=3.0)
    except Exception:
        pass

def _on_sigterm(_sig, _frm):
    _graceful_shutdown("SIGTERM")

signal.signal(signal.SIGTERM, _on_sigterm)
atexit.register(lambda: _graceful_shutdown("atexit"))

# ----------------------- Точка входа (для локального запуска) -----------------------

if __name__ == "__main__":
    # Локально: uvicorn/flask встроенный (для отладки). На Render работает gunicorn.
    app_flask.run(host="0.0.0.0", port=PORT)
