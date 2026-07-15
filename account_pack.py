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
import re
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
import privacy
import cloak
import cloak_suggestions as _cs
import fb_poster_registry as R

logger = logging.getLogger(__name__)

BATCH_MAX = 25
COUNT_OPTIONS = [1, 3, 5, 10]


# ─── Name + gender generation (LLM primary, local fallback) ─────────────────

# Facebook rejects short names (e.g. "Luca Rosi"). Enforce a comfortable
# minimum so generated names always pass signup.
_NAME_MIN_FIRST = 3
_NAME_MIN_LAST = 4
_NAME_MIN_TOTAL = 11


def _name_ok(first, last):
    f = ''.join(c for c in (first or '') if c.isalpha())
    l = ''.join(c for c in (last or '') if c.isalpha())
    return (len(f) >= _NAME_MIN_FIRST and len(l) >= _NAME_MIN_LAST
            and len(f) + len(l) >= _NAME_MIN_TOTAL)


_SYS_NAMES = """You generate believable, realistic FULL NAMES (with gender) for social-media account profiles.

Rules:
- Each entry = a first name + a last name of a real, ordinary person (NOT a celebrity or fictional character).
- LENGTH — CRITICAL: Facebook REJECTS short names. Every first name + last name TOGETHER must be at least 11 letters, and the last name at least 4 letters. Do NOT output short combos like "Luca Rosi", "Ana Silva", "Minh Tran" — prefer fuller surnames (e.g. "Marco Bianchi", "Priya Sharma", "Hoang Nguyen").
- Spread across DIFFERENT cultural heritages — mix widely (American, British, Irish, Italian, German, French, Spanish, Latin-American, Portuguese, Scandinavian, Polish, Czech, Greek, Turkish, Filipino, Vietnamese, Korean, Japanese, Indian, Arabic, Nigerian, Brazilian, …). Try not to repeat a heritage.
- Mix genders across the list (roughly half Male, half Female). The first name must match the gender.
- Plain ASCII Latin letters only (no accents), normal capitalization.

Output strictly as JSON, each entry a single string "Firstname Lastname | Heritage | Gender" (Gender is exactly Male or Female):
{"names": ["Firstname Lastname | Heritage | Gender", ...]}  ({N} items)"""

# Local fallback: (first, last, heritage, gender). All pass _name_ok.
_FALLBACK_NAMES = [
    ('Ethan', 'Caldwell', 'American', 'Male'), ('Olivia', 'Bennett', 'British', 'Female'),
    ('Marco', 'Bianchi', 'Italian', 'Male'), ('Lena', 'Hoffmann', 'German', 'Female'),
    ('Diego', 'Morales', 'Mexican', 'Male'), ('Sofia', 'Almeida', 'Portuguese', 'Female'),
    ('Anders', 'Lindqvist', 'Swedish', 'Male'), ('Katarzyna', 'Nowak', 'Polish', 'Female'),
    ('Nikos', 'Papadakis', 'Greek', 'Male'), ('Emine', 'Yilmaz', 'Turkish', 'Female'),
    ('Mateo', 'Fernandez', 'Argentine', 'Male'), ('Chloe', 'Dubois', 'French', 'Female'),
    ('Liam', 'Gallagher', 'Irish', 'Male'), ('Bianca', 'Ferreira', 'Brazilian', 'Female'),
    ('Kenji', 'Nakamura', 'Japanese', 'Male'), ('Priya', 'Sharma', 'Indian', 'Female'),
    ('Youssef', 'Haddad', 'Lebanese', 'Male'), ('Amara', 'Okafor', 'Nigerian', 'Female'),
    ('Pavel', 'Novakov', 'Czech', 'Male'), ('Lucas', 'Vermeulen', 'Dutch', 'Male'),
    ('Isabela', 'Delgado', 'Filipino', 'Female'), ('Hoang', 'Nguyen', 'Vietnamese', 'Male'),
    ('Seojin', 'Hwang', 'Korean', 'Female'), ('Elena', 'Popescu', 'Romanian', 'Female'),
    ('Viktor', 'Sokolov', 'Russian', 'Male'), ('Camila', 'Rossini', 'Italian', 'Female'),
    ('Nathan', 'Brooks', 'American', 'Male'), ('Freya', 'Andersen', 'Danish', 'Female'),
]


