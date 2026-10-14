import asyncio
import logging
import os
import re
import time
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI
import uvicorn

import config
import database as db

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- Коллажи (медиагруппы) — все фото в одном сообщении ---
from aiogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

album_data: dict[str, dict] = {}  # media_group_id -> {"msgs": [оригиналы], "topic_ids": [], "ts": float}
_album_flush_tasks: set[str] = set()

FIRST_MESSAGE_NOTICE = (
    "✅ <b>Ваше сообщение доставлено администратору.</b>\n\n"
    "С этого момента этот чат работает как диалог: вы можете уточнять детали "
    "или дополнять пост, а ответы модератора будут приходить прямо сюда.\n\n"
    "📌 Если вы прислали пост — после проверки он появится в канале."
)


def _prune_album_data(limit: int = 300) -> None:
    """Удаляет старые записи, чтобы память не росла бесконечно."""
    if len(album_data) <= limit:
        return
    now = time.monotonic()
    for key in [k for k, d in album_data.items() if now - d.get("ts", 0) > 6 * 3600]:
        album_data.pop(key, None)


def _build_media_list(msgs, final_caption: str | None = None) -> list:
    """Собирает InputMedia-объекты из оригинальных сообщений (для send_media_group)."""
    media = []
    for i, m in enumerate(msgs):
        cap = final_caption if i == 0 else None
        parse_mode = "HTML" if cap else None
        if m.photo:
            media.append(InputMediaPhoto(media=m.photo[-1].file_id, caption=cap, parse_mode=parse_mode))
        elif m.video:
            media.append(InputMediaVideo(media=m.video.file_id, caption=cap, parse_mode=parse_mode))
        elif m.document:
            media.append(InputMediaDocument(media=m.document.file_id, caption=cap, parse_mode=parse_mode))
        elif m.audio:
            media.append(InputMediaAudio(media=m.audio.file_id, caption=cap, parse_mode=parse_mode))
    return media


def get_album_action_keyboard(user_id: int, mgid: str) -> InlineKeyboardMarkup:
    """Кнопки под коллажем в топике модератора."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Опубликовать коллаж", callback_data=f"pub_req:{user_id}:{mgid}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_req:{user_id}")
    ]])


def get_album_confirm_keyboard(user_id: int, mgid: str) -> InlineKeyboardMarkup:
    """Кнопка подтверждения публикации коллажа."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Подтвердить публикацию!", callback_data=f"pub_confirm:{user_id}:{mgid}")],
        [InlineKeyboardButton(text="↩ Отмена", callback_data=f"pub_cancel:{user_id}:{mgid}")]
    ])


async def _send_first_notice(user_id: int) -> None:
    """Единожды шлём пользователю уведомление о режиме чата."""
    if db.is_welcomed(user_id):
        return
    try:
        await bot.send_message(chat_id=user_id, text=FIRST_MESSAGE_NOTICE)
        db.mark_welcomed(user_id)
    except TelegramAPIError:
        pass


async def flush_album_group(mgid: str, user_id: int, topic_id: int) -> None:
    """Собирает все фото коллажа в одно сообщение и отправляет в топик модератора."""
    await asyncio.sleep(1.2)  # ждём доставки всех фото группы
    try:
        entry = album_data.get(mgid)
        _album_flush_tasks.discard(mgid)
        if not entry or not entry.get("msgs"):
            return
        msgs = entry["msgs"]
        media = list(_build_media_list(msgs))
        if not media:
            # не медиа-группа — просто переслать поштучно
            for m in msgs:
                try:
                    await m.copy_to(chat_id=config.ADMIN_CHAT_ID, message_thread_id=topic_id,
                                    reply_markup=get_post_action_keyboard(user_id))
                except TelegramAPIError:
                    pass
            await _send_first_notice(user_id)
            return
        sent = await bot.send_media_group(
            chat_id=config.ADMIN_CHAT_ID,
            message_thread_id=topic_id,
            media=media,
        )
        entry["topic_ids"] = [m.message_id for m in sent]
        # Коллаж в топике теперь одно сообщение — под ним клавиатура модератора
        await bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            message_thread_id=topic_id,
            text=f"📎 Коллаж ({len(sent)} элементов).",
            reply_markup=get_album_action_keyboard(user_id, mgid),
        )
        await _send_first_notice(user_id)
    except Exception as e:
        logging.error(f"Ошибка отправки коллажа в топик: {e}")
        _album_flush_tasks.discard(mgid)
        for m in (album_data.pop(mgid, {}).get("msgs") or []):
            try:
                await m.copy_to(chat_id=config.ADMIN_CHAT_ID, message_thread_id=topic_id,
                                reply_markup=get_post_action_keyboard(user_id))
            except TelegramAPIError:
                pass
        await _send_first_notice(user_id)

