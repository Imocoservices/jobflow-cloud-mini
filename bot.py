# bot.py — JobFlow AI Telegram bot (PTB 20.6)
# - Groups messages into sessions by Telegram chat within 10 minutes
# - Saves photos/audio to output/sessions/<session_id>
# - Transcribes audio with Whisper (OpenAI)
# - Generates AI quote suggestion and patches Flask at /api/sessions/<id>
# Windows-friendly; reads .env from C:\Users\Joeyv\relationshipbot\.env

from __future__ import annotations

import os
import io
import time
import json
import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# ---------- Env loading (your .env lives in relationshipbot) ----------
REL_ENV = Path("C:/Users/Joeyv/relationshipbot/.env")
if REL_ENV.exists():
    load_dotenv(REL_ENV)
else:
    load_dotenv()  # fallback to local if you ever move .env

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5065")
TIMEBOX_MIN = int(os.getenv("SESSION_MERGE_MINUTES", "10"))

# ---------- Paths ----------
ROOT = Path(__file__).parent.resolve()
OUTPUT = ROOT / "output"
SESS_ROOT = OUTPUT / "sessions"
SESS_ROOT.mkdir(parents=True, exist_ok=True)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jobflow.bot")

# ---------- OpenAI clients ----------
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in C:/Users/Joeyv/relationshipbot/.env")

oai = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Local utils ----------
from utils.session_store import ensure_session_folder, load_report, save_report
from utils.ai_quote import generate_quote


# -------------------- Helper functions --------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _post_json(url: str, payload: Dict[str, Any], retries: int = 3, timeout: int = 10) -> Optional[Dict[str, Any]]:
    last_err = None
    for i in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.ok:
                return r.json()
            last_err = f"{r.status_code} {r.text}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4 * (i + 1))
    log.warning("POST %s failed after %d retries: %s", url, retries, last_err)
    return None


def find_or_create_session_id(chat_id: int) -> str:
    """
    Return an existing session id for this chat if updated within TIMEBOX_MIN,
    otherwise create a new one.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TIMEBOX_MIN)
    candidate_id = None
    candidate_dt = None

    for p in SESS_ROOT.iterdir():
        if not p.is_dir():
            continue
        rp = p / "report.json"
        if not rp.exists():
            continue
        s = _read_json(rp)
        if str(s.get("telegram_chat_id", "")) != str(chat_id):
            continue
        upd = s.get("updated_at")
        try:
            dt = datetime.fromisoformat(upd) if upd else None
        except Exception:
            dt = None
        if dt and dt >= cutoff and (candidate_dt is None or dt > candidate_dt):
            candidate_dt = dt
            candidate_id = s.get("id")

    if candidate_id:
        return candidate_id

    # Create a fresh session
    sid = datetime.now().strftime("tg_%Y%m%d_%H%M%S")
    sp = ensure_session_folder(SESS_ROOT, sid)
    data = load_report(sp)
    data["id"] = sid
    data["telegram_chat_id"] = str(chat_id)
    save_report(sp, data)
    log.info("Created new session %s for chat %s", sid, chat_id)
    return sid


# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "JobFlow AI bot is ready. Send photos and a voice note within 10 minutes — "
        "I’ll group them into a session and draft your quote."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return
    chat_id = msg.chat_id
    session_id = find_or_create_session_id(chat_id)
    sp = ensure_session_folder(SESS_ROOT, session_id)

    # largest size
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    fname = f"photo_{int(time.time())}.jpg"
    fpath = sp / fname
    await tg_file.download_to_drive(custom_path=str(fpath))

    # Patch media into session
    url = f"{PUBLIC_BASE_URL}/api/sessions/{session_id}"
    payload = {"media": {"photos": [fname]}}
    _post_json(url, payload)

    log.info("Photo saved %s -> session %s", fname, session_id)
    await msg.reply_text(f"📷 Added photo to session {session_id}")


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or (not msg.voice and not msg.audio):
        return
    chat_id = msg.chat_id
    session_id = find_or_create_session_id(chat_id)
    sp = ensure_session_folder(SESS_ROOT, session_id)

    # Download audio
    tg_file = await context.bot.get_file((msg.voice or msg.audio).file_id)
    afname = f"audio_{int(time.time())}.ogg"
    apath = sp / afname
    await tg_file.download_to_drive(custom_path=str(apath))
    log.info("Audio saved %s -> session %s", afname, session_id)

    # Transcribe with Whisper
    transcript_text = ""
    try:
        with open(apath, "rb") as fh:
            tr = oai.audio.transcriptions.create(
                model="whisper-1",
                file=fh,
                response_format="text",
            )
        transcript_text = (tr or "").strip()
    except Exception as e:
        transcript_text = f"(transcription error: {e})"
        log.exception("Whisper transcription failed")

    # Append transcript + add audio media
    current = load_report(sp)
    merged_transcript = (current.get("transcript", "") + "\n" + transcript_text).strip()
    patch_url = f"{PUBLIC_BASE_URL}/api/sessions/{session_id}"
    _post_json(patch_url, {"transcript": merged_transcript, "media": {"audio": [afname]}})

    # Generate AI quote suggestion from transcript
    try:
        quoted = generate_quote(transcript_text, "")
        estimate = {
            "suggested_price": quoted.get("suggested_price"),
            "quote_suggested": quoted.get("quote_suggested", []),
        }
        patch = {
            "summary": quoted.get("summary", ""),
            "tasks": quoted.get("tasks", []),
            "materials": quoted.get("materials", []),
            "entities": quoted.get("entities", {}),
            "estimate": estimate,
        }
        _post_json(patch_url, patch)
        log.info("AI quote suggestion saved for session %s (suggested $%s)",
                 session_id, estimate.get("suggested_price"))
    except Exception as e:
        log.exception("AI quote generation failed")
        _post_json(patch_url, {"summary": f"AI quote error: {e}"})

    await msg.reply_text(
        f"🎙️ Transcribed & analyzed.\nSession: {session_id}\nOpen http://127.0.0.1:5065/admin to review."
    )


# -------------------- Entry --------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in C:/Users/Joeyv/relationshipbot/.env")

    log.info("Starting JobFlow AI Telegram bot…")
    log.info("Sessions dir: %s", SESS_ROOT)
    log.info("Dashboard base URL: %s", PUBLIC_BASE_URL)
    log.info("Session merge window: %s minutes", TIMEBOX_MIN)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))

    log.info("Polling started. Send /start, photo, and a voice note.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
