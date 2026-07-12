"""/password — human-friendly but strong passphrase generator.

Instead of random hero-glyph strings (which no human would ever type), this
builds readable word-based passwords — a few real words with exactly ONE
capital letter, a number, and one special char, e.g. `Purple-lantern-cactus47!`.

Primary path: an LLM (gpt-4o-mini) invents each password creatively, so there
is NO fixed dictionary to trace them back to and every batch is a fresh, unique
combination. If OPENAI_API_KEY is unset or the API fails, it falls back to a
local CSPRNG passphrase from the built-in word pool so /password always works.

Usage:
  /password            → 5 passphrases
  /password 8          → 8 passphrases
  /password 8 4        → 8 passphrases, ~4 words each (fallback path)

No storage — passwords are shown once, never persisted.
"""
import html
import asyncio
import logging
import secrets

from telegram import Update
from telegram.ext import ContextTypes

import cloak_suggestions as _cs   # reuse its OpenAI JSON helper

logger = logging.getLogger(__name__)

# Curated word pool — common, easy-to-type, unambiguous words (3-7 letters,
# nothing offensive or confusable). More words = more entropy per pick.
WORDS = [
    'apple', 'amber', 'anchor', 'arrow', 'autumn', 'bacon', 'badge', 'bamboo',
    'banana', 'basket', 'beacon', 'bear', 'berry', 'bishop', 'blossom', 'bottle',
    'boulder', 'branch', 'bridge', 'bronze', 'brook', 'bubble', 'buffalo', 'button',
    'cabin', 'cactus', 'candle', 'canvas', 'canyon', 'carbon', 'carpet', 'castle',
    'cedar', 'cherry', 'circus', 'clever', 'cliff', 'clover', 'cobalt', 'comet',
    'copper', 'coral', 'cosmic', 'cotton', 'cricket', 'crimson', 'crystal', 'cyclone',
    'daisy', 'dawn', 'delta', 'desert', 'diamond', 'dolphin', 'donut', 'dragon',
    'dune', 'eagle', 'ember', 'emerald', 'engine', 'falcon', 'feather', 'fennel',
    'fern', 'ferret', 'fiddle', 'firefly', 'flame', 'flint', 'forest', 'fossil',
    'foxglove', 'frost', 'galaxy', 'garden', 'garnet', 'giant', 'ginger', 'glacier',
    'granite', 'grape', 'gravel', 'guitar', 'hammer', 'harbor', 'harvest', 'hazel',
    'heron', 'hickory', 'honey', 'hunter', 'igloo', 'indigo', 'island', 'ivory',
    'jaguar', 'jasmine', 'jungle', 'juniper', 'kettle', 'kitten', 'koala', 'ladder',
    'lagoon', 'lantern', 'lemon', 'leopard', 'lily', 'lantern', 'lotus', 'lumber',
    'lunar', 'maple', 'marble', 'marigold', 'meadow', 'melon', 'meteor', 'midnight',
    'mint', 'mirror', 'monsoon', 'moss', 'mountain', 'mushroom', 'nectar', 'nickel',
    'noodle', 'nutmeg', 'oasis', 'ocean', 'olive', 'onyx', 'orange', 'orbit',
    'orchid', 'otter', 'oyster', 'palm', 'panda', 'parrot', 'peach', 'pebble',
    'pepper', 'phoenix', 'pigeon', 'pine', 'pixel', 'planet', 'plum', 'pocket',
    'pony', 'poppy', 'prairie', 'pretzel', 'puffin', 'pumpkin', 'purple', 'quartz',
    'quiver', 'rabbit', 'radish', 'rain', 'raven', 'ribbon', 'ridge', 'river',
    'robin', 'rocket', 'rose', 'ruby', 'saddle', 'saffron', 'sage', 'salmon',
    'sandy', 'sapphire', 'scarlet', 'shadow', 'shark', 'shell', 'silver', 'sketch',
    'sleigh', 'smoke', 'snail', 'sparrow', 'spruce', 'stag', 'starfish', 'stone',
    'storm', 'sugar', 'summer', 'sunset', 'sundae', 'sunny', 'swan', 'sycamore',
    'table', 'tangerine', 'teapot', 'temple', 'thistle', 'thunder', 'tiger', 'timber',
    'toast', 'tomato', 'topaz', 'tortoise', 'trumpet', 'tulip', 'tundra', 'turtle',
    'umbrella', 'valley', 'velvet', 'violet', 'walnut', 'walrus', 'wander', 'whale',
    'wheat', 'willow', 'window', 'winter', 'wombat', 'yarn', 'yellow', 'zebra',
    'zephyr', 'zigzag',
]
# De-dup while preserving determinism-free order (a couple of words repeat above).
WORDS = list(dict.fromkeys(WORDS))

DIGITS = '23456789'                # no 0/1 (avoid O/l confusion)
SPECIAL = '!@#$%^&*?'              # widely accepted on IG/FB/Microsoft signups
SEPARATORS = ['-', '_', '.']

# Defaults + guardrails.
DEFAULT_COUNT = 5
DEFAULT_WORDS = 3
MIN_WORDS = 2
MAX_WORDS = 5
MAX_COUNT = 25


def generate_password(n_words=DEFAULT_WORDS):
    """Return one passphrase: `n_words` common words joined by a separator,
    with exactly ONE capital (first letter of the first word), then a 2-digit
    number and one special char appended. e.g. `Purple-lantern-cactus47!`."""
    n_words = max(MIN_WORDS, min(MAX_WORDS, n_words))
    words = [secrets.choice(WORDS) for _ in range(n_words)]
    # Exactly one capital letter: capitalize the first word's first letter only.
    words[0] = words[0][0].upper() + words[0][1:]
    sep = secrets.choice(SEPARATORS)
    number = ''.join(secrets.choice(DIGITS) for _ in range(2))
    special = secrets.choice(SPECIAL)
    return sep.join(words) + number + special