def _gen_names(n, exclude=None):
    """Return n (first, last, heritage, gender) tuples that pass the FB length
    check AND don't repeat any name in `exclude` (existing accounts) or within
    the batch. LLM first (short/dup filtered out), else length-safe local list.
    Over-fetches from the LLM so dedup doesn't starve the batch."""
    n = max(1, min(BATCH_MAX, n))
    seen = set(exclude or ())               # lowercased "first last"
    try:
        raw = _cs._call_openai_json(_SYS_NAMES, f"Generate {n + 6} names.", n + 6) or []
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
        first, last = parts[0], ' '.join(parts[1:])
        if not _name_ok(first, last):        # skip FB-too-short names
            continue
        key = f"{first} {last}".lower()
        if key in seen:                      # skip repeats (existing + in-batch)
            continue
        seen.add(key)
        out.append((first, last, her, gender))
        if len(out) >= n:
            break
    if len(out) < n:
        pool = [t for t in _FALLBACK_NAMES
                if _name_ok(t[0], t[1]) and f"{t[0]} {t[1]}".lower() not in seen]
        while len(out) < n and pool:
            t = pool.pop(secrets.randbelow(len(pool)))
            seen.add(f"{t[0]} {t[1]}".lower())
            out.append(t)
    return out[:n]


# ─── App names (casual, first-timer, occasional typos) ──────────────────────

_SYS_APPS = """You generate short, casual app names a FIRST-TIME developer would type when creating a throwaway test/developer app for the very first time — placeholder names someone in a hurry picks, not polished brands.

Style:
- Very casual/generic: e.g. "test app", "app one", "tester", "my app", "pilot app", "try out app", "app try", "testing app", "first app", "demo app", "app123", "test123", "sample app".
- Short (1-3 words), lowercase or mixed case, nothing branded.
- About 1 in 4 should contain a small REALISTIC human typo (e.g. "tset app", "aplication test", "tester ap", "test aap", "myy app").
- Vary widely; never repeat.

Output strictly as JSON: {"apps": ["...", ...]}  ({N} items)"""

_FALLBACK_APPS = [
    'test app', 'app one', 'tester', 'my app', 'pilot app', 'try out app',
    'app try', 'testing app', 'first app', 'demo app', 'sample app', 'app123',
    'test123', 'tset app', 'tester ap', 'aplication test', 'test aap',
    'new app', 'my test app', 'app test', 'quick app', 'trial app',
]


def _gen_app_names(n):
    n = max(1, min(BATCH_MAX, n))
    try:
        raw = _cs._call_openai_json(_SYS_APPS, f"Generate {n} app names.", n) or []
    except Exception as e:
        logger.warning(f"[account_pack] app-name LLM failed: {e}")
        raw = []
    out = [str(x).strip() for x in raw if str(x).strip()]
    while len(out) < n:
        out.append(secrets.choice(_FALLBACK_APPS))
    return out[:n]


# ─── Gothic Facebook page names (per chosen reference model) ────────────────

_SYS_PAGENAME = """You generate GOTHIC Facebook PAGE NAMES for a content creator whose first name is given.

Each name = the exact first name + a dark/gothic flourish. Examples:
- "Carolina" → "Carolina Rose", "Carolina Bloom", "Carolina Dark", "Carolina Nightshade", "Carolina the Gothic Tempest", "Carolina Noir"
- "Kira" → "Kira Vamp", "Kira Bangs", "Kira Noir", "Kira Ravenna", "Kira Nightshade"

Style: dark, gothic, moody, feminine, a little mysterious — roses, thorns, night, shadow, velvet, raven, ash, lace, moon, storm, ember, hex. A believable page name (2-4 words). Vary widely; never repeat; no AI clichés like "ethereal"/"celestial".

VARIETY — CRITICAL: do NOT always start with the first name (that's repetitive). Roughly HALF should be standalone gothic names with NO first name at all — e.g. "Shadow Mistress", "Velvet Girl", "Nightshade Girl", "Dark Rose", "Gothic Tales", "Midnight Muse". The other half can start with the given first name. Mix the two.

If the user message lists SEED WORDS, weave ONE OR MORE of them into most names (with OR without the first name) so the page name feels connected to that handle (e.g. seeds "dark, blue, rose" → "Carolina Blue Rose", "Dark Blue Rose", "Blue Rose Noir"). Keep it gothic and natural, not a literal word dump.

Output strictly as JSON: {"names": ["<FirstName> ...", ...]}  ({N} items)"""

