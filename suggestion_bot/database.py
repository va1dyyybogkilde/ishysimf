import os
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager

# Считываем ссылку на базу данных из переменных окружения Render
DATABASE_URL = os.getenv("DATABASE_URL")

# Render передает URL с префиксом postgres://, который нужно заменить на postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


# --- Пул подключений (не открываем новое соединение на каждый запрос) ---
_pool: ThreadedConnectionPool | None = None
_POOL_MIN = 1
_POOL_MAX = 5


def _get_pool() -> ThreadedConnectionPool:
    """Лениво создаёт пул подключений к PostgreSQL."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise ValueError("Переменная окружения DATABASE_URL не установлена!")
        _pool = ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, DATABASE_URL, sslmode="prefer")
    return _pool


@contextmanager
def get_connection():
    """Возвращает подключение из пула и возвращает его обратно после использования.

    Поддерживает прежний синтаксис `with get_connection() as conn:`.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            pool.putconn(conn, close=False)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def ping() -> bool:
    """Проверка доступности БД (для health-check)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1


def init_db() -> None:
    """Инициализация таблиц базы данных в PostgreSQL."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    topic_name TEXT,
                    topic_id BIGINT,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    welcomed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Для уже существующей таблицы — только добавляем колонку, данные не трогаем
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS welcomed BOOLEAN DEFAULT FALSE;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    channel_msg_id BIGINT UNIQUE,
                    user_id BIGINT,
                    topic_id BIGINT,
                    post_text TEXT,
                    media_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Для уже существующих таблиц (старая схема без media_ids)
            cur.execute("""
                ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_ids TEXT;
            """)
            # Таблица администраторов: обычные админы (НЕ owners)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    added_by BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.commit()


# --- Управление администраторами (обычные админы, которых добавляют OWNER'ы) ---

def add_admin(user_id: int, added_by: int) -> bool:
    """Добавляет обычного администратора. True если добавлен новый, False если уже был."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admins (user_id, added_by) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET added_by = EXCLUDED.added_by "
                "RETURNING (xmax = 0) AS inserted",
                (user_id, added_by),
            )
            row = cur.fetchone()
            return bool(row and row[0])


def remove_admin(user_id: int) -> bool:
    """Удаляет обычного администратора. Возвращает True если был удалён."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admins WHERE user_id = %s RETURNING user_id", (user_id,))
            return cur.fetchone() is not None


def is_admin_user(user_id: int) -> bool:
    """Является ли пользователь дополнительным (не основным) администратором."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


def get_admin_records() -> list[tuple]:
    """Все обычные администраторы (user_id, added_by, created_at)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, added_by, created_at FROM admins ORDER BY created_at")
            return cur.fetchall()


def get_stats() -> tuple[int, int]:
    """Получение статистики пользователей и постов."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users;")
            users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM posts;")
            posts_count = cur.fetchone()[0]
            return users_count, posts_count


def get_daily_stats(since) -> tuple[int, int, int, int]:
    """Статистика за период: (новых юзеров, новых постов, всего юзеров, всего постов).

    `since` — datetime, с которого считать 'новых'.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE created_at >= %s", (since,))
            new_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM posts WHERE created_at >= %s", (since,))
            new_posts = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM posts")
            total_posts = cur.fetchone()[0]
            return new_users, new_posts, total_users, total_posts


def find_post_by_channel_msg_id(channel_msg_id: int) -> tuple | None:
    """Поиск поста по ID сообщения из канала."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, topic_id, post_text, created_at
                FROM posts
                WHERE channel_msg_id = %s;
            """, (channel_msg_id,))
            return cur.fetchone()


def get_user_info(user_id: int) -> tuple | None:
    """Получение имени пользователя и названия топика."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, topic_name FROM users WHERE user_id = %s;", (user_id,))
            return cur.fetchone()


def get_all_users() -> list[int]:
    """Список всех не заблокированных пользователей для рассылки."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE is_blocked = FALSE;")
            rows = cur.fetchall()
            return [row[0] for row in rows]


def get_user_topic(user_id: int) -> int | None:
    """Получение topic_id пользователя."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT topic_id FROM users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None


def save_user_topic(user_id: int, username: str, topic_name: str, topic_id: int) -> None:
    """Сохранение или обновление данных топика пользователя."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, topic_name, topic_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    topic_name = EXCLUDED.topic_name,
                    topic_id = EXCLUDED.topic_id;
            """, (user_id, username, topic_name, topic_id))
        conn.commit()


def is_user_blocked(user_id: int) -> bool:
    """Проверка заблокирован ли пользователь."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_blocked FROM users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else False


def is_welcomed(user_id: int) -> bool:
    """Получал ли пользователь уже приветственное уведомление."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT welcomed FROM users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else False


def mark_welcomed(user_id: int) -> None:
    """Пометить, что уведомление пользователю уже отправлено."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET welcomed = TRUE WHERE user_id = %s;", (user_id,))
        conn.commit()


def block_user(user_id: int) -> None:
    """Блокировка пользователя."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, is_blocked)
                VALUES (%s, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET is_blocked = TRUE;
            """, (user_id,))
        conn.commit()


def unblock_user(user_id: int) -> None:
    """Разблокировка пользователя."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_blocked = FALSE WHERE user_id = %s;", (user_id,))
        conn.commit()


def save_post(
    channel_msg_id: int,
    user_id: int,
    topic_id: int,
    post_text: str,
    media_ids: list[int] | None = None,
) -> None:
    """Сохранение опубликованного поста (media_ids — ID сообщений альбома в канале)."""
    media_ids_str = ",".join(str(x) for x in media_ids) if media_ids else None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO posts (channel_msg_id, user_id, topic_id, post_text, media_ids)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (channel_msg_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    topic_id = EXCLUDED.topic_id,
                    post_text = EXCLUDED.post_text,
                    media_ids = EXCLUDED.media_ids;
            """, (channel_msg_id, user_id, topic_id, post_text, media_ids_str))
        conn.commit()


def delete_post(channel_msg_id: int) -> list[int] | None:
    """Удаление поста из БД. Возвращает сохранённые media_ids (ID сообщений в канале)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT media_ids FROM posts WHERE channel_msg_id = %s;", (channel_msg_id,))
            row = cur.fetchone()
            cur.execute("DELETE FROM posts WHERE channel_msg_id = %s;", (channel_msg_id,))
            conn.commit()
            if not row or not row[0]:
                return None
            return [int(x) for x in row[0].split(",") if x.strip().lstrip("-").isdigit()]


def get_user_by_topic(topic_id: int) -> int | None:
    """Получение user_id по topic_id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE topic_id = %s;", (topic_id,))
            row = cur.fetchone()
            return row[0] if row else None


def get_user_posts(user_id: int) -> list[tuple]:
    """Все опубликованные посты пользователя (свежие первыми)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_msg_id, post_text, created_at FROM posts "
                "WHERE user_id = %s ORDER BY id DESC;",
                (user_id,),
            )
            return cur.fetchall()
