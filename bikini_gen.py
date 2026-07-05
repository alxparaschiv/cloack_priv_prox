"""/bikini_gen — Batch bikini "thirst-trap" image generator for a reference model.

No image upload: you pick one of your existing models (carolina, kira, …) and
the bot pulls that model's reference photos from her Google Drive folders
(folders whose name starts with the model name), generates a BATCH of bikini
images (5/10/15/20) in varied settings, uploads them to a fresh Drive folder,
and returns the folder link.

Every image targets the same Sophie-Rain expression: neutral → innocent →
slightly teasing — NOT smiling, NOT laughing, NOT sad. Soft, direct gaze.

Suggestive-but-clothed (bikini) only, clearly-adult subject — guardrails are
in the prompt and the engine's safety filter enforces the hard line.

Reuses all Drive + WaveSpeed plumbing from artistic_bg_gen.

Env deps:
  - WAVESPEED_API_KEY            — nano-banana-pro
  - REEL_GOOGLE_TOKEN_PICKLE     — Drive holding the model reference folders
"""
import time
import base64
import random
import asyncio
import logging

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import cloak
import artistic_bg_gen as _ag

logger = logging.getLogger(__name__)

ENGINE = _ag.ENGINE
ASPECT = '9:16'                 # IG story / thirst-trap vertical
REFS_PER_CALL = 3              # identity refs per generation (face lock)
COUNT_OPTIONS = [5, 10, 15, 20]
BATCH_MAX = 20

# Settings cycled across a batch so each image differs while the vibe holds.
SETTINGS = [
    "poolside at night, warm indoor lighting, turquoise pool water glowing behind her",
    "on a sandy tropical beach in bright daylight, ocean behind her",
    "a bedroom mirror selfie, soft daylight from a window",
    "on a sunny hotel balcony overlooking the sea",
    "a bathroom mirror selfie, clean modern bathroom",
    "lounging on a poolside deck chair in afternoon sun",
    "in a cozy living room, lamp-lit, casual evening",
    "on the deck of a boat/yacht, open water behind her",
    "by a hotel pool at golden hour, palm trees behind",
    "sitting on a made bed, soft natural daylight",
]

# Expression — the defining element. Softened across retries for safety edge
# cases without changing the look.
EXPRESSION_LEVELS = [
    "neutral, innocent, faintly teasing expression — soft relaxed face, lips "
    "softly together or barely parted, a calm direct gaze straight into the "
    "camera, eyebrows relaxed. NOT smiling, NOT laughing, NOT sad, NOT a "
    "sultry pout — understated and a little innocent, with just a hint of a "
    "tease in the eyes",
    "soft neutral expression with a faint innocent tease — relaxed face, "
    "calm direct gaze, lips lightly together, not smiling and not sad",
    "calm soft neutral expression, gentle direct gaze, relaxed and natural",
]


def _build_prompt(setting, softening_level=0):
    expression = EXPRESSION_LEVELS[min(softening_level, len(EXPRESSION_LEVELS) - 1)]
    return (
        "Generate a new photo of the SAME woman shown in the reference images. "
        "Preserve her face, hair color and texture, eye color, complexion and "
        "overall identity EXACTLY — same person, do not change her features.\n"
        "\n"
        "AGE — CRITICAL: she is a mature ADULT woman in her mid-to-late "
        "twenties, clearly of legal age, with a grown adult woman's face and "
        "figure. Do NOT make her look youthful, teenage, schoolgirl, or "
        "childlike.\n"
        "\n"
        "WARDROBE: a stylish bikini (vary the color/pattern). Suggestive but "
        "NOT explicit — she is clothed in the bikini; breasts and buttocks "
        "stay covered, no exposed nipples or genitals, nothing uncovered. "
        "Think a casual Instagram-story bikini selfie, not pornography.\n"
        "\n"
        f"SETTING: {setting}.\n"
        "\n"
        "POSE / FRAMING: a casual self-shot Instagram-story photo — natural "
        "thirst-trap framing, full-body or upper-thigh-up vertical shot, her "
        "looking into the camera. Relaxed, candid posture (a hand in her hair, "
        "leaning, or holding the phone), not a stiff studio pose. " + expression + ".\n"
        "\n"
        "CAMERA / PHOTOGRAPHY STYLE — CRITICAL (makes it look REAL, not a "
        "studio render):\n"
        "  • Smartphone front-camera / selfie look ONLY — NOT professional "
        "photography, NOT a magazine shoot.\n"
        "  • ABSOLUTELY NO bokeh, NO depth-of-field blur, NO heavy background "
        "blur. Foreground and background roughly equally sharp.\n"
        "  • NO cinematic lighting, NO dramatic shadows, NO color grading, NO "
        "film filter, NO HDR, NO professional retouching, NO skin smoothing.\n"
        "  • Plain ambient/natural light for the setting.\n"
        "  • Slight imperfections are GOOD: a little sensor noise, slightly "
        "imperfect framing, natural skin texture (visible pores, slight "
        "unevenness — NOT airbrushed), a stray hair or two.\n"
        "  • Photorealistic, candid, Instagram-feed feel — like a phone photo "
        "she took herself.\n"
        "\n"
        "COMPOSITION: vertical 9:16 Instagram-story aspect. Subject fills the "
        "frame; no letterboxing, no borders, no captions or text overlays."
    )


