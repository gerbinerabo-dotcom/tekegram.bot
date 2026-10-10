from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import mafia_db
import mafia_game


def _room_text(room_id: int) -> str:
    """Формирует текст игрового лобби."""
    room = mafia_db.get_room(room_id)

    if room is None:
        return "Игровая комната не найдена."

    players = mafia_db.get_players(room_id)

    status_names = {
        "lobby": "Регистрация",
        "mafia_count_vote": "Выбор количества мафии",
        "waiting_roles": "Ожидание выдачи ролей",
        "discussion": "Обсуждение",
        "day_vote": "Дневное голосование",
        "night": "Ночь",
        "finished": "Игра завершена",
        "cancelled": "Игра отменена",
    }

    lines = [
        f"🎭 МАФИЯ — игра №{room_id}",
        "",
        f"Статус: {status_names.get(room['status'], room['status'])}",
        f"Игроков: {len(players)}",
        "",
        "👥 Участники:",
    ]

    if players:
        for number, player in enumerate(players, start=1):
            name = player["first_name"]
            if player["username"]:
                name += f" (@{player['username']})"

            lines.append(f"{number}. {name}")
    else:
        lines.append("Пока никто не присоединился.")

    if room["status"] == "lobby":
        lines.extend([
            "",
            f"Для начала нужно минимум {mafia_game.MIN_PLAYERS} игроков.",
            "После регистрации участники проголосуют за количество мафии.",
        ])

    elif room["status"] == "mafia_count_vote":
        votes = mafia_db.get_mafia_count_votes(room_id)
        lines.extend([
            "",
            f"Голосов за 1 мафию: {votes[1]}",
            f"Голосов за 2 мафии: {votes[2]}",
        ])

        if len(players) < mafia_game.TWO_MAFIA_MIN_PLAYERS:
            lines.append(
                "Для двух мафиози нужно минимум "
                f"{mafia_game.TWO_MAFIA_MIN_PLAYERS} игроков."
            )

    elif room["status"] == "waiting_roles":
        lines.extend([
            "",
            "Все участники должны открыть личный чат с ботом.",
            "Нажмите кнопку «🎭 Получить роль» и Start.",
        ])

    return "\n".join(lines)


async def _keyboard(
    room_id: int,
    bot_username: str,
) -> InlineKeyboardMarkup:
    """Создаёт кнопки в зависимости от этапа игры."""
    room = mafia_db.get_room(room_id)

    if room is None:
        return InlineKeyboardMarkup([])

    rows = []
    role_url = (
        f"https://t.me/{bot_username}?start=mafia_{room_id}"
    )

    if room["status"] == "lobby":
        rows.append([
            InlineKeyboardButton(
                "✅ Присоединиться",
                callback_data=f"mafia:join:{room_id}",
            ),
            InlineKeyboardButton(
                "🚪 Выйти",
                callback_data=f"mafia:leave:{room_id}",
            ),
        ])

        rows.append([
            InlineKeyboardButton(
                "🎭 Получить роль",
                url=role_url,
            ),
        ])

        rows.append([
            InlineKeyboardButton(
                "🗳 Начать выбор мафии",
                callback_data=f"mafia:startcount:{room_id}",
            ),
        ])

    elif room["status"] == "mafia_count_vote":
        rows.append([
            InlineKeyboardButton(
                "1️⃣ Одна мафия",
                callback_data=f"mafia:count:1:{room_id}",
            ),
            InlineKeyboardButton(
                "2️⃣ Две мафии",
                callback_data=f"mafia:count:2:{room_id}",
            ),
        ])

        rows.append([
            InlineKeyboardButton(
                "🎭 Получить роль",
                url=role_url,
            ),
        ])

        rows.append([
            InlineKeyboardButton(
                "🏁 Завершить голосование",
                callback_data=f"mafia:endcount:{room_id}",
            ),
        ])

    elif room["status"] == "waiting_roles":
        rows.append([
            InlineKeyboardButton(
                "🎭 Получить роль",
                url=role_url,
            ),
        ])

    return InlineKeyboardMarkup(rows)