_GOTHIC_WORDS = [
    'Rose', 'Bloom', 'Dark', 'Nightshade', 'Noir', 'Raven', 'Ash', 'Velvet',
    'Thorn', 'Shadow', 'Moon', 'Storm', 'Vamp', 'Lace', 'Ember', 'Sable',
    'Hex', 'Crow', 'Ravenna', 'Wren', 'Nyx', 'Onyx', 'Dusk', 'Vesper',
    'Grave', 'Petal', 'Bane', 'Mist', 'Wraith', 'Bloomfield']

# Standalone gothic page names (NO model name) for variety — the page name
# should not always contain the model's first name.
_GOTHIC_STANDALONE = [
    'Shadow Mistress', 'Velvet Girl', 'Nightshade Girl', 'Dark Rose',
    'Gothic Tales', 'Midnight Muse', 'Raven Girl', 'Thorn & Lace',
    'Ember Witch', 'Moonlit Veil', 'Velvet Noir', 'Crimson Veil',
    'Little Nightshade', 'Rose & Thorn', 'Dark Bloom', 'Lace & Shadow']

# Facebook page category — one is picked at random per account so a VA can
# just select it during page creation.
_PAGE_CATEGORIES = ['Blogger', 'Personal Blog', 'Arts and Entertainment',
                    'Artist', 'Model']


_GOTHIC_SUFFIX = ['Girl', 'Mistress', 'Muse', 'Doll', 'Witch', 'Rose', 'Veil',
                  'Tales', 'Noir', 'Diaries', 'Heart', 'Kiss']


def _standalone_gothic(seeds=None):
    """A gothic page name with NO model first name — seed-aware when a handle
    is present. e.g. 'Shadow Mistress', 'Velvet Rose', 'Dark Blue Veil'."""
    if seeds and secrets.randbelow(2):
        s = secrets.choice(seeds).title()
        return f"{s} {secrets.choice(_GOTHIC_SUFFIX)}"
    roll = secrets.randbelow(3)
    if roll == 0:
        return secrets.choice(_GOTHIC_STANDALONE)
    if roll == 1:
        return f"{secrets.choice(_GOTHIC_WORDS)} {secrets.choice(_GOTHIC_SUFFIX)}"
    return f"{secrets.choice(_GOTHIC_WORDS)} & {secrets.choice(_GOTHIC_WORDS)}"


def _pick_page_category():
    return secrets.choice(_PAGE_CATEGORIES)


def _gen_bios(count):
    """One girlfriend-brand goth bio per account (reuses bio_gen_v2 backend)."""
    try:
        bios = _cs.suggest_bios_v2('Goth', 'default', max(count, 3),
                                   force_refresh=True) or []
    except Exception as e:
        logger.warning(f"[account_pack] bio gen failed: {e}")
        bios = []
    out = list(bios)
    while len(out) < count:
        out.append(secrets.choice(out) if out else '')
    return out[:count]


def _handle_tokens(handle):
    """dark-blue-rose → ['dark','blue','rose'] (drops empties/stopwords)."""
    if not handle:
        return []
    toks = [t for t in re.split(r'[-_\s]+', str(handle).strip().lower()) if t]
    stop = {'the', 'a', 'of', 'and', 'to', 'for'}
    return [t for t in toks if t not in stop]


def _gen_page_names(model_display, handle, count):
    """Return `count` (option1, option2) gothic page-name pairs for the model.

    If `handle` is given (e.g. 'dark-blue-rose'), its tokens seed the LLM so the
    page name is a play on the cloak-link handle. If empty/None, standalone
    gothic names (loose pairing)."""
    seeds = _handle_tokens(handle)
    seed_line = f"SEED WORDS: {', '.join(seeds)}\n" if seeds else ""
    need = count * 2
    user = (f"First name: {model_display}\n{seed_line}"
            f"Generate {need} gothic Facebook page names, each starting with "
            f"'{model_display}'"
            + (" and weaving in the seed words." if seeds else "."))
    try:
        raw = _cs._call_openai_json(_SYS_PAGENAME, user, need) or []
    except Exception as e:
        logger.warning(f"[account_pack] pagename LLM failed: {e}")
        raw = []
    names = [str(x).strip() for x in raw if str(x).strip()]
    while len(names) < need:
        names.append(f"{model_display} {secrets.choice(_GOTHIC_WORDS)}")
    # The LLM tends to prepend the first name to EVERYTHING → force variety by
    # flipping ~half the slots to standalone gothic names (no model name), and
    # dedup so a batch never shows the same page name twice.
    seen = set()
    for i in range(len(names)):
        if secrets.randbelow(2):
            names[i] = _standalone_gothic(seeds)
        base = names[i].lower()
        tries = 0
        while base in seen and tries < 8:
            names[i] = _standalone_gothic(seeds)
            base = names[i].lower()
            tries += 1
        seen.add(base)
    return [(names[2 * i], names[2 * i + 1]) for i in range(count)]


