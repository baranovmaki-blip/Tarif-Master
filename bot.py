"""
Тариф-Мастер — Telegram-бот с каталогом тарифов Билайн внутри WebApp
(мини-приложения), с простой CRM прямо в Telegram. Никаких вопросов
клиенту — он сам листает тарифы в WebApp, а на решающих этапах бот может
подключить менеджера:

  /start -> заводит заказ (уникальный номер #1001, #1002...) -> кнопка
     "Открыть каталог тарифов" (открывает webapp.html, см. WEBAPP_URL) ->
     клиент выбирает тариф и жмёт "Купить" внутри WebApp -> WebApp
     закрывается и передаёт выбор боту (Telegram.WebApp.sendData) ->
     web_app_data_received сразу создаёт платёж в ЮKassa и присылает ссылку
     в чат (без повторного подтверждения — клиент уже подтвердил покупку
     кнопкой в WebApp), дальше фоновая проверка оплаты. В любой момент
     клиент может позвать менеджера сам, бот сам предложит менеджера при
     возражении ("дорого", "подумаю" и т.п.), а если клиент "завис" — бот
     сам напоминает о себе с растущим интервалом, пока не ответит.
  -> после оплаты бот САМ запрашивает ссылку на подключение у Билайна
     (mycompany.beeline.ru + IMAP) и пересылает клиенту — без ручных команд.
     Если автоматика не справилась — уведомляет администратора с кнопками
     "Подключить вручную"/"Отказать" как запасной путь.

Все заказы хранятся в словаре orders (см. ORDERS_FILE) и переживают
перезапуск бота — статусы: new / paid / connected / declined.

Запуск: python bot.py
Каталог тарифов (внешний вид) — в webapp.html рядом с этим файлом; его
нужно разместить на публичном https-адресе, см. README.md.
Зависимости и деплой, админ-команды — см. README.md рядом с этим файлом.
"""

from __future__ import annotations  # чтобы аннотации вида `X | None` работали и на Python 3.9

import asyncio
import email as email_lib
import imaplib
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from datetime import timezone as dt_timezone

import aiohttp
import anthropic
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ==========================================================================
# === НАСТРОЙКИ. Все секреты и реальные значения — в файле .env рядом с   ===
# === этим файлом (он в .gitignore, в git не попадает — см. .env.example ===
# === как образец). Здесь только заглушки на случай, если .env не создан. ===
# ==========================================================================
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "YOUR_CLAUDE_API_KEY")

# Основной админ (вы). Уведомления и админ-команды доступны отсюда.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "YOUR_ADMIN_CHAT_ID")
# Необязательный второй админ/менеджер — тоже получает уведомления и может
# пользоваться командами CRM. Оставьте заглушку, если не нужен.
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "YOUR_MANAGER_CHAT_ID")

# ЮKassa — оформление и проверка реальных платежей.
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "YOUR_SHOP_ID")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "YOUR_SECRET_KEY")
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

# Telegram-username менеджера (без @), куда бот отправляет клиента при
# нажатии "Связаться с менеджером" — открывается личный чат напрямую.
MANAGER_TELEGRAM_USERNAME = os.environ.get("MANAGER_TELEGRAM_USERNAME", "MinBar_Co")

# Публичный HTTPS-адрес, где реально размещён webapp.html (Telegram WebApp
# ОБЯЗАН открываться по https — локальный файл или http не подойдут).
# Пока не задан — кнопка "Открыть каталог тарифов" не покажется, см. main().
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

# Файл, в который сохраняются все заказы (CRM), чтобы не терять их при
# перезапуске бота.
ORDERS_FILE = "orders.json"

# URL веб-приложения Google Apps Script — сюда бот дублирует каждый заказ
# при любом изменении статуса. Код самого Apps Script — в google_apps_script.gs.
GOOGLE_SHEETS_URL = os.environ.get("GOOGLE_SHEETS_URL", "")

# Часовой пояс для еженедельного отчёта (по умолчанию МСК, UTC+3).
TIMEZONE_OFFSET_HOURS = int(os.environ.get("TIMEZONE_OFFSET_HOURS", "3"))

# ===== Автоматическое получение ссылки на подключение (mycompany.beeline.ru) =====
# Страница "Мой Билайн для бизнеса" — вводите корпоративную почту, на неё
# приходит одноразовая ссылка. Селекторы (input[name="email"], button[type=
# "submit"]) проверены вживую на реальной странице на момент написания кода.
BEELINE_AUTH_URL = "https://mycompany.beeline.ru/auth"

# Эта же почта используется и как логин на mycompany.beeline.ru, и как ящик,
# который бот проверяет через IMAP в поисках письма с одноразовой ссылкой.
EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "YOUR_EMAIL@gmail.com")
# Пароль приложения Gmail (НЕ обычный пароль от аккаунта) — как получить,
# см. README.md.
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "YOUR_GMAIL_APP_PASSWORD")
IMAP_SERVER = "imap.gmail.com"

# Адрес отправителя письма с одноразовой ссылкой — подтверждён по реальному
# письму от Билайна (не угадан). Латиницей — поэтому можно искать прямо
# через IMAP SEARCH FROM, без обходных путей для кириллицы.
BEELINE_SENDER_EMAIL = os.environ.get("BEELINE_SENDER_EMAIL", "b2bmycompany@beeline.ru")

CONFIRMATION_EMAIL_TIMEOUT_SECONDS = 90  # сколько ждать письмо, прежде чем сдаться
CONFIRMATION_EMAIL_POLL_INTERVAL_SECONDS = 5  # как часто перепроверять почту

CLAUDE_MODEL = "claude-opus-5"
CONNECTION_FEE_RUB = 1500