async def _refresh_lobby(
    room_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    message,
) -> None:
    """Обновляет текст и кнопки игрового сообщения."""
    bot_info = await context.bot.get_me()

    keyboard = await _keyboard(
        room_id,
        bot_info.username,
    )

    try:
        await message.edit_text(
            _room_text(room_id),
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        # Telegram может сообщить, что сообщение уже не изменилось.
        if "message is not modified" not in str(exc).lower():
            raise


async def mafia_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Создаёт игровое лобби в группе."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    if chat.type not in {"group", "supergroup"}:
        await message.reply_text(
            "🎭 Создавать игру нужно в группе Telegram."
        )
        return

    existing = mafia_db.get_active_room_by_chat(chat.id)

    if existing is not None:
        await message.reply_text(
            f"В этой группе уже есть активная игра №{existing['id']}."
        )
        return

    try:
        room_id = mafia_db.create_room(
            chat_id=chat.id,
            host_user_id=user.id,
        )

        # Создатель автоматически становится первым игроком.
        mafia_game.join_lobby(
            room_id=room_id,
            user_id=user.id,
            first_name=user.first_name or "Игрок",
            username=user.username,
        )

    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    bot_info = await context.bot.get_me()

    sent_message = await message.reply_text(
        _room_text(room_id),
        reply_markup=await _keyboard(
            room_id,
            bot_info.username,
        ),
    )

    mafia_db.update_room(
        room_id,
        message_id=sent_message.message_id,
    )


async def handle_mafia_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обрабатывает кнопки игрового лобби."""
    query = update.callback_query
    user = update.effective_user

    if query is None or user is None or not query.data:
        return

    try:
        parts = query.data.split(":")
        action = parts[1]

        if action in {"join", "leave", "startcount", "endcount"}:
            room_id = int(parts[2])
        elif action == "count":
            choice = int(parts[2])
            room_id = int(parts[3])
        else:
            await query.answer("Неизвестная кнопка.", show_alert=True)
            return

        room = mafia_db.get_room(room_id)

        if room is None:
            await query.answer(
                "Эта игровая комната больше не существует.",
                show_alert=True,
            )
            return

        if action == "join":
            mafia_game.join_lobby(
                room_id=room_id,
                user_id=user.id,
                first_name=user.first_name or "Игрок",
                username=user.username,
            )
            result_message = "Ты присоединился к игре."

        elif action == "leave":
            mafia_game.leave_lobby(room_id, user.id)
            result_message = "Ты вышел из лобби."

        elif action == "startcount":
            if user.id != room["host_user_id"]:
                await query.answer(
                    "Начать голосование может создатель игры.",
                    show_alert=True,
                )
                return

            if room["status"] != "lobby":
                raise mafia_game.MafiaGameError(
                    "Регистрация уже закрыта."
                )

            players = mafia_db.get_players(room_id)

            if len(players) < mafia_game.MIN_PLAYERS:
                raise mafia_game.MafiaGameError(
                    f"Нужно минимум {mafia_game.MIN_PLAYERS} игроков."
                )

            mafia_db.update_room(
                room_id,
                status="mafia_count_vote",
            )
            result_message = "Голосование за число мафиози началось."

        elif action == "count":
            mafia_game.vote_for_mafia_count(
                room_id,
                user.id,
                choice,
            )
            result_message = f"Твой голос за {choice} мафиози учтён."

        else:  # endcount
            if user.id != room["host_user_id"]:
                await query.answer(
                    "Завершить голосование может создатель игры.",
                    show_alert=True,
                )
                return

            result = mafia_game.resolve_mafia_count_vote(room_id)
            result_message = (
                "Голосование завершено. "
                f"Выбрано мафиози: {result['mafia_count']}."
            )

        await query.answer(result_message)

        if query.message is not None:
            await _refresh_lobby(
                room_id,
                context,
                query.message,
            )

    except (ValueError, mafia_game.MafiaGameError) as exc:
        await query.answer(str(exc), show_alert=True)


def register_mafia_handlers(app: Application) -> None:
    """Подготавливает базу и регистрирует команды игры."""
    mafia_db.init_db()

    app.add_handler(
        CommandHandler("mafia", mafia_command)
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_mafia_callback,
            pattern=r"^mafia:",
        )
    )