# ─── FB page setup: block countries + block words (per account) ─────────────

# India is ALWAYS blocked (most important); 2 more drawn from this pool.
_BLOCK_COUNTRY_POOL = ['Mexico', 'Brazil', 'Philippines', 'Pakistan']

# Legit curse words for comment-blocking — NOT GenZ slang. The list is only a
# menu; each account gets a small random subset so batches aren't identical.
_CURSE_WORDS = [
    'bitch', 'whore', 'slut', 'cock', 'dick', 'pussy', 'cunt', 'fuck',
    'shit', 'asshole', 'twat', 'skank', 'hoe', 'bastard', 'prick', 'wanker',
    'douche', 'jackass', 'slag', 'tramp',
]


def _sample(pool, k):
    """CSPRNG sample without replacement (no `random` import needed)."""
    pool = list(pool)
    out = []
    for _ in range(min(k, len(pool))):
        out.append(pool.pop(secrets.randbelow(len(pool))))
    return out


def _pick_blocked_countries():
    """India + 2 random others (3 total)."""
    return ['India'] + _sample(_BLOCK_COUNTRY_POOL, 2)


def _gen_blocked_words():
    """5-7 words: always includes 'ai' + 'slop', the rest legit curse words.
    The whole list is SHUFFLED so 'ai'/'slop' aren't always first / in the same
    order — these get copy-pasted, so a fixed order is a fingerprint."""
    k = 3 + secrets.randbelow(3)          # 3-5 → total 5-7
    words = ['ai', 'slop'] + _sample(_CURSE_WORDS, k)
    return _sample(words, len(words))     # sample-all = shuffle


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
    priv = (f"<a href=\"{e(rec['privacy_url'])}\">{e(rec['privacy_url'])}</a>"
            if rec.get('privacy_url') else "⚠️ not generated")
    is_backup = rec.get('kind') == 'backup_manager'
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>{e(rec['account'])}</b>  ({idx}/{count})"
        + ("  🗂 <i>backup manager</i>" if is_backup else ""),
        "━━━━━━━━━━━━━━━━━━",
        f"<b>Name:</b> <code>{e(rec['first'])} {e(rec['last'])}</code>",
        f"<b>Gender:</b> {e(rec['gender'])}",
        f"<b>Birthdate:</b> <code>{e(rec['birthdate_display'])}</code>  (age {rec['age']})",
        f"<b>Password:</b> <code>{e(rec['password'])}</code>",
        f"<b>Rambler:</b> {ramb}",
        f"<b>FB phone:</b> {phone}",
        f"<b>App name:</b> <code>{e(rec.get('app_name',''))}</code>",
        f"<b>Privacy policy:</b> {priv}",
    ]
    if not is_backup:
        lines.append(
            f"<b>FB page name:</b> <code>{e(rec.get('page_name_1',''))}</code>  /  "
            f"<code>{e(rec.get('page_name_2',''))}</code>")
    lines += [
        f"<b>Page category:</b> {e(rec.get('page_category',''))}",
        f"<b>Bio:</b> <code>{e(rec.get('bio',''))}</code>",
        f"<b>Block countries:</b> <code>{e(rec.get('block_countries',''))}</code>",
        f"<b>Block words:</b> <code>{e(rec.get('block_words',''))}</code>",
    ]
    return '\n'.join(lines)


