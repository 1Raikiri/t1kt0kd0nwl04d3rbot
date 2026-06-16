import logging
import os
import re
import time
import tempfile
from collections import defaultdict

import requests
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

# ── tikwm API (видео + фото) ──────────────────────────────────────────────────
def tikwm_fetch(url: str) -> dict | None:
    """Получает данные через tikwm.com API. Возвращает data dict или None."""
    try:
        resp = requests.post(
            TIKWM_API,
            data={"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1},
            headers=TIKWM_HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning(f"tikwm вернул {resp.status_code}")
            return None
        j = resp.json()
        if j.get("code") != 0:
            logger.warning(f"tikwm error: {j.get('msg')}")
            return None
        return j.get("data")
    except Exception as e:
        logger.warning(f"tikwm exception: {e}")
        return None

# ── yt-dlp скачивание видео ───────────────────────────────────────────────────
def download_video_ytdlp(url: str, output_path: str) -> str:
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
        "3. Отправь ссылку сюда",
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
        # ── Пробуем tikwm API (работает и для фото и для видео) ──────────────
        data = tikwm_fetch(url)

        if data:
            images = data.get("images")  # список URL фото если это карусель

            # ── Фото карусель ─────────────────────────────────────────────────
            if images:
                await status_msg.edit_text(f"🖼 Отправляю {len(images)} фото...")
                media_group = []
                for i, img_url in enumerate(images[:10]):
                    img_resp = requests.get(img_url, headers=TIKWM_HEADERS, timeout=15)
                    caption = "🖼 Без водяного знака" if i == 0 else None
                    media_group.append(InputMediaPhoto(media=img_resp.content, caption=caption))
                await msg.reply_media_group(media=media_group)
                await status_msg.delete()
                return

            # ── Видео через tikwm ─────────────────────────────────────────────
            play_url = data.get("hdplay") or data.get("play")
            if play_url:
                await status_msg.edit_text("⏬ Скачиваю видео...")
                video_resp = requests.get(play_url, headers=TIKWM_HEADERS, timeout=60, stream=True)
                content = video_resp.content
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

        # ── Fallback: yt-dlp для видео ────────────────────────────────────────
        await status_msg.edit_text("⏬ Скачиваю через резервный метод...")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "video.%(ext)s")
            filepath = download_video_ytdlp(url, output_path)
            size_mb = os.path.getsize(filepath) / (1024 * 1024)

            if size_mb <= 50:
                await status_msg.edit_text("📤 Отправляю...")
                with open(filepath, "rb") as f:
                    await msg.reply_video(video=f, caption="✅ Без водяного знака", supports_streaming=True)
                await status_msg.delete()
            else:
                await status_msg.edit_text(f"😔 Видео слишком большое ({size_mb:.1f} МБ) для Telegram.")

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"DownloadError для {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать.\n\n"
            "Возможные причины:\n"
            "• Видео/фото удалено или приватное\n"
            "• Ссылка устарела\n\n"
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
