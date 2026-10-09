
import logging
import sys

from telegram import Update
from telegram.ext import Application

from config import ADMIN_ID, BOT_TOKEN, LOG_FILE
import database
from admin_handlers import register_admin_handlers
from user_handlers import register_user_handlers


def setup_logging() -> None:
    """Настраивает вывод сообщений в терминал и лог-файл."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )

    # Скрываем слишком подробные HTTP-логи.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Проверь файл .env."
        )

    # Разрешаем первый запуск с ADMIN_ID=0,
    # чтобы получить собственный Telegram ID через команду /id.
    if ADMIN_ID <= 0:
        logger.warning(
            "ADMIN_ID пока не настроен. "
            "Открой бота в Telegram и отправь /id. "
            "Затем укажи полученный ID в .env и перезапусти бота."
        )

    # Создаём таблицы SQLite перед запуском обработчиков.
    database.init_db()
    logger.info("База данных SQLite готова.")

    # Создаём приложение Telegram-бота.
    app = Application.builder().token(BOT_TOKEN).build()

    # Подключаем административные и пользовательские обработчики.
    register_admin_handlers(app)
    register_user_handlers(app)

    logger.info("Telegram-бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()