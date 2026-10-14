
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from config import ADMIN_ID
from filters import is_admin, is_blocked_user
from keyboards import admin_menu_keyboard
from conversation import handle_user_message
from mafia_handlers import handle_mafia_start_payload
from finance_handlers import register_finance_handlers
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Обрабатывает команду /start."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    # Бот предназначен для личных сообщений.
    if message.chat.type != "private":
        await message.reply_text(
            "Пожалуйста, напиши боту в личные сообщения."
        )
        return
    # Обрабатываем ссылку получения роли в игре «Мафия».
    if await handle_mafia_start_payload(update, context):
        return

    # Для администратора открываем меню управления.
    if is_admin(user.id):
        await message.reply_text(
            "🛠 Админ-панель\n\n"
            "Добро пожаловать! Выбери нужный раздел.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    # Сохраняем профиль пользователя.
    database.upsert_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    if is_blocked_user(user.id):
        await message.reply_text(
            "⛔ Возможность отправлять обращения "
            "временно ограничена."
        )
        return

    await message.reply_text(
        "Привет! 👋\n\n"
        "Это бот для анонимного общения с администратором.\n\n"
        "📨 Отправь сообщение, фотографию, видео, GIF "
        "или стикер — и оно будет передано администратору.\n\n"
        "💬 Ответ придёт тебе сюда от имени бота. "
        "Личный аккаунт администратора тебе не раскрывается.\n\n"
        "Важно: администратор сможет видеть твой Telegram ID, "
        "имя и username, если он установлен, чтобы отвечать "
        "на сообщения и управлять обращениями.\n\n"
        "Просто напиши своё первое сообщение!\n\n"
        "💰 Финансы — отдельный раздел для личного учёта.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💰 Личные финансы", callback_data="finance:open")
        ]]),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Обрабатывает команду /help."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if is_admin(user.id):
        await message.reply_text(
            "🛠 Команды администратора:\n\n"
            "/start — открыть меню\n"
            "/admin — открыть админ-панель\n"
            "/id — узнать свой Telegram ID\n"
            "/help — помощь"
        )
    else:
        await message.reply_text(
            "ℹ️ Как пользоваться ботом:\n\n"
            "1. Отправь сообщение или поддерживаемое вложение.\n"
            "2. Администратор получит обращение.\n"
            "3. Ответ придёт сюда от имени бота.\n\n"
            "Команды:\n"
            "/start — начать общение\n"
            "/help — помощь\n"
            "/privacy — информация о приватности\n"
            "/finance — личный учёт доходов и расходов\n"
            "/mafiastats — статистика игры «Мафия»\n"
            "/buy — условия и покупка Mafia+\n"
            "/mypurchases — статус покупок\n"
            "/terms — условия Mafia+\n"
            "/paysupport — помощь по оплате"
        )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Показывает Telegram ID текущего пользователя."""

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if is_admin(user.id):
        await message.reply_text(
            f"🆔 Твой Telegram ID: {user.id}\n"
            "Этот ID используется для проверки доступа "
            "к административным функциям."
        )
    else:
        await message.reply_text(
            f"🆔 Твой Telegram ID: {user.id}"
        )


async def privacy_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Объясняет принцип работы анонимного общения."""

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "🔒 Информация о приватности\n\n"
        "• Ответы отправляются от имени Telegram-бота.\n"
        "• Личный аккаунт администратора не раскрывается.\n"
        "• Администратор может видеть твой Telegram ID, "
        "имя, username и содержание обращений.\n"
        "• История сообщений хранится для работы сервиса.\n"
        "• Финансовые записи хранятся в базе бота на сервере; владелец сервера может получить к ним доступ.\n"
        "• Не отправляй пароли, платёжные данные "
        "или другую конфиденциальную информацию."
    )


def register_user_handlers(app: Application) -> None:
    """Регистрирует команды и приём сообщений пользователей."""

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    register_finance_handlers(app)

    # Последним регистрируем обработчик обычных сообщений.
    # Он также передаёт сообщения администратора в режим ответа.
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_user_message,
        )
    )
