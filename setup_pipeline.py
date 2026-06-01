"""Telegram entry point for the full Meta Dev pipeline.

User flow:
  /setup_full  → bot asks for blob + optional profile name
  User pastes  → bot validates blob, picks/uses profile, spawns full_pipeline.py
                  as a background subprocess. The pipeline sends its own status
                  + screenshots to Telegram via the existing TELEGRAM_BOT_TOKEN.

The pipeline runtime is ~30-50 min (including the 10min anti-flag cooldown).
"""
import os, sys, asyncio, subprocess, re, base64, json, requests, logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Per-chat state — whether we're waiting for a blob from this user
_state: dict[int, dict] = {}

USAGE = (
    '🛤 *Full Meta Dev pipeline*\n\n'
    'I run Stages 0-12 end-to-end on one GoLogin profile:\n'
    '  • FB login + AC bind (phone)\n'
    '  • Meta Dev wizard → Complete Registration\n'
    '  • Create FB app + IG app\n'
    '  • Set privacy URL + Save + Publish (shard 1)\n'
    '  • Customize add 4 perms + Graph Explorer 5 perms + Generate Token + extend (shard 2)\n'
    '  • Save Drive blob + CSV with all credentials\n\n'
    'Reply with the account blob in your next message. Format:\n'
    '`<blob_string>`\n'
    'or\n'
    '`<blob_string>\\n<Validated Profile N>`\n\n'
    'If you don\'t specify a profile, I\'ll use the next available "Validated Profile N".\n\n'
    'Pipeline runs ~30-50 min. Screenshots arrive at every step. Cancel with /cancel.'
)

async def setup_full_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — primes the chat for a blob paste."""
    chat_id = update.effective_chat.id
    _state[chat_id] = {'awaiting': 'blob', 'started_at': asyncio.get_event_loop().time()}
    await update.message.reply_text(USAGE, parse_mode='Markdown', disable_web_page_preview=True)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in _state:
        del _state[chat_id]
        await update.message.reply_text('🚫 setup cancelled')
    else:
        await update.message.reply_text('nothing to cancel')

async def setup_full_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle the blob paste. Returns True if this message was for us (consume it).

    Called from bot.py's text router. If state[chat_id] is awaiting blob, parse + spawn."""
    chat_id = update.effective_chat.id
    st = _state.get(chat_id)
    if not st or st.get('awaiting') != 'blob':
        return False  # not ours, let other handlers see it
    text = (update.message.text or '').strip()
    if not text:
        return False

    # Parse: blob is on first line; optional profile name on second line
    lines = text.split('\n')
    blob = lines[0].strip()
    profile_name = lines[1].strip() if len(lines) > 1 else None

    # Validate blob — must end with a base64 cookies block
    if ':' not in blob or len(blob) < 200:
        await update.message.reply_text('❌ that doesn\'t look like a valid blob. Try /setup_full again.')
        del _state[chat_id]
        return True
    try:
        b64 = blob.rsplit(':', 1)[-1]
        cookies = json.loads(base64.b64decode(b64).decode('utf-8'))
        c_user = next((c['value'] for c in cookies if c['name'] == 'c_user'), None)
        if not c_user:
            await update.message.reply_text('❌ blob has no c_user cookie. Bad cookies.')
            del _state[chat_id]
            return True
    except Exception as e:
        await update.message.reply_text(f'❌ failed to parse blob: {e}')
        del _state[chat_id]
        return True

    # Auto-pick profile if not given
    if not profile_name:
        try:
            import meta_dev as mdm
            profs = mdm._list_validated_profiles()
            if not profs:
                await update.message.reply_text('❌ no Validated Profile N profiles found in GoLogin')
                del _state[chat_id]
                return True
            # Pick the next one not already used. For now, just the highest-numbered one.
            profile_name = profs[-1]['name']
        except Exception as e:
            await update.message.reply_text(f'❌ profile picker failed: {e}')
            del _state[chat_id]
            return True

    # Acknowledge + spawn
    del _state[chat_id]
    await update.message.reply_text(
        f'🛤 starting pipeline\n'
        f'  email c_user: `{c_user}`\n'
        f'  profile: `{profile_name}`\n'
        f'  expected runtime: 30-50 min (incl 10min anti-flag cooldown)\n\n'
        f'screenshots arrive at every step. cancel with `pkill -f full_pipeline` from /shell',
        parse_mode='Markdown'
    )

    # Spawn master_account_create.py — the ONE unified script (no more full_pipeline shim).
    # Per [[feedback-one-script-no-parallel]]: shards 1+2 are inlined into master now.
    log_path = f'/tmp/setup_full_{c_user}.log'
    try:
        with open(log_path, 'w') as logf:
            subprocess.Popen(
                ['python3', '-u', '/app/master_account_create.py', blob, profile_name],
                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from this process group
            )
        await update.message.reply_text(f'pipeline subprocess spawned. tail -F {log_path}')
    except Exception as e:
        await update.message.reply_text(f'❌ spawn failed: {e}')

    return True
