
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from config import ADMIN_ID
from filters import is_admin, is_blocked_user
from keyboards import (
    conversation_actions_keyboard,
    reply_cancel_keyboard,
)


logger = logging.getLogger(__name__)

REPLY_STATE_KEY = "active_reply_conversation_id"


def extract_message_data(message):
    """Определяет тип сообщения и извлекает его данные."""

    if message.text is not None:
        return "text", message.text, None, None

    if message.photo:
        return "photo", None, message.caption, message.photo[-1].file_id

    if message.animation:
        return (
            "animation", None, message.caption,
            message.animation.file_id,
        )

    if message.sticker:
        return "sticker", None, None, message.sticker.file_id

    if message.video:
        return "video", None, message.caption, message.video.file_id

    if message.document:
        return (
            "document", None, message.caption,
            message.document.file_id,
        )

    if message.audio:
        return "audio", None, message.caption, message.audio.file_id

    if message.voice:
        return "voice", None, message.caption, message.voice.file_id

    if message.video_note:
        return "video_note", None, None, message.video_note.file_id

    if message.contact:
        contact = message.contact
        description = (
            f"Контакт: {contact.first_name}; "
            f"телефон: {contact.phone_number}"
        )
        return "contact", description, None, None

    if message.location:
        location = message.location
        description = (
            f"Широта: {location.latitude}; "
            f"долгота: {location.longitude}"
        )
        return "location", description, None, None

    if message.venue:
        venue = message.venue
        return "venue", venue.title, venue.address, None

    return "other", None, message.caption, None


def _get_user_name(user) -> str:
    """Возвращает имя без ссылки на личный аккаунт."""

    name = user.first_name or "Без имени"

    if user.last_name:
        name += f" {user.last_name}"

    return name


async def handle_user_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Принимает сообщения пользователей и передаёт их админу."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    # Бот предназначен для личных диалогов.
    if message.chat.type != "private":
        return

    # Сообщения администратора обрабатываем отдельно.
    if is_admin(user.id):
        if context.user_data.get(REPLY_STATE_KEY) is not None:
            await handle_admin_reply(update, context)
        return

    # Обновляем профиль пользователя.
    database.upsert_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    # Проверяем блокировку.
    if is_blocked_user(user.id):
        await message.reply_text(
            "К сожалению, отправка обращений сейчас недоступна."
        )
        return

    if ADMIN_ID <= 0:
        logger.error("ADMIN_ID не настроен.")
        await message.reply_text(
            "Сервис временно недоступен. Попробуйте позже."
        )
        return

    # Получаем существующий диалог или создаём новый.
    conversation = database.get_conversation_by_user(user.id)

    if conversation is None:
        database.get_or_create_conversation(user.id)

        # Восстанавливаем профиль после создания диалога.
        database.upsert_user(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )

        conversation = database.get_conversation_by_user(user.id)

    if conversation is None:
        logger.error("Не удалось создать диалог пользователя %s", user.id)
        await message.reply_text(
            "Не удалось сохранить обращение. Попробуйте позже."
        )
        return

    conversation_id = conversation["id"]

    # Новое сообщение вновь открывает закрытый диалог.
    if conversation["status"] == "closed":
        database.set_conversation_status(conversation_id, "open")

    content_type, text, caption, file_id = extract_message_data(message)

    # Сохраняем входящее сообщение и увеличиваем счётчик непрочитанного.
    database.add_message(
        conversation_id=conversation_id,
        sender="user",
        content_type=content_type,
        text=text,
        caption=caption,
        file_id=file_id,
        telegram_message_id=message.message_id,
        is_read=False,
    )

    username = (
        f"@{user.username}"
        if user.username
        else "не указан"
    )

    preview = text or caption or f"[{content_type}]"
    preview = preview[:700]

    notification_text = (
        "📨 НОВОЕ АНОНИМНОЕ ОБРАЩЕНИЕ\n\n"
        f"Диалог №{conversation_id}\n"
        f"Имя: {_get_user_name(user)}\n"
        f"Username: {username}\n"
        f"Telegram ID: {user.id}\n"
        f"Содержание: {preview}"
    )

    reply_markup = conversation_actions_keyboard(
        conversation_id=conversation_id,
        telegram_id=user.id,
        is_blocked=False,
        status="open",
    )

    try:
        # Отдельное уведомление с кнопками управления.
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=notification_text,
            reply_markup=reply_markup,
        )

        # Копируем сообщение без ссылки на личный аккаунт отправителя.
        try:
            await context.bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        except TelegramError:
            logger.exception(
                "Не удалось скопировать сообщение %s",
                message.message_id,
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ Не удалось скопировать оригинал сообщения. "
                    "Его доступные данные сохранены в SQLite."
                ),
            )

    except TelegramError:
        logger.exception(
            "Не удалось уведомить администратора о диалоге %s",
            conversation_id,
        )
        await message.reply_text(
            "Сообщение сохранено, но сейчас не удалось "
            "уведомить администратора. Попробуйте позже."
        )


