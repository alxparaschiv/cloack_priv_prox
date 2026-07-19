"""One-time Facebook SMS verification — a SHORT-TERM (single-use) number for
verifying a Facebook Page's phone. This is DISTINCT from /rental_facebook (a 7-day
rental): here you get ONE temporary number (~$0.75), Facebook texts a single code,
the bot fetches it automatically, and the number is done. Command: /fb_page_verify.
"""
import asyncio
import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import textverified_client as tvc

logger = logging.getLogger('page_verify')

POLL_TIMEOUT = int(os.getenv('PAGE_VERIFY_TIMEOUT', '300'))   # seconds to wait for the SMS


async def _do_page_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Acquire a single-use FB verification number, show it, then poll + return the code."""
    msg = await update.message.reply_text(
        "📱 Getting a one-time <b>Facebook Page verification</b> number "
        "(single-use — <i>not</i> a 7-day rental)…", parse_mode='HTML')
    loop = asyncio.get_event_loop()
    try:
        v = await loop.run_in_executor(
            None, lambda: tvc._client().create_verification("facebook", "sms"))
    except Exception as e:
        logger.warning(f"[page_verify] create failed: {e}")
        await msg.edit_text(f"❌ Couldn't get a verification number: {e}")
        return
    number = (v or {}).get('number')
    vid = (v or {}).get('id')
    if not number or not vid:
        await msg.edit_text("❌ TextVerified didn't return a usable number — try again.")
        return
    await msg.edit_text(
        f"📱 <b>FB Page verification number</b> (single-use)\n\n"
        f"<code>{number}</code>\n\n"
        f"1️⃣ Enter this number on the Facebook <b>Page phone-verification</b> screen.\n"
        f"2️⃣ Facebook texts a code to it — I grab it automatically.\n\n"
        f"⏳ Waiting for the code (up to {POLL_TIMEOUT // 60} min)…", parse_mode='HTML')
    code = await loop.run_in_executor(
        None, lambda: tvc._client().poll_sms(vid, timeout=POLL_TIMEOUT))
    if code:
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ <b>Verification code:</b> <code>{code}</code>\n<i>(number {number})</i>",
            parse_mode='HTML')
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            "⏳ No code arrived in time. Make sure you entered the number on Facebook, "
            "then run /fb_page_verify again for a fresh number.")


async def fb_page_verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_page_verify(update, context)