PAYMENT_CHECK_INTERVAL_SECONDS = 30       # как часто проверять все ожидающие платежи
PAYMENT_REMINDER_DELAY_SECONDS = 30 * 60  # через сколько напомнить неоплатившему
IDLE_NUDGE_DELAY_SECONDS = 3 * 60         # первое напоминание — через 3 минуты
# Дальше напоминания повторяются, пока клиент не ответит (любая кнопка/сообщение
# отменяет цепочку). Разрыв между повторами каждый раз растёт на этот шаг:
# 3 мин -> +5 (8 мин) -> +10 (18 мин) -> +15 (33 мин) -> +20 ...
IDLE_NUDGE_STEP_SECONDS = 5 * 60
# ==========================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ===== Тарифы (вшиты в код, как просили) =====
# gb: число гигабайт, либо строка "безлимит". minutes: число минут, либо None.
#
# Пока что оставлен только Билайн (по просьбе — "непубличные тарифы Билайн"
# как маркетинговая подача). Другие операторы не удалены навсегда — просто
# закомментированы, чтобы вернуть в любой момент одной правкой.
TARIFFS = {
    # Реальные тарифы с mycompany.beeline.ru (со скриншотов пользователя), а
    # не выдуманные — включая семейство "Пробизнес" на разных тиерах минут
    # (некоторые со скидкой 25-35% на первые 12 месяцев — это отражено в
    # "short"). Названия с одинаковым "Пробизнес N.0" различаются по числу
    # минут в скобках, чтобы не было двух одинаковых кнопок в каталоге.
    "Билайн": [
        {
            "name": "Пробизнес 1.0 (600 мин)",
            "price": 229,
            "minutes": 600,
            "gb": "безлимит",
            "sms": 300,
            "short": "Скидка 25% на первые 12 месяцев",
        },
        {
            "name": "Пробизнес 1.0 (900 мин)",
            "price": 270,
            "minutes": 900,
            "gb": "безлимит",
            "sms": 300,
            "short": "Скидка 25% на первые 12 месяцев",
        },
        {
            "name": "Решение за 300",
            "price": 305,
            "minutes": 500,
            "gb": "безлимит",
            "sms": 100,
            "short": "Безлимитный интернет по доступной цене",
        },
        {
            "name": "Пробизнес 3.0 (2000 мин)",
            "price": 364,
            "minutes": 2000,
            "gb": "безлимит",
            "sms": 1000,
            "short": "Скидка 35% на первые 12 месяцев",
        },
        {
            "name": "Решение за 400",
            "price": 407,
            "minutes": 800,
            "gb": 35,
            "sms": 300,
            "short": "35 ГБ плюс безлимитный интернет по России",
        },
        {
            "name": "Пробизнес 2.0 (1500 мин)",
            "price": 460,
            "minutes": 1500,
            "gb": "безлимит",
            "sms": 500,
            "short": "Баланс интернета и звонков для бизнеса",
        },
        {
            "name": "Гибкий интернет 2.0",
            "price": 500,
            "minutes": None,
            "gb": "безлимит",
            "sms": None,
            "short": "Безлимитный интернет, звонки на Билайн бесплатно",
        },
        {
            "name": "Решение за 550",
            "price": 559,
            "minutes": 1800,
            "gb": 60,
            "sms": 1000,
            "short": "Много интернета и SMS для активного общения",
        },
        {
            "name": "Пробизнес 3.0 (2500 мин)",
            "price": 610,
            "minutes": 2500,
            "gb": "безлимит",
            "sms": 1000,
            "short": "Для среднего бизнеса",
        },
        {
            "name": "Пробизнес 3.0 (3000 мин)",
            "price": 715,
            "minutes": 3000,
            "gb": "безлимит",
            "sms": 1000,
            "short": "Для активного бизнес-общения",
        },
        {
            "name": "Пробизнес 3.0 (5000 мин)",
            "price": 1020,
            "minutes": 5000,
            "gb": "безлимит",
            "sms": 1000,
            "short": "Много минут для компании",
        },
        {
            "name": "Пробизнес 3.0 (6000 мин)",
            "price": 3000,
            "minutes": 6000,
            "gb": "безлимит",
            "sms": 2000,
            "short": "Максимум минут и SMS",
        },
    ],
    # "МТС": [
    #     {"name": "Тариф X", "price": 300, "minutes": 500, "gb": 40},
    #     {"name": "Тариф Y", "price": 380, "minutes": 800, "gb": 30},
    # ],
    # "Мегафон": [
    #     {"name": "Тариф Z", "price": 320, "minutes": 400, "gb": 35},
    #     {"name": "Тариф W", "price": 400, "minutes": 1200, "gb": 25},
    # ],
    # "Tele2": [
    #     {"name": "Тариф А", "price": 300, "minutes": 300, "gb": 50},
    #     {"name": "Тариф B", "price": 350, "minutes": 600, "gb": 40},
    # ],
    # "Yota": [
    #     {"name": "Yota безлимит", "price": 390, "minutes": None, "gb": "безлимит"},
    # ],
}
DEFAULT_OPERATOR = "Билайн"  # раз оператор один — выбираем его автоматически, шаг выбора убран

# Фразы, по которым бот считает, что клиент возражает, и сам предлагает
# менеджера. Простое совпадение по подстроке — надёжнее и быстрее, чем
# спрашивать об этом Claude на каждое сообщение. Рекомендации, как расширить
# это AI-классификацией, если понадобится, — в README.
OBJECTION_KEYWORDS = ("дорог", "подума", "не увер", "дешевле", "сравни")

# ==========================================================================
# === CRM: ЗАКАЗЫ (в памяти + сохранение в ORDERS_FILE) ===
# ==========================================================================
# orders: номер заказа -> данные. Статусы: new / paid / connected / declined.
# Пример записи — см. README.md.
orders: dict[int, dict] = {}
_next_order_number = 1001  # первый заказ получит именно этот номер


def _save_orders() -> None:
    try:
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"next_order_number": _next_order_number, "orders": orders},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        logger.exception("Не удалось сохранить %s", ORDERS_FILE)


def _load_orders() -> None:
    """Вызывается один раз при старте — восстанавливает заказы после перезапуска."""
    global _next_order_number
    if not os.path.exists(ORDERS_FILE):
        return
    try:
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _next_order_number = data.get("next_order_number", 1001)
        # Ключи в JSON всегда строки — возвращаем номерам заказов тип int.
        orders.update({int(k): v for k, v in data.get("orders", {}).items()})
        logger.info("Загружено заказов из %s: %d", ORDERS_FILE, len(orders))
    except Exception:
        logger.exception("Не удалось загрузить %s", ORDERS_FILE)


def _generate_order_number() -> int:
    global _next_order_number
    number = _next_order_number
    _next_order_number += 1
    return number


def _set_order_status(order_number: int, new_status: str, *, timestamp_field: str | None = None) -> str:
    """Меняет статус заказа и сохраняет на диск. Возвращает старый статус."""
    order = orders[order_number]
    old_status = order["status"]
    order["status"] = new_status
    if timestamp_field:
        order[timestamp_field] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_orders()
    return old_status


async def send_to_google_sheets(context: ContextTypes.DEFAULT_TYPE, order_number: int) -> None:
    """Дублирует текущее состояние заказа в Google Sheets (через Apps Script).
    Если Sheets недоступен — данные никуда не теряются (они уже сохранены в
    orders.json), просто уведомляем админа, что синхронизация не прошла."""
    if not GOOGLE_SHEETS_URL or GOOGLE_SHEETS_URL == "YOUR_GOOGLE_SHEETS_URL":
        return  # интеграция ещё не настроена — молча пропускаем

    order = orders.get(order_number)
    if order is None:
        return

    payload = {
        "order_number": order_number,
        "created_at": order["created_at"],
        "name": order["name"],
        "phone": order["phone"],
        "telegram_id": order["user_id"],
        "operator": order["operator"],
        "tariff": order["tariff"],
        "price": order["price"],
        "status": order["status"],
        "paid_at": order["paid_at"],
        "connected_at": order["connected_at"],
        "comment": order.get("comment", ""),
    }

    try:
        session = await _get_http_session()
        async with session.post(GOOGLE_SHEETS_URL, json=payload) as response:
            if response.status != 200:
                raise RuntimeError(f"Google Sheets вернул статус {response.status}")
    except Exception:
        logger.exception("Не удалось отправить заказ #%s в Google Sheets", order_number)
        await notify_admins(
            context,
            f"⚠️ Не удалось синхронизировать заказ #{order_number} с Google Sheets "
            "(данные не потеряны — сохранены в orders.json).",
        )


