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
import datetime as _dt
from email.header import decode_header
from email.utils import parsedate_to_datetime
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

IMAP_HOST = 'imap.rambler.ru'
IMAP_PORT = 993
IMAP_TIMEOUT = 15

# Look in the last N emails per folder for the most recent code
RECENT_N = 30

# Folders to scan. INBOX first (most likely), Spam second (FB sometimes
# gets quarantined). Trash/Sent/Drafts skipped.
SCAN_FOLDERS = ['INBOX', 'Spam']

# Notification / activity senders we EXPLICITLY skip even though their
# domain matches 'facebook' / 'instagram'. These never carry codes —
# they're feed reminders, group updates, friend suggestions, etc.
# Adding to this list is purely subtractive — it only PREVENTS bad
# matches; it never blocks a sender that's already working today.
REJECTED_SENDER_PREFIXES = (
    'groupupdates@facebookmail.com',
    'reminders@facebookmail.com',
    'friendupdates@facebookmail.com',
    'newsletter@facebookmail.com',
    'notify@facebookmail.com',
    'mention@facebookmail.com',
    'comment@facebookmail.com',
    'tag@facebookmail.com',
)

# An email is "fresh enough" to be a recent verification code if its
# date is within this many minutes. Older matches are STILL returned
# (so we don't regress accounts where the existing flow works), but
# the user-facing reply prepends a ⚠️ stale warning + age so it's
# obvious if the code is from 4 years ago vs. 4 minutes ago.
FRESH_MINUTES = 15


def _mime_decode(s):
    """MIME-decode a header (handles =?UTF-8?B?...?= bundles). Returns
    a plain unicode string."""
    if not s:
        return ''
    out = []
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or 'utf-8', errors='replace'))
            except Exception:
                out.append(txt.decode('utf-8', errors='replace'))
        else:
            out.append(txt)
    return ''.join(out)


def _age_str(dt):
    """Format the age of an email (datetime) in a human-readable way:
    '4y ago', '2mo ago', '13d ago', '47m ago', '8s ago'. Returns
    ('age_string', is_stale_bool)."""
    if not dt:
        return ('unknown age', True)
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        delta = now - dt
    except Exception:
        return ('unknown age', True)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ('just now', False)
    if seconds < 60:
        return (f'{seconds}s ago', seconds > FRESH_MINUTES * 60)
    minutes = seconds // 60
    if minutes < 60:
        return (f'{minutes}m ago', minutes > FRESH_MINUTES)
    hours = minutes // 60
    if hours < 24:
        return (f'{hours}h ago', True)
    days = hours // 24
    if days < 30:
        return (f'{days}d ago', True)
    months = days // 30
    if months < 12:
        return (f'{months}mo ago', True)
    years = days // 365
    return (f'{years}y ago', True)


