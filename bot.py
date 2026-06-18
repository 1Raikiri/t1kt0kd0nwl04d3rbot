import asyncio
import logging
import os
import re
import time
import tempfile
import urllib.request
from collections import defaultdict

import aiohttp
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = "@nasvaivolne"

RATE_LIMIT = 3
RATE_WINDOW = 60

TIKTOK_RE = re.compile(
    r"https?://(www\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

INSTAGRAM_RE = re.compile(
    r"https?://(www\.)?instagram\.com/(reel|reels|p|tv|stories)/\S+",
    re.IGNORECASE,
)

# Cookies для Instagram можно передать двумя способами:
# 1. INSTAGRAM_COOKIES_CONTENT — содержимое cookies.txt прямо в переменной окружения
# 2. INSTAGRAM_COOKIES_FILE — путь к файлу с cookies на диске
# Ниже — устойчивая к человеческим ошибкам логика: если в любой из двух
# переменных лежит содержимое файла (а не путь), мы это определяем
# и сами сохраняем во временный файл.
def _resolve_instagram_cookies() -> str | None:
    raw_content = os.environ.get("INSTAGRAM_COOKIES_CONTENT")
    raw_file = os.environ.get("INSTAGRAM_COOKIES_FILE")

    candidates = [raw_content, raw_file]
    for value in candidates:
        if not value:
            continue
        # Если значение выглядит как содержимое cookies-файла (есть символы
        # новой строки или начинается с "# Netscape"), а не как путь к файлу —
        # сохраняем его во временный файл.
        looks_like_content = "\n" in value or value.strip().startswith("#")
        if looks_like_content:
            cookies_path = os.path.join(tempfile.gettempdir(), "instagram_cookies.txt")
            with open(cookies_path, "w", encoding="utf-8") as f:
                f.write(value)
            logger.info("Instagram cookies: содержимое сохранено во временный файл")
            return cookies_path
        # Иначе это похоже на путь к существующему файлу
        if os.path.exists(value):
            logger.info(f"Instagram cookies: используется файл по пути {value}")
            return value
        logger.warning(f"Instagram cookies: значение '{value[:50]}...' — это не существующий путь и не похоже на содержимое cookies")

    logger.info("Instagram cookies не настроены (ни CONTENT, ни FILE)")
    return None

INSTAGRAM_COOKIES_FILE = _resolve_instagram_cookies()

TIKWM_API = "https://www.tikwm.com/api/"
TIKWM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.tikwm.com",
    "Referer": "https://www.tikwm.com/",
}

YDL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ── Антиспам ─────────────────────────────────────────────────────────────────
user_requests: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < RATE_WINDOW]
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return True
    user_requests[user_id].append(now)
    # Удаляем пустые записи, чтобы словарь не рос бесконечно
    if not user_requests[user_id]:
        del user_requests[user_id]
    return False

def cleanup_rate_limits():
    """Удаляет устаревшие записи из user_requests. Вызывается по расписанию."""
    now = time.time()
    stale = [uid for uid, times in user_requests.items() if not any(now - t < RATE_WINDOW for t in times)]
    for uid in stale:
        del user_requests[uid]
    if stale:
        logger.info(f"cleanup_rate_limits: удалено {len(stale)} устаревших записей")

