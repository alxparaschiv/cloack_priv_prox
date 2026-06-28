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
    """Find a TextVerified rental matching this phone number.

    Two-step lookup:
      1) Accounts CSV — preferred (gives us profile + status context).
      2) TextVerified API directly — fallback for rentals that were never
         written to the CSV (e.g. ad-hoc Instagram rentals, rentals from
         today that haven't been bound to an FB account yet).

    Returns (rental_id, profile_name, status, notes) or (None, None, None, err)."""
    # ── step 1: accounts CSV
    try:
        import accounts_sheet as asm
        rows = asm._read_all_rows()
        header = asm.HEADER
        rp_idx = header.index('Rental Phone') if 'Rental Phone' in header else None
        ri_idx = header.index('Rental ID') if 'Rental ID' in header else None
        gp_idx = header.index('GoLogin Profile') if 'GoLogin Profile' in header else None
        st_idx = header.index('Status') if 'Status' in header else None
        if rp_idx is not None and ri_idx is not None:
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
                    })
            if matches:
                latest = matches[-1]  # append-only sheet — freshest is last
                return latest['rental_id'], latest['profile'], latest['status'], None
    except Exception as e:
        logger.warning(f"accounts CSV lookup failed (will try TextVerified direct): {e}")

    # ── step 2: TextVerified API direct lookup
    try:
        from textverified_client import _client
        body = _client()._request('GET', '/api/pub/v2/reservations/rental/nonrenewable')
        items = body if isinstance(body, list) else (body.get('data') or [])
        matches = []
        for r in items:
            num = re.sub(r'\D', '', r.get('number','') or '')
            if num.endswith(phone10):
                matches.append({
                    'rental_id': r.get('id',''),
                    'state': r.get('state',''),
                    'service': r.get('serviceName',''),
                    'createdAt': r.get('createdAt',''),
                })
        if not matches:
            return None, None, None, f"no rental found for phone ending {phone10} (checked accounts CSV + TextVerified directly)"
        # Newest first
        matches.sort(key=lambda x: x['createdAt'], reverse=True)
        m = matches[0]
        return m['rental_id'], f"(not-in-CSV, service={m['service']})", m['state'], None
    except Exception as e:
        return None, None, None, f"TextVerified rental lookup failed: {type(e).__name__}: {e}"


def _extract_code(content):
    """Extract a numeric verification code from SMS text.
    Handles codes split by a space or hyphen (e.g. "123 456" -> "123456"),
    which Meta/Instagram commonly use. Returns the code string or None.
    """
    # 1) Contiguous run of 4-8 digits (the common case). Lookarounds (not \b)
    #    so we grab the whole run and don't trip on adjacent punctuation.
    m_code = re.search(r'(?<!\d)(\d{4,8})(?!\d)', content)
    if m_code:
        return m_code.group(1)
    # 2) Split form: two short digit groups separated by ANY run of whitespace
    #    (space, NBSP, tab, newline) and/or dash variants. Meta/Instagram send
    #    "123 456", but the separator can be a non-breaking space or newline,
    #    which the old single-char [ \-] class missed.
    sep = r'[\s   \-‐-―]+'
    m_split = re.search(r'(?<!\d)(\d{2,4})' + sep + r'(\d{2,4})(?!\d)', content)
    if m_split:
        joined = m_split.group(1) + m_split.group(2)
        if 4 <= len(joined) <= 8:
            return joined
    return None


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
    code = _extract_code(content)
    if not code:
        return None, content, sender, created_at, "no numeric code pattern in SMS"
    return code, content, sender, created_at, None


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
