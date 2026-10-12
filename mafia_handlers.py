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
from datetime import datetime, timezone
import logging

import mafia_db
import mafia_game

logger = logging.getLogger(__name__)


def _schedule_phase(room_id: int, seconds: float) -> None:
    """Schedules a phase timeout; its state checks make old jobs harmless."""
    job_queue = _application.job_queue if _application else None
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(f"mafia-room-{room_id}"):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name(f"mafia-countdown-{room_id}"):
        job.schedule_removal()
    room = mafia_db.get_room(room_id)
    if room is None:
        return
    job_queue.run_once(
        _phase_timeout,
        max(0.1, seconds),
        data={
            "room_id": room_id,
            "status": room["status"],
            "day_number": room["day_number"],
        },
        name=f"mafia-room-{room_id}",
    )


def _phase_durations(room: dict) -> dict[str, int]:
    if room.get("phase_preset") == "quick":
        return {"discussion": 30, "day_vote": 20, "night": 30}
    return {
        "discussion": mafia_game.DISCUSSION_SECONDS,
        "day_vote": mafia_game.DAY_VOTE_SECONDS,
        "night": mafia_game.NIGHT_SECONDS,
    }


def _schedule_discussion_countdown(room_id: int, duration: int) -> None:
    if _application is None or _application.job_queue is None:
        return
    queue = _application.job_queue
    for job in queue.get_jobs_by_name(f"mafia-countdown-{room_id}"):
        job.schedule_removal()
    warning = 30 if duration >= 60 else 10
    queue.run_once(
        _discussion_countdown,
        max(0.1, duration - warning),
        data={"room_id": room_id, "remaining": warning},
        name=f"mafia-countdown-{room_id}",
    )


async def _discussion_countdown(context: ContextTypes.DEFAULT_TYPE) -> None:
    room_id = int(context.job.data["room_id"])
    remaining = int(context.job.data["remaining"])
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "discussion":
        return
    await context.bot.send_message(
        chat_id=room["chat_id"],
        text=f"⏳ До голосования осталось {remaining} секунд.",
    )
    if remaining == 30:
        context.job_queue.run_once(
            _discussion_countdown,
            20,
            data={"room_id": room_id, "remaining": 10},
            name=f"mafia-countdown-{room_id}",
        )


_application: Application | None = None


async def _phase_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    room_id = int(data["room_id"])
    room = mafia_db.get_room(room_id)
    if (
        room is None
        or room["status"] != data["status"]
        or int(room["day_number"]) != int(data["day_number"])
    ):
        return

    if room.get("phase_deadline"):
        deadline = datetime.fromisoformat(room["phase_deadline"])
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            _schedule_phase(room_id, remaining)
            if room["status"] == "discussion":
                _schedule_discussion_countdown(room_id, int(remaining))
            return

    if room["status"] == "discussion":
        await _start_day_vote(room_id, context)
    elif room["status"] == "day_vote":
        await _finish_day_vote(room_id, context)
    elif room["status"] == "night":
        await _finish_night(room_id, context)