async def begin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conversation_id: int,
):
    """Включает режим ответа для выбранного диалога."""

    query = update.callback_query

    if query is None:
        return

    if not is_admin(update.effective_user.id if update.effective_user else None):
        await query.answer("Доступ запрещён.", show_alert=True)
        return

    conversation = database.get_conversation_by_id(conversation_id)

    if conversation is None:
        await query.answer("Диалог не найден.", show_alert=True)
        return

    context.user_data[REPLY_STATE_KEY] = conversation_id

    await query.answer()

    if query.message is not None:
        await query.message.reply_text(
            f"✍️ Режим ответа включён для диалога №{conversation_id}.\n\n"
            "Отправь следующим сообщением текст, фото, GIF, стикер "
            "или другой поддерживаемый тип сообщения.\n"
            "Бот доставит его пользователю от своего имени.",
            reply_markup=reply_cancel_keyboard(),
        )


async def cancel_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Отменяет текущий режим ответа."""

    query = update.callback_query

    if query is None:
        return

    if not is_admin(update.effective_user.id if update.effective_user else None):
        await query.answer("Доступ запрещён.", show_alert=True)
        return

    context.user_data.pop(REPLY_STATE_KEY, None)

    await query.answer("Ответ отменён.")

    if query.message is not None:
        await query.message.reply_text("❌ Режим ответа отменён.")


async def handle_admin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Доставляет сообщение администратора выбранному пользователю."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if not is_admin(user.id):
        return

    conversation_id = context.user_data.get(REPLY_STATE_KEY)

    if conversation_id is None:
        return

    conversation = database.get_conversation_by_id(conversation_id)

    if conversation is None:
        context.user_data.pop(REPLY_STATE_KEY, None)
        await message.reply_text("Диалог больше не существует.")
        return

    recipient_id = conversation["telegram_id"]

    content_type, text, caption, file_id = extract_message_data(message)

    try:
        # Копируем сообщение от бота, не раскрывая личный аккаунт админа.
        await context.bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )

        # Записываем исходящий ответ в историю только после отправки.
        database.add_message(
            conversation_id=conversation_id,
            sender="admin",
            content_type=content_type,
            text=text,
            caption=caption,
            file_id=file_id,
            telegram_message_id=message.message_id,
            is_read=True,
        )

        context.user_data.pop(REPLY_STATE_KEY, None)

        await message.reply_text(
            f"✅ Ответ отправлен пользователю из диалога №{conversation_id}."
        )

    except TelegramError:
        logger.exception(
            "Не удалось отправить ответ в диалог %s",
            conversation_id,
        )
        await message.reply_text(
            "⚠️ Не удалось доставить сообщение. "
            "Проверь, доступен ли пользователь, и попробуй ещё раз "
            "или отмени ответ кнопкой."
        )