
from datetime import datetime, timezone

import database


def get_statistics_snapshot() -> dict[str, int]:
    """Возвращает текущие показатели из базы данных."""
    return database.get_statistics()


def format_statistics() -> str:
    """Формирует текст статистики для Telegram."""

    stats = get_statistics_snapshot()
    now = datetime.now(timezone.utc)

    return (
        "📊 СТАТИСТИКА БОТА\n\n"
        f"👥 Всего пользователей: {stats['users']}\n"
        f"🚫 Заблокировано пользователей: {stats['blocked_users']}\n\n"
        f"💬 Всего диалогов: {stats['conversations']}\n"
        f"🟢 Открытых диалогов: {stats['open_conversations']}\n\n"
        f"📨 Всего сообщений: {stats['messages']}\n"
        f"📅 Сообщений сегодня (UTC): {stats['messages_today']}\n"
        f"✍️ Ответов сегодня (UTC): {stats['replies_today']}\n"
        f"🔔 Непрочитанных обращений: {stats['unread_messages']}\n\n"
        f"🕒 Время формирования: {now:%Y-%m-%d %H:%M} UTC"
    )