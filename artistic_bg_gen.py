"""/artistic_bg — Generate unique image(s) whose style matches a folder
of reference images in Drive.

Engine: WaveSpeed `google/nano-banana-pro/edit` (Gemini 3 Pro Image,
~$0.07-0.20/img). The `images` array accepts multiple references — same
pattern reel_bot uses when it sends a model+pose pair. Picks 6 random
images from the reference folder per generation, sends them all in the
`images` array with a prompt that asks for a new visually-distinct
image in the same aesthetic.

Two flows:
  1. Wizard-driven single shot: geelark_image_wizard calls
     generate_artistic_bg(profile_subfolder_name) to drop ONE image into
     that profile's Drive subfolder.
  2. /artistic_bg Telegram command: inline-keyboard picker — choose
     a type (any 'Images bg *' Drive folder, or Mixed across all),
     choose a count, generate N images into a timestamped batch
     subfolder under OUTPUT_ROOT_NAME, and DM back the Drive folder
     link so the user can open + bulk-download.

Output is saved to a single root Drive folder:
   `Images generated for account setup in GeeLark/`
   sub-folder per profile when invoked from the wizard;
   sub-folder per batch (`batch_<type>_<ts>/`) when invoked from /artistic_bg.

Env deps:
  - WAVESPEED_API_KEY        — WaveSpeed AI bearer token
  - REEL_GOOGLE_TOKEN_PICKLE — Drive auth for reel-bot-Carolina's Drive (where
                               the 'Images bg *' folders + the output root live).
                               Separate from GOOGLE_TOKEN_PICKLE (cloak's Drive).
"""
import os
import io
import base64
import time
import random
import logging
import pickle
import asyncio

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

WAVESPEED_API_BASE = 'https://api.wavespeed.ai/api/v3'
WAVESPEED_API_KEY = os.environ.get('WAVESPEED_API_KEY', '')

REF_FOLDER_NAME    = 'Images bg goth artistic'      # default / wizard path
REF_FOLDER_PREFIX  = 'Images bg '                    # auto-discover all types matching this prefix
OUTPUT_ROOT_NAME   = 'Images generated for account setup in GeeLark'
REFS_PER_CALL      = 6
ENGINE             = 'nano-banana-pro'  # Gemini 3 Pro Image; ~$0.07-0.20/img

# Hard cap on how many backgrounds a single batch can request. Each image
# costs $0.07-0.20 and burns ~60-120s, so a 50-img batch is ~$5-10 + ~1h.
BATCH_MAX = 50

# Prompt designed for no-people aesthetic-matching backgrounds. The model
# tends to insert subjects unless explicitly told not to.
PROMPT = (
    "Create a new, completely unique artistic background image — visually "
    "DISTINCT from any single reference but in the same aesthetic family "
    "as the reference images: same color palette, same mood, same artistic "
    "treatment, same compositional feel. The new image must NOT copy or "
    "directly reproduce any single reference; combine and reinterpret their "
    "style into something fresh. No people, no faces, no human figures. "
    "Pure background art — abstract textures, surfaces, patterns, "
    "atmospheres, environments. Square 1:1 composition suitable for a "
    "profile background."
)


# ─── Drive ─────────────────────────────────────────────────────────────────

