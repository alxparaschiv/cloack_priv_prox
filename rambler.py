"""/rambler — Fetch the latest Meta (Facebook OR Instagram) verification code from a Rambler inbox.

Conversation flow:
  /rambler              → prompt for "<email>:<password>"
  user replies          → IMAP fetch, extract code, reply

Why this exists: during Meta Dev account creation / unlock flows, the user
sometimes needs to grab a fresh FB or IG confirmation code from a Rambler inbox.
Manual login to Rambler in the GoLogin browser is annoying — easier to
just paste creds here and get the code back instantly.

The fetcher walks the most-recent N messages and returns the FIRST one whose
sender is Facebook OR Instagram (Meta family). Reply tags the service so the
user knows which platform the code is for.

Note: cred are NOT persisted. They're held in context.user_data only for
the single fetch + then popped.
"""
import os
import re
import imaplib
import email as _em
import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

IMAP_HOST = 'imap.rambler.ru'
IMAP_PORT = 993
IMAP_TIMEOUT = 15

# Look in the last N emails for the most recent Facebook code
RECENT_N = 30


def _fetch_latest_meta_code(email_addr: str, password: str):
    """Connect to Rambler IMAP, return (code, subject, sender, service, error).

    Walks the last RECENT_N messages newest-first; returns the FIRST one whose
    sender is Facebook OR Instagram with a 4-8 digit code in the subject.
    service = 'Facebook' | 'Instagram' (derived from sender header).
    """
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    except Exception as e:
        return None, None, None, None, f"connect failed: {type(e).__name__}: {e}"
    try:
        m.login(email_addr, password)
    except Exception as e:
        try: m.logout()
        except: pass
        return None, None, None, None, f"login failed (check email/password): {type(e).__name__}: {e}"
    try:
        m.select('INBOX')
        _, ids = m.search(None, 'ALL')
        all_ids = ids[0].split() if ids and ids[0] else []
        for eid in all_ids[::-1][:RECENT_N]:
            _, data = m.fetch(eid, '(RFC822.HEADER)')
            msg = _em.message_from_bytes(data[0][1])
            subj = msg.get('Subject', '') or ''
            frm = msg.get('From', '') or ''
            frm_lower = frm.lower()
            if 'instagram' in frm_lower:
                service = 'Instagram'
            elif 'facebook' in frm_lower:
                service = 'Facebook'
            else:
                continue
            cm = re.search(r'\b(\d{4,8})\b', subj)
            if cm:
                m.logout()
                return cm.group(1), subj, frm, service, None
        m.logout()
        return None, None, None, None, f"no Facebook or Instagram email with a code found in last {RECENT_N} messages"
    except Exception as e:
        try: m.logout()
        except: pass
        return None, None, None, None, f"fetch failed: {type(e).__name__}: {e}"

# back-compat alias — some callers may import the old name
_fetch_latest_fb_code = _fetch_latest_meta_code


async def rambler_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /rambler — prompt for credentials."""
    context.user_data['expecting_rambler_creds'] = True
    await update.message.reply_text(
        "📬 *Rambler code fetcher*\n\n"
        "Send your Rambler credentials in the format:\n"
        "`email@rambler.ru:password`\n\n"
        "I'll fetch the most recent *Facebook OR Instagram* email and reply with the code "
        "from its subject. The reply will tag which service the code is for.\n"
        "Credentials are NOT stored — used for one fetch then dropped.",
        parse_mode='Markdown')


async def rambler_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the text reply with email:password."""
    if not context.user_data.get('expecting_rambler_creds'):
        return
    text = (update.message.text or '').strip()
    context.user_data.pop('expecting_rambler_creds', None)
    # Parse email:password (be permissive about whitespace)
    parts = text.split(':', 1)
    if len(parts) != 2 or '@' not in parts[0]:
        await update.message.reply_text(
            "❌ Format error. Expected `email@rambler.ru:password`. Run /rambler again.",
            parse_mode='Markdown')
        return
    email_addr = parts[0].strip()
    password = parts[1].strip()
    if not email_addr or not password:
        await update.message.reply_text("❌ Empty email or password. Run /rambler again.")
        return

    await update.message.reply_text(f"🔍 Connecting to Rambler IMAP for `{email_addr}`…", parse_mode='Markdown')
    code, subject, sender, service, err = _fetch_latest_meta_code(email_addr, password)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not code:
        await update.message.reply_text(
            f"⚠️ No Facebook or Instagram code found in last {RECENT_N} messages.",
            parse_mode='Markdown')
        return
    icon = '📘' if service == 'Facebook' else '📷'
    await update.message.reply_text(
        f"{icon} *{service} code: `{code}`*\n\n"
        f"From: `{sender}`\n"
        f"Subject: `{subject}`",
        parse_mode='Markdown')