# --- Настройка FastAPI для Web Service на Render ---
app = FastAPI()


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "Bot is active and running 24/7!"}


async def run_fastapi():
    """Запуск веб-сервера на порту 10000 (требование Render)."""
    port = int(os.getenv("PORT", 10000))
    server_config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


# --------------------------------------------------


# Проверка прав администратора
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# FSM-состояния для админ-панели
class AdminStates(StatesGroup):
    waiting_for_post_link = State()
    waiting_for_broadcast_text = State()


def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главного меню админ-панели."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти автора поста", callback_data="admin_find")],
        [InlineKeyboardButton(text="📢 Массовая рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="❌ Закрыть меню", callback_data="admin_close")]
    ])


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены для режимов ввода."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")
    ]])


def get_post_action_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Стандартные кнопки под предложенным постом."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Опубликовать пост", callback_data=f"pub_req:{user_id}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_req:{user_id}")
    ]])


def get_find_result_keyboard(channel_msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с действиями над найденным постом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить пост из канала", callback_data=f"del_post:{channel_msg_id}")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="❌ Закрыть меню", callback_data="admin_close")]
    ])


def get_pub_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения публикации."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Подтвердить публикацию!", callback_data=f"pub_confirm:{user_id}")],
        [InlineKeyboardButton(text="↩ Отмена", callback_data=f"reset_actions:{user_id}")]
    ])


def get_block_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения блокировки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Подтвердить блокировку!", callback_data=f"block_confirm:{user_id}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"reset_actions:{user_id}")]
    ])


async def get_or_create_user_topic(user: types.User) -> int:
    """Получение или создание темы (топика) для пользователя в админ-чате с записью в БД."""
    user_id = user.id
    topic_id = db.get_user_topic(user_id)

    if not topic_id:
        topic_name = (user.full_name or f"User {user_id}")[:128]
        topic = await bot.create_forum_topic(chat_id=config.ADMIN_CHAT_ID, name=topic_name)
        topic_id = topic.message_thread_id

        username = user.username or ""
        db.save_user_topic(user_id, username, topic_name, topic_id)

        user_username = f"@{username}" if username else "без имени пользователя"
        info_text = (
            f"👤 <b>Новый пользователь зарегистрирован!</b>\n\n"
            f"<b>{user.full_name}</b> – {user_username}\n"
            f"<code>ID: {user_id}</code> | 🌐 {user.language_code or 'ru'}"
        )
        pinned = await bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            message_thread_id=topic_id,
            text=info_text,
            parse_mode="HTML"
        )
        try:
            await bot.pin_chat_message(chat_id=config.ADMIN_CHAT_ID, message_id=pinned.message_id)
        except TelegramAPIError:
            pass

    return topic_id