def _drive_service():
    """Build a Drive client using reel-bot-Carolina's pickle.

    REEL_GOOGLE_TOKEN_PICKLE is the SEPARATE pickle that authenticates against
    the Drive holding 'Images bg *' folders + 'Images generated for account
    setup in GeeLark'. We deliberately don't use cloak's own GOOGLE_TOKEN_PICKLE
    here — those are different Drives.
    """
    raw = os.environ.get('REEL_GOOGLE_TOKEN_PICKLE') \
          or os.environ.get('GOOGLE_TOKEN_PICKLE')   # fallback for local testing
    if not raw:
        raise RuntimeError("neither REEL_GOOGLE_TOKEN_PICKLE nor GOOGLE_TOKEN_PICKLE is set")
    creds = pickle.loads(base64.b64decode(raw))
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _find_folder_by_name(svc, name, parent_id=None):
    q = [
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
        f"name='{name}'",
    ]
    if parent_id:
        q.append(f"'{parent_id}' in parents")
    res = svc.files().list(q=' and '.join(q),
                           fields='files(id,name,createdTime)',
                           orderBy='createdTime',
                           supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    items = res.get('files') or []
    return items[0] if items else None


def _ensure_folder(svc, name, parent_id=None):
    """Find a folder by exact name (under optional parent), create if missing."""
    f = _find_folder_by_name(svc, name, parent_id)
    if f:
        return f['id']
    body = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
    if parent_id:
        body['parents'] = [parent_id]
    f = svc.files().create(body=body, fields='id',
                           supportsAllDrives=True).execute()
    return f['id']


def _list_art_type_folders(svc):
    """Find every top-level 'Images bg *' folder. Returns list of {id,name}.

    These are the discoverable 'types' the user can pick from in /artistic_bg.
    Paginated to avoid the unscoped-single-page miss bug per
    [[reference-drive-folder-pagination]].
    """
    out = []
    page_token = None
    while True:
        q = (f"mimeType='application/vnd.google-apps.folder' and trashed=false "
             f"and name contains '{REF_FOLDER_PREFIX}'")
        res = svc.files().list(q=q,
                               fields='nextPageToken, files(id,name)',
                               pageSize=100,
                               pageToken=page_token,
                               supportsAllDrives=True,
                               includeItemsFromAllDrives=True).execute()
        for f in res.get('files') or []:
            # Drive's `name contains` is a substring match — verify prefix.
            if (f.get('name') or '').startswith(REF_FOLDER_PREFIX):
                out.append({'id': f['id'], 'name': f['name']})
        page_token = res.get('nextPageToken')
        if not page_token: break
    # De-duplicate by id (in case Drive returns the same folder twice).
    seen = set(); uniq = []
    for f in out:
        if f['id'] in seen: continue
        seen.add(f['id']); uniq.append(f)
    uniq.sort(key=lambda x: x['name'].lower())
    return uniq


def _list_images_in_folder(svc, folder_id):
    out = []
    page_token = None
    while True:
        q = (f"'{folder_id}' in parents and trashed=false and "
             f"(mimeType contains 'image/' or "
             f"name contains '.jpg' or name contains '.jpeg' or name contains '.png')")
        res = svc.files().list(q=q, fields='nextPageToken, files(id,name,mimeType)',
                               pageSize=100, pageToken=page_token,
                               supportsAllDrives=True,
                               includeItemsFromAllDrives=True).execute()
        for f in res.get('files') or []:
            mt = (f.get('mimeType') or '').lower()
            nm = (f.get('name') or '').lower()
            if mt.startswith('image/') or nm.endswith(('.jpg', '.jpeg', '.png')):
                out.append(f)
        page_token = res.get('nextPageToken')
        if not page_token: break
    return out


def _download_image_bytes(svc, file_id):
    buf = io.BytesIO()
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _upload_bytes_to_drive(svc, parent_folder_id, file_name, content_bytes,
                            mime='image/jpeg'):
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime,
                              resumable=False)
    body = {'name': file_name, 'parents': [parent_folder_id]}
    res = svc.files().create(body=body, media_body=media, fields='id',
                             supportsAllDrives=True).execute()
    return res['id']


def _folder_drive_url(folder_id):
    return f'https://drive.google.com/drive/folders/{folder_id}'


# ─── WaveSpeed ─────────────────────────────────────────────────────────────

