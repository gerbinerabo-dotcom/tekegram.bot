from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def admin_menu_keyboard():
    """Главное меню администратора."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📨 Все обращения",
                callback_data="admin:inbox:0",
            ),
            InlineKeyboardButton(
                "🔔 Непрочитанные",
                callback_data="admin:unread:0",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Пользователи",
                callback_data="admin:users:0",
            ),
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data="admin:stats",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔎 Поиск",
                callback_data="admin:search",
            ),
            InlineKeyboardButton(
                "📢 Рассылка",
                callback_data="admin:broadcast",
            ),
        ],
    ])


def conversation_list_keyboard(
    conversations,
    page=0,
    unread_only=False,
    page_size=20,
):
    """Кнопки списка обращений."""

    section = "unread" if unread_only else "inbox"
    buttons = []

    for conversation in conversations:
        conversation_id = conversation["id"]
        unread = conversation.get("unread_count", 0)

        name = (
            conversation.get("first_name")
            or conversation.get("username")
            or f'Пользователь {conversation.get("telegram_id", "")}'
        )

        username = conversation.get("username")
        if username and conversation.get("first_name"):
            name = f"{name} (@{username})"

        label = f"🔴 {name}" if unread else f"👤 {name}"

        if unread:
            label += f" · {unread}"

        buttons.append([
            InlineKeyboardButton(
                label[:60],
                callback_data=f"conv:{conversation_id}",
            )
        ])

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"admin:{section}:{page - 1}",
            )
        )

    if len(conversations) >= page_size:
        navigation.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"admin:{section}:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append([
        InlineKeyboardButton(
            "🏠 Главное меню",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def conversation_actions_keyboard(
    conversation_id,
    telegram_id,
    is_blocked=False,
    status="open",
):
    """Кнопки управления отдельным диалогом."""

    block_button = (
        InlineKeyboardButton(
            "🔓 Разблокировать",
            callback_data=f"unblock:{telegram_id}",
        )
        if is_blocked
        else InlineKeyboardButton(
            "🚫 Заблокировать",
            callback_data=f"block:{telegram_id}",
        )
    )

    status_button = (
        InlineKeyboardButton(
            "🔄 Открыть диалог",
            callback_data=f"reopen:{conversation_id}",
        )
        if status == "closed"
        else InlineKeyboardButton(
            "✅ Закрыть диалог",
            callback_data=f"close:{conversation_id}",
        )
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✍️ Ответить",
                callback_data=f"reply:{conversation_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "📜 История",
                callback_data=f"history:{conversation_id}:0",
            ),
            InlineKeyboardButton(
                "✓ Прочитано",
                callback_data=f"read:{conversation_id}",
            ),
        ],
        [block_button],
        [status_button],
        [
            InlineKeyboardButton(
                "⬅️ К обращениям",
                callback_data="admin:inbox:0",
            )
        ],
    ])


def users_list_keyboard(users, page=0, page_size=20):
    """Кнопки списка пользователей."""

    buttons = []

    for user in users:
        telegram_id = user["telegram_id"]
        name = (
            user.get("first_name")
            or user.get("username")
            or str(telegram_id)
        )

        marker = "🚫" if user.get("is_blocked") else "👤"

        buttons.append([
            InlineKeyboardButton(
                f"{marker} {name}"[:60],
                callback_data=f"user:{telegram_id}",
            )
        ])

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"admin:users:{page - 1}",
            )
        )

    if len(users) >= page_size:
        navigation.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"admin:users:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append([
        InlineKeyboardButton(
            "🏠 Главное меню",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def user_actions_keyboard(telegram_id, is_blocked=False):
    """Кнопки управления выбранным пользователем."""

    block_button = (
        InlineKeyboardButton(
            "🔓 Разблокировать",
            callback_data=f"unblock:{telegram_id}",
        )
        if is_blocked
        else InlineKeyboardButton(
            "🚫 Заблокировать",
            callback_data=f"block:{telegram_id}",
        )
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💬 Открыть переписку",
                callback_data=f"userconv:{telegram_id}",
            )
        ],
        [block_button],
        [
            InlineKeyboardButton(
                "⬅️ К пользователям",
                callback_data="admin:users:0",
            )
        ],
    ])


def history_keyboard(
    conversation_id,
    oldest_message_id=None,
    has_older=False,
):
    """Навигация по истории переписки."""

    buttons = []

    if has_older and oldest_message_id is not None:
        buttons.append([
            InlineKeyboardButton(
                "⬅️ Более старые сообщения",
                callback_data=(
                    f"history:{conversation_id}:{oldest_message_id}"
                ),
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "💬 К диалогу",
            callback_data=f"conv:{conversation_id}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def reply_cancel_keyboard():
    """Кнопка отмены режима ответа."""

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Отменить ответ",
                callback_data="reply:cancel",
            )
        ]
    ])


def broadcast_confirmation_keyboard():
    """Подтверждение массовой рассылки."""

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Подтвердить рассылку",
                callback_data="broadcast:confirm",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отменить",
                callback_data="broadcast:cancel",
            )
        ],
    ])


def back_to_admin_keyboard():
    """Возврат в главное меню."""

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 Главное меню",
                callback_data="admin:home",
            )
        ]
    ])