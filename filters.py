
from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_ID
from database import is_user_blocked


def is_admin(user_id: int | None) -> bool:
    """Проверяет, принадлежит ли Telegram ID администратору."""
    return (
        user_id is not None
        and ADMIN_ID > 0
        and user_id == ADMIN_ID
    )


async def require_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Разрешает действие только администратору."""

    user = update.effective_user

    if user is not None and is_admin(user.id):
        return True

    query = update.callback_query

    if query is not None:
        await query.answer(
            "⛔ Доступ запрещён.",
            show_alert=True,
        )
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            "⛔ Эта функция доступна только администратору."
        )

    return False


def is_blocked_user(user_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь в базе."""
    return is_user_blocked(user_id)