# 1. Открытие админ-панели через /admin или /start (для админов) и приветствие пользователей
# noinspection PyUnfilledRequiredParameters
@dp.message(Command("admin"))
# noinspection PyUnfilledRequiredParameters
@dp.message(Command("start"))
async def cmd_start_admin(message: types.Message, state: FSMContext) -> None:
    await state.clear()

    if not message.from_user:
        return

    # Если пишет обычный пользователь (не админ)
    if not is_admin(message.from_user.id):
        if db.is_user_blocked(message.from_user.id):
            await message.answer("❌ Вы заблокированы в этом боте.")
            return

        if message.text and message.text.startswith("/start"):
            welcome_text = (
                "Здравствуйте! 👋\n\n"
                "Сразу пишите, с какой целью вы обратились:\n"
                "• ℹ️ Узнать статус или удалить пост\n"
                "• 📝 Опубликовать свой пост\n\n"
                "📌 Если хотите опубликовать пост — не ждите ответа модератора, "
                "сразу присылайте текст и фото/видео!"
            )
            await message.answer(welcome_text)

            try:
                await get_or_create_user_topic(message.from_user)
            except TelegramAPIError as e:
                logging.error(f"Ошибка при создании темы регистрации: {e}")
        return

    # Если пишет админ
    await message.answer(
        "<b>Панель администратора предложки</b>\nВыберите необходимое действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )


# Республика кэша ссылки на канал для /myposts
_channel_username: str | None = None


async def _get_channel_username() -> str | None:
    """Кэширует username канала, чтобы строить публичные ссылки на посты."""
    global _channel_username
    if _channel_username is None:
        try:
            chat = await bot.get_chat(config.CHANNEL_ID)
            _channel_username = chat.username or ""
        except TelegramAPIError:
            _channel_username = ""
    return _channel_username or None


def _post_link(channel_username: str | None, msg_id: int) -> str:
    """Ссылка на пост: публичная (t.me/username/id) или скрытая (t.me/c/id/id)."""
    if channel_username:
        return f"https://t.me/{channel_username}/{msg_id}"
    clean = str(config.CHANNEL_ID)
    if clean.startswith("-100"):
        clean = clean[4:]
    return f"https://t.me/c/{clean}/{msg_id}"


# 1b. Команда /myposts — статус своих опубликованных постов
@dp.message(Command("myposts", "my_posts"))
async def cmd_my_posts(message: types.Message) -> None:
    if not message.from_user or is_admin(message.from_user.id):
        return

    if db.is_user_blocked(message.from_user.id):
        await message.answer("❌ Вы заблокированы в этом боте.")
        return

    posts = db.get_user_posts(message.from_user.id)
    if not posts:
        await message.answer(
            "У вас пока нет опубликованных постов.\n\n"
            "Пришлите текст или фото — после модерации он появится в канале, "
            "а ссылка придёт сюда по команде /myposts."
        )
        return

    channel_username = await _get_channel_username()
    lines = []
    for i, (channel_msg_id, post_text, created_at) in enumerate(posts, 1):
        preview = (post_text or "[Медиафайл]").strip().replace("\n", " ")[:60]
        try:
            date_str = created_at.strftime("%d.%m.%Y %H:%M")
        except AttributeError:
            date_str = ""
        link = _post_link(channel_username, channel_msg_id)
        lines.append(
            f"{i}. <b>{preview}…</b>\n"
            f"   📅 {date_str}\n"
            f"   🔗 <a href='{link}'>открыть пост</a>"
        )

    await message.answer(
        f"<b>Ваши опубликованные посты ({len(posts)}):</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# 1c. Команда /help
@dp.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    text = (
        "📖 <b>Ищу тебя Симферополь</b> — бот для публикации объявлений о поиске людей.\n\n"
        "📝 <b>Как оставить объявление:</b>\n"
        "Просто пришлите сообщение (текст, фото или видео). Оно уйдёт модераторам, "
        "и после проверки появится в нашем канале.\n\n"
        "🔗 <b>Узнать свой пост:</b>\n"
        "Команда /myposts покажет ваши опубликованные объявления и ссылки на них.\n\n"
        "♻️ <b>Удалить пост:</b>\n"
        "Напишите «удалить пост» — модератор найдёт его по вашей ссылке и уберёт из канала.\n\n"
        "📌 <b>Правила:</b> без оскорблений, спама и личных данных третьих лиц. "
        "За нарушения — блокировка."
    )
    await message.answer(text, parse_mode="HTML")


# 2. Обработка кнопок админ-панели
@dp.callback_query(F.data.startswith("admin_"))
async def handle_admin_callbacks(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not isinstance(callback.message, types.Message):
        await callback.answer("Ошибка доступа к сообщению.", show_alert=True)
        return

    action = callback.data

    if action == "admin_stats":
        users_count, posts_count = db.get_stats()
        await callback.message.edit_text(
            f"<b>📊 Статистика бота:</b>\n\n"
            f"👤 Всего пользователей: <b>{users_count}</b>\n"
            f"📝 Опубликовано постов: <b>{posts_count}</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()

    elif action == "admin_find":
        await state.set_state(AdminStates.waiting_for_post_link)
        await callback.message.edit_text(
            "🔍 <b>Поиск автора поста:</b>\n\n"
            "Пришлите ссылку на пост из канала или его ID:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()

    elif action == "admin_broadcast":
        await state.set_state(AdminStates.waiting_for_broadcast_text)
        await callback.message.edit_text(
            "📢 <b>Массовая рассылка:</b>\n\n"
            "Пришлите текст сообщения, которое нужно отправить всем пользователям бота:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()

    elif action == "admin_cancel":
        await state.clear()
        await callback.message.edit_text(
            "<b>Панель администратора предложки</b>\nДействие отменено. Выберите вариант:",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer("Отменено")

    elif action == "admin_close":
        await state.clear()
        await callback.message.delete()
        await callback.answer()


# 3. Поиск автора поста по ссылке или ID
@dp.message(AdminStates.waiting_for_post_link)
async def process_find_input(message: types.Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return

    query = (message.text or "").strip()
    channel_msg_id = None

    match = re.search(r"t\.me/(?:c/\d+|[^/]+)/(\d+)", query)
    if match:
        channel_msg_id = int(match.group(1))
    elif query.isdigit():
        channel_msg_id = int(query)

    if not channel_msg_id:
        await message.answer(
            "❌ Некорректный формат ссылки или ID. Попробуйте еще раз или нажмите Отмена:",
            reply_markup=get_cancel_keyboard()
        )
        return

    post_data = db.find_post_by_channel_msg_id(channel_msg_id)
    if not post_data:
        await state.clear()
        await message.answer(
            f"❌ Пост #{channel_msg_id} не найден в базе данных бота.",
            reply_markup=get_admin_keyboard()
        )
        return

    user_id, topic_id, post_text, created_at = post_data
    user_info = db.get_user_info(user_id)

    user_tag = f"@{user_info[0]}" if user_info and user_info[0] else "без имени пользователя"
    full_name = user_info[1] if user_info else "Неизвестно"

    admin_chat_clean = str(config.ADMIN_CHAT_ID).replace("-100", "")
    topic_link = f"https://t.me/c/{admin_chat_clean}/{topic_id}"

    response = (
        f"<b>Информация о посте #{channel_msg_id}:</b>\n\n"
        f"👤 <b>Автор:</b> {full_name} ({user_tag})\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📅 <b>Дата публикации:</b> {created_at}\n"
        f"💬 <b>Текст поста:</b>\n{post_text}\n\n"
        f"🔗 <a href='{topic_link}'>Перейти в тему обсуждения</a>"
    )

    await state.clear()
    await message.answer(
        response,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_find_result_keyboard(channel_msg_id)
    )


# 4. Выполнение массовой рассылки
@dp.message(AdminStates.waiting_for_broadcast_text)
async def process_broadcast_input(message: types.Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return

    text_to_send = message.text or ""
    users = db.get_all_users()
    delivered = 0

    status_msg = await message.answer("⏳ Выполняется рассылка...")

    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=text_to_send)
            delivered += 1
            await asyncio.sleep(0.05)
        except TelegramAPIError:
            pass

    await state.clear()
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\nУспешно доставлено: <b>{delivered}/{len(users)}</b> пользователей.",
        parse_mode="HTML"
    )
    await message.answer("Панель управления:", reply_markup=get_admin_keyboard())


# 5. Прием предложений от пользователей в ЛС (с поддержкой медиа и альбомов)
@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message) -> None:
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return

    if db.is_user_blocked(message.from_user.id):
        await message.answer("❌ Вы заблокированы в предложке.")
        return

    try:
        topic_id = await get_or_create_user_topic(message.from_user)
    except TelegramAPIError as e:
        logging.error(f"Ошибка при получении/создании темы: {e}")
        await message.answer("⚠️ Не удалось связаться с администрацией. Проверьте настройки админ-чата.")
        return

    # Если это коллаж (медиагруппа) — буферизируем все фото и отправим одним сообщением
    if message.media_group_id:
        mgid = message.media_group_id
        entry = album_data.get(mgid)
        if not entry:
            entry = {"msgs": [], "topic_ids": [], "ts": time.monotonic()}
            album_data[mgid] = entry
        entry["msgs"].append(message)
        if mgid not in _album_flush_tasks:
            _album_flush_tasks.add(mgid)
            asyncio.create_task(flush_album_group(mgid, message.from_user.id, topic_id))
        return

    # Обычное сообщение: копируем в топик админ-чата
    await message.copy_to(
        chat_id=config.ADMIN_CHAT_ID,
        message_thread_id=topic_id,
        reply_markup=get_post_action_keyboard(message.from_user.id)
    )
    # Единожды уведомляем пользователя о режиме чата
    await _send_first_notice(message.from_user.id)

# 6. Запрос на публикацию / замена клавиатуры на подтверждение
@dp.callback_query(F.data.startswith("pub_req:"))
async def pub_request_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(":")
    user_id = int(parts[1])
    mgid = parts[2] if len(parts) > 2 else None

    if isinstance(callback.message, types.Message):
        kb = get_album_confirm_keyboard(user_id, mgid) if mgid else get_pub_confirm_keyboard(user_id)
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Нажмите «Подтвердить публикацию!» еще раз.", show_alert=False)

# 7. Подтвержденная публикация поста в канал
@dp.callback_query(F.data.startswith("pub_confirm:"))
async def publish_post_confirm(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data or not isinstance(callback.message, types.Message):
        return

    parts = callback.data.split(":")
    user_id = int(parts[1])
    mgid = parts[2] if len(parts) > 2 else None
    source_msg = callback.message

    original_text = source_msg.text or source_msg.caption or ""
    footer = getattr(config, "POST_FOOTER", "")
    final_primary = (f"{original_text}\n\n{footer}").strip() if footer else original_text.strip()

    published_ids: list[int] = []
    try:
        if mgid:
            # Коллаж: собираем медиа из оригинальных сообщений и шлём ОДНИМ коллажем
            entry = album_data.pop(mgid, {})
            msgs = entry.get("msgs") or []
            media = list(_build_media_list(msgs, final_primary))
            if media:
                sent = await bot.send_media_group(
                    chat_id=config.CHANNEL_ID,
                    media=media,
                )
                published_ids = [m.message_id for m in sent]
            else:
                # не медиа — копируем поштучно
                for m in msgs:
                    pm = await m.copy_to(chat_id=config.CHANNEL_ID)
                    published_ids.append(pm.message_id)
        else:
            if source_msg.text:
                pm = await bot.send_message(
                    chat_id=config.CHANNEL_ID,
                    text=final_primary,
                    parse_mode="HTML",
                )
            else:
                pm = await bot.copy_message(
                    chat_id=config.CHANNEL_ID,
                    from_chat_id=source_msg.chat.id,
                    message_id=source_msg.message_id,
                    caption=final_primary,
                    parse_mode="HTML",
                )
            published_ids.append(pm.message_id)
    except TelegramBadRequest as e:
        logging.error(f"Ошибка публикации поста: {e}")
        await callback.answer(
            "❌ Бот не смог опубликовать пост! Убедитесь, что бот добавлен в канал и имеет право 'Публикация сообщений'.",
            show_alert=True,
        )
        return

    # Сохраняем в БД (первый ID — по нему строится ссылка, остальные — для удаления коллажа)
    db.save_post(
        channel_msg_id=published_ids[0],
        user_id=int(user_id),
        topic_id=source_msg.message_thread_id or 0,
        post_text=original_text or "[Медиафайл]",
        media_ids=published_ids,
    )

    await callback.answer("Пост успешно опубликован!", show_alert=True)
    published_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовано", callback_data="none"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_req:{user_id}")
    ]])
    await callback.message.edit_reply_markup(reply_markup=published_kb)


@dp.callback_query(F.data.startswith("pub_cancel:"))
async def pub_cancel_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(":")
    user_id = int(parts[1])
    mgid = parts[2] if len(parts) > 2 else None

    if isinstance(callback.message, types.Message):
        kb = get_album_action_keyboard(user_id, mgid) if mgid else get_post_action_keyboard(user_id)
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Отменено")

# 8. Запрос на блокировку / замена клавиатуры на подтверждение
@dp.callback_query(F.data.startswith("block_req:"))
async def block_request_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, user_id_str = callback.data.split(":")
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=get_block_confirm_keyboard(int(user_id_str)))
    await callback.answer("Нажмите «Подтвердить блокировку!» еще раз.", show_alert=False)


# 9. Подтвержденная блокировка пользователя
@dp.callback_query(F.data.startswith("block_confirm:"))
async def block_user_confirm(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, user_id_str = callback.data.split(":")
    user_id = int(user_id_str)

    db.block_user(user_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Разблокировать", callback_data=f"unblock:{user_id}")
    ]])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=kb)

    await callback.answer("Пользователь заблокирован!", show_alert=True)


