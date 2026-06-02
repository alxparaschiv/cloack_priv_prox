"""3-STEP TELEGRAM WIZARD for the full Meta Dev pipeline.

User flow:
  /setup_full
    → Step 1/3: paste the blob (as TEXT, multi-line OK, multi-message OK accumulate; OR upload as .txt)
    → Step 2/3: pick a GoLogin profile from numbered list of "Validated Profile N"
    → Step 3/3: pick rental — 'fresh' (auto-rent) OR 'lr_xxx +1xxx' (reuse existing)
    → Bot spawns master_account_create.py as one subprocess
    → Screenshots + vision feedback stream to this chat via the existing TELEGRAM_BOT_TOKEN

Cancel anytime with /cancel.

The pipeline runs ~50-60 min including 10-min anti-flag cooldown.
"""
import os, sys, asyncio, subprocess, re, base64, json, requests, logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Per-chat state machine
_state: dict[int, dict] = {}

STEP1 = (
    '🛤 *Full Meta Dev pipeline — Step 1/3*\n\n'
    'Send the account *blob* — one of:\n'
    '  • paste as plain text (multi-line OK, I\'ll accumulate across messages)\n'
    '  • upload a `.txt` file containing the blob\n\n'
    'Format: `email:fbpw:email:emailpw:fb_url:dob:UA:b64cookies`\n'
    '(or the FB-first variant: `fbid:fbpw:email:emailpw:token:b64cookies`)\n\n'
    'Cancel with /cancel'
)

STEP2_HEADER = '🛤 *Step 2/3 — pick a GoLogin profile*\n\nReply with a number (1, 2, 3…):'
STEP3 = (
    '🛤 *Step 3/3 — rental phone*\n\n'
    'Reply with one of:\n'
    '  • `fresh` — I\'ll rent a brand-new 7-day Facebook phone via TextVerified\n'
    '  • `lr_xxxxxxxxxxxx +1XXXXXXXXXX` — reuse this rental (id + space + +1phone)'
)


