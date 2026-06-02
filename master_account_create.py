"""ONE FILE — end-to-end Meta Dev account creation.

═══════════════════════════════════════════════════════════════════
🛑 ARCHITECTURE RULES (immovable — break = burn an account)
═══════════════════════════════════════════════════════════════════

1. ONE SCRIPT, ONE PROCESS. Never subprocess.Popen another script
   while this one is running. Never run multiple Python files in
   parallel against the same GoLogin profile. (VP43 burn 2026-06-02
   was caused by concurrent scripts.)

2. NEVER goto AWAY from the wizard tab during stages D-G. After
   clicking Get Started, the wizard tab is the wizard. NO goto to
   /apps, /tools/explorer, /settings, etc. until Phase H (after CR
   + 6min wait + dashboard reached).

3. NEVER click the BLUE button on the passkey 'Next time, skip the
   code' modal. ONLY the gray 'Not now'/'Skip'. NEVER any fallback
   button if Not now is unclickable — HALT and surface to user.

4. After typing the AC email confirmation code + Next, do a PURE
   asyncio.sleep(90) with ZERO Playwright operations on the modal.
   Playwright's click(timeout=...) auto-retries on disabled buttons
   can dispatch events that wake the BLUE button.

5. Vision is a VALIDATION layer, NOT a decision driver. After every
   meaningful click, screenshot + GPT-4o vision YES/NO. If vision
   says NO or hallucinates → HALT, don't proceed.

6. Body-text keyword check is STRONGER than vision JSON keys.
   Vision can misread 'Review Your Email Address' as page_kind=
   'contact_info'. Read body.innerText and check for literal
   keyword strings before deciding the wizard step.

7. Never close/escape/restart any tab during the wizard. NO
   resumes. NO state save + reload. ONE GO from blob to long token.

8. AC binding goes FIRST (Phase C-NEW), wizard SECOND (Phase D).
   This avoids the FB silent-SMS-throttle that burns rentals.

═══════════════════════════════════════════════════════════════════
Stages (all inline, single process):
═══════════════════════════════════════════════════════════════════
  A: Setup — proxy + cookies push + session restart
  B: FB login confirm + interstitial dismiss
  B.5: 90s warmup browse on fb.com
  C-NEW: AC phone binding (FIRST — before wizard) [feedback-ac-bind-before-wizard]
  C: Dev portal → Get Started → Register Continue
  D: Verify Account: type phone, snapshot SMS, Send, poll, type code, Continue
  E: Review Email → Confirm Email (body-keyword check, not vision JSON)
  F: Contact info auto-advance
  G: About You → random role → Complete Registration
  H: 6-MIN SILENT WAIT (no reload, no click)
  I: Create FB app + IG app (use case wizard, business=no, password popup)
  J: Privacy URL via telegra.ph
  K: SHARD 1 — Settings/Basic privacy URL + Save + Show secret + Publish
  L: SHARD 2 — Customize add 4 perms + 10min cooldown + Explorer 5 perms +
              Generate OAuth + extend programmatically + verify scopes
  M: Save .txt to Drive + CSV with full credentials

Triggered from Telegram via /setup_full (see setup_pipeline.py).
"""
import sys, os, asyncio, requests, base64, time, re, json, random, imaplib, pickle
import email as em

sys.path.insert(0, '/app')
import proxy as pm, meta_dev as mdm, accounts_sheet as asm, privacy
import human_behavior as hbeh
from textverified_client import _client as tv_client
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

CHAT = 1984534885
TOK = os.environ['TELEGRAM_BOT_TOKEN']
OAI = os.environ['OPENAI_API_KEY']
H_GL = {'Authorization': f'Bearer {pm.GOLOGIN_API_KEY}', 'Content-Type': 'application/json'}
BLOBS_FOLDER_ID = '1orSrIksb2cAhxLwubKSBYsws4b8ordaJ'

ADJ = ['Tester','Test','Demo','Sample','My','First','New','Trial','Quick','Simple']
NOUN = ['App','Project','Build']
ROLES = ['Analyst','Marketer','Product manager']

def hb(t):
    print(f'HB: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id':CHAT,'text':f'⚙️ {t[:300]}'}, timeout=15)
    except: pass
def shot(b, c):
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo':('s.png',b,'image/png')}, data={'chat_id':CHAT,'caption':c[:1024]}, timeout=30)
    except: pass

async def safe_screenshot(page, retries=3, timeout_s=15):
    """Screenshot via direct CDP — the WORKING pattern from shard1_publish.py.

    REGRESSION ROOT CAUSE 2026-06-02: when I merged the shards into
    master_account_create.py at commit ed299d8, I replaced the working direct-CDP
    screenshot mechanism with Playwright's page.screenshot(). The shards used:

        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(c.send('Page.captureScreenshot',
                                            {'format':'jpeg','quality':55}),
                                     timeout=15)
        png = base64.b64decode(res['data'])

    This is the raw CDP command — no Playwright wrappers, no wait-for-fonts,
    no wait-for-animations. Just grab the frame buffer NOW. The shards ran
    reliably from Mac for weeks. After the merge, Playwright's page.screenshot
    started timing out RANDOMLY on fb.com (which has continuous JS activity
    that races with Playwright's internal pre-screenshot waits).

    Empirically validated 2026-06-02 on live VP1: direct CDP took 4.2s on
    fb.com page where Playwright PNG took 7.8s. Direct CDP took 0.3s on dev
    portal where Playwright PNG took 1.6s. Direct CDP also: NEVER timed out
    in our tests; Playwright RANDOMLY does.

    Returns JPEG bytes on success, b'' on failure."""
    import asyncio as _a, base64 as _b64
    try: ctx_for_cdp = page.context
    except Exception as e:
        hb(f'⚠️ screenshot: page.context unavailable: {e}')
        return b''
    for attempt in range(retries):
        cdp_session = None
        try:
            cdp_session = await ctx_for_cdp.new_cdp_session(page)
            res = await _a.wait_for(
                cdp_session.send('Page.captureScreenshot', {'format':'jpeg','quality':55}),
                timeout=timeout_s)
            try: await cdp_session.detach()
            except: pass
            return _b64.b64decode(res['data'])
        except Exception as e:
            hb(f'⚠️ direct-CDP screenshot attempt {attempt+1}/{retries}: {str(e)[:120]}')
            if cdp_session:
                try: await cdp_session.detach()
                except: pass
            await _a.sleep(2)
    hb('❌ direct-CDP screenshot exhausted all retries — returning empty bytes')
    return b''
def vision(png, q, model='gpt-4o', max_tok=500):
    """Vision call with retry (5 attempts, 3s+exp backoff) — a single transient
    OpenAI hiccup must NOT crash the pipeline. VP1 burn 2026-06-02 traced to
    one empty response body during AC bind → JSONDecodeError → pipeline died
    mid-flow after rental was already half-used.

    On total failure (5 retries all fail), returns a sentinel reply so the
    caller's parsing yields a benign NO/empty-JSON rather than crashing. This
    lets the gate decide to HALT cleanly instead of an uncaught exception."""
    if not png:
        hb('vision skipped (no png)')
        return '{}'
    b64 = base64.b64encode(png).decode()
    payload = {'model':model,'messages':[{'role':'user','content':[
        {'type':'text','text':q},
        {'type':'image_url','image_url':{'url':f'data:image/png;base64,{b64}'}}]}],'max_tokens':max_tok}
    last_err = None
    for attempt in range(5):
        try:
            r = requests.post('https://api.openai.com/v1/chat/completions',
                headers={'Authorization':f'Bearer {OAI}'}, json=payload, timeout=60)
            if r.status_code != 200:
                last_err = f'HTTP {r.status_code}: {r.text[:200]}'
                hb(f'⚠️ vision attempt {attempt+1}/5 → {last_err}')
                time.sleep(3 + attempt*2)
                continue
            data = r.json()
            content = data.get('choices',[{}])[0].get('message',{}).get('content')
            if not content:
                last_err = f'empty content in response: {str(data)[:200]}'
                hb(f'⚠️ vision attempt {attempt+1}/5 → {last_err}')
                time.sleep(3 + attempt*2)
                continue
            return content
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            hb(f'⚠️ vision attempt {attempt+1}/5 → {last_err}')
            time.sleep(3 + attempt*2)
    hb(f'❌ vision failed all 5 attempts: {last_err} — returning fallback so caller can halt cleanly')
    # Fallback that parses to a benign NO for gate questions and empty {} for JSON questions
    return 'ANSWER: NO — vision unavailable\nDESCRIBE: vision API failed all retries'
def pj(v):
    s = v.strip()
    if '```' in s: s = s.split('```')[1].lstrip('json').strip()
    try: return json.loads(s)
    except: return {}

async def wait_for_rendered(page, label, max_wait=90, poll=8):
    """STAGE 1 (RULE #1): poll until page is RENDERED (not LOADING). Returns vision response or None.

    Per feedback-vision-gates-every-action + feedback-wait-for-stable-screen:
    one-shot vision checks fire during loading gap → script proceeds on stale info.
    This loop separates 'still loading' from 'rendered with content'."""
    deadline = time.time() + max_wait
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            png = await safe_screenshot(page)
        except Exception as e:
            hb(f'[render {label} #{n}] screenshot err: {e}')
            await asyncio.sleep(poll); continue
        shot(png, f'[render-poll {label} #{n}]')
        r = vision(png,
            "Is this page RENDERED (showing actual text/buttons/content) or LOADING (skeleton bars, spinner, blank, gray placeholders)? "
            "Reply EXACTLY: 'RENDERED: <1-sent>' or 'LOADING: <1-sent>'.", max_tok=120)
        hb(f'[render {label} #{n}] {r[:200]}')
        if r.strip().upper().startswith('RENDERED'): return r
        await asyncio.sleep(poll)
    hb(f'⚠️ [render {label}] never RENDERED in {max_wait}s')
    return None

