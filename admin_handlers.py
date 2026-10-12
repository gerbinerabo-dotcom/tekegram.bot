
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from config import ADMIN_ID
from conversation import (
    REPLY_STATE_KEY,
    begin_reply,
    cancel_reply,
    extract_message_data,
)
from filters import require_admin
from keyboards import (
    admin_menu_keyboard,
    broadcast_confirmation_keyboard,
    conversation_actions_keyboard,
    conversation_list_keyboard,
    history_keyboard,
    reply_cancel_keyboard,
    user_actions_keyboard,
    users_list_keyboard,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
ADMIN_INPUT_STATE = "admin_input_state"
BROADCAST_DRAFT = "broadcast_draft"


async def _show_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conversation_id: int,
):
    """Показывает диалог и доступные действия."""

    query = update.callback_query

    conversation = database.get_conversation_by_id(
        conversation_id
    )

    if conversation is None:
        if query and query.message:
            await query.message.reply_text("Диалог не найден.")
        return

    # При открытии диалога помечаем входящие сообщения прочитанными.
    database.mark_conversation_read(conversation_id)

    conversation = database.get_conversation_by_id(
        conversation_id
    )

    if conversation is None:
        return

    messages = database.get_messages(
        conversation_id,
        limit=10,
    )

    name = (
        conversation.get("first_name")
        or conversation.get("username")
        or str(conversation["telegram_id"])
    )

    lines = [
        f"💬 Диалог №{conversation_id}",
        f"Имя: {name}",
        f"Telegram ID: {conversation['telegram_id']}",
        f"Username: @{conversation['username']}"
        if conversation.get("username")
        else "Username: не указан",
        f"Статус: {conversation['status']}",
        "",
        "Последние сообщения:",
    ]

    if not messages:
        lines.append("Сообщений пока нет.")
    else:
        for item in messages:
            sender = (
                "👤 Пользователь"
                if item["sender"] == "user"
                else "🛠 Ты"
            )

            content = (
                item.get("text")
                or item.get("caption")
                or f"[{item['content_type']}]"
            )

            content = str(content).replace("\x00", "")[:250]

            lines.append(
                f"{sender} · {item['created_at']}\n{content}"
            )

    text = "\n\n".join(lines)

    # Telegram ограничивает длину обычного сообщения.
    if len(text) > 3800:
        text = text[:3800]

    if query and query.message:
        await query.message.reply_text(
            text,
            reply_markup=conversation_actions_keyboard(
                conversation_id=conversation_id,
                telegram_id=conversation["telegram_id"],
                is_blocked=bool(conversation["is_blocked"]),
                status=conversation["status"],
            ),
        )


async def _show_conversation_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    unread_only: bool = False,
):
    query = update.callback_query
    page = max(0, page)

    conversations = database.list_conversations(
        unread_only=unread_only,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )

    title = (
        "🔔 Непрочитанные обращения"
        if unread_only
        else "📨 Все обращения"
    )

    if not conversations:
        text = (
            f"{title}\n\n"
            "Обращений в этом разделе пока нет."
        )
    else:
        text = f"{title}\n\nВыбери диалог:"

    if query and query.message:
        await query.message.reply_text(
            text,
            reply_markup=conversation_list_keyboard(
                conversations,
                page=page,
                unread_only=unread_only,
                page_size=PAGE_SIZE,
            ),
        )


async def _show_users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    users=None,
    title: str = "👥 Пользователи",
):
    query = update.callback_query
    page = max(0, page)

    if users is None:
        users = database.list_users(
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )

    if query and query.message:
        await query.message.reply_text(
            f"{title}\n\nНайдено в списке: {len(users)}",
            reply_markup=users_list_keyboard(
                users,
                page=page,
                page_size=PAGE_SIZE,
            ),
        )


async def _show_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
):
    query = update.callback_query
    user = database.get_user_by_telegram_id(telegram_id)

    if user is None:
        if query and query.message:
            await query.message.reply_text(
                "Пользователь не найден в базе."
            )
        return

    name = (
        " ".join(
            part for part in [
                user.get("first_name"),
                user.get("last_name"),
            ] if part
        )
        or "Без имени"
    )

    text = (
        "👤 Карточка пользователя\n\n"
        f"Имя: {name}\n"
        f"Username: @{user['username']}"
        if user.get("username")
        else f"👤 Карточка пользователя\n\nИмя: {name}\n"
        "Username: не указан"
    )

    text += (
        f"\nTelegram ID: {user['telegram_id']}"
        f"\nВнутренний номер: {user['id']}"
        f"\nЗаблокирован: {'да' if user['is_blocked'] else 'нет'}"
        f"\nДобавлен: {user['created_at']}"
        f"\nПоследняя активность: {user['last_seen_at']}"
    )

    if query and query.message:
        await query.message.reply_text(
            text,
            reply_markup=user_actions_keyboard(
                telegram_id=telegram_id,
                is_blocked=bool(user["is_blocked"]),
            ),
        )