async def _update_order_status(
    context: ContextTypes.DEFAULT_TYPE,
    order_number: int,
    new_status: str,
    *,
    timestamp_field: str | None = None,
) -> str:
    """Единая точка смены статуса: локально (orders.json) + Google Sheets."""
    old_status = _set_order_status(order_number, new_status, timestamp_field=timestamp_field)
    await send_to_google_sheets(context, order_number)
    return old_status


# ===== Состояния диалога (ConversationHandler) =====
# Каталог и карточки тарифов теперь живут внутри WebApp (webapp.html) —
# /start только открывает его кнопкой. Дальше клиент листает тарифы уже в
# мини-приложении, а не в чате; в чат он возвращается один раз — с выбором
# тарифа (см. web_app_data_received) — весь путь укладывается в одно
# состояние ConversationHandler.
BROWSING = 0

_HUMAN_BUTTON_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("💬 Связаться с менеджером", callback_data="human")]]
)
_HUMAN_CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Да, свяжите меня с менеджером", callback_data="human")]]
)


# ==========================================================================
# === АДМИНЫ (один или два чата — вы и, опционально, менеджер) ===
# ==========================================================================
def _valid_admin_ids() -> list[str]:
    placeholders = {"", "YOUR_ADMIN_CHAT_ID", "YOUR_MANAGER_CHAT_ID"}
    return [cid for cid in (ADMIN_CHAT_ID, MANAGER_CHAT_ID) if cid not in placeholders]


ADMIN_CHAT_IDS = _valid_admin_ids()


def is_admin_chat(chat_id) -> bool:
    return str(chat_id) in ADMIN_CHAT_IDS


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Не удалось отправить уведомление админу %s", chat_id)


async def _notify_status_change(
    context: ContextTypes.DEFAULT_TYPE,
    order_number: int,
    old_status: str,
    new_status: str,
    reply_markup=None,
) -> None:
    order = orders[order_number]
    text = (
        f"🔄 Статус заказа #{order_number} изменён:\n"
        f"   {old_status} → {new_status}\n\n"
        f"👤 Клиент: {order['name']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"📌 Тариф: {order['tariff']}\n"
        f"💰 Сумма: {order['price']} ₽"
    )
    await notify_admins(context, text, reply_markup=reply_markup)