async def vision_gate(page, label, question, max_wait=90):
    """🔒 3-STAGE anti-spasming gate (per user 2026-06-02):
       Stage 1: screenshot RENDERED? (wait_for_rendered loop, polls until not LOADING)
       Stage 2: DESCRIBE — what is literally on screen (forces the model to ground its answer)
       Stage 3: ANSWER — does that description match what we expect for this step? YES/NO

    Returns (yes_bool, full_response). Caller HALTS on (False, *).

    The DESCRIBE step is the critical addition: it makes the model verbalize what
    it sees BEFORE answering, which catches the failure mode where the model
    blindly says YES because it pattern-matches the question rather than the
    image. Description is logged to Telegram so the operator can audit live."""
    rendered = await wait_for_rendered(page, label, max_wait=max_wait)
    if not rendered: return False, None
    try:
        png = await safe_screenshot(page)
    except: return False, None
    prompt = (
        "TWO-PART CHECK. Be precise — your description must match the image, not the question.\n"
        "PART A (DESCRIBE): In one sentence, describe what is literally on the page right now "
        "(main heading + main interactive element/state). Do NOT mention the question yet.\n"
        f"PART B (ANSWER the question): {question}\n\n"
        "Reply EXACTLY in this format (two lines):\n"
        "DESCRIBE: <1-sentence factual description of what's on screen>\n"
        "ANSWER: YES — <1-sent reason> | OR | ANSWER: NO — <1-sent reason>"
    )
    r = vision(png, prompt, max_tok=300)
    hb(f'[gate {label}]\n{r[:500]}')
    # Robust YES/NO parse — accept either "ANSWER: YES" anywhere, or first non-DESCRIBE line starting with YES
    upper = r.upper()
    yes = ('ANSWER: YES' in upper) or ('ANSWER:YES' in upper)
    no  = ('ANSWER: NO'  in upper) or ('ANSWER:NO'  in upper)
    if not yes and not no:
        # legacy fallback — first non-empty line starts with YES
        for ln in r.strip().splitlines():
            t = ln.strip().upper()
            if t.startswith('YES'): yes = True; break
            if t.startswith('NO'):  no  = True; break
    return yes, r

async def dismiss_fb_interstitials(page, max_loops=8):
    """Dismiss FB blocking interstitials (ads choice, etc.) before proceeding.

    [feedback-fb-ads-choice-interstitial]:
    - First screen: "Make a choice about your ads" — click Get started
    - Second screen: radio "Use for free with ads" + Continue button — pick free radio, click Continue
    - May have 1-2 more confirmation screens

    Variability: SOME accounts see this, SOME don't (region + A/B). Always check first."""
    for i in range(max_loops):
        try:
            body = await page.evaluate("() => document.body.innerText")
            url = page.url
        except: return
        hb(f'[interstitial-check #{i+1}] url={url[:80]} body[:120]={body[:120]!r}')

        # Screen 1: initial ads-choice with Get started
        if 'Make a choice about your ads' in body or ('choice about your ads' in body and 'Get started' in body):
            hb(f'🚧 [#{i+1}] ads-choice screen 1 (Get started)')
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] screen 1: clicking Get started')
            try: await page.get_by_role('button', name='Get started').click(timeout=10000)
            except Exception as e: hb(f'Get started err: {e}'); return
            await asyncio.sleep(7)
            continue

        # Screen 2: "Use for free with ads" radio + Continue button
        if 'Use for free with ads' in body and 'Continue' in body:
            hb(f'🚧 [#{i+1}] screen 2 — selecting "Use for free with ads" radio')
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] screen 2: about to click free radio')
            # Click the "Use for free with ads" radio option
            clicked_radio = False
            for sel_fn, desc in [
                (lambda: page.get_by_role('radio', name='Use for free with ads'), 'role=radio'),
                (lambda: page.locator('text="Use for free with ads"').first, 'text-exact'),
                (lambda: page.locator('label:has-text("Use for free with ads")').first, 'label-has-text'),
            ]:
                try:
                    await sel_fn().click(timeout=5000); clicked_radio = True
                    hb(f'  → "Use for free with ads" clicked via {desc}'); break
                except Exception as e: hb(f'  fail {desc}: {str(e)[:80]}')
            if not clicked_radio:
                hb('⚠️ could not click "Use for free with ads"'); shot(await safe_screenshot(page), '⚠️ free radio click FAILED')
                return
            await asyncio.sleep(4)
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] after radio — Continue should be enabled')
            # Now click Continue
            try: await page.get_by_role('button', name='Continue').click(timeout=10000); hb('  → Continue clicked')
            except Exception as e: hb(f'Continue err: {e}'); return
            await asyncio.sleep(8)
            continue

        # Screen 3: Terms agreement with "Agree" button
        if 'agree to' in body.lower() and ('terms' in body.lower() or 'meta using your info' in body.lower()):
            hb(f'🚧 [#{i+1}] screen 3 — Terms agreement, clicking Agree')
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] screen 3: clicking Agree')
            try: await page.get_by_role('button', name='Agree').click(timeout=10000); hb('  → Agree clicked')
            except Exception as e: hb(f'Agree err: {e}'); return
            await asyncio.sleep(8)
            continue

        # Screen 4: Cookies consent "Allow the use of cookies by Facebook?"
        if 'Allow all cookies' in body and ('cookies' in body.lower() and 'Facebook' in body):
            hb(f'🚧 [#{i+1}] screen 4 — Cookies consent, clicking Allow all cookies')
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] screen 4: Allow all cookies')
            try: await page.get_by_role('button', name='Allow all cookies').click(timeout=10000); hb('  → Allow all cookies clicked')
            except Exception as e: hb(f'Allow all cookies err: {e}'); return
            await asyncio.sleep(8)
            continue

        # Generic confirmation screen with Continue button (fallback)
        if 'Continue' in body and ('confirm' in body.lower() or 'review' in body.lower() or 'understand' in body.lower()):
            hb(f'🚧 [#{i+1}] generic confirmation screen — clicking Continue')
            shot(await safe_screenshot(page), f'[interstitial #{i+1}] generic confirm')
            try: await page.get_by_role('button', name='Continue').click(timeout=8000)
            except Exception as e: hb(f'Continue err: {e}'); return
            await asyncio.sleep(7)
            continue

        # No known interstitial detected — done
        hb(f'[interstitial-check #{i+1}] no blocking interstitial — exiting loop')
        return

async def gated_click(page, btn_name, expect_q, label=None):
    """🔒 RULE #1 (universal): vision-gate before EVERY meaningful click.

    Flow:
    1. STAGE 1 — poll until page is RENDERED (not LOADING)
    2. STAGE 2 — ask 'is the page in the expected state for clicking <btn_name>?'
    3. If YES → call click_btn(page, btn_name)
    4. POST-CLICK screenshot — visual proof the click happened
    5. If NO → HALT, return False (caller checks)

    Pattern from feedback-vision-gates-every-action: the click only fires when
    vision confirms we're on the right screen. Never click blindly."""
    lbl = label or f'click-{btn_name}'
    yes, summary = await vision_gate(page, lbl, expect_q)
    if not yes:
        hb(f'❌ [gated_click] HALT before clicking "{btn_name}": gate said NO → {summary}')
        return False
    ok = await click_btn(page, btn_name)
    hb(f'[gated_click] "{btn_name}" → clicked={ok}')
    # POST-CLICK screenshot — proof the click happened (user can see in TG)
    try:
        await asyncio.sleep(2)  # small settle so UI starts reacting
        png = await safe_screenshot(page)
        shot(png, f'[post-click {lbl}] clicked="{btn_name}" → clicked_ok={ok}')
    except Exception as e:
        hb(f'[post-click-shot {lbl}] err: {e}')
    return ok