# ── Проверка подписки ─────────────────────────────────────────────────────────
async def is_subscribed(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await ctx.bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/nasvaivolne")],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ])

# ── tikwm API (видео + фото) ──────────────────────────────────────────────────
async def tikwm_fetch(url: str) -> dict | None:
    """Получает данные через tikwm.com API. Возвращает data dict или None."""
    try:
        async with aiohttp.ClientSession(headers=TIKWM_HEADERS) as session:
            async with session.post(
                TIKWM_API,
                data={"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"tikwm вернул {resp.status}")
                    return None
                j = await resp.json()
                if j.get("code") != 0:
                    logger.warning(f"tikwm error: {j.get('msg')}")
                    return None
                return j.get("data")
    except Exception as e:
        logger.warning(f"tikwm exception: {e}")
        return None

def detect_url(text: str) -> tuple[str | None, str | None]:
    """Определяет платформу и ссылку в тексте сообщения."""
    match = TIKTOK_RE.search(text)
    if match:
        return "tiktok", match.group(0)
    match = INSTAGRAM_RE.search(text)
    if match:
        return "instagram", match.group(0)
    return None, None

# ── yt-dlp скачивание видео/фото ──────────────────────────────────────────────
def download_instagram_ytdlp(url: str, tmpdir: str) -> dict:
    """
    Скачивает контент из Instagram через yt-dlp.
    Возвращает dict: {"type": "video", "path": str} или
                      {"type": "images", "paths": [str, ...]}
    Поднимает yt_dlp.utils.DownloadError при неудаче.
    """
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HEADERS,
    }
    if INSTAGRAM_COOKIES_FILE:
        base_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE

    # ── Этап 1: разведка без скачивания, чтобы понять что внутри (видео/фото/карусель) ──
    probe_opts = {**base_opts, "skip_download": True}
    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    entries = [e for e in entries if e is not None]

    if not entries:
        raise yt_dlp.utils.DownloadError("Не найдено ни видео, ни фото в результате")

    video_paths = []
    image_paths = []

    for idx, entry in enumerate(entries):
        # Фото в Instagram отдаётся без видеоформатов (formats пустой или только image)
        formats = entry.get("formats") or []
        has_video_format = any(f.get("vcodec") not in (None, "none") for f in formats)
        is_image_entry = entry.get("vcodec") == "none" or not has_video_format

        if is_image_entry:
            # Прямая ссылка на изображение
            img_url = entry.get("url") or (formats[-1].get("url") if formats else None)
            if not img_url:
                continue
            ext = entry.get("ext") or "jpg"
            dest = os.path.join(tmpdir, f"photo_{idx}.{ext}")
            # Скачиваем напрямую — это просто статичный файл изображения
            req = urllib.request.Request(img_url, headers=YDL_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as f:
                f.write(resp.read())
            image_paths.append(dest)
        else:
            # Видео — скачиваем через yt-dlp с обычным выбором формата
            entry_url = entry.get("webpage_url") or entry.get("url") or url
            video_opts = {
                **base_opts,
                "outtmpl": os.path.join(tmpdir, f"video_{idx}.%(ext)s"),
                "format": "best[ext=mp4]/best",
            }
            with yt_dlp.YoutubeDL(video_opts) as ydl_dl:
                v_info = ydl_dl.extract_info(entry_url, download=True)
                filename = ydl_dl.prepare_filename(v_info)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                if os.path.exists(filename):
                    video_paths.append(filename)

    if image_paths and not video_paths:
        return {"type": "images", "paths": image_paths}
    if video_paths:
        return {"type": "video", "path": video_paths[0]}

    raise yt_dlp.utils.DownloadError("Не найдено ни видео, ни фото в результате")

def download_video_ytdlp(url: str, output_path: str) -> str:
    """Простое скачивание видео через yt-dlp — используется как fallback для TikTok."""
    ydl_opts = {
        "outtmpl": output_path,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HEADERS,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            filename = filename.rsplit(".", 1)[0] + ".mp4"
        return filename

# ── Хэндлеры ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я скачиваю видео и фото:\n\n"
        "🎵 TikTok — видео и фото-карусели без водяного знака\n"
        "📸 Instagram — Reels и посты\n\n"
        "Просто отправь ссылку!"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Как пользоваться:*\n\n"
        "*TikTok:*\n"
        "1. Открой TikTok и найди нужное видео или фото\n"
        "2. Нажми «Поделиться» → «Скопировать ссылку»\n"
        "3. Отправь ссылку сюда\n\n"
        "*Instagram:*\n"
        "1. Открой Reels или пост\n"
        "2. Нажми «Поделиться» → «Скопировать ссылку»\n"
        "3. Отправь ссылку сюда",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""

    platform, url = detect_url(text)
    if not url:
        await update.message.reply_text(
            "🤔 Не вижу ссылки на TikTok или Instagram.\n"
            "Отправь ссылку вида: https://vm.tiktok.com/... "
            "или https://instagram.com/reel/..."
        )
        return

    if not await is_subscribed(user.id, ctx):
        await update.message.reply_text(
            "📢 Чтобы пользоваться ботом, подпишись на наш канал!\n\n"
            "После подписки нажми кнопку «Я подписался» ✅",
            reply_markup=subscription_keyboard(),
        )
        ctx.user_data["pending_url"] = url
        ctx.user_data["pending_platform"] = platform
        return

    await process_url(update, ctx, url, platform)

async def handle_check_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if not await is_subscribed(user.id, ctx):
        await query.edit_message_text(
            "❌ Ты ещё не подписался на канал.\n\n"
            "Подпишись и нажми кнопку снова 👇",
            reply_markup=subscription_keyboard(),
        )
        return

    await query.edit_message_text("✅ Спасибо за подписку! Обрабатываю твою ссылку...")

    pending_url = ctx.user_data.pop("pending_url", None)
    pending_platform = ctx.user_data.pop("pending_platform", None)
    if pending_url:
        await process_url(update, ctx, pending_url, pending_platform)

async def process_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str, platform: str = "tiktok"):
    user = update.effective_user

    if is_rate_limited(user.id):
        msg = update.message or update.callback_query.message
        await msg.reply_text("⏳ Слишком много запросов. Подожди немного.")
        return

    msg = update.message or update.callback_query.message
    status_msg = await msg.reply_text("⏳ Обрабатываю ссылку...")
    logger.info(f"User {user.id} (@{user.username}) запросил [{platform}]: {url}")

    try:
        # ── TikTok: сначала пробуем tikwm API (видео + фото-карусели) ────────
        if platform == "tiktok":
            data = await tikwm_fetch(url)

            if data:
                images = data.get("images")

                # ── Фото карусель ─────────────────────────────────────────
                if images:
                    await status_msg.edit_text(f"🖼 Отправляю {len(images)} фото...")
                    media_group = []
                    async with aiohttp.ClientSession(headers=TIKWM_HEADERS) as session:
                        for i, img_url in enumerate(images[:10]):
                            async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=15)) as img_resp:
                                img_bytes = await img_resp.read()
                            caption = "🖼 Без водяного знака" if i == 0 else None
                            media_group.append(InputMediaPhoto(media=img_bytes, caption=caption))
                    await msg.reply_media_group(media=media_group)
                    await status_msg.delete()
                    return

                # ── Видео через tikwm ────────────────────────────────────
                play_url = data.get("hdplay") or data.get("play")
                if play_url:
                    await status_msg.edit_text("⏬ Скачиваю видео...")
                    async with aiohttp.ClientSession(headers=TIKWM_HEADERS) as session:
                        async with session.get(play_url, timeout=aiohttp.ClientTimeout(total=60)) as video_resp:
                            content = await video_resp.read()
                    size_mb = len(content) / (1024 * 1024)

                    if size_mb <= 50:
                        await status_msg.edit_text("📤 Отправляю...")
                        await msg.reply_video(
                            video=content,
                            caption="✅ Без водяного знака",
                            supports_streaming=True,
                        )
                        await status_msg.delete()
                    else:
                        await msg.reply_text(
                            f"📹 Видео слишком большое ({size_mb:.1f} МБ).\n"
                            f"Скачай по ссылке:\n{play_url}"
                        )
                        await status_msg.delete()
                    return

        # ── Instagram и fallback для TikTok: yt-dlp ──────────────────────────
        await status_msg.edit_text("⏬ Скачиваю...")
        with tempfile.TemporaryDirectory() as tmpdir:
            if platform == "instagram":
                result = download_instagram_ytdlp(url, tmpdir)

                if result["type"] == "images":
                    paths = result["paths"]
                    await status_msg.edit_text(f"🖼 Отправляю {len(paths)} фото...")
                    media_group = []
                    for i, path in enumerate(paths[:10]):
                        with open(path, "rb") as f:
                            caption = "✅ Готово" if i == 0 else None
                            media_group.append(InputMediaPhoto(media=f.read(), caption=caption))
                    await msg.reply_media_group(media=media_group)
                    await status_msg.delete()
                    return

                filepath = result["path"]
            else:
                output_path = os.path.join(tmpdir, "video.%(ext)s")
                filepath = download_video_ytdlp(url, output_path)

            size_mb = os.path.getsize(filepath) / (1024 * 1024)

            if size_mb <= 50:
                await status_msg.edit_text("📤 Отправляю...")
                with open(filepath, "rb") as f:
                    await msg.reply_video(video=f, caption="✅ Готово", supports_streaming=True)
                await status_msg.delete()
            else:
                await status_msg.edit_text(f"😔 Видео слишком большое ({size_mb:.1f} МБ) для Telegram.")

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"DownloadError для {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать.\n\n"
            "Возможные причины:\n"
            "• Видео/фото удалено, приватное или ограничено\n"
            "• Ссылка устарела\n\n"
            "Попробуй скопировать ссылку заново."
        )
    except Exception as e:
        logger.exception(f"Неожиданная ошибка: {e}")
        await status_msg.edit_text("⚠️ Что-то пошло не так. Попробуй позже.")

# ── Запуск ────────────────────────────────────────────────────────────────────
async def cleanup_loop():
    """Фоновая задача: чистит user_requests каждые 10 минут."""
    while True:
        await asyncio.sleep(600)
        cleanup_rate_limits()

async def on_startup(app):
    asyncio.create_task(cleanup_loop())

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_check_sub, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
