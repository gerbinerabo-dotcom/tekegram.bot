
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATABASE_PATH


class _ClosingConnection(sqlite3.Connection):
    """Commits or rolls back on context exit, then always closes the handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _now() -> str:
    """Возвращает текущее время UTC в ISO-формате."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """Создаёт подключение к базе данных."""
    connection = sqlite3.connect(
        str(DATABASE_PATH),
        timeout=10,
        factory=_ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Преобразует строку SQLite в обычный словарь."""
    return dict(row) if row is not None else None


def init_db() -> None:
    """Создаёт все необходимые таблицы и индексы."""
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL UNIQUE,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        is_blocked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'closed')),
        unread_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_message_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        sender TEXT NOT NULL
            CHECK (sender IN ('user', 'admin')),
        content_type TEXT NOT NULL DEFAULT 'text',
        text TEXT,
        caption TEXT,
        file_id TEXT,
        telegram_message_id INTEGER,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_type TEXT NOT NULL DEFAULT 'text',
        text TEXT,
        caption TEXT,
        file_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        total_count INTEGER NOT NULL DEFAULT 0,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS finance_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
        category TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        note TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS finance_drafts (
        telegram_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
        category TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        product_id TEXT NOT NULL,
        amount_stars INTEGER NOT NULL CHECK (amount_stars > 0),
        currency TEXT NOT NULL,
        invoice_payload TEXT NOT NULL UNIQUE,
        telegram_payment_charge_id TEXT UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'paid', 'refunded', 'rejected')),
        created_at TEXT NOT NULL,
        purchased_at TEXT,
        refunded_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_purchases_user_status
        ON purchases(telegram_id, status, product_id);

    CREATE INDEX IF NOT EXISTS idx_users_username
        ON users(username);

    CREATE INDEX IF NOT EXISTS idx_users_telegram_id
        ON users(telegram_id);

    CREATE INDEX IF NOT EXISTS idx_conversations_updated
        ON conversations(updated_at);

    CREATE INDEX IF NOT EXISTS idx_conversations_unread
        ON conversations(unread_count);

    CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages(conversation_id, id);

    CREATE INDEX IF NOT EXISTS idx_messages_created
        ON messages(created_at);

    CREATE INDEX IF NOT EXISTS idx_messages_unread
        ON messages(conversation_id, is_read, sender);

    CREATE INDEX IF NOT EXISTS idx_finance_user_date
        ON finance_transactions(telegram_id, created_at);
    """

    with _connect() as connection:
        connection.executescript(schema)


def create_purchase_intent(
    telegram_id: int,
    product_id: str,
    amount_stars: int,
    currency: str,
    invoice_payload: str,
) -> int:
    """Stores a pending invoice; this does not grant access."""
    with _connect() as connection:
        cursor = connection.execute(
            """INSERT INTO purchases (
                telegram_id, product_id, amount_stars, currency,
                invoice_payload, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (telegram_id, product_id, amount_stars, currency, invoice_payload, _now()),
        )
        return int(cursor.lastrowid)


def get_purchase_by_payload(invoice_payload: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM purchases WHERE invoice_payload = ?",
            (invoice_payload,),
        ).fetchone()
        return _dict(row)


def reject_pending_purchase(invoice_payload: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE purchases SET status = 'rejected' WHERE invoice_payload = ? AND status = 'pending'",
            (invoice_payload,),
        )
        return cursor.rowcount == 1


def record_successful_star_payment(
    telegram_id: int,
    product_id: str,
    amount_stars: int,
    currency: str,
    invoice_payload: str,
    charge_id: str,
) -> str:
    """Validates and records a payment exactly once; returns a result code."""
    with _connect() as connection:
        purchase = connection.execute(
            "SELECT * FROM purchases WHERE invoice_payload = ?",
            (invoice_payload,),
        ).fetchone()
        if purchase is None:
            if connection.execute(
                "SELECT 1 FROM purchases WHERE telegram_payment_charge_id = ?",
                (charge_id,),
            ).fetchone():
                return "duplicate_charge"
            connection.execute(
                """INSERT INTO purchases (
                    telegram_id, product_id, amount_stars, currency,
                    invoice_payload, telegram_payment_charge_id,
                    status, created_at, purchased_at
                ) VALUES (?, 'unknown', ?, ?, ?, ?, 'rejected', ?, ?)""",
                (telegram_id, amount_stars, currency, invoice_payload, charge_id, _now(), _now()),
            )
            return "unknown_payload"
        if purchase["telegram_payment_charge_id"] == charge_id:
            return "duplicate"
        if purchase["status"] != "pending":
            return "already_processed"
        if connection.execute(
            "SELECT 1 FROM purchases WHERE telegram_payment_charge_id = ?",
            (charge_id,),
        ).fetchone():
            return "duplicate_charge"
        if (
            int(purchase["telegram_id"]) != telegram_id
            or purchase["product_id"] != product_id
            or int(purchase["amount_stars"]) != amount_stars
            or purchase["currency"] != currency
        ):
            connection.execute(
                """UPDATE purchases SET status = 'rejected',
                   telegram_payment_charge_id = ?, purchased_at = ? WHERE id = ?""",
                (charge_id, _now(), purchase["id"]),
            )
            return "mismatch"
        cursor = connection.execute(
            """UPDATE purchases
               SET status = 'paid', telegram_payment_charge_id = ?, purchased_at = ?
               WHERE id = ? AND status = 'pending'""",
            (charge_id, _now(), purchase["id"]),
        )
        return "paid" if cursor.rowcount == 1 else "already_processed"


