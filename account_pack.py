"""/account_pack — one-tap full account identity package (single OR batch).

Merges the existing generators into one "finished package" per account:
  • Full name    — first + last, believable, each from a different heritage (LLM)
  • Birthdate    — random, so the person is 25-40 years old today
  • Password     — the LLM password generator (readable, strong, unique)
  • FB phone     — a fresh 7-day TextVerified Facebook rental (real number)

Flow: /account_pack → pick how many (1/3/5/10) → each finished stack is posted
as its own tidy, copy-pasteable message with a header, then a summary with the
TextVerified balance.

Note: each phone is a REAL paid rental, so a batch of N spends N rentals.
"""
import html
import asyncio
import logging
import calendar
import datetime
import secrets

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import password_gen
import rental
import cloak_suggestions as _cs

logger = logging.getLogger(__name__)

BATCH_MAX = 10
COUNT_OPTIONS = [1, 3, 5, 10]


# ─── Name generation (LLM primary, local fallback) ──────────────────────────

_SYS_NAMES = """You generate believable, realistic FULL NAMES for social-media account profiles.

Rules:
- Each entry = a first name + a last name of a real, ordinary person (NOT a celebrity or fictional character).
- Spread them across DIFFERENT cultural heritages — mix widely (American, British, Irish, Italian, German, French, Spanish, Latin-American, Portuguese, Scandinavian, Polish, Czech, Greek, Turkish, Filipino, Vietnamese, Korean, Japanese, Indian, Arabic, Nigerian, Brazilian, …). Try not to repeat a heritage within the list.
- Realistic and common-but-not-famous; first + last should plausibly go together for that heritage.
- Plain ASCII Latin letters only (no accents/diacritics that break signup forms), normal capitalization.

Output strictly: {"names": [{"first": "...", "last": "...", "heritage": "..."}, ...]}  ({N} items)"""

# Local fallback: (first, last, heritage) across varied heritages.
_FALLBACK_NAMES = [
    ('Ethan', 'Caldwell', 'American'), ('Olivia', 'Bennett', 'British'),
    ('Marco', 'Ricci', 'Italian'), ('Lena', 'Hoffmann', 'German'),
    ('Diego', 'Morales', 'Mexican'), ('Sofia', 'Almeida', 'Portuguese'),
    ('Anders', 'Lindqvist', 'Swedish'), ('Katarzyna', 'Nowak', 'Polish'),
    ('Nikos', 'Papadakis', 'Greek'), ('Emre', 'Yilmaz', 'Turkish'),
    ('Mateo', 'Fernandez', 'Argentine'), ('Chloe', 'Dubois', 'French'),
    ('Liam', 'Murphy', 'Irish'), ('Ana', 'Silva', 'Brazilian'),
    ('Kenji', 'Nakamura', 'Japanese'), ('Priya', 'Nair', 'Indian'),
    ('Omar', 'Haddad', 'Lebanese'), ('Chinedu', 'Okafor', 'Nigerian'),
    ('Mia', 'Novak', 'Czech'), ('Lucas', 'Vermeulen', 'Dutch'),
    ('Isabela', 'Cruz', 'Filipino'), ('Minh', 'Tran', 'Vietnamese'),
    ('Jisoo', 'Park', 'Korean'), ('Elena', 'Popescu', 'Romanian'),
]


def _gen_names(n):
    """Return a list of n (first, last, heritage) tuples. LLM first, else local."""
    n = max(1, min(BATCH_MAX, n))
    try:
        raw = _cs._call_openai_json(_SYS_NAMES, f"Generate {n} names.", n) or []
    except Exception as e:
        logger.warning(f"[account_pack] name LLM failed: {e}")
        raw = []
    out = []
    for item in raw:
        if isinstance(item, dict):
            first = str(item.get('first', '')).strip()
            last = str(item.get('last', '')).strip()
            her = str(item.get('heritage', '')).strip() or '—'
        elif isinstance(item, str) and item.strip():
            parts = item.strip().split()
            first, last, her = parts[0], ' '.join(parts[1:]) or '', '—'
        else:
            continue
        if first and last:
            out.append((first, last, her))
    # Top up / fall back locally if the LLM came up short.
    if len(out) < n:
        pool = list(_FALLBACK_NAMES)
        while len(out) < n and pool:
            out.append(pool.pop(secrets.randbelow(len(pool))))
    return out[:n]


# ─── Birthdate (age 25-40) ──────────────────────────────────────────────────

def random_birthdate():
    """Return (date, age) with age guaranteed in [25, 40] as of today."""
    today = datetime.date.today()
    for _ in range(20):
        age_target = 25 + secrets.randbelow(16)      # 25..40
        year = today.year - age_target
        month = 1 + secrets.randbelow(12)
        day = 1 + secrets.randbelow(calendar.monthrange(year, month)[1])
        bd = datetime.date(year, month, day)
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        if 25 <= age <= 40:
            return bd, age
    return bd, age


