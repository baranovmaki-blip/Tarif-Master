"""
«Красивые номера» — вкладка бота «Тариф-Мастер» с каталогом красивых
(золотых/серебряных/VIP/эксклюзивных) номеров на продажу вместе с тарифом.

Каталог (какие номера/цены/типы существуют) — вшит в код в _DEFAULT_NUMBERS
ниже, тем же способом, что и TARIFFS в bot.py: чтобы добавить/убрать/
переоценить номер — правьте список в этом файле. Никакой внешней Google
Sheets тут нет (сознательно, по просьбе) — единственное, что хранится вне
кода, это ЖИВОЕ состояние (статус/резерв), в локальном numbers.json рядом
с orders.json, чтобы резервы переживали перезапуск бота.

Использование (уже подключено в bot.py):
    from beautiful_numbers import register_numbers_handlers, numbers_expiry_sweep_job
    register_numbers_handlers(application)
    application.job_queue.run_repeating(numbers_expiry_sweep_job, interval=900, first=60)

Команда клиента: /numbers -> меню фильтров -> список карточек с кнопкой
«Заказать» -> резерв на 24 часа -> «Подключить тариф + номер» (заводит
обычный заказ в CRM bot.py с номером в комментарии и открывает каталог
тарифов) / «Только номер» (уведомляет админа, дальше вручную) / «Отмена»
(сразу снимает резерв).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

RESERVATION_HOURS = 24
PAGE_SIZE = 5

# Файл, куда сохраняется ЖИВОЕ состояние каталога (статус/резервы), чтобы
# пережить перезапуск бота — тот же приём, что ORDERS_FILE в bot.py.
NUMBERS_FILE = "numbers.json"

FILTERS: dict[str, str | None] = {
    "all": None,
    "gold": "Золотой",
    "silver": "Серебряный",
    "vip": "VIP",
    "excl": "Эксклюзив",
}
FILTER_LABELS = {
    "all": "Все",
    "gold": "Золотые",
    "silver": "Серебряные",
    "vip": "VIP",
    "excl": "Эксклюзив",
}
TYPE_EMOJI = {
    "Золотой": "🥇",
    "Серебряный": "🥈",
    "VIP": "👑",
    "Эксклюзив": "💎",
}

# ===== Каталог номеров (вшит в код, как и TARIFFS в bot.py) =====
# Реальные номера с физических SIM-карт (МТС и T2/Tele2). Тип/цена — по
# хвостовым цифрам номера: "3333"/"4444"/"1111"/"2222" -> Серебряный,
# 10 000 ₽; "5555"/"0000" -> Золотой, 15 000 ₽; "6666" -> Эксклюзив,
# 20 000 ₽; "9999" -> VIP, 25 000 ₽ (плюс id 14 — изначально эксклюзивный
# номер без повторяющегося хвоста, 15 000 ₽). Поправьте здесь, если нужно
# ещё разделить по типу/оператору. reserved_until/reserved_by заполняет и
# очищает сам бот при заказе/отмене/истечении — не трогайте их руками.
_DEFAULT_NUMBERS: list[dict] = [
    {"id": "1", "number": "9851793333", "operator": "МТС", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "2", "number": "9851796666", "operator": "МТС", "price": 20000, "type": "Эксклюзив",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "3", "number": "9852145555", "operator": "МТС", "price": 15000, "type": "Золотой",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "4", "number": "9851893333", "operator": "МТС", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "5", "number": "9851826666", "operator": "МТС", "price": 20000, "type": "Эксклюзив",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "6", "number": "9851834444", "operator": "МТС", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "7", "number": "9851930000", "operator": "МТС", "price": 15000, "type": "Золотой",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "8", "number": "9851792222", "operator": "МТС", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "9", "number": "9017591111", "operator": "T2", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "10", "number": "9779514444", "operator": "T2", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "11", "number": "9777162222", "operator": "T2", "price": 10000, "type": "Серебряный",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "12", "number": "9776929999", "operator": "T2", "price": 25000, "type": "VIP",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "13", "number": "9017929999", "operator": "T2", "price": 25000, "type": "VIP",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
    {"id": "14", "number": "9919220001", "operator": "T2", "price": 15000, "type": "Эксклюзив",
     "status": "Доступен", "added_date": "05.09.2026", "reserved_until": "", "reserved_by": ""},
]


# ==========================================================================
# === Данные: локальное хранение (numbers.json), без внешних сервисов ===
# ==========================================================================
def _load_numbers() -> list[dict]:
    try:
        with open(NUMBERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return [dict(row) for row in _DEFAULT_NUMBERS]
    except Exception:
        logger.exception("Не удалось прочитать %s — начинаю со стандартного каталога номеров.", NUMBERS_FILE)
        return [dict(row) for row in _DEFAULT_NUMBERS]


def _save_numbers() -> None:
    with open(NUMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(NUMBERS, f, ensure_ascii=False, indent=2)


NUMBERS: list[dict] = _load_numbers()


async def fetch_numbers(*, force: bool = False) -> list[dict]:
    """Сигнатура (async, с force) сохранена ради минимальных правок в
    вызывающем коде — реально это просто чтение локального списка, без
    сети и без кэша (он тут не нужен)."""
    return NUMBERS


async def reserve_number(number_id: str, telegram_id: int) -> dict:
    """Пытается зарезервировать номер на 24 часа. Проверяет, что номер
    ещё "Доступен" — при однопроцессном asyncio-боте между проверкой и
    записью нет await, так что гонка между двумя параллельными заказами
    одного номера исключена."""
    row = next((r for r in NUMBERS if str(r.get("id")) == str(number_id)), None)
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row.get("status") != "Доступен":
        return {"ok": False, "error": "not_available", "status": row.get("status")}

    until = datetime.now(timezone.utc) + timedelta(hours=RESERVATION_HOURS)
    row["status"] = "Зарезервирован"
    row["reserved_until"] = until.isoformat()
    row["reserved_by"] = str(telegram_id)
    _save_numbers()

    return {"ok": True, "reserved_until": row["reserved_until"]}


async def release_number(number_id: str) -> dict:
    """Снимает резерв (кнопка «Отмена» в диалоге подтверждения)."""
    row = next((r for r in NUMBERS if str(r.get("id")) == str(number_id)), None)
    if row is None:
        return {"ok": False, "error": "not_found"}

    row["status"] = "Доступен"
    row["reserved_until"] = ""
    row["reserved_by"] = ""
    _save_numbers()

    return {"ok": True}


# ==========================================================================
# === Форматирование ===
# ==========================================================================
def format_phone(raw: str) -> str:
    """"9998887777" -> "+7 999 888-77-77". Терпимо к чему угодно нецифровому
    в исходной строке; если после чистки не ровно 10 цифр — возвращает как
    есть с "+7 " спереди, чтобы не падать на нестандартных записях в таблице."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return f"+7 {raw}".strip()
    return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def format_price(price) -> str:
    try:
        value = int(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{value:,}".replace(",", " ") + " ₽"


def _type_emoji(type_name: str) -> str:
    return TYPE_EMOJI.get(type_name, "📱")


def _matches_filter(row: dict, filter_key: str) -> bool:
    wanted = FILTERS.get(filter_key)
    return wanted is None or row.get("type") == wanted


def _available(row: dict) -> bool:
    return row.get("status") == "Доступен"


def _card_text(row: dict, index: int) -> str:
    available = _available(row)
    status_note = "" if available else f" ({row.get('status')})"
    operator = row.get("operator")
    operator_note = f"\n📶 {operator}" if operator else ""
    return (
        f"{index}️⃣ {_type_emoji(row.get('type'))} {format_phone(row.get('number', ''))}"
        f"{operator_note}\n"
        f"💰 {format_price(row.get('price'))}{status_note}"
    )


# ==========================================================================
# === Экран каталога (список + фильтры + пагинация) ===
# ==========================================================================
def _filter_row_buttons() -> list[list[InlineKeyboardButton]]:
    order = ["all", "gold", "silver", "vip", "excl"]
    row = [InlineKeyboardButton(FILTER_LABELS[key], callback_data=f"bn:f:{key}:0") for key in order]
    return [row]


async def _render_catalog(filter_key: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = await fetch_numbers()
    matching = [r for r in rows if _matches_filter(r, filter_key)]

    header = f"📱 <b>КАТАЛОГ КРАСИВЫХ НОМЕРОВ</b>\n\nКатегория: {FILTER_LABELS[filter_key]}\n"

    if not matching:
        text = header + "\nВ этой категории пока нет номеров."
        keyboard = _filter_row_buttons() + [[InlineKeyboardButton("🔍 Поиск по цифрам", callback_data="bn:search")]]
        return text, InlineKeyboardMarkup(keyboard)

    total_pages = (len(matching) - 1) // PAGE_SIZE + 1
    page = max(0, min(page, total_pages - 1))
    page_rows = matching[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]

    lines = [header]
    buttons: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(page_rows, start=1):
        available = _available(row)
        lines.append(_card_text(row, i))
        if available:
            buttons.append(
                [InlineKeyboardButton(
                    f"Заказать {format_phone(row.get('number', ''))}",
                    callback_data=f"bn:o:{row.get('id')}",
                )]
            )

    if total_pages > 1:
        lines.append(f"\nСтраница {page + 1} из {total_pages}")
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"bn:f:{filter_key}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Далее ▶", callback_data=f"bn:f:{filter_key}:{page + 1}"))
        if nav:
            buttons.append(nav)

    buttons.extend(_filter_row_buttons())
    buttons.append([InlineKeyboardButton("🔍 Поиск по цифрам", callback_data="bn:search")])

    return "\n\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, keyboard = await _render_catalog("all", 0)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, filter_key, page_str = query.data.split(":")
    text, keyboard = await _render_catalog(filter_key, int(page_str))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ==========================================================================
