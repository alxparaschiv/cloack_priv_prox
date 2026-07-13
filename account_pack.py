"""/account_pack — one-tap full account identity package (single OR batch)
   + /batch_sms — fetch the SMS codes for the whole last batch at once.

Each package = a sequentially-numbered FB META POSTER account with:
  • Full name    — first + last, believable, mixed heritage + gender (LLM)
  • Gender       — Male / Female
  • Birthdate    — random, age 25-40, shown with the month as a name
  • Password     — the LLM password generator
  • Rambler email— one consumed from the Drive pool (email:password)
  • FB phone     — a fresh 7-day TextVerified Facebook rental (10-digit)

Everything is logged to a Drive JSON tracker + a native Google Sheet (link
returned each batch), and every account is exported as its own .txt (a batch
comes back as a .zip). See fb_poster_registry for the persistence + numbering.

/batch_sms pulls the verification code for every number in the most recent
batch in one shot (no more typing numbers into /sms one by one).
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
import fb_poster_registry as R

logger = logging.getLogger(__name__)

BATCH_MAX = 25
COUNT_OPTIONS = [1, 3, 5, 10]


# ─── Name + gender generation (LLM primary, local fallback) ─────────────────

_SYS_NAMES = """You generate believable, realistic FULL NAMES (with gender) for social-media account profiles.

Rules:
- Each entry = a first name + a last name of a real, ordinary person (NOT a celebrity or fictional character).
- Spread across DIFFERENT cultural heritages — mix widely (American, British, Irish, Italian, German, French, Spanish, Latin-American, Portuguese, Scandinavian, Polish, Czech, Greek, Turkish, Filipino, Vietnamese, Korean, Japanese, Indian, Arabic, Nigerian, Brazilian, …). Try not to repeat a heritage.
- Mix genders across the list (roughly half Male, half Female). The first name must match the gender.
- Plain ASCII Latin letters only (no accents), normal capitalization.

