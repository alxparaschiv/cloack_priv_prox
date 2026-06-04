"""/ig_setup_private — Wizard that takes an existing IG account + GeeLark phone
and configures the account (login + bio + link + profile pic + Private toggle)
via Appium UI automation on the GeeLark cloud phone.

Per user spec (2026-06-04):
  - Login with paste-in credentials (username:password[:2fa_secret])
  - Set bio + optional link in bio
  - Profile pic: choose from Drive folder (GPT-4o vision filters face-visible
    photos out + ranks by seducing/cleavage), OR upload via Telegram
  - Toggle account to Private
  - Auto-stop the phone at the end (per [geelark-autostop-after-install])

The GeeLark phone is expected to ALREADY exist with Instagram installed —
i.e. /geelark_profile_open was run for this profile first. /ig_setup_private
just validates the phone exists + IG is installed, then runs the setup.

GoLogin profile + GeeLark phone share the same name by convention (set up
by /geelark_profile_open). When the IG account is eventually made public,
the user opens the matching GoLogin browser profile to connect via API for
autonomous posting — the cross-platform identity link is the name.

NOTE — Shard B (this commit): wizard collects ALL inputs but the actual
Appium login flow lives in geelark_ig_automation.py (Shard D) which doesn't
exist yet. The final 'confirm and run' step currently echoes the plan back
and stops. Once Shard D lands, that placeholder is swapped for the real call.
"""
import os
import re
import logging
import base64

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from geelark_open import _geelark_post, _gologin_find_profile_by_name

logger = logging.getLogger(__name__)


# ─── State machine: stored in context.user_data['ig_setup_state'] ──────────
# Steps in order: creds → phone → bio → link → pic_source → (pic) → confirm
STEP_CREDS         = 'creds'
STEP_PHONE         = 'phone'
STEP_BIO           = 'bio'
STEP_LINK          = 'link'
STEP_PIC_SOURCE    = 'pic_source'
STEP_PIC_UPLOAD    = 'pic_upload'
STEP_PIC_DRIVE     = 'pic_drive_folder'
STEP_PIC_CHOOSE    = 'pic_drive_choose'
STEP_CONFIRM       = 'confirm'

SKIP_WORDS = {'skip', 'none', 'no', '-'}


def _parse_creds(text):
    """Accepts username:password OR username:password:2fa_secret.
    Returns (username, password, totp_secret_or_None) or (None, None, None).
    """
    parts = text.strip().split(':')
    if len(parts) < 2:
        return None, None, None
    username = parts[0].strip()
    password = parts[1].strip()
    if not username or not password:
        return None, None, None
    totp = None
    if len(parts) >= 3:
        totp = ':'.join(parts[2:]).strip()  # tolerate extra colons in TOTP secret
    return username, password, totp


def _find_geelark_phone_by_name(name):
    """Look up the GeeLark phone whose serialName matches `name` (case-insensitive).
    Returns (phone_id, err)."""
    target = name.strip().lower()
    page = 1
    while True:
        data, err = _geelark_post('/phone/list', {'page': page, 'pageSize': 100})
        if err:
            return None, err
        items = data.get('items') or data.get('list') or []
        if not items:
            break
        for it in items:
            n = (it.get('serialName') or it.get('profileName') or '').strip().lower()
            if n == target:
                return it.get('id') or it.get('phoneId') or it.get('envId'), None
        if len(items) < 100:
            break
        page += 1
    return None, f"no GeeLark phone named '{name}'"


# ─── Telegram handlers ─────────────────────────────────────────────────────