def has_product_access(telegram_id: int, product_id: str) -> bool:
    with _connect() as connection:
        return connection.execute(
            """SELECT 1 FROM purchases
               WHERE telegram_id = ? AND product_id = ? AND status = 'paid'
               LIMIT 1""",
            (telegram_id, product_id),
        ).fetchone() is not None


def get_user_purchases(telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with _connect() as connection:
        rows = connection.execute(
            """SELECT id, product_id, amount_stars, currency, status,
                      created_at, purchased_at, refunded_at
               FROM purchases WHERE telegram_id = ?
               ORDER BY id DESC LIMIT ?""",
            (telegram_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_purchase_by_id(purchase_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM purchases WHERE id = ?", (purchase_id,)
        ).fetchone()
        return _dict(row)


def mark_purchase_refunded(purchase_id: int) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            """UPDATE purchases SET status = 'refunded', refunded_at = ?
               WHERE id = ? AND status = 'paid'""",
            (_now(), purchase_id),
        )
        return cursor.rowcount == 1


# --------------------------------------------------
# USERS
# --------------------------------------------------

def upsert_user(
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> int:
    """Создаёт пользователя или обновляет его данные."""

    now = _now()

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name,
                last_name, created_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen_at = excluded.last_seen_at
            """,
            (
                telegram_id,
                username,
                first_name,
                last_name,
                now,
                now,
            ),
        )

        row = connection.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

        return int(row["id"])


def get_user_by_telegram_id(
    telegram_id: int,
) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return _dict(row)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Ищет пользователя по внутреннему номеру базы."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return _dict(row)


def search_users(
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Ищет пользователей по ID, username и имени."""
    query = query.strip().lstrip("@")

    if not query:
        return []

    limit = max(1, min(limit, 100))
    pattern = f"%{query}%"

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM users
            WHERE CAST(telegram_id AS TEXT) LIKE ?
               OR COALESCE(username, '') LIKE ?
               OR COALESCE(first_name, '') LIKE ?
               OR COALESCE(last_name, '') LIKE ?
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()

        return [dict(row) for row in rows]


def list_users(
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM users
            ORDER BY last_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows]


def set_user_blocked(
    telegram_id: int,
    blocked: bool,
) -> bool:
    """Блокирует или разблокирует пользователя."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE users
            SET is_blocked = ?
            WHERE telegram_id = ?
            """,
            (int(blocked), telegram_id),
        )
        return cursor.rowcount > 0


def is_user_blocked(telegram_id: int) -> bool:
    user = get_user_by_telegram_id(telegram_id)
    return bool(user and user["is_blocked"])


def get_all_user_telegram_ids(
    include_blocked: bool = False,
) -> list[int]:
    """Возвращает адресатов, которым можно делать рассылку."""
    sql = "SELECT telegram_id FROM users"

    if not include_blocked:
        sql += " WHERE is_blocked = 0"

    sql += " ORDER BY id"

    with _connect() as connection:
        rows = connection.execute(sql).fetchall()
        return [int(row["telegram_id"]) for row in rows]


# --------------------------------------------------
# CONVERSATIONS
# --------------------------------------------------

def get_or_create_conversation(
    telegram_id: int,
) -> dict[str, Any]:
    """Получает или создаёт один постоянный диалог с пользователем."""

    upsert_user(telegram_id)

    now = _now()

    with _connect() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO conversations (
                user_id, created_at, updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user["id"], now, now),
        )

        row = connection.execute(
            """
            SELECT * FROM conversations WHERE user_id = ?
            """,
            (user["id"],),
        ).fetchone()

        return dict(row)


def get_conversation_by_id(
    conversation_id: int,
) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT c.*, u.telegram_id, u.username,
                   u.first_name, u.last_name, u.is_blocked
            FROM conversations c
            JOIN users u ON u.id = c.user_id
            WHERE c.id = ?
            """,
            (conversation_id,),
        ).fetchone()

        return _dict(row)


def get_conversation_by_user(
    telegram_id: int,
) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT c.*, u.telegram_id, u.username,
                   u.first_name, u.last_name, u.is_blocked
            FROM conversations c
            JOIN users u ON u.id = c.user_id
            WHERE u.telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

        return _dict(row)


def list_conversations(
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Возвращает диалоги вместе с данными пользователей."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    sql = """
        SELECT
            c.*,
            u.telegram_id,
            u.username,
            u.first_name,
            u.last_name,
            u.is_blocked,
            (
                SELECT COALESCE(m.caption, m.text, '[' || m.content_type || ']')
                FROM messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.id DESC
                LIMIT 1
            ) AS last_message_preview
        FROM conversations c
        JOIN users u ON u.id = c.user_id
    """

    if unread_only:
        sql += " WHERE c.unread_count > 0"

    sql += """
        ORDER BY c.last_message_at DESC, c.id DESC
        LIMIT ? OFFSET ?
    """

    with _connect() as connection:
        rows = connection.execute(
            sql,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows]


def set_conversation_status(
    conversation_id: int,
    status: str,
) -> bool:
    if status not in {"open", "closed"}:
        raise ValueError("status должен быть open или closed")

    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE conversations
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, _now(), conversation_id),
        )
        return cursor.rowcount > 0


# --------------------------------------------------
# MESSAGES
# --------------------------------------------------

def add_message(
    conversation_id: int,
    sender: str,
    content_type: str = "text",
    text: str | None = None,
    caption: str | None = None,
    file_id: str | None = None,
    telegram_message_id: int | None = None,
    is_read: bool | None = None,
) -> int:
    """Сохраняет сообщение и обновляет счётчик непрочитанного."""

    if sender not in {"user", "admin"}:
        raise ValueError("sender должен быть user или admin")

    if is_read is None:
        is_read = sender == "admin"

    now = _now()

    with _connect() as connection:
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

        if conversation is None:
            raise ValueError(
                f"Диалог {conversation_id} не найден"
            )

        cursor = connection.execute(
            """
            INSERT INTO messages (
                conversation_id, sender, content_type,
                text, caption, file_id,
                telegram_message_id, is_read, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                sender,
                content_type,
                text,
                caption,
                file_id,
                telegram_message_id,
                int(is_read),
                now,
            ),
        )

        message_id = int(cursor.lastrowid)

        unread_increment = int(sender == "user" and not is_read)

        connection.execute(
            """
            UPDATE conversations
            SET last_message_at = ?,
                updated_at = ?,
                unread_count = unread_count + ?
            WHERE id = ?
            """,
            (now, now, unread_increment, conversation_id),
        )

        return message_id


