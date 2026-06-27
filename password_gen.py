"""/password — cryptographically strong password generator.

Built for opening accounts quickly: one command spits out a batch of strong
passwords, each tap-to-copy. Uses the `secrets` module (CSPRNG), guarantees at
least one lowercase, uppercase, digit and special char per password, and avoids
characters that signup forms commonly reject (quotes, backslash, spaces and
visually ambiguous glyphs like O/0, l/1/I).

Usage:
  /password            → 5 passwords, 16 chars each
  /password 8          → 8 passwords, 16 chars each
  /password 8 20       → 8 passwords, 20 chars each

No network, no storage — pure local generation.
"""
import secrets

from telegram import Update
from telegram.ext import ContextTypes

# Character pools. Ambiguous glyphs (O/0, l/1/I) are dropped so the passwords
# are still easy to read/type by hand when a form won't accept a paste.
LOWER = 'abcdefghijkmnpqrstuvwxyz'        # no 'l'
UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ'        # no 'I', 'O'
DIGITS = '23456789'                       # no '0', '1'
# Special set that Instagram / Facebook / Microsoft signup all accept.
SPECIAL = '!@#$%^&*-_=+?'

ALL = LOWER + UPPER + DIGITS + SPECIAL

# Defaults + guardrails.
DEFAULT_COUNT = 5
DEFAULT_LENGTH = 16
MAX_COUNT = 25
MIN_LENGTH = 8
MAX_LENGTH = 64


def generate_password(length=DEFAULT_LENGTH):
    """Return one strong password of `length` chars with at least one of each
    class (lower/upper/digit/special), positions shuffled with the CSPRNG."""
    length = max(MIN_LENGTH, min(MAX_LENGTH, length))
    # Guarantee one from each class, fill the rest from the full pool.
    chars = [
        secrets.choice(LOWER),
        secrets.choice(UPPER),
        secrets.choice(DIGITS),
        secrets.choice(SPECIAL),
    ]
    chars += [secrets.choice(ALL) for _ in range(length - len(chars))]
    # Fisher-Yates shuffle so the guaranteed chars aren't always up front.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return ''.join(chars)


def generate_batch(count=DEFAULT_COUNT, length=DEFAULT_LENGTH):
    """Return a list of `count` strong passwords."""
    count = max(1, min(MAX_COUNT, count))
    return [generate_password(length) for _ in range(count)]


def _parse_args(args):
    """Parse [count] [length] from the command args; fall back to defaults."""
    count, length = DEFAULT_COUNT, DEFAULT_LENGTH
    try:
        if len(args) >= 1:
            count = int(args[0])
        if len(args) >= 2:
            length = int(args[1])
    except (ValueError, TypeError):
        pass
    return count, length


# ─── Telegram command ──────────────────────────────────────────────────────

async def password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    count, length = _parse_args(context.args or [])
    pwds = generate_batch(count, length)
    # Each on its own line in a code block → tap-to-copy per line in Telegram.
    body = '\n'.join(f'`{p}`' for p in pwds)
    actual_len = len(pwds[0])
    caption = (
        f'🔑 *Strong passwords* — {len(pwds)} × {actual_len} chars\n\n'
        f'{body}\n\n'
        f'_Each has upper + lower + digit + special, CSPRNG-generated, '
        f'ambiguous chars (O/0, l/1/I) removed. Tap a line to copy._\n'
        f'_Usage: `/password [count] [length]` — e.g. `/password 8 20`._'
    )
    await msg.reply_text(caption, parse_mode='Markdown')
