"""
/blob — Facebook account blob → Cookie-Editor JSON decoder.

Accepts a seller-format FB account blob in any common layout (rigid old
format `email:pass:email:emailpass:profile_url:dob:UA:cookies_b64` or
modern variants), extracts the cookies, sanitizes them for Chrome's
strict Cookie-Editor import, returns a .json file the user can save
and import into the Cookie-Editor browser extension.

Telegram UX: /blob → user pastes (or attaches as .txt) → bot accumulates
across messages if needed → on parse success, sends the cleaned JSON
back as a Telegram document.
"""

import base64
import json
import logging
import re
import io
import html as _html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def parse_fb_account_blob(blob):
    """Smart parser — finds the FB cookie blob in any common seller-format
    text and extracts cookies + email + UA + profile URL. Only cookies are
    required; other fields are opportunistic.

    Returns dict: {email, profile_url, user_agent, cookies, cookies_b64,
                   profile_id, password, email_password, dob}
    Raises ValueError if no valid cookie blob is found.
    """
    if not blob or not isinstance(blob, str):
        raise ValueError('empty blob')
    text = blob.strip()

    # 1. Find cookie blob — longest base64 chunk that decodes to a JSON list
    candidates = re.findall(r'[A-Za-z0-9+/=_\-]{100,}', text)
    cookies = None
    cookies_b64 = None
    for cand in sorted(candidates, key=len, reverse=True):
        cleaned = ''.join(cand.split()).rstrip(',;')
        pad = (-len(cleaned)) % 4
        try:
            decoded = base64.b64decode(cleaned + '=' * pad, validate=False)
            data = json.loads(decoded.decode('utf-8', 'replace'))
        except Exception:
            continue
        if (isinstance(data, list) and data
                and isinstance(data[0], dict)
                and ('domain' in data[0] or 'name' in data[0])):
            cookies = data
            cookies_b64 = cleaned + '=' * pad
            break
    if not cookies:
        raise ValueError('no valid base64 cookie blob found in input '
                         '(expected a long base64 string that decodes '
                         'to a JSON array of cookie dicts)')

    # 2. profile_id from c_user cookie
    c_user = next(
        (str(c.get('value', '')) for c in cookies
         if str(c.get('name', '')).lower() == 'c_user'), None)

    # 3. email
    email_m = re.search(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', text)
    email = email_m.group(0).strip() if email_m else ''

    # 4. user-agent
    ua_m = re.search(r'Mozilla/[^\n:]+', text)
    user_agent = ua_m.group(0).strip() if ua_m else None

    # 5. profile URL
    url_m = re.search(
        r'https?://(?:www\.|m\.|mbasic\.)?facebook\.com[^\s:,]*', text)
    profile_url = url_m.group(0) if url_m else (
        f'https://www.facebook.com/profile.php?id={c_user}' if c_user else '')

    # 6. password / email_password / dob — best effort (often present as
    # plain text fields between colons; we don't try too hard)
    parts = text.split(':')
    password = parts[1].strip() if len(parts) >= 2 else ''
    email_password = parts[3].strip() if len(parts) >= 4 else ''
    dob_m = re.search(r'\b(?:19|20)\d{2}[/-]\d{1,2}[/-]\d{1,2}\b', text)
    dob = dob_m.group(0) if dob_m else ''

    return {
        'email': email,
        'password': password,
        'email_password': email_password,
        'profile_url': profile_url,
        'dob': dob,
        'user_agent': user_agent,
        'cookies_b64': cookies_b64,
        'cookies': cookies,
        'profile_id': c_user or '',
    }


_SAMESITE_NORMALIZE = {
    'none': 'no_restriction',
    'no_restriction': 'no_restriction',
    'lax': 'lax',
    'strict': 'strict',
    'unspecified': 'unspecified',
    '': 'unspecified',
}


def cookies_to_editor_json(raw_cookies):
    """Sanitize raw FB cookies → Cookie-Editor extension import format.
    Drops cookies that violate Chrome's strict rules (would throw
    individual 'Failed to create cookie X' toasts on import).

    Returns (json_text, dropped_count).
    """
    cookies = []
    dropped = []
    for c in raw_cookies:
        try:
            name = str(c.get('name', '')).strip()
            value = str(c.get('value', ''))
            domain = str(c.get('domain', '')).strip() or '.facebook.com'
            if not name:
                dropped.append(('?', 'empty name'))
                continue
            # Domain: must start with '.' or be a host
            if domain and not domain.startswith('.') and '.' not in domain:
                domain = '.' + domain
            path = str(c.get('path', '/')) or '/'
            secure = bool(c.get('secure', True))
            http_only = bool(c.get('httpOnly', False))
            ss_raw = str(c.get('sameSite', '')).lower()
            same_site = _SAMESITE_NORMALIZE.get(ss_raw, 'unspecified')
            exp = c.get('expirationDate') or c.get('expires')
            is_session = exp in (None, -1, 0, '0', '-1')
            entry = {
                'domain': domain,
                'name': name,
                'value': value,
                'path': path,
                'secure': secure,
                'httpOnly': http_only,
                'sameSite': same_site,
                'hostOnly': False,
                'session': is_session,
                'storeId': '0',
            }
            if not is_session:
                try:
                    entry['expirationDate'] = float(exp)
                except (ValueError, TypeError):
                    entry['session'] = True
            cookies.append(entry)
        except Exception as e:
            dropped.append((c.get('name', '?'), str(e)[:60]))
    return json.dumps(cookies, indent=2), len(dropped)


# ─── Telegram handlers ────────────────────────────────────────────────

async def blob_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/blob — start the cookie generator flow."""
    context.user_data['expecting_blob_input'] = True
    context.user_data['blob_buffer'] = ''
    await update.message.reply_text(
        "🍪 <b>FB Account Blob → Cookie-Editor JSON</b>\n\n"
        "Decodes a seller-format FB account blob into a Cookie-Editor "
        "importable JSON file.\n\n"
        "<b>📎 Recommended:</b> save the blob as <code>account.txt</code> "
        "and attach it. Whole thing in one shot.\n\n"
        "<b>📝 Alternative:</b> paste the blob as text. If Telegram splits "
        "it across multiple messages, bot accumulates until parsing succeeds.\n\n"
        "<i>Send /cancel to abort, /reset to clear the buffer.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="blob:cancel")]]))


async def blob_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a text message when /blob is expected."""
    if not context.user_data.get('expecting_blob_input'):
        return
    text = (update.message.text or '').strip()
    if text.lower() in ('/cancel', 'cancel'):
        context.user_data.pop('expecting_blob_input', None)
        context.user_data.pop('blob_buffer', None)
        await update.message.reply_text("❌ Cookie generator cancelled.")
        return
    if text.lower() in ('/reset', 'reset'):
        context.user_data['blob_buffer'] = ''
        await update.message.reply_text(
            "🧹 Buffer cleared. Paste the blob again from the start.")
        return
    await _process_blob_chunk(update, context, text)


async def blob_document_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a .txt document upload when /blob is expected."""
    if not context.user_data.get('expecting_blob_input'):
        return
    doc = update.message.document
    if not doc:
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (5MB max).")
        return
    file = await doc.get_file()
    blob_bytes = await file.download_as_bytearray()
    try:
        text = blob_bytes.decode('utf-8', errors='replace')
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't read file: {e}")
        return
    await _process_blob_chunk(update, context, text)


async def _process_blob_chunk(update, context, text):
    buf = context.user_data.get('blob_buffer', '') + text
    context.user_data['blob_buffer'] = buf
    try:
        parsed = parse_fb_account_blob(buf)
    except ValueError as e:
        err = str(e)
        # If error looks like "still parsing" — wait for more chunks
        if 'no valid base64 cookie blob found' in err and len(buf) < 50000:
            n_lines = buf.count('\n') + 1
            await update.message.reply_text(
                f"📥 Received chunk ({len(text)} chars, total now "
                f"{len(buf)} chars across {n_lines} message(s)).\n\n"
                f"<i>Still waiting for more — keep sending. Send "
                f"<code>/reset</code> to clear and start over.</i>",
                parse_mode='HTML')
            return
        context.user_data.pop('expecting_blob_input', None)
        context.user_data.pop('blob_buffer', None)
        await update.message.reply_text(
            f"❌ Blob parse failed: <code>{_html.escape(err)}</code>",
            parse_mode='HTML')
        return
    except Exception as e:
        context.user_data.pop('expecting_blob_input', None)
        context.user_data.pop('blob_buffer', None)
        logger.exception('blob parse crashed')
        await update.message.reply_text(
            f"❌ Unexpected error: <code>{_html.escape(str(e))}</code>",
            parse_mode='HTML')
        return
    # Success — clear state, build the cookie file
    context.user_data.pop('expecting_blob_input', None)
    context.user_data.pop('blob_buffer', None)
    raw_cookies = parsed.get('cookies') or []
    json_text, dropped = cookies_to_editor_json(raw_cookies)
    fname = f"fb_cookies_{parsed.get('profile_id', 'unknown')[:16]}.json"
    bio = io.BytesIO(json_text.encode('utf-8'))
    bio.name = fname
    await update.message.reply_document(
        document=InputFile(bio, filename=fname),
        caption=(f"✅ <b>Cookies decoded</b>\n\n"
                 f"📧 <b>Email:</b> <code>{_html.escape(parsed.get('email','?'))}</code>\n"
                 f"🆔 <b>Profile ID:</b> <code>{_html.escape(parsed.get('profile_id','?'))}</code>\n"
                 f"🍪 <b>Cookies:</b> {len(raw_cookies) - dropped} kept"
                 + (f", {dropped} dropped" if dropped else "")
                 + "\n\n<i>Open Cookie-Editor → Import → paste this JSON.</i>"),
        parse_mode='HTML')


async def blob_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle blob:* inline buttons."""
    query = update.callback_query
    await query.answer()
    if query.data == 'blob:cancel':
        context.user_data.pop('expecting_blob_input', None)
        context.user_data.pop('blob_buffer', None)
        await query.edit_message_text("❌ Cookie generator cancelled.")
