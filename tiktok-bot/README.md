# TikTok Downloader Bot

Телеграм бот для скачивания TikTok видео без водяного знака.

## Быстрый старт

### 1. Получи токен бота
- Открой [@BotFather](https://t.me/BotFather) в Telegram
- `/newbot` → введи имя и username бота
- Скопируй токен

### 2. Задеплой на Railway

1. Залей этот код на GitHub (новый репозиторий)
2. Зайди на [railway.app](https://railway.app)
3. **New Project** → **Deploy from GitHub repo** → выбери репо
4. Зайди в **Variables** и добавь:
   ```
   BOT_TOKEN = твой_токен_от_BotFather
   ```
5. Railway сам запустит бота через `Procfile`

### 3. Готово!

Бот работает 24/7. Отправь ему любую ссылку на TikTok.

## Локальный запуск (для теста)

```bash
pip install -r requirements.txt
BOT_TOKEN=твой_токен python bot.py
```

## Структура

```
bot.py           — основной код бота
requirements.txt — зависимости
Procfile         — команда запуска для Railway
runtime.txt      — версия Python
```

## Функции

- ✅ Скачивание без водяного знака через yt-dlp
- ✅ Поддержка коротких ссылок (vm.tiktok.com, vt.tiktok.com)
- ✅ Антиспам (3 запроса в минуту на пользователя)
- ✅ Проверка размера файла (лимит Telegram 50 МБ)
- ✅ Понятные сообщения об ошибках