async def _show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conversation_id: int,
    before_id: int = 0,
):
    query = update.callback_query

    conversation = database.get_conversation_by_id(
        conversation_id
    )

    if conversation is None:
        if query and query.message:
            await query.message.reply_text("Диалог не найден.")
        return

    messages = database.get_messages(
        conversation_id,
        limit=51,
        before_id=before_id if before_id > 0 else None,
    )

    has_older = len(messages) > 50

    # Если есть более старые записи, оставляем последние 50
    # из полученной страницы.
    if has_older:
        messages = messages[1:]

    lines = [
        f"📜 История диалога №{conversation_id}",
        "",
    ]

    if not messages:
        lines.append("История пока пуста.")
    else:
        for item in messages:
            sender = (
                "Пользователь"
                if item["sender"] == "user"
                else "Ты"
            )

            content = (
                item.get("text")
                or item.get("caption")
                or f"[{item['content_type']}]"
            )

            content = str(content).replace("\x00", "")[:180]

            lines.append(
                f"#{item['id']} · {sender}\n"
                f"{item['created_at']}\n{content}"
            )

    text = "\n\n".join(lines)

    if len(text) > 3800:
        text = text[:3800]

    oldest_id = messages[0]["id"] if messages else None

    if query and query.message:
        await query.message.reply_text(
            text,
            reply_markup=history_keyboard(
                conversation_id=conversation_id,
                oldest_message_id=oldest_id,
                has_older=has_older,
            ),
        )


async def _show_statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    stats = database.get_statistics()

    text = (
        "📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {stats['users']}\n"
        f"🚫 Заблокировано: {stats['blocked_users']}\n"
        f"💬 Всего диалогов: {stats['conversations']}\n"
        f"🟢 Открытых диалогов: {stats['open_conversations']}\n"
        f"📨 Всего сообщений: {stats['messages']}\n"
        f"📅 Сообщений сегодня (UTC): {stats['messages_today']}\n"
        f"✍️ Ответов сегодня (UTC): {stats['replies_today']}\n"
        f"🔔 Непрочитанных обращений: {stats['unread_messages']}"
    )

    if query and query.message:
        await query.message.reply_text(
            text,
            reply_markup=admin_menu_keyboard(),
        )


