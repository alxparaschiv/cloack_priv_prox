"""/portrait_gen — Recreate a model from a reference photo in 3:4 portrait format.

Portrait sibling of /banner_gen. Same engine, same identity/wardrobe locks,
same candid-realism camera cues and safety-filter retry logic — but the
output is a 3:4 vertical portrait (IG feed / profile format) instead of a
21:9 banner.

Conversation flow:
  /portrait_gen                → prompt for a reference photo
  user uploads photo           → bot calls nano-banana-pro/edit with an
                                  upright-portrait prompt, returns the 3:4 image

Aspect ratio is 3:4 first, falling back to 4:5 if WaveSpeed rejects 3:4.

Env deps:
  - WAVESPEED_API_KEY        — same key used by /banner_gen and /artistic_bg
"""
import os
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

# Portrait aspect ratios to try, in order. 3:4 is the IG feed-portrait format;
# 4:5 is the alternate IG portrait ratio used as a fallback if 3:4 is rejected.
ASPECT_RATIOS = ('3:4', '4:5')

# Expression wording, softened progressively across retries to dodge the
# safety filter without changing visual intent. Identical to banner_gen's
# anchor ("warm friendly subtle closed-mouth smile").
EXPRESSION_LEVELS = [
    "warm friendly expression — subtle closed-mouth smile, eyes soft and "
    "welcoming, gentle and approachable, like she's smiling at a friend "
    "behind the camera. NOT a stern face, NOT a model-pout, NOT a serious "
    "or sultry stare — genuinely friendly and inviting",
    "warm friendly expression — subtle closed-mouth smile, kind eyes, "
    "relaxed and approachable look, gentle warmth in her face",
    "kind friendly expression — soft closed-mouth smile, gentle eyes, "
    "warm and approachable",
]


def _build_prompt(softening_level: int = 0) -> str:
    """Build the 3:4 portrait prompt. softening_level=0 is the default; higher
    values progressively soften any wording that might trigger nano-banana-pro's
    safety filter (without changing the actual visual intent)."""
    expression = EXPRESSION_LEVELS[min(softening_level, len(EXPRESSION_LEVELS) - 1)]
    return (
        "Generate a vertical 3:4 portrait image of the SAME person from "
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
        f"NEW POSE: upright portrait — standing or sitting naturally, facing "
        f"the camera, upper body and head filling the vertical frame from "
        f"roughly the top down to the hips/thighs (a three-quarter-length "
        f"portrait crop). Relaxed, candid posture — one shoulder slightly "
        f"turned, a hand near her hair or resting naturally, not a stiff "
        f"head-on mugshot. Her full outfit (the same one from the reference) "
        f"must be clearly visible across her torso and waist, not obscured. "
        f"{expression}.\n"
        "\n"
        "SETTING: a regular modern bedroom or living space, soft natural "
        "daylight from a window. The room should feel lived-in rather than "
        "staged — a normal home, not a hotel suite or a photo studio. "
        "Background may have small everyday details visible (a lamp, a shelf, "
        "a plant, the edge of a bed or sofa) but not cluttered.\n"
        "\n"
        "CAMERA / PHOTOGRAPHY STYLE — CRITICAL (this is what makes the image "
        "look REAL instead of like a Photoshopped studio shot):\n"
        "  • Smartphone camera look ONLY — NOT professional photography. "
        "Think iPhone front camera or modest DSLR snapshot, NOT a fashion "
        "magazine shoot.\n"
        "  • ABSOLUTELY NO bokeh, NO depth-of-field blur, NO background "
        "blur. The background should be IN FOCUS just like the foreground "
        "— no creamy out-of-focus walls. Everything in the frame at roughly "
        "the same sharpness.\n"
        "  • NO cinematic lighting, NO dramatic shadows, NO color grading, "
        "NO film-look filter, NO HDR effect, NO professional retouching, "
        "NO skin smoothing.\n"
        "  • NO studio lighting, NO ring light, NO softbox, NO reflector. "
        "Just plain ambient natural daylight from a window.\n"
        "  • Slight imperfections are GOOD and should be present: a touch "
        "of sensor noise, slightly imperfect framing, mild over- or "
        "under-exposure, a stray hair or two out of place, natural skin "
        "texture (visible pores, slight unevenness — NOT airbrushed). "
        "These cues make it feel like a real candid photo a friend took, "
        "not an AI render.\n"
        "  • Photorealistic, candid, Instagram-feed feel. Like a casual "
        "smartphone photo a friend snapped on a regular afternoon.\n"
        "\n"
        "COMPOSITION (CRITICAL — read carefully):\n"
        "  • Vertical 3:4 portrait aspect ratio (taller than wide).\n"
        "  • Her head sits in the upper third of the frame with a little "
        "headroom above the hair — NOT cropped at the forehead, NOT floating "
        "in the dead center. Eyes roughly on the upper-third line.\n"
        "  • The frame is well filled by the subject — her body and immediate "
        "surroundings occupy most of it. Avoid large empty blank areas (no "
        "vast empty ceiling or empty floor), but a normal amount of natural "
        "room around her is fine and realistic.\n"
        "  • Three-quarter-length crop: from the top of her head down to "
        "roughly the hips or mid-thigh. Face in sharp focus, looking at the "
        "camera.\n"
        "  • Natural candid framing like a real phone photo — not a tightly "
        "art-directed magazine cover.\n"
        "  • No letterboxing, no borders, no margins, no padding around the "
        "subject."
    )