# === Поиск по цифрам ===
# ==========================================================================
async def search_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["bn_awaiting_search"] = True
    await query.message.reply_text("🔍 Введите цифры номера (например, 8887) — покажу совпадения.")


async def search_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Зарегистрирован в group=-1 (см. register_numbers_handlers), чтобы
    успеть перехватить текст ДО общего текстового хендлера в conversation
    bot.py (question_during_offer), который иначе заберёт себе любое
    текстовое сообщение. Если поиск не был запрошен кнопкой — тихо
    пропускаем апдейт дальше (не поднимаем ApplicationHandlerStop), и
    conversation обрабатывает его как обычно."""
    if not context.user_data.get("bn_awaiting_search"):
        return

    context.user_data["bn_awaiting_search"] = False
    query_digits = re.sub(r"\D", "", update.message.text or "")

    if not query_digits:
        await update.message.reply_text("Не увидел цифр — откройте /numbers и попробуйте ещё раз.")
        raise ApplicationHandlerStop

    rows = await fetch_numbers()
    matches = [r for r in rows if query_digits in re.sub(r"\D", "", r.get("number", ""))]

    if not matches:
        await update.message.reply_text(
            f"По «{query_digits}» ничего не нашёл. Откройте /numbers, чтобы посмотреть весь каталог."
        )
        raise ApplicationHandlerStop

    lines = [f"🔍 <b>Результаты поиска по «{query_digits}»</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(matches[:PAGE_SIZE], start=1):
        available = _available(row)
        lines.append(_card_text(row, i))
        if available:
            buttons.append(
                [InlineKeyboardButton(
                    f"Заказать {format_phone(row.get('number', ''))}",
                    callback_data=f"bn:o:{row.get('id')}",
                )]
            )
    if len(matches) > PAGE_SIZE:
        lines.append(f"\n…и ещё {len(matches) - PAGE_SIZE} — уточните поиск цифрами.")

    await update.message.reply_text(
        "\n\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    raise ApplicationHandlerStop


# ==========================================================================
# === Заказ номера: резерв на 24 часа -> подтверждение ===
# ==========================================================================
async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    number_id = query.data.split(":")[2]

    result = await reserve_number(number_id, query.from_user.id)
    if not result.get("ok"):
        if result.get("error") == "not_available":
            await query.answer("Этот номер уже забронирован — выберите другой.", show_alert=True)
        else:
            await query.answer("Не получилось забронировать номер, попробуйте ещё раз.", show_alert=True)
        return

    await query.answer()

    rows = await fetch_numbers(force=True)
    row = next((r for r in rows if str(r.get("id")) == str(number_id)), None)
    phone = format_phone(row.get("number", "")) if row else "?"
    price = format_price(row.get("price")) if row else "?"
    operator = row.get("operator", "") if row else ""

    context.user_data["bn_pending"] = {"id": number_id, "phone": phone, "price": price, "operator": operator}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Подключить тариф + номер", callback_data=f"bn:c:tariff:{number_id}")],
        [InlineKeyboardButton("Только номер", callback_data=f"bn:c:only:{number_id}")],
        [InlineKeyboardButton("Отмена", callback_data=f"bn:c:cancel:{number_id}")],
    ])
    operator_line = f"📶 Оператор: {operator}\n" if operator else ""
    await query.message.reply_text(
        f"✅ Вы выбрали номер {phone}.\n"
        f"{operator_line}"
        f"💰 {price}\n\n"
        f"Он закреплён за вами на {RESERVATION_HOURS} часа. Подключить тариф?",
        reply_markup=keyboard,
    )


async def confirm_tariff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Подключить тариф + номер» — заводит обычный заказ в CRM bot.py (как
    /start), помечает его выбранным номером в свободном поле "comment" и
    открывает каталог тарифов WebApp, как обычно."""
    import bot as core

    query = update.callback_query
    number_id = query.data.split(":")[3]
    pending = context.user_data.get("bn_pending") or {}
    phone = pending.get("phone", "?")
    price = pending.get("price", "?")
    number_operator = pending.get("operator", "")

    await query.answer()

    user = query.from_user
    order_number = core._generate_order_number()
    number_note = f"{phone}"
    if number_operator:
        number_note += f" ({number_operator})"
    core.orders[order_number] = {
        "user_id": user.id,
        "chat_id": query.message.chat_id,
        "name": user.full_name,
        "phone": "не указан",
        "operator": core.DEFAULT_OPERATOR,
        "tariff": "уточняется у клиента",
        "price": core.CONNECTION_FEE_RUB,
        "status": "new",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "paid_at": None,
        "connected_at": None,
        "payment_id": None,
        "comment": f"Красивый номер: {number_note} ({price}, резерв 24ч)",
    }
    core._save_orders()
    context.user_data["order_number"] = order_number

    if core.WEBAPP_URL:
        keyboard = ReplyKeyboardMarkup(
            [[core.KeyboardButton("🟡 Открыть каталог тарифов", web_app=core.WebAppInfo(url=core.WEBAPP_URL))]],
            resize_keyboard=True,
        )
    else:
        keyboard = core._HUMAN_BUTTON_KEYBOARD

    await query.message.reply_text(
        f"Отлично! Номер {phone} закреплён за заказом #{order_number}.\n\n"
        "Теперь откройте каталог и выберите тариф — соединим их вместе.",
        reply_markup=keyboard,
    )

    await core.send_to_google_sheets(context, order_number)
    await core.notify_admins(
        context,
        f"🆕 Заказ #{order_number} с красивым номером\n"
        f"👤 Клиент: {user.full_name} (@{user.username or 'без username'}, id {user.id})\n"
        f"📱 Номер: {number_note} ({price})",
    )


