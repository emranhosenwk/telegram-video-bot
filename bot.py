import os
import re
import uuid
import asyncio
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================
BOT_TAG = "via @Doownloderbot"

# একসাথে কয়টা ডাউনলোড চলবে
MAX_CONCURRENT_JOBS = 20
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Telegram Bot API-তে অনেক সময় বড় ফাইল গেলে সমস্যা হয়,
# তাই 49MB এর উপর হলে document হিসেবে পাঠাবো (তবুও খুব বড় হলে fail হতে পারে)
SOFT_LIMIT_MB = 49

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


# =========================
# Helpers
# =========================
def _mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


def _find_first_file(folder: Path, exts: set[str]) -> Path | None:
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            return p
    return None


def _ydl_video(url: str, outtmpl: str) -> None:
    # Best video+audio merge করে mp4 করার চেষ্টা
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "retries": 7,
        "fragment_retries": 7,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 8,
        "quiet": True,
        "no_warnings": True,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)


def _ydl_audio_mp3(url: str, outtmpl: str) -> None:
    # Best audio বের করে FFmpeg দিয়ে mp3 বানাবে
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "retries": 7,
        "fragment_retries": 7,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 8,
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)


async def _safe_unlink(p: Path):
    try:
        p.unlink()
    except:
        pass


async def _cleanup_dir(d: Path):
    try:
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    await _safe_unlink(p)
            d.rmdir()
    except:
        pass


# =========================
# Telegram Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = (user.first_name or user.full_name or "User").strip()
    await update.message.reply_text(f'Hello {name} 👋\nSend me your video link.')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    m = URL_RE.search(update.message.text.strip())
    if not m:
        await update.message.reply_text("Please send a valid video link.")
        return

    url = m.group(1)

    # Background-ish: async task (একই সাথে অনেক ইউজার)
    asyncio.create_task(process_url(update, context, url))


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id

    async with job_semaphore:
        # প্রতিটা কাজের জন্য ইউনিক ফোল্ডার (duplicate/overwrite আটকাবে)
        job_id = uuid.uuid4().hex[:20]
        job_dir = DOWNLOADS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        video_path = None
        audio_path = None

        try:
            # 1) VIDEO download
            video_out = str(job_dir / "video.%(ext)s")
            await asyncio.to_thread(_ydl_video, url, video_out)

            # সাধারণত mp4 হবে, না হলে webm/mkv
            video_path = _find_first_file(job_dir, {".mp4", ".mkv", ".webm"})

            if not video_path or not video_path.exists():
                await context.bot.send_message(chat_id=chat_id, text="Download failed. Please try another link.")
                await _cleanup_dir(job_dir)
                return

            # 2) AUDIO download (MP3)
            audio_out = str(job_dir / "audio.%(ext)s")
            audio_ok = True
            try:
                await asyncio.to_thread(_ydl_audio_mp3, url, audio_out)
                audio_path = _find_first_file(job_dir, {".mp3"})
                if not audio_path or not audio_path.exists():
                    audio_ok = False
            except:
                audio_ok = False

            # 3) SEND VIDEO (একবারই)
            caption = BOT_TAG

            v_size = video_path.stat().st_size
            if _mb(v_size) > SOFT_LIMIT_MB:
                # বড় হলে document হিসেবে (video হিসেবে ফেল হতে পারে)
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=open(video_path, "rb"),
                    caption=caption,
                )
            else:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=open(video_path, "rb"),
                    caption=caption,
                )

            # 4) SEND AUDIO (আলাদা)
            if audio_ok and audio_path:
                a_size = audio_path.stat().st_size
                if _mb(a_size) > SOFT_LIMIT_MB:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=open(audio_path, "rb"),
                        caption=caption,
                    )
                else:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=open(audio_path, "rb"),
                        caption=caption,
                    )
            else:
                # অডিও না পারলে ভিডিও পাঠানো থাকবে, শুধু ইনফো মেসেজ
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Video sent ✅\nAudio extract করা যায়নি (কিছু লিংকে আলাদা audio stream থাকে না)।",
                )

        except Exception:
            await context.bot.send_message(chat_id=chat_id, text="Download failed. Please try another link.")
        finally:
            await _cleanup_dir(job_dir)


def main():
    token = "8307565562:AAH8TYqZUQbn9nL0IzFGgcZ_x8t_9wQRZrM"
    if not token:
        raise SystemExit("ERROR: BOT_TOKEN not set. CMD তে আগে BOT_TOKEN সেট করো।")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # long polling
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()