import os
import re
import uuid
import shutil
import asyncio
import logging
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TAG = "via @Doownloderbot"

# একসাথে কতজন ইউজারের ডাউনলোড হ্যান্ডেল করবে
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "10"))
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# =========================
# HELPERS
# =========================
def _is_url(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-\.\(\)\[\]\s]", "_", name, flags=re.UNICODE)
    return name.strip()[:120] if name else "file"


def _find_first_file(folder: Path, exts: tuple[str, ...]) -> Path | None:
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            return p
    return None


def ydl_download_video(url: str, outtmpl: str) -> dict:
    """
    ভিডিও + অডিও (merged) ডাউনলোড করবে (best) এবং mp4 এ merge করার চেষ্টা করবে।
    """
    ydl_opts = {
        "format": "bv*+ba/best",          # best video+audio
        "outtmpl": outtmpl,              # e.g. /path/video.%(ext)s
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,  # speed boost (HLS/DASH এ কাজে লাগে)
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "http_chunk_size": 10 * 1024 * 1024,  # 10MB chunks (কখনো কখনো speed বাড়ায়)
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info


def ydl_download_audio_only(url: str, outtmpl: str) -> dict:
    """
    শুধু অডিও (best audio) ডাউনলোড করবে।
    Telegram এ m4a/webm/opus সবই সাধারণত send_audio দিয়ে যায়।
    """
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,              # e.g. /path/audio.%(ext)s
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "http_chunk_size": 10 * 1024 * 1024,
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info


async def _cleanup_dir(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# =========================
# HANDLERS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    first = (update.effective_user.first_name or "").strip() or "User"
    await update.message.reply_text(f'Hello {first}, send video link')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if not _is_url(text):
        await update.message.reply_text("Please send a valid video link.")
        return

    # কোনো অপশন দেখাবে না—সরাসরি কাজ শুরু (কিন্তু কোনো “downloading…” মেসেজও দিবে না)
    # ব্যাকগ্রাউন্ডে কাজ করবে যাতে একসাথে অনেক ইউজার হ্যান্ডেল হয়
    context.application.create_task(process_url(update, context, text))


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    chat_id = update.effective_chat.id

    # প্রতিটি কাজের জন্য ইউনিক ফোল্ডার (একই নামের ফাইল overwrite/duplicate সমস্যা কমে)
    job_id = uuid.uuid4().hex[:16]
    job_dir = DOWNLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path: Path | None = None
    audio_path: Path | None = None

    try:
        async with job_semaphore:
            # 1) VIDEO download
            video_out = str(job_dir / "video.%(ext)s")
            info = await asyncio.to_thread(ydl_download_video, url, video_out)

            # ভিডিও ফাইল খুঁজে বের করা (mp4/mkv/webm যাই হোক)
            video_path = _find_first_file(job_dir, (".mp4", ".mkv", ".webm", ".mov"))
            if not video_path or not video_path.exists():
                await context.bot.send_message(chat_id=chat_id, text="Download failed. Please try another link.")
                return

            # 2) AUDIO download (separate)
            audio_out = str(job_dir / "audio.%(ext)s")
            await asyncio.to_thread(ydl_download_audio_only, url, audio_out)

            audio_path = _find_first_file(job_dir, (".m4a", ".mp3", ".aac", ".opus", ".ogg", ".webm"))
            # audio_path না পেলেও ভিডিওটা অন্তত পাঠাবে (fail-safe)
            # তবে তুমি “অডিও অবশ্যই লাগবে” চাইলে এখানে fail করাতে পারো

            title = _safe_filename((info.get("title") or "video").strip())

            # Uploading status শুধু “typing/uploading” জায়গায় দেখাবে
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

            # VIDEO send
            with open(video_path, "rb") as vf:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=f"{BOT_TAG}",
                    supports_streaming=True,
                )

            # AUDIO send (separate)
            if audio_path and audio_path.exists():
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_AUDIO)
                with open(audio_path, "rb") as af:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=af,
                        title=title,
                        caption=f"{BOT_TAG}",
                    )

    except Exception as e:
        logging.exception("Job failed: %s", e)
        try:
            await context.bot.send_message(chat_id=chat_id, text="Download failed. Please try another link.")
        except Exception:
            pass
    finally:
        await _cleanup_dir(job_dir)


# =========================
# MAIN (Render compatible)
# =========================
async def main():
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise SystemExit("BOT_TOKEN not set")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())