def _wavespeed_submit_multi_ref(images_data_uris, prompt=PROMPT,
                                  engine=ENGINE,
                                  aspect_ratio='1:1', resolution='2k'):
    """Submit a multi-reference image-edit task to nano-banana-pro.
    Returns request_id or raises.
    """
    if not WAVESPEED_API_KEY:
        raise RuntimeError("WAVESPEED_API_KEY not set in env")
    url = f"{WAVESPEED_API_BASE}/google/{engine}/edit"
    body = {
        'prompt': prompt,
        'images': images_data_uris,
        'aspect_ratio': aspect_ratio,
        'resolution': resolution,
        'output_format': 'jpeg',
        'enable_sync_mode': False,
        'enable_base64_output': False,
    }
    r = requests.post(url, json=body, timeout=60,
                      headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}',
                               'Content-Type': 'application/json'})
    if r.status_code != 200:
        raise RuntimeError(f"WaveSpeed submit HTTP {r.status_code}: {r.text[:400]}")
    j = r.json()
    rid = (j.get('data') or {}).get('id') or j.get('id')
    if not rid:
        raise RuntimeError(f"WaveSpeed submit: no request id in response: {j}")
    return rid


def _wavespeed_wait(request_id, max_wait=300, poll_every=3):
    """Poll until the task completes. Returns the output image URL or raises."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(f"{WAVESPEED_API_BASE}/predictions/{request_id}/result",
                         headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}'},
                         timeout=30)
        r.raise_for_status()
        j = r.json()
        d = j.get('data') or j
        status = d.get('status') or ''
        if status in ('completed', 'succeeded'):
            outputs = d.get('outputs') or []
            if outputs:
                first = outputs[0]
                return first if isinstance(first, str) else first.get('url')
            raise RuntimeError("WaveSpeed completed but no outputs")
        if status == 'failed':
            raise RuntimeError(f"WaveSpeed failed: {j}")
        time.sleep(poll_every)
    raise RuntimeError(f"WaveSpeed timed out after {max_wait}s (request_id={request_id})")


# ─── Core single-image generator ───────────────────────────────────────────

def _refs_pool(svc, ref_folder_ids):
    """Build a single combined pool of refs from one or many folders.

    For 'Mixed' mode we pass multiple folder ids and randomly sample REFS_PER_CALL
    refs across the union — so a single generation can pull from several types
    at once, biasing the output toward 'something in between'.
    """
    pool = []
    for fid in ref_folder_ids:
        pool.extend(_list_images_in_folder(svc, fid))
    return pool


def _generate_one(svc, ref_folder_ids, parent_folder_id, emit, file_prefix='artistic_bg'):
    """Pick fresh random refs → submit → wait → download → upload to Drive.

    ref_folder_ids: list of folder ids to pull refs from (1 for single-type,
        N for Mixed).
    parent_folder_id: where to drop the generated image.
    Returns (drive_id, local_path, err).
    """
    pool = _refs_pool(svc, ref_folder_ids)
    if len(pool) < REFS_PER_CALL:
        return None, None, (f"ref pool has only {len(pool)} images, "
                            f"need at least {REFS_PER_CALL}")
    chosen = random.sample(pool, REFS_PER_CALL)
    emit(f"🎲 picked {REFS_PER_CALL} random refs: {[c['name'] for c in chosen]}")

    data_uris = []
    for f in chosen:
        b = _download_image_bytes(svc, f['id'])
        mt = (f.get('mimeType') or '').lower()
        if not mt.startswith('image/'):
            mt = 'image/jpeg'
        b64 = base64.b64encode(b).decode('ascii')
        data_uris.append(f"data:{mt};base64,{b64}")

    out_url = None
    last_err = None
    MAX_ATTEMPTS = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            emit(f"🤖 submitting to WaveSpeed {ENGINE} (multi-ref, attempt {attempt}/{MAX_ATTEMPTS})…")
            rid = _wavespeed_submit_multi_ref(data_uris)
            emit(f"⏳ request {rid} — polling for result (max ~5 min)…")
            out_url = _wavespeed_wait(rid)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            emit(f"⚠️ attempt {attempt}/{MAX_ATTEMPTS} failed: {last_err}")
            if attempt < MAX_ATTEMPTS:
                emit(f"   → sleeping 10s before retry")
                time.sleep(10)
    if not out_url:
        return None, None, f"all {MAX_ATTEMPTS} attempts failed; last err: {last_err}"
    emit(f"✅ generation complete — downloading output")

    try:
        img_r = requests.get(out_url, timeout=60)
        img_r.raise_for_status()
        img_bytes = img_r.content
    except Exception as e:
        return None, None, f"output fetch err: {type(e).__name__}: {e}"

    ts = time.strftime('%Y%m%d_%H%M%S')
    # Add random suffix so two images generated in the same second don't collide.
    fname = f'{file_prefix}_{ts}_{random.randint(1000, 9999)}.jpg'
    local_path = f'/tmp/{fname}'
    with open(local_path, 'wb') as f: f.write(img_bytes)

    drive_id = _upload_bytes_to_drive(svc, parent_folder_id, fname, img_bytes)
    return drive_id, local_path, None


# ─── Main flow (wizard path — single-shot, unchanged signature) ────────────

def generate_artistic_bg(profile_subfolder_name=None, send_progress=None):
    """End-to-end: pick 6 refs → call WaveSpeed → download → save to Drive.

    profile_subfolder_name: when set, creates / reuses that subfolder under
      the OUTPUT_ROOT_NAME for the saved image. None = save directly in root.
    send_progress: optional callable(text) for streaming updates.
    Returns (drive_file_id, local_temp_path, error_or_None).
    """
    def emit(msg):
        logger.info(f"[artistic_bg] {msg}")
        if send_progress:
            try: send_progress(msg)
            except Exception: pass

    svc = _drive_service()
    ref_folder = _find_folder_by_name(svc, REF_FOLDER_NAME)
    if not ref_folder:
        return None, None, f"reference folder '{REF_FOLDER_NAME}' not found in Drive"
    emit(f"📁 ref folder: {REF_FOLDER_NAME} ({ref_folder['id']})")

    out_root_id = _ensure_folder(svc, OUTPUT_ROOT_NAME)
    parent_id = out_root_id
    if profile_subfolder_name:
        parent_id = _ensure_folder(svc, profile_subfolder_name, out_root_id)

    drive_id, local_path, err = _generate_one(svc, [ref_folder['id']], parent_id, emit)
    if not err:
        emit(f"💾 saved to Drive: {OUTPUT_ROOT_NAME}/"
             f"{profile_subfolder_name + '/' if profile_subfolder_name else ''}"
             f"{os.path.basename(local_path)}")
    return drive_id, local_path, err


# ─── Batch generator (new — /artistic_bg path) ─────────────────────────────

def generate_artistic_bg_batch(type_label, ref_folder_ids, count, send_progress=None):
    """Generate `count` images into a fresh batch subfolder under OUTPUT_ROOT.

    type_label: short slug used in the batch folder name + filenames
        (e.g. 'goth_artistic', 'mixed').
    ref_folder_ids: list of folder ids to sample refs from.
    Returns (batch_folder_id, batch_folder_url, successes, errors)
        successes: list of (drive_id, local_path) for images that landed.
        errors: list of error strings for images that failed.
    """
    def emit(msg):
        logger.info(f"[artistic_bg_batch] {msg}")
        if send_progress:
            try: send_progress(msg)
            except Exception: pass

    svc = _drive_service()
    out_root_id = _ensure_folder(svc, OUTPUT_ROOT_NAME)
    ts = time.strftime('%Y%m%d_%H%M%S')
    batch_name = f'batch_{type_label}_{ts}'
    batch_id = _ensure_folder(svc, batch_name, out_root_id)
    batch_url = _folder_drive_url(batch_id)
    emit(f"📁 batch folder: `{OUTPUT_ROOT_NAME}/{batch_name}`")
    emit(f"🔗 {batch_url}")

    successes = []
    errors = []
    for i in range(1, count + 1):
        emit(f"\n── 🖼 image {i}/{count} ──")
        try:
            drive_id, local_path, err = _generate_one(
                svc, ref_folder_ids, batch_id, emit,
                file_prefix=f'artistic_{type_label}')
            if err:
                emit(f"❌ image {i} failed: {err}")
                errors.append(f"{i}: {err}")
            else:
                successes.append((drive_id, local_path))
                emit(f"💾 image {i} saved (drive id {drive_id})")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            emit(f"❌ image {i} crashed: {err}")
            errors.append(f"{i}: {err}")
    return batch_id, batch_url, successes, errors


# ─── Telegram command + callbacks ──────────────────────────────────────────

# Count options shown in the count picker.
COUNT_OPTIONS = [1, 3, 5, 10, 20]


def _type_slug(name):
    """'Images bg goth artistic' → 'goth_artistic'."""
    base = name[len(REF_FOLDER_PREFIX):] if name.startswith(REF_FOLDER_PREFIX) else name
    return base.strip().lower().replace(' ', '_').replace('-', '_')


def _type_picker_keyboard(types):
    """One button per discovered type + a 'Mixed (all)' button + Cancel.

    Type ids are cached in user_data['artbg_types'] keyed by index so the
    callback data stays well under the 64B Telegram cap.
    """
    rows = []
    for idx, t in enumerate(types):
        label = f"🎨 {t['name'].replace(REF_FOLDER_PREFIX, '')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"artbg:t:{idx}")])
    rows.append([InlineKeyboardButton("🎲 Mixed (random across all types)",
                                       callback_data="artbg:t:mix")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="artbg:cancel")])
    return InlineKeyboardMarkup(rows)


def _count_picker_keyboard(idx_token):
    rows = []
    row = []
    for n in COUNT_OPTIONS:
        row.append(InlineKeyboardButton(f"{n}", callback_data=f"artbg:n:{idx_token}:{n}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="artbg:back")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="artbg:cancel")])
    return InlineKeyboardMarkup(rows)


async def artistic_bg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entrypoint — discover type folders + show the type picker."""
    msg = update.message
    if not WAVESPEED_API_KEY:
        await msg.reply_text(
            "❌ `WAVESPEED_API_KEY` not set in env. Add it to Railway and redeploy.",
            parse_mode='Markdown')
        return

    # Discover types in a worker thread (Drive call).
    try:
        types = await asyncio.to_thread(lambda: _list_art_type_folders(_drive_service()))
    except Exception as e:
        await msg.reply_text(f"❌ Drive list err: `{type(e).__name__}: {e}`",
                              parse_mode='Markdown')
        return

    if not types:
        await msg.reply_text(
            f"❌ no `{REF_FOLDER_PREFIX}*` folders found in Drive.\n"
            f"Create one (e.g. `{REF_FOLDER_NAME}`) and drop your refs in it.",
            parse_mode='Markdown')
        return

    context.user_data['artbg_types'] = types
    lines = "\n".join(f"• `{t['name']}`" for t in types)
    await msg.reply_text(
        "🎨 *Artistic background generator — batch*\n\n"
        f"Engine: `{ENGINE}` (~$0.07-0.20/img)\n"
        f"Each image picks {REFS_PER_CALL} random refs from the chosen type.\n\n"
        f"*Available types:*\n{lines}\n\n"
        "Pick a type 👇",
        parse_mode='Markdown',
        reply_markup=_type_picker_keyboard(types))