async def handle_admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Обработчик всех кнопок админ-панели."""

    query = update.callback_query

    if query is None or not query.data:
        return

    data = query.data

    # Эти действия обрабатываются отдельными функциями.
    if data.startswith("reply:cancel:"):
        try:
            conversation_id = int(data.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Некорректный номер диалога.", show_alert=True)
            return
        await cancel_reply(update, context, conversation_id)
        return

    if data.startswith("reply:"):
        try:
            conversation_id = int(data.split(":")[1])
        except (IndexError, ValueError):
            await query.answer("Некорректный номер диалога.", show_alert=True)
            return

        context.user_data.pop(ADMIN_INPUT_STATE, None)
        context.user_data.pop(BROADCAST_DRAFT, None)

        await begin_reply(update, context, conversation_id)
        return

    # Все остальные административные действия защищаем проверкой ID.
    if not await require_admin(update, context):
        return

    await query.answer()

    try:
        if data == "admin:home":
            if query.message:
                await query.message.reply_text(
                    "🛠 Админ-панель",
                    reply_markup=admin_menu_keyboard(),
                )

        elif data.startswith("admin:inbox:"):
            page = int(data.split(":")[2])
            await _show_conversation_list(
                update, context, page=page
            )

        elif data.startswith("admin:unread:"):
            page = int(data.split(":")[2])
            await _show_conversation_list(
                update,
                context,
                page=page,
                unread_only=True,
            )

        elif data.startswith("admin:users:"):
            page = int(data.split(":")[2])
            await _show_users(update, context, page=page)

        elif data == "admin:stats":
            await _show_statistics(update, context)

        elif data == "admin:search":
            context.user_data[ADMIN_INPUT_STATE] = "search"

            if query.message:
                await query.message.reply_text(
                    "🔎 Отправь username, имя или Telegram ID "
                    "для поиска.\n\n"
                    "Чтобы отменить, отправь /cancel."
                )

        elif data == "admin:broadcast":
            context.user_data[ADMIN_INPUT_STATE] = "broadcast"

            if query.message:
                await query.message.reply_text(
                    "📢 Отправь текст, фотографию, видео, GIF "
                    "или другой тип сообщения для рассылки.\n\n"
                    "Перед отправкой всем пользователям бот "
                    "покажет подтверждение."
                )

        elif data.startswith("conv:"):
            conversation_id = int(data.split(":")[1])
            await _show_conversation(
                update, context, conversation_id
            )

        elif data.startswith("history:"):
            parts = data.split(":")
            conversation_id = int(parts[1])
            before_id = int(parts[2]) if len(parts) > 2 else 0

            await _show_history(
                update,
                context,
                conversation_id,
                before_id,
            )

        elif data.startswith("read:"):
            conversation_id = int(data.split(":")[1])
            database.mark_conversation_read(conversation_id)

            await query.answer("Обращение помечено прочитанным.")

        elif data.startswith("close:"):
            conversation_id = int(data.split(":")[1])

            if database.set_conversation_status(
                conversation_id, "closed"
            ):
                await _show_conversation(
                    update, context, conversation_id
                )

        elif data.startswith("reopen:"):
            conversation_id = int(data.split(":")[1])

            if database.set_conversation_status(
                conversation_id, "open"
            ):
                await _show_conversation(
                    update, context, conversation_id
                )

        elif data.startswith("block:"):
            telegram_id = int(data.split(":")[1])

            if database.set_user_blocked(telegram_id, True):
                await query.answer("Пользователь заблокирован.")
                await _show_user(update, context, telegram_id)
            else:
                await query.answer("Пользователь не найден.", show_alert=True)

        elif data.startswith("unblock:"):
            telegram_id = int(data.split(":")[1])

            if database.set_user_blocked(telegram_id, False):
                await query.answer("Пользователь разблокирован.")
                await _show_user(update, context, telegram_id)
            else:
                await query.answer("Пользователь не найден.", show_alert=True)

        elif data.startswith("user:"):
            telegram_id = int(data.split(":")[1])
            await _show_user(update, context, telegram_id)

        elif data.startswith("userconv:"):
            telegram_id = int(data.split(":")[1])
            user = database.get_user_by_telegram_id(telegram_id)

            if user is None:
                if query.message:
                    await query.message.reply_text(
                        "Пользователь не найден."
                    )
                return

            database.get_or_create_conversation(telegram_id)

            # get_or_create_conversation может обновить профиль,
            # поэтому восстанавливаем известные данные пользователя.
            database.upsert_user(
                telegram_id=telegram_id,
                username=user.get("username"),
                first_name=user.get("first_name"),
                last_name=user.get("last_name"),
            )

            conversation = database.get_conversation_by_user(
                telegram_id
            )

            if conversation:
                await _show_conversation(
                    update, context, conversation["id"]
                )

        elif data == "broadcast:cancel":
            context.user_data.pop(BROADCAST_DRAFT, None)
            context.user_data.pop(ADMIN_INPUT_STATE, None)

            if query.message:
                await query.message.reply_text(
                    "❌ Рассылка отменена.",
                    reply_markup=admin_menu_keyboard(),
                )

        elif data == "broadcast:confirm":
            draft = context.user_data.get(BROADCAST_DRAFT)

            if not draft:
                await query.answer(
                    "Нет подготовленной рассылки.",
                    show_alert=True,
                )
                return

            context.user_data.pop(BROADCAST_DRAFT, None)
            context.user_data.pop(ADMIN_INPUT_STATE, None)

            if query.message:
                await query.message.reply_text(
                    "⏳ Рассылка запущена. Дождись отчёта."
                )

            try:
                # Этот модуль создадим на следующем этапе.
                from broadcast import execute_broadcast

                result = await execute_broadcast(
                    context,
                    query.message.chat_id if query.message else ADMIN_ID,
                    draft,
                )

                if query.message:
                    await query.message.reply_text(
                        "✅ Рассылка завершена.\n\n"
                        f"Всего адресатов: {result['total']}\n"
                        f"Доставлено: {result['sent']}\n"
                        f"Ошибок: {result['failed']}"
                    )

            except Exception:
                logger.exception("Ошибка выполнения рассылки")

                if query.message:
                    await query.message.reply_text(
                        "⚠️ Во время рассылки возникла ошибка. "
                        "Подробности записаны в журнал."
                    )

        else:
            await query.answer(
                "Неизвестное действие.",
                show_alert=True,
            )

    except (ValueError, IndexError, KeyError):
        logger.exception("Некорректные данные кнопки: %s", data)

        if query.message:
            await query.message.reply_text(
                "⚠️ Не удалось обработать это действие. "
                "Попробуй снова."
            )


async def handle_admin_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Принимает ввод для поиска и подготовки рассылки."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if user.id != ADMIN_ID or ADMIN_ID <= 0:
        return

    # Режим ответа обрабатывает conversation.py.
    if context.user_data.get(REPLY_STATE_KEY) is not None:
        return

    state = context.user_data.get(ADMIN_INPUT_STATE)

    if state == "search":
        if not message.text:
            await message.reply_text(
                "Для поиска отправь текст, username или Telegram ID."
            )
            return

        query_text = message.text.strip()

        if query_text == "/cancel":
            context.user_data.pop(ADMIN_INPUT_STATE, None)
            await message.reply_text("Поиск отменён.")
            return

        context.user_data.pop(ADMIN_INPUT_STATE, None)

        users = database.search_users(query_text, limit=PAGE_SIZE)

        if not users:
            await message.reply_text(
                "Ничего не найдено. Проверь запрос и попробуй снова.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        await message.reply_text(
            f"🔎 Результаты поиска: {len(users)}",
            reply_markup=users_list_keyboard(
                users,
                page=0,
                page_size=PAGE_SIZE,
            ),
        )

    elif state == "broadcast":
        content_type, text, caption, file_id = extract_message_data(
            message
        )

        if content_type == "other" and not message.text:
            await message.reply_text(
                "Этот тип сообщения пока не поддерживается для рассылки. "
                "Попробуй текст, фото, видео, GIF или стикер."
            )
            return

        context.user_data.pop(ADMIN_INPUT_STATE, None)

        recipients = database.get_all_user_telegram_ids(
            include_blocked=False
        )

        context.user_data[BROADCAST_DRAFT] = {
            "from_chat_id": message.chat_id,
            "message_id": message.message_id,
            "content_type": content_type,
            "text": text,
            "caption": caption,
            "file_id": file_id,
            "total_count": len(recipients),
        }

        preview = text or caption or f"[{content_type}]"

        await message.reply_text(
            "📋 Проверь рассылку перед отправкой.\n\n"
            f"Тип сообщения: {content_type}\n"
            f"Содержание: {preview[:500]}\n\n"
            f"Получателей: {len(recipients)}\n\n"
            "Подтверди отправку кнопкой ниже или отмени её.",
            reply_markup=broadcast_confirmation_keyboard(),
        )


async def cancel_admin_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Отменяет ввод поиска или подготовку рассылки."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if not await require_admin(update, context):
        return

    context.user_data.pop(ADMIN_INPUT_STATE, None)
    context.user_data.pop(BROADCAST_DRAFT, None)
    context.user_data.pop(REPLY_STATE_KEY, None)

    await message.reply_text(
        "Текущее действие отменено.",
        reply_markup=admin_menu_keyboard(),
    )


def register_admin_handlers(app: Application) -> None:
    """Регистрирует обработчики админ-панели."""

    app.add_handler(
        CommandHandler("admin", open_admin_panel)
    )

    app.add_handler(
        CommandHandler("cancel", cancel_admin_input)
    )

    # Группа -1 позволяет обработать ввод администратора
    # до общего обработчика входящих сообщений.
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_input,
        ),
        group=-1,
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_admin_callback,
            pattern=(
                r"^(admin:|conv:|reply:|history:|block:|unblock:|"
                r"close:|reopen:|read:|user:|userconv:|broadcast:)"
            ),
        )
    )


async def open_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Открывает главное меню администратора."""

    if not await require_admin(update, context):
        return

    message = update.effective_message

    if message:
        await message.reply_text(
            "🛠 Админ-панель\n\nВыбери нужный раздел.",
            reply_markup=admin_menu_keyboard(),
        )