# ─── Package assembly ───────────────────────────────────────────────────────

def _format_package(idx, count, pkg):
    first, last, her = pkg['name']
    bd, age = pkg['birth']
    pw = pkg['password']
    e = html.escape
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Account {idx}/{count}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>Name:</b> <code>{e(first)} {e(last)}</code>",
        f"<b>Heritage:</b> {e(her)}",
        f"<b>Birthdate:</b> <code>{bd.isoformat()}</code>  ({bd.strftime('%b %d, %Y')}, age {age})",
        f"<b>Password:</b> <code>{e(pw)}</code>",
    ]
    if pkg.get('phone_e164'):
        lines += [
            f"<b>FB phone (E.164):</b> <code>{e(pkg['phone_e164'])}</code>",
            f"<b>FB phone (10-digit):</b> <code>{e(pkg['phone_10'])}</code>",
            f"<b>Rental ID:</b> <code>{e(str(pkg['rental_id']))}</code>  ·  7-day",
        ]
    else:
        lines.append(f"<b>FB phone:</b> ⚠️ {e(pkg.get('phone_err', 'rental failed'))}")
    return '\n'.join(lines)


def generate_packages(count, emit, post_one):
    """Build `count` full account packages. Names/passwords/birthdates are made
    up front (fast); FB rentals happen one at a time (the slow, paid part), and
    each finished package is posted via post_one(text) as its phone lands.
    Returns (ok_count, phone_ok_count, balance_str)."""
    count = max(1, min(BATCH_MAX, count))
    emit(f"🧩 building {count} account package(s)… (names + passwords first, "
         f"then a real 7-day FB number each)")

    names = _gen_names(count)
    pwds, _used_ai = password_gen.make_passwords(count)
    births = [random_birthdate() for _ in range(count)]

    phone_ok = 0
    for i in range(count):
        pkg = {'name': names[i], 'password': pwds[i], 'birth': births[i]}
        emit(f"📱 {i+1}/{count} renting Facebook number…")
        rental_id, phone, err = rental._rent_seven_day('facebook')
        if err or not phone:
            pkg['phone_err'] = err or 'no number returned'
        else:
            digits = ''.join(c for c in phone if c.isdigit())
            pkg['rental_id'] = rental_id
            pkg['phone_e164'] = phone if phone.startswith('+') else f'+{phone}'
            pkg['phone_10'] = digits[-10:] if len(digits) >= 10 else digits
            phone_ok += 1
        post_one(_format_package(i + 1, count, pkg))

    return count, phone_ok, rental._balance_str()


# ─── Telegram flow ──────────────────────────────────────────────────────────

def _count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{n}", callback_data=f"acctpack:count:{n}")
         for n in COUNT_OPTIONS],
        [InlineKeyboardButton("✖ cancel", callback_data="acctpack:cancel")],
    ])


async def account_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧩 <b>Account package generator</b>\n\n"
        "Each package = a believable <b>full name</b> (mixed heritage), a "
        "<b>birthdate</b> (age 25-40), a strong <b>password</b>, and a fresh "
        "<b>7-day Facebook phone number</b> (real TextVerified rental).\n\n"
        f"💰 balance: <code>{rental._balance_str()}</code> · each account uses "
        "one paid rental.\n\n"
        "How many accounts?",
        parse_mode='HTML', reply_markup=_count_kb())


async def account_pack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or '').split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'cancel':
        await q.edit_message_text("✖ cancelled.")
        return

    if action == 'count':
        try:
            count = min(BATCH_MAX, int(parts[2]))
        except (ValueError, IndexError):
            await q.edit_message_text("⚠️ bad count — run /account_pack again.")
            return
        await q.edit_message_text(
            f"🧩 generating <b>{count}</b> full account package(s)…\n"
            f"~{max(1, count)}-{count*2} min (one real FB rental each). "
            f"I'll post each as it's ready.",
            parse_mode='HTML')

        loop = asyncio.get_running_loop()
        chat = q.message.chat

        def emit(m):
            try:
                asyncio.run_coroutine_threadsafe(chat.send_message(m), loop)
            except Exception:
                pass

        def post_one(text):
            try:
                asyncio.run_coroutine_threadsafe(
                    chat.send_message(text, parse_mode='HTML'), loop)
            except Exception:
                pass

        ok, phone_ok, balance = await asyncio.to_thread(
            generate_packages, count, emit, post_one)

        await chat.send_message(
            f"🎉 <b>Done</b> — {ok} package(s), {phone_ok}/{ok} with a live FB "
            f"number.\n💰 TextVerified balance: <code>{html.escape(balance)}</code>\n\n"
            f"<i>Use /sms with a package's number to grab its SMS code when it "
            f"arrives.</i>",
            parse_mode='HTML')
