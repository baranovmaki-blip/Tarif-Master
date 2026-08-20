"""
Тариф-Мастер — «кнопка-ссылка»: отдельный, максимально простой бот без
заказов, тарифов и CRM. Одна кнопка "Получить ссылку" — при нажатии сам
заходит на mycompany.beeline.ru, вводит бизнес-почту, ждёт письмо с
одноразовой ссылкой и присылает её. Можно жать сколько угодно раз подряд —
каждый раз отдельный, независимый запрос.

Доступ — только админу (ADMIN_CHAT_ID), потому что кнопка реально отправляет
заявку на настоящий сайт Билайна: массовые нажатия посторонними людьми
выглядели бы как спам-атака на форму.

Запуск: python link_bot.py
Тот же .env, что и у bot.py (общая почта/Билайн-аккаунт), плюс отдельная
переменная LINK_BOT_TOKEN — токен ЭТОГО бота, полученный у @BotFather.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import logging
import os
import re
import time
from datetime import date

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

# ==========================================================================
# === НАСТРОЙКИ — переменные общие с bot.py (тот же .env), плюс токен    ===
# === именно этого бота.                                                 ===
# ==========================================================================
LINK_BOT_TOKEN = os.environ.get("LINK_BOT_TOKEN", "YOUR_LINK_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "YOUR_ADMIN_CHAT_ID")

BEELINE_AUTH_URL = "https://mycompany.beeline.ru/auth"
EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "YOUR_EMAIL@gmail.com")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "YOUR_GMAIL_APP_PASSWORD")
IMAP_SERVER = "imap.gmail.com"
BEELINE_SENDER_EMAIL = os.environ.get("BEELINE_SENDER_EMAIL", "b2bmycompany@beeline.ru")

CONFIRMATION_EMAIL_TIMEOUT_SECONDS = 90
CONFIRMATION_EMAIL_POLL_INTERVAL_SECONDS = 5
# ==========================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_GET_LINK_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🔗 Получить ссылку", callback_data="get_link")]]
)

# Заявки идут в один и тот же почтовый ящик — если два запроса пойдут
# одновременно, оба могут выхватить одно и то же письмо. Лок заставляет их
# идти строго по очереди (см. подробный разбор в bot.py, _beeline_lock).
_beeline_lock = asyncio.Lock()


async def request_beeline_link() -> None:
    """Заходит на mycompany.beeline.ru, вводит EMAIL_ACCOUNT и жмёт "Далее"."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(BEELINE_AUTH_URL, wait_until="networkidle", timeout=30000)
            await page.locator('input[name="email"]').fill(EMAIL_ACCOUNT)
            await page.locator('button[type="submit"]').click()
            await page.wait_for_timeout(2000)
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
    """Синхронная проверка почты — ищет самое свежее сегодняшнее письмо от
    sender_email, достаёт из него первую ссылку. Подробности — см. bot.py."""
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=20)
    try:
        imap.login(EMAIL_ACCOUNT, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        today_imap = date.today().strftime("%d-%b-%Y")
        status, data = imap.search(None, "FROM", sender_email, "SINCE", today_imap)
        if status != "OK" or not data[0]:
            return None

        latest_id = data[0].split()[-1]
        status, msg_data = imap.fetch(latest_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return None

        msg = email_lib.message_from_bytes(msg_data[0][1])
        body = _extract_email_body(msg)

        match = re.search(r'https?://[^\s"\'<>]+', body)
        return match.group(0) if match else None
    finally:
        imap.logout()


async def get_confirmation_link(sender_email: str, timeout_seconds: int) -> str | None:
    """Опрашивает почту, пока не найдёт письмо со ссылкой или не кончится время."""
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


def _is_admin(chat_id) -> bool:
    return str(chat_id) == str(ADMIN_CHAT_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_chat.id):
        await update.message.reply_text("Этот бот только для админа.")
        return
    await update.message.reply_text(
        "Жмите кнопку — получите свежую одноразовую ссылку на подключение с Билайна. "
        "Можно жать сколько угодно раз.",
        reply_markup=_GET_LINK_KEYBOARD,
    )


async def get_link_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update.effective_chat.id):
        await query.answer("Только для админа.", show_alert=True)
        return
    await query.answer()

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Запрашиваю ссылку у Билайна — это может занять до минуты...",
    )

    async with _beeline_lock:
        try:
            await request_beeline_link()
        except Exception:
            logger.exception("Не удалось отправить запрос на %s", BEELINE_AUTH_URL)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Не удалось открыть/заполнить форму на mycompany.beeline.ru — "
                "возможно, сайт изменил структуру страницы.",
                reply_markup=_GET_LINK_KEYBOARD,
            )
            return

        link = await get_confirmation_link(BEELINE_SENDER_EMAIL, CONFIRMATION_EMAIL_TIMEOUT_SECONDS)

    if link is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Заявка отправлена, но письмо не пришло за "
            f"{CONFIRMATION_EMAIL_TIMEOUT_SECONDS} сек. Проверьте почту {EMAIL_ACCOUNT} вручную.",
            reply_markup=_GET_LINK_KEYBOARD,
        )
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Ссылка: {link}",
        reply_markup=_GET_LINK_KEYBOARD,
    )


def main() -> None:
    if not LINK_BOT_TOKEN or LINK_BOT_TOKEN == "YOUR_LINK_BOT_TOKEN":
        raise SystemExit(
            "Не задан LINK_BOT_TOKEN. Получите токен у @BotFather (/newbot) и впишите "
            "в .env как LINK_BOT_TOKEN=..."
        )
    if not ADMIN_CHAT_ID or ADMIN_CHAT_ID == "YOUR_ADMIN_CHAT_ID":
        raise SystemExit("Не задан ADMIN_CHAT_ID в .env.")

    application = Application.builder().token(LINK_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(get_link_pressed, pattern="^get_link$"))

    logger.info("link_bot запущен, слушаю обновления...")
    application.run_polling()


if __name__ == "__main__":
    main()
