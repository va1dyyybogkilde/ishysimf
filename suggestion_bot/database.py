import os
import psycopg2
import psycopg2.extras

# Считываем ссылку на базу данных из переменных окружения Render
DATABASE_URL = os.getenv("DATABASE_URL")

# Render передает URL с префиксом postgres://, который нужно заменить на postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_connection():
    """Создает подключение к PostgreSQL."""
    if not DATABASE_URL:
        raise ValueError("Переменная окружения DATABASE_URL не установлена!")
    return psycopg2.connect(DATABASE_URL, sslmode="prefer")


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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
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
        conn.commit()


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


def get_stats() -> tuple[int, int]:
    """Получение статистики пользователей и постов."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users;")
            users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM posts;")
            posts_count = cur.fetchone()[0]
            return users_count, posts_count


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