async def confirm_only_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Только номер» — без тарифа, дальше клиента ведёт менеджер вручную
    (номер остаётся зарезервированным на 24 часа, статус в таблице уже
    "Зарезервирован" — снимать его тут не нужно)."""
    import bot as core

    query = update.callback_query
    pending = context.user_data.get("bn_pending") or {}
    phone = pending.get("phone", "?")
    price = pending.get("price", "?")
    operator = pending.get("operator", "")
    number_note = f"{phone} ({operator})" if operator else phone

    await query.answer()
    await query.message.reply_text(
        f"Хорошо, номер {phone} ({price}) зарезервирован за вами на {RESERVATION_HOURS} часа. "
        "Менеджер свяжется с вами для оплаты."
    )
    await core.notify_admins(
        context,
        f"📱 Заказ только номера (без тарифа): {number_note} ({price}) — "
        f"{query.from_user.full_name} (@{query.from_user.username or 'без username'}, id {query.from_user.id}). "
        "Свяжитесь для оплаты.",
    )


async def confirm_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    number_id = query.data.split(":")[3]

    await query.answer()
    await release_number(number_id)
    context.user_data.pop("bn_pending", None)
    await query.message.reply_text("Резерв снят. Номер снова доступен в каталоге — /numbers.")


# ==========================================================================
# === Фоновая job: подчищает просроченные резервы даже без активности ===
# ==========================================================================
async def numbers_expiry_sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снимает резерв с номеров, у которых истекли 24 часа, даже если сутки
    никто не открывал /numbers — иначе просроченный резерв "зависнет" до
    следующего визита клиента (reserve_number/release_number сами этого не
    делают, они дёргаются только по действию конкретного клиента)."""
    now = datetime.now(timezone.utc)
    changed = False

    for row in NUMBERS:
        if row.get("status") != "Зарезервирован" or not row.get("reserved_until"):
            continue
        try:
            until = datetime.fromisoformat(row["reserved_until"])
        except ValueError:
            continue
        if until <= now:
            row["status"] = "Доступен"
            row["reserved_until"] = ""
            row["reserved_by"] = ""
            changed = True

    if changed:
        _save_numbers()


# ==========================================================================
# === Регистрация хендлеров ===
# ==========================================================================
def register_numbers_handlers(application) -> None:
    # group=-1 — раньше, чем ConversationHandler в bot.py (у него в состоянии
    # BROWSING висит MessageHandler(filters.TEXT & ~filters.COMMAND, ...),
    # который иначе заберёт себе любой текст, включая цифры для поиска.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_input_handler), group=-1
    )

    application.add_handler(CommandHandler("numbers", cmd_numbers))
    application.add_handler(CallbackQueryHandler(filter_callback, pattern=r"^bn:f:"))
    application.add_handler(CallbackQueryHandler(search_start_callback, pattern=r"^bn:search$"))
    application.add_handler(CallbackQueryHandler(order_callback, pattern=r"^bn:o:"))
    application.add_handler(CallbackQueryHandler(confirm_tariff_callback, pattern=r"^bn:c:tariff:"))
    application.add_handler(CallbackQueryHandler(confirm_only_callback, pattern=r"^bn:c:only:"))
    application.add_handler(CallbackQueryHandler(confirm_cancel_callback, pattern=r"^bn:c:cancel:"))