def get_messages(
    conversation_id: int,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Получает историю от старых сообщений к новым.

    Если передан before_id, возвращает более старые сообщения.
    """
    limit = max(1, min(limit, 200))

    with _connect() as connection:
        if before_id is None:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                      AND id < ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (conversation_id, before_id, limit),
            ).fetchall()

        return [dict(row) for row in rows]


def get_message(
    message_id: int,
) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

        return _dict(row)


def mark_conversation_read(conversation_id: int) -> int:
    """Помечает входящие сообщения прочитанными."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE messages
            SET is_read = 1
            WHERE conversation_id = ?
              AND sender = 'user'
              AND is_read = 0
            """,
            (conversation_id,),
        )

        changed_count = cursor.rowcount

        connection.execute(
            """
            UPDATE conversations
            SET unread_count = 0, updated_at = ?
            WHERE id = ?
            """,
            (_now(), conversation_id),
        )

        return changed_count


def count_unread_messages() -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT COALESCE(SUM(unread_count), 0) AS total "
            "FROM conversations"
        ).fetchone()

        return int(row["total"])


# --------------------------------------------------
# BROADCASTS
# --------------------------------------------------

def create_broadcast(
    content_type: str = "text",
    text: str | None = None,
    caption: str | None = None,
    file_id: str | None = None,
    total_count: int = 0,
) -> int:
    """Регистрирует новую рассылку."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO broadcasts (
                content_type, text, caption, file_id,
                total_count, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_type,
                text,
                caption,
                file_id,
                max(0, total_count),
                _now(),
            ),
        )

        return int(cursor.lastrowid)