async def setup_full_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 0 — kick off the wizard."""
    chat_id = update.effective_chat.id
    _state[chat_id] = {'awaiting': 'blob', 'data': {}}
    await update.message.reply_text(STEP1, parse_mode='Markdown', disable_web_page_preview=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in _state:
        del _state[chat_id]
        await update.message.reply_text('🚫 setup_full cancelled')
    else:
        await update.message.reply_text('nothing to cancel')


def _list_validated_profiles():
    """Hit GoLogin API for all profiles starting with 'Validated Profile'.
    Strips '+ CE' suffix for sort key but keeps full name for the profile_id lookup."""
    gl = os.getenv('GOLOGIN_API_KEY')
    if not gl: return []
    out = []
    for page in [1, 2, 3, 4, 5]:
        try:
            r = requests.get(f'https://api.gologin.com/browser/v2?limit=100&page={page}',
                             headers={'Authorization': f'Bearer {gl}'}, timeout=20).json()
        except Exception as e:
            logger.warning(f'gologin api page {page} err: {e}')
            break
        profs = r.get('profiles', [])
        if not profs: break
        for p in profs:
            n = (p.get('name') or '').strip()
            # Match "Validated Profile N" or "Validated Profile N + CE"
            m = re.match(r'^Validated Profile\s+(\d+)(\s*\+\s*CE)?$', n, re.IGNORECASE)
            if m:
                out.append({'id': p['id'], 'name': n, 'num': int(m.group(1))})
    out.sort(key=lambda x: x['num'])
    return out


async def setup_full_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Text router callback — handles each step's response.
    Returns True if this message was for the wizard (consumed it)."""
    chat_id = update.effective_chat.id
    st = _state.get(chat_id)
    if not st: return False
    text = (update.message.text or '').strip()
    if not text: return False

    awaiting = st.get('awaiting')

    # ─── Step 1: blob ─────────────────────────────────────────────
    if awaiting == 'blob':
        # Multi-message accumulation: keep appending text until a valid blob is recognized
        accumulated = st.get('blob_buffer', '') + ('\n' if st.get('blob_buffer') else '') + text
        # Strip whitespace/newlines from the accumulated string for parsing
        flattened = ''.join(accumulated.split())
        # Heuristic: a valid blob has many colons + a long base64 tail (W3si... is typical cookie JSON b64)
        if ':' not in flattened or len(flattened) < 500 or 'W3si' not in flattened:
            # Save what we have, prompt for more
            st['blob_buffer'] = accumulated
            await update.message.reply_text(
                f'🟡 received {len(accumulated)} chars so far — keep pasting (need the full blob with `W3si...` base64 cookies tail), or /cancel'
            )
            return True
        # We have a candidate blob — try to parse
        text = flattened
        try:
            b64 = text.rsplit(':', 1)[-1]
            cookies = json.loads(base64.b64decode(b64).decode('utf-8'))
            c_user = next((c['value'] for c in cookies if c['name'] == 'c_user'), None)
            if not c_user:
                await update.message.reply_text('❌ blob has no c_user cookie. Cookies invalid.')
                del _state[chat_id]
                return True
        except Exception as e:
            await update.message.reply_text(f'❌ failed to parse blob: {e}')
            del _state[chat_id]
            return True

        # Uniqueness check via accounts CSV
        try:
            import accounts_sheet as asm
            email = text.split(':')[0]
            rows = asm._read_all_rows()
            if rows and len(rows) >= 2:
                headers = rows[0]
                fb_id_col = headers.index('FB Profile ID') if 'FB Profile ID' in headers else None
                email_col = headers.index('FB Email') if 'FB Email' in headers else None
                profile_col = headers.index('GoLogin Profile') if 'GoLogin Profile' in headers else None
                status_col = headers.index('Status') if 'Status' in headers else None
                prior = []
                for r in rows[1:]:
                    if len(r) <= 1: continue
                    fb_match = fb_id_col is not None and len(r) > fb_id_col and r[fb_id_col] == c_user
                    em_match = email_col is not None and len(r) > email_col and r[email_col] == email
                    if fb_match or em_match:
                        prior.append({
                            'profile': r[profile_col] if profile_col is not None and len(r) > profile_col else '',
                            'status':  r[status_col] if status_col is not None and len(r) > status_col else '',
                        })
                if prior:
                    msg = f'🚨 *UNIQUENESS WARNING* — this blob was already used:\n'
                    for p in prior[:5]:
                        msg += f'  • `{p["profile"]}` → `{p["status"]}`\n'
                    msg += '\nReply `ALLOW DUPLICATE` to proceed anyway, or /cancel.'
                    st['blob'] = text
                    st['c_user'] = c_user
                    st['awaiting'] = 'confirm_duplicate'
                    await update.message.reply_text(msg, parse_mode='Markdown')
                    return True
        except Exception as e:
            logger.warning(f'uniqueness check err: {e}')

        # New blob — proceed to step 2
        st['blob'] = text
        st['c_user'] = c_user
        await _present_profile_picker(update, st)
        return True

    if awaiting == 'confirm_duplicate':
        if text.upper().strip() == 'ALLOW DUPLICATE':
            await update.message.reply_text('⚠️ override accepted — proceeding with duplicate blob')
            await _present_profile_picker(update, st)
            return True
        else:
            await update.message.reply_text('🚫 cancelled. Use /setup_full to start over.')
            del _state[chat_id]
            return True

    # ─── Step 2: pick profile ─────────────────────────────────────
    if awaiting == 'profile':
        try:
            idx = int(text) - 1
        except ValueError:
            await update.message.reply_text('❌ reply with a number (e.g. `1`)')
            return True
        profiles = st.get('profiles', [])
        if idx < 0 or idx >= len(profiles):
            await update.message.reply_text(f'❌ out of range 1..{len(profiles)}')
            return True
        chosen = profiles[idx]
        st['profile_id'] = chosen['id']
        st['profile_name'] = chosen['name']
        await update.message.reply_text(f'✅ selected `{chosen["name"]}`', parse_mode='Markdown')
        st['awaiting'] = 'rental'
        await update.message.reply_text(STEP3, parse_mode='Markdown')
        return True

    # ─── Step 3: pick rental ──────────────────────────────────────
    if awaiting == 'rental':
        env_extra = {}
        if text.lower().strip() == 'fresh':
            await update.message.reply_text('📞 a fresh rental will be created at pipeline start')
        else:
            # Expect: lr_xxx +1XXXXXXXXXX
            m = re.match(r'^(lr_[A-Z0-9]+)\s+(\+?\d+)$', text.strip(), re.IGNORECASE)
            if not m:
                await update.message.reply_text(
                    '❌ format: `lr_xxxxxxxxxxxx +1XXXXXXXXXX` (rental id + space + +1phone). Try again or /cancel.',
                    parse_mode='Markdown')
                return True
            rental_id = m.group(1)
            phone = m.group(2)
            if not phone.startswith('+'): phone = '+' + phone
            env_extra['REUSE_RENTAL_ID'] = rental_id
            env_extra['REUSE_RENTAL_PHONE'] = phone
            await update.message.reply_text(f'✅ reusing `{rental_id}` `{phone}`', parse_mode='Markdown')

        # All 3 steps collected — spawn the pipeline
        blob = st['blob']
        profile_name = st['profile_name']
        c_user = st['c_user']
        del _state[chat_id]

        await update.message.reply_text(
            f'🛤 *spawning pipeline*\n'
            f'  c_user: `{c_user}`\n'
            f'  profile: `{profile_name}`\n'
            f'  rental: `{env_extra.get("REUSE_RENTAL_ID", "fresh")}`\n\n'
            f'expected runtime: ~50-60 min (incl. 10-min anti-flag cooldown)\n'
            f'screenshots + vision feedback stream to this chat throughout',
            parse_mode='Markdown')

        log_path = f'/tmp/setup_full_{c_user}.log'
        try:
            env = {**os.environ, **env_extra}
            with open(log_path, 'w') as logf:
                subprocess.Popen(
                    ['python3', '-u', '/app/master_account_create.py', blob, profile_name],
                    env=env,
                    stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            await update.message.reply_text(f'✅ pipeline subprocess spawned. log: `{log_path}`', parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f'❌ spawn failed: {e}')

        return True

    return False


async def setup_full_document_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle .txt document uploads during step 1. Returns True if consumed."""
    chat_id = update.effective_chat.id
    st = _state.get(chat_id)
    if not st or st.get('awaiting') != 'blob': return False
    doc = update.message.document
    if not doc: return False
    # Accept any text-y document (mime text/plain or .txt extension)
    name = (doc.file_name or '').lower()
    if not (name.endswith('.txt') or (doc.mime_type or '').startswith('text/')):
        return False  # not a text doc — let other handlers see it
    if doc.file_size and doc.file_size > 200000:
        await update.message.reply_text(f'❌ document too large ({doc.file_size} bytes, cap 200KB)')
        return True
    try:
        f = await context.bot.get_file(doc.file_id)
        content = await f.download_as_bytearray()
        text = content.decode('utf-8', errors='replace').strip()
    except Exception as e:
        await update.message.reply_text(f'❌ failed to download document: {e}')
        return True
    # Replace any accumulated buffer with this document's content
    st['blob_buffer'] = text
    # Now feed it through the text handler logic by faking a message
    # Easiest: call the text handler with the doc content as the "next text"
    class _FakeMsg:
        def __init__(self, t): self.text = t
        async def reply_text(self, *a, **kw): return await update.message.reply_text(*a, **kw)
    class _FakeUpdate:
        def __init__(self, u, t):
            self.message = _FakeMsg(t)
            self.effective_chat = u.effective_chat
    # Clear buffer so the call sees only the document content
    st['blob_buffer'] = ''
    return await setup_full_text_received(_FakeUpdate(update, text), context)


async def _present_profile_picker(update: Update, st: dict):
    """Lookup all Validated Profile N on GoLogin + present numbered list."""
    profiles = _list_validated_profiles()
    if not profiles:
        await update.message.reply_text('❌ no "Validated Profile N" profiles found on GoLogin.')
        chat_id = update.effective_chat.id
        if chat_id in _state: del _state[chat_id]
        return
    st['profiles'] = profiles
    st['awaiting'] = 'profile'
    lines = [STEP2_HEADER, '']
    for i, p in enumerate(profiles, 1):
        lines.append(f'  *{i}*. `{p["name"]}`')
    msg = '\n'.join(lines)
    # Telegram cap ~4096 chars; chunk if needed
    if len(msg) > 3800:
        msg = '\n'.join(lines[:80]) + '\n…(list truncated, reply with the number for the profile you want)'
    await update.message.reply_text(msg, parse_mode='Markdown')