def _model_ref_folders(svc, model):
    """Return the model's REFERENCE face folder(s).

    Convention in Drive: the canonical reference face lives in a folder named
    `reference <Model>` (e.g. "reference Carolina"), NOT in folders that merely
    start with the model name — those (e.g. "Carolina Goth Nurse") are finished
    CONTENT/OUTPUT folders full of unrelated generated images, and sampling them
    produced faces that looked nothing like the model.

    Prefer the exact `reference <model>` folder. If it doesn't exist, fall back
    to any `reference <model> …` variant folder (some models only have variants
    like "reference Kira new goth"). Returns a list of {id, name}.
    """
    ml = model.lower().strip()
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name contains 'reference {model}'")
    res = svc.files().list(
        q=q, fields='files(id,name)', pageSize=100,
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = res.get('files') or []
    exact = [f for f in files if (f.get('name') or '').lower() == f'reference {ml}']
    if exact:
        return exact
    pref = f'reference {ml}'
    return [f for f in files if (f.get('name') or '').lower().startswith(pref)]


def _gen_one_bikini(svc, pool, parent_id, setting, emit):
    """Sample refs → generate one bikini image → upload to Drive. Returns
    (drive_id, err)."""
    chosen = random.sample(pool, min(REFS_PER_CALL, len(pool)))
    data_uris = []
    for f in chosen:
        b = _ag._download_image_bytes(svc, f['id'])
        mt = (f.get('mimeType') or '').lower()
        if not mt.startswith('image/'):
            mt = 'image/jpeg'
        data_uris.append(f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}")

    out_url = None
    last_err = None
    MAX_ATTEMPTS = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        soften = max(0, attempt - 1)
        prompt = _build_prompt(setting, soften)
        try:
            rid = _ag._wavespeed_submit_multi_ref(
                data_uris, prompt=prompt, aspect_ratio=ASPECT, resolution='2k')
            out_url = _ag._wavespeed_wait(rid)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            emit(f"   ⚠️ attempt {attempt}/{MAX_ATTEMPTS}: {last_err}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(8)
    if not out_url:
        return None, f"all {MAX_ATTEMPTS} attempts failed; last: {last_err}"

    try:
        r = requests.get(out_url, timeout=60); r.raise_for_status()
        img = r.content
    except Exception as e:
        return None, f"output fetch err: {e}"
    ts = time.strftime('%Y%m%d_%H%M%S')
    fname = f"bikini_{ts}_{random.randint(1000,9999)}.jpg"
    drive_id = _ag._upload_bytes_to_drive(svc, parent_id, fname, img)
    return drive_id, None


def generate_bikini_batch(model, count, emit):
    """Resolve model refs → generate `count` bikini images into a fresh Drive
    folder. Returns (batch_url, ok_count, errors)."""
    svc = _ag._drive_service()
    folders = _model_ref_folders(svc, model)
    if not folders:
        return None, 0, [f"no reference folders found for '{model}' in Drive"]
    pool = []
    for f in folders:
        try:
            pool.extend(_ag._list_images_in_folder(svc, f['id']))
        except Exception:
            pass
    if len(pool) < 1:
        return None, 0, [f"no reference images found for '{model}' — expected a "
                         f"'reference {model}' folder in Drive with at least 1 face photo"]
    emit(f"📁 {len(folders)} ref folders · {len(pool)} ref images for {model}")

    root = _ag._ensure_folder(svc, _ag.OUTPUT_ROOT_NAME)
    ts = time.strftime('%Y%m%d_%H%M%S')
    batch_id = _ag._ensure_folder(svc, f"bikini_{model}_{ts}", root)
    url = _ag._folder_drive_url(batch_id)

    ok, errs = 0, []
    for i in range(1, count + 1):
        setting = SETTINGS[(i - 1) % len(SETTINGS)]
        emit(f"── 👙 {i}/{count} — {setting.split(',')[0]} ──")
        try:
            _id, err = _gen_one_bikini(svc, pool, batch_id, setting, emit)
            if err:
                errs.append(f"{i}: {err}")
            else:
                ok += 1
        except Exception as e:
            errs.append(f"{i}: {type(e).__name__}: {e}")
    return url, ok, errs


# ─── Telegram flow ─────────────────────────────────────────────────────────

def _model_kb():
    rows = []
    try:
        for m in (cloak._known_models() or []):
            rows.append([InlineKeyboardButton(f"👤 {m}", callback_data=f"bikini:model:{m}")])
    except Exception as e:
        logger.warning(f"[bikini] model list err: {e}")
    rows.append([InlineKeyboardButton("✖ cancel", callback_data="bikini:cancel")])
    return InlineKeyboardMarkup(rows)


def _count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{n}", callback_data=f"bikini:count:{n}") for n in COUNT_OPTIONS],
        [InlineKeyboardButton("✖ cancel", callback_data="bikini:cancel")],
    ])