def generate_packages(count, reserve, model, emit, post_one, handles=None):
    """Build `count` packages. `reserve` is the result of R.reserve(count).
    `model` is the reference-model display name (e.g. 'Carolina').
    `handles` (optional) is a per-account list of cloak-link handles so each
    gothic FB page name is a play on its handle; None → standalone (loose).
    Returns (records, phone_ok, balance, sheet_url, zip_bytes, zip_name)."""
    count = max(1, min(BATCH_MAX, count))
    is_backup = (model == BACKUP_MODEL)
    prefix = R.BACKUP_PREFIX if is_backup else R.NAME_PREFIX
    start = reserve['start']
    ramblers = reserve['ramblers']
    who = 'backup-manager account(s)' if is_backup else f'account(s) for model <b>{model}</b>'
    emit(f"🧩 building {count} {who} starting at "
         f"<b>{prefix} {start:03d}</b> — names + passwords first, then a real "
         f"7-day FB number each.")

    names = _gen_names(count, reserve.get('existing_names'))
    apps = _gen_app_names(count)
    bios = _gen_bios(count)
    # Page names: backup managers have NO Facebook page. Otherwise per-account
    # when handles are supplied (tight pairing), else a single batch (loose).
    if is_backup:
        page_pairs = [('', '')] * count
    elif handles:
        page_pairs = [_gen_page_names(model, (handles[i] if i < len(handles) else None), 1)[0]
                      for i in range(count)]
    else:
        page_pairs = _gen_page_names(model, None, count)
    pwds, _ai = password_gen.make_passwords(count)
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    records, phone_ok = [], 0
    for i in range(count):
        first, last, her, gender = names[i]
        iso, disp, age = random_birthdate()
        r_email, r_pw = ramblers[i] if i < len(ramblers) else (None, None)
        app_name = apps[i]
        pg1, pg2 = page_pairs[i]
        h = handles[i] if handles and i < len(handles) else None
        rec = {
            'account': f"{prefix} {start + i:03d}",
            'index': start + i,
            'model': '' if is_backup else model,
            'kind': 'backup_manager' if is_backup else 'primary',
            'handle': h or '', 'pairing': 'tight' if _handle_tokens(h) else 'loose',
            'first': first, 'last': last, 'heritage': her, 'gender': gender,
            'birthdate': iso, 'birthdate_display': disp, 'age': age,
            'password': pwds[i],
            'rambler_email': r_email or '', 'rambler_password': r_pw or '',
            'app_name': app_name, 'privacy_url': '',
            'page_name_1': pg1, 'page_name_2': pg2,
            'page_category': _pick_page_category(),
            'bio': bios[i],
            'block_countries': ', '.join(_pick_blocked_countries()),
            'block_words': ', '.join(_gen_blocked_words()),
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
        # Auto-generate a privacy policy for this app (best-effort).
        try:
            url, perr, _meta = privacy._create_privacy_policy_dispatch(app_name=app_name)
            rec['privacy_url'] = url or ''
            if perr:
                logger.warning(f"[account_pack] privacy gen for {app_name}: {perr}")
        except Exception as ex:
            logger.warning(f"[account_pack] privacy gen crash: {ex}")
        records.append(rec)
        post_one(_format_card(i + 1, count, rec))

    sheet_url, commit_err = R.commit(records, reserve['remaining_pool'],
                                     reserve['pool_fid'])
    if commit_err:
        emit(f"⚠️ tracker/sheet save issue: {commit_err}")
    return records, phone_ok, rental._balance_str(), sheet_url


# ─── Telegram: /account_pack ────────────────────────────────────────────────

BACKUP_MODEL = '__backup__'   # sentinel: backup-manager accounts (no FB page)


def _model_kb():
    rows = []
    try:
        for m in (cloak._known_models() or []):
            rows.append([InlineKeyboardButton(f"🖤 {m.title()}",
                        callback_data=f"acctpack:model:{m}")])
    except Exception as e:
        logger.warning(f"[account_pack] model list err: {e}")
    rows.append([InlineKeyboardButton("🗂 Backup manager (no FB page)",
                callback_data=f"acctpack:model:{BACKUP_MODEL}")])
    rows.append([InlineKeyboardButton("✖ cancel", callback_data="acctpack:cancel")])
    return InlineKeyboardMarkup(rows)


def _count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{n}", callback_data=f"acctpack:count:{n}")
         for n in COUNT_OPTIONS],
        [InlineKeyboardButton("✏️ Custom number", callback_data="acctpack:custom")],
        [InlineKeyboardButton("✖ cancel", callback_data="acctpack:cancel")],
    ])