def _cancel_job(context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def _looks_like_objection(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in OBJECTION_KEYWORDS)


# ==========================================================================
# === ИНТЕГРАЦИЯ С CLAUDE (с шаблонным запасным вариантом) ===
# ==========================================================================
def _claude_client() -> anthropic.Anthropic | None:
    if not CLAUDE_API_KEY or CLAUDE_API_KEY == "YOUR_CLAUDE_API_KEY":
        return None
    return anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def _tariffs_catalog_for_prompt() -> str:
    """Обычный текст (без HTML) со всеми тарифами — для системного промпта Claude."""
    lines = []
    for t in TARIFFS[DEFAULT_OPERATOR]:
        gb_text = "безлимитный интернет" if t["gb"] == "безлимит" else f"{t['gb']} ГБ"
        minutes_text = f"{t['minutes']} мин, " if t["minutes"] is not None else ""
        lines.append(f"«{t['name']}» — {t['price']} ₽/мес, {minutes_text}{gb_text}")
    return "\n".join(lines)


def get_ai_answer(question: str, operator: str) -> str:
    """Ответ на произвольный вопрос клиента (например «Какие условия?», «А что по Пробизнес 2.0?»)."""
    fallback = (
        "Спасибо за вопрос! Подключение занимает немного времени и стоит "
        f"{CONNECTION_FEE_RUB} ₽ разово. Нажмите «Оплатить», чтобы оформить, "
        "или уточните что-то ещё."
    )

    client = _claude_client()
    if client is None:
        return fallback

    system_prompt = (
        "Ты — консультант компании «Тариф-Мастер», помогаешь клиентам подбирать и "
        "подключать тарифы сотовой связи. Отвечай кратко (2-3 предложения), по-русски, "
        "дружелюбно, с маркетинговым напором (это непубличные тарифы, экономия до 90%). "
        f"Факты: подключение стоит {CONNECTION_FEE_RUB} ₽ разово, делается удалённо, "
        f"без визита в салон. Доступные тарифы {operator}:\n{_tariffs_catalog_for_prompt()}\n\n"
        "Как происходит подключение (если спросят) — ровно в этом порядке: "
        "1) мы присылаем ссылку на подключение непубличного тарифа; "
        "2) клиент переходит по ссылке и указывает номер телефона; "
        "3) на указанный номер приходит код подтверждения; "
        "4) тариф подключается в течение 1 часа."
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return text.strip() or fallback
    except Exception:
        logger.exception("Claude API недоступен — используем шаблонный ответ")
        return fallback


# ==========================================================================
# === ИНТЕГРАЦИЯ С ЮKASSA (создание и проверка платежей через aiohttp) ===
# ==========================================================================
_http_session: aiohttp.ClientSession | None = None


async def _get_http_session() -> aiohttp.ClientSession:
    """Одна общая aiohttp-сессия на всё время работы бота (так рекомендует aiohttp)."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def _yookassa_auth() -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(login=YOOKASSA_SHOP_ID, password=YOOKASSA_SECRET_KEY)


async def create_yookassa_payment(amount_rub: int, description: str) -> tuple[str, str]:
    """Создаёт платёж в ЮKassa. Возвращает (payment_id, ссылка на оплату)."""
    session = await _get_http_session()

    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{BOT_TOKEN.split(':')[0]}"},
        "capture": True,
        "description": description,
    }
    headers = {
        "Idempotence-Key": str(uuid.uuid4()),  # обязателен для ЮKassa, чтобы не задвоить платёж
        "Content-Type": "application/json",
    }

    async with session.post(
        YOOKASSA_API_URL, json=payload, headers=headers, auth=_yookassa_auth()
    ) as response:
        data = await response.json()
        if response.status not in (200, 201):
            raise RuntimeError(f"ЮKassa вернула ошибку {response.status}: {data}")

    return data["id"], data["confirmation"]["confirmation_url"]


async def get_yookassa_payment_status(payment_id: str) -> str:
    """Возвращает статус платежа: pending / waiting_for_capture / succeeded / canceled."""
    session = await _get_http_session()
    url = f"{YOOKASSA_API_URL}/{payment_id}"

    async with session.get(url, auth=_yookassa_auth()) as response:
        data = await response.json()
        if response.status != 200:
            raise RuntimeError(f"ЮKassa вернула ошибку {response.status}: {data}")

    return data["status"]


# ==========================================================================
# === АВТОМАТИЧЕСКОЕ ПОДКЛЮЧЕНИЕ ЧЕРЕЗ mycompany.beeline.ru + ПОЧТУ (IMAP) ===
# ==========================================================================
# Как это работает: заходим на страницу "Мой Билайн для бизнеса", вводим
# корпоративную почту, жмём "Далее" — на почту приходит одноразовая ссылка на
# подключение. Дальше проверяем почту через IMAP, находим письмо, достаём
# ссылку и пересылаем клиенту.
#
# ВАЖНО — честно о рисках этого подхода (тот же сайт, что и в переписке):
# - Селекторы (input[name="email"], button[type="submit"]) проверены вживую
#   на реальной странице на момент написания, но mycompany.beeline.ru — это
#   сторонний сайт, который может измениться в любой момент без предупреждения
#   и сломать автоматизацию. Если это произойдёт — бот не упадёт (ошибки
#   пойманы), но перестанет получать ссылки автоматически и будет сообщать
#   об этом в админ-чат — тогда придётся посмотреть на сайт заново и поправить
#   селекторы здесь.
# - Реальная отправка формы протестирована пользователем вживую (со своей
#   настоящей почтой) — письмо от Билайна пришло, отправитель подтверждён:
#   b2bmycompany@beeline.ru (BEELINE_SENDER_EMAIL). Раньше поиск шёл по
#   угаданному слову в теме письма — заменили на поиск по этому реальному,
#   латиницей, адресу отправителя, что и надёжнее, и быстрее.


async def request_beeline_link() -> None:
    """Заходит на mycompany.beeline.ru, вводит EMAIL_ACCOUNT и жмёт "Далее" —
    Билайн присылает одноразовую ссылку на эту почту."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(BEELINE_AUTH_URL, wait_until="networkidle", timeout=30000)
            await page.locator('input[name="email"]').fill(EMAIL_ACCOUNT)
            await page.locator('button[type="submit"]').click()
            await page.wait_for_timeout(2000)  # даём странице обработать отправку
        finally:
            await browser.close()


def _extract_email_body(msg: email_lib.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="ignore")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(errors="ignore") if payload else ""


def _check_inbox_once(sender_email: str) -> str | None:
    """Синхронная проверка почты (imaplib не умеет в asyncio) — ищет самое
    свежее сегодняшнее письмо от sender_email, достаёт из него первую ссылку.

    Ищем по адресу отправителя (латиница, ASCII-safe — можно доверить прямо
    IMAP SEARCH FROM), а не по теме письма: тема часто на кириллице, и сам
    протокол IMAP не поддерживает такое в обычных SEARCH-строках без ручной
    возни с "literal"-форматом. И не ограничиваемся непрочитанными — письмо
    могли открыть в самом Gmail до того, как бот успел проверить."""
    # timeout обязателен: без него imaplib может зависнуть НАВСЕГДА, если
    # сервер молчит вместо явной ошибки (например, после частых подключений
    # подряд) — тогда внешний таймаут в get_confirmation_link не спасёт,
    # потому что он проверяется только МЕЖДУ вызовами этой функции.
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=20)
    try:
        imap.login(EMAIL_ACCOUNT, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        # SINCE — чтобы не перебирать старые письма от этого же отправителя,
        # если они были раньше; сам адрес и SINCE оба ASCII, IMAP SEARCH
        # отрабатывает мгновенно (в отличие от постраничного разбора вручную).
        today_imap = date.today().strftime("%d-%b-%Y")  # формат IMAP, например 19-Aug-2026
        status, data = imap.search(None, "FROM", sender_email, "SINCE", today_imap)
        if status != "OK" or not data[0]:
            return None

        latest_id = data[0].split()[-1]  # IMAP отдаёт id по возрастанию — последний самый свежий
        status, msg_data = imap.fetch(latest_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return None

        msg = email_lib.message_from_bytes(msg_data[0][1])
        body = _extract_email_body(msg)

        # Письмо обычно HTML — ссылка в href="..." стоит вплотную к кавычке/тегу,
        # поэтому останавливаемся не только на пробеле, но и на " ' < > (иначе
        # в ссылку попадёт мусор вроде "">Подключить</a>).
        match = re.search(r'https?://[^\s"\'<>]+', body)
        return match.group(0) if match else None
    finally:
        imap.logout()


async def get_confirmation_link(sender_email: str, timeout_seconds: int) -> str | None:
    """Опрашивает почту каждые CONFIRMATION_EMAIL_POLL_INTERVAL_SECONDS секунд,
    пока не найдёт письмо со ссылкой или не истечёт timeout_seconds. Одна
    неудачная попытка (например, временный сбой IMAP) не обрывает весь
    процесс — пробуем ещё раз на следующем шаге, пока не кончится время."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            link = await asyncio.to_thread(_check_inbox_once, sender_email)
        except Exception:
            logger.exception("Сбой при проверке почты — пробуем ещё раз")
            link = None
        if link:
            return link
        await asyncio.sleep(CONFIRMATION_EMAIL_POLL_INTERVAL_SECONDS)
    return None


# Все заявки на mycompany.beeline.ru уходят с ОДНОЙ и той же почты, и код
# ищет "последнее письмо от Билайна" в общем ящике — если два заказа
# оплатят почти одновременно, оба запроса будут ждать письмо параллельно и
# рискуют забрать одну и ту же (последнюю) ссылку себе. Лок заставляет
# заказы проходить через Билайн/почту строго по одному, в порядке очереди —
# при пачке одновременных оплат клиенты получат ссылку с небольшой
# задержкой друг за другом, зато без риска перепутать ссылки.
_beeline_lock = asyncio.Lock()


async def auto_register_tariff(context: ContextTypes.DEFAULT_TYPE, order_number: int) -> str | None:
    """Запрашивает у mycompany.beeline.ru одноразовую ссылку и достаёт её из
    письма через IMAP. Возвращает ссылку или None — в случае None сама
    уведомляет админов, почему не получилось, так что вызывающему коду
    достаточно проверить результат на None."""
    async with _beeline_lock:
        try:
            await request_beeline_link()
        except Exception:
            logger.exception("Не удалось отправить запрос на %s (заказ #%s)", BEELINE_AUTH_URL, order_number)
            await notify_admins(
                context,
                f"⚠️ Не удалось открыть/заполнить форму на mycompany.beeline.ru для заказа "
                f"#{order_number} — возможно, сайт изменил структуру страницы. "
                "Понадобится получить ссылку и подключить вручную.",
            )
            return None

        link = await get_confirmation_link(BEELINE_SENDER_EMAIL, CONFIRMATION_EMAIL_TIMEOUT_SECONDS)

        if link is None:
            logger.warning("Письмо с ссылкой не пришло вовремя для заказа #%s", order_number)
            await notify_admins(
                context,
                f"⚠️ Заявка на mycompany.beeline.ru отправлена, но письмо с ссылкой не пришло "
                f"за {CONFIRMATION_EMAIL_TIMEOUT_SECONDS} сек. Проверьте почту {EMAIL_ACCOUNT} "
                f"вручную и отправьте ссылку клиенту (заказ #{order_number}).",
            )
            return None

        return link


# ==========================================================================
# === ОБРАБОТЧИКИ ДИАЛОГА С КЛИЕНТОМ ===
# ==========================================================================
_CATALOG_INTRO_TEXT = (
    "📱 <b>ТАРИФЫ БИЛАЙН</b>\n\n"
    "Подключаем выгодные тарифы. Официальный партнёр.\n\n"
    "👇 Откройте каталог и выберите тариф:"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Заводит заказ в CRM и открывает кнопку WebApp с каталогом тарифов —
    сам список и карточки теперь внутри мини-приложения (webapp.html), не в
    чате; никаких вопросов клиенту, тариф выбирает он сам."""
    context.user_data.clear()
    context.user_data["operator"] = DEFAULT_OPERATOR  # пока только Билайн — шаг выбора оператора убран

    user = update.effective_user

    order_number = _generate_order_number()
    orders[order_number] = {
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "name": user.full_name,
        # Телефон и тариф не спрашиваем вопросами — тариф проставится, когда
        # клиент выберет его в WebApp и нажмёт "Купить" (см. web_app_data_received);
        # телефон уточняет менеджер или форма подключения после оплаты.
        "phone": "не указан",
        "operator": DEFAULT_OPERATOR,
        "tariff": "уточняется у клиента",
        "price": CONNECTION_FEE_RUB,
        "status": "new",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "paid_at": None,
        "connected_at": None,
        "payment_id": None,
        "comment": "",  # свободное поле — можно дописать вручную прямо в Google Sheets
    }
    _save_orders()
    context.user_data["order_number"] = order_number
    await send_to_google_sheets(context, order_number)

    await notify_admins(
        context,
        f"🆕 Новый заказ #{order_number}\n👤 Клиент: {user.full_name} (@{user.username or 'без username'}, id {user.id})",
    )

    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟡 Открыть каталог тарифов", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    else:
        # WEBAPP_URL ещё не настроен (см. README) — предупреждаем админа один
        # раз за сессию и не показываем клиенту нерабочую кнопку.
        logger.warning("WEBAPP_URL не задан — кнопка каталога не будет показана клиенту.")
        keyboard = _HUMAN_BUTTON_KEYBOARD

    await update.message.reply_text(
        _CATALOG_INTRO_TEXT,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    # Если клиент "завис" и 3 минуты не жмёт ни одну кнопку — сами напомним о себе,
    # а дальше повторяем с растущим интервалом, пока он не ответит (см. idle_nudge_job).
    context.job_queue.run_once(
        idle_nudge_job,
        when=IDLE_NUDGE_DELAY_SECONDS,
        chat_id=update.effective_chat.id,
        user_id=user.id,
        data={
            "chat_id": update.effective_chat.id,
            "user_id": user.id,
            "next_interval": IDLE_NUDGE_STEP_SECONDS,
        },
        name=f"idle_{user.id}",
    )

    return BROWSING


async def web_app_data_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WebApp закрылся и передал выбор клиента (Telegram.WebApp.sendData) —
    клиент уже подтвердил покупку кнопкой "Купить" внутри WebApp, поэтому
    сразу создаём платёж в ЮKassa, без лишнего повторного подтверждения в чате."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        tariff_name = payload["tariff"]
    except (ValueError, KeyError, AttributeError):
        await update.message.reply_text("Не разобрал выбор — откройте каталог ещё раз и выберите тариф.")
        return BROWSING

    tariffs = TARIFFS[DEFAULT_OPERATOR]
    tariff = next((t for t in tariffs if t["name"] == tariff_name), None)
    if tariff is None:
        await update.message.reply_text("Такого тарифа не нашёл — откройте каталог ещё раз.")
        return BROWSING

    user = update.effective_user
    _cancel_job(context, f"idle_{user.id}")

    order_number = context.user_data.get("order_number")
    if order_number in orders:
        orders[order_number]["tariff"] = tariff["name"]
        _save_orders()
        await send_to_google_sheets(context, order_number)

    description = f"Тариф-Мастер — подключение тарифа «{tariff['name']}» (Билайн), заказ #{order_number}"

    try:
        payment_id, confirmation_url = await create_yookassa_payment(CONNECTION_FEE_RUB, description)
    except Exception:
        # Даже если оплата онлайн не собралась — менеджер остаётся рабочим
        # путём подключения, клиент не должен упереться в тупик.
        logger.exception("Не удалось создать платёж в ЮKassa")
        await update.message.reply_text(
            f"Для подключения тарифа «{tariff['name']}» напишите нашему менеджеру:\n"
            f"@{MANAGER_TELEGRAM_USERNAME}\n\n"
            "Оплата онлайн сейчас недоступна — сообщите об этом менеджеру."
        )
        await notify_admins(context, f"⚠️ Ошибка создания платежа в ЮKassa по заказу #{order_number}")
        return ConversationHandler.END

    if order_number in orders:
        orders[order_number]["payment_id"] = payment_id
        _save_orders()

    # Напоминание, если клиент не оплатит в течение получаса.
    context.job_queue.run_once(
        remind_payment_job,
        when=PAYMENT_REMINDER_DELAY_SECONDS,
        data={"order_number": order_number, "chat_id": update.effective_chat.id},
        name=f"remind_{order_number}",
    )

    await update.message.reply_text(
        f"Вы выбрали тариф «{tariff['name']}».\n\n"
        f"Для подключения напишите нашему менеджеру:\n"
        f"@{MANAGER_TELEGRAM_USERNAME}\n\n"
        f"Или оплатите онлайн: {confirmation_url}\n\n"
        "После оплаты тариф будет подключён автоматически."
    )
    return ConversationHandler.END


async def human_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Клиент (сам или после подсказки бота при возражении) просит менеджера."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    _cancel_job(context, f"idle_{user.id}")

    order_number = context.user_data.get("order_number")
    await notify_admins(
        context,
        f"📞 Клиент {user.full_name} (@{user.username or 'без username'}, ID: {user.id}), "
        f"заказ #{order_number}, просит связаться с ним.",
    )

    manager_link_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💬 Написать менеджеру", url=f"https://t.me/{MANAGER_TELEGRAM_USERNAME}")]]
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "Сейчас с вами свяжется менеджер в течение 5 минут. "
            "Можете и сами сразу написать — нажмите кнопку ниже."
        ),
        reply_markup=manager_link_keyboard,
    )
    return BROWSING


async def question_during_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Клиент вместо нажатия кнопки написал текст — либо возражение, либо вопрос."""
    text = update.message.text
    operator = context.user_data.get("operator", DEFAULT_OPERATOR)

    if _looks_like_objection(text):
        await update.message.reply_text(
            "Понимаю ваши сомнения. Хотите, я подключу живого менеджера, "
            "который ответит на все вопросы?",
            reply_markup=_HUMAN_CONFIRM_KEYBOARD,
        )
        return BROWSING

    answer = get_ai_answer(text, operator)
    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟡 Открыть каталог тарифов", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    else:
        keyboard = _HUMAN_BUTTON_KEYBOARD
    await update.message.reply_text(answer, reply_markup=keyboard)
    return BROWSING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Хорошо, отменил. Чтобы начать заново — напишите /start.")
    return ConversationHandler.END


async def check_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check_payment — клиент вручную просит перепроверить свой платёж прямо сейчас."""
    user_id = update.effective_user.id
    my_pending = [
        (num, o) for num, o in orders.items()
        if o["user_id"] == user_id and o["status"] == "new" and o.get("payment_id")
    ]

    if not my_pending:
        await update.message.reply_text("У вас нет ожидающих оплаты заказов.")
        return

    for order_number, order in my_pending:
        try:
            status = await get_yookassa_payment_status(order["payment_id"])
        except Exception:
            logger.exception("Не удалось проверить статус платежа по заказу #%s", order_number)
            await update.message.reply_text("Не получилось проверить статус — попробуйте чуть позже.")
            continue

        if status == "succeeded":
            await _finalize_paid_order(context, order_number)
        else:
            await update.message.reply_text(
                f"Заказ #{order_number}: платёж пока не подтверждён (статус: {status}). Проверю ещё раз позже."
            )


# ==========================================================================
# === CRM / АДМИН-ПАНЕЛЬ (доступна только из ADMIN_CHAT_ID / MANAGER_CHAT_ID) ===
# ==========================================================================
async def _connect_order(context: ContextTypes.DEFAULT_TYPE, order_number: int, notify_chat_id: int) -> None:
    """Подключает заказ: пытается автоматически получить ссылку у Билайна
    (см. auto_register_tariff) и переслать её клиенту. notify_chat_id — куда
    слать статус процесса (чат/человек, который запустил подключение)."""
    order = orders.get(order_number)
    if order is None:
        await context.bot.send_message(chat_id=notify_chat_id, text=f"Заказ #{order_number} не найден.")
        return
    if order["status"] != "paid":
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=f"Заказ #{order_number} сейчас в статусе «{order['status']}» — подключать можно только оплаченные.",
        )
        return

    await context.bot.send_message(
        chat_id=notify_chat_id,
        text=f"⏳ Запрашиваю ссылку у Билайна для заказа #{order_number}, это может занять до минуты...",
    )

    try:
        link = await auto_register_tariff(context, order_number)
    except Exception:
        # Подстраховка: auto_register_tariff обычно сама ловит свои ошибки, но
        # если случится что-то непредвиденное — не остаёмся молча без ответа.
        logger.exception("Непредвиденная ошибка автоподключения для заказа #%s", order_number)
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=f"⚠️ Непредвиденная ошибка при подключении заказа #{order_number} — "
            "статус остался «paid», подключите вручную.",
        )
        return

    if link is None:
        # auto_register_tariff уже сама объяснила причину в notify_admins — здесь просто
        # не меняем статус заказа, он остаётся "paid" и виден в /paid для ручной обработки.
        await context.bot.send_message(
            chat_id=notify_chat_id,
            text=f"⚠️ Не получилось автоматически получить ссылку для заказа #{order_number} "
            "(подробности выше). Получите её вручную и отправьте клиенту, затем можно "
            f"считать заказ подключённым — статус остался «paid».",
        )
        return

    old_status = await _update_order_status(context, order_number, "connected", timestamp_field="connected_at")

    await context.bot.send_message(
        chat_id=order["chat_id"],
        text=f"✅ Ссылка для подключения тарифа: {link}",
    )
    await _notify_status_change(context, order_number, old_status, "connected")


