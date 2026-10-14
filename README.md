# Бот предложка — Ищу тебя Симферополь

Телеграм-бот для приёма объявлений о поиске людей в Симферополе. Полная копия архитектуры бота "Фрики Крыма".

## Возможности

- Приём постов (текст + фото/видео) в личных сообщениях
- Публикация в канал через модерацию
- Тематические топики в админ-группе на каждого пользователя
- Блокировка/разблокировка пользователей
- Поиск автора поста
- Массовая рассылка
- Статистика

## Деплой на Render (бесплатно)

### 1. Создайте проект на Render

1. https://render.com → New → Web Service
2. Подключите GitHub репозиторий
3. Settings:
   - **Name:** `ishysimf-bot`
   - **Region:** Frankfurt (или ближайший)
   - **Branch:** `main`
   - **Root Directory:** (пусто)
   - **Runtime:** `Python`
   - **Build Command:** `pip install -r suggestion_bot/requirements.txt`
   - **Start Command:** `cd suggestion_bot && python main.py`

### 2. Переменные окружения

В Render → Environment:

```
BOT_TOKEN=ВАШ_ТОКЕН_БОТА
ADMIN_CHAT_ID=-100XXXXXXXXX      # ID группы администрации с форумами
CHANNEL_ID=-100XXXXXXXXX         # ID канала для публикации
ADMIN_IDS=XXXXXXXXX              # через запятую Telegram ID админов
DATABASE_URL=postgresql://...     # из Neon Tech
```

### 3. База данных Neon Tech

1. https://console.neon.tech → New Project → PostgreSQL
2. Скопируйте Connection string (замените `postgres://` на `postgresql://`)
3. Добавьте в переменные окружения Render

### 4. Telegram настройка

1. Создайте бота через @BotFather (если ещё нет)
2. Создайте группу администрации — включите **форумы/топики**
3. Добавьте бота в группу и канал как админа
4. Получите ID группы и канала через @userinfobot

## Локальный запуск

```bash
cd suggestion_bot
cp ../.env.example .env   # создайте .env из примеров
pip install -r requirements.txt
python main.py
```

## Структура файлов

```
suggestion_bot/
├── main.py         # логика бота
├── config.py       # настройки
├── database.py     # работа с PostgreSQL (Neon)
├── requirements.txt
.env                # секреты (не коммитить!)
```