async def ig_setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — kick off the wizard."""
    context.user_data['ig_setup_state'] = {'step': STEP_CREDS, 'data': {}}
    await update.message.reply_text(
        "📸 *Instagram Private Account Setup*\n\n"
        "I'll walk you through logging into an existing IG account on a GeeLark "
        "phone, setting bio + link + profile pic, and switching to Private.\n\n"
        "*Prerequisite:* the GeeLark phone for this account already exists with "
        "Instagram installed (via /geelark_profile_open).\n\n"
        "*Step 1/6 — IG credentials*\n"
        "Send:\n"
        "  `username:password`  (no 2FA)\n"
        "  `username:password:totp_secret`  (with 2FA)",
        parse_mode='Markdown',
    )


async def ig_setup_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Central text dispatcher for the wizard. Called by bot.py's _text_router
    when 'ig_setup_state' is present in user_data.
    """
    state = context.user_data.get('ig_setup_state')
    if not state:
        return
    text = (update.message.text or '').strip()
    step = state['step']
    data = state['data']

    if text.lower() == 'cancel':
        context.user_data.pop('ig_setup_state', None)
        await update.message.reply_text("❌ Cancelled. Run /ig_setup_private to start over.")
        return

    # ── STEP 1: credentials
    if step == STEP_CREDS:
        u, p, t = _parse_creds(text)
        if not u:
            await update.message.reply_text(
                "❌ Couldn't parse credentials. Expected `username:password` or "
                "`username:password:totp_secret`. Try again or type `cancel`.",
                parse_mode='Markdown')
            return
        data['username'] = u
        data['password'] = p
        data['totp'] = t
        state['step'] = STEP_PHONE
        await update.message.reply_text(
            f"✅ creds captured for `@{u}` (2FA: {'yes' if t else 'no'})\n\n"
            f"*Step 2/6 — GeeLark phone*\n"
            f"Send the GeeLark phone profile name (must already exist via "
            f"/geelark_profile_open — same name as the GoLogin profile).",
            parse_mode='Markdown')
        return

    # ── STEP 2: phone name
    if step == STEP_PHONE:
        name = text
        await update.message.reply_text(
            f"🔍 looking up GeeLark phone `{name}`…", parse_mode='Markdown')
        phone_id, err = _find_geelark_phone_by_name(name)
        if err or not phone_id:
            await update.message.reply_text(
                f"❌ {err}\n\nMake sure the GeeLark phone exists (via "
                f"/geelark_profile_open). Try another name or `cancel`.",
                parse_mode='Markdown')
            return
        # Also confirm matching GoLogin profile exists (warn but don't block — user
        # may have deleted it intentionally though it's the convention to keep it)
        gid, _ = _gologin_find_profile_by_name(name)
        gologin_note = f"✅ matching GoLogin profile found ({gid})" if gid else "⚠️ no matching GoLogin profile (not required, but the convention is to keep them paired)"
        data['phone_name'] = name
        data['phone_id'] = phone_id
        data['gologin_profile_id'] = gid
        state['step'] = STEP_BIO
        await update.message.reply_text(
            f"✅ GeeLark phone found: `{phone_id}`\n"
            f"{gologin_note}\n\n"
            f"*Step 3/6 — Bio*\n"
            f"Send the IG bio text (keep under ~150 chars, line breaks ok).",
            parse_mode='Markdown')
        return

    # ── STEP 3: bio
    if step == STEP_BIO:
        if len(text) > 150:
            await update.message.reply_text(
                f"⚠️ bio is {len(text)} chars — IG caps at 150. Trim and resend, "
                f"or send anyway? Reply `force` to use as-is, or send a shorter version.",
                parse_mode='Markdown')
            data['bio_pending'] = text
            return
        if text.lower() == 'force' and data.get('bio_pending'):
            data['bio'] = data['bio_pending']
            data.pop('bio_pending', None)
        else:
            data['bio'] = text
            data.pop('bio_pending', None)
        state['step'] = STEP_LINK
        await update.message.reply_text(
            f"✅ bio set ({len(data['bio'])} chars)\n\n"
            f"*Step 4/6 — Link in bio*\n"
            f"Send a single URL, or `skip` to leave it empty.",
            parse_mode='Markdown')
        return

    # ── STEP 4: link
    if step == STEP_LINK:
        if text.lower() in SKIP_WORDS:
            data['link'] = None
        else:
            if not text.startswith(('http://', 'https://')):
                text = 'https://' + text
            data['link'] = text
        state['step'] = STEP_PIC_SOURCE
        await update.message.reply_text(
            f"✅ link: `{data['link'] or '(none)'}`\n\n"
            f"*Step 5/6 — Profile picture source*\n"
            f"Reply `upload` to send a photo here, or `drive` to scan a "
            f"Drive folder + pick from candidates that pass the face-safe filter.",
            parse_mode='Markdown')
        return

    # ── STEP 5: pic source
    if step == STEP_PIC_SOURCE:
        choice = text.lower().strip()
        if choice == 'upload':
            state['step'] = STEP_PIC_UPLOAD
            await update.message.reply_text(
                "📤 send the profile photo as an image attachment now.",
                parse_mode='Markdown')
            return
        if choice == 'drive':
            state['step'] = STEP_PIC_DRIVE
            await update.message.reply_text(
                "📁 send the *Drive folder name* containing candidate images "
                "(case-insensitive substring match against your Drive root).",
                parse_mode='Markdown')
            return
        await update.message.reply_text(
            "❌ reply `upload` or `drive` (or `cancel`).",
            parse_mode='Markdown')
        return

    # ── STEP 5b: Drive folder name → kick off image scan (Shard C will handle)
    if step == STEP_PIC_DRIVE:
        data['drive_folder_query'] = text
        await update.message.reply_text(
            f"📁 will scan Drive folder matching `{text}` for candidates.\n"
            f"⚠️ Drive image-picker (GPT-4o vision filter) lands in *Shard C* — "
            f"not implemented yet. For now reply `upload` to send a photo via TG.",
            parse_mode='Markdown')
        # For now bounce back to the upload path so the wizard can be tested end-to-end
        state['step'] = STEP_PIC_SOURCE
        return

    # ── STEP 6 awaiting: confirm + run (or cancel)
    if step == STEP_CONFIRM:
        if text.lower() in ('yes', 'y', 'go', 'run'):
            await update.message.reply_text(
                "🚀 *would run setup now*, but the Appium IG flow lands in *Shard D* "
                "and isn't implemented yet.\n\nState collected:\n"
                f"  username: `@{data['username']}`\n"
                f"  phone_id: `{data['phone_id']}`\n"
                f"  bio: `{data['bio'][:60]}{'…' if len(data['bio'])>60 else ''}`\n"
                f"  link: `{data.get('link') or '(none)'}`\n"
                f"  pic: `{data.get('pic_source','?')}`\n\n"
                f"Next shard will boot the phone, connect Appium, and execute. Cancelled for now.",
                parse_mode='Markdown')
            context.user_data.pop('ig_setup_state', None)
            return
        if text.lower() in ('no', 'n', 'cancel'):
            context.user_data.pop('ig_setup_state', None)
            await update.message.reply_text("❌ cancelled.")
            return
        await update.message.reply_text("reply `yes` to run or `no` to cancel.")
        return