async def _decline_order(context: ContextTypes.DEFAULT_TYPE, order_number: int) -> str:
    order = orders.get(order_number)
    if order is None:
        return f"Заказ #{order_number} не найден."

    old_status = await _update_order_status(context, order_number, "declined")

    await context.bot.send_message(
        chat_id=order["chat_id"],
        text="К сожалению, подключить тариф не получилось. Мы свяжемся с вами, чтобы уточнить детали.",
    )
    await _notify_status_change(context, order_number, old_status, "declined")
    return f"По заказу #{order_number} отправлен отказ."


async def admin_connect_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin_chat(update.effective_chat.id):
        return
    order_number = int(query.data.rsplit("_", 1)[1])
    await query.edit_message_text(f"Обрабатываю подключение заказа #{order_number}...")
    await _connect_order(context, order_number, update.effective_chat.id)


async def admin_decline_order_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin_chat(update.effective_chat.id):
        return
    order_number = int(query.data.rsplit("_", 1)[1])
    result = await _decline_order(context, order_number)
    await query.edit_message_text(result)


def _parse_order_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/orders — список всех заказов (номер, имя, статус, дата)."""
    if not is_admin_chat(update.effective_chat.id):
        return
    if not orders:
        await update.message.reply_text("Заказов пока нет.")
        return
    lines = [
        f"#{num} — {o['name']}, статус: {o['status']}, {o['created_at']}"
        for num, o in sorted(orders.items())
    ]
    await update.message.reply_text("Все заказы:\n" + "\n".join(lines))


async def order_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/order <номер> — подробная информация по одному заказу."""
    if not is_admin_chat(update.effective_chat.id):
        return
    order_number = _parse_order_arg(context)
    if order_number is None:
        await update.message.reply_text("Использование: /order <номер заказа>")
        return
    order = orders.get(order_number)
    if order is None:
        await update.message.reply_text(f"Заказ #{order_number} не найден.")
        return

    await update.message.reply_text(
        f"📋 Заказ #{order_number}\n\n"
        f"👤 Имя: {order['name']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"📡 Оператор: {order['operator']}\n"
        f"📌 Тариф: {order['tariff']}\n"
        f"💰 Сумма: {order['price']} ₽\n"
        f"📊 Статус: {order['status']}\n"
        f"🕐 Создан: {order['created_at']}\n"
        f"💳 Оплачен: {order['paid_at'] or '—'}\n"
        f"🔌 Подключён: {order['connected_at'] or '—'}"
    )


