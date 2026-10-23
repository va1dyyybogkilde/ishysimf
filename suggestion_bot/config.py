import os
from dotenv import load_dotenv

# Ищем .env и в папке бота, и в корне проекта — чтобы работало из любого CWD
_env_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_env_dir, ".env"))
load_dotenv(os.path.join(_env_dir, "..", ".env"))
load_dotenv()

# Токен бота от @BotFather (обязательно в env на Render / в .env локально)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ID супергруппы администрации (с включенными форумами/топиками)
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1001234567890"))

# ID канала для публикации постов
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1009876543210"))

# Список OWNER'ов — сверх-администраторов, которые могут УПРАВЛЯТЬ администраторами
# (добавлять/удалять). Это ваши личные ID. Обычные администраторы (в БД)
# могут модерировать, но НЕ добавляют новых админов.
OWNER_IDS = [
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "7541245548,81248243").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ADMIN_IDS — базовый набор администраторов из env (OWNERS по умолчанию).
# Реальный список админов дополняется таблицей `admins` в БД.
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", ",".join(str(i) for i in OWNER_IDS)).split(",")
    if x.strip().lstrip("-").isdigit()
]

# Автоматическая подпись под постом в формате HTML
DEFAULT_FOOTER = (
    "предложить пост : @ishysimf_bot\n\n"
    "удалить / узнать пост: @ishysimf_bot\n\n"
    '📢 <a href="https://t.me/ishysimf">Ищу тебя Симферополь</a>'
)

POST_FOOTER = os.getenv("POST_FOOTER", DEFAULT_FOOTER).replace("\\n", "\n")