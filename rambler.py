"""/rambler{,_microsoft} — Fetch the latest verification code from a Rambler inbox.

Conversation flows:
  /rambler            → prompt for "<email>:<password>", fetches FB/IG codes
  /rambler_microsoft  → same, fetches Microsoft account codes

Why this exists: during account creation / unlock flows, the user often needs
to grab a fresh confirmation code from a Rambler inbox. Manual login to
Rambler in the GoLogin browser is annoying — easier to just paste creds and
get the code back instantly.

Each fetcher walks the most-recent N messages and returns the FIRST one whose
sender keyword matches the requested service. Reply tags the service so the
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


def _extract_code_from_body(msg):
    """Extract a 4-8 digit code from the email body (HTML or plain).
    Strips HTML tags before regex so we don't match digits inside CSS/attributes.
    Returns the FIRST matching code, with a preference for digits adjacent to
    'code', 'confirmation', 'verification' keywords.
    """
    raw_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ('text/plain', 'text/html'):
                try:
                    raw_parts.append(part.get_payload(decode=True).decode('utf-8','replace'))
                except Exception:
                    pass
    else:
        try:
            raw_parts.append((msg.get_payload(decode=True) or b'').decode('utf-8','replace'))
        except Exception:
            pass
    body = '\n'.join(raw_parts)
    # Strip HTML tags + collapse whitespace so we look at human-visible text only
    text = re.sub(r'<[^>]+>', ' ', body)
    text = re.sub(r'\s+', ' ', text)
    # Prefer code-keyword-adjacent digits: 6-digit number near "code"/"confirmation"
    for m in re.finditer(r'\b(\d{4,8})\b', text):
        code = m.group(1)
        start = max(0, m.start()-60)
        ctx = text[start:m.end()+60].lower()
        if any(kw in ctx for kw in ('code', 'confirm', 'verif')):
            return code
    # Fallback: first 5-8 digit run anywhere in the visible body
    fm = re.search(r'\b(\d{5,8})\b', text)
    return fm.group(1) if fm else None


def _fetch_latest_code(email_addr: str, password: str, services):
    """Connect to Rambler IMAP, return (code, subject, sender, service, error).

    `services` is a list of (keyword, display_name) tuples — e.g.
    [('instagram','Instagram'), ('facebook','Facebook')] for Meta, or
    [('microsoft','Microsoft'), ('accountprotection','Microsoft'),
     ('outlook','Microsoft')] for Microsoft account codes.

    The walker checks the From: header for ANY of the keywords (lowercased
    substring match), tagging with the corresponding display_name. Subject
    is tried first for the code, then the body as a fallback.
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
            # Cheap header pre-check first (avoid fetching full body for non-matching mail)
            _, hdr_data = m.fetch(eid, '(RFC822.HEADER)')
            hdr_msg = _em.message_from_bytes(hdr_data[0][1])
            frm = hdr_msg.get('From', '') or ''
            subj = hdr_msg.get('Subject', '') or ''
            frm_lower = frm.lower()
            service = None
            for kw, display in services:
                if kw in frm_lower:
                    service = display
                    break
            if not service:
                continue
            # 1) try subject (FB convention: "648099 is your code…",
            #    MS convention: "Microsoft account security code: 1234567")
            code = None
            sm = re.search(r'\b(\d{4,8})\b', subj)
            if sm: code = sm.group(1)
            # 2) fall back to body (IG convention: subject has no digits, code is in body)
            if not code:
                _, full_data = m.fetch(eid, '(RFC822)')
                full_msg = _em.message_from_bytes(full_data[0][1])
                code = _extract_code_from_body(full_msg)
            if code:
                m.logout()
                return code, subj, frm, service, None
            # Email matched service but no code parseable — keep walking
        m.logout()
        names = ', '.join(sorted({d for _, d in services}))
        return None, None, None, None, f"no {names} email with a code found in last {RECENT_N} messages"
    except Exception as e:
        try: m.logout()
        except: pass
        return None, None, None, None, f"fetch failed: {type(e).__name__}: {e}"


# Sender-keyword tables per provider.
META_SERVICES = [
    ('instagram', 'Instagram'),
    ('facebook',  'Facebook'),
]
MICROSOFT_SERVICES = [
    # Microsoft sends account verification mail from a few addresses;
    # `accountprotection.microsoft.com` is the most common, but seen also
    # plain `microsoft.com`, `microsoftonline.com`, and Outlook security mail.
    ('accountprotection', 'Microsoft'),
    ('microsoft',         'Microsoft'),
    ('microsoftonline',   'Microsoft'),
    ('outlook',           'Microsoft'),
]


def _fetch_latest_meta_code(email_addr, password):
    return _fetch_latest_code(email_addr, password, META_SERVICES)

def _fetch_latest_microsoft_code(email_addr, password):
    return _fetch_latest_code(email_addr, password, MICROSOFT_SERVICES)

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


# ─── /rambler_microsoft ─────────────────────────────────────────────────────

async def rambler_microsoft_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /rambler_microsoft — prompt for credentials."""
    context.user_data['expecting_rambler_microsoft_creds'] = True
    await update.message.reply_text(
        "🪟 *Rambler Microsoft code fetcher*\n\n"
        "Send your Rambler credentials in the format:\n"
        "`email@rambler.ru:password`\n\n"
        "I'll fetch the most recent *Microsoft account* email and reply with "
        "the verification code.\n"
        "Credentials are NOT stored — used for one fetch then dropped.",
        parse_mode='Markdown')


async def rambler_microsoft_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the text reply with email:password for /rambler_microsoft."""
    if not context.user_data.get('expecting_rambler_microsoft_creds'):
        return
    text = (update.message.text or '').strip()
    context.user_data.pop('expecting_rambler_microsoft_creds', None)
    parts = text.split(':', 1)
    if len(parts) != 2 or '@' not in parts[0]:
        await update.message.reply_text(
            "❌ Format error. Expected `email@rambler.ru:password`. "
            "Run /rambler_microsoft again.",
            parse_mode='Markdown')
        return
    email_addr = parts[0].strip()
    password = parts[1].strip()
    if not email_addr or not password:
        await update.message.reply_text("❌ Empty email or password. Run /rambler_microsoft again.")
        return

    await update.message.reply_text(
        f"🔍 Connecting to Rambler IMAP for `{email_addr}`…",
        parse_mode='Markdown')
    code, subject, sender, service, err = _fetch_latest_microsoft_code(email_addr, password)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not code:
        await update.message.reply_text(
            f"⚠️ No Microsoft code found in last {RECENT_N} messages.",
            parse_mode='Markdown')
        return
    await update.message.reply_text(
        f"🪟 *{service} code: `{code}`*\n\n"
        f"From: `{sender}`\n"
        f"Subject: `{subject}`",
        parse_mode='Markdown')
