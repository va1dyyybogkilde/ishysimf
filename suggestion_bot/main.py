import asyncio
import logging
import os
import re
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

# --- Настройка FastAPI для Web Service на Render ---
app = FastAPI()


@app.get("/")
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
                "• ℹ Узнать статус или удалить пост\n"
                "• 📝 Опубликовать свой пост\n\n"
                "📌 Если хотите опубликовать пост — не ждите ответа модератора, "
                "сразу присылайте текст и фото/видео!\n\n"
                "🔍 <b>Ищу тебя Симферополь</b> — бот для подачи объявлений о поиске людей"
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
        reply_markup=get_admin_keyboard()
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

    kb = get_post_action_keyboard(message.from_user.id)

    # Если это часть медиагруппы (альбома), Telegram шлет их пачкой.
    # Копируем сообщение в топик админ-чата вместе с клавиатурой управления под ним.
    await message.copy_to(
        chat_id=config.ADMIN_CHAT_ID,
        message_thread_id=topic_id,
        reply_markup=kb
    )

    # Отправляем подтверждение пользователю только для первого сообщения из пачки
    # (чтобы не спамить «Ваше предложение отправлено» 5 раз, если прислали 5 фото)
    if not message.media_group_id:
        await message.answer("Ваше предложение отправлено администрации!")
    else:
        # Для альбомов проверяем, отправляли ли мы уже уведомление этому пользователю недавно,
        # либо отправляем один раз на группу (можно просто дублировать текст, но лучше мягко уведомить)
        pass


# 6. Запрос на публикацию / замена клавиатуры на подтверждение
@dp.callback_query(F.data.startswith("pub_req:"))
async def pub_request_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data:
        return

    _, user_id_str = callback.data.split(":")
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=get_pub_confirm_keyboard(int(user_id_str)))
    await callback.answer("Нажмите «Подтвердить публикацию!» еще раз.", show_alert=False)


# 7. Подтвержденная публикация поста в канал
@dp.callback_query(F.data.startswith("pub_confirm:"))
async def publish_post_confirm(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    if not callback.data or not isinstance(callback.message, types.Message):
        return

    _, user_id = callback.data.split(":")
    source_msg = callback.message

    original_text = source_msg.text or source_msg.caption or ""
    footer = getattr(config, "POST_FOOTER", "")
    final_caption = f"{original_text}\n\n{footer}".strip() if footer else original_text.strip()

    try:
        if source_msg.text:
            published_msg = await bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=final_caption,
                parse_mode="HTML"
            )
        else:
            published_msg = await bot.copy_message(
                chat_id=config.CHANNEL_ID,
                from_chat_id=source_msg.chat.id,
                message_id=source_msg.message_id,
                caption=final_caption,
                parse_mode="HTML"
            )
    except TelegramBadRequest as e:
        logging.error(f"Ошибка публикации поста: {e}")
        await callback.answer(
            "❌ Бот не смог опубликовать пост! Убедитесь, что бот добавлен в канал и имеет право 'Публикация сообщений'.",
            show_alert=True
        )
        return

    db.save_post(
        channel_msg_id=published_msg.message_id,
        user_id=int(user_id),
        topic_id=source_msg.message_thread_id or 0,
        post_text=original_text or "[Медиафайл]"
    )

    await callback.answer("Пост успешно опубликован!", show_alert=True)
    published_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовано", callback_data="none"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_req:{user_id}")
    ]])
    await callback.message.edit_reply_markup(reply_markup=published_kb)


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


# 12. Отправка ответа администратора из темы автору
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
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот и веб-сервер успешно запущены!")

    # Одновременный запуск FastAPI (для пингов) и aiogram (для бота)
    await asyncio.gather(
        run_fastapi(),
        dp.start_polling(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())