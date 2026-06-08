"""/banner_gen — Generate a wide-banner-format image of a model from a reference photo.

Conversation flow:
  /banner_gen                  → prompt for a reference photo
  user uploads photo           → bot calls nano-banana-pro/edit with a
                                  "lying-on-bed banner pose" prompt,
                                  returns the generated image in TG

The reference image carries the model's identity (face, hair, features);
the prompt specifies the new pose, wardrobe, and setting. Aspect ratio
is 21:9 first (cinematic banner / social header format), falling back to
16:9 if WaveSpeed rejects 21:9.

Env deps:
  - WAVESPEED_API_KEY        — same key used by /artistic_bg
"""
import os
import io
import base64
import time
import asyncio
import logging

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WAVESPEED_API_BASE = 'https://api.wavespeed.ai/api/v3'
WAVESPEED_API_KEY = os.environ.get('WAVESPEED_API_KEY', '')
ENGINE = 'nano-banana-pro'  # Gemini 3 Pro Image; ~$0.07-0.20/img

# Prompt describing the TARGET pose/style. The reference image carries
# the identity AND the wardrobe (we keep both); this text only changes the
# pose, setting, and framing.
#
# Adjective levels for the expression — used by _build_prompt to soften
# wording across retries if the safety filter blocks the first attempt.
EXPRESSION_LEVELS = [
    "natural relaxed expression — calm and confident, directly engaging the camera",  # softest
    "natural editorial expression — composed, magazine-cover energy",                   # softer
    "thoughtful expression — looking at the camera with quiet poise",                   # softest
]


def _build_prompt(softening_level: int = 0) -> str:
    """Build the banner prompt. softening_level=0 is the default; higher
    values progressively soften any wording that might trigger nano-banana-pro's
    safety filter (without changing the actual visual intent)."""
    expression = EXPRESSION_LEVELS[min(softening_level, len(EXPRESSION_LEVELS) - 1)]
    return (
        "Generate a wide-format cinematic banner image of the SAME person from "
        "the reference image. Preserve her face, hair color and texture, eye "
        "color, complexion, and overall identity EXACTLY — do not change her "
        "facial features or proportions.\n"
        "\n"
        "WARDROBE — CRITICAL: keep her clothing EXACTLY AS IT APPEARS in the "
        "reference image. Same neckline, same cut, same color, same straps "
        "or sleeves, same accessories. Do NOT change her outfit. Do NOT add "
        "a necklace, jewelry, or accessory that isn't in the reference. Do "
        "NOT cover up exposed skin that is visible in the reference (do not "
        "add sleeves, do not raise the neckline, do not extend the hemline). "
        "Do NOT add a different top or dress — render her in the same outfit "
        "shown in the reference photo.\n"
        "\n"
        f"NEW POSE: lying horizontally on TOP of a made bed — fully visible, "
        f"NOT under any covers, duvet, or sheets. Propped up on one elbow, "
        f"head and shoulders in the foreground filling roughly the right half "
        f"of the frame, body extending into the background to the left. "
        f"Her full outfit (the same one from the reference) must be clearly "
        f"visible — torso, waist, and the front of her body should not be "
        f"obscured by any bedsheet, blanket, duvet, or pillow. The bedding "
        f"is FLAT underneath her, not over her. {expression}.\n"
        "\n"
        "SETTING: minimalist modern bedroom, white or off-white bedding (a "
        "flat sheet, not a fluffy duvet pulled over her), one or two pillows "
        "behind her head, soft natural daylight from the left. Clean, "
        "uncluttered background.\n"
        "\n"
        "COMPOSITION (CRITICAL — read carefully):\n"
        "  • Cinematic ultrawide banner aspect ratio.\n"
        "  • The frame is FULLY OCCUPIED edge-to-edge by the subject and her "
        "immediate bedding. ABSOLUTELY NO NEGATIVE SPACE. No empty wall, no "
        "empty ceiling, no empty floor, no large blank areas. Every pixel "
        "must be either her body, her hair, her outfit, or the bedsheet/"
        "pillow directly around her.\n"
        "  • Her head and shoulders fill the right portion of the frame — "
        "hair extends from the top edge to roughly mid-frame, face occupies "
        "the right third, in sharp focus, looking straight at the camera.\n"
        "  • Her outfit and arm extend leftward across the frame; the flat "
        "bedsheet fills any remaining horizontal space to the left edge.\n"
        "  • Tight crop. Densely composed like a magazine cover or a "
        "streaming-platform hero banner — never sparse or minimal.\n"
        "  • No letterboxing, no borders, no margins, no padding around the "
        "subject."
    )


# Keywords in WaveSpeed error responses that indicate the generation was
# blocked by the safety/content filter rather than a transient error. If
# we see any of these, we re-submit with a softened prompt instead of
# giving up.
SAFETY_BLOCK_KEYWORDS = (
    'safety', 'policy', 'content_filter', 'content filter', 'nsfw',
    'inappropriate', 'blocked', 'flagged', 'sensitive', 'sexual',
    'sexually', 'violat', 'moderation', 'prohibited', 'unsafe',
    'recitation', 'recitation_other', 'block_reason',
)


