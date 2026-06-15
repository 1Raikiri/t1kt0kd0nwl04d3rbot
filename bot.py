import logging
import os
import re
import time
import tempfile
from collections import defaultdict

import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
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

# Антиспам: не больше N запросов в X секунд на пользователя
RATE_LIMIT = 3        # запросов
RATE_WINDOW = 60      # секунд

# Паттерн TikTok ссылок
TIKTOK_RE = re.compile(
    r"https?://(www\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

# ── Антиспам ─────────────────────────────────────────────────────────────────
user_requests: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    timestamps = user_requests[user_id]
    # Удаляем старые
    user_requests[user_id] = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return True
    user_requests[user_id].append(now)
    return False

# ── Скачивание ────────────────────────────────────────────────────────────────
YDL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def get_direct_url(url: str) -> tuple[str, str]:
    """Возвращает (прямая_ссылка, заголовок) без скачивания."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HEADERS,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", "TikTok видео")
        # Ищем формат без водяного знака
        formats = info.get("formats", [])
        # Приоритет: mp4 без watermark, потом лучший доступный
        best_url = None
        for f in reversed(formats):
            if f.get("ext") == "mp4" and f.get("url"):
                best_url = f["url"]
                break
        if not best_url:
            best_url = info.get("url") or formats[-1]["url"]
        return best_url, title

def download_tiktok(url: str, output_path: str) -> str:
    """Скачивает видео без водяного знака, возвращает путь к файлу."""
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
        "👋 Привет! Я скачиваю видео из TikTok без водяного знака.\n\n"
        "Просто отправь мне ссылку на видео — и готово 🎬"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Как пользоваться:*\n\n"
        "1. Открой TikTok и найди нужное видео\n"
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

    # Ищем TikTok ссылку в тексте
    match = TIKTOK_RE.search(text)
    if not match:
        await update.message.reply_text(
            "🤔 Не вижу ссылки на TikTok.\n"
            "Отправь ссылку вида: https://vm.tiktok.com/..."
        )
        return

    url = match.group(0)

    # Антиспам
    if is_rate_limited(user.id):
        await update.message.reply_text(
            f"⏳ Слишком много запросов. Подожди немного и попробуй снова."
        )
        return

    status_msg = await update.message.reply_text("⏬ Скачиваю видео...")
    logger.info(f"User {user.id} (@{user.username}) запросил: {url}")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "video.%(ext)s")
            filepath = download_tiktok(url, output_path)

            size_mb = os.path.getsize(filepath) / (1024 * 1024)

            if size_mb <= 50:
                # Отправляем файлом — лучший вариант
                await status_msg.edit_text("📤 Отправляю...")
                with open(filepath, "rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption="✅ Без водяного знака",
                        supports_streaming=True,
                    )
                await status_msg.delete()
            else:
                # Файл большой — отправляем прямую ссылку
                await status_msg.edit_text("🔗 Получаю ссылку...")
                try:
                    direct_url, title = get_direct_url(url)
                    await update.message.reply_text(
                        f"📹 *{title}*\n\n"
                        f"Видео слишком большое ({size_mb:.1f} МБ) для Telegram.\n"
                        f"Скачай по прямой ссылке:\n{direct_url}",
                        parse_mode="Markdown",
                    )
                    await status_msg.delete()
                except Exception:
                    await status_msg.edit_text(
                        f"😔 Видео слишком большое ({size_mb:.1f} МБ) для Telegram "
                        f"и не удалось получить прямую ссылку.\n"
                        f"Попробуй скачать через браузер."
                    )

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"DownloadError для {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать видео.\n\n"
            "Возможные причины:\n"
            "• Видео удалено или приватное\n"
            "• Ссылка устарела\n"
            "• TikTok заблокировал запрос\n\n"
            "Попробуй скопировать ссылку заново."
        )
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}", exc_info=True)
        await status_msg.edit_text(
            "⚠️ Что-то пошло не так. Попробуй позже."
        )

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