# 10. Разблокировка пользователя из админки
@dp.callback_query(F.data.startswith("unblock:"))
async def unblock_user_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, user_id_str = callback.data.split(":")
    user_id = int(user_id_str)

    db.unblock_user(user_id)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=get_post_action_keyboard(user_id))

    await callback.answer("Пользователь разблокирован!", show_alert=True)


# 12. Удаление поста из канала (вызывается из результатов поиска поста)
@dp.callback_query(F.data.startswith("del_post:"))
async def delete_post_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, channel_msg_id_str = callback.data.split(":")
    channel_msg_id = int(channel_msg_id_str)

    # media_ids возвращается из БД для удаления всего альбома
    media_ids = db.delete_post(channel_msg_id) or [channel_msg_id]

    deleted_count = 0
    for mid in media_ids:
        try:
            await bot.delete_message(chat_id=config.CHANNEL_ID, message_id=mid)
            deleted_count += 1
        except TelegramAPIError:
            pass

    if isinstance(callback.message, types.Message):
        if deleted_count:
            await callback.message.edit_text(
                "🗑 Пост удалён из канала.",
                reply_markup=get_admin_keyboard()
            )
        else:
            await callback.message.edit_text(
                "⚠️ Не удалось удалить пост (возможно, он уже удалён).",
                reply_markup=get_admin_keyboard()
            )
    await callback.answer(
        f"Удалено сообщений: {deleted_count}" if deleted_count else "Пост не найден в канале.",
        show_alert=True
    )


