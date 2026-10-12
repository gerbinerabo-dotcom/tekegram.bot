import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import mafia_db
from config import MAFIA_TEST_MODE


MIN_PLAYERS = 2 if MAFIA_TEST_MODE else 3
TWO_MAFIA_MIN_PLAYERS = 7

DISCUSSION_SECONDS = 120
DAY_VOTE_SECONDS = 30
NIGHT_SECONDS = 60

ROLE_LABELS = {
    "mafia": "🔫 Мафия",
    "don": "🕴 Дон мафии",
    "doctor": "🩺 Доктор",
    "detective": "🕵️ Детектив",
    "sheriff": "⭐ Шериф",
    "maniac": "🔪 Маньяк",
    "civilian": "👤 Мирный житель",
}


class MafiaGameError(ValueError):
    """Ошибка игрового правила."""


def _deadline(seconds: int) -> str:
    """Вычисляет срок окончания фазы в UTC."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return deadline.isoformat(timespec="seconds")


def _get_room(room_id: int) -> dict[str, Any]:
    room = mafia_db.get_room(room_id)
    if room is None:
        raise MafiaGameError("Игровая комната не найдена.")
    return room


def _get_player(
    room_id: int,
    user_id: int,
) -> dict[str, Any]:
    player = mafia_db.get_player(room_id, user_id)
    if player is None:
        raise MafiaGameError("Ты не участвуешь в этой игре.")
    return player


def _get_alive_player(
    room_id: int,
    user_id: int,
) -> dict[str, Any]:
    player = _get_player(room_id, user_id)

    if not player["is_alive"]:
        raise MafiaGameError("Ты выбыл из игры и не можешь действовать.")

    return player


def _get_winner(room_id: int) -> str | None:
    """Проверяет условия победы мафии и мирных."""
    players = mafia_db.get_players(room_id)
    alive_players = [p for p in players if p["is_alive"]]

    mafia_count = sum(1 for p in alive_players if p["role"] in {"mafia", "don"})
    maniac_count = sum(1 for p in alive_players if p["role"] == "maniac")
    town_count = len(alive_players) - mafia_count - maniac_count

    if mafia_count == 0 and maniac_count == 0:
        return "town"

    if maniac_count == 1 and mafia_count == 0 and town_count == 0:
        return "maniac"

    if mafia_count > 0 and mafia_count >= town_count + maniac_count:
        return "mafia"

    return None


def _finish_if_won(room_id: int) -> str | None:
    """Завершает игру, если одна из сторон победила."""
    winner = _get_winner(room_id)

    if winner is not None:
        mafia_db.finish_room(room_id, winner)

    return winner


def join_lobby(
    room_id: int,
    user_id: int,
    first_name: str,
    username: str | None = None,
) -> None:
    """Добавляет игрока в лобби."""
    room = _get_room(room_id)

    if room["status"] != "lobby":
        raise MafiaGameError("Регистрация в этой комнате уже закрыта.")

    mafia_db.add_player(
        room_id=room_id,
        user_id=user_id,
        first_name=first_name,
        username=username,
    )


def leave_lobby(room_id: int, user_id: int) -> None:
    """Позволяет игроку выйти до начала игры."""
    room = _get_room(room_id)

    if room["status"] != "lobby":
        raise MafiaGameError("Во время игры покинуть лобби нельзя.")

    if not mafia_db.remove_player(room_id, user_id):
        raise MafiaGameError("Ты не находишься в этом лобби.")


def vote_for_mafia_count(
    room_id: int,
    user_id: int,
    choice: int,
) -> None:
    """Принимает голос за одного или двух мафиози."""
    room = _get_room(room_id)

    if room["status"] != "mafia_count_vote":
        raise MafiaGameError("Сейчас нельзя голосовать за число мафиози.")

    player = _get_alive_player(room_id, user_id)

    if not player["is_alive"]:
        raise MafiaGameError("Выбывшие игроки не могут голосовать.")

    if choice not in (1, 2):
        raise MafiaGameError("Выбери 1 или 2 мафиози.")

    mafia_db.set_mafia_count_vote(room_id, user_id, choice)


def resolve_mafia_count_vote(room_id: int) -> dict[str, Any]:
    """Подводит итог голосования за количество мафиози."""
    room = _get_room(room_id)

    if room["status"] != "mafia_count_vote":
        raise MafiaGameError("Голосование за количество мафии не идёт.")

    players = mafia_db.get_players(room_id)

    if len(players) < MIN_PLAYERS:
        raise MafiaGameError(
            f"Для начала игры нужно минимум {MIN_PLAYERS} игроков."
        )

    votes = mafia_db.get_mafia_count_votes(room_id)
    eligible_for_two = len(players) >= TWO_MAFIA_MIN_PLAYERS

    # При равенстве голосов выбирается один мафиози.
    chosen = 2 if votes[2] > votes[1] and eligible_for_two else 1

    mafia_db.update_room(
        room_id,
        mafia_count=chosen,
        status="waiting_roles",
        phase_deadline=None,
    )

    return {
        "mafia_count": chosen,
        "votes": votes,
        "player_count": len(players),
    }


def assign_roles(room_id: int) -> dict[int, str]:
    """
    Назначает роли один раз.

    Все игроки обязаны заранее открыть личный чат с ботом.
    Повторный вызов не перемешивает уже назначенные роли.
    """
    # Используем транзакцию, чтобы не сохранить половину ролей
    # при сбое или одновременном запуске двух обработчиков.
    with mafia_db._db() as connection:
        connection.execute("BEGIN IMMEDIATE")

        room_row = connection.execute(
            "SELECT * FROM mafia_rooms WHERE id = ?",
            (room_id,),
        ).fetchone()

        if room_row is None:
            raise MafiaGameError("Игровая комната не найдена.")

        room = dict(room_row)

        if room["status"] != "waiting_roles":
            raise MafiaGameError(
                "Комната ещё не готова к распределению ролей."
            )

        player_rows = connection.execute(
            """
            SELECT *
            FROM mafia_players
            WHERE room_id = ?
            ORDER BY joined_at, user_id
            """,
            (room_id,),
        ).fetchall()

        players = [dict(p) for p in player_rows]

        if len(players) < MIN_PLAYERS:
            raise MafiaGameError(
                f"Нужно минимум {MIN_PLAYERS} игроков."
            )

        # Если роли уже назначены полностью, повторно не меняем их.
        assigned = {
            int(p["user_id"]): p["role"]
            for p in players
            if p["role"] is not None
        }

        if len(assigned) == len(players):
            return assigned

        if assigned:
            raise MafiaGameError(
                "Обнаружено неполное распределение ролей. "
                "Нельзя случайно переназначить оставшиеся роли."
            )

        not_ready = [
            p for p in players if not p["dm_ready"]
        ]

        if not_ready:
            raise MafiaGameError(
                "Не все игроки открыли личный чат с ботом. "
                "Попросите каждого нажать «Получить роль» и Start."
            )

        # В тестовом режиме для двух игроков
        # используем только две роли.
        if MAFIA_TEST_MODE and len(players) == 2:
            mafia_count = 1
            roles = ["mafia", "civilian"]
        elif len(players) == 3:
            mafia_count = 1
            roles = ["mafia", "sheriff", "civilian"]
        elif len(players) == 4:
            mafia_count = 1
            roles = ["mafia", "sheriff", "doctor", "civilian"]
        elif len(players) >= 9:
            mafia_count = int(room["mafia_count"])
            mafia_count = 2 if mafia_count == 2 else 1
            roles = (
                ["don", "mafia"] if mafia_count == 2 else ["mafia"]
            ) + ["doctor", "sheriff", "maniac"]
            roles += ["civilian"] * (len(players) - len(roles))
        else:
            mafia_count = int(room["mafia_count"])

            if (
                mafia_count == 2
                and len(players) < TWO_MAFIA_MIN_PLAYERS
            ):
                mafia_count = 1

                connection.execute(
                    """
                    UPDATE mafia_rooms
                    SET mafia_count = 1
                    WHERE id = ?
                    """,
                    (room_id,),
                )

            if mafia_count not in (1, 2):
                raise MafiaGameError(
                    "Некорректное число мафиози."
                )

            mafia_roles = ["don", "mafia"] if mafia_count == 2 else ["mafia"]
            special_roles = ["doctor", "sheriff"]
            roles = mafia_roles + special_roles
            roles += ["civilian"] * (len(players) - len(roles))

        # Role order must not reveal the first player as the mafia.
        random.SystemRandom().shuffle(roles)

        result: dict[int, str] = {}

        for player, role in zip(players, roles):
            user_id = int(player["user_id"])

            connection.execute(
                """
                UPDATE mafia_players
                SET role = ?, role_sent = 0
                WHERE room_id = ? AND user_id = ? AND role IS NULL
                """,
                (role, room_id, user_id),
            )

            result[user_id] = role

        return result


def begin_game_after_roles_sent(room_id: int, discussion_seconds: int | None = None) -> None:
    """Начинает первый день только после отправки всех ролей."""
    room = _get_room(room_id)

    if room["status"] != "waiting_roles":
        raise MafiaGameError("Комната сейчас не ожидает выдачи ролей.")

    players = mafia_db.get_players(room_id)

    if len(players) < MIN_PLAYERS:
        raise MafiaGameError("Недостаточно игроков.")

    if not all(
        p["role"] is not None
        and p["dm_ready"]
        and p["role_sent"]
        for p in players
    ):
        raise MafiaGameError(
            "Сначала необходимо отправить роли всем игрокам."
        )

    mafia_db.update_room(
        room_id,
        status="discussion",
        day_number=1,
        phase_deadline=_deadline(discussion_seconds or DISCUSSION_SECONDS),
    )


def start_day_vote(room_id: int, vote_seconds: int | None = None) -> None:
    """Moves the game from discussion into the timed vote phase."""
    room = _get_room(room_id)
    if room["status"] != "discussion":
        raise MafiaGameError("Сейчас нельзя начать дневное голосование.")
    mafia_db.update_room(
        room_id,
        status="day_vote",
        phase_deadline=_deadline(vote_seconds or DAY_VOTE_SECONDS),
    )


def start_night(room_id: int, night_seconds: int | None = None) -> None:
    """Starts the night after a tie or a day with no votes."""
    room = _get_room(room_id)
    if room["status"] != "day_vote":
        raise MafiaGameError("Дневное голосование уже завершено.")
    mafia_db.update_room(
        room_id,
        status="night",
        phase_deadline=_deadline(night_seconds or NIGHT_SECONDS),
    )


def cast_day_vote(
    room_id: int,
    user_id: int,
    target_id: int,
    allowed_targets: set[int] | None = None,
) -> None:
    """Принимает дневной голос."""
    room = _get_room(room_id)

    if room["status"] != "day_vote":
        raise MafiaGameError("Сейчас дневное голосование не идёт.")

    voter = _get_alive_player(room_id, user_id)
    target = _get_alive_player(room_id, target_id)

    if user_id == target_id:
        raise MafiaGameError("Нельзя голосовать против самого себя.")

    if allowed_targets is not None and target_id not in allowed_targets:
        raise MafiaGameError("В повторном голосовании выбери кандидата из списка.")

    if target["role"] is None or voter["role"] is None:
        raise MafiaGameError("Роли ещё не назначены.")

    mafia_db.set_action_vote(
        room_id=room_id,
        day_number=int(room["day_number"]),
        phase="day_vote",
        voter_id=user_id,
        target_id=target_id,
    )


def cast_night_action(
    room_id: int,
    user_id: int,
    target_id: int,
    phase: str,
) -> str | None:
    """
    Принимает ночное действие.

    Для детектива возвращает роль проверяемого игрока.
    Результат проверки обработчик должен отправлять только в личку.
    """
    room = _get_room(room_id)

    if room["status"] != "night":
        raise MafiaGameError("Сейчас ночная фаза не идёт.")

    if phase not in {"mafia", "doctor", "detective", "sheriff", "maniac"}:
        raise MafiaGameError("Неизвестное ночное действие.")

    actor = _get_alive_player(room_id, user_id)
    target = _get_alive_player(room_id, target_id)

    required_role = {
        "mafia": "mafia",
        "doctor": "doctor",
        "detective": "detective",
        "sheriff": "sheriff",
        "maniac": "maniac",
    }[phase]

    if actor["role"] != required_role and not (
        phase == "mafia" and actor["role"] == "don"
    ):
        raise MafiaGameError("Твоя роль не может выполнять это действие.")

    if target["role"] is None:
        raise MafiaGameError("Роль цели ещё не назначена.")

    if phase == "mafia" and target["role"] in {"mafia", "don"}:
        raise MafiaGameError("Мафия не может выбрать другого мафиози.")

    if phase in {"detective", "sheriff"} and user_id == target_id:
        raise MafiaGameError("Нельзя проверить самого себя.")

    mafia_db.set_action_vote(
        room_id=room_id,
        day_number=int(room["day_number"]),
        phase=phase,
        voter_id=user_id,
        target_id=target_id,
    )

    if phase in {"detective", "sheriff"}:
        if phase == "sheriff" and target["role"] == "don":
            return "civilian"
        return str(target["role"])

    return None


def resolve_day_vote(room_id: int, night_seconds: int | None = None) -> dict[str, Any]:
    """
    Подсчитывает дневные голоса и при необходимости исключает игрока.

    При равенстве голосов никого не исключает: обработчик может
    предложить повторное голосование между лидерами.
    """
    room = _get_room(room_id)

    if room["status"] != "day_vote":
        raise MafiaGameError("Дневное голосование уже завершено.")

    players = mafia_db.get_players(room_id)
    player_by_id = {int(p["user_id"]): p for p in players}

    votes = mafia_db.get_action_votes(
        room_id,
        int(room["day_number"]),
        "day_vote",
    )

    tally: Counter[int] = Counter()

    for vote in votes:
        voter_id = int(vote["voter_id"])
        target_id = int(vote["target_id"])

        voter = player_by_id.get(voter_id)
        target = player_by_id.get(target_id)

        if not voter or not target:
            continue

        if not voter["is_alive"] or not target["is_alive"]:
            continue

        if voter_id == target_id:
            continue

        tally[target_id] += 1

    if not tally:
        return {
            "status": "no_votes",
            "eliminated": None,
            "tied": [],
            "counts": {},
            "winner": None,
        }

    highest = max(tally.values())
    leaders = sorted(
        user_id for user_id, total in tally.items()
        if total == highest
    )

    counts = dict(tally)

    if len(leaders) > 1:
        return {
            "status": "tie",
            "eliminated": None,
            "tied": leaders,
            "counts": counts,
            "winner": None,
        }

    eliminated_id = leaders[0]
    mafia_db.set_player_alive(room_id, eliminated_id, False)

    winner = _finish_if_won(room_id)

    if winner is None:
        mafia_db.update_room(
            room_id,
            status="night",
            phase_deadline=_deadline(night_seconds or NIGHT_SECONDS),
        )

    eliminated = player_by_id[eliminated_id]

    return {
        "status": "eliminated",
        "eliminated": eliminated_id,
        "eliminated_role": eliminated["role"],
        "tied": [],
        "counts": counts,
        "winner": winner,
    }


def resolve_night(room_id: int, discussion_seconds: int | None = None) -> dict[str, Any]:
    """
    Завершает ночь: обрабатывает выбор мафии и защиту доктора.
    Если партия продолжается, переводит комнату к новому дню.
    """
    room = _get_room(room_id)

    if room["status"] != "night":
        raise MafiaGameError("Сейчас нельзя завершить ночь.")

    day_number = int(room["day_number"])
    players = mafia_db.get_players(room_id)
    player_by_id = {int(p["user_id"]): p for p in players}

    mafia_votes = mafia_db.get_action_votes(
        room_id, day_number, "mafia"
    )
    doctor_votes = mafia_db.get_action_votes(
        room_id, day_number, "doctor"
    )
    maniac_votes = mafia_db.get_action_votes(room_id, day_number, "maniac")

    tally: Counter[int] = Counter()

    for vote in mafia_votes:
        voter = player_by_id.get(int(vote["voter_id"]))
        target_id = int(vote["target_id"])
        target = player_by_id.get(target_id)

        if not voter or not target:
            continue

        if not voter["is_alive"] or voter["role"] not in {"mafia", "don"}:
            continue

        if not target["is_alive"] or target["role"] in {"mafia", "don"}:
            continue

        tally[target_id] += 1

    victim_id: int | None = None

    if tally:
        highest = max(tally.values())
        leaders = [
            target_id for target_id, total in tally.items()
            if total == highest
        ]

        # Если голоса мафии разделились поровну, жертвы нет.
        if len(leaders) == 1:
            victim_id = leaders[0]

    protected_id: int | None = None

    for vote in doctor_votes:
        doctor = player_by_id.get(int(vote["voter_id"]))
        target_id = int(vote["target_id"])
        target = player_by_id.get(target_id)

        if (
            doctor
            and target
            and doctor["is_alive"]
            and doctor["role"] == "doctor"
            and target["is_alive"]
        ):
            protected_id = target_id

    maniac_tally: Counter[int] = Counter()
    for vote in maniac_votes:
        actor = player_by_id.get(int(vote["voter_id"]))
        target_id = int(vote["target_id"])
        target = player_by_id.get(target_id)
        if actor and target and actor["is_alive"] and actor["role"] == "maniac" and target["is_alive"]:
            maniac_tally[target_id] += 1
    maniac_victim = None
    if maniac_tally:
        highest = max(maniac_tally.values())
        leaders = [target_id for target_id, total in maniac_tally.items() if total == highest]
        if len(leaders) == 1:
            maniac_victim = leaders[0]

    killed_ids: set[int] = set()

    if victim_id is not None and victim_id != protected_id:
        mafia_db.set_player_alive(room_id, victim_id, False)
        killed_ids.add(victim_id)
    if maniac_victim is not None and maniac_victim != protected_id and maniac_victim not in killed_ids:
        mafia_db.set_player_alive(room_id, maniac_victim, False)
        killed_ids.add(maniac_victim)

    winner = _finish_if_won(room_id)

    if winner is None:
        mafia_db.update_room(
            room_id,
            status="discussion",
            day_number=day_number + 1,
            phase_deadline=_deadline(discussion_seconds or DISCUSSION_SECONDS),
        )

    return {
        "killed": sorted(killed_ids),
        "protected": protected_id,
        "winner": winner,
        "next_phase": None if winner else "discussion",
    }


def get_winner(room_id: int) -> str | None:
    """Возвращает победителя, если партия уже завершена по правилам."""
    return _get_winner(room_id)
