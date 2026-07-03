"""
Cloak wizard AI suggestion generator.

Provides handle / overlay-text / bio suggestions for the /cloak new wizard
so the user picks from a clickable shortlist instead of typing each text
field. Backed by OpenAI gpt-4o-mini (~$0.001 per batch of 8).

System prompts bake in the "character-driven / slightly toxic / internet-
native" tone from the user's reference example ("will arrest you if you
don't behave") — NOT the cliche poetic AI-aesthetic style.

Public functions:
    suggest_slugs(niche, model_name, n=8, force_refresh=False)
    suggest_overlay_text(niche, model_name, n=8, force_refresh=False)
    suggest_display_names(niche, model_name, n=8, force_refresh=False)
    suggest_bios(niche, handle, n=8, force_refresh=False)

Each returns a list of strings. Cached in-memory for 10 min per
(kind, niche, ...) tuple so rapid refresh doesn't waste API calls.
Pass force_refresh=True to bypass cache (the wizard's 🔄 button).

Falls back to per-niche local pools if OPENAI_API_KEY is unset or the
API call fails — bot stays usable offline.
"""

import os
import json
import time
import logging
import random as _random

import requests

logger = logging.getLogger(__name__)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

# 10-min in-memory cache to avoid hammering OpenAI on rapid clicks.
_CACHE = {}      # key=(kind, niche, model_or_handle) → (timestamp, [strs])
_CACHE_TTL = 600


# ─── System prompts ──────────────────────────────────────────────────────

_SYS_SLUG = """You generate Instagram-style handle suggestions for a content creator's bio-link page.

TONE — extremely online, character-driven, slightly toxic/flirty, internet-native. Match the user's chosen aesthetic niche.

EXAMPLE STYLES (mix these depending on niche):
- Witchy/fairycore: moonwaterdiary, mistycauldron, faerievelvet, honeyspellbook, thymeandtarot
- Y2K vampire: dialupdesire, myspacevampire, vampkoi.exe, bloodkiss.mp3, crimsonchatroom
- Goth-internetcore: graveyardgoldfish, cemeterymilkshake, mothdustbunny, cursedpetstore, vampkoi.exe
- Gamer-internet: laggingnecromancer, glitchedsuccubus, criticalhitbunny, voidspawnkitty, respawnwitch.exe
- Ultra-rare one-word: velunara, nytheria, astravyn, obsqyra, vaesera, mythrine, soluneth
- Soft influencer rare: lovellea, miralune, evarelle, caelisse, aurelisse, ambrielle

RULES:
- Lowercase only
- 4-25 chars
- Allowed chars: letters / digits / _ / - / . (Instagram-handle compatible)
- NO underscores at start, NO numbers like "99" or "88" unless part of an .exe/.mp3-style suffix
- Should feel like a REAL handle a 20-something would actually use, NOT a brand or fantasy name
- AVOID AI clichés ("ethereal", "celestial", "divine", "moonlit", "enchanted")

Output strictly: {"handles": ["...", "...", ...]}  (JSON, {N} items)"""


_SYS_OVERLAY = """You generate ultra-short overlay text for OnlyFans bio-link landing pages.

This text sits on top of the OF teaser card image — the first thing visitors read.

TONE — short, punchy, character-driven, slightly toxic/flirty, internet-native. Niche-aware.

EXAMPLES THAT WORK (and the niche they were for):
- Police: "officer wants to chat", "you're under arrest 👮‍♀️"
- Goth: "kiss me bite me", "midnight only 🖤"
- Gamer: "respawn?", "main character 💀"
- Nun: "forbidden content ✝️", "say one hail mary"
- Schoolgirl: "after class? 📚", "extra credit 💋"
- Nurse: "open wide 💉", "house call?"

RULES:
- 1-4 words MAX (this fits on a card overlay)
- Emojis welcome — 1-2 per overlay
- Implied interaction with viewer ("you", "us", "now")
- A power dynamic, joke, or tease — NEVER poetic AI fluff
- AVOID: "dreams", "vibes only", "aesthetic", "soft", "ethereal"

Output strictly: {"overlays": ["...", "...", ...]}  (JSON, {N} items)"""


