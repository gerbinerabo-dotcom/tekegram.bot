"""Telegram Stars checkout and purchase support for the optional Mafia+ feature."""

import logging
import sqlite3
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import database
from config import (
    ADMIN_ID,
    MAFIA_PLUS_PAYMENTS_ENABLED,
    MAFIA_PLUS_PRICE_STARS,
)
from filters import is_admin

logger = logging.getLogger(__name__)

PRODUCT_ID = "mafia_plus_role_stats"
PRODUCT_TITLE = "Mafia+: победы по ролям"
PRODUCT_DESCRIPTION = (
    "Бессрочный доступ к расширенной статистике побед и партий по ролям. "
    "Обычная игра и базовая статистика остаются бесплатными."
)
CURRENCY = "XTR"


def has_mafia_plus_access(telegram_id: int) -> bool:
    return is_admin(telegram_id) or database.has_product_access(telegram_id, PRODUCT_ID)


def _terms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принимаю условия и продолжить", callback_data="payments:accept")],
        [InlineKeyboardButton("ℹ️ Мои покупки", callback_data="payments:purchases")],
        [InlineKeyboardButton("❌ Отменить", callback_data="payments:cancel")],
    ])


def _terms_text() -> str:
    status = (
        "Платежи включены. Перед покупкой прочитай условия и убедись, что имеешь право совершать её."
        if MAFIA_PLUS_PAYMENTS_ENABLED
        else "Платежи сейчас выключены. Это техническая подготовка, не предложение совершить реальную покупку."
    )
    return (
        "ℹ️ Условия Mafia+\n\n"
        "Mafia+ — разовая покупка бессрочного доступа к расширенной статистике "
        "побед по игровым ролям. Основная игра «Мафия» и базовая статистика "
        "останутся бесплатными.\n\n"
        "Если для покупки в твоей ситуации требуются возрастные условия, "
        "согласие родителя или дополнительное оформление, не совершай её до "
        "выполнения этих требований.\n\n"
        f"Стоимость: {MAFIA_PLUS_PRICE_STARS} ⭐. Платёж проходит только через "
        "Telegram Stars (XTR). Stars — внутренняя валюта Telegram; их перевод "
        "в тенге или вывод средств этим ботом не гарантируется.\n\n"
        "Доступ выдаётся только после подтверждённого Telegram платежа. "
        "Автоматический возврат не обещается; по вопросу покупки напиши "
        "/paysupport.\n\n" + status
    )


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_terms_text())


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if message.chat.type != "private":
        await message.reply_text("Покупки и подтверждение условий доступны в личном чате с ботом.")
        return
    if has_mafia_plus_access(user.id):
        await message.reply_text("✅ У тебя уже есть доступ Mafia+. Посмотреть его можно командой /mypurchases.")
        return
    await message.reply_text(_terms_text(), reply_markup=_terms_keyboard())


async def _payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not query.data:
        return
    if query.data == "payments:terms":
        await query.answer()
        if query.message is not None:
            await query.message.reply_text(_terms_text(), reply_markup=_terms_keyboard())
        return
    if query.data == "payments:purchases":
        if query.message is None or query.message.chat.type != "private":
            await query.answer("Покупки можно посмотреть только в личном чате с ботом.", show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(_purchases_text(user.id))
        return
    if query.data == "payments:cancel":
        await query.answer("Отменено.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        return
    if query.data != "payments:accept":
        await query.answer("Неизвестное действие.", show_alert=True)
        return
    if query.message is None or query.message.chat.type != "private":
        await query.answer("Продолжи в личном чате с ботом.", show_alert=True)
        return
    if has_mafia_plus_access(user.id):
        await query.answer("Доступ уже куплен.", show_alert=True)
        return
    if not MAFIA_PLUS_PAYMENTS_ENABLED:
        await query.answer("Платежи пока выключены.", show_alert=True)
        await query.message.reply_text(
            "Покупка пока недоступна: платежи отключены. Счёт не создан, "
            "списания не было."
        )
        return

    payload = f"{PRODUCT_ID}:{user.id}:{uuid.uuid4().hex}"
    try:
        database.create_purchase_intent(
            telegram_id=user.id,
            product_id=PRODUCT_ID,
            amount_stars=MAFIA_PLUS_PRICE_STARS,
            currency=CURRENCY,
            invoice_payload=payload,
        )
        await context.bot.send_invoice(
            chat_id=user.id,
            title=PRODUCT_TITLE,
            description=PRODUCT_DESCRIPTION,
            payload=payload,
            provider_token="",
            currency=CURRENCY,
            prices=[LabeledPrice(PRODUCT_TITLE, MAFIA_PLUS_PRICE_STARS)],
            start_parameter="mafia-plus-stats",
        )
    except (TelegramError, OSError, ValueError, sqlite3.Error):
        try:
            database.reject_pending_purchase(payload)
        except sqlite3.Error:
            logger.exception("Не удалось закрыть неотправленный счёт Mafia+")
        logger.exception("Не удалось создать счёт Mafia+ для пользователя %s", user.id)
        await query.message.reply_text("Не удалось создать счёт. Попробуй позже или напиши /paysupport.")
    await query.answer("Счёт отправлен.")


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query is None:
        return
    try:
        purchase = database.get_purchase_by_payload(query.invoice_payload)
        valid = bool(
            MAFIA_PLUS_PAYMENTS_ENABLED
            and purchase is not None
            and purchase["status"] == "pending"
            and int(purchase["telegram_id"]) == query.from_user.id
            and purchase["product_id"] == PRODUCT_ID
            and int(purchase["amount_stars"]) == query.total_amount == MAFIA_PLUS_PRICE_STARS
            and purchase["currency"] == query.currency == CURRENCY
            and query.invoice_payload.startswith(f"{PRODUCT_ID}:{query.from_user.id}:")
            and not has_mafia_plus_access(query.from_user.id)
        )
    except sqlite3.Error:
        logger.exception("Не удалось проверить предоплату Telegram Stars")
        valid = False
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="Не удалось проверить заказ. Платёж отменён; напиши /paysupport, если нужна помощь.",
        )


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    payment = message.successful_payment if message else None
    if user is None or payment is None:
        return
    try:
        result = database.record_successful_star_payment(
            telegram_id=user.id,
            product_id=PRODUCT_ID,
            amount_stars=payment.total_amount,
            currency=payment.currency,
            invoice_payload=payment.invoice_payload,
            charge_id=payment.telegram_payment_charge_id,
        )
    except (OSError, ValueError, sqlite3.Error):
        logger.exception("Не удалось сохранить подтверждённый платёж пользователя %s", user.id)
        await message.reply_text("Платёж получен, но мы не смогли обновить доступ автоматически. Напиши /paysupport.")
        return

    if result == "paid":
        await message.reply_text(
            "✅ Оплата подтверждена. Mafia+ активирован!\n"
            "🎮 Расширенная статистика доступна по кнопке ниже.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Перейти к статистике", callback_data="mafia:stats")
            ]]),
        )
        return
    if result == "duplicate":
        await message.reply_text("Этот платёж уже был обработан. Статус доступа смотри в /mypurchases.")
        return
    logger.warning("Платёж не принят для выдачи доступа: user=%s result=%s", user.id, result)
    await message.reply_text(
        "Не удалось сопоставить платёж с заказом, поэтому доступ не был выдан. "
        "Напиши /paysupport; сохрани сообщение-квитанцию Telegram."
    )


