import logging
import os
import re
import time
import tempfile
from collections import defaultdict

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

# ── Антиспам ─────────────────────────────────────────────────────────────────
user_requests: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < RATE_WINDOW]
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return True
    user_requests[user_id].append(now)
    return False

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

# ── Скачивание ────────────────────────────────────────────────────────────────
YDL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def extract_info(url: str) -> dict:
    """Получает информацию о посте без скачивания."""
    ydl_opts = {"quiet": True, "no_warnings": True, "http_headers": YDL_HEADERS}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def is_slideshow(info: dict) -> bool:
    """Определяет является ли пост слайдшоу (фото карусель)."""
    # Слайдшоу — когда нет видео формата, только изображения
    formats = info.get("formats", [])
    images = [f for f in formats if f.get("ext") in ("jpg", "jpeg", "png", "webp")]
    videos = [f for f in formats if f.get("vcodec") not in (None, "none")]
    return len(images) > 0 and len(videos) == 0

def get_image_urls(info: dict) -> list[str]:
    """Возвращает список URL изображений из слайдшоу."""
    formats = info.get("formats", [])
    # Берём только изображения
    images = [f for f in formats if f.get("ext") in ("jpg", "jpeg", "png", "webp") and f.get("url")]
    # Убираем дубли по URL
    seen = set()
    unique = []
    for f in images:
        u = f["url"]
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique

def download_video(url: str, output_path: str) -> str:
    """Скачивает видео без водяного знака."""
    ydl_opts = {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
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

def get_direct_url(info: dict) -> str:
    """Возвращает прямую ссылку на видео."""
    formats = info.get("formats", [])
    for f in reversed(formats):
        if f.get("ext") == "mp4" and f.get("url"):
            return f["url"]
    return info.get("url") or formats[-1]["url"]

# ── Хэндлеры ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я скачиваю из TikTok:\n\n"
        "🎬 Видео — без водяного знака\n"
        "🖼 Фото карусели — все фото сразу\n\n"
        "Просто отправь ссылку!"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Как пользоваться:*\n\n"
        "1. Открой TikTok и найди нужное видео или фото\n"
        "2. Нажми «Поделиться» → «Скопировать ссылку»\n"
        "3. Отправь ссылку сюда\n\n"
        "Поддерживаемые форматы ссылок:\n"
        "• `https://www.tiktok.com/@user/video/123`\n"
        "• `https://vm.tiktok.com/abc123`\n"
        "• `https://vt.tiktok.com/abc123`",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""

    match = TIKTOK_RE.search(text)
    if not match:
        await update.message.reply_text(
            "🤔 Не вижу ссылки на TikTok.\n"
            "Отправь ссылку вида: https://vm.tiktok.com/..."
        )
        return

    if not await is_subscribed(user.id, ctx):
        await update.message.reply_text(
            "📢 Чтобы пользоваться ботом, подпишись на наш канал!\n\n"
            "После подписки нажми кнопку «Я подписался» ✅",
            reply_markup=subscription_keyboard(),
        )
        ctx.user_data["pending_url"] = match.group(0)
        return

    await process_url(update, ctx, match.group(0))

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

    await query.edit_message_text("✅ Спасибо за подписку! Теперь отправь ссылку на TikTok.")

    pending_url = ctx.user_data.pop("pending_url", None)
    if pending_url:
        await process_url(update, ctx, pending_url)

async def process_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    user = update.effective_user

    if is_rate_limited(user.id):
        msg = update.message or update.callback_query.message
        await msg.reply_text("⏳ Слишком много запросов. Подожди немного.")
        return

    msg = update.message or update.callback_query.message
    status_msg = await msg.reply_text("⏳ Обрабатываю ссылку...")
    logger.info(f"User {user.id} (@{user.username}) запросил: {url}")

    try:
        info = extract_info(url)
        title = info.get("title", "")

        # ── Слайдшоу (фото карусель) ──────────────────────────────────────────
        if is_slideshow(info):
            image_urls = get_image_urls(info)
            if not image_urls:
                await status_msg.edit_text("❌ Не удалось получить фото из этого поста.")
                return

            await status_msg.edit_text(f"🖼 Отправляю {len(image_urls)} фото...")

            # Telegram принимает медиагруппы до 10 штук
            media_group = []
            for i, img_url in enumerate(image_urls[:10]):
                caption = f"🖼 {len(image_urls)} фото • @asiadvizh" if i == 0 else None
                media_group.append(InputMediaPhoto(media=img_url, caption=caption))

            await msg.reply_media_group(media=media_group)
            await status_msg.delete()

        # ── Видео ─────────────────────────────────────────────────────────────
        else:
            await status_msg.edit_text("⏬ Скачиваю видео...")
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, "video.%(ext)s")
                filepath = download_video(url, output_path)
                size_mb = os.path.getsize(filepath) / (1024 * 1024)

                if size_mb <= 50:
                    await status_msg.edit_text("📤 Отправляю...")
                    with open(filepath, "rb") as video_file:
                        await msg.reply_video(
                            video=video_file,
                            caption="✅ Без водяного знака",
                            supports_streaming=True,
                        )
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("🔗 Видео большое, получаю ссылку...")
                    try:
                        direct_url = get_direct_url(info)
                        await msg.reply_text(
                            f"📹 Видео слишком большое ({size_mb:.1f} МБ) для Telegram.\n"
                            f"Скачай по прямой ссылке:\n{direct_url}",
                        )
                        await status_msg.delete()
                    except Exception:
                        await status_msg.edit_text(
                            f"😔 Видео слишком большое ({size_mb:.1f} МБ).\n"
                            f"Попробуй скачать через браузер."
                        )

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"DownloadError для {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать.\n\n"
            "Возможные причины:\n"
            "• Видео/фото удалено или приватное\n"
            "• Ссылка устарела\n"
            "• TikTok заблокировал запрос\n\n"
            "Попробуй скопировать ссылку заново."
        )
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Что-то пошло не так. Попробуй позже.")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_check_sub, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