async def artbg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle artbg:* inline buttons (type picker → count picker → run)."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == 'artbg:cancel':
        await q.edit_message_text("❌ cancelled.")
        return

    if data == 'artbg:back':
        types = context.user_data.get('artbg_types') or []
        if not types:
            await q.edit_message_text("⚠️ session expired — run /artistic_bg again.")
            return
        await q.edit_message_text(
            "🎨 *Artistic background generator — batch*\n\nPick a type 👇",
            parse_mode='Markdown',
            reply_markup=_type_picker_keyboard(types))
        return

    if data.startswith('artbg:t:'):
        token = data.split(':', 2)[2]
        types = context.user_data.get('artbg_types') or []
        if not types:
            await q.edit_message_text("⚠️ session expired — run /artistic_bg again.")
            return
        if token == 'mix':
            label = '🎲 Mixed (random across all types)'
        else:
            try: i = int(token)
            except ValueError:
                await q.edit_message_text("⚠️ bad selection — run /artistic_bg again.")
                return
            if i < 0 or i >= len(types):
                await q.edit_message_text("⚠️ stale selection — run /artistic_bg again.")
                return
            label = f"🎨 {types[i]['name'].replace(REF_FOLDER_PREFIX, '')}"
        await q.edit_message_text(
            f"*Type:* {label}\n\nHow many backgrounds? (each takes ~60-120s)",
            parse_mode='Markdown',
            reply_markup=_count_picker_keyboard(token))
        return

    if data.startswith('artbg:n:'):
        # artbg:n:<token>:<count>
        parts = data.split(':')
        if len(parts) != 4:
            await q.edit_message_text("⚠️ bad callback — run /artistic_bg again.")
            return
        _, _, token, count_s = parts
        try: count = int(count_s)
        except ValueError:
            await q.edit_message_text("⚠️ bad count — run /artistic_bg again.")
            return
        if count < 1 or count > BATCH_MAX:
            await q.edit_message_text(f"⚠️ count must be 1–{BATCH_MAX}.")
            return
        await _kickoff_batch(update, context, token, count)
        return