def _purchases_text(telegram_id: int) -> str:
    purchases = database.get_user_purchases(telegram_id)
    if not purchases:
        return "Покупок пока нет. Условия Mafia+ можно посмотреть командой /buy."
    labels = {"pending": "ожидает оплаты", "paid": "оплачена", "refunded": "возвращена", "rejected": "не подтверждена"}
    products = {PRODUCT_ID: "Mafia+", "unknown": "неизвестный товар"}
    lines = ["ℹ️ Мои покупки:"]
    for purchase in purchases:
        lines.append(
            f"• #{purchase['id']} — {products.get(purchase['product_id'], purchase['product_id'])}, "
            f"{purchase['amount_stars']} ⭐ — "
            f"{labels.get(purchase['status'], purchase['status'])}"
        )
    return "\n".join(lines)


async def my_purchases_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is not None and user is not None:
        await message.reply_text(_purchases_text(user.id))


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    details = " ".join(context.args).strip() or "вопрос по платежу (добавь детали ответом на это сообщение)"
    if ADMIN_ID <= 0:
        logger.warning("Для /paysupport не задан ADMIN_ID; обращение пользователя %s не доставлено", user.id)
        await message.reply_text("Поддержка пока не настроена. Попробуй позже.")
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Обращение /paysupport от пользователя {user.id} (@{user.username or 'без username'}):\n{details}",
        )
        await message.reply_text("Обращение по оплате отправлено в поддержку.")
    except TelegramError:
        logger.exception("Не удалось переслать /paysupport от пользователя %s", user.id)
        await message.reply_text("Не получилось доставить обращение. Попробуй позже.")


async def refund_stars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not is_admin(user.id):
        await message.reply_text("Команда доступна только администратору.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text("Формат: /refundstars <номер_покупки из /mypurchases>")
        return
    purchase = database.get_purchase_by_id(int(context.args[0]))
    if not purchase or purchase["status"] != "paid" or not purchase["telegram_payment_charge_id"]:
        await message.reply_text("Не найдена оплаченная покупка для возврата.")
        return
    try:
        refunded = await context.bot.refund_star_payment(
            user_id=int(purchase["telegram_id"]),
            telegram_payment_charge_id=purchase["telegram_payment_charge_id"],
        )
    except TelegramError:
        logger.exception("Telegram отклонил Stars refund для покупки %s", purchase["id"])
        await message.reply_text("Telegram не подтвердил возврат. Покупка осталась оплаченной.")
        return
    if refunded and database.mark_purchase_refunded(int(purchase["id"])):
        await message.reply_text("Возврат подтверждён Telegram, доступ Mafia+ отозван.")
    else:
        await message.reply_text("Telegram не подтвердил возврат; статус покупки не изменён.")


def register_payment_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("mypurchases", my_purchases_command))
    app.add_handler(CommandHandler("terms", terms_command))
    app.add_handler(CommandHandler("paysupport", paysupport_command))
    app.add_handler(CommandHandler("refundstars", refund_stars_command))
    app.add_handler(CallbackQueryHandler(_payment_callback, pattern=r"^payments:"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