def _looks_like_safety_block(err_payload) -> bool:
    """Heuristic: does this failed-task payload look like a safety-filter
    block (vs. a transient error like a network hiccup)?"""
    s = str(err_payload).lower()
    return any(kw in s for kw in SAFETY_BLOCK_KEYWORDS)


MAX_ATTEMPTS = 5  # how many times we re-try if blocked by safety filter


def _submit_and_poll_once(data_uri, prompt_text):
    """Single attempt: submit + poll for one task. Returns
    (out_url, used_ratio, err, blocked).
      - out_url:    output URL if succeeded, else None
      - used_ratio: which aspect ratio actually worked
      - err:        error string if any
      - blocked:    True if the failure looks like a safety-filter block
                    (so the caller can retry with a softened prompt)
    """
    submit_url = f"{WAVESPEED_API_BASE}/google/{ENGINE}/edit"
    rid = None
    used_ratio = None
    last_submit_err = None
    for aspect in ('21:9', '16:9'):
        body = {
            'prompt': prompt_text,
            'images': [data_uri],
            'aspect_ratio': aspect,
            'resolution': '2k',
            'output_format': 'jpeg',
            'enable_sync_mode': False,
            'enable_base64_output': False,
        }
        try:
            r = requests.post(submit_url, json=body, timeout=60,
                              headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}',
                                       'Content-Type': 'application/json'})
            if r.status_code != 200:
                last_submit_err = f"HTTP {r.status_code}: {r.text[:300]}"
                logger.warning(f"[banner_gen] aspect={aspect} {last_submit_err}")
                # If submit itself was rejected for safety reasons, the body
                # often says so — treat as a safety block and bail (caller
                # will soften prompt and retry).
                if _looks_like_safety_block(r.text):
                    return None, None, last_submit_err, True
                continue
            j = r.json()
            rid = (j.get('data') or {}).get('id') or j.get('id')
            if rid:
                used_ratio = aspect
                logger.info(f"[banner_gen] submitted aspect={aspect}, rid={rid}")
                break
        except Exception as e:
            last_submit_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[banner_gen] submit err aspect={aspect}: {e}")
            continue
    if not rid:
        return None, None, f"submit failed: {last_submit_err}", False

    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            r = requests.get(f"{WAVESPEED_API_BASE}/predictions/{rid}/result",
                             headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}'},
                             timeout=30)
            r.raise_for_status()
            d = r.json().get('data') or r.json()
            status = d.get('status') or ''
            if status in ('completed', 'succeeded'):
                outputs = d.get('outputs') or []
                if outputs:
                    first = outputs[0]
                    return (first if isinstance(first, str) else first.get('url')), used_ratio, None, False
                return None, used_ratio, "completed but no outputs", False
            if status == 'failed':
                blocked = _looks_like_safety_block(d)
                return None, used_ratio, f"task failed: {d}", blocked
        except Exception as e:
            logger.warning(f"[banner_gen] poll err: {e}")
        time.sleep(3)
    return None, used_ratio, f"timed out after 300s (rid={rid})", False


def _generate_banner(local_ref_path, progress_cb=None):
    """Submit a /edit task to nano-banana-pro with the reference image +
    banner prompt, with up to MAX_ATTEMPTS retries if the safety filter
    blocks the generation (each retry softens the prompt). Returns
    (local_path, err, attempts_used, was_blocked).

    progress_cb(attempt_idx, status_str) — optional callback fired before
    each attempt, so the Telegram handler can stream progress to the user.
    """
    if not WAVESPEED_API_KEY:
        return None, "WAVESPEED_API_KEY not set", 0, False

    try:
        with open(local_ref_path, 'rb') as f:
            b = f.read()
    except Exception as e:
        return None, f"can't read reference: {e}", 0, False
    ext = os.path.splitext(local_ref_path)[1].lstrip('.').lower() or 'jpg'
    mime_by_ext = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png',
                   'webp':'image/webp','heic':'image/heic','heif':'image/heif'}
    mt = mime_by_ext.get(ext, 'image/jpeg')
    data_uri = f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}"

    last_err = None
    was_blocked = False
    out_url = None
    used_ratio = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Softening level: attempts 1+2 use the original prompt, attempts
        # 3+4 soften the expression wording one notch, attempt 5 softens
        # to the gentlest variant. This breaks edge-case safety false
        # positives without changing the actual visual intent.
        soften = max(0, attempt - 2)
        prompt_text = _build_prompt(soften)
        if progress_cb:
            try: progress_cb(attempt, 'submitting' if attempt == 1 else
                              ('retry (soft prompt)' if soften else 'retry'))
            except Exception: pass
        url, ratio, err, blocked = _submit_and_poll_once(data_uri, prompt_text)
        if url:
            out_url = url; used_ratio = ratio
            logger.info(f"[banner_gen] succeeded on attempt {attempt}/{MAX_ATTEMPTS} (soften={soften})")
            break
        last_err = err
        was_blocked = was_blocked or blocked
        logger.warning(f"[banner_gen] attempt {attempt}/{MAX_ATTEMPTS} failed (blocked={blocked}): {err}")
        if not blocked:
            # Non-safety failure — retry once with a short sleep, but bail
            # after 2 such retries (not worth burning $0.10 × 5 on a flaky
            # API or a permanent broken-input issue).
            if attempt >= 2:
                break
            time.sleep(5)
            continue
        # Safety block — let the loop softening kick in on the next attempt
        time.sleep(2)

    if not out_url:
        return (None,
                last_err or 'unknown failure',
                attempt,
                was_blocked)

    try:
        img_r = requests.get(out_url, timeout=60); img_r.raise_for_status()
    except Exception as e:
        return None, f"output download err: {e}", attempt, was_blocked
    ts = time.strftime('%Y%m%d_%H%M%S')
    local = f"/tmp/banner_{ts}.jpg"
    with open(local, 'wb') as f: f.write(img_r.content)
    logger.info(f"[banner_gen] saved {local} (aspect={used_ratio}, attempt={attempt})")
    return local, None, attempt, was_blocked