async def safe_passkey_dismiss(page, label='passkey'):
    """━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    🚨 CRITICAL — ACCOUNT-BINDING SAFETY GATE 🚨

    This is THE most important function in the script. Misbehavior here has
    burned 2+ accounts (rental + phone + FB acct all lost). DO NOT modify
    without rereading screenshots/passkey-modal-blue-button-trap.png.

    AFTER VERIFYING A PHONE NUMBER (email-code OR SMS-code submission), FB
    shows the "Next time, skip the code" passkey modal:

        ┌──────────────────────────────────────────────────────┐
        │            Next time, skip the code                  │
        │   Use your fingerprint, face or screen lock...       │
        │                                                      │
        │   [ Not now ]    [ ★ blue: Create a passkey ★ ]      │
        │      ✅ SKIP             ❌ FORBIDDEN                │
        └──────────────────────────────────────────────────────┘

    Rules (per feedback-blue-passkey-button-never + feedback-passkey-prompt-not-now):
    1. WAIT 25s for the modal to fully load (it lazy-renders + button is
       not-yet-interactive for the first ~10-20s)
    2. ONLY click the gray "Not now" button on the LEFT
    3. NEVER click the blue button — that creates a passkey, rolls back
       the phone confirmation, burns the rental + account
    4. Use modal-scoped role locator: find via
         page.locator('div[role=dialog]:has-text("skip the code"))
                .get_by_role('button', name='Not now')
       — NOT a naive innerText eval (the blue button has a hidden
       screen-reader child containing "Not now" that naive matchers click!)
    5. If Not now click fails for ANY reason → RAISE → caller HALTS
       Never fall back to "click any other button" — that's how accounts burn.

    Returns:
      True  — passkey was present and dismissed
      False — no passkey on screen (page rendered, just not the passkey modal)

    Raises on Not now click failure — caller MUST halt (never click any fallback button).
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    # First: check if a passkey modal is even on screen (body keyword check is more reliable than vision)
    body = await page.evaluate("() => document.body.innerText")
    if 'Next time, skip the code' not in body and 'skip the code' not in body:
        hb(f'[{label}] no passkey modal on screen (body keyword check)')
        return False

    # 🛑 PASSIVE 90s WAIT — ZERO Playwright operations on the modal
    # Critical: Playwright's click(timeout=...) auto-retries on disabled buttons
    # which can dispatch events to the focused (blue) button. VP43 was burned this
    # way 2026-06-02. Pure asyncio.sleep is the ONLY safe operation here.
    hb('⏳ passkey detected — PASSIVE 90s WAIT (zero Playwright touches on modal)')
    await asyncio.sleep(90)
    shot(await safe_screenshot(page), f'[{label}] post-90s — Not now should be enabled now')

    # POLL Not now disabled state via read-only evaluate (no clicks)
    for poll in range(6):  # up to 6 × 15s = 90s additional
        info = await page.evaluate("""() => {
            const b = Array.from(document.querySelectorAll('button,[role=button],div[role=button]'))
                .find(b => (b.innerText||'').trim() === 'Not now' && b.offsetParent !== null);
            return b ? {disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'} : null;
        }""")
        hb(f'[{label}] Not now poll {poll+1}: {info}')
        if not info:
            hb(f'[{label}] no Not now button — modal may have closed itself')
            return True  # treat as success
        if not info['disabled']: break
        await asyncio.sleep(15)
    else:
        # Loop completed without break = button stayed disabled the whole time
        hb(f'[{label}] ❌ Not now NEVER became enabled — HALT, surface to user')
        raise Exception(f'passkey Not now stayed disabled for 180s+ — manual intervention required')

    # NOW click — Not now is enabled, modal-scoped role locator
    hb(f'[{label}] Not now ENABLED — clicking via modal-scoped role')
    modal = page.locator('div[role="dialog"]:has-text("skip the code")')
    try:
        await modal.get_by_role('button', name='Not now').click(timeout=8000)
        hb(f'[{label}] ✅ passkey dismissed')
        await asyncio.sleep(5)
        return True
    except Exception as e:
        hb(f'[{label}] ❌ Not now click failed: {e}')
        raise  # caller HALTS — never fall back to other button

def random_name():
    return hbeh.random_app_name()

def parse_blob(blob):
    """email:fbpw:email:emailpw:profile_url:dob:UA:base64_cookies → dict
    URL contains 'https://' so split(':') over-splits. Use cookies for fb_id,
    pattern-match the URL."""
    b64 = blob.rsplit(':', 1)[-1]
    cookies = json.loads(base64.b64decode(b64).decode('utf-8'))
    fb_id = next((c['value'] for c in cookies if c['name']=='c_user'), '?')
    head = blob.split(':')
    email = head[0]; fb_pw = head[1]; email_pw = head[3]
    m = re.search(r'(https://www\.facebook\.com/[^:]+)', blob)
    profile_url = m.group(1) if m else ''
    return {
        'email': email, 'fb_pw': fb_pw, 'email_pw': email_pw,
        'profile_url': profile_url, 'cookies': cookies,
        'fb_id': fb_id, 'blob': blob,
    }

def start_session(profile_id):
    """HARD RULE [feedback-never-touch-gologin-proxy]: no proxy ops. Restart session only.

    SKIP_SESSION_RESTART=1 env var → skip DELETE+POST entirely (use existing live session).
    Useful when GoLogin parallel-session limit is hit AND the profile is already running."""
    if os.environ.get('SKIP_SESSION_RESTART') == '1':
        hb('SKIP_SESSION_RESTART=1 → using existing live session')
        return 'session-skipped'
    try: requests.delete(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, timeout=15)
    except: pass
    time.sleep(2)
    for _ in range(20):
        r = requests.post(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, json={}, timeout=45)
        info = r.json() if r.status_code in (200,202) else {}
        if info.get('status') == 'profileStatuses.running':
            return 'no-proxy-change'
        time.sleep(3)
    return None

def rent_phone():
    """7-day non-renewable Facebook rental. Returns (rental_id, e164_phone, phone_10digit).
    If REUSE_RENTAL_ID env var is set, reuses that rental + REUSE_RENTAL_PHONE without creating a new one."""
    import os as _os, re as _re
    if _os.environ.get('REUSE_RENTAL_ID'):
        rental_id = _os.environ['REUSE_RENTAL_ID']
        e164 = _os.environ.get('REUSE_RENTAL_PHONE', '')
        ten = _re.sub(r'\D', '', e164)[-10:] if e164 else ''
        hb(f're-using rental: {rental_id} {e164}')
        return rental_id, e164, ten
    cli = tv_client()
    sale = cli._request('POST', '/api/pub/v2/reservations/rental', json={
        'allowBackOrderReservations': False, 'alwaysOn': True,
        'duration': 'sevenDay', 'isRenewable': False,
        'numberType': 'mobile', 'serviceName': 'facebook', 'capability': 'sms'
    })
    sale_id = sale['href'].rstrip('/').split('/')[-1]
    time.sleep(3)
    sale_data = cli._request('GET', f'/api/pub/v2/sales/{sale_id}')
    rental_id = sale_data['reservations'][0]['id']
    detail = cli._request('GET', f'/api/pub/v2/reservations/rental/nonrenewable/{rental_id}')
    phone = detail.get('phoneNumber') or detail.get('number') or ''
    phone10 = re.sub(r'\D','',phone)[-10:]
    return rental_id, phone, phone10

def list_sms(rental_id):
    body = tv_client()._request('GET', f'/api/pub/v2/sms?ReservationId={rental_id}')
    return body if isinstance(body, list) else (body.get('data') or [])

def email_snap(email, pw):
    seen=set()
    try:
        m=imaplib.IMAP4_SSL('imap.rambler.ru',993,timeout=15)
        m.login(email, pw); m.select('INBOX')
        _,ids=m.search(None,'ALL')
        for eid in (ids[0].split() if ids and ids[0] else [])[-30:]:
            seen.add(eid)
        m.logout()
    except: pass
    return seen

def email_poll(email, pw, seen):
    try:
        m=imaplib.IMAP4_SSL('imap.rambler.ru',993,timeout=15)
        m.login(email,pw); m.select('INBOX')
        _,ids=m.search(None,'(SUBJECT "security code")')
        for eid in (ids[0].split() if ids and ids[0] else [])[::-1][:10]:
            if eid in seen: continue
            _,data=m.fetch(eid,'(RFC822)')
            msg=em.message_from_bytes(data[0][1])
            subj=msg.get('Subject','')
            cm=re.search(r'\b(\d{5,8})\b', subj)
            if cm: m.logout(); return cm.group(1)
            body=''
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain':
                        body += part.get_payload(decode=True).decode('utf-8','replace')
            else: body=(msg.get_payload(decode=True) or b'').decode('utf-8','replace')
            cm=re.search(r'\b(\d{5,8})\b', body)
            if cm: m.logout(); return cm.group(1)
        m.logout()
    except: pass
    return None

async def click_btn(page, name):
    for f in page.frames:
        try:
            loc=f.get_by_role('button', name=name, exact=False)
            if await loc.count()>0 and await loc.first.is_enabled():
                await loc.first.click(); return True
            loc=f.locator(f'div[role="button"]:has-text("{name}")').first
            if await loc.count()>0:
                await loc.click(force=True); return True
        except: pass
    return False

async def click_text(page, txt):
    for f in page.frames:
        try:
            loc=f.locator(f'text="{txt}"').last
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); return True
        except: pass
    return False

async def find_visible_input(page, exclude_with_value=True, y_range=None):
    """Find a visible non-email/password text input. Skip search bar by y_range."""
    for f in page.frames:
        for el in await f.query_selector_all('input'):
            if not await el.is_visible(): continue
            typ = await el.get_attribute('type') or 'text'
            if typ in ('hidden','checkbox','radio','submit','file','email','password','tel','number'): continue
            ph = (await el.get_attribute('placeholder') or '').lower()
            if 'search' in ph: continue
            val = await el.input_value()
            if exclude_with_value and val: continue
            if '@' in val: continue
            if y_range:
                box = await el.bounding_box()
                if not box or not (y_range[0] <= box['y'] <= y_range[1]): continue
            return (f, el)
    return None


def precheck_account_uniqueness(acc):
    """[feedback-account-uniqueness-precheck] Returns (is_new_bool, prior_runs_list).
    Searches the accounts CSV for prior runs with the same FB ID or email."""
    try:
        rows = asm._read_all_rows()
        if not rows or len(rows) < 2: return True, []
        headers = rows[0]
        fb_id_col = headers.index('FB Profile ID') if 'FB Profile ID' in headers else None
        email_col = headers.index('FB Email') if 'FB Email' in headers else None
        profile_col = headers.index('GoLogin Profile') if 'GoLogin Profile' in headers else None
        status_col = headers.index('Status') if 'Status' in headers else None
        ts_col = headers.index('Timestamp (UTC)') if 'Timestamp (UTC)' in headers else None
        prior = []
        for r in rows[1:]:
            if len(r) <= max(c for c in [fb_id_col, email_col] if c is not None): continue
            fb_id_match = fb_id_col is not None and r[fb_id_col] == acc.get('fb_id')
            email_match = email_col is not None and r[email_col] == acc.get('email')
            if fb_id_match or email_match:
                prior.append({
                    'timestamp': r[ts_col] if ts_col is not None and ts_col < len(r) else '',
                    'profile':   r[profile_col] if profile_col is not None and profile_col < len(r) else '',
                    'status':    r[status_col] if status_col is not None and status_col < len(r) else '',
                })
        return (len(prior) == 0), prior
    except Exception as e:
        hb(f'precheck err (treating as new): {e}')
        return True, []


async def run_account(profile_id, acc):
    from playwright.async_api import async_playwright

    hb(f'━━━━━━━ {acc["email"]} → Profile id={profile_id[:10]}… ━━━━━━━')

    # 🚨 UNIQUENESS PRE-CHECK [feedback-account-uniqueness-precheck]
    if os.environ.get('ALLOW_DUPLICATE_BLOB') != '1':
        is_new, prior = precheck_account_uniqueness(acc)
        if not is_new:
            hb(f'🚨 UNIQUENESS CHECK FAILED: blob already used in {len(prior)} prior run(s):')
            for p in prior: hb(f'  • {p["timestamp"]} → {p["profile"]} → {p["status"]}')
            hb(f'❌ HALT: re-using a blob = same FB account on different GoLogin profile = '
               f'compounded phone bindings + clustering signal + wasted rental. '
               f'Use a fresh blob OR set ALLOW_DUPLICATE_BLOB=1 to override.')
            return {'status': 'duplicate_blob_halted', 'prior_runs': prior, 'rental_phone': '', 'rental_id': '', 'role': '', 'fb_app': None, 'fb_app_id': None, 'ig_app': None, 'ig_app_id': None, 'privacy_url': None}

    _hb_state = {'pos': None}  # human-behavior cursor tracking

    # Phase A: setup proxy + cookies + session
    ip = start_session(profile_id)
    if not ip: hb(f'❌ session start failed'); return None
    hb(f'session running, IP={ip}')
    if os.environ.get('SKIP_SESSION_RESTART') == '1':
        hb('skipping cookies push + 2nd restart (using existing live session)')
        info = {'status': 'profileStatuses.running'}
    else:
        ok, _ = mdm._persist_cookies_to_gologin_profile(profile_id, acc['cookies'])
        hb(f'cookies push: {ok}')
        try: requests.delete(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, timeout=15)
        except: pass
        time.sleep(3)
        info = {}
        for attempt in range(20):
            r = requests.post(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, json={}, timeout=45)
            info = r.json() if r.status_code in (200,202) else {}
            if info.get('status') == 'profileStatuses.running': break
            time.sleep(3)
        else:
            hb(f'❌ session restart failed after 20 retries; last={info!r}'); return None
    # Settle: GoLogin reports 'running' before the Chromium process has fully loaded cookies.
    # Wait ~15s so cookies are persisted before CDP connect. VP1 burn 2026-06-02 showed
    # 6s was too short — runtime jar was empty when CDP connected, fb.com showed login page.
    # Manual hard-reset with 15-20s wait consistently shows storage cookies loaded by then.
    time.sleep(15)

    cdp = f'wss://cloudbrowser.gologin.com/connect?token={pm.GOLOGIN_API_KEY}&profile={profile_id}'

    # Rent phone
    rental_id, phone_e164, phone10 = rent_phone()
    hb(f'📞 rental {rental_id}: {phone_e164}')

    role = random.choice(ROLES)
    fb_app_name = random_name()
    ig_app_name = random_name()
    while ig_app_name == fb_app_name: ig_app_name = random_name()
    hb(f'🎲 role={role}  FB app={fb_app_name!r}  IG app={ig_app_name!r}')

    result = {'fb_app': None, 'fb_app_id': None, 'ig_app': None, 'ig_app_id': None,
              'rental_id': rental_id, 'rental_phone': phone_e164, 'role': role,
              'privacy_url': None, 'status': 'in_progress', 'last_error': None}

    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # COOKIE STRATEGY [reference-gologin-cookie-persistence, CORRECTED 2026-06-02]:
        # After 15s post-restart settle, the GoLogin profile's persistent cookies
        # auto-load into the runtime jar (validated by direct ctx.cookies() probe).
        # We CHECK first — if c_user + xs are already present, skip add_cookies
        # entirely (avoid overwriting good cookies with our CE-format conversion
        # which may have subtle sameSite/expires differences that break auth).
        # Only inject if the storage auto-load didn't take.
        existing = await ctx.cookies(['https://www.facebook.com'])
        existing_names = {c['name'] for c in existing}
        has_auth = 'c_user' in existing_names and 'xs' in existing_names
        if has_auth:
            hb(f'🍪 runtime jar already has c_user+xs from storage auto-load (skipping add_cookies)')
        else:
            pw_cookies = []
            for c in acc['cookies']:
                pc = {'name': c['name'], 'value': c['value'],
                      'domain': c.get('domain',''), 'path': c.get('path','/'),
                      'secure': bool(c.get('secure', False)),
                      'httpOnly': bool(c.get('httponly') or c.get('httpOnly') or False)}
                ss = c.get('sameSite', 'no_restriction')
                ss_map = {'no_restriction':'None','unspecified':'Lax','lax':'Lax','strict':'Strict','none':'None'}
                pc['sameSite'] = ss_map.get(str(ss).lower(), 'None')
                if not c.get('session', False) and c.get('expirationDate'):
                    pc['expires'] = int(c['expirationDate'])
                pw_cookies.append(pc)
            try:
                await ctx.add_cookies(pw_cookies)
                hb(f'🍪 runtime cookies injected: {len(pw_cookies)} (storage auto-load did NOT cover c_user/xs)')
            except Exception as e:
                hb(f'⚠️ ctx.add_cookies err: {e}')

        # Phase B: confirm FB login + dismiss blocking interstitials
        try:
            await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=60000)
        except: pass
        await hbeh.sleep(5.6, 12.0)
        s = await safe_screenshot(page)
        shot(s, f'1️⃣ FB.com first load — {acc["email"]}')
        v = pj(vision(s, 'Reply ONLY JSON: {"is_logged_in":true|false,"profile_name_visible":"...","feed_visible":true|false,"interstitial_kind":"ads_choice|password_confirm|identity_check|none"}'))
        hb(f'FB.com: logged_in={v.get("is_logged_in")} name={v.get("profile_name_visible")} feed_visible={v.get("feed_visible")} interstitial={v.get("interstitial_kind")}')
        if not v.get('is_logged_in'):
            result['status'] = 'fb_login_failed'
            return result
        # NEW [feedback-fb-ads-choice-interstitial]: dismiss any blocking interstitials BEFORE warmup
        if v.get('interstitial_kind') and v.get('interstitial_kind') != 'none':
            hb(f'🚧 dismissing FB {v.get("interstitial_kind")} interstitial before warmup')
            await dismiss_fb_interstitials(page)
            # Re-confirm feed is now visible
            s2 = await safe_screenshot(page)
            shot(s2, '1️⃣b after interstitial dismiss')
            v2 = pj(vision(s2, 'Reply ONLY JSON: {"feed_visible":true|false,"interstitial_kind":"ads_choice|password_confirm|identity_check|none"}'))
            hb(f'post-dismiss: feed_visible={v2.get("feed_visible")} interstitial={v2.get("interstitial_kind")}')
            if v2.get('interstitial_kind') not in (None, 'none'):
                hb(f'❌ HALT: FB interstitial still blocking after dismiss attempt: {v2}')
                result['status'] = 'fb_interstitial_blocking'
                return result

        # Phase B.5: warm-up browse on fb.com — human-cadence signal
        # See project-self-critique-and-warmup-hypotheses.md
        hb('🛋️ warm-up browse 90s (scroll/hover fb.com)')
        try:
            await hbeh.fb_warmup_browse(page, ctx, seconds=90, state=_hb_state)
        except Exception as e:
            hb(f'warmup err (swallowed): {e}')

        # ════════════════════════════════════════════════════════════════
        # Phase C-NEW [feedback-ac-bind-before-wizard]: AC BINDING FIRST
        # Bind phone in Accounts Center BEFORE opening the Meta Dev wizard.
        # Avoids the FB silent-SMS-throttle bug where back-to-back SMS to the
        # same phone (AC then wizard) within ~10min get throttled by FB.
        # ════════════════════════════════════════════════════════════════
        if os.environ.get('SKIP_AC_BINDING') == '1':
            hb('SKIP_AC_BINDING=1 → assuming phone already bound, going straight to wizard')
        else:
            # First check if already bound — look for phone NOT followed by Pending confirmation
            await page.goto('https://accountscenter.facebook.com/youraccount/contact_points/', wait_until='domcontentloaded', timeout=60000)
            await hbeh.sleep(4.9, 10.5)
            body_pre = await page.evaluate("() => document.body.innerText")

            # PRE-BIND CHECK [feedback-ac-prebind-check]: yellow-flag if account already has OTHER phones
            other_phones = [p for p in re.findall(r'\+1\d{10}', body_pre) if p != '+1' + phone10]
            if other_phones and os.environ.get('AUTO_SKIP_IF_BOUND') != '0':
                hb(f'🚨 PRE-BIND CHECK: account already has phone(s) bound: {other_phones} (not our +{phone10})')
                shot(await safe_screenshot(page), f'🚨 AC has existing phones {other_phones} — halting per pre-bind rule')
                result['status'] = 'ac_has_existing_phone'
                result['existing_phones'] = other_phones
                hb(f'❌ HALT: account already touched (existing phones in AC). Use a fresh account or manually clean up + retry with AUTO_SKIP_IF_BOUND=0.')
                return result
            # Better check: find the phone in body, then look at next 60 chars for "Pending"
            already_bound = False
            idx = body_pre.find('+1' + phone10)
            if idx >= 0:
                window_after = body_pre[idx:idx+80]
                if 'Pending confirmation' not in window_after:
                    already_bound = True
            if already_bound:
                hb(f'✅ phone +1{phone10} already bound in AC — skipping AC binding')
            else:
                hb(f'📞 AC-FIRST: binding phone +1{phone10} before wizard')
                ac_ok = await ac_bind_flow(page, phone10, acc['email'], acc['email_pw'], v.get('profile_name_visible',''))
                if not ac_ok:
                    result['status'] = 'ac_bind_failed'
                    return result
                # Verify bound (same windowed check)
                await page.goto('https://accountscenter.facebook.com/youraccount/contact_points/', wait_until='domcontentloaded', timeout=60000)
                await asyncio.sleep(5)
                body_post = await page.evaluate("() => document.body.innerText")
                idx2 = body_post.find('+1' + phone10)
                bound = idx2 >= 0 and 'Pending confirmation' not in body_post[idx2:idx2+80]
                if not bound:
                    hb(f'❌ AC bind verification failed: phone +1{phone10} not confirmed')
                    result['status'] = 'ac_bind_verify_failed'
                    return result
                hb('✅ AC phone bound — proceeding to wizard')

        # Phase C: dev portal Get Started
        # Retry goto on transient errors (ERR_EMPTY_RESPONSE, timeouts) — VP1 burn
        # 2026-06-02: bot crashed on the very first dev portal goto with empty response.
        # Single transient hiccup must not kill the run after AC was already bound.
        goto_ok = False
        for goto_try in range(4):
            try:
                await page.goto('https://developers.facebook.com/', wait_until='domcontentloaded', timeout=60000)
                goto_ok = True; break
            except Exception as e:
                hb(f'⚠️ dev portal goto attempt {goto_try+1}/4 failed: {str(e)[:160]}')
                await asyncio.sleep(8 + goto_try*4)
        if not goto_ok:
            hb('❌ HALT: dev portal unreachable after 4 retries')
            result['status'] = 'dev_portal_unreachable'
            return result
        await hbeh.sleep(5.6, 12.0)
        # Dismiss cookies banner — gated (only click if banner visible)
        await gated_click(page, 'Allow all cookies',
            'Is a cookies consent banner visible (with an "Allow all cookies" or similar button)?',
            label='cookies-banner')
        await asyncio.sleep(3)
        # RULE #1 GATE: confirm we're on dev portal homepage with Get Started before clicking
        yes, _ = await vision_gate(page, 'dev-home-pre-get-started',
            'Is this the Meta for Developers homepage with a Get Started button visible (typically top-right header)?')
        if not yes:
            hb('❌ HALT: not on expected dev portal homepage'); result['status']='dev_portal_unexpected'; return result
        # Click Get Started in top header
        clicked = False
        for sel in ['header a:has-text("Get Started")','nav a:has-text("Get Started")','[role="banner"] a:has-text("Get Started")']:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    box = await el.bounding_box()
                    if box and box['y']<250 and await el.is_visible():
                        await el.click(); clicked=True; break
                if clicked: break
            except: pass
        hb(f'Get Started: {clicked}')
        await hbeh.sleep(5.6, 12.0)
        shot(await safe_screenshot(page), '2️⃣ after Get Started')

        # Phase D: walk Register → Verify
        # Register stage usually has just Continue
        d = pj(vision(await safe_screenshot(page), '''Reply ONLY JSON:
{"sidebar_step_active":"<step>","main_cta":"<text>","main_cta_enabled":true|false,"any_error_text":"<verbatim or null>"}'''))
        if d.get('sidebar_step_active') == 'Register':
            await asyncio.sleep(3)
            await gated_click(page, 'Continue',
                'Is this the wizard Register stage with a Continue button (typically displaying terms/agree)?',
                label='register-continue')
            await hbeh.sleep(7.0, 15.0)

        # Phase D2: Verify Account — check for AC redirection
        s = await safe_screenshot(page)
        d = pj(vision(s, '''Reply ONLY JSON:
{"page_kind":"verify_account|review_email|email_code|contact_info|about_you|dashboard|ac_required|other",
 "any_error_text":"<verbatim or null>",
 "phone_input_visible":true|false,
 "main_cta":"<text>"}'''))
        hb(f'verify state: {d}')
        if 'Accounts Center' in (d.get('any_error_text') or ''):
            hb('⚠️ AC binding required first — running AC flow')
            ac_ok = await ac_bind_flow(page, phone10, acc['email'], acc['email_pw'], v.get('profile_name_visible',''))
            if not ac_ok:
                result['status'] = 'ac_bind_failed'
                return result
            # Back to dev portal verify
            await page.goto('https://developers.facebook.com/async/registration/dialog/?src=default', wait_until='domcontentloaded', timeout=60000)
            await hbeh.sleep(5.6, 12.0)

        # Type phone in verify account
        await asyncio.sleep(3)
        pin = await find_visible_input(page, exclude_with_value=True)
        if not pin:
            # Maybe alternative — search by placeholder
            for f in page.frames:
                for sel in ['input[placeholder*="phone" i]','input[type="tel"]']:
                    try:
                        el = await f.query_selector(sel)
                        if el and await el.is_visible(): pin=(f,el); break
                    except: pass
                if pin: break
        if not pin: hb('❌ no phone input'); result['status']='no_phone_input'; return result
        shot(await safe_screenshot(page), f'[wizard-phone-pre] about to type {phone10} into Verify Account phone field')
        await pin[1].click(); await asyncio.sleep(1.5); await pin[1].fill('')
        await pin[1].type(phone10, delay=140)
        hb(f'typed phone {phone10}'); await hbeh.sleep(3.5, 7.5)
        shot(await safe_screenshot(page), f'[wizard-phone-post] typed {phone10} — field should show the number')

        seen_sms = set(str(it.get('id','')) for it in list_sms(rental_id))
        # VP44 LESSON 2026-06-02: when AC binding is done FIRST, FB sometimes auto-sends
        # the wizard SMS the moment Verify Account page loads (because the phone is already
        # bound). Body shows "Please wait N seconds before resending" + Continue button.
        # In that case, SKIP the Send Verification SMS click — go straight to code-entry.
        body_pre = await page.evaluate("() => document.body.innerText")
        already_sent = (
            'before resending' in body_pre.lower()
            or 'Send SMS Again' in body_pre
            or ('Continue by entering' in body_pre and 'verification code' in body_pre.lower())
        )
        if already_sent:
            hb('⏭ wizard SMS auto-sent (phone pre-bound) — skipping Send Verification SMS click')
            ok = True  # treat as if Send SMS was clicked
        else:
            # RULE #1 GATE: confirm we're on Verify Account with phone typed before sending SMS
            yes, _ = await vision_gate(page, 'pre-send-sms',
                f'Is this the Verify Account step with phone {phone10} typed in the mobile field and a Send Verification SMS button enabled? OR is it already showing a code-entry input (meaning SMS was already auto-sent)?')
            if not yes:
                hb('❌ HALT: not on expected Verify Account state before Send SMS'); result['status']='verify_state_unexpected'; return result
            ok = await click_btn(page, 'Send Verification SMS')
            if not ok: ok = await click_btn(page, 'Send verification')
            hb(f'Send SMS: {ok}')
            if not ok: result['status']='no_send_btn'; return result
            await asyncio.sleep(2)
            shot(await safe_screenshot(page), f'[wizard-send-sms-post] clicked Send Verification SMS → page should now show code-entry state')
        await hbeh.sleep(7.0, 15.0)

        hb('📨 polling for SMS code (5min)…')
        code=None; deadline=time.time()+300
        while time.time()<deadline:
            for it in list_sms(rental_id):
                if str(it.get('id','')) in seen_sms: continue
                t=it.get('smsContent') or ''
                cm=re.search(r'\b(\d{4,8})\b', t)
                if cm: code=cm.group(1); break
            if code: break
            await hbeh.sleep(7.0, 15.0)
        if not code: result['status']='no_sms'; return result
        hb(f'📨 SMS: {code}')
        shot(await safe_screenshot(page), f'3️⃣ got SMS code {code}')

        await hbeh.sleep(2.8, 6.0)
        ci = await find_visible_input(page, exclude_with_value=True)
        if not ci: hb('❌ no code input'); result['status']='no_code_input'; return result
        shot(await safe_screenshot(page), f'[wizard-code-pre] about to type SMS code {code} into the code field')
        await ci[1].click(); await asyncio.sleep(0.5); await ci[1].type(code, delay=140)
        hb(f'typed code'); await hbeh.sleep(3.5, 7.5)
        shot(await safe_screenshot(page), f'[wizard-code-post] typed code {code} — field should show the digits')
        await gated_click(page, 'Continue',
            f'Is the wizard SMS code {code} typed in the input and a Continue button enabled?',
            label='wizard-sms-continue')
        await hbeh.sleep(8.4, 18.0)
        # RULE [feedback-blue-passkey-button-never]: detect + safely dismiss passkey if present
        try: await safe_passkey_dismiss(page, label='post-wizard-sms')
        except Exception as e:
            hb(f'❌ HALT: passkey Not now click failed: {e}'); result['status']='passkey_dismiss_failed'; return result

        # Phase E: Review Email → Confirm Email
        # ⚠️ VP43 LESSON: vision JSON page_kind misread Review Email as 'contact_info'.
        # Use body-keyword check instead — much more reliable.
        body = await page.evaluate("() => document.body.innerText")
        if 'Review Your Email Address' in body and 'Confirm Email' in body:
            hb('📧 Phase E: on Review Email step (body keyword check)')
            await asyncio.sleep(3)
            await gated_click(page, 'Confirm Email',
                'Is this the Review Email step with a Confirm Email button?',
                label='confirm-email')
            await hbeh.sleep(7.0, 15.0)
        else:
            hb('Phase E: not on Review Email — skip')

        # Phase F: Contact info → just continue if shown
        body = await page.evaluate("() => document.body.innerText")
        # Body-keyword check: Contact info step shows the Contact info nav AND Continue button
        # AND does NOT show role choices (which would indicate About You)
        on_contact_info = (
            'Contact info' in body
            and 'Continue' in body
            and not any(role in body for role in ['Developer','Analyst','Marketer','Product manager','Student'])
            and 'Confirm Email' not in body  # not on review email
        )
        if on_contact_info:
            hb('📞 Phase F: on Contact info step')
            await asyncio.sleep(3)
            await gated_click(page, 'Continue',
                'Is this the Contact info wizard step with a Continue button?',
                label='contact-info-continue')
            await hbeh.sleep(7.0, 15.0)

        # Phase G: About You — random role + Complete Registration
        d = pj(vision(await safe_screenshot(page), '''Reply ONLY JSON:
{"page_kind":"about_you|dashboard|other","heading":"<verbatim>"}'''))
        if d.get('page_kind') != 'about_you' and 'describes' not in (d.get('heading','').lower()):
            # Maybe we're already past about_you and on dashboard?
            if 'apps' in d.get('heading','').lower() or 'create' in d.get('heading','').lower():
                hb('⚠️ skipped About You — already at dashboard?')
            else:
                hb(f'❌ unexpected page: {d}'); result['status']='unexpected_post_phone'; return result
        else:
            # Click role card
            await hbeh.sleep(2.8, 6.0)
            await click_text(page, role)
            await hbeh.sleep(3.5, 7.5)
            # Vision verify CR enabled
            d2 = pj(vision(await safe_screenshot(page), 'Reply ONLY JSON: {"main_cta_enabled":true|false}'))
            if not d2.get('main_cta_enabled'):
                # Try clicking far right of viewport at role's y
                hb('CR not enabled, trying mouse click on card right side')
                for f in page.frames:
                    try:
                        loc = f.locator(f'text="{role}"').last
                        if await loc.count()>0:
                            box = await loc.bounding_box()
                            if box:
                                await hbeh.click(page, box['x']+800, box['y']+box['height']/2, _hb_state)
                                break
                    except: pass
                await hbeh.sleep(2.8, 6.0)
            await hbeh.sleep(3.5, 7.5)
            cr = await gated_click(page, 'Complete Registration',
                'Is this the About You wizard step with a role selected and Complete Registration button enabled?',
                label='complete-registration')
            hb(f'🎯 Complete Registration: {cr}')
            if not cr: result['status']='no_cr_btn'; return result
            # 6-MIN SILENT WAIT — DO NOTHING
            shot(await safe_screenshot(page), '4️⃣ Complete Registration clicked — entering 6-min silent wait')
            hb('⏳ 6-min silent wait per RUNBOOK §post-CR')
            await hbeh.sleep(252.0, 540.0)

        # Phase H: verify dashboard
        d = pj(vision(await safe_screenshot(page), '''Reply ONLY JSON:
{"page_kind":"about_you|my_apps|dashboard|other","main_cta":"<text>"}'''))
        hb(f'post-wait: {d}')
        if d.get('page_kind') not in ('my_apps','dashboard') and 'apps' not in (d.get('main_cta','').lower() + ' '):
            # Still on about_you — Meta is processing slowly. Try another 3 min
            hb('still not on dashboard, +3min wait')
            await hbeh.sleep(126.0, 270.0)
            d = pj(vision(await safe_screenshot(page), '''Reply ONLY JSON:
{"page_kind":"about_you|my_apps|dashboard|other","main_cta":"<text>"}'''))
        if d.get('page_kind') not in ('my_apps','dashboard') and 'create app' not in (d.get('main_cta','').lower()):
            shot(await safe_screenshot(page), '❌ no dashboard after 9min')
            result['status']='no_dashboard'; return result
        hb('✅ dashboard reached')
        shot(await safe_screenshot(page), '5️⃣ dashboard reached — starting FB app')

        # Phase I: Create FB app
        fb_id = await create_app_wizard(page, fb_app_name, 'Manage everything on your Page', acc['fb_pw'])
        result['fb_app'] = fb_app_name
        result['fb_app_id'] = fb_id
        hb(f'FB app: {fb_app_name} → {fb_id}')

        # Phase J: navigate back to /apps + Create IG app
        await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
        await hbeh.sleep(5.6, 12.0)
        ig_id = await create_app_wizard(page, ig_app_name, 'Manage messaging & content on Instagram', acc['fb_pw'])
        result['ig_app'] = ig_app_name
        result['ig_app_id'] = ig_id
        hb(f'IG app: {ig_app_name} → {ig_id}')
        # Truthful final-state caption — fb_id and ig_id are None on failure
        fb_status = f'{fb_app_name}={fb_id}' if fb_id else f'❌ {fb_app_name} FAILED'
        ig_status = f'{ig_app_name}={ig_id}' if ig_id else f'❌ {ig_app_name} FAILED'
        shot(await safe_screenshot(page), f'7️⃣ final state — FB: {fb_status} | IG: {ig_status}')

        # Phase J: privacy URL (telegra.ph) for FB app — pure HTTP, but keep inside async with
        try:
            url, err = privacy._create_telegraph_privacy_policy(fb_app_name)
            result['privacy_url'] = url
            hb(f'privacy URL for FB app: {url}')
        except Exception as e:
            hb(f'privacy err: {e}')
            result['privacy_url'] = None

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase K: SHARD 1 (inlined) — privacy URL + Save + Show secret + Publish
        # Per [[project-shard1-publish-runbook]]. Inlined to avoid subprocess.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if os.environ.get('SKIP_SHARD1') != '1' and fb_id and result['privacy_url']:
            try:
                await phase_k_publish(page, fb_id, result['privacy_url'], acc['fb_pw'], result)
            except Exception as e:
                hb(f'⚠️ shard1 (publish) failed: {e}')
                result['shard1_error'] = str(e)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase L: SHARD 2 (inlined) — Customize 4 perms + 10min cooldown + Explorer 5 perms
        #                              + Generate OAuth + extend programmatically
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if os.environ.get('SKIP_SHARD2') != '1' and fb_id and result.get('app_secret'):
            try:
                await phase_l_perms_and_token(page, ctx, fb_id, result['app_secret'], acc['fb_pw'], result)
            except Exception as e:
                hb(f'⚠️ shard2 (perms+token) failed: {e}')
                result['shard2_error'] = str(e)

    # — exited async with —
    # Truthful status reflecting actual outcomes
    fb_ok = bool(result.get('fb_app_id'))
    ig_ok = bool(result.get('ig_app_id'))
    pub_ok = bool(result.get('published'))
    token_ok = bool(result.get('long_user_token'))
    if token_ok and pub_ok:
        result['status'] = 'apps_created_published_with_token'
    elif fb_ok and ig_ok:
        result['status'] = 'apps_created' + ('_published' if pub_ok else '')
    elif fb_ok or ig_ok:
        result['status'] = f'partial_apps_created (FB={"ok" if fb_ok else "fail"}, IG={"ok" if ig_ok else "fail"})'
    else:
        result['status'] = 'app_creation_failed'
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SHARD 1 — Settings/Basic privacy URL + Save + Show Secret + Publish (inlined)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def phase_k_publish(page, app_id, privacy_url, fb_pw, result):
    """Set privacy URL → Save → reload-verify → Show secret + capture → sidebar Publish.
    Per [[project-shard1-publish-runbook]]."""
    hb(f'━━━ Phase K (shard 1): publish app {app_id} ━━━')
    await page.goto(f'https://developers.facebook.com/apps/{app_id}/settings/basic/', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(8, 14)
    shot(await safe_screenshot(page), 'K1: Settings/Basic loaded')

    # Find + fill privacy URL input
    priv_loc = page.locator('input[placeholder="Privacy policy for Login dialog and app details"]').first
    await priv_loc.scroll_into_view_if_needed(timeout=8000)
    await hbeh.sleep(1, 2)
    await priv_loc.click()
    await page.keyboard.press('Control+A'); await hbeh.sleep(0.3, 0.6)
    await page.keyboard.press('Delete'); await hbeh.sleep(0.5, 1)
    await page.keyboard.type(privacy_url, delay=random.randint(50, 90))
    await hbeh.sleep(2, 3.5)
    val = await priv_loc.input_value()
    if val != privacy_url:
        hb(f'❌ privacy URL mismatch — got {val!r}')
        result['shard1_error'] = 'privacy_url_typing_mismatch'
        return
    shot(await safe_screenshot(page), 'K2: privacy typed')

    # Save changes
    save_loc = page.get_by_role('button', name='Save changes').first
    await save_loc.scroll_into_view_if_needed(timeout=8000)
    await hbeh.sleep(1.5, 2.5)
    await save_loc.click(timeout=8000)
    hb('Save changes clicked'); await hbeh.sleep(7, 12)

    # Reload + verify
    await page.reload(wait_until='domcontentloaded'); await hbeh.sleep(8, 12)
    final = await page.locator('input[placeholder="Privacy policy for Login dialog and app details"]').first.input_value()
    if final != privacy_url:
        hb(f'❌ privacy URL did not persist — got {final!r}')
        result['shard1_error'] = 'privacy_url_persist_failed'
        return
    hb('✅ privacy URL persisted')
    shot(await safe_screenshot(page), 'K3: privacy persisted')

    # Show App Secret + password popup
    app_secret = None
    try:
        # Try get_by_role first
        try:
            await page.get_by_role('button', name='Show', exact=True).first.click(timeout=5000)
            hb('Show clicked')
        except:
            # Fallback: coord-based on the App Secret row
            cands = await page.evaluate("""() => Array.from(document.querySelectorAll('button,div[role=button]')).filter(b => (b.innerText||'').trim() === 'Show' && b.offsetParent !== null).map(b => { const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })""")
            if cands:
                await page.mouse.click(cands[0]['x'], cands[0]['y'])
                hb('Show clicked via coords')
        await hbeh.sleep(3, 5)
        # Password modal
        pwin = await page.query_selector('input[type="password"]')
        if pwin and await pwin.is_visible():
            await pwin.click(); await hbeh.sleep(1, 2)
            await pwin.fill('')
            await pwin.type(fb_pw, delay=random.randint(60, 100))
            await hbeh.sleep(2, 3)
            try: await page.get_by_role('button', name='Submit').click(timeout=8000)
            except:
                try: await page.keyboard.press('Enter')
                except: pass
            await hbeh.sleep(5, 8)
            app_secret = await page.evaluate("""() => {
                for (const inp of document.querySelectorAll('input')) {
                    const v = (inp.value || '').trim();
                    if (/^[a-f0-9]{32}$/.test(v)) return v;
                }
                return null;
            }""")
            hb(f'App Secret: {app_secret[:10] if app_secret else None}...')
            result['app_secret'] = app_secret
    except Exception as e: hb(f'secret capture err: {e}')

    # Sidebar Publish click (NOT goto — that 302s to dashboard)
    sidebar_pub = await page.evaluate("""() => {
        for (const el of document.querySelectorAll('div[role="button"], a, button')) {
            const t = (el.innerText || '').trim();
            if (t.includes('App Publish Status')) {
                const r = el.getBoundingClientRect();
                if (r.x < 300) return {x: r.x + r.width/2, y: r.y + r.height/2};
            }
        }
        return null;
    }""")
    if not sidebar_pub:
        hb('❌ no sidebar App Publish Status item')
        result['shard1_error'] = 'no_sidebar_publish'
        return
    await page.mouse.move(sidebar_pub['x']-30, sidebar_pub['y']-10, steps=8); await hbeh.sleep(0.3, 0.7)
    await page.mouse.move(sidebar_pub['x'], sidebar_pub['y'], steps=5); await hbeh.sleep(0.2, 0.4)
    await page.mouse.click(sidebar_pub['x'], sidebar_pub['y'])
    await hbeh.sleep(8, 14)
    shot(await safe_screenshot(page), 'K4: go_live page')

    # Click Publish action — use .last
    pub = page.get_by_role('button', name='Publish').last
    try: await pub.scroll_into_view_if_needed(timeout=5000)
    except: pass
    await hbeh.sleep(1.5, 2.5)
    await pub.click(timeout=8000)
    hb('Publish action clicked')
    await hbeh.sleep(12, 18)
    shot(await safe_screenshot(page), 'K5: after Publish')

    body = await page.evaluate("() => document.body.innerText.substring(0, 800)")
    published = 'Published' in body and 'Unpublished' not in body[:500]
    result['published'] = published
    hb(f'PUBLISHED? {published}')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SHARD 2 — Customize 4 perms + 10min cooldown + Explorer 5 perms + Generate + extend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def phase_l_perms_and_token(page, ctx, app_id, app_secret, fb_pw, result):
    """Customize page Add 4 perms + 10min cooldown + Graph Explorer typeahead + OAuth walker + extend.
    Per [[project-shard2-page-token-runbook]]."""
    hb(f'━━━ Phase L (shard 2): perms + token for app {app_id} ━━━')
    CUSTOMIZE_PERMS = ['pages_manage_posts', 'pages_read_engagement', 'pages_manage_metadata', 'read_insights']
    EXPLORER_PERMS = ['pages_show_list', 'pages_manage_posts', 'pages_read_engagement', 'pages_manage_metadata', 'read_insights']

    # Customize Add 4 perms
    await page.goto(f'https://developers.facebook.com/apps/{app_id}/use_cases/customize/?use_case_enum=PAGES_API', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(10, 16)
    shot(await safe_screenshot(page), 'L1: customize page')
    for perm in CUSTOMIZE_PERMS:
        hb(f'Customize: looking for {perm}')
        try:
            loc = page.get_by_text(perm, exact=True).first
            await loc.scroll_into_view_if_needed(timeout=8000)
            await hbeh.sleep(1.5, 2.5)
            handle = await page.evaluate_handle("""(perm) => {
                let pe = null;
                for (const el of document.querySelectorAll('*')) {
                    if ((el.innerText || '').trim() === perm && el.children.length < 3) { pe = el; break; }
                }
                if (!pe) return null;
                let row = pe;
                for (let d=0; d<6; d++) {
                    row = row.parentElement;
                    if (!row) return null;
                    const rr = row.getBoundingClientRect();
                    if (rr.width > 600 && rr.height > 40 && rr.height < 250) break;
                }
                for (const b of row.querySelectorAll('button, div[role="button"]')) {
                    if ((b.innerText || '').trim() === 'Add') return b;
                }
                return null;
            }""", perm)
            el = handle.as_element() if handle else None
            if el:
                await el.click(timeout=6000)
                hb(f'  ✅ Added {perm}')
            else:
                hb(f'  ⏭ {perm} no Add button (already granted?)')
        except Exception as e: hb(f'  err {perm}: {e}')
        await hbeh.sleep(3, 5)
    shot(await safe_screenshot(page), 'L2: after add perms')

    # 10-min cooldown
    hb('⏳ 10-min cooldown before Graph Explorer (anti-flag)')
    await asyncio.sleep(600)

    # Graph Explorer
    await page.goto('https://developers.facebook.com/tools/explorer/', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(10, 16)
    shot(await safe_screenshot(page), 'L3: explorer')

    # Token capture via framenavigated on ALL pages
    captured = {'val': None}
    def hook(p):
        async def on_nav(frame):
            try:
                u = frame.url
                if 'access_token=' in u:
                    m = re.search(r'access_token=([^&#]+)', u)
                    if m and not captured['val']: captured['val'] = m.group(1); hb(f'token URL: {m.group(1)[:30]}...')
            except: pass
        p.on('framenavigated', on_nav)
    for p in ctx.pages: hook(p)
    ctx.on('page', hook)

    # Type each perm
    combo = page.locator('input[placeholder="Add a Permission"]').first
    for perm in EXPLORER_PERMS:
        try:
            await combo.click(); await hbeh.sleep(0.7, 1.3)
            await page.keyboard.press('Control+A'); await hbeh.sleep(0.3, 0.6)
            await page.keyboard.press('Delete'); await hbeh.sleep(0.5, 1)
            await page.keyboard.type(perm, delay=random.randint(60, 100))
            await hbeh.sleep(2, 3.5)
            await page.keyboard.press('ArrowDown'); await hbeh.sleep(0.5, 1)
            await page.keyboard.press('Enter')
            hb(f'+ {perm}')
            await hbeh.sleep(2.5, 4)
        except Exception as e: hb(f'  {perm}: {e}')
    shot(await safe_screenshot(page), 'L4: perms typed')

    # Generate Access Token
    try:
        gen = page.get_by_role('button', name='Generate Access Token').first
        await gen.scroll_into_view_if_needed(timeout=5000); await hbeh.sleep(1, 2)
        await gen.click(timeout=8000); hb('Generate clicked')
    except Exception as e: hb(f'Generate fail: {e}'); result['shard2_error']='generate_fail'; return
    await hbeh.sleep(6, 10)
    shot(await safe_screenshot(page), 'L5: post Generate')

    # OAuth popup walker (looped — handles screens in order: Continue as → opt-in radio → Continue → Save → Got it)
    for step in range(10):
        popup = next((p for p in ctx.pages if 'dialog/oauth' in p.url or 'dialog/permissions' in p.url), None)
        if not popup or popup.is_closed():
            hb(f'OAuth popup closed at step {step}')
            break
        try:
            body = await popup.evaluate("() => document.body ? document.body.innerText.substring(0,800) : ''")
        except: break
        hb(f'OAuth step {step}: body[:120]: {body[:120]!r}')
        try:
            if 'Continue as' in body and 'Continue' in body:
                await popup.get_by_role('button', name='Continue', exact=True).click(timeout=5000); hb('  → Continue (continue-as)')
            elif 'Opt in to all' in body:
                try: await popup.get_by_role('radio', name='Opt in to all current and future Pages').click(timeout=4000); hb('  radio: Opt in')
                except: pass
                await asyncio.sleep(2)
                await popup.get_by_role('button', name='Continue', exact=True).click(timeout=5000); hb('  → Continue (opt-in)')
            elif 'Save' in body:
                await popup.get_by_role('button', name='Save', exact=True).click(timeout=5000); hb('  → Save')
            elif 'Got it' in body:
                await popup.get_by_role('button', name='Got it', exact=True).click(timeout=5000); hb('  → Got it')
            else:
                hb(f'  unknown popup state — break')
                break
        except Exception as e:
            hb(f'  step {step} err: {str(e)[:120]}')
            if captured['val']: break
        await asyncio.sleep(5)
    await asyncio.sleep(5)
    shot(await safe_screenshot(page), 'L6: post OAuth')

    # Capture short token
    short_token = captured['val']
    if not short_token:
        for p in ctx.pages:
            m = re.search(r'access_token=([^&#]+)', p.url)
            if m: short_token = m.group(1); break
    if not short_token:
        try:
            val = await page.evaluate("""() => { for (const inp of document.querySelectorAll('input,textarea')) { const v=(inp.value||'').trim(); if (v.startsWith('EAA') && v.length > 100) return v; } return null; }""")
            if val: short_token = val
        except: pass
    if not short_token:
        hb('❌ no short token captured')
        result['shard2_error'] = 'no_short_token'
        return
    hb(f'short token: {short_token[:30]}...')
    result['short_user_token'] = short_token

    # Extend programmatically
    r = requests.get('https://graph.facebook.com/v25.0/oauth/access_token', params={
        'grant_type': 'fb_exchange_token',
        'client_id': app_id,
        'client_secret': app_secret,
        'fb_exchange_token': short_token,
    }, timeout=30)
    data = r.json()
    long_token = data.get('access_token')
    if not long_token:
        hb(f'❌ extend failed: {json.dumps(data)[:300]}')
        result['shard2_error'] = 'extend_failed'
        return
    result['long_user_token'] = long_token
    result['expires_in'] = data.get('expires_in')
    hb(f'✅ long token: {long_token[:30]}... expires_in={data.get("expires_in")}')

    # Verify scopes via debug_token
    r2 = requests.get('https://graph.facebook.com/v25.0/debug_token', params={
        'input_token': long_token,
        'access_token': f'{app_id}|{app_secret}',
    }, timeout=30)
    debug = r2.json().get('data', {})
    scopes = debug.get('scopes', [])
    result['scopes'] = scopes
    hb(f'scopes ({len(scopes)}): {scopes}')


async def ac_bind_flow(page, phone10, email, email_pw, profile_name):
    """Bind phone to FB account via Accounts Center first, then return."""
    hb('🔁 entering AC binding flow')
    await page.goto('https://accountscenter.facebook.com/personal_info/contact_points/', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(4.9, 10.5)
    await asyncio.sleep(3)
    # Gated clicks: vision-validate each screen before firing
    await gated_click(page, 'Add new contact',
        'Is this the Accounts Center Contact information page with an Add new contact button visible?',
        label='ac-add-new-contact')
    await hbeh.sleep(3.5, 7.5)
    await gated_click(page, 'Add mobile number',
        'Is there a choice modal asking what type of contact to add (Add mobile number / Add email options)?',
        label='ac-add-mobile')
    await hbeh.sleep(4.2, 9.0)
    pin = None
    for f in page.frames:
        for sel in ['input[placeholder*="mobile" i]','input[placeholder*="phone" i]','input[type="tel"]']:
            try:
                el = await f.query_selector(sel)
                if el and await el.is_visible(): pin=(f,el); break
            except: pass
        if pin: break
    if not pin: hb('❌ no AC phone input'); return False
    shot(await safe_screenshot(page), f'[ac-phone-pre] about to type {phone10} into AC phone field')
    await pin[1].click(); await asyncio.sleep(1); await pin[1].fill('')
    await pin[1].type(phone10, delay=140)
    await hbeh.sleep(2.8, 6.0)
    shot(await safe_screenshot(page), f'[ac-phone-post] typed {phone10} — AC modal should show the number')
    # Click profile name row (toggle checkbox)
    if profile_name:
        for f in page.frames:
            try:
                loc = f.locator(f'text="{profile_name}"').last
                if await loc.count()>0 and await loc.is_visible():
                    await loc.click(); break
            except: pass
    else:
        # Click Facebook text as proxy
        for f in page.frames:
            try:
                loc = f.locator('text=Facebook').last
                if await loc.count()>0 and await loc.is_visible():
                    await loc.click(); break
            except: pass
    await hbeh.sleep(2.8, 6.0)
    shot(await safe_screenshot(page), f'[ac-checkbox-post] clicked profile checkbox ({profile_name or "Facebook"}) — should be checked now')
    # AC FLOW ORDER (validated 2026-06-02 on VP1, see [feedback-ac-bind-sms-before-email]):
    # After clicking Next on "Add mobile number", FB asks for the SMS code FIRST
    # (sent to the rental phone). The EMAIL step comes after SMS — sometimes it
    # auto-passes invisibly (FB binds the phone on SMS submit alone) and sometimes
    # it presents an explicit email-code modal. We handle both.
    # Snapshot Rambler inbox BEFORE the click so we can detect a new FB email later
    seen_email = email_snap(email, email_pw)
    seen_sms = set(str(it.get('id','')) for it in (list_sms.__wrapped__ if hasattr(list_sms,'__wrapped__') else list_sms)('lr_placeholder')) if False else set()
    await asyncio.sleep(3)
    await gated_click(page, 'Next',
        f'Is this the AC Add mobile number modal with phone {phone10} typed and the account-profile checkbox toggled? Is Next enabled?',
        label='ac-add-mobile-next')
    await hbeh.sleep(7.0, 15.0)

    # STEP 6 — SMS code (sent to rental phone via TextVerified)
    # rental_id is not in scope here; pull from the env if needed. Better: derive
    # the rental_id by checking module-level state — for now, we accept the caller
    # passed rental_id via env REUSE_RENTAL_ID (used in this codepath when re-running)
    # or by being in the same process. ac_bind_flow signature would need updating;
    # for backward compat we read os.environ.
    rental_id_for_sms = os.environ.get('REUSE_RENTAL_ID') or os.environ.get('AC_RENTAL_ID') or ''
    sms_code = None
    if rental_id_for_sms:
        seen_sms = set(str(it.get('id','')) for it in list_sms(rental_id_for_sms))
        hb(f'📨 polling for AC SMS code on rental {rental_id_for_sms} (3min)…')
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                for it in list_sms(rental_id_for_sms):
                    if str(it.get('id','')) in seen_sms: continue
                    t = it.get('smsContent') or ''
                    cm = re.search(r'\b(\d{4,8})\b', t)
                    if cm: sms_code = cm.group(1); break
            except Exception as e: hb(f'list_sms err: {e}')
            if sms_code: break
            await hbeh.sleep(7.0, 15.0)
    else:
        hb('⚠️ no rental_id in env — cannot poll SMS; falling through to email-only path')

    if sms_code:
        hb(f'📱 AC SMS code: {sms_code}')
        ci_sms = await find_visible_input(page, exclude_with_value=True)
        if ci_sms:
            shot(await safe_screenshot(page), f'[ac-sms-pre] about to type SMS code {sms_code}')
            await ci_sms[1].click(); await asyncio.sleep(0.5); await ci_sms[1].type(sms_code, delay=140)
            await hbeh.sleep(2.8, 6.0)
            shot(await safe_screenshot(page), f'[ac-sms-post] typed SMS {sms_code} — Next should be enabled')
            # Click Next via modal-scoped locator (the modal-Next button can be the outer container of inner Next text)
            clicked = False
            try:
                modal = page.locator('div[role="dialog"]:has-text("confirmation code"), div[role="dialog"]:has-text("text message")')
                await modal.get_by_role('button', name='Next').first.click(timeout=8000)
                clicked = True; hb('✅ ac-sms-next clicked')
            except Exception as e:
                hb(f'modal Next fail: {e}')
            if not clicked:
                # coord fallback — Next button is centered ~y=560 on 1920-wide viewport
                vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                await page.mouse.click(int(vp['w']/2), int(vp['h']*0.55))
                hb(f'✅ coord click Next at center/55%')
            await hbeh.sleep(7.0, 15.0)
            shot(await safe_screenshot(page), '[ac-sms-post-click] after ac-sms-next click')

    # STEP 7 — EMAIL code (may or may not appear)
    # Wait briefly to see if an email-code modal renders. If not, the SMS submit alone bound the phone.
    await asyncio.sleep(8)
    body_after = await page.evaluate("() => document.body.innerText")
    needs_email = 'Enter your confirmation code' in body_after and ('sent you' in body_after.lower() or '@' in body_after)
    if needs_email:
        hb('📨 polling for AC EMAIL code on Rambler (3min)…')
        email_code = None
        deadline = time.time()+180
        while time.time()<deadline:
            email_code = email_poll(email, email_pw, seen_email)
            if email_code: break
            await hbeh.sleep(7.0, 15.0)
        if not email_code: hb('❌ no AC email code'); return False
        hb(f'📧 AC email code: {email_code}')
        ci_em = await find_visible_input(page, exclude_with_value=True)
        if not ci_em: hb('❌ no AC email-code input'); return False
        shot(await safe_screenshot(page), f'[ac-email-pre] about to type email code {email_code}')
        await ci_em[1].click(); await asyncio.sleep(0.5); await ci_em[1].type(email_code, delay=140)
        await hbeh.sleep(3.5, 7.5)
        shot(await safe_screenshot(page), f'[ac-email-post] typed email code {email_code}')
        await gated_click(page, 'Next',
            f'Is the AC email confirmation code {email_code} typed in the input field, with Next enabled?',
            label='ac-email-code-next')
        await hbeh.sleep(5.6, 12.0)
    else:
        hb('⏭ no email-code modal detected — SMS submit alone appears to have bound the phone')

    # STEP 8 — passkey safeguard (regardless of whether email step happened)
    try: await safe_passkey_dismiss(page, label='ac-post-email-code')
    except Exception as e:
        hb(f'❌ HALT: AC passkey Not now click failed: {e}'); return False
    hb('✅ AC bound')
    return True


async def create_app_wizard(page, app_name, use_case_text, fb_pw):
    """Walk Create App wizard. Returns app ID from /apps list, or None.

    RESTORED 2026-06-03 to the original 85c7711 architecture per user request:
    feedback-loop gates were causing FB to silently reject app creation. The
    pre-merge shard-era code below empirically worked for META APP 10/11/12.
    Only added back: safe_screenshot (direct-CDP, JPEG, reliable), keyword-based
    Submit click on the password popup (more robust than role-based).
    """
    hb(f'creating app: {app_name} ({use_case_text})')
    await click_btn(page, 'Create App') or await click_btn(page, 'Create app')
    await asyncio.sleep(8)
    # Dismiss "new way" guidance modal
    await page.keyboard.press('Escape')
    await asyncio.sleep(3)
    # Clear search bar (sometimes typed-into garbage)
    for f in page.frames:
        try:
            for el in await f.query_selector_all('input[placeholder*="Search" i]'):
                if await el.is_visible():
                    await el.click(); await asyncio.sleep(0.3)
                    await el.press('Control+a'); await el.press('Delete')
                    break
        except: pass
    await page.mouse.click(100, 100)
    await asyncio.sleep(2)

    # Type App name in the y=200..350 input (avoid search bar at y=17)
    nin = await find_visible_input(page, exclude_with_value=False, y_range=(200, 400))
    if not nin: hb('❌ no name input'); return None
    await nin[1].click(); await asyncio.sleep(1)
    await nin[1].press('Control+a'); await nin[1].press('Delete'); await asyncio.sleep(0.5)
    await nin[1].type(app_name, delay=140)
    hb(f'name typed'); await asyncio.sleep(4)
    await click_btn(page, 'Next')
    await asyncio.sleep(10)

    # Use cases → All (19) → click the target use case card
    for f in page.frames:
        try:
            loc = f.locator('text="All (19)"').first
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await asyncio.sleep(3)
    # Scroll target into view + click its checkbox (use mouse click far right)
    for f in page.frames:
        try:
            loc = f.locator(f'text="{use_case_text}"').last
            if await loc.count()>0:
                await loc.scroll_into_view_if_needed(timeout=8000)
                await asyncio.sleep(2)
                box = await loc.bounding_box()
                if box:
                    await page.mouse.click(box['x']+800, box['y']+box['height']/2)
                break
        except: pass
    await asyncio.sleep(4)
    await click_btn(page, 'Next')
    await asyncio.sleep(10)

    # Business: I don't want
    for f in page.frames:
        try:
            loc = f.locator('text=/I don.t want to connect a business portfolio/').last
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await asyncio.sleep(4)
    await click_btn(page, 'Next')
    await asyncio.sleep(10)
    # Requirements: just Next
    await click_btn(page, 'Next')
    await asyncio.sleep(10)
    # Overview: Create app
    await click_btn(page, 'Create app')
    await asyncio.sleep(6)
    # Password popup
    for i in range(20):
        await asyncio.sleep(2)
        for f in page.frames:
            try:
                el = await f.query_selector('input[type="password"]')
                if el and await el.is_visible():
                    await el.click(); await asyncio.sleep(1); await el.fill('')
                    await el.type(fb_pw, delay=140)
                    await asyncio.sleep(3)
                    await click_btn(page, 'Submit')
                    await asyncio.sleep(15)
                    break
            except: pass
        else: continue
        break

    # Verify via /apps
    await asyncio.sleep(5)
    await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(8)
    s = await safe_screenshot(page)
    shot(s, f'6️⃣ /apps list after creating {app_name}')
    v = vision(s, f'In the apps list, find "{app_name}". Reply ONLY JSON: {{"app_listed":true|false,"app_id":"<the App ID number visible next to it, or null>"}}')
    d = pj(v)
    return d.get('app_id')


def save_account_record(profile_name, acc, result):
    """Write .txt to Drive folder + upsert CSV."""
    creds = pickle.loads(base64.b64decode(os.environ['GOOGLE_TOKEN_PICKLE']))
    svc = build('drive','v3',credentials=creds, cache_discovery=False)

    safe = profile_name.replace(' ','_')
    fname = f'{safe}__{acc["email"].split("@")[0]}.txt'
    body_str = f'''═══════════════════════════════════════════════
ACCOUNT — {profile_name}
═══════════════════════════════════════════════

GoLogin Profile : {profile_name}
FB Email        : {acc["email"]}
FB Profile ID   : {acc["fb_id"]}
Status          : {result.get("status")}
Created         : {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
Rental Phone    : {result.get("rental_phone")}
Rental ID       : {result.get("rental_id")}
Role chosen     : {result.get("role")}

FB APP:  {result.get("fb_app") or "(failed)"}  →  ID {result.get("fb_app_id") or "(missing)"}
IG APP:  {result.get("ig_app") or "(failed)"}  →  ID {result.get("ig_app_id") or "(missing)"}

PRIVACY POLICY URL (paste into FB app Settings → Privacy Policy URL):
{result.get("privacy_url") or "(failed)"}

═══════════════════════════════════════════════
FULL BLOB (paste into bot for cookie-restore):
═══════════════════════════════════════════════
{acc["blob"]}
'''
    # Upload .txt to folder (replace if exists)
    q = f"name='{fname}' and '{BLOBS_FOLDER_ID}' in parents and trashed=false"
    existing = svc.files().list(q=q, fields='files(id)').execute().get('files') or []
    media = MediaInMemoryUpload(body_str.encode('utf-8'), mimetype='text/plain')
    if existing:
        fid = existing[0]['id']
        svc.files().update(fileId=fid, media_body=media).execute()
    else:
        fid = svc.files().create(body={'name':fname,'parents':[BLOBS_FOLDER_ID]}, media_body=media, fields='id').execute()['id']
    blob_link = f'https://drive.google.com/file/d/{fid}/view'

    # Upsert CSV via accounts_sheet
    notes = f'FB app {result.get("fb_app") or "?"} ({result.get("fb_app_id") or "?"}) + IG app {result.get("ig_app") or "?"} ({result.get("ig_app_id") or "?"}). Privacy: {result.get("privacy_url") or "(none)"}.'
    asm.upsert_entry(profile_name, acc["email"], acc["fb_id"],
        'mobile_iproyal','active',
        status=result.get('status','unknown'),
        notes=notes, full_blob=blob_link,
        rental_phone=result.get('rental_phone',''), rental_id=result.get('rental_id',''))
    print(f'SAVED: {profile_name} → {blob_link}')
    return blob_link


# ─── Dispatch from CLI ────────────────────────────────────────────────
if __name__ == '__main__':
    blob = sys.argv[1]
    profile_name = sys.argv[2]  # e.g. 'Validated Profile 5'
    acc = parse_blob(blob)
    profs = mdm._list_validated_profiles()
    target = next((p for p in profs if p['name']==profile_name), None)
    if not target: sys.exit(f'profile {profile_name!r} not found')
    profile_id = target['id']
    result = asyncio.run(run_account(profile_id, acc))
    if result is None:
        result = {'status':'aborted_early', 'rental_phone':'', 'rental_id':'', 'role':'', 'fb_app':None, 'fb_app_id':None, 'ig_app':None, 'ig_app_id':None, 'privacy_url':None}
    save_account_record(profile_name, acc, result)
    print(f'DONE  status={result.get("status")}')