async def _start_day_vote(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "discussion":
        return
    durations = _phase_durations(room)
    mafia_game.start_day_vote(room_id, durations["day_vote"])
    _schedule_phase(room_id, durations["day_vote"])
    await _refresh_room_post(room_id, context)
    players = [p for p in mafia_db.get_players(room_id) if p["is_alive"]]
    buttons = [
        [InlineKeyboardButton(
            f"🗳 {p['first_name']}",
            callback_data=f"mafia:vote:{room_id}:{p['user_id']}",
        )]
        for p in players
    ]
    await context.bot.send_message(
        chat_id=room["chat_id"],
        text=f"☀️ День {room['day_number']}: голосуйте, кого исключить. Голосование длится {durations['day_vote']} секунд.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _finish_day_vote(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "day_vote":
        return
    durations = _phase_durations(room)
    result = mafia_game.resolve_day_vote(room_id, durations["night"])
    if result["winner"]:
        await _refresh_room_post(room_id, context)
        await _announce_winner(room, result["winner"], context)
        return
    if result["status"] in {"tie", "no_votes"}:
        mafia_game.start_night(room_id, durations["night"])
        outcome = "Голоса разделились — никто не выбыл." if result["status"] == "tie" else "Голосов не было — никто не выбыл."
    else:
        player = mafia_db.get_player(room_id, int(result["eliminated"]))
        outcome = f"Игрок {player['first_name'] if player else 'участник'} выбыл. Его роль: {mafia_game.ROLE_LABELS.get(result['eliminated_role'], result['eliminated_role'])}."
    _schedule_phase(room_id, durations["night"])
    await context.bot.send_message(
        chat_id=room["chat_id"],
        text=f"📣 {outcome}\n🌙 Наступает ночь.",
    )
    await _refresh_room_post(room_id, context)
    await _start_night(room_id, context)


async def _start_night(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "night":
        return
    players = mafia_db.get_players(room_id)
    alive = [p for p in players if p["is_alive"]]
    names = {int(p["user_id"]): p["first_name"] for p in alive}
    prompts = {
        "mafia": "🌙 Ночь: выбери цель для мафии.",
        "don": "🌙 Ночь: выбери цель для мафии.",
        "doctor": "🌙 Ночь: выбери, кого защитить.",
        "detective": "🌙 Ночь: выбери игрока для проверки.",
        "sheriff": "🌙 Ночь: выбери игрока для проверки.",
        "maniac": "🌙 Ночь: выбери, кого устранить.",
    }
    for actor in alive:
        role = actor["role"]
        if role not in prompts:
            continue
        targets = [p for p in alive if int(p["user_id"]) != int(actor["user_id"])]
        if role in {"mafia", "don"}:
            targets = [p for p in targets if p["role"] not in {"mafia", "don"}]
        if role in {"detective", "sheriff"}:
            targets = [p for p in targets if int(p["user_id"]) != int(actor["user_id"])]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                names[int(target["user_id"])],
                callback_data=f"mafia:night:{room_id}:{target['user_id']}",
            )]
            for target in targets
        ])
        try:
            await context.bot.send_message(
                chat_id=int(actor["user_id"]),
                text=prompts[role],
                reply_markup=keyboard,
            )
        except TelegramError:
            pass
    _schedule_phase(room_id, _phase_durations(room)["night"])