async def ig_setup_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct photo upload during STEP_PIC_UPLOAD."""
    state = context.user_data.get('ig_setup_state')
    if not state or state['step'] != STEP_PIC_UPLOAD:
        return False  # not awaiting a photo — let other handlers process it
    photo = (update.message.photo or [None])[-1]  # highest-res variant
    if not photo:
        await update.message.reply_text("❌ no photo found in message. Send an image attachment.")
        return True
    # Download via Telegram Bot API + save to /tmp for later upload to the phone
    f = await context.bot.get_file(photo.file_id)
    local_path = f"/tmp/ig_setup_pic_{photo.file_unique_id}.jpg"
    await f.download_to_drive(local_path)
    state['data']['pic_source'] = 'upload'
    state['data']['pic_local_path'] = local_path
    state['step'] = STEP_CONFIRM
    await _send_confirmation(update, state['data'])
    return True


async def _send_confirmation(update, data):
    """Final summary + ask user to confirm before running setup."""
    lines = [
        "*Step 6/6 — Confirm and run*",
        "",
        f"  IG account: `@{data['username']}`",
        f"  2FA: `{'enabled' if data.get('totp') else 'no'}`",
        f"  GeeLark phone: `{data['phone_name']}` ({data['phone_id']})",
        f"  GoLogin profile: `{data['gologin_profile_id'] or '(missing)'}`",
        f"  Bio: `{data['bio']}`",
        f"  Link: `{data.get('link') or '(none)'}`",
        f"  Profile pic: `{data.get('pic_source','?')}`",
        "",
        "Reply `yes` to run the setup, or `no` to cancel.",
    ]
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