_SYS_DISPLAY = """You generate short display-name suggestions for a content creator's bio-link landing page.

This is the BIG bold name shown under the OF card — a step softer than the handle, used like "Alex" or "Caro" or "Vampy Kira".

TONE — friendly, slightly playful, niche-aware. Often the model's first name + a niche flavor word, OR a short character-name.

EXAMPLES:
- "Vampy Kira", "Caro", "Alex 🩸", "Goth Lina", "Officer K", "Sister Vera"

RULES:
- 1-3 words MAX (10-25 chars)
- Letters + spaces + emojis OK
- Should feel personal — NOT a brand
- AVOID Pinterest-poetry words

Output strictly: {"names": ["...", "...", ...]}  (JSON, {N} items)"""


_SYS_BIO = """You generate Instagram-bio-style taglines for a content creator's bio-link landing page.

THIS IS THE MOST IMPORTANT FIELD. The bio is what makes a visitor laugh / get curious / tap the OF link.

TONE — short (5-12 words), character-driven, slightly toxic/flirty, internet-native, ACCIDENTALLY funny, weirdly specific. Sounds like a real person joking, NOT a "poetic AI Pinterest quote".

REFERENCE EXAMPLES (and the niche they were for):
- Police: "will arrest you if you don't behave"
- Goth: "hexing people recreationally"
- Goth: "evil but aesthetically pleasing"
- Goth: "church fears me slightly"
- Nun: "spiritually unavailable"
- Nun: "nun aesthetic without the morality"
- Nun: "forbidden in several monasteries"
- Gamer: "your main character but worse"
- Normal: "will ruin your attention span"
- Normal: "your mom would like me"
- Normal: "certified eye contact avoider"
- Normal: "unfortunately always online"
- Schoolgirl: "always after detention"
- Goth: "smoke break behind the cathedral"

THE "SAUCE":
- Inside joke + tiny power dynamic + repostable/memorable
- Implied interaction with viewer
- Or self-deprecating humor that lands

RULES:
- 5-12 words
- AVOID: "ethereal", "vibes", "aesthetic", "dancing", "moonlit", "soft mornings",
  "documenting moments", "playlists and chaos", "romanticizing"
- AVOID Pinterest-poetry sentence structure
- Each line should feel like it could caption a selfie

Output strictly: {"bios": ["...", "...", ...]}  (JSON, {N} items)"""


_SYS_BIO_V2 = """You generate short, intimate "girlfriend-brand" Instagram bio taglines for a content creator's bio-link landing page.

GOAL: make the visitor feel like SHE is already HIS girlfriend — a warm, flirty, parasocial bond. The bio quietly claims the "your #1 favorite <niche> girl / girlfriend" territory, tied to the niche's fantasy. Cheesy ON PURPOSE, but in a bonding, endearing, makes-him-feel-chosen way.

TONE — short (4-10 words), second-person, sweet + a little flirty, cutely possessive ("your", "yours", "for you"), niche-flavored. Warm cliché, NOT mean, NOT "toxic", NOT a poetic Pinterest quote.

REFERENCE EXAMPLES (and niche):
- Goth: "not your typical goth girlfriend"
- Goth: "your favorite goth girl"
- Goth: "the goth gf your mom warned you about"
- Police: "we'll have to handcuff you if you misbehave"
- Police: "your favorite officer, off duty for you"
- Nurse: "i'll take good care of you"
- Gamer: "the player two you've been waiting for"
- Nun: "praying you slide into my dms"
- Teacher: "detention with me isn't a punishment"
- Normal: "the girl next door, but yours"

THE "SAUCE":
- Parasocial ownership: "your", "yours", "for you"
- Girlfriend framing fused with the niche's role/fantasy (uniform, dynamic, setting)
- Warm + a little teasing — bonding, not distancing
- Feels like she picked HIM

RULES:
- 4-10 words
- Lean on "your ... girlfriend / girl" framing wherever it fits
- Always tie to the niche's role/fantasy
- AVOID: "ethereal", "vibes", "aesthetic", "moonlit", "soft mornings", Pinterest-poetry
- Each line should caption a soft selfie and make him feel chosen

Output strictly: {"bios": ["...", "...", ...]}  (JSON, {N} items)"""