# 11. Сброс действий (кнопка Отмена)
@dp.callback_query(F.data.startswith("reset_actions:"))
async def reset_actions_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, user_id_str = callback.data.split(":")
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=get_post_action_keyboard(int(user_id_str)))
    await callback.answer("Действие отменено.")


# 13. Отправка ответа администратора из темы автору
@dp.message(F.chat.id == config.ADMIN_CHAT_ID)
async def handle_admin_reply(message: types.Message) -> None:
    if not message.message_thread_id or (message.text and message.text.startswith("/")):
        return

    user_id = db.get_user_by_topic(message.message_thread_id)
    if user_id:
        try:
            await message.copy_to(chat_id=user_id)
        except TelegramAPIError:
            await message.reply("Не удалось доставить сообщение (пользователь заблокировал бота).")


async def main() -> None:
    db.init_db()
    await _health_check()
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот и веб-сервер успешно запущены!")

    # Одновременный запуск FastAPI (для пингов) и aiogram (для бота)
    await asyncio.gather(
        run_fastapi(),
        dp.start_polling(bot)
    )


async def _health_check() -> None:
    """Проверяет при старте, что бот имеет доступ к каналу и админ-группе."""
    try:
        me = await bot.get_me()
        print(f"🤖 Бот: @{me.username}")
    except TelegramAPIError as e:
        print(f"❌ Не удалось получить информацию о боте: {e}")
        return
    for label, chat_id in (("админ-группа", config.ADMIN_CHAT_ID),
                           ("канал", config.CHANNEL_ID)):
        try:
            chat = await bot.get_chat(chat_id)
            ok = "✅" if chat.type in ("supergroup", "channel", "group") else "⚠️"
            print(f"{ok} Доступ к {label} ({chat_id}): {chat.title}")
        except TelegramAPIError as e:
            print(f"❌ Нет доступа к {label} ({chat_id}). "
                  f"Добавьте бота администратором и включите топики. {e}")
    try:
        topic = await bot.create_forum_topic(chat_id=config.ADMIN_CHAT_ID, name="health-check")
        print("✅ Бот может создавать топики в админ-группе")
        try:
            await bot.delete_forum_topic(
                chat_id=config.ADMIN_CHAT_ID,
                message_thread_id=topic.message_thread_id,
            )
            print("   (тестовый топик удалён)")
        except TelegramAPIError:
            pass
    except TelegramAPIError as e:
        print(f"⚠️ Не удалось создать топик в админ-группе (убедитесь, что включены форумы): {e}")


if __name__ == "__main__":
    asyncio.run(main())