async def bikini_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👙 *Bikini batch generator*\n\n"
        "Generates a batch of bikini Instagram-story images of one of your "
        "models (refs pulled from her Drive folders — no upload). Varied "
        "settings, same soft neutral-innocent-teasing expression. Uploaded "
        "to a Drive folder.\n\n"
        f"Engine: `{ENGINE}` · ~$0.07-0.20/img · ~1-2 min each.\n\n"
        "Pick a model 👇",
        parse_mode='Markdown', reply_markup=_model_kb())


async def bikini_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'cancel':
        await q.edit_message_text("✖ cancelled.")
        return

    if action == 'model':
        model = parts[2]
        context.user_data['bikini_model'] = model
        await q.edit_message_text(
            f"👤 *{model}* selected.\n\nHow many images?",
            parse_mode='Markdown', reply_markup=_count_kb())
        return

    if action == 'count':
        model = context.user_data.get('bikini_model')
        if not model:
            await q.edit_message_text("⚠️ session expired — run /bikini_gen again.")
            return
        count = min(BATCH_MAX, int(parts[2]))
        await q.edit_message_text(
            f"👙 generating *{count}* bikini images for *{model}*…\n"
            f"~{count}-{count*2} min. I'll post the Drive link when done.",
            parse_mode='Markdown')

        loop = asyncio.get_running_loop()
        chat = q.message.chat

        def emit(m):
            try:
                asyncio.run_coroutine_threadsafe(chat.send_message(m), loop)
            except Exception:
                pass

        url, ok, errs = await asyncio.to_thread(generate_bikini_batch, model, count, emit)

        if not url:
            await chat.send_message(f"❌ couldn't start: {errs[0] if errs else 'unknown'}")
            return
        tail = ""
        if errs:
            tail = f"\n⚠️ {len(errs)} failed (e.g. {errs[0][:120]})"
        await chat.send_message(
            f"🎉 *Done* — {ok}/{count} images for *{model}*\n"
            f"📂 [Open Drive folder]({url})\n`{url}`{tail}",
            parse_mode='Markdown', disable_web_page_preview=True)
