
import os
from pathlib import Path

from dotenv import load_dotenv


# Основная папка проекта
BASE_DIR = Path(__file__).resolve().parent

# Загружаем настройки из .env
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE)

# Токен Telegram-бота
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Telegram ID администратора
ADMIN_ID_VALUE = os.getenv("ADMIN_ID", "0").strip()

try:
    ADMIN_ID = int(ADMIN_ID_VALUE)
except ValueError as exc:
    raise RuntimeError(
        "ADMIN_ID должен быть числом. Проверь файл .env."
    ) from exc

# Пути для хранения данных
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

DATABASE_PATH = DATA_DIR / "bot.db"
LOG_FILE = LOGS_DIR / "bot.log"

# Создаём необходимые папки автоматически
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def validate_config() -> None:
    """Проверяет настройки перед запуском бота."""

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN в файле .env."
        )

    if ADMIN_ID <= 0:
        raise RuntimeError(
            "Укажи действительный Telegram ID в ADMIN_ID."
        )