async def _refresh_room_post(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room.get("message_id") is None:
        return
    try:
        bot_info = await context.bot.get_me()
        await context.bot.edit_message_text(
            chat_id=room["chat_id"],
            message_id=room["message_id"],
            text=_room_text(room_id),
            reply_markup=await _keyboard(room_id, bot_info.username),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Не удалось обновить сообщение игры %s: %s", room_id, exc)
    except TelegramError:
        logger.exception("Не удалось обновить сообщение игры %s", room_id)


async def _maybe_finish_night(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "night":
        return
    players = [p for p in mafia_db.get_players(room_id) if p["is_alive"]]
    votes = {
        role: {int(v["voter_id"]) for v in mafia_db.get_action_votes(room_id, int(room["day_number"]), role)}
        for role in ("mafia", "doctor", "detective", "sheriff", "maniac")
    }
    required = {
        "mafia": {int(p["user_id"]) for p in players if p["role"] in {"mafia", "don"}},
        "doctor": {int(p["user_id"]) for p in players if p["role"] == "doctor"},
        "detective": {int(p["user_id"]) for p in players if p["role"] == "detective"},
        "sheriff": {int(p["user_id"]) for p in players if p["role"] == "sheriff"},
        "maniac": {int(p["user_id"]) for p in players if p["role"] == "maniac"},
    }
    if all(not required[role] or required[role] <= votes[role] for role in required):
        await _finish_night(room_id, context)


async def _finish_night(room_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "night":
        return
    durations = _phase_durations(room)
    result = mafia_game.resolve_night(room_id, durations["discussion"])
    if result["winner"]:
        await _refresh_room_post(room_id, context)
        await _announce_winner(room, result["winner"], context)
        return
    _schedule_phase(room_id, durations["discussion"])
    _schedule_discussion_countdown(room_id, durations["discussion"])
    if not result["killed"]:
        outcome = "Никто не выбыл этой ночью."
    else:
        eliminated = [mafia_db.get_player(room_id, int(user_id)) for user_id in result["killed"]]
        outcome = "Ночью выбыл: " + ", ".join(
            f"{p['first_name']} ({mafia_game.ROLE_LABELS.get(p['role'], p['role'])})"
            for p in eliminated if p
        ) + "."
    await context.bot.send_message(
        chat_id=room["chat_id"],
        text=f"🌅 Наступило утро. {outcome}\nНачинается обсуждение на {durations['discussion']} секунд.",
    )
    await _refresh_room_post(room_id, context)


async def _announce_winner(room: dict, winner: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    side = {
        "mafia": "🎉 Победила мафия!",
        "town": "🎉 Победили мирные жители!",
        "maniac": "🎉 Маньяк остался последним и победил!",
    }.get(winner, "🎉 Игра завершена!")
    players = mafia_db.get_players(int(room["id"]))
    roles = "\n".join(
        f"{p['first_name']}: {mafia_game.ROLE_LABELS.get(p['role'], '—')}"
        for p in players
    )
    await context.bot.send_message(
        chat_id=room["chat_id"],
        text=f"{side}\n\nРоли игроков:\n{roles}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Сыграть ещё раз", callback_data=f"mafia:replay:{room['id']}"),
            InlineKeyboardButton("📊 Моя статистика", callback_data="mafia:stats"),
        ]]),
    )


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
            name += " ✅" if player.get("is_ready") else ""
            name += " 📩" if player.get("dm_ready") else ""
            lines.append(f"{number}. {name}")
    else:
        lines.append("Пока никто не присоединился.")

    if room["status"] == "lobby":
        ready_count = sum(1 for player in players if player["is_ready"])
        dm_count = sum(1 for player in players if player["dm_ready"])
        lines.extend([
            "",
            f"Для начала нужно минимум {mafia_game.MIN_PLAYERS} игроков.",
            f"Готовы: {ready_count}/{len(players)} · открыли личный чат: {dm_count}/{len(players)}.",
            "⚡ Для быстрой игры с одной мафией используйте «Быстрый старт».",
            "Для больших партий сохранено голосование за количество мафии.",
            f"Темп фаз: {'быстрый' if room.get('phase_preset') == 'quick' else 'обычный'}.",
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
        ready_count = sum(1 for player in players if player["dm_ready"])
        lines.extend([
            "",
            f"Личный чат открыли: {ready_count} из {len(players)} игроков.",
            "Каждый участник должен нажать «🎭 Получить роль» и Start.",
            "Когда все будут готовы, бот выдаст роли автоматически.",
        ])
    elif room["status"] == "discussion":
        duration = _phase_durations(room)["discussion"]
        lines.extend([
            "",
            f"☀️ День {room['day_number']}: обсудите игру.",
            f"Голосование начнётся автоматически через {duration} секунд или раньше по кнопке.",
        ])
    elif room["status"] == "day_vote":
        lines.extend(["", "🗳 Идёт дневное голосование."])
    elif room["status"] == "night":
        lines.extend(["", "🌙 Ночь. Ночные роли получают цели в личные сообщения."])

    return "\n".join(lines)


async def _keyboard(
    room_id: int,
    bot_username: str,
) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру, сохраняя обычный процесс для больших партий."""
    room = mafia_db.get_room(room_id)
    if room is None:
        return InlineKeyboardMarkup([])

    players = mafia_db.get_players(room_id)
    rows = []
    role_url = f"https://t.me/{bot_username}?start=mafia_{room_id}"

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
            InlineKeyboardButton("🎭 Получить роль", url=role_url),
        ])
        rows.append([
            InlineKeyboardButton("🙋 Я готов", callback_data=f"mafia:ready:{room_id}"),
            InlineKeyboardButton(
                "⏱ Темп: быстрый" if room.get("phase_preset") == "quick" else "⏱ Темп: обычный",
                callback_data=f"mafia:preset:{room_id}",
            ),
        ])

        # Добавляем короткий путь только для небольших команд.
        # Большие партии по-прежнему пользуются обычным голосованием.
        if (
            len(players) >= mafia_game.MIN_PLAYERS
            and len(players) < mafia_game.TWO_MAFIA_MIN_PLAYERS
        ):
            rows.append([
                InlineKeyboardButton(
                    "⚡ Быстрый старт (1 мафия)",
                    callback_data=f"mafia:quickstart:{room_id}",
                ),
            ])

        rows.append([
            InlineKeyboardButton(
                "🗳 Начать выбор мафии",
                callback_data=f"mafia:startcount:{room_id}",
            ),
        ])

    elif room["status"] == "mafia_count_vote":
        count_buttons = [InlineKeyboardButton(
            "1️⃣ Одна мафия",
            callback_data=f"mafia:count:1:{room_id}",
        )]
        if len(players) >= mafia_game.TWO_MAFIA_MIN_PLAYERS:
            count_buttons.append(InlineKeyboardButton(
                "2️⃣ Две мафии",
                callback_data=f"mafia:count:2:{room_id}",
            ))
        rows.append(count_buttons)
        rows.append([
            InlineKeyboardButton("🎭 Получить роль", url=role_url),
        ])
        rows.append([
            InlineKeyboardButton(
                "🏁 Завершить голосование",
                callback_data=f"mafia:endcount:{room_id}",
            ),
        ])

    elif room["status"] == "waiting_roles":
        rows.append([
            InlineKeyboardButton("🎭 Получить роль", url=role_url),
        ])

    elif room["status"] == "discussion":
        rows.append([
            InlineKeyboardButton(
                "🗳 Начать голосование сейчас",
                callback_data=f"mafia:startvote:{room_id}",
            ),
        ])

    elif room["status"] == "finished":
        rows.append([
            InlineKeyboardButton("🔁 Сыграть ещё раз", callback_data=f"mafia:replay:{room_id}"),
            InlineKeyboardButton("📊 Моя статистика", callback_data="mafia:stats"),
        ])

    return InlineKeyboardMarkup(rows)


async def _refresh_lobby(
    room_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    message,
) -> None:
    """Обновляет текст и кнопки игрового сообщения."""
    bot_info = await context.bot.get_me()
    keyboard = await _keyboard(room_id, bot_info.username)

    try:
        await message.edit_text(
            _room_text(room_id),
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _role_action_callback(
    action: str,
    room_id: int,
    target_id: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return True
    room = mafia_db.get_room(room_id)
    if room is None:
        await query.answer("Игра не найдена.", show_alert=True)
        return True
    if action == "vote":
        try:
            mafia_game.cast_day_vote(room_id, user.id, target_id)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return True
        await query.answer("Голос учтён.")
        current = mafia_db.get_room(room_id)
        if current is not None:
            votes = mafia_db.get_action_votes(room_id, int(current["day_number"]), "day_vote")
            alive = [p for p in mafia_db.get_players(room_id) if p["is_alive"]]
            voted = {int(v["voter_id"]) for v in votes}
            if {int(p["user_id"]) for p in alive} <= voted:
                await _finish_day_vote(room_id, context)
        return True

    player = mafia_db.get_player(room_id, user.id)
    if player is None:
        await query.answer("Ты не участвуешь в этой игре.", show_alert=True)
        return True
    if player["role"] not in {"mafia", "don", "doctor", "detective", "sheriff", "maniac"}:
        await query.answer("У твоей роли нет ночного действия.", show_alert=True)
        return True
    try:
        phase = "mafia" if player["role"] == "don" else player["role"]
        result = mafia_game.cast_night_action(room_id, user.id, target_id, phase)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return True
    await query.answer("Действие принято.")
    if player["role"] in {"detective", "sheriff"} and result is not None:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🕵️ Результат проверки: {mafia_game.ROLE_LABELS.get(result, result)}",
        )
    await _maybe_finish_night(room_id, context)
    return True


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
        reply_markup=await _keyboard(room_id, bot_info.username),
    )
    mafia_db.update_room(room_id, message_id=sent_message.message_id)


def _statistics_text(user_id: int) -> str:
    stats = mafia_db.get_player_statistics(user_id)
    win_rate = round(100 * stats["wins"] / stats["games"]) if stats["games"] else 0
    return (
        "📊 Твоя статистика в «Мафии»\n\n"
        f"Сыграно партий: {stats['games']}\n"
        f"Побед: {stats['wins']} ({win_rate}%)\n\n"
        f"🔫 Мафия: {stats['mafia_roles']}\n"
        f"🕴 Дон: {stats['don_roles']}\n"
        f"🩺 Доктор: {stats['doctor_roles']}\n"
        f"🕵️ Детектив: {stats['detective_roles']}\n"
        f"⭐ Шериф: {stats['sheriff_roles']}\n"
        f"🔪 Маньяк: {stats['maniac_roles']}\n"
        f"👤 Мирный: {stats['civilian_roles']}"
    )


ROLE_RULES = {
    "mafia": "Каждую ночь вместе с мафией выбирай игрока для устранения.",
    "don": "Ты мафия и голосуешь за ночную цель. Шериф видит тебя как мирного.",
    "doctor": "Каждую ночь выбери игрока, которого хочешь защитить.",
    "detective": "Каждую ночь проверь игрока; результат увидишь только ты.",
    "sheriff": "Каждую ночь проверь игрока. Дона мафии проверка показывает мирным.",
    "maniac": "Ты играешь один против всех. Устраняй игроков ночью; победа — если останешься один.",
    "civilian": "Обсуждай и голосуй днём, чтобы вычислить мафию.",
}


async def mafia_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None and update.effective_user is not None:
        await update.effective_message.reply_text(
            _statistics_text(update.effective_user.id)
        )


async def _deliver_roles(
    room_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reply_message=None,
) -> None:
    """Выдаёт роли всем готовым игрокам и начинает первый день."""
    room = mafia_db.get_room(room_id)
    if room is None or room["status"] != "waiting_roles":
        return

    players = mafia_db.get_players(room_id)
    if len(players) < mafia_game.MIN_PLAYERS:
        message_text = (
            f"Для игры нужно минимум {mafia_game.MIN_PLAYERS} игроков."
        )
        if reply_message is not None:
            await reply_message.reply_text(message_text)
        return

    ready_count = sum(1 for player in players if player["dm_ready"])
    if not all(player["dm_ready"] for player in players):
        await _refresh_room_post(room_id, context)
        message_text = (
            f"Личные чаты подключили {ready_count} из {len(players)} игроков. "
            "Остальным нужно нажать «🎭 Получить роль» и Start."
        )
        if reply_message is not None:
            await reply_message.reply_text(
                "✅ Личный чат подключён!\n\n" + message_text
            )
        else:
            try:
                await context.bot.send_message(
                    chat_id=room["chat_id"],
                    text=f"🎭 Игра №{room_id}: {message_text}",
                )
            except TelegramError:
                pass
        return

    try:
        mafia_game.assign_roles(room_id)
    except ValueError as exc:
        if reply_message is not None:
            await reply_message.reply_text(f"Не удалось назначить роли: {exc}")
        else:
            try:
                await context.bot.send_message(
                    chat_id=room["chat_id"],
                    text=f"Игра №{room_id}: не удалось назначить роли: {exc}",
                )
            except TelegramError:
                pass
        return

    players = mafia_db.get_players(room_id)
    failed_players = []

    for player in players:
        if player["role_sent"]:
            continue

        role = player["role"]
        if role is None:
            failed_players.append(player["first_name"])
            continue

        try:
            await context.bot.send_message(
                chat_id=int(player["user_id"]),
                text=(
                    f"🎭 Твоя роль в игре №{room_id}:\n\n"
                    f"{mafia_game.ROLE_LABELS[role]}\n\n"
                    f"{ROLE_RULES.get(role, '')}\n\n"
                    "Сохраняй свою роль в секрете!"
                ),
            )
            mafia_db.mark_role_sent(room_id, int(player["user_id"]))
        except TelegramError:
            failed_players.append(player["first_name"])

    if failed_players:
        warning = (
            "Не удалось отправить роли некоторым игрокам: "
            + ", ".join(failed_players)
            + ". Проверьте, что они открыли личный чат с ботом."
        )
        if reply_message is not None:
            await reply_message.reply_text(warning)
        else:
            try:
                await context.bot.send_message(
                    chat_id=room["chat_id"],
                    text=f"⚠️ Игра №{room_id}: {warning}",
                )
            except TelegramError:
                pass
        return

    players = mafia_db.get_players(room_id)
    if not all(player["role_sent"] and player["dm_ready"] for player in players):
        return

    try:
        discussion_seconds = _phase_durations(room)["discussion"]
        mafia_game.begin_game_after_roles_sent(room_id, discussion_seconds)
    except ValueError as exc:
        error_text = f"Роли отправлены, но не удалось начать игру: {exc}"
        if reply_message is not None:
            await reply_message.reply_text(error_text)
        else:
            try:
                await context.bot.send_message(
                    chat_id=room["chat_id"],
                    text=f"Игра №{room_id}: {error_text}",
                )
            except TelegramError:
                pass
        return

    # Убираем устаревшие кнопки с исходного сообщения лобби.
    try:
        bot_info = await context.bot.get_me()
        keyboard = await _keyboard(room_id, bot_info.username)
        if room.get("message_id") is not None:
            await context.bot.edit_message_text(
                chat_id=room["chat_id"],
                message_id=room["message_id"],
                text=_room_text(room_id),
                reply_markup=keyboard,
            )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            pass
    except TelegramError:
        pass

    try:
        await context.bot.send_message(
            chat_id=room["chat_id"],
            text=(
                f"🎭 Игра №{room_id}: все роли выданы!\n"
                f"Начинается обсуждение на {_phase_durations(mafia_db.get_room(room_id))['discussion']} секунд."
            ),
        )
    except TelegramError:
        pass

    discussion_seconds = _phase_durations(mafia_db.get_room(room_id))["discussion"]
    _schedule_phase(room_id, discussion_seconds)
    _schedule_discussion_countdown(room_id, discussion_seconds)

    if reply_message is not None:
        await reply_message.reply_text(
            "✅ Все роли выданы! Игра начинается.\n"
            "Возвращайся в групповой чат."
        )


async def handle_mafia_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Обрабатывает кнопки лобби, голосования и быстрого старта."""
    query = update.callback_query
    user = update.effective_user

    if query is None or user is None or not query.data:
        return

    try:
        parts = query.data.split(":")
        action = parts[1]

        if action == "stats":
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(_statistics_text(user.id))
            return

        if action in {"vote", "night"}:
            room_id = int(parts[2])
            target_id = int(parts[3])
            await _role_action_callback(action, room_id, target_id, update, context)
            return
        if action == "startvote":
            room_id = int(parts[2])
        elif action in {"join", "leave", "startcount", "endcount", "quickstart", "ready", "preset", "replay"}:
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
            if room["status"] != "lobby":
                raise mafia_game.MafiaGameError(
                    "Регистрация в этой комнате уже закрыта."
                )

            existing_player = mafia_db.get_player(room_id, user.id)
            if existing_player is not None:
                await query.answer(
                    "✅ Ты уже участвуешь в этой игре!",
                    show_alert=True,
                )
                return

            mafia_game.join_lobby(
                room_id=room_id,
                user_id=user.id,
                first_name=user.first_name or "Игрок",
                username=user.username,
            )
            result_message = "✅ Ты присоединился к игре!"

        elif action == "ready":
            if room["status"] != "lobby":
                raise mafia_game.MafiaGameError("Отметиться готовым можно только в лобби.")
            if mafia_db.get_player(room_id, user.id) is None:
                raise mafia_game.MafiaGameError("Сначала присоединись к лобби.")
            mafia_db.set_player_ready(room_id, user.id, True)
            result_message = "🙋 Готовность отмечена. Открой также личный чат с ботом для получения роли."

        elif action == "preset":
            if user.id != room["host_user_id"]:
                await query.answer("Темп игры меняет создатель лобби.", show_alert=True)
                return
            if room["status"] != "lobby":
                raise mafia_game.MafiaGameError("Темп можно выбрать только до старта.")
            preset = "normal" if room.get("phase_preset") == "quick" else "quick"
            mafia_db.update_room(room_id, phase_preset=preset)
            result_message = "⏱ Темп установлен: " + ("быстрый" if preset == "quick" else "обычный")

        elif action == "replay":
            if user.id != room["host_user_id"]:
                await query.answer("Повторную игру может создать ведущий прошлой партии.", show_alert=True)
                return
            if room["status"] != "finished":
                raise mafia_game.MafiaGameError("Повторная игра доступна после завершения партии.")
            new_room_id = mafia_db.create_room(room["chat_id"], user.id)
            mafia_db.update_room(new_room_id, phase_preset=room.get("phase_preset", "normal"))
            mafia_game.join_lobby(new_room_id, user.id, user.first_name or "Игрок", user.username)
            bot_info = await context.bot.get_me()
            posted = await context.bot.send_message(
                chat_id=room["chat_id"],
                text=_room_text(new_room_id),
                reply_markup=await _keyboard(new_room_id, bot_info.username),
            )
            mafia_db.update_room(new_room_id, message_id=posted.message_id)
            await query.answer("Новое лобби создано.")
            return

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

            mafia_db.update_room(room_id, status="mafia_count_vote")
            result_message = "Голосование за количество мафии началось."

        elif action == "count":
            mafia_game.vote_for_mafia_count(room_id, user.id, choice)
            result_message = f"Твой голос за {choice} мафиози учтён."

        elif action == "startvote":
            if user.id != room["host_user_id"]:
                await query.answer("Начать голосование может создатель игры.", show_alert=True)
                return
            if room["status"] != "discussion":
                raise mafia_game.MafiaGameError("Сейчас идёт не обсуждение.")
            result_message = "Дневное голосование началось."

        elif action == "endcount":
            if user.id != room["host_user_id"]:
                await query.answer(
                    "Завершить голосование может создатель игры.",
                    show_alert=True,
                )
                return

            players = mafia_db.get_players(room_id)
            ready_count = sum(1 for player in players if player["is_ready"])
            if ready_count != len(players):
                raise mafia_game.MafiaGameError(
                    f"Готовы {ready_count} из {len(players)} игроков. Пусть все нажмут «Я готов»."
                )

            result = mafia_game.resolve_mafia_count_vote(room_id)
            result_message = (
                "Голосование завершено. "
                f"Выбрано мафиози: {result['mafia_count']}."
            )

        else:  # quickstart
            if user.id != room["host_user_id"]:
                await query.answer(
                    "Быстрый старт может запустить только создатель лобби.",
                    show_alert=True,
                )
                return

            if room["status"] != "lobby":
                raise mafia_game.MafiaGameError(
                    "Быстрый старт доступен только до начала голосования."
                )

            players = mafia_db.get_players(room_id)
            player_count = len(players)
            ready_count = sum(1 for player in players if player["is_ready"])
            if player_count < mafia_game.MIN_PLAYERS:
                raise mafia_game.MafiaGameError(
                    f"Нужно минимум {mafia_game.MIN_PLAYERS} игроков."
                )
            if ready_count != player_count:
                raise mafia_game.MafiaGameError(
                    f"Готовы {ready_count} из {player_count} игроков. Пусть все нажмут «Я готов»."
                )
            if player_count >= mafia_game.TWO_MAFIA_MIN_PLAYERS:
                raise mafia_game.MafiaGameError(
                    "Для большой команды используй обычное голосование за количество мафии."
                )

            mafia_db.update_room(
                room_id,
                status="waiting_roles",
                mafia_count=1,
                phase_deadline=None,
            )
            result_message = (
                "⚡ Быстрый старт включён: одна мафия. "
                "Сейчас бот проверит готовность личных чатов."
            )

        await query.answer(result_message)

        if query.message is not None:
            await _refresh_lobby(room_id, context, query.message)

        if action == "startvote":
            await _start_day_vote(room_id, context)
            return

        if action in {"quickstart", "endcount"}:
            # Выдаём роли сразу, если все уже нажали «Получить роль».
            # Иначе бот попросит ещё не готовых игроков открыть личный чат.
            await _deliver_roles(room_id, context)

    except (ValueError, mafia_game.MafiaGameError) as exc:
        await query.answer(str(exc), show_alert=True)


async def handle_mafia_start_payload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Обрабатывает ссылку /start mafia_ID."""
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return True

    if not context.args or not context.args[0].startswith("mafia_"):
        return False

    payload = context.args[0]
    try:
        room_id = int(payload[len("mafia_"):])
    except ValueError:
        await message.reply_text("Некорректная ссылка на игру.")
        return True

    if message.chat.type != "private":
        await message.reply_text(
            "Открой эту ссылку в личном чате с ботом."
        )
        return True

    room = mafia_db.get_room(room_id)
    if room is None:
        await message.reply_text("Игровая комната не найдена.")
        return True

    player = mafia_db.get_player(room_id, user.id)
    if player is None:
        await message.reply_text(
            "Сначала присоединись к игре кнопкой "
            "«✅ Присоединиться» в групповом чате."
        )
        return True

    if room["status"] in {"finished", "cancelled"}:
        await message.reply_text("Эта игра уже завершена.")
        return True

    mafia_db.set_dm_ready(room_id, user.id)
    await _refresh_room_post(room_id, context)
    room = mafia_db.get_room(room_id)
    player = mafia_db.get_player(room_id, user.id)

    if room is None or player is None:
        await message.reply_text(
            "Не удалось найти игрока или игру."
        )
        return True

    if player["role"] is not None and player["role_sent"]:
        await message.reply_text(
            f"🎭 Твоя роль в игре №{room_id}:\n\n"
            f"{mafia_game.ROLE_LABELS[player['role']]}"
        )
        return True

    if room["status"] != "waiting_roles":
        await message.reply_text(
            "✅ Личный чат подключён!\n\n"
            "Дождись, пока создатель выберет обычное голосование "
            "или нажмёт «⚡ Быстрый старт». Роли выдадутся, когда "
            "все игроки будут готовы."
        )
        return True

    await _deliver_roles(room_id, context, reply_message=message)
    return True


def register_mafia_handlers(app: Application) -> None:
    """Подготавливает базу и регистрирует команды игры."""
    global _application
    _application = app
    mafia_db.init_db()

    app.add_handler(CommandHandler("mafia", mafia_command))
    app.add_handler(CommandHandler("mafiastats", mafia_stats_command))
    app.add_handler(
        CallbackQueryHandler(
            handle_mafia_callback,
            pattern=r"^mafia:",
        )
    )

    # Restore pending timers after a bot restart.
    for room in mafia_db.get_active_rooms():
        deadline = room.get("phase_deadline")
        remaining = 1.0
        if deadline:
            remaining = max(
                0.1,
                (datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds(),
            )
        _schedule_phase(int(room["id"]), remaining)
        if room["status"] == "discussion":
            _schedule_discussion_countdown(int(room["id"]), int(remaining))