async def new_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new — заказы со статусом new."""
    if not is_admin_chat(update.effective_chat.id):
        return
    new_ones = [(num, o) for num, o in sorted(orders.items()) if o["status"] == "new"]
    if not new_ones:
        await update.message.reply_text("Новых заказов нет.")
        return
    lines = [f"#{num} — {o['name']}, {o['created_at']}" for num, o in new_ones]
    await update.message.reply_text("Новые заказы:\n" + "\n".join(lines))


async def paid_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/paid — оплаченные заказы, которые нужно подключить."""
    if not is_admin_chat(update.effective_chat.id):
        return
    paid_ones = [(num, o) for num, o in sorted(orders.items()) if o["status"] == "paid"]
    if not paid_ones:
        await update.message.reply_text("Оплаченных заказов, ожидающих подключения, нет.")
        return
    lines = [f"#{num} — {o['name']}, {o['tariff']}, оплачен {o['paid_at']}" for num, o in paid_ones]
    await update.message.reply_text("Ожидают подключения:\n" + "\n".join(lines))


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/connect <номер> — подключить тариф клиенту вручную."""
    if not is_admin_chat(update.effective_chat.id):
        return
    order_number = _parse_order_arg(context)
    if order_number is None:
        await update.message.reply_text("Использование: /connect <номер заказа>")
        return
    await _connect_order(context, order_number, update.effective_chat.id)


async def decline_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/decline <номер> — отказать клиенту вручную."""
    if not is_admin_chat(update.effective_chat.id):
        return
    order_number = _parse_order_arg(context)
    if order_number is None:
        await update.message.reply_text("Использование: /decline <номер заказа>")
        return
    result = await _decline_order(context, order_number)
    await update.message.reply_text(result)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — сколько всего заказов и в каких статусах (за всё время)."""
    total = len(orders)
    counts = {"new": 0, "paid": 0, "connected": 0, "declined": 0}
    for o in orders.values():
        counts[o["status"]] = counts.get(o["status"], 0) + 1

    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"Всего заказов: {total}\n"
        f"🆕 Новых: {counts['new']}\n"
        f"✅ Оплачено: {counts['paid']}\n"
        f"🔌 Подключено: {counts['connected']}\n"
        f"❌ Отказано: {counts['declined']}"
    )


def _parse_created_date(created_at: str) -> date:
    return datetime.strptime(created_at, "%Y-%m-%d %H:%M").date()


def _build_report(date_from: date, date_to: date, title: str) -> str:
    """Сводный отчёт по заказам, созданным в диапазоне [date_from, date_to] (включительно)."""
    in_period = [o for o in orders.values() if date_from <= _parse_created_date(o["created_at"]) <= date_to]

    total = len(in_period)
    new_count = sum(1 for o in in_period if o["status"] == "new")
    paid_count = sum(1 for o in in_period if o["status"] == "paid")
    connected_count = sum(1 for o in in_period if o["status"] == "connected")
    declined_count = sum(1 for o in in_period if o["status"] == "declined")

    successful = paid_count + connected_count  # успешно оплаченные (в т.ч. уже подключённые)
    revenue = sum(o["price"] for o in in_period if o["status"] in ("paid", "connected"))
    conversion = round(successful / total * 100) if total else 0
    avg_check = round(revenue / successful) if successful else 0

    return (
        f"📊 {title}\n\n"
        f"✅ Всего заказов: {total}\n"
        f"🆕 Новых: {new_count}\n"
        f"💰 Оплачено: {paid_count}\n"
        f"🔗 Подключено: {connected_count}\n"
        f"❌ Отказов: {declined_count}\n\n"
        f"💵 Выручка: {revenue} ₽\n"
        f"📈 Конверсия: {conversion}%\n\n"
        f"📅 Средний чек: {avg_check} ₽"
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — сводный отчёт. Без аргументов — за сегодня, либо
    /report ГГГГ-ММ-ДД ГГГГ-ММ-ДД — за произвольный период."""
    if not is_admin_chat(update.effective_chat.id):
        return

    if not context.args:
        today = date.today()
        await update.message.reply_text(_build_report(today, today, f"ОТЧЁТ ЗА {today.isoformat()}"))
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Использование: /report — за сегодня, или /report 2025-09-01 2025-09-30 — за период."
        )
        return

    try:
        date_from = datetime.strptime(context.args[0], "%Y-%m-%d").date()
        date_to = datetime.strptime(context.args[1], "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text("Даты должны быть в формате ГГГГ-ММ-ДД, например 2025-09-01.")
        return

    title = f"ОТЧЁТ С {date_from.isoformat()} ПО {date_to.isoformat()}"
    await update.message.reply_text(_build_report(date_from, date_to, title))


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматический отчёт за неделю — каждое воскресенье в 20:00 (см. TIMEZONE_OFFSET_HOURS)."""
    today = date.today()
    week_start = today - timedelta(days=6)
    title = f"ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ({week_start.isoformat()} — {today.isoformat()})"
    await notify_admins(context, _build_report(week_start, today, title))


# ==========================================================================
# === ФОНОВЫЕ ЗАДАЧИ (оплата — каждые 30 сек, напоминания — разово) ===
# ==========================================================================
async def _finalize_paid_order(context: ContextTypes.DEFAULT_TYPE, order_number: int) -> None:
    """Оплата подтверждена — бот сразу сам запрашивает ссылку у Билайна и
    пересылает клиенту, без ручного /connect. Если автоматика не смогла
    (сайт/почта не сработали) — auto_register_tariff уже объяснила причину
    админам, а заказ остаётся в статусе "paid": можно подключить вручную
    кнопкой или командой /connect (запасной вариант, а не основной путь)."""
    order = orders[order_number]
    old_status = await _update_order_status(context, order_number, "paid", timestamp_field="paid_at")

    await context.bot.send_message(
        chat_id=order["chat_id"],
        text=(
            "✅ Оплата прошла успешно!\n"
            "Получаю для вас ссылку на подключение — это может занять до минуты..."
        ),
    )
    await _notify_status_change(context, order_number, old_status, "paid")

    _cancel_job(context, f"remind_{order_number}")

    link = await auto_register_tariff(context, order_number)

    if link is None:
        # Заказ остаётся "paid" — предлагаем ручной запасной вариант.
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Подключить вручную", callback_data=f"connect_order_{order_number}"),
                    InlineKeyboardButton("❌ Отказать", callback_data=f"decline_order_{order_number}"),
                ]
            ]
        )
        await notify_admins(
            context,
            f"Заказ #{order_number} остался в статусе «paid» — автоматика не справилась "
            "(подробности выше), подключите вручную.",
            reply_markup=keyboard,
        )
        return

    old_status2 = await _update_order_status(context, order_number, "connected", timestamp_field="connected_at")
    await context.bot.send_message(
        chat_id=order["chat_id"],
        text=f"✅ Ссылка для подключения тарифа: {link}",
    )
    await _notify_status_change(context, order_number, old_status2, "connected")


async def check_pending_payments_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается автоматически каждые PAYMENT_CHECK_INTERVAL_SECONDS секунд."""
    pending = [
        (num, o) for num, o in orders.items()
        if o["status"] == "new" and o.get("payment_id")
    ]
    if not pending:
        return

    for order_number, order in pending:
        try:
            status = await get_yookassa_payment_status(order["payment_id"])
        except Exception:
            logger.exception("Не удалось проверить статус платежа по заказу #%s", order_number)
            continue

        if status == "succeeded":
            await _finalize_paid_order(context, order_number)
        elif status in ("canceled", "expired"):
            order["payment_id"] = None  # можно будет попробовать оплатить заново
            _save_orders()
            logger.info("Платёж по заказу #%s отменён/истёк", order_number)


async def remind_payment_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разовое напоминание клиенту через PAYMENT_REMINDER_DELAY_SECONDS после создания платежа."""
    data = context.job.data
    order_number = data["order_number"]
    order = orders.get(order_number)

    # Если заказ уже оплачен/отменён — напоминать не нужно.
    if order is None or order["status"] != "new" or not order.get("payment_id"):
        return

    await context.bot.send_message(
        chat_id=data["chat_id"],
        text="Вы ещё не оплатили. Ссылка действительна 24 часа.",
    )


async def idle_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент долго не принимал решение — напоминаем о себе и сразу планируем
    следующее напоминание с увеличенным интервалом (см. IDLE_NUDGE_STEP_SECONDS).
    Цепочка повторяется, пока клиент не нажмёт любую кнопку — это отменяет job
    по имени f"idle_{user_id}" (см. _cancel_job в web_app_data_received,
    human_button_pressed)."""
    data = context.job.data
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text="Вы всё ещё здесь? Нужна помощь?",
        reply_markup=_HUMAN_BUTTON_KEYBOARD,
    )

    next_interval = data["next_interval"]
    context.job_queue.run_once(
        idle_nudge_job,
        when=next_interval,
        chat_id=data["chat_id"],
        user_id=data["user_id"],
        data={
            "chat_id": data["chat_id"],
            "user_id": data["user_id"],
            "next_interval": next_interval + IDLE_NUDGE_STEP_SECONDS,
        },
        name=f"idle_{data['user_id']}",
    )


