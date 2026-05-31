"""/sms — Fetch the latest SMS code from a TextVerified rental.

Conversation flow:
  /sms                  → prompt for a phone number
  user replies          → look up rental by phone, fetch newest SMS, extract code

Why this exists: during Meta Dev account creation, we often need a fresh SMS
code from a TextVerified rental (for AC binding confirmation, wizard phone
verify, etc.). Manual TextVerified web checks are tedious — paste the phone
number, get the code back instantly. Mirrors the /rambler pattern.

Phone number formats accepted (US numbers only — TextVerified pool):
  +14709482006  4709482006  14709482006
  (470) 948-2006   470-948-2006   470.948.2006
  +1 (470) 948 2006   etc.
Anything that contains 10 contiguous digits, with optional leading 1 / +1
country code, is normalized to a 10-digit US number.
"""
import os
import re
import logging
import sys

sys.path.insert(0, '/app')
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _normalize_phone(text):
    """Accept any common US phone format → return 10-digit number string, or None."""
    digits = re.sub(r'\D', '', text or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return None


def _find_rental_by_phone(phone10):
    """Search the accounts CSV for a rental matching this phone number.
    Returns (rental_id, profile_name, status, notes) or (None, None, None, err)."""
    try:
        import accounts_sheet as asm
        rows = asm._read_all_rows()
        header = asm.HEADER
        rp_idx = header.index('Rental Phone') if 'Rental Phone' in header else None
        ri_idx = header.index('Rental ID') if 'Rental ID' in header else None
        gp_idx = header.index('GoLogin Profile') if 'GoLogin Profile' in header else None
        st_idx = header.index('Status') if 'Status' in header else None
        if rp_idx is None or ri_idx is None:
            return None, None, None, "accounts sheet missing Rental Phone / Rental ID columns"
        matches = []
        for r in rows:
            rp = r[rp_idx] if rp_idx < len(r) else ''
            ri = r[ri_idx] if ri_idx < len(r) else ''
            if not (rp and ri):
                continue
            rp_digits = re.sub(r'\D', '', rp)
            if rp_digits.endswith(phone10) and ri.startswith('lr_'):
                matches.append({
                    'rental_id': ri,
                    'profile': r[gp_idx] if gp_idx is not None and gp_idx < len(r) else '?',
                    'status': r[st_idx] if st_idx is not None and st_idx < len(r) else '?',
                    'phone_full': rp,
                })
        if not matches:
            return None, None, None, f"no rental in accounts CSV with phone ending {phone10}"
        # If multiple, prefer the freshest (last in the sheet) — accounts CSV is append-only
        latest = matches[-1]
        return latest['rental_id'], latest['profile'], latest['status'], None
    except Exception as e:
        return None, None, None, f"accounts lookup failed: {type(e).__name__}: {e}"


def _fetch_latest_sms(rental_id):
    """Get newest SMS for the rental + extract numeric code.
    Returns (code, content, sender, created_at, error)."""
    try:
        from textverified_client import _client
        body = _client()._request('GET', f'/api/pub/v2/sms?ReservationId={rental_id}')
    except Exception as e:
        return None, None, None, None, f"TextVerified API error: {type(e).__name__}: {e}"
    data = body if isinstance(body, list) else (body.get('data') or [])
    if not data:
        return None, None, None, None, f"no SMS yet for rental {rental_id}"
    # Sort newest first by createdAt
    try:
        data.sort(key=lambda s: s.get('createdAt', ''), reverse=True)
    except Exception:
        pass
    sms = data[0]
    content = sms.get('smsContent') or sms.get('content') or ''
    sender = sms.get('from') or sms.get('sender') or ''
    created_at = sms.get('createdAt', '')
    if not content:
        return None, None, sender, created_at, "latest SMS has no content"
    # Most FB / IG / Meta codes are 4-8 digits. Match the first run.
    m_code = re.search(r'\b(\d{4,8})\b', content)
    if not m_code:
        return None, content, sender, created_at, "no numeric code pattern in SMS"
    return m_code.group(1), content, sender, created_at, None


async def sms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /sms — prompt for phone number."""
    context.user_data['expecting_sms_phone'] = True
    await update.message.reply_text(
        "📱 *TextVerified SMS code fetcher*\n\n"
        "Send the rental phone number in any common US format:\n"
        "`+14709482006`  `4709482006`  `14709482006`\n"
        "`(470) 948-2006`  `470-948-2006`\n\n"
        "I'll look up the rental in the accounts sheet, fetch the most recent SMS, "
        "and reply with the code.",
        parse_mode='Markdown')


async def sms_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the text reply with a phone number."""
    if not context.user_data.get('expecting_sms_phone'):
        return
    text = (update.message.text or '').strip()
    context.user_data.pop('expecting_sms_phone', None)

    phone10 = _normalize_phone(text)
    if not phone10:
        await update.message.reply_text(
            "❌ Couldn't parse a US phone number from that. "
            "I need 10 digits (optionally with a +1 / 1 country code). Run /sms again.")
        return

    await update.message.reply_text(
        f"🔍 Looking up rental for `+1{phone10}`…", parse_mode='Markdown')

    rental_id, profile, status, err = _find_rental_by_phone(phone10)
    if err:
        await update.message.reply_text(f"❌ {err}\n\nDouble-check the number and run /sms again.")
        return

    await update.message.reply_text(
        f"📋 Found: `{rental_id}`\n"
        f"Profile: `{profile}` · Status: `{status}`\n\n"
        f"Fetching newest SMS…",
        parse_mode='Markdown')

    code, content, sender, created_at, err = _fetch_latest_sms(rental_id)
    if err:
        await update.message.reply_text(f"⚠️ {err}")
        return
    if not code:
        await update.message.reply_text(
            f"⚠️ SMS arrived but no code pattern found.\n"
            f"Content: `{(content or '')[:200]}`",
            parse_mode='Markdown')
        return
    await update.message.reply_text(
        f"✅ *Code: `{code}`*\n\n"
        f"From: `{sender}`\n"
        f"Received: `{created_at}`\n"
        f"Content: `{(content or '')[:200]}`",
        parse_mode='Markdown')
