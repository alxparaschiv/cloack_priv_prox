"""/blocklist — Instagram blocked-words generator with anti-cluster guard.

Always-anchored words: ai · slop · fake · fakeprofile.
Everything else is sampled from a curated five-category pool, optionally
perturbed by gpt-4o-mini for vocabulary novelty, and gated by a Jaccard
similarity check against the last 200 generated lists so no two outputs
share more than ~half their vocabulary. That last bit is the actual
guarantee against "I can tell these accounts came from the same bot."

Storage: history JSON at Drive root, name `acc-setup-bot · blocklist-history.json`.
Uses cloak's own GOOGLE_TOKEN_PICKLE (NOT the reel-bot one).

Env deps:
  - GOOGLE_TOKEN_PICKLE   — Drive auth for history persistence
  - OPENAI_API_KEY        — optional, only if BLOCKLIST_USE_LLM is on
  - BLOCKLIST_USE_LLM     — '1' (default) = LLM perturbation on, '0' = off
"""
import os
import io
import json
import time
import random
import base64
import pickle
import logging

import requests
from telegram import Update
from telegram.ext import ContextTypes

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)


# ─── Word pools ─────────────────────────────────────────────────────────────

ANCHORS = ['ai', 'slop', 'fake', 'fakeprofile']

HARD_SWEARS = [
    'motherfucker', 'cunt', 'dickhead', 'twat', 'bullshit', 'fucker',
    'asshole', 'bastard', 'piece of shit', 'jackass', 'prick', 'whore',
    'slut', 'douchebag', 'dipshit', 'bitch', 'fuckface',
]

MILD_INSULTS = [
    'scammer', 'sleazy', 'sketchy', 'fraud', 'shady', 'scumbag', 'dirtbag',
    'lowlife', 'grifter', 'weasel', 'snake', 'phony', 'pervert', 'creep',
    'dumbass', 'loser', 'troll', 'clown', 'drift', 'weird', 'gross', 'lame',
    'bogus', 'annoying', 'mimic', 'knockoff', 'copycat', 'impostor', 'poser',
    'clone', 'generated', 'synthetic', 'bot', 'spam',
]

SLANG = [
    'clanker', 'NPC', 'mid', 'cheugy', 'chopped', 'Ohio', 'delulu', 'cooked',
    'glazing', 'brainlet', '404 coded', 'basic', 'salty', 'sus', 'thirsty',
    'try-hard', 'lowkey shady',
]

WILDCARDS = [
    'rust', 'mildew', 'vinegar', 'sawdust', 'gristle', 'kelp', 'gravel',
    'lint', 'mossy', 'hatpin', 'ottoman', 'accordion', 'kettle', 'barnacle',
    'parsley', 'drywall', 'tinfoil', 'fluff', 'ashtray', 'soggy', 'fogbank',
    'brickdust', 'lukewarm', 'razorblade', 'threadbare', 'jellybean',
    'sandpaper', 'moss',
]

NON_ANCHOR_POOLS = {
    'hard_swears':  HARD_SWEARS,
    'mild_insults': MILD_INSULTS,
    'slang':        SLANG,
    'wildcards':    WILDCARDS,
}


# ─── Drive history (anti-cluster guard storage) ─────────────────────────────

HISTORY_FILE_NAME = 'acc-setup-bot · blocklist-history.json'
HISTORY_KEEP_LAST = 200