async def _kickoff_batch(update, context, token, count):
    """Resolve the type token → list of ref folder ids → start batch in a
    worker thread. Streams progress + the final folder link back to TG."""
    q = update.callback_query
    chat_id = q.message.chat_id
    types = context.user_data.get('artbg_types') or []
    if not types:
        await q.edit_message_text("⚠️ session expired — run /artistic_bg again.")
        return

    if token == 'mix':
        ref_ids = [t['id'] for t in types]
        type_label = 'mixed'
        pretty = 'Mixed (random across all types)'
    else:
        try: i = int(token)
        except ValueError:
            await q.edit_message_text("⚠️ bad selection.")
            return
        if i < 0 or i >= len(types):
            await q.edit_message_text("⚠️ stale selection.")
            return
        ref_ids = [types[i]['id']]
        type_label = _type_slug(types[i]['name'])
        pretty = types[i]['name']

    await q.edit_message_text(
        f"🎨 *Batch started*\n\n"
        f"Type: `{pretty}`\n"
        f"Count: *{count}*\n"
        f"Engine: `{ENGINE}`\n\n"
        f"Streaming progress below — final Drive folder link at the end.",
        parse_mode='Markdown')

    loop = asyncio.get_event_loop()

    async def send_async(text):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                            parse_mode='Markdown',
                                            disable_web_page_preview=True)
        except Exception:
            # Markdown can fail on stray refs filenames — retry plain.
            try:
                await context.bot.send_message(chat_id=chat_id, text=text,
                                                disable_web_page_preview=True)
            except Exception: pass

    def sync_progress(text):
        try:
            asyncio.run_coroutine_threadsafe(send_async(text), loop)
        except Exception: pass

    batch_id, batch_url, successes, errors = await asyncio.to_thread(
        generate_artistic_bg_batch, type_label, ref_ids, count, sync_progress)

    ok_n, fail_n = len(successes), len(errors)
    summary = (
        f"🎉 *Batch complete*\n\n"
        f"✅ {ok_n}/{count} images generated\n"
        + (f"❌ {fail_n} failed\n" if fail_n else '')
        + f"\n📂 *Drive folder:* [open]({batch_url})\n"
        f"`{batch_url}`\n\n"
        f"Open the link, select all + download to grab them in one go."
    )
    await context.bot.send_message(chat_id=chat_id, text=summary,
                                    parse_mode='Markdown',
                                    disable_web_page_preview=False)
