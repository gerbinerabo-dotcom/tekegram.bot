import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import DATA_DIR


# Отдельная база только для игры «Мафия».
MAFIA_DATABASE_PATH = DATA_DIR / "mafia.db"


def _now() -> str:
    """Возвращает текущее время в UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """Создаёт подключение к игровой базе."""
    connection = sqlite3.connect(
        str(MAFIA_DATABASE_PATH),
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Открывает базу и гарантирует сохранение или откат изменений."""
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Преобразует запись SQLite в словарь."""
    return dict(row) if row is not None else None


def init_db() -> None:
    """Создаёт таблицы игровой базы, если их ещё нет."""
    schema = """
    CREATE TABLE IF NOT EXISTS mafia_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER,
        host_user_id INTEGER NOT NULL,

        status TEXT NOT NULL DEFAULT 'lobby'
            CHECK (status IN (
                'lobby',
                'mafia_count_vote',
                'waiting_roles',
                'discussion',
                'day_vote',
                'night',
                'finished',
                'cancelled'
            )),

        mafia_count INTEGER NOT NULL DEFAULT 1
            CHECK (mafia_count IN (1, 2)),

        day_number INTEGER NOT NULL DEFAULT 0,
        phase_deadline TEXT,
        winner TEXT,
        phase_preset TEXT NOT NULL DEFAULT 'normal'
            CHECK (phase_preset IN ('quick', 'normal')),
        stats_recorded INTEGER NOT NULL DEFAULT 0
            CHECK (stats_recorded IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_mafia_rooms_chat_status
        ON mafia_rooms(chat_id, status);

    CREATE TABLE IF NOT EXISTS mafia_players (
        room_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT NOT NULL,

        role TEXT CHECK (
            role IS NULL OR
            role IN ('mafia', 'don', 'doctor', 'detective', 'sheriff', 'maniac', 'civilian')
        ),

        is_alive INTEGER NOT NULL DEFAULT 1
            CHECK (is_alive IN (0, 1)),

        dm_ready INTEGER NOT NULL DEFAULT 0
            CHECK (dm_ready IN (0, 1)),

        role_sent INTEGER NOT NULL DEFAULT 0
            CHECK (role_sent IN (0, 1)),

        is_ready INTEGER NOT NULL DEFAULT 0
            CHECK (is_ready IN (0, 1)),

        joined_at TEXT NOT NULL,

        PRIMARY KEY (room_id, user_id),

        FOREIGN KEY (room_id)
            REFERENCES mafia_rooms(id)
            ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_mafia_players_user
        ON mafia_players(user_id);

    CREATE TABLE IF NOT EXISTS mafia_count_votes (
        room_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        choice INTEGER NOT NULL CHECK (choice IN (1, 2)),
        updated_at TEXT NOT NULL,

        PRIMARY KEY (room_id, user_id),

        FOREIGN KEY (room_id)
            REFERENCES mafia_rooms(id)
            ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS mafia_actions (
        room_id INTEGER NOT NULL,
        day_number INTEGER NOT NULL,
        phase TEXT NOT NULL CHECK (
            phase IN ('day_vote', 'mafia', 'doctor', 'detective', 'sheriff', 'maniac')
        ),
        voter_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL,

        PRIMARY KEY (room_id, day_number, phase, voter_id),

        FOREIGN KEY (room_id)
            REFERENCES mafia_rooms(id)
            ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_mafia_actions_room_phase
        ON mafia_actions(room_id, day_number, phase);

    CREATE TABLE IF NOT EXISTS mafia_stats (
        user_id INTEGER PRIMARY KEY,
        games INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        mafia_roles INTEGER NOT NULL DEFAULT 0,
        mafia_role_games INTEGER NOT NULL DEFAULT 0,
        mafia_role_wins INTEGER NOT NULL DEFAULT 0,
        don_roles INTEGER NOT NULL DEFAULT 0,
        don_role_games INTEGER NOT NULL DEFAULT 0,
        don_role_wins INTEGER NOT NULL DEFAULT 0,
        doctor_roles INTEGER NOT NULL DEFAULT 0,
        doctor_role_games INTEGER NOT NULL DEFAULT 0,
        doctor_role_wins INTEGER NOT NULL DEFAULT 0,
        detective_roles INTEGER NOT NULL DEFAULT 0,
        detective_role_games INTEGER NOT NULL DEFAULT 0,
        detective_role_wins INTEGER NOT NULL DEFAULT 0,
        sheriff_roles INTEGER NOT NULL DEFAULT 0,
        sheriff_role_games INTEGER NOT NULL DEFAULT 0,
        sheriff_role_wins INTEGER NOT NULL DEFAULT 0,
        maniac_roles INTEGER NOT NULL DEFAULT 0,
        maniac_role_games INTEGER NOT NULL DEFAULT 0,
        maniac_role_wins INTEGER NOT NULL DEFAULT 0,
        civilian_roles INTEGER NOT NULL DEFAULT 0,
        civilian_role_games INTEGER NOT NULL DEFAULT 0,
        civilian_role_wins INTEGER NOT NULL DEFAULT 0
    );
    """

    with _db() as connection:
        connection.executescript(schema)

    _migrate_game_schema()
    _migrate_role_win_statistics()


def _migrate_role_win_statistics() -> None:
    """Adds per-role win counters without replacing existing statistics."""
    role_win_columns = (
        "mafia_role_games", "mafia_role_wins", "don_role_games", "don_role_wins",
        "doctor_role_games", "doctor_role_wins", "detective_role_games",
        "detective_role_wins", "sheriff_role_games", "sheriff_role_wins",
        "maniac_role_games", "maniac_role_wins", "civilian_role_games",
        "civilian_role_wins",
    )
    with _db() as connection:
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(mafia_stats)")
        }
        for column in role_win_columns:
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE mafia_stats ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )


def _migrate_game_schema() -> None:
    """Adds new game options and role types without discarding active games."""
    with _db() as connection:
        room_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(mafia_rooms)")
        }
        if "phase_preset" not in room_columns:
            connection.execute(
                "ALTER TABLE mafia_rooms ADD COLUMN phase_preset TEXT NOT NULL DEFAULT 'normal'"
            )
        if "stats_recorded" not in room_columns:
            connection.execute(
                "ALTER TABLE mafia_rooms ADD COLUMN stats_recorded INTEGER NOT NULL DEFAULT 0"
            )
        player_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(mafia_players)")
        }
        if "is_ready" not in player_columns:
            connection.execute(
                "ALTER TABLE mafia_players ADD COLUMN is_ready INTEGER NOT NULL DEFAULT 0"
            )
        connection.commit()

        player_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mafia_players'"
        ).fetchone()["sql"]
        if "'sheriff'" not in player_sql or "'maniac'" not in player_sql or "'don'" not in player_sql:
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE mafia_players_new (
                    room_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    role TEXT CHECK (role IS NULL OR role IN
                        ('mafia', 'don', 'doctor', 'detective', 'sheriff', 'maniac', 'civilian')),
                    is_alive INTEGER NOT NULL DEFAULT 1 CHECK (is_alive IN (0, 1)),
                    dm_ready INTEGER NOT NULL DEFAULT 0 CHECK (dm_ready IN (0, 1)),
                    role_sent INTEGER NOT NULL DEFAULT 0 CHECK (role_sent IN (0, 1)),
                    joined_at TEXT NOT NULL,
                    is_ready INTEGER NOT NULL DEFAULT 0 CHECK (is_ready IN (0, 1)),
                    PRIMARY KEY (room_id, user_id),
                    FOREIGN KEY (room_id) REFERENCES mafia_rooms(id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                INSERT INTO mafia_players_new (
                    room_id, user_id, username, first_name, role,
                    is_alive, dm_ready, role_sent, joined_at, is_ready
                ) SELECT room_id, user_id, username, first_name, role,
                    is_alive, dm_ready, role_sent, joined_at, is_ready
                  FROM mafia_players
            """)
            connection.execute("DROP TABLE mafia_players")
            connection.execute("ALTER TABLE mafia_players_new RENAME TO mafia_players")
            connection.execute("CREATE INDEX idx_mafia_players_user ON mafia_players(user_id)")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

        action_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mafia_actions'"
        ).fetchone()["sql"]
        if "'maniac'" not in action_sql or "'sheriff'" not in action_sql:
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE mafia_actions_new (
                    room_id INTEGER NOT NULL,
                    day_number INTEGER NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN
                        ('day_vote', 'mafia', 'doctor', 'detective', 'sheriff', 'maniac')),
                    voter_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (room_id, day_number, phase, voter_id),
                    FOREIGN KEY (room_id) REFERENCES mafia_rooms(id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                INSERT INTO mafia_actions_new
                SELECT room_id, day_number, phase, voter_id, target_id, updated_at
                FROM mafia_actions
            """)
            connection.execute("DROP TABLE mafia_actions")
            connection.execute("ALTER TABLE mafia_actions_new RENAME TO mafia_actions")
            connection.execute("CREATE INDEX idx_mafia_actions_room_phase ON mafia_actions(room_id, day_number, phase)")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")


def create_room(chat_id: int, host_user_id: int) -> int:
    """Создаёт отдельную игровую комнату в группе."""
    now = _now()

    with _db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO mafia_rooms (
                chat_id,
                host_user_id,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'lobby', ?, ?)
            """,
            (chat_id, host_user_id, now, now),
        )

        return int(cursor.lastrowid)


def get_room(room_id: int) -> dict[str, Any] | None:
    """Возвращает комнату по её номеру."""
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM mafia_rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        return _dict(row)


def get_active_room_by_chat(
    chat_id: int,
) -> dict[str, Any] | None:
    """Находит активную комнату в конкретном чате."""
    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM mafia_rooms
            WHERE chat_id = ?
              AND status NOT IN ('finished', 'cancelled')
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return _dict(row)


def get_active_rooms() -> list[dict[str, Any]]:
    """Returns rooms that still need game phase processing."""
    with _db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM mafia_rooms
            WHERE status IN ('discussion', 'day_vote', 'night')
            ORDER BY id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_room(room_id: int, **fields: Any) -> None:
    """Обновляет разрешённые поля комнаты."""
    allowed_fields = {
        "message_id",
        "host_user_id",
        "status",
        "mafia_count",
        "day_number",
        "phase_deadline",
        "winner",
        "phase_preset",
    }

    unknown_fields = set(fields) - allowed_fields
    if unknown_fields:
        raise ValueError(
            f"Нельзя менять поля комнаты: {', '.join(sorted(unknown_fields))}"
        )

    if not fields:
        return

    fields["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [room_id]

    with _db() as connection:
        cursor = connection.execute(
            f"UPDATE mafia_rooms SET {assignments} WHERE id = ?",
            values,
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Комната №{room_id} не найдена.")


def add_player(
    room_id: int,
    user_id: int,
    first_name: str,
    username: str | None = None,
) -> None:
    """Добавляет игрока в лобби или обновляет его имя."""
    now = _now()

    with _db() as connection:
        room = connection.execute(
            "SELECT status FROM mafia_rooms WHERE id = ?",
            (room_id,),
        ).fetchone()

        if room is None:
            raise ValueError("Игровая комната не найдена.")

        if room["status"] != "lobby":
            raise ValueError("Присоединиться можно только до начала игры.")

        connection.execute(
            """
            INSERT INTO mafia_players (
                room_id, user_id, username, first_name, joined_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (room_id, user_id, username, first_name, now),
        )


def remove_player(room_id: int, user_id: int) -> bool:
    """Удаляет игрока из комнаты."""
    with _db() as connection:
        cursor = connection.execute(
            """
            DELETE FROM mafia_players
            WHERE room_id = ? AND user_id = ?
            """,
            (room_id, user_id),
        )
        return cursor.rowcount > 0


def get_player(
    room_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    """Возвращает игрока в определённой комнате."""
    with _db() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM mafia_players
            WHERE room_id = ? AND user_id = ?
            """,
            (room_id, user_id),
        ).fetchone()
        return _dict(row)


def get_players(room_id: int) -> list[dict[str, Any]]:
    """Возвращает игроков комнаты в порядке присоединения."""
    with _db() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM mafia_players
            WHERE room_id = ?
            ORDER BY joined_at, user_id
            """,
            (room_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def set_dm_ready(room_id: int, user_id: int) -> bool:
    """Отмечает, что игрок открыл личный чат с ботом и нажал Start."""
    with _db() as connection:
        cursor = connection.execute(
            """
            UPDATE mafia_players
            SET dm_ready = 1
            WHERE room_id = ? AND user_id = ?
            """,
            (room_id, user_id),
        )
        return cursor.rowcount > 0


def set_player_ready(room_id: int, user_id: int, ready: bool = True) -> bool:
    with _db() as connection:
        cursor = connection.execute(
            "UPDATE mafia_players SET is_ready = ? WHERE room_id = ? AND user_id = ?",
            (int(ready), room_id, user_id),
        )
        return cursor.rowcount > 0


def get_player_statistics(user_id: int) -> dict[str, int]:
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM mafia_stats WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return {"games": 0, "wins": 0, "mafia_roles": 0, "don_roles": 0,
                    "doctor_roles": 0, "detective_roles": 0, "sheriff_roles": 0,
                    "maniac_roles": 0, "civilian_roles": 0,
                    "mafia_role_wins": 0, "don_role_wins": 0,
                    "doctor_role_wins": 0, "detective_role_wins": 0,
                    "sheriff_role_wins": 0, "maniac_role_wins": 0,
                    "civilian_role_wins": 0,
                    "mafia_role_games": 0, "don_role_games": 0,
                    "doctor_role_games": 0, "detective_role_games": 0,
                    "sheriff_role_games": 0, "maniac_role_games": 0,
                    "civilian_role_games": 0}
        return dict(row)


def finish_room(room_id: int, winner: str) -> bool:
    """Finalizes a game and records player statistics once."""
    role_columns = {
        "mafia": ("mafia_roles", "mafia_role_games", "mafia_role_wins"),
        "don": ("don_roles", "don_role_games", "don_role_wins"),
        "doctor": ("doctor_roles", "doctor_role_games", "doctor_role_wins"),
        "detective": ("detective_roles", "detective_role_games", "detective_role_wins"),
        "sheriff": ("sheriff_roles", "sheriff_role_games", "sheriff_role_wins"),
        "maniac": ("maniac_roles", "maniac_role_games", "maniac_role_wins"),
        "civilian": ("civilian_roles", "civilian_role_games", "civilian_role_wins"),
    }
    with _db() as connection:
        room = connection.execute(
            "SELECT stats_recorded FROM mafia_rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if room is None or room["stats_recorded"]:
            return False
        players = connection.execute(
            "SELECT user_id, role FROM mafia_players WHERE room_id = ?", (room_id,)
        ).fetchall()
        for player in players:
            role_columns_for_player = role_columns.get(player["role"])
            if role_columns_for_player is None:
                continue
            role_column, role_games_column, role_win_column = role_columns_for_player
            is_win = (
                winner == "town" and player["role"] not in {"mafia", "don", "maniac"}
                or winner == "mafia" and player["role"] in {"mafia", "don"}
                or winner == "maniac" and player["role"] == "maniac"
            )
            connection.execute(
                f"""INSERT INTO mafia_stats (
                        user_id, games, wins, {role_column},
                        {role_games_column}, {role_win_column}
                    ) VALUES (?, 1, ?, 1, 1, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        games = games + 1, wins = wins + excluded.wins,
                        {role_column} = {role_column} + 1,
                        {role_games_column} = {role_games_column} + 1,
                        {role_win_column} = {role_win_column} + excluded.{role_win_column}""",
                (player["user_id"], int(is_win), int(is_win)),
            )
        connection.execute(
            "UPDATE mafia_rooms SET status = 'finished', winner = ?, phase_deadline = NULL, stats_recorded = 1, updated_at = ? WHERE id = ?",
            (winner, _now(), room_id),
        )
        return True


def set_player_role(
    room_id: int,
    user_id: int,
    role: str,
) -> None:
    """Назначает роль игроку."""
    if role not in {"mafia", "don", "doctor", "detective", "sheriff", "maniac", "civilian"}:
        raise ValueError("Неизвестная роль.")

    with _db() as connection:
        cursor = connection.execute(
            """
            UPDATE mafia_players
            SET role = ?, role_sent = 0
            WHERE room_id = ? AND user_id = ?
            """,
            (role, room_id, user_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("Игрок не найден в этой комнате.")


def mark_role_sent(room_id: int, user_id: int) -> None:
    """Отмечает, что личное сообщение с ролью отправлено."""
    with _db() as connection:
        connection.execute(
            """
            UPDATE mafia_players
            SET role_sent = 1
            WHERE room_id = ? AND user_id = ?
            """,
            (room_id, user_id),
        )


def set_player_alive(
    room_id: int,
    user_id: int,
    is_alive: bool,
) -> None:
    """Меняет статус жизни игрока."""
    with _db() as connection:
        connection.execute(
            """
            UPDATE mafia_players
            SET is_alive = ?
            WHERE room_id = ? AND user_id = ?
            """,
            (int(is_alive), room_id, user_id),
        )


def set_mafia_count_vote(
    room_id: int,
    user_id: int,
    choice: int,
) -> None:
    """Сохраняет или меняет голос за одного или двух мафиози."""
    if choice not in (1, 2):
        raise ValueError("Можно выбрать только 1 или 2 мафиози.")

    now = _now()

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO mafia_count_votes (
                room_id, user_id, choice, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                choice = excluded.choice,
                updated_at = excluded.updated_at
            """,
            (room_id, user_id, choice, now),
        )


def get_mafia_count_votes(room_id: int) -> dict[int, int]:
    """Возвращает подсчёт голосов за количество мафиози."""
    with _db() as connection:
        rows = connection.execute(
            """
            SELECT choice, COUNT(*) AS total
            FROM mafia_count_votes
            WHERE room_id = ?
            GROUP BY choice
            """,
            (room_id,),
        ).fetchall()

        result = {1: 0, 2: 0}
        for row in rows:
            result[int(row["choice"])] = int(row["total"])
        return result


def set_action_vote(
    room_id: int,
    day_number: int,
    phase: str,
    voter_id: int,
    target_id: int,
) -> None:
    """Сохраняет или меняет один голос/ночное действие игрока."""
    allowed_phases = {"day_vote", "mafia", "doctor", "detective", "sheriff", "maniac"}
    if phase not in allowed_phases:
        raise ValueError("Неизвестная игровая фаза.")

    now = _now()

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO mafia_actions (
                room_id, day_number, phase, voter_id, target_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(room_id, day_number, phase, voter_id)
            DO UPDATE SET
                target_id = excluded.target_id,
                updated_at = excluded.updated_at
            """,
            (room_id, day_number, phase, voter_id, target_id, now),
        )


def get_action_votes(
    room_id: int,
    day_number: int,
    phase: str,
) -> list[dict[str, Any]]:
    """Возвращает голоса и действия для определённой фазы."""
    with _db() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM mafia_actions
            WHERE room_id = ? AND day_number = ? AND phase = ?
            """,
            (room_id, day_number, phase),
        ).fetchall()
        return [dict(row) for row in rows]


def clear_action_votes(
    room_id: int,
    day_number: int,
    phase: str,
) -> None:
    """Удаляет голоса только для выбранной фазы и игрового дня."""
    with _db() as connection:
        connection.execute(
            """
            DELETE FROM mafia_actions
            WHERE room_id = ? AND day_number = ? AND phase = ?
            """,
            (room_id, day_number, phase),
        )
