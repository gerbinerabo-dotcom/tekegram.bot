
import asyncio
import logging

from telegram.error import RetryAfter, TelegramError

import database

logger = logging.getLogger(__name__)

# Пауза между отправками для снижения риска превышения лимитов Telegram.
SEND_INTERVAL = 0.05


async def execute_broadcast(
    context,
    admin_chat_id: int,
    draft: dict,
) -> dict[str, int]:
    """Отправляет подтверждённую рассылку зарегистрированным пользователям.

    Сообщение копируется от имени бота. Личный аккаунт администратора
    получателям не раскрывается.
    """

    from_chat_id = draft.get("from_chat_id")
    message_id = draft.get("message_id")

    if not from_chat_id or not message_id:
        raise ValueError(
            "Не найден исходный чат или ID сообщения для рассылки."
        )

    recipients = database.get_all_user_telegram_ids(
        include_blocked=False
    )

    total = len(recipients)

    broadcast_id = database.create_broadcast(
        content_type=draft.get("content_type", "text"),
        text=draft.get("text"),
        caption=draft.get("caption"),
        file_id=draft.get("file_id"),
        total_count=total,
    )

    sent = 0
    failed = 0

    try:
        for recipient_id in recipients:
            try:
                await context.bot.copy_message(
                    chat_id=recipient_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                sent += 1

            except RetryAfter as exc:
                # Telegram просит временно снизить частоту запросов.
                await asyncio.sleep(float(exc.retry_after) + 1)

                try:
                    await context.bot.copy_message(
                        chat_id=recipient_id,
                        from_chat_id=from_chat_id,
                        message_id=message_id,
                    )
                    sent += 1

                except TelegramError:
                    failed += 1
                    logger.warning(
                        "Не удалось отправить рассылку пользователю %s",
                        recipient_id,
                        exc_info=True,
                    )

            except TelegramError:
                failed += 1
                logger.warning(
                    "Ошибка отправки рассылки пользователю %s",
                    recipient_id,
                    exc_info=True,
                )

            # Небольшая пауза между адресатами.
            await asyncio.sleep(SEND_INTERVAL)

    except Exception:
        # Сохраняем частичный результат, если процесс прервался.
        database.finish_broadcast(
            broadcast_id=broadcast_id,
            sent_count=sent,
            failed_count=failed + (total - sent - failed),
            status="partial" if sent else "failed",
        )
        raise

    status = (
        "completed"
        if failed == 0
        else "partial"
        if sent > 0
        else "failed"
    )

    database.finish_broadcast(
        broadcast_id=broadcast_id,
        sent_count=sent,
        failed_count=failed,
        status=status,
    )

    logger.info(
        "Рассылка %s завершена: всего=%s, доставлено=%s, ошибок=%s",
        broadcast_id,
        total,
        sent,
        failed,
    )

    return {
        "broadcast_id": broadcast_id,
        "total": total,
        "sent": sent,
        "failed": failed,
    }