def generate_batch(count=DEFAULT_COUNT, n_words=DEFAULT_WORDS):
    """Return a list of `count` local CSPRNG passphrases (fallback path)."""
    count = max(1, min(MAX_COUNT, count))
    return [generate_password(n_words) for _ in range(count)]


# ─── LLM path (primary) ─────────────────────────────────────────────────────
# An LLM invents each password, so there's no fixed word pool to trace back to
# and every batch is a fresh, creative, non-repeating set.

_ALLOWED_SPECIAL = set('!@#$%^&*?-_')

_SYS_PWD = """You generate strong, human-friendly PASSWORDS for creating brand-new online accounts.

Each password MUST be:
- Readable and typeable — built from real words / short memorable combos, NOT random glyph soup.
- Creative and UNIQUE across the whole list — vary the vocabulary WIDELY (nature, food, places, animals, objects, adjectives, verbs, moods…). Never reuse a word, theme, or structure between entries. Surprise me.
- Exactly ONE capital letter.
- Contain at least one digit AND at least one special character from: ! @ # $ % ^ & * ? - _
- 14 to 24 characters long, no spaces, no quotes, no backslash. Avoid ambiguous 0/O and 1/l/I.
- Strong enough that it isn't guessable (enough length + word variety).

Vary the STRUCTURE too — some word-word-word then number+special, some Word+number then word+special, some with the number/special tucked in the middle. Be unpredictable so no two look alike.

Output strictly: {"passwords": ["...", "...", ...]}  (JSON, {N} distinct items)"""


def _sanitize(pw):
    """Coerce an LLM password to the allowed alphabet + guarantee it has a
    digit and a special char (append if the model forgot). Returns cleaned
    string or None if unsalvageable."""
    if not pw or not isinstance(pw, str):
        return None
    # Drop spaces/quotes/backslash and anything non-allowed.
    keep = []
    for c in pw.strip():
        if c.isalnum() or c in _ALLOWED_SPECIAL:
            keep.append(c)
    s = ''.join(keep)
    # Map only ambiguous DIGITS (0/1) to safe ones — never touch letters, or we
    # would mangle real words (hello→he22o). Words are self-disambiguating.
    s = s.translate(str.maketrans('01', '87'))
    if len(s) < 8:
        return None
    if not any(c.isupper() for c in s):
        # capitalize first alpha → keeps it human, adds the required capital
        s = ''.join((c.upper() if (i == next((k for k, ch in enumerate(s) if ch.isalpha()), -1)) else c)
                    for i, c in enumerate(s))
    if not any(c.isdigit() for c in s):
        s += secrets.choice(DIGITS) + secrets.choice(DIGITS)
    if not any(c in _ALLOWED_SPECIAL for c in s):
        s += secrets.choice(SPECIAL)
    return s[:32]


def generate_batch_llm(count):
    """Ask the LLM for `count` creative passwords. Returns a de-duplicated list
    of sanitized strings (may be shorter than `count`), or [] on failure."""
    count = max(1, min(MAX_COUNT, count))
    try:
        raw = _cs._call_openai_json(_SYS_PWD, f"Generate {count} unique strong "
                                    f"passwords.", count) or []
    except Exception as e:
        logger.warning(f"[password] LLM gen failed: {e}")
        return []
    out, seen = [], set()
    for p in raw:
        s = _sanitize(p)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def make_passwords(count, n_words=DEFAULT_WORDS):
    """Primary=LLM, fallback/top-up=local. Returns (passwords, used_ai)."""
    count = max(1, min(MAX_COUNT, count))
    ai = generate_batch_llm(count)
    if len(ai) >= count:
        return ai[:count], True
    # Top up any shortfall (or full fallback) with the local generator.
    need = count - len(ai)
    combined = list(dict.fromkeys(ai + generate_batch(need, n_words)))[:count]
    return combined, bool(ai)


def _parse_args(args):
    """Parse [count] [n_words] from the command args; fall back to defaults."""
    count, n_words = DEFAULT_COUNT, DEFAULT_WORDS
    try:
        if len(args) >= 1:
            count = int(args[0])
        if len(args) >= 2:
            n_words = int(args[1])
    except (ValueError, TypeError):
        pass
    return count, n_words


# ─── Telegram command ──────────────────────────────────────────────────────

async def password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    count, n_words = _parse_args(context.args or [])
    count = max(1, min(MAX_COUNT, count))
    pwds, used_ai = await asyncio.to_thread(make_passwords, count, n_words)
    # HTML (not Markdown): passwords contain chars like & * _ that break
    # Telegram's legacy-Markdown parser. <code> gives per-line tap-to-copy.
    body = '\n'.join(f'<code>{html.escape(p)}</code>' for p in pwds)
    src = ('🤖 AI-generated (unique every time)' if used_ai
           else '🔒 local generator (set OPENAI_API_KEY for AI mode)')
    caption = (
        f'🔑 <b>Strong passwords</b> — {len(pwds)}\n\n'
        f'{body}\n\n'
        f'<i>{src}. Readable words, one capital + a number + a special char. '
        f'Easy to type, still strong. Tap a line to copy.</i>\n'
        f'<i>Usage: <code>/password [count]</code> — e.g. /password 10</i>'
    )
    await msg.reply_text(caption, parse_mode='HTML')