# ==========================================================================
# === ОПИСАНИЕ БОТА (видно ДО нажатия Start — экран пустого чата и профиль) ===
# ==========================================================================
# Telegram показывает это ДО того, как человек вообще написал боту — то есть
# бот не может "написать первым", но МОЖЕТ заранее выставить текст, который
# виден на пустом экране чата и в профиле. Настраивается через Bot API
# (set_my_description/set_my_short_description), поэтому применяется
# автоматически при каждом запуске — вручную через @BotFather делать не нужно.
# Ограничение Telegram: short description — не больше 120 символов (иначе
# set_my_short_description падает с ошибкой) — поэтому здесь версия короче,
# чем в FULL_DESCRIPTION (лимит 512), где помещается весь текст полностью.
# Отображаемое имя бота (не @username, а то, что видно в шапке чата и профиле).
BOT_NAME = "Tarif - Beeline"

SHORT_DESCRIPTION = (
    "🐝 Официальный сервис подключения непубличных тарифов Билайн. Жмите Start!"
)
FULL_DESCRIPTION = (
    "🐝 Официальный сервис по подключению непубличных тарифных планов Билайн\n\n"
    "🛡 Гарантия: не подключили — вернём деньги в течение 3 дней.\n"
    "📞 +7 (930) 909-77-77\n"
    "🌐 tarif-master.ru\n\n"
    "▶️ Нажмите Start и выберите тариф из списка."
)