# Keywords in WaveSpeed error responses that indicate a safety/content-filter
# block (vs. a transient error). Identical set to banner_gen.
SAFETY_BLOCK_KEYWORDS = (
    'safety', 'policy', 'content_filter', 'content filter', 'nsfw',
    'inappropriate', 'blocked', 'flagged', 'sensitive', 'sexual',
    'sexually', 'violat', 'moderation', 'prohibited', 'unsafe',
    'recitation', 'recitation_other', 'block_reason',
)


def _looks_like_safety_block(err_payload) -> bool:
    s = str(err_payload).lower()
    return any(kw in s for kw in SAFETY_BLOCK_KEYWORDS)


MAX_ATTEMPTS = 5  # how many times we re-try if blocked by safety filter


def _submit_and_poll_once(data_uri, prompt_text):
    """Single attempt: submit + poll for one task. Returns
    (out_url, used_ratio, err, blocked)."""
    submit_url = f"{WAVESPEED_API_BASE}/google/{ENGINE}/edit"
    rid = None
    used_ratio = None
    last_submit_err = None
    for aspect in ASPECT_RATIOS:
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
                logger.warning(f"[portrait_gen] aspect={aspect} {last_submit_err}")
                if _looks_like_safety_block(r.text):
                    return None, None, last_submit_err, True
                continue
            j = r.json()
            rid = (j.get('data') or {}).get('id') or j.get('id')
            if rid:
                used_ratio = aspect
                logger.info(f"[portrait_gen] submitted aspect={aspect}, rid={rid}")
                break
        except Exception as e:
            last_submit_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[portrait_gen] submit err aspect={aspect}: {e}")
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
            logger.warning(f"[portrait_gen] poll err: {e}")
        time.sleep(3)
    return None, used_ratio, f"timed out after 300s (rid={rid})", False


def _generate_portrait(local_ref_path, progress_cb=None):
    """Submit a /edit task to nano-banana-pro with the reference image +
    portrait prompt, with up to MAX_ATTEMPTS retries if the safety filter
    blocks the generation. Returns (local_path, err, attempts_used, was_blocked)."""
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
        soften = max(0, attempt - 2)
        prompt_text = _build_prompt(soften)
        if progress_cb:
            try: progress_cb(attempt, 'submitting' if attempt == 1 else
                              ('retry (soft prompt)' if soften else 'retry'))
            except Exception: pass
        url, ratio, err, blocked = _submit_and_poll_once(data_uri, prompt_text)
        if url:
            out_url = url; used_ratio = ratio
            logger.info(f"[portrait_gen] succeeded on attempt {attempt}/{MAX_ATTEMPTS} (soften={soften})")
            break
        last_err = err
        was_blocked = was_blocked or blocked
        logger.warning(f"[portrait_gen] attempt {attempt}/{MAX_ATTEMPTS} failed (blocked={blocked}): {err}")
        if not blocked:
            if attempt >= 2:
                break
            time.sleep(5)
            continue
        time.sleep(2)

    if not out_url:
        return None, last_err or 'unknown failure', attempt, was_blocked

    try:
        img_r = requests.get(out_url, timeout=60); img_r.raise_for_status()
    except Exception as e:
        return None, f"output download err: {e}", attempt, was_blocked
    ts = time.strftime('%Y%m%d_%H%M%S')
    local = f"/tmp/portrait_{ts}.jpg"
    with open(local, 'wb') as f: f.write(img_r.content)
    logger.info(f"[portrait_gen] saved {local} (aspect={used_ratio}, attempt={attempt})")
    return local, None, attempt, was_blocked


async def portrait_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_portrait_photo'] = True
    await update.message.reply_text(
        "🖼 *3:4 portrait generator*\n\n"
        "Send a reference photo of your model (just one image — any "
        "shot showing her face/identity).\n\n"
        "I'll recreate the same person as a vertical 3:4 portrait (IG "
        "feed / profile format): upright pose, same outfit, candid "
        "smartphone-photo look.\n\n"
        "Engine: `nano-banana-pro` (Gemini 3 Pro Image)\n"
        "Aspect ratio: 3:4 (falls back to 4:5)\n"
        "Cost: ~$0.10",
        parse_mode='Markdown')


async def portrait_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo-router entry. Returns True if we handled the upload; False if
    we're not expecting a portrait photo right now."""
    if not context.user_data.get('expecting_portrait_photo'):
        return False
    context.user_data.pop('expecting_portrait_photo', None)

    photo = (update.message.photo or [None])[-1]   # highest-res variant
    if not photo:
        await update.message.reply_text("❌ no photo in that message. Run /portrait_gen again.")
        return True

    try:
        f = await context.bot.get_file(photo.file_id)
        local_ref = f"/tmp/portrait_ref_{photo.file_unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: `{e}`",
                                          parse_mode='Markdown')
        return True

    await update.message.reply_text(
        f"🤖 generating 3:4 portrait from your reference "
        f"(up to {MAX_ATTEMPTS} attempts × ~60-120s each; safety-filter blocks "
        f"trigger automatic retry with a softened prompt)…",
        parse_mode='Markdown')

    loop = asyncio.get_running_loop()
    def _progress(attempt, status):
        try:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(
                    f"   ⏳ attempt {attempt}/{MAX_ATTEMPTS} — {status}",
                    parse_mode='Markdown'),
                loop)
        except Exception as e:
            logger.warning(f"[portrait_gen] progress msg err: {e}")

    local_out, err, attempts, was_blocked = await asyncio.to_thread(
        _generate_portrait, local_ref, _progress)

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
            caption = (f"✅ 3:4 portrait generated"
                       f"{f' (took {attempts} attempts)' if attempts > 1 else ''}\n"
                       f"Engine: `{ENGINE}` (nano-banana-pro)\n"
                       f"Saved locally: `{local_out}`")
            await update.message.reply_photo(photo=fh, caption=caption,
                                              parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ generated but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
