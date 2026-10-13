"""Personal income and expense tracker for bot users."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database


STATE_KEY = "finance_entry"
EXPENSE_CATEGORIES = {
    "food": "🍽 Еда",
    "transport": "🚕 Транспорт",
    "home": "🏠 Дом",
    "health": "💊 Здоровье",
    "shopping": "🛍 Покупки",
    "other": "📦 Другое",
}
INCOME_CATEGORIES = {
    "salary": "💼 Зарплата",
    "business": "🧾 Работа/дело",
    "gift": "🎁 Подарок",
    "other": "📦 Другое",
}


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖ Расход", callback_data="finance:kind:expense"),
            InlineKeyboardButton("➕ Доход", callback_data="finance:kind:income"),
        ],
        [
            InlineKeyboardButton("📊 Этот месяц", callback_data="finance:summary"),
            InlineKeyboardButton("📈 График 6 мес.", callback_data="finance:chart"),
        ],
        [InlineKeyboardButton("🧾 Последние записи", callback_data="finance:recent")],
        [InlineKeyboardButton("↩️ Удалить последнюю запись", callback_data="finance:delete")],
    ])


def _money(cents: int) -> str:
    sign = "−" if cents < 0 else ""
    absolute = abs(cents)
    return sign + f"{absolute // 100:,}".replace(",", " ") + f",{absolute % 100:02d}"


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    serial = year * 12 + month - 1 + offset
    return serial // 12, serial % 12 + 1


async def open_finance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.chat.type != "private":
        return
    context.user_data.pop(STATE_KEY, None)
    database.clear_finance_draft(user.id)
    await message.reply_text(
        "💰 Личные финансы\n\n"
        "Записывай доходы и расходы, смотри месячную сводку и график.\n"
        "Записи хранятся в базе бота; владелец сервера может получить к ним доступ.\n"
        "Используй одну валюту для всех записей: бот не пересчитывает валюты.\n\n"
        "Сумму можно отправить как 1250 или 1250,50; после неё можно добавить заметку.",
        reply_markup=_menu_keyboard(),
    )


async def _show_summary(query, telegram_id: int) -> None:
    now = datetime.now(timezone.utc)
    month = _month_key(now.year, now.month)
    rows = database.list_finance_transactions(telegram_id, month)
    income = sum(r["amount_cents"] for r in rows if r["kind"] == "income")
    expense = sum(r["amount_cents"] for r in rows if r["kind"] == "expense")
    balance = income - expense
    by_category: dict[str, int] = {}
    for row in rows:
        if row["kind"] == "expense":
            by_category[row["category"]] = by_category.get(row["category"], 0) + row["amount_cents"]
    category_lines = "\n".join(
        f"  • {EXPENSE_CATEGORIES.get(cat, cat)}: {_money(amount)}"
        for cat, amount in sorted(by_category.items(), key=lambda item: item[1], reverse=True)
    ) or "  Пока нет расходов"
    await query.message.reply_text(
        f"📊 Сводка за {month}\n\n"
        f"➕ Доходы: {_money(income)}\n"
        f"➖ Расходы: {_money(expense)}\n"
        f"💵 Разница: {_money(balance)}\n\n"
        f"Расходы по категориям:\n{category_lines}",
        reply_markup=_menu_keyboard(),
    )


async def _show_chart(query, telegram_id: int) -> None:
    now = datetime.now(timezone.utc)
    first_year, first_month = _shift_month(now.year, now.month, -5)
    start = _month_key(first_year, first_month)
    rows = database.list_finance_transactions(telegram_id, start)
    totals: dict[str, list[int]] = {}
    for row in rows:
        month = row["created_at"][:7]
        totals.setdefault(month, [0, 0])[0 if row["kind"] == "income" else 1] += row["amount_cents"]
    max_value = max((max(v) for v in totals.values()), default=0)
    lines = ["📈 Доходы и расходы за 6 месяцев", "(шкала отдельно нормирована для каждого месяца)", ""]
    for offset in range(-5, 1):
        year, month = _shift_month(now.year, now.month, offset)
        key = _month_key(year, month)
        income, expense = totals.get(key, [0, 0])
        scale = max(income, expense, 1)
        income_bar = "█" * max(1, round(income / scale * 10)) if income else "·"
        expense_bar = "█" * max(1, round(expense / scale * 10)) if expense else "·"
        lines.append(f"{key}  ➕ {income_bar} {_money(income)}")
        lines.append(f"       ➖ {expense_bar} {_money(expense)}")
    lines.extend(["", f"Максимум за период: {_money(max_value)}"])
    await query.message.reply_text("\n".join(lines), reply_markup=_menu_keyboard())


async def _show_recent(query, telegram_id: int) -> None:
    rows = database.list_finance_transactions(telegram_id, "0000-01")[:10]
    if not rows:
        text = "🧾 Записей пока нет. Нажми «Расход» или «Доход», чтобы добавить первую."
    else:
        lines = []
        for row in rows:
            mark = "➕" if row["kind"] == "income" else "➖"
            category = (INCOME_CATEGORIES if row["kind"] == "income" else EXPENSE_CATEGORIES).get(row["category"], row["category"])
            note = f" — {row['note']}" if row["note"] else ""
            lines.append(f"{mark} {row['created_at'][:10]} · {category}: {_money(row['amount_cents'])}{note}")
        text = "🧾 Последние записи\n\n" + "\n".join(lines)
    await query.message.reply_text(text, reply_markup=_menu_keyboard())


async def handle_finance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    data = query.data or ""
    if data == "finance:open":
        context.user_data.pop(STATE_KEY, None)
        database.clear_finance_draft(user.id)
        await query.message.reply_text(
            "💰 Личные финансы\n\n"
            "Записывай доходы и расходы, смотри месячную сводку и график.\n"
            "Записи хранятся в базе бота; владелец сервера может получить к ним доступ.\n\n"
            "Сумму можно отправить как 1250 или 1250,50; после неё можно добавить заметку.",
            reply_markup=_menu_keyboard(),
        )
    elif data.startswith("finance:kind:"):
        kind = data.rsplit(":", 1)[1]
        if kind not in {"income", "expense"}:
            return
        context.user_data[STATE_KEY] = {"kind": kind}
        database.set_finance_draft(user.id, kind)
        categories = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
        rows = [[InlineKeyboardButton(label, callback_data=f"finance:category:{kind}:{key}")] for key, label in categories.items()]
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="finance:cancel")])
        await query.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("finance:category:"):
        _, _, kind, category = data.split(":", 3)
        if kind not in {"income", "expense"}:
            return
        categories = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
        if category not in categories:
            return
        context.user_data[STATE_KEY] = {"kind": kind, "category": category}
        database.set_finance_draft(user.id, kind, category)
        await query.message.reply_text(
            f"Введи сумму для категории «{categories[category]}». Можно дописать заметку, например: 1250,50 продукты"
        )
    elif data == "finance:cancel":
        context.user_data.pop(STATE_KEY, None)
        database.clear_finance_draft(user.id)
        await query.message.reply_text("Ввод отменён.", reply_markup=_menu_keyboard())
    elif data == "finance:summary":
        await _show_summary(query, user.id)
    elif data == "finance:chart":
        await _show_chart(query, user.id)
    elif data == "finance:recent":
        await _show_recent(query, user.id)
    elif data == "finance:delete":
        deleted = database.delete_last_finance_transaction(user.id)
        await query.message.reply_text(
            "Последняя запись удалена." if deleted else "У тебя пока нет записей.",
            reply_markup=_menu_keyboard(),
        )


async def handle_finance_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    state = context.user_data.get(STATE_KEY)
    if user is None or message is None or message.chat.type != "private":
        return
    if not state:
        state = database.get_finance_draft(user.id)
    if not state:
        return
    # Keep financial amounts and notes out of the anonymous message inbox.
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    amount_text = parts[0].replace(" ", "").replace(",", ".") if parts else ""
    try:
        amount = Decimal(amount_text)
        cents_decimal = amount * 100
        if not amount.is_finite() or cents_decimal != cents_decimal.to_integral_value():
            raise InvalidOperation
        cents = int(cents_decimal)
        if cents <= 0 or cents > 100_000_000_000:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.reply_text("Не понял сумму. Отправь положительное число, например 1250 или 1250,50.")
        raise ApplicationHandlerStop
    category_map = INCOME_CATEGORIES if state["kind"] == "income" else EXPENSE_CATEGORIES
    if state.get("category") not in category_map:
        context.user_data.pop(STATE_KEY, None)
        database.clear_finance_draft(user.id)
        raise ApplicationHandlerStop
    note = parts[1][:160] if len(parts) > 1 else None
    database.add_finance_transaction(
        telegram_id=user.id,
        kind=state["kind"],
        category=state["category"],
        amount_cents=cents,
        note=note,
    )
    mark = "Доход" if state["kind"] == "income" else "Расход"
    context.user_data.pop(STATE_KEY, None)
    database.clear_finance_draft(user.id)
    await message.reply_text(f"✅ {mark} записан: {_money(cents)}", reply_markup=_menu_keyboard())
    raise ApplicationHandlerStop


def register_finance_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("finance", open_finance))
    app.add_handler(CallbackQueryHandler(handle_finance_callback, pattern=r"^finance:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_finance_text),
        # Run before the generic admin-message handler, which also uses -1.
        group=-2,
    )