def _drive_service():
    raw = os.environ.get('GOOGLE_TOKEN_PICKLE')
    if not raw:
        raise RuntimeError('GOOGLE_TOKEN_PICKLE not set')
    creds = pickle.loads(base64.b64decode(raw))
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _find_history_file(svc):
    """Returns the Drive file id for the history JSON, or None if not yet created."""
    q = (f"name='{HISTORY_FILE_NAME}' and trashed=false "
         f"and mimeType='application/json'")
    res = svc.files().list(q=q, fields='files(id,name)',
                           supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    items = res.get('files') or []
    return items[0]['id'] if items else None


def _load_history():
    """Returns list[list[str]] of previous outputs. Empty on first run / failure."""
    try:
        svc = _drive_service()
        fid = _find_history_file(svc)
        if not fid:
            return []
        data = svc.files().get_media(fileId=fid).execute()
        parsed = json.loads(data.decode('utf-8'))
        return parsed.get('lists') or []
    except Exception as e:
        logger.warning(f'[blocklist] history load failed: {e}')
        return []


def _save_history(lists):
    """Write the trimmed history list back to Drive."""
    try:
        svc = _drive_service()
        fid = _find_history_file(svc)
        body_bytes = json.dumps(
            {'lists': lists[-HISTORY_KEEP_LAST:],
             'updated_at': int(time.time())},
            indent=2).encode('utf-8')
        media = MediaIoBaseUpload(io.BytesIO(body_bytes),
                                   mimetype='application/json',
                                   resumable=False)
        if fid:
            svc.files().update(fileId=fid, media_body=media,
                               supportsAllDrives=True).execute()
        else:
            svc.files().create(
                body={'name': HISTORY_FILE_NAME,
                      'mimeType': 'application/json'},
                media_body=media, fields='id',
                supportsAllDrives=True).execute()
        return True
    except Exception as e:
        logger.warning(f'[blocklist] history save failed: {e}')
        return False


# ─── Candidate generator (pure Python) ──────────────────────────────────────

def _multinomial_split(total, n_categories):
    """Split `total` into n_categories non-negative ints, randomly. Sums to total."""
    if total <= 0:
        return [0] * n_categories
    cuts = sorted(random.sample(range(total + n_categories - 1),
                                  n_categories - 1)) \
           if n_categories > 1 else []
    sizes = []
    prev = 0
    for c in cuts:
        sizes.append(c - prev)
        prev = c + 1
    sizes.append(total + n_categories - 1 - prev)
    return sizes


def _generate_candidate(target_length):
    """Pull a stack of `target_length` words from the pools. Anchors always
    present and scattered at random positions in the final list."""
    target_length = max(len(ANCHORS) + 1, target_length)
    remainder = target_length - len(ANCHORS)
    splits = _multinomial_split(remainder, len(NON_ANCHOR_POOLS))
    pool_keys = list(NON_ANCHOR_POOLS.keys())
    random.shuffle(pool_keys)  # randomize category order each call

    picked = []
    for k, n in zip(pool_keys, splits):
        pool = NON_ANCHOR_POOLS[k]
        n = min(n, len(pool))
        picked += random.sample(pool, n)

    # Shuffle non-anchors then thread anchors in at random positions.
    random.shuffle(picked)
    out = list(picked)
    insert_positions = sorted(random.sample(range(len(out) + len(ANCHORS)),
                                              len(ANCHORS)))
    anchors_shuffled = random.sample(ANCHORS, len(ANCHORS))
    # Insert in reverse so positions stay valid as we grow.
    for pos, word in zip(reversed(insert_positions), reversed(anchors_shuffled)):
        out.insert(pos, word)
    return out


# ─── LLM perturbation (optional) ────────────────────────────────────────────

def _llm_perturb(words):
    """Ask gpt-4o-mini to swap 2-4 non-anchor words for natural synonyms.
    Returns the perturbed list, or `words` unchanged on any failure."""
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return words
    system = (
        "You substitute words in a list of Instagram blocked-words. "
        "RULES: swap 2-4 NON-ANCHOR words for natural-feeling synonyms or "
        "near-equivalents that a real (non-bot) person would actually use. "
        "NEVER touch the anchor words: ai, slop, fake, fakeprofile. "
        "NEVER add AI cliches (ethereal/celestial/dreams/vibes/etc.). "
        "Keep the same number of words. Lowercase preferred. "
        'Output STRICTLY JSON: {"words": ["...", "...", ...]}'
    )
    user = (f"List ({len(words)} words):\n" + ", ".join(words)
            + "\n\nSwap 2-4 non-anchor words for natural synonyms.")
    try:
        r = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}',
                     'Content-Type': 'application/json'},
            json={
                'model': 'gpt-4o-mini',
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': user}],
                'response_format': {'type': 'json_object'},
                'temperature': 1.0,
                'max_tokens': 600,
            }, timeout=20)
        if r.status_code != 200:
            logger.warning(f'[blocklist] LLM HTTP {r.status_code}: {r.text[:200]}')
            return words
        parsed = json.loads(r.json()['choices'][0]['message']['content'])
        out = parsed.get('words') or []
        if not isinstance(out, list) or len(out) < len(words) - 2:
            return words
        out = [str(w).strip() for w in out if isinstance(w, (str, int, float))]
        # Hard-enforce anchors regardless of LLM behavior.
        anchors_lower = {a.lower() for a in ANCHORS}
        out_lower = {w.lower() for w in out}
        missing = [a for a in ANCHORS if a.lower() not in out_lower]
        if missing:
            out += missing
        return out
    except Exception as e:
        logger.warning(f'[blocklist] LLM perturb failed: {e}')
        return words


# ─── Anti-cluster guard ─────────────────────────────────────────────────────

def _jaccard(a, b):
    sa, sb = set(w.lower() for w in a), set(w.lower() for w in b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _max_jaccard_vs(history, candidate):
    if not history:
        return 0.0
    return max(_jaccard(prev, candidate) for prev in history)


# ─── Orchestrator ───────────────────────────────────────────────────────────

def generate_blocklist(use_llm=None, max_jaccard=0.45, max_tries=12):
    """Generate a single blocked-words list with the anti-cluster guard.

    Returns (words, meta) where meta is a dict with:
      length, max_jaccard_vs_history, llm_used, tries, history_size.
    """
    if use_llm is None:
        use_llm = os.environ.get('BLOCKLIST_USE_LLM', '1') == '1'
    history = _load_history()
    best = None
    best_score = 2.0
    tries = 0
    for tries in range(1, max_tries + 1):
        target_len = random.randint(10, 55)
        cand = _generate_candidate(target_len)
        if use_llm:
            cand = _llm_perturb(cand)
        score = _max_jaccard_vs(history, cand)
        if score < best_score:
            best, best_score = cand, score
        if score <= max_jaccard:
            break
    history.append(best)
    _save_history(history)
    meta = {
        'length': len(best),
        'max_jaccard_vs_history': round(best_score, 3),
        'llm_used': use_llm,
        'tries': tries,
        'history_size': len(history),
    }
    return best, meta


# ─── Telegram command ──────────────────────────────────────────────────────

async def blocklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await msg.reply_text('🚫 generating blocked-words list…')
    import asyncio
    try:
        words, meta = await asyncio.to_thread(generate_blocklist)
    except Exception as e:
        await msg.reply_text(f'❌ generation failed: `{type(e).__name__}: {e}`',
                              parse_mode='Markdown')
        return
    body = ', '.join(words)
    llm_label = 'on' if meta['llm_used'] else 'off'
    caption = (
        f'🚫 *Blocked-words list*\n\n'
        f'`{body}`\n\n'
        f'• {meta["length"]} words\n'
        f'• max Jaccard vs last {meta["history_size"]}: '
        f'*{meta["max_jaccard_vs_history"]}* (lower = more unique)\n'
        f'• LLM perturbation: {llm_label} · tries: {meta["tries"]}\n\n'
        f'_Tap-to-copy the line above and paste into IG → Settings → Hidden '
        f'Words → Custom words._'
    )
    await msg.reply_text(caption, parse_mode='Markdown')