def _mime_decode(s):
    """MIME-decode a header (handles =?UTF-8?B?...?= bundles). Returns
    a plain unicode string."""
    if not s:
        return ''
    out = []
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or 'utf-8', errors='replace'))
            except Exception:
                out.append(txt.decode('utf-8', errors='replace'))
        else:
            out.append(txt)
    return ''.join(out)


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
    """Connect to Rambler IMAP, return (code, subject, sender, service,
    age_str, is_stale, error).

    `services` is a list of (keyword, display_name) tuples — e.g.
    [('instagram','Instagram'), ('facebook','Facebook')] for Meta, or
    [('microsoft','Microsoft'), ('accountprotection','Microsoft'),
     ('outlook','Microsoft')] for Microsoft account codes.

    Improvements over the original "first match in INBOX" logic:
      • Scans ALL folders in SCAN_FOLDERS (INBOX + Spam) — codes can
        land in Spam and the old logic missed them entirely.
      • Skips notification-noise senders (groupupdates@, reminders@, …)
        which match the broad 'facebook' keyword but never carry codes.
      • Collects ALL service-matching candidates across folders, then
        picks the actually-newest one by INTERNALDATE / Date: header.
        Old logic relied on IMAP UID order in one folder, which can
        misorder messages that arrived via different routes.
      • MIME-decodes the Subject so non-ASCII subjects (Hindi, Russian,
        Chinese, etc.) display readably in TG instead of as base64.
      • Returns the email's age and a is_stale flag (>15min old). The
        caller surfaces these so a 4-year-old match is obviously stale.
    """
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    except Exception as e:
        return None, None, None, None, None, False, f"connect failed: {type(e).__name__}: {e}"
    try:
        m.login(email_addr, password)
    except Exception as e:
        try: m.logout()
        except: pass
        return None, None, None, None, None, False, \
            f"login failed (check email/password): {type(e).__name__}: {e}"

    candidates = []  # list of dicts with code/subj/frm/service/dt per match
    try:
        for folder in SCAN_FOLDERS:
            rv, info = m.select(folder, readonly=True)
            if rv != 'OK':
                logger.info(f"[rambler] skip folder {folder!r}: select={rv}")
                continue
            _, ids = m.search(None, 'ALL')
            all_ids = ids[0].split() if ids and ids[0] else []
            if not all_ids:
                continue
            # Walk newest-first by IMAP UID, capped at RECENT_N per folder
            for eid in all_ids[::-1][:RECENT_N]:
                try:
                    _, hdr_data = m.fetch(eid, '(RFC822.HEADER)')
                    hdr_msg = _em.message_from_bytes(hdr_data[0][1])
                except Exception as e:
                    logger.warning(f"[rambler] header fetch err in {folder}/{eid}: {e}")
                    continue
                frm = hdr_msg.get('From', '') or ''
                subj_raw = hdr_msg.get('Subject', '') or ''
                subj = _mime_decode(subj_raw)
                date_hdr = hdr_msg.get('Date', '') or ''
                frm_lower = frm.lower()
                # Skip known-noise senders even if their domain matches
                if any(p in frm_lower for p in REJECTED_SENDER_PREFIXES):
                    continue
                # Match against the service keyword list (UNCHANGED — same
                # broad match the old logic used, just additive filtering
                # on top)
                service = None
                for kw, display in services:
                    if kw in frm_lower:
                        service = display
                        break
                if not service:
                    continue
                # Parse code: subject first (existing logic), body fallback
                code = None
                sm = re.search(r'\b(\d{4,8})\b', subj)
                if sm:
                    code = sm.group(1)
                if not code:
                    try:
                        _, full_data = m.fetch(eid, '(RFC822)')
                        full_msg = _em.message_from_bytes(full_data[0][1])
                        code = _extract_code_from_body(full_msg)
                    except Exception as e:
                        logger.warning(f"[rambler] body fetch err in {folder}/{eid}: {e}")
                if not code:
                    continue
                # Parse the email date for freshness sorting
                try:
                    dt = parsedate_to_datetime(date_hdr) if date_hdr else None
                except Exception:
                    dt = None
                candidates.append({
                    'code': code, 'subj': subj, 'frm': frm,
                    'service': service, 'dt': dt, 'folder': folder,
                })
        try: m.logout()
        except: pass
    except Exception as e:
        try: m.logout()
        except: pass
        return None, None, None, None, None, False, f"fetch failed: {type(e).__name__}: {e}"

    if not candidates:
        names = ', '.join(sorted({d for _, d in services}))
        return None, None, None, None, None, False, \
            f"no {names} email with a code found in last {RECENT_N} messages " \
            f"(scanned: {', '.join(SCAN_FOLDERS)})"

    # Pick the truly newest by Date: header. If a candidate has no
    # parseable date, it sorts to the bottom (we still return it as a
    # last resort, but only if nothing dated is available).
    def _key(c):
        return c['dt'] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
    candidates.sort(key=_key, reverse=True)
    best = candidates[0]
    age_str, is_stale = _age_str(best['dt'])
    return best['code'], best['subj'], best['frm'], best['service'], \
        age_str, is_stale, None


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
    code, subject, sender, service, age, is_stale, err = _fetch_latest_meta_code(email_addr, password)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not code:
        await update.message.reply_text(
            f"⚠️ No Facebook or Instagram code found in last {RECENT_N} messages.",
            parse_mode='Markdown')
        return
    icon = '📘' if service == 'Facebook' else '📷'
    stale_warn = (f"⚠️ *STALE — this email is {age}*. Likely NOT a fresh "
                  f"verification code; the latest matching email in this "
                  f"inbox is old. If you just triggered a new code, try "
                  f"again in 1 min or check whether the request actually "
                  f"sent (FB sometimes silently rate-limits new accounts).\n\n"
                  if is_stale else f"_(received {age})_\n\n")
    await update.message.reply_text(
        f"{icon} *{service} code: `{code}`*\n\n"
        f"{stale_warn}"
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
    code, subject, sender, service, age, is_stale, err = _fetch_latest_microsoft_code(email_addr, password)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not code:
        await update.message.reply_text(
            f"⚠️ No Microsoft code found in last {RECENT_N} messages.",
            parse_mode='Markdown')
        return
    stale_warn = (f"⚠️ *STALE — this email is {age}*. Likely NOT a fresh "
                  f"verification code; the latest matching email in this "
                  f"inbox is old.\n\n"
                  if is_stale else f"_(received {age})_\n\n")
    await update.message.reply_text(
        f"🪟 *{service} code: `{code}`*\n\n"
        f"{stale_warn}"
        f"From: `{sender}`\n"
        f"Subject: `{subject}`",
        parse_mode='Markdown')