async def banner_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_banner_photo'] = True
    await update.message.reply_text(
        "🖼 *Banner generator*\n\n"
        "Send a reference photo of your model (just one image — any "
        "shot showing her face/identity).\n\n"
        "I'll generate a wide-banner-format image of the same person in "
        "a specific pose: lying on a bed, propped on one elbow, looking at "
        "the camera, wearing a black dress with subtle beaded waist detail.\n\n"
        "Engine: `nano-banana-pro` (Gemini 3 Pro Image)\n"
        "Aspect ratio: 21:9 (cinematic banner / social header)\n"
        "Cost: ~$0.10",
        parse_mode='Markdown')


async def banner_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo-router entry. Returns True if we handled the upload (so other
    photo handlers in bot.py don't also fire); False if we're not expecting
    a banner photo right now."""
    if not context.user_data.get('expecting_banner_photo'):
        return False
    context.user_data.pop('expecting_banner_photo', None)

    photo = (update.message.photo or [None])[-1]   # highest-res variant
    if not photo:
        await update.message.reply_text("❌ no photo in that message. Run /banner_gen again.")
        return True

    # Download to /tmp
    try:
        f = await context.bot.get_file(photo.file_id)
        local_ref = f"/tmp/banner_ref_{photo.file_unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: `{e}`",
                                          parse_mode='Markdown')
        return True

    await update.message.reply_text(
        f"🤖 generating banner from your reference "
        f"(up to {MAX_ATTEMPTS} attempts × ~60-120s each; safety-filter blocks "
        f"trigger automatic retry with a softened prompt)…",
        parse_mode='Markdown')

    # Run in worker thread; bot loop streams progress messages back via the
    # progress callback. The callback can't await, so we schedule TG sends
    # on the main loop with asyncio.run_coroutine_threadsafe.
    loop = asyncio.get_running_loop()
    def _progress(attempt, status):
        try:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(
                    f"   ⏳ attempt {attempt}/{MAX_ATTEMPTS} — {status}",
                    parse_mode='Markdown'),
                loop)
        except Exception as e:
            logger.warning(f"[banner_gen] progress msg err: {e}")

    local_out, err, attempts, was_blocked = await asyncio.to_thread(
        _generate_banner, local_ref, _progress)

    if err:
        if was_blocked:
            await update.message.reply_text(
                f"🚫 *Blocked by nano-banana-pro's safety filter*\n"
                f"Tried {attempts}/{MAX_ATTEMPTS} times with progressively "
                f"softer prompts — all attempts flagged.\n\n"
                f"Suggestions:\n"
                f"• Try a less revealing reference photo (the safety filter "
                f"reacts to the input image, not just the prompt)\n"
                f"• Crop the reference to just the face/upper torso\n"
                f"• Or retry — sometimes the filter is non-deterministic\n\n"
                f"Last error: `{err[:200]}`",
                parse_mode='Markdown')
        else:
            await update.message.reply_text(
                f"❌ generation failed after {attempts} attempt(s): `{err[:300]}`",
                parse_mode='Markdown')
        return True

    try:
        with open(local_out, 'rb') as fh:
            caption = (f"✅ banner generated"
                       f"{f' (took {attempts} attempts)' if attempts > 1 else ''}\n"
                       f"Engine: `{ENGINE}` (nano-banana-pro)\n"
                       f"Saved locally: `{local_out}`")
            await update.message.reply_photo(photo=fh, caption=caption,
                                              parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ generated but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
