"""batch_verify.py — aggregate-then-confirm code fetchers.

/batch_sms      → paste many phone numbers into chat (one or many per message);
                  they accumulate; tap "Get all codes" to fetch every SMS code.
/batch_rambler  → same, but paste email:password lines and fetch each Rambler
                  inbox's latest FB/IG code.

Session lives in user_data['batch_verify'] = {'mode': 'sms'|'rambler',
'items': [...]}. Text is routed here from bot._text_router while a session is
active; buttons use the `bverify:` callback namespace.
"""
import re
import html
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sms_verified
import rambler

logger = logging.getLogger(__name__)


def _kb(n):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Get all codes ({n})",
                              callback_data="bverify:confirm")],
        [InlineKeyboardButton("🗑 Clear", callback_data="bverify:clear"),
         InlineKeyboardButton("✖ Cancel", callback_data="bverify:cancel")],
    ])


def _render(sess):
    mode = sess['mode']
    items = sess['items']
    if not items:
        body = "<i>nothing added yet</i>"
    elif mode == 'sms':
        body = '\n'.join(f"{i}. <code>{html.escape(p)}</code>"
                         for i, p in enumerate(items, 1))
    else:
        body = '\n'.join(f"{i}. <code>{html.escape(c['email'])}</code>"
                         for i, c in enumerate(items, 1))
    label = ("📲 <b>Batch SMS</b> — paste phone numbers (any format, one or many "
             "per message)." if mode == 'sms' else
             "📧 <b>Batch Rambler</b> — paste <code>email:password</code> lines "
             "(one or many per message).")
    return f"{label}\n\n<b>Collected ({len(items)}):</b>\n{body}"


# ─── Commands ───────────────────────────────────────────────────────────────

async def batch_sms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['batch_verify'] = {'mode': 'sms', 'items': []}
    await update.message.reply_text(_render(context.user_data['batch_verify']),
                                    parse_mode='HTML', reply_markup=_kb(0))


async def batch_rambler_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['batch_verify'] = {'mode': 'rambler', 'items': []}
    await update.message.reply_text(_render(context.user_data['batch_verify']),
                                    parse_mode='HTML', reply_markup=_kb(0))


# ─── Text aggregation (routed from bot._text_router) ────────────────────────

_PHONE_RE = re.compile(r'(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}')


def _extract_numbers(text):
    """Pull US phone numbers in ANY format (spaces/dashes/parens/+1) from a
    message. Returns de-duplicated 10-digit strings."""
    out, seen = [], set()
    for m in _PHONE_RE.findall(text):
        d = ''.join(ch for ch in m if ch.isdigit())
        if len(d) == 11 and d[0] == '1':
            d = d[1:]
        if len(d) == 10 and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _extract_creds(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if ':' in line and '@' in line.split(':', 1)[0]:
            parts = line.split(':')
            out.append({'email': parts[0].strip(),
                        'password': parts[1].strip() if len(parts) > 1 else ''})
    return out


async def batch_verify_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = context.user_data.get('batch_verify')
    if not sess:
        return False
    text = update.message.text or ''
    if sess['mode'] == 'sms':
        have = set(sess['items'])
        for p in _extract_numbers(text):
            if p not in have:
                sess['items'].append(p); have.add(p)
    else:
        have = {(c['email'], c['password']) for c in sess['items']}
        for c in _extract_creds(text):
            key = (c['email'], c['password'])
            if key not in have:
                sess['items'].append(c); have.add(key)
    await update.message.reply_text(_render(sess), parse_mode='HTML',
                                    reply_markup=_kb(len(sess['items'])))
    return True


# ─── Fetch (blocking; run in a thread) ──────────────────────────────────────

def _fetch_sms(phones):
    out = []
    for p in phones:
        rid, _profile, _status, err = sms_verified._find_rental_by_phone(p)
        if err or not rid:
            out.append((p, None, err or 'no rental found'))
            continue
        code, _content, _sender, _created, ferr = sms_verified._fetch_latest_sms(rid)
        out.append((p, code, ferr))
    return out


def _fetch_rambler(creds):
    out = []
    for c in creds:
        code, _subj, _sender, service, age, stale, err = \
            rambler._fetch_latest_meta_code(c['email'], c['password'])
        note = err
        if code and stale:
            note = f"stale ({age})"
        out.append((c['email'], code, note))
    return out


async def _do_confirm(chat, context, sess):
    mode, items = sess['mode'], sess['items']
    if not items:
        await chat.send_message("nothing to fetch — add some first.")
        return
    await chat.send_message(
        f"🔑 fetching codes for {len(items)} "
        f"{'number' if mode == 'sms' else 'inbox'}(s)… (a few seconds each)")
    if mode == 'sms':
        results = await asyncio.to_thread(_fetch_sms, items)
    else:
        results = await asyncio.to_thread(
            _fetch_rambler, items)
    e = html.escape
    lines = [f"🔑 <b>Batch {'SMS' if mode == 'sms' else 'Rambler'} codes</b>", ""]
    got = 0
    for ident, code, note in results:
        head = f"<code>{e(str(ident))}</code>"
        if code:
            got += 1
            tail = f"  ✅ <code>{e(str(code))}</code>" + (f"  <i>{e(note)}</i>" if note else "")
        else:
            tail = f"  ⏳ {e(note or 'no code yet')}"
        lines.append(f"{head}{tail}")
    lines += ["", f"<i>{got}/{len(results)} codes ready. Add more or re-run "
                  f"once new ones arrive.</i>"]
    await chat.send_message('\n'.join(lines), parse_mode='HTML')


async def batch_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = (q.data or '').split(':', 1)[1] if ':' in (q.data or '') else ''
    sess = context.user_data.get('batch_verify')
    if not sess:
        await q.edit_message_text("session expired — run /batch_sms or /batch_rambler again.")
        return
    if action == 'cancel':
        context.user_data.pop('batch_verify', None)
        await q.edit_message_text("✖ cancelled.")
        return
    if action == 'clear':
        sess['items'] = []
        await q.edit_message_text(_render(sess), parse_mode='HTML', reply_markup=_kb(0))
        return
    if action == 'confirm':
        await _do_confirm(q.message.chat, context, sess)
        return
