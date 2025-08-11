# bot.py
# --- Простая и надежная реализация вебхука на Flask + Telegram HTTP API ---
# Работает на Render/Gunicorn без фоновых потоков и event loop.
# Переменные окружения: BOT_TOKEN, WEBHOOK_SECRET, APP_URL
#
# Эндпоинты:
#   GET  /health                             -> {"status":"ok"}
#   GET  /                                   -> короткий текст и echo стата
#   POST /webhook/<WEBHOOK_SECRET>           -> прием апдейтов от Telegram

from __future__ import annotations

import json
import os
import sys
import time
import typing as t
from dataclasses import dataclass

import httpx
from flask import Flask, abort, jsonify, request

# -------------------- Конфиг --------------------

@dataclass
class Config:
    token: str
    secret: str
    app_url: str

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def webhook_url(self) -> str:
        # Вебхук всегда ведет на /webhook/<secret>
        return f"{self.app_url.rstrip('/')}/webhook/{self.secret}"


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    secret = os.environ.get("WEBHOOK_SECRET") or "mySecret_2025"
    app_url = os.environ.get("APP_URL")

    if not token:
        print("ERROR: BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    if not app_url:
        print("ERROR: APP_URL is not set", file=sys.stderr)
        sys.exit(1)

    return Config(token=token.strip(), secret=secret.strip(), app_url=app_url.strip())


CFG = load_config()

# Единый HTTP-клиент (keep-alive), чтобы не тратить время на соединения
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CLIENT = httpx.Client(timeout=HTTP_TIMEOUT)


def tg_api(method: str, payload: dict) -> dict:
    """Вызов Telegram Bot API с обработкой ошибок."""
    url = f"{CFG.api_base}/{method}"
    try:
        r = CLIENT.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Telegram API error: {data}")
        return data
    except Exception as e:
        print(f"[tg_api] {method} failed: {e}", file=sys.stderr)
        raise


def set_webhook() -> None:
    """Ставит (или переcтавит) вебхук с нужным URL."""
    # Сначала удалим, затем поставим — так надежнее при повторных деплоях
    try:
        tg_api("deleteWebhook", {"drop_pending_updates": False})
    except Exception:
        # Не критично, просто продолжим
        pass

    # Ставим вебхук
    tg_api(
        "setWebhook",
        {
            "url": CFG.webhook_url,
            # Можно ограничить типы апдейтов, если нужно
            "allowed_updates": ["message", "edited_message"],
            # На рендере внешний сертификат не нужен
        },
    )
    print(f"[init] Webhook set -> {CFG.webhook_url}", file=sys.stderr)


# -------------------- Flask app --------------------

app = Flask(__name__)

# Простая внутренняя «статистика» — чтобы видеть, что живём
STATS = {
    "start_ts": int(time.time()),
    "updates": 0,
    "last_update_ts": 0,
}


@app.before_first_request
def _on_startup() -> None:
    # Ставим вебхук при старте воркера gunicorn
    set_webhook()


@app.get("/")
def index():
    return jsonify(
        status="ok",
        webhook_url=CFG.webhook_url,
        updates=STATS["updates"],
        up_seconds=int(time.time()) - STATS["start_ts"],
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


def _handle_message(msg: dict) -> None:
    """Базовая логика: /start -> приветствие, иначе — эхо."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    text = msg.get("text") or ""
    if text.startswith("/start"):
        reply = "Привет! Я на связи 🤖"
    else:
        # Простое эхо
        reply = f"Ты написал: {text}" if text else "Я получил сообщение 🙂"

    tg_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": reply,
            "disable_web_page_preview": True,
        },
    )


@app.post(f"/webhook/{CFG.secret}")
def telegram_webhook():
    """Основной обработчик вебхука Telegram."""
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        abort(400)

    if not isinstance(payload, dict):
        abort(400)

    # Обновим статистику
    STATS["updates"] += 1
    STATS["last_update_ts"] = int(time.time())

    # Обрабатываем только сообщения
    message = payload.get("message") or payload.get("edited_message")
    if message:
        try:
            _handle_message(message)
        except Exception as e:
            # Не роняем веб-хук — логируем и отвечаем 200, чтобы TG не слал ретраи бесконечно
            print(f"[webhook] handle error: {e}\npayload={json.dumps(payload, ensure_ascii=False)}", file=sys.stderr)

    # Telegram ждёт быстрый 200 OK
    return jsonify(ok=True)


# Дополнительно: если хочется видеть «буфер»/вебхук в json (удобно для ручной проверки)
@app.get("/debug")
def debug():
    return jsonify(
        status="ok",
        webhook_url=CFG.webhook_url,
        stats=STATS,
    )


# Точка входа для gunicorn: `gunicorn bot:app` или `gunicorn bot:app_flask`
app_flask = app  # совместимость с твоей текущей Proc/Render командой