# ─── OpenAI call ─────────────────────────────────────────────────────────

def _call_openai_json(system_prompt, user_prompt, n=8,
                      model='gpt-4o-mini', timeout=20):
    """Call OpenAI chat-completions with JSON output. Returns list of strings
    extracted from the first array-valued key in the response, or []."""
    if not OPENAI_API_KEY:
        return []
    try:
        resp = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [
                    {'role': 'system',
                     'content': system_prompt.replace('{N}', str(n))},
                    {'role': 'user', 'content': user_prompt},
                ],
                'response_format': {'type': 'json_object'},
                'temperature': 1.0,   # high variance per call
                'max_tokens': 500,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"[cloak-sugg] OpenAI {resp.status_code}: "
                           f"{resp.text[:200]}")
            return []
        content = resp.json()['choices'][0]['message']['content']
        parsed = json.loads(content)
        # Extract the first list-valued field (handles / overlays / names / bios)
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return [str(x).strip() for x in v
                            if isinstance(x, (str, int, float))][:n]
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed
                    if isinstance(x, (str, int, float))][:n]
        return []
    except Exception as e:
        logger.warning(f"[cloak-sugg] OpenAI call failed: {e}")
        return []


def _cache_get(key):
    rec = _CACHE.get(key)
    if not rec:
        return None
    ts, vals = rec
    if time.time() - ts > _CACHE_TTL:
        del _CACHE[key]
        return None
    return vals


def _cache_put(key, vals):
    _CACHE[key] = (time.time(), vals)


# ─── Public API ──────────────────────────────────────────────────────────

def suggest_slugs(niche, model_name, n=8, force_refresh=False):
    """Returns list of n handle suggestions for (niche, model)."""
    key = ('slug', (niche or '').lower(), (model_name or '').lower())
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return cached
    user = (f"Niche: {niche}\n"
            f"Model first-name (for inspiration only, don't include literally): {model_name}\n\n"
            f"Generate {n} handle suggestions for this niche. "
            f"Mix the example styles — some aesthetic-niche, "
            f"some ultra-rare one-word, some chaotic-internet-cute.")
    out = _call_openai_json(_SYS_SLUG, user, n)
    if not out:
        out = _local_fallback_slugs(niche, n)
    _cache_put(key, out)
    return out


def suggest_overlay_text(niche, model_name, n=8, force_refresh=False):
    """Returns list of n overlay-text suggestions for (niche, model)."""
    key = ('overlay', (niche or '').lower(), (model_name or '').lower())
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return cached
    user = (f"Niche: {niche}\nModel: {model_name}\n\n"
            f"Generate {n} overlay-text options (1-4 words each, emojis OK).")
    out = _call_openai_json(_SYS_OVERLAY, user, n)
    if not out:
        out = _local_fallback_overlays(niche, n)
    _cache_put(key, out)
    return out


def suggest_display_names(niche, model_name, n=8, force_refresh=False):
    """Returns list of n display-name suggestions."""
    key = ('display', (niche or '').lower(), (model_name or '').lower())
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return cached
    user = (f"Niche: {niche}\nModel first-name: {model_name}\n\n"
            f"Generate {n} display-name options. Often '{model_name}' + "
            f"niche flavor (e.g. 'Goth {model_name}', '{model_name} 🩸') OR "
            f"a short character name. 1-3 words each.")
    out = _call_openai_json(_SYS_DISPLAY, user, n)
    if not out:
        out = _local_fallback_display(niche, model_name, n)
    _cache_put(key, out)
    return out