Output strictly as JSON, each entry a single string "Firstname Lastname | Heritage | Gender" (Gender is exactly Male or Female):
{"names": ["Firstname Lastname | Heritage | Gender", ...]}  ({N} items)"""

# Local fallback: (first, last, heritage, gender).
_FALLBACK_NAMES = [
    ('Ethan', 'Caldwell', 'American', 'Male'), ('Olivia', 'Bennett', 'British', 'Female'),
    ('Marco', 'Ricci', 'Italian', 'Male'), ('Lena', 'Hoffmann', 'German', 'Female'),
    ('Diego', 'Morales', 'Mexican', 'Male'), ('Sofia', 'Almeida', 'Portuguese', 'Female'),
    ('Anders', 'Lindqvist', 'Swedish', 'Male'), ('Katarzyna', 'Nowak', 'Polish', 'Female'),
    ('Nikos', 'Papadakis', 'Greek', 'Male'), ('Emine', 'Yilmaz', 'Turkish', 'Female'),
    ('Mateo', 'Fernandez', 'Argentine', 'Male'), ('Chloe', 'Dubois', 'French', 'Female'),
    ('Liam', 'Murphy', 'Irish', 'Male'), ('Ana', 'Silva', 'Brazilian', 'Female'),
    ('Kenji', 'Nakamura', 'Japanese', 'Male'), ('Priya', 'Nair', 'Indian', 'Female'),
    ('Omar', 'Haddad', 'Lebanese', 'Male'), ('Amara', 'Okafor', 'Nigerian', 'Female'),
    ('Petr', 'Novak', 'Czech', 'Male'), ('Lucas', 'Vermeulen', 'Dutch', 'Male'),
    ('Isabela', 'Cruz', 'Filipino', 'Female'), ('Minh', 'Tran', 'Vietnamese', 'Male'),
    ('Jisoo', 'Park', 'Korean', 'Female'), ('Elena', 'Popescu', 'Romanian', 'Female'),
]


def _gen_names(n):
    """Return n (first, last, heritage, gender) tuples. LLM first, else local."""
    n = max(1, min(BATCH_MAX, n))
    try:
        raw = _cs._call_openai_json(_SYS_NAMES, f"Generate {n} names.", n) or []
    except Exception as e:
        logger.warning(f"[account_pack] name LLM failed: {e}")
        raw = []
    out = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        segs = [p.strip() for p in s.split('|')]
        name_part = segs[0] if segs else ''
        her = segs[1] if len(segs) > 1 and segs[1] else '—'
        gender = segs[2] if len(segs) > 2 and segs[2] else ''
        gender = 'Female' if gender.lower().startswith('f') else (
            'Male' if gender.lower().startswith('m') else secrets.choice(['Male', 'Female']))
        parts = name_part.split()
        if len(parts) < 2:
            continue
        out.append((parts[0], ' '.join(parts[1:]), her, gender))
    if len(out) < n:
        pool = list(_FALLBACK_NAMES)
        while len(out) < n and pool:
            out.append(pool.pop(secrets.randbelow(len(pool))))
    return out[:n]


# ─── Birthdate (age 25-40, month shown as a name) ───────────────────────────

def random_birthdate():
    """Return (iso_str, display_str, age) with age guaranteed in [25, 40]."""
    today = datetime.date.today()
    bd, age = None, None
    for _ in range(30):
        age_target = 25 + secrets.randbelow(16)
        year = today.year - age_target
        month = 1 + secrets.randbelow(12)
        day = 1 + secrets.randbelow(calendar.monthrange(year, month)[1])
        bd = datetime.date(year, month, day)
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        if 25 <= age <= 40:
            break
    display = f"{bd.strftime('%B')} {bd.day}, {bd.year}"     # e.g. "November 13, 1997"
    return bd.isoformat(), display, age


# ─── Package card ───────────────────────────────────────────────────────────

def _format_card(idx, count, rec):
    e = html.escape
    ramb = (f"<code>{e(rec['rambler_email'])}</code> : <code>{e(rec['rambler_password'])}</code>"
            if rec.get('rambler_email') else "⚠️ pool empty — add to Drive")
    phone = f"<code>{e(rec['phone10'])}</code>" if rec.get('phone10') else \
            f"⚠️ {e(rec.get('phone_err', 'rental failed'))}"
    rid = (f"<code>{e(rec['rental_id'])}</code> · 7-day"
           if rec.get('rental_id') else "—")
    return '\n'.join([
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>{e(rec['account'])}</b>  ({idx}/{count})",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>Name:</b> <code>{e(rec['first'])} {e(rec['last'])}</code>",
        f"<b>Gender:</b> {e(rec['gender'])}",
        f"<b>Birthdate:</b> <code>{e(rec['birthdate_display'])}</code>  (age {rec['age']})",
        f"<b>Password:</b> <code>{e(rec['password'])}</code>",
        f"<b>Rambler:</b> {ramb}",
        f"<b>FB phone:</b> {phone}",
        f"<b>Rental ID:</b> {rid}",
    ])


def generate_packages(count, reserve, emit, post_one):
    """Build `count` packages. `reserve` is the result of R.reserve(count).
    Returns (records, phone_ok, balance, sheet_url, zip_bytes, zip_name)."""
    count = max(1, min(BATCH_MAX, count))
    start = reserve['start']
    ramblers = reserve['ramblers']
    emit(f"🧩 building {count} account(s) starting at "
         f"<b>{R.NAME_PREFIX} {start:03d}</b> — names + passwords first, then a "
         f"real 7-day FB number each.")

    names = _gen_names(count)
    pwds, _ai = password_gen.make_passwords(count)
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    records, phone_ok = [], 0
    for i in range(count):
        first, last, her, gender = names[i]
        iso, disp, age = random_birthdate()
        r_email, r_pw = ramblers[i] if i < len(ramblers) else (None, None)
        rec = {
            'account': f"{R.NAME_PREFIX} {start + i:03d}",
            'index': start + i,
            'first': first, 'last': last, 'heritage': her, 'gender': gender,
            'birthdate': iso, 'birthdate_display': disp, 'age': age,
            'password': pwds[i],
            'rambler_email': r_email or '', 'rambler_password': r_pw or '',
            'created_utc': now,
        }
        emit(f"📱 {i+1}/{count} renting Facebook number for {rec['account']}…")
        rental_id, phone, err = rental._rent_seven_day('facebook')
        if err or not phone:
            rec['phone_err'] = err or 'no number returned'
            rec['phone10'] = ''
            rec['rental_id'] = ''
        else:
            digits = ''.join(c for c in phone if c.isdigit())
            rec['phone10'] = digits[-10:] if len(digits) >= 10 else digits
            rec['rental_id'] = rental_id
            phone_ok += 1
        records.append(rec)
        post_one(_format_card(i + 1, count, rec))

    sheet_url, commit_err = R.commit(records, reserve['remaining_pool'],
                                     reserve['pool_fid'])
    if commit_err:
        emit(f"⚠️ tracker/sheet save issue: {commit_err}")
    zip_bytes, zip_name = R.build_zip(records)
    return records, phone_ok, rental._balance_str(), sheet_url, zip_bytes, zip_name


# ─── Telegram: /account_pack ────────────────────────────────────────────────

def _count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{n}", callback_data=f"acctpack:count:{n}")
         for n in COUNT_OPTIONS],
        [InlineKeyboardButton("✏️ Custom number", callback_data="acctpack:custom")],
        [InlineKeyboardButton("✖ cancel", callback_data="acctpack:cancel")],
    ])


async def account_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧩 <b>Account package generator</b>\n\n"
        "Each account = <b>FB META POSTER NNN</b> with a believable name + "
        "gender, a birthdate (age 25-40), a strong password, a Rambler email "
        "(from your Drive pool), and a fresh <b>7-day Facebook number</b>.\n\n"
        "Everything is logged to a Drive Google Sheet + JSON, and you get a "
        ".txt per account (batch → .zip).\n\n"
        f"💰 balance: <code>{html.escape(rental._balance_str())}</code> · each "
        "account uses one paid rental.\n\nHow many accounts?",
        parse_mode='HTML', reply_markup=_count_kb())


async def _run_batch(chat, context, count):
    count = max(1, min(BATCH_MAX, count))
    await chat.send_message(
        f"🧩 generating <b>{count}</b> account package(s)…", parse_mode='HTML')

    # Reserve numbering + rambler creds FIRST — abort if Drive is unreachable so
    # we never rent numbers we can't log or assign duplicate FB META POSTER #s.
    reserve = await asyncio.to_thread(R.reserve, count)
    if not reserve['ok']:
        await chat.send_message(
            f"❌ aborted before renting anything — {html.escape(reserve['err'])}\n"
            f"Fix Google Drive access and try again.")
        return
    if not reserve['had_pool']:
        await chat.send_message(
            "ℹ️ no <code>FB META POSTER · rambler pool.txt</code> found on Drive "
            "yet — accounts will be created without Rambler emails. Add the file "
            "(one <code>email:password</code> per line) to include them.",
            parse_mode='HTML')

    loop = asyncio.get_running_loop()

    def emit(m):
        try:
            asyncio.run_coroutine_threadsafe(
                chat.send_message(m, parse_mode='HTML'), loop)
        except Exception:
            pass

    def post_one(text):
        try:
            asyncio.run_coroutine_threadsafe(
                chat.send_message(text, parse_mode='HTML'), loop)
        except Exception:
            pass

    (records, phone_ok, balance, sheet_url,
     zip_bytes, zip_name) = await asyncio.to_thread(
        generate_packages, count, reserve, emit, post_one)

    # Send the .txt (single) or .zip (batch) file.
    import io as _io
    if len(records) == 1:
        doc = _io.BytesIO(R.account_txt(records[0]).encode('utf-8'))
        doc.name = records[0]['account'].replace(' ', '_') + '.txt'
    else:
        doc = _io.BytesIO(zip_bytes)
        doc.name = zip_name
    try:
        await context.bot.send_document(chat_id=chat.id, document=doc,
                                        filename=doc.name)
    except Exception as e:
        await chat.send_message(f"⚠️ couldn't attach the file: {e}")

    sheet_line = (f"📊 <a href=\"{sheet_url}\">Google Sheet (all accounts)</a>"
                  if sheet_url else "📊 sheet link unavailable")
    await context.bot.send_message(
        chat_id=chat.id,
        text=(f"🎉 <b>Done</b> — {len(records)} account(s), {phone_ok} with a "
              f"live FB number.\n{sheet_line}\n"
              f"💰 balance: <code>{html.escape(balance)}</code>\n\n"
              f"Tap below to grab the SMS codes for this whole batch."),
        parse_mode='HTML', disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔑 Get SMS codes for this batch", callback_data="acctpack:sms")]]))


async def account_pack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or '').split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'cancel':
        await q.edit_message_text("✖ cancelled.")
        return

    if action == 'custom':
        context.user_data['expecting_acctpack_count'] = True
        await q.edit_message_text(
            f"✏️ reply with how many accounts to create (1–{BATCH_MAX}).")
        return

    if action == 'count':
        try:
            count = min(BATCH_MAX, int(parts[2]))
        except (ValueError, IndexError):
            await q.edit_message_text("⚠️ bad count — run /account_pack again.")
            return
        await q.edit_message_text(f"🧩 starting {count} account(s)…")
        await _run_batch(q.message.chat, context, count)
        return

    if action == 'sms':
        await _do_batch_sms(q.message.chat, context)
        return


async def account_pack_count_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('expecting_acctpack_count', None)
    txt = (update.message.text or '').strip()
    try:
        count = int(''.join(ch for ch in txt if ch.isdigit()))
    except ValueError:
        await update.message.reply_text(f"⚠️ '{txt}' isn't a number. Run /account_pack again.")
        return
    if count < 1 or count > BATCH_MAX:
        await update.message.reply_text(f"⚠️ pick a number 1–{BATCH_MAX}.")
        return
    await _run_batch(update.message.chat, context, count)


# ─── Telegram: /batch_sms ───────────────────────────────────────────────────

def _fetch_batch_codes(batch):
    import sms_verified
    out = []
    for item in batch:
        rid = item.get('rental_id')
        if not rid:
            out.append((item, None, 'no rental id', None))
            continue
        code, content, sender, created, err = sms_verified._fetch_latest_sms(rid)
        out.append((item, code, err, content))
    return out


async def _do_batch_sms(chat, context):
    batch = await asyncio.to_thread(R.last_batch)
    if not batch:
        await chat.send_message(
            "ℹ️ no recent batch found. Create accounts with /account_pack first.")
        return
    await chat.send_message(
        f"🔑 fetching SMS codes for the last batch ({len(batch)} number(s))…")
    results = await asyncio.to_thread(_fetch_batch_codes, batch)
    e = html.escape
    lines = ["🔑 <b>Batch SMS codes</b>", ""]
    got = 0
    for item, code, err, _content in results:
        head = f"<b>{e(item['account'])}</b> · <code>{e(item.get('phone10',''))}</code>"
        if code:
            got += 1
            lines.append(f"{head}\n  ✅ <code>{e(code)}</code>")
        else:
            lines.append(f"{head}\n  ⏳ {e(err or 'no code yet')}")
    lines += ["", f"<i>{got}/{len(results)} codes ready. Re-run once more arrive "
                  f"(FB must send the SMS first).</i>"]
    await chat.send_message('\n'.join(lines), parse_mode='HTML')


async def batch_sms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_batch_sms(update.message.chat, context)