def finish_broadcast(
    broadcast_id: int,
    sent_count: int,
    failed_count: int,
    status: str = "completed",
) -> bool:
    if status not in {"completed", "partial", "failed"}:
        raise ValueError("Недопустимый статус рассылки")

    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE broadcasts
            SET sent_count = ?,
                failed_count = ?,
                status = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                max(0, sent_count),
                max(0, failed_count),
                status,
                _now(),
                broadcast_id,
            ),
        )

        return cursor.rowcount > 0


def list_broadcasts(
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM broadcasts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [dict(row) for row in rows]


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

def set_setting(key: str, value: str) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _now()),
        )


def get_setting(
    key: str,
    default: str | None = None,
) -> str | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()

        return str(row["value"]) if row else default


# --------------------------------------------------
# PERSONAL FINANCE
# --------------------------------------------------

def add_finance_transaction(
    telegram_id: int,
    kind: str,
    category: str,
    amount_cents: int,
    note: str | None = None,
) -> int:
    """Сохраняет финансовую запись, привязанную к Telegram ID владельца."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO finance_transactions (
                telegram_id, kind, category, amount_cents, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, kind, category, amount_cents, note, _now()),
        )
        return int(cursor.lastrowid)


def list_finance_transactions(
    telegram_id: int,
    since_month: str,
) -> list[dict[str, Any]]:
    """Возвращает собственные записи пользователя начиная с YYYY-MM."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM finance_transactions
            WHERE telegram_id = ? AND substr(created_at, 1, 7) >= ?
            ORDER BY created_at DESC, id DESC
            """,
            (telegram_id, since_month),
        ).fetchall()
        return [dict(row) for row in rows]


def delete_last_finance_transaction(telegram_id: int) -> bool:
    """Удаляет последнюю финансовую запись только её владельца."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id FROM finance_transactions
            WHERE telegram_id = ? ORDER BY id DESC LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "DELETE FROM finance_transactions WHERE id = ? AND telegram_id = ?",
            (row["id"], telegram_id),
        )
        return True


def set_finance_draft(
    telegram_id: int,
    kind: str,
    category: str | None = None,
) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO finance_drafts (telegram_id, kind, category, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                kind = excluded.kind,
                category = excluded.category,
                updated_at = excluded.updated_at
            """,
            (telegram_id, kind, category, _now()),
        )


def get_finance_draft(telegram_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT kind, category FROM finance_drafts WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return _dict(row)


def clear_finance_draft(telegram_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM finance_drafts WHERE telegram_id = ?",
            (telegram_id,),
        )


# --------------------------------------------------
# STATISTICS
# --------------------------------------------------

def get_statistics() -> dict[str, int]:
    """Возвращает основные показатели работы бота."""
    today = datetime.now(timezone.utc).date().isoformat()

    with _connect() as connection:
        users = connection.execute(
            "SELECT COUNT(*) AS total FROM users"
        ).fetchone()["total"]

        blocked = connection.execute(
            "SELECT COUNT(*) AS total FROM users WHERE is_blocked = 1"
        ).fetchone()["total"]

        conversations = connection.execute(
            "SELECT COUNT(*) AS total FROM conversations"
        ).fetchone()["total"]

        open_conversations = connection.execute(
            "SELECT COUNT(*) AS total FROM conversations "
            "WHERE status = 'open'"
        ).fetchone()["total"]

        messages = connection.execute(
            "SELECT COUNT(*) AS total FROM messages"
        ).fetchone()["total"]

        messages_today = connection.execute(
            "SELECT COUNT(*) AS total FROM messages "
            "WHERE substr(created_at, 1, 10) = ?",
            (today,),
        ).fetchone()["total"]

        replies_today = connection.execute(
            """
            SELECT COUNT(*) AS total FROM messages
            WHERE sender = 'admin'
              AND substr(created_at, 1, 10) = ?
            """,
            (today,),
        ).fetchone()["total"]

        unread = connection.execute(
            "SELECT COALESCE(SUM(unread_count), 0) AS total "
            "FROM conversations"
        ).fetchone()["total"]

    return {
        "users": int(users),
        "blocked_users": int(blocked),
        "conversations": int(conversations),
        "open_conversations": int(open_conversations),
        "messages": int(messages),
        "messages_today": int(messages_today),
        "replies_today": int(replies_today),
        "unread_messages": int(unread),
    }