def suggest_bios(niche, handle, n=8, force_refresh=False):
    """Returns list of n bio taglines for (niche, handle)."""
    key = ('bio', (niche or '').lower(), (handle or '').lower())
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return cached
    user = (f"Niche: {niche}\nHandle: {handle}\n\n"
            f"Generate {n} bio taglines for THIS handle in THIS niche. "
            f"Match the tone of the reference examples — character-driven, "
            f"slightly toxic, accidentally funny.")
    out = _call_openai_json(_SYS_BIO, user, n)
    if not out:
        out = _local_fallback_bios(niche, n)
    _cache_put(key, out)
    return out


def suggest_bios_v2(niche, handle, n=8, force_refresh=False):
    """Girlfriend-brand / intimate-bonding bio variant (see _SYS_BIO_V2).
    Separate cache namespace so it never collides with suggest_bios."""
    key = ('bio_v2', (niche or '').lower(), (handle or '').lower())
    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            return cached
    user = (f"Niche: {niche}\nHandle: {handle}\n\n"
            f"Generate {n} intimate girlfriend-brand bio taglines for THIS "
            f"handle in THIS niche. Match the reference tone — warm, flirty, "
            f"'your #1 favorite {niche} girlfriend', a little cheesy but "
            f"bonding, always tied to the niche's fantasy.")
    out = _call_openai_json(_SYS_BIO_V2, user, n)
    if not out:
        out = _local_fallback_bios(niche, n)
    _cache_put(key, out)
    return out


# ─── Local fallbacks (no-API mode) ───────────────────────────────────────
# Per-niche pools so the bot stays usable if OPENAI_API_KEY is unset or
# the API call fails. Quality is lower than the API path but acceptable.

_FALLBACK_SLUGS = {
    'default':  ['moonwaterdiary', 'velunara', 'nytheria', 'crybabykraken',
                 'mothdustbunny', 'astravyn', 'obsqyra', 'soluneth',
                 'vaesera', 'lovellea', 'graveyardgoldfish', 'cemeterymilkshake'],
    'goth':     ['graveyardgoldfish', 'cemeterymilkshake', 'vampkoi.exe',
                 'cursedpetstore', 'velvetcurse', 'mothdustbunny',
                 'hexedhoneybee', 'crimsonchatroom'],
    'police':   ['officermistakes', 'badge21', 'copbabe', 'cuff.me.softly',
                 'precinct12', 'sirenchatroom', 'arrestme.exe', 'wardenwhims'],
    'gamer':    ['laggingnecromancer', 'respawnwitch.exe', 'glitchedsuccubus',
                 'criticalhitbunny', 'voidspawnkitty', 'gameoverfairy',
                 'manadrainangel', 'ragequitfaerie'],
    'nun':      ['silencednun', 'cathedralcrush', 'prayerwhispers',
                 'veiledconfession', 'cloister.exe', 'forbiddenchapel',
                 'crucifixcoven', 'penancepretty'],
    'cyberpunk':['neondoll', 'chromeangel.exe', 'glitchsucc', 'cyberbabe2049',
                 'midnightprotocol', 'voidsiren', 'arcadevamp', 'pixeldoll'],
    'schoolgirl':['afterclass', 'detentiondoll', 'plaidpoison', 'studyhallsiren',
                  'aplusbadbabe', 'lockerlolita', 'recess.exe', 'extracredit'],
}