async def _post_init(application: Application) -> None:
    """Вызывается один раз при старте бота — обновляет имя и маркетинговое описание."""
    try:
        await application.bot.set_my_name(BOT_NAME)
        await application.bot.set_my_short_description(SHORT_DESCRIPTION)
        await application.bot.set_my_description(FULL_DESCRIPTION)
    except Exception:
        logger.exception("Не удалось обновить имя/описание бота")


# ==========================================================================
# === ЗАПУСК БОТА ===
# ==========================================================================
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise SystemExit(
            "Не задан BOT_TOKEN. Вставьте токен бота в начале bot.py "
            "(переменная BOT_TOKEN) или задайте переменную окружения BOT_TOKEN."
        )
    if not ADMIN_CHAT_IDS:
        logger.warning(
            "Не задан ни ADMIN_CHAT_ID, ни MANAGER_CHAT_ID — уведомления и админ-команды работать не будут."
        )
    if YOOKASSA_SHOP_ID == "YOUR_SHOP_ID" or YOOKASSA_SECRET_KEY == "YOUR_SECRET_KEY":
        logger.warning(
            "YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы — кнопка «Оплатить» "
            "не сможет создать реальный платёж, пока вы их не впишете."
        )
    if not WEBAPP_URL:
        logger.warning(
            "WEBAPP_URL не задан — кнопка «Открыть каталог тарифов» не покажется клиенту "
            "(нужен настоящий https-адрес, где размещён webapp.html, см. README)."
        )

    _load_orders()

    application = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BROWSING: [
                MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_received),
                CallbackQueryHandler(human_button_pressed, pattern="^human$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, question_during_offer),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conversation)
    application.add_handler(CommandHandler("check_payment", check_payment_command))

    # CRM / админ-панель (работает в любом чате из ADMIN_CHAT_IDS — сами хендлеры это проверяют).
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("order", order_detail_command))
    application.add_handler(CommandHandler("new", new_orders_command))
    application.add_handler(CommandHandler("paid", paid_orders_command))
    application.add_handler(CommandHandler("connect", connect_command))
    application.add_handler(CommandHandler("decline", decline_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CallbackQueryHandler(admin_connect_button, pattern=r"^connect_order_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_decline_order_button, pattern=r"^decline_order_\d+$"))

    # Еженедельный отчёт — каждое воскресенье в 20:00 (часовой пояс — TIMEZONE_OFFSET_HOURS).
    application.job_queue.run_daily(
        weekly_report_job,
        time=dt_time(hour=20, minute=0, tzinfo=dt_timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))),
        days=(6,),  # 6 = воскресенье (0 = понедельник, как в datetime.date.weekday())
    )

    # Фоновая проверка всех ожидающих платежей — раз в 30 секунд, без участия человека.
    application.job_queue.run_repeating(
        check_pending_payments_job, interval=PAYMENT_CHECK_INTERVAL_SECONDS, first=10
    )

    logger.info("Бот запущен, слушаю обновления...")
    application.run_polling()


if __name__ == "__main__":
    main()