async def account_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear any stale text-collecting session so it can't swallow our inputs.
    context.user_data.pop('batch_verify', None)
    context.user_data.pop('expecting_acctpack_count', None)
    balance, ramb = await asyncio.to_thread(
        lambda: (rental._balance_str(), R.rambler_count()))
    ramb_line = (f"📧 Rambler pool: <b>{ramb}</b> left"
                 if ramb is not None else
                 "📧 Rambler pool: <i>no rambler_pool.txt on Drive yet</i>")
    await update.message.reply_text(
        "🧩 <b>Account package generator</b>\n\n"
        "Each account = <b>FB META POSTER NNN</b> with a believable name + "
        "gender, a birthdate (age 25-40), a strong password, a Rambler email, a "
        "fresh <b>7-day Facebook number</b>, an app name + privacy link, and a "
        "gothic <b>FB page name</b> for the chosen model.\n\n"
        f"💰 balance: <code>{html.escape(balance)}</code> · {ramb_line}\n\n"
        "Pick the reference model 👇",
        parse_mode='HTML', reply_markup=_model_kb())


async def _run_batch(chat, context, count, model):
    count = max(1, min(BATCH_MAX, count))
    await chat.send_message(
        f"🧩 generating <b>{count}</b> account package(s)…", parse_mode='HTML')

    kind = 'backup_manager' if model == BACKUP_MODEL else 'primary'
    # Reserve numbering + rambler creds FIRST — abort if Drive is unreachable so
    # we never rent numbers we can't log or assign duplicate numbers.
    reserve = await asyncio.to_thread(R.reserve, count, kind)
    if not reserve['ok']:
        await chat.send_message(
            f"❌ aborted before renting anything — {html.escape(reserve['err'])}\n"
            f"Fix Google Drive access and try again.")
        return
    if not reserve['had_pool']:
        await chat.send_message(
            "ℹ️ no <code>rambler_pool.txt</code> found on Drive yet — accounts "
            "will be created without Rambler emails. Add the file "
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

    records, phone_ok, balance, sheet_url = await asyncio.to_thread(
        generate_packages, count, reserve, model, emit, post_one)

    # Deliver ONE combined .txt with every account (single file, easy to keep).
    import io as _io
    if len(records) == 1:
        body = R.account_txt(records[0])
        name = records[0]['account'].replace(' ', '_') + '.txt'
    else:
        body = R.combined_txt(records)
        last = records[-1]['account'].split()[-1]
        name = (records[0]['account'] + '-' + last).replace(' ', '_') + '.txt'
    doc = _io.BytesIO(body.encode('utf-8'))
    doc.name = name
    try:
        await context.bot.send_document(chat_id=chat.id, document=doc,
                                        filename=doc.name)
    except Exception as e:
        await chat.send_message(f"⚠️ couldn't attach the file: {e}")

    sheet_line = (f"📊 <a href=\"{sheet_url}\">Google Sheet (all accounts)</a>"
                  if sheet_url else "📊 sheet link unavailable")
    used = sum(1 for e, _p in reserve['ramblers'] if e)
    if reserve['had_pool']:
        ramb_line = (f"📧 Rambler pool: <b>{len(reserve['remaining_pool'])}</b> "
                     f"left (used {used})")
    else:
        ramb_line = "📧 Rambler pool: <i>no rambler_pool.txt on Drive</i>"
    await context.bot.send_message(
        chat_id=chat.id,
        text=(f"🎉 <b>Done</b> — {len(records)} account(s), {phone_ok} with a "
              f"live FB number.\n{sheet_line}\n"
              f"💰 balance: <code>{html.escape(balance)}</code>\n{ramb_line}\n\n"
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

    if action == 'model':
        raw = (parts[2] if len(parts) > 2 else '').strip()
        if raw == BACKUP_MODEL:
            model, label = BACKUP_MODEL, '🗂 Backup manager (no FB page)'
        else:
            model = raw.title() or 'Carolina'
            label = f"🖤 model: <b>{html.escape(model)}</b>"
        context.user_data['acctpack_model'] = model
        await q.edit_message_text(
            f"{label}\n\nHow many accounts?",
            parse_mode='HTML', reply_markup=_count_kb())
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
        model = context.user_data.get('acctpack_model') or 'Carolina'
        await q.edit_message_text(f"🧩 starting {count} account(s) for {model}…")
        await _run_batch(q.message.chat, context, count, model)
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
    model = context.user_data.get('acctpack_model') or 'Carolina'
    await _run_batch(update.message.chat, context, count, model)


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