_FALLBACK_OVERLAYS = {
    'default':  ['OFF 💦😘', 'tap me', 'you wish 👀', 'see more 🔓',
                 'unlock 💋', 'mine? 🩷', 'on tap 🔥', 'open 💌'],
    'goth':     ['kiss + bite 🖤', 'midnight only', 'spell me 🕯', 'cursed 💀',
                 'unholy hours', 'pray for me', 'cathedral hours', 'sin softly'],
    'police':   ['under arrest 👮‍♀️', 'pat down? 🚨', 'cuff me', 'badge.exe',
                 'detention 💋', 'siren only', 'frisk 🔓', 'speak up'],
    'gamer':    ['respawn? 🎮', 'main char 💀', 'GG no re', 'loot drop 💋',
                 'crit hit 🔥', 'level 18+', 'press start', 'AFK never'],
    'nun':      ['forbidden ✝️', 'confess 🖤', 'sin softly', 'pray for me',
                 'holy hours', 'veiled 💋', 'amen 🩸', 'cloister.exe'],
    'schoolgirl':['after class? 📚', 'extra credit 💋', 'A+ student', 'recess 🩷',
                  'late again', 'study buddy', 'detention 💀', 'top of class 🔥'],
}

_FALLBACK_DISPLAY = ['{name}', '{name} 🩸', 'goth {name}', '{name} after dark',
                     'lil {name}', '{name}.exe', 'vampy {name}', '{name} 🖤']

_FALLBACK_BIOS = {
    'default':  ['will ruin your attention span',
                 'probably ignoring your text right now',
                 'bad influence with good outfits',
                 'romanticizing absolutely nothing',
                 'certified eye contact avoider',
                 'unfortunately always online',
                 'your mom would like me',
                 'i make everything awkward on purpose'],
    'goth':     ['hexing people recreationally',
                 'evil but aesthetically pleasing',
                 'church fears me slightly',
                 "don't worry the curse is temporary",
                 'probably banned from your village',
                 'medieval behavior in 4k',
                 'will bite if disrespected',
                 'pretty sure this is sacrilegious'],
    'police':   ['will arrest you if you don\'t behave',
                 'badge first, questions never',
                 'officially off-duty for you',
                 'cuff me back, i dare you',
                 'reading you your rights softly',
                 'public menace with a badge',
                 'speaking purely as an officer',
                 'this conversation is being recorded'],
    'nun':      ['forbidden in several monasteries',
                 'spiritually unavailable',
                 'nun aesthetic without the morality',
                 'looks holy makes bad decisions',
                 'probably whispering latin somewhere',
                 'emotionally in a cathedral',
                 'not beating the gothic allegations',
                 'devotion issues'],
    'gamer':    ['your main character but worse',
                 'will ragequit your relationship',
                 'patch notes: i got hotter',
                 'speedrunning bad decisions',
                 'AFK from your problems',
                 'critically online',
                 'achievement unlocked: ignored you',
                 'on cooldown emotionally'],
    'schoolgirl':['always after detention somehow',
                  'top of class in chaos',
                  'late assignment, late everything',
                  'study hall confessional',
                  'plaid skirt diplomatic immunity',
                  'extra credit not for grades',
                  'class president of distraction',
                  'recess is the only A i need'],
}


def _local_fallback_slugs(niche, n):
    key = (niche or '').lower()
    pool = list(_FALLBACK_SLUGS.get(key, _FALLBACK_SLUGS['default']))
    _random.shuffle(pool)
    return pool[:n]


def _local_fallback_overlays(niche, n):
    key = (niche or '').lower()
    pool = list(_FALLBACK_OVERLAYS.get(key, _FALLBACK_OVERLAYS['default']))
    _random.shuffle(pool)
    return pool[:n]


def _local_fallback_display(niche, model, n):
    nm = (model or 'Caro').capitalize()
    pool = [t.format(name=nm) for t in _FALLBACK_DISPLAY]
    _random.shuffle(pool)
    return pool[:n]


def _local_fallback_bios(niche, n):
    key = (niche or '').lower()
    pool = list(_FALLBACK_BIOS.get(key, _FALLBACK_BIOS['default']))
    _random.shuffle(pool)
    return pool[:n]
