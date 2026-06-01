"""SHARD 2 — Customize add perms + Graph Explorer typeahead + Generate Token + extend.

Per project-shard2-page-token-runbook.md.

Usage:
  APP_ID=<id> APP_SECRET=<32hex> FB_PW=<pw> python3 shard2_perms_token.py <profile_id>

For META APP 12:
  APP_ID=2107024670234005
  APP_SECRET=e280583110d5191f11333fe8b414fdcb
  Profile: 6a1ca48c5cdf7b25a1f4f876
"""
import asyncio, os, sys, base64, requests, random, json, re
sys.path.insert(0, '/app')
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
TOK = os.environ['TELEGRAM_BOT_TOKEN']
OAI = os.environ['OPENAI_API_KEY']
CHAT = 1984534885

APP_ID = os.environ['APP_ID']
APP_SECRET = os.environ['APP_SECRET']
FB_PW = os.environ.get('FB_PW', '')
PID = sys.argv[1]

# Customize: 4 advanced perms to Add (the ones not auto-granted)
CUSTOMIZE_PERMS = ['pages_manage_posts', 'pages_read_engagement', 'pages_manage_metadata', 'read_insights']

# Graph Explorer: 5 perms to type via typeahead
EXPLORER_PERMS = ['pages_show_list', 'pages_manage_posts', 'pages_read_engagement', 'pages_manage_metadata', 'read_insights']

def hb(t):
    print(f'S2: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id':CHAT,'text':f'⚙️ S2: {t[:300]}'}, timeout=15)
    except: pass

async def shot(ctx, page, label):
    try:
        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(c.send('Page.captureScreenshot', {'format':'jpeg','quality':55}), timeout=15)
        png = base64.b64decode(res['data'])
        requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo':('s.jpg',png,'image/jpeg')}, data={'chat_id':CHAT,'caption':f'[S2-{label}]'}, timeout=30)
    except Exception as e: hb(f'shot err: {e}')

async def j(min_s, max_s=None):
    """Jittered sleep per feedback-jitter-and-uniqueness."""
    if max_s is None: max_s = min_s * 1.4
    await asyncio.sleep(random.uniform(min_s, max_s))

async def main():
    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={PID}'
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = next((p for p in ctx.pages if 'developers.facebook.com' in p.url), None) or ctx.pages[0]
        await page.bring_to_front()
        hb(f'━━━ Shard 2: perms + token for app {APP_ID} ━━━')

        # =============================================================
        # STEP A: Customize page — Add the 4 advanced perms
        # =============================================================
        url = f'https://developers.facebook.com/apps/{APP_ID}/use_cases/customize/?use_case_enum=PAGES_API'
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        await j(10, 16)
        await shot(ctx, page, '1-customize-page')

        for perm in CUSTOMIZE_PERMS:
            hb(f'Customize: looking for "{perm}"')
            try:
                loc = page.get_by_text(perm, exact=True).first
                await loc.scroll_into_view_if_needed(timeout=8000)
                await j(1.5, 2.5)
            except Exception as e: hb(f'  scroll-into-view fail: {e}'); continue
            # Find Add button in same row
            try:
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
            except Exception as e: hb(f'  add err: {e}')
            await j(3, 5)
        await shot(ctx, page, '2-after-add-perms')

        # =============================================================
        # STEP B/C: Cool-down — 10 min between Customize and Explorer
        # =============================================================
        hb('⏳ 10-min cool-down before Graph Explorer (anti-flag)')
        await asyncio.sleep(600)

        # =============================================================
        # STEP D: Graph API Explorer — type each perm via typeahead
        # =============================================================
        # Open in new tab? No — per 4-tab rule, reuse existing tab.
        # Actually Explorer needs to be its own tab per the runbook. Use existing dev portal tab.
        await page.goto('https://developers.facebook.com/tools/explorer/', wait_until='domcontentloaded', timeout=60000)
        await j(10, 16)
        await shot(ctx, page, '3-explorer-fresh')

        # Verify Meta App selector points to our app
        body = await page.evaluate("() => document.body.innerText.substring(0, 1500)")
        hb(f'body[:300]: {body[:300]!r}')

        # Find combobox — placeholder "Add a Permission"
        combo = page.locator('input[placeholder="Add a Permission"]').first
        for perm in EXPLORER_PERMS:
            try:
                await combo.click()
                await j(0.5, 1)
                await page.keyboard.press('Control+A'); await j(0.3, 0.6)
                await page.keyboard.press('Delete'); await j(0.5, 1)
                await page.keyboard.type(perm, delay=random.randint(60, 100))
                await j(2, 3)
                await page.keyboard.press('ArrowDown'); await j(0.4, 0.8)
                await page.keyboard.press('Enter')
                hb(f'  typed + Enter: {perm}')
                await j(2, 3)
            except Exception as e:
                hb(f'  ❌ perm {perm}: {e}')
        await shot(ctx, page, '4-perms-typed')

        # Click Generate Access Token (or Get Token → Generate Access Token)
        # Capture short user token via framenavigated event
        captured_token = {'val': None}
        async def on_frame_navigated(frame):
            try:
                url = frame.url
                if 'access_token=' in url:
                    m = re.search(r'access_token=([^&]+)', url)
                    if m and not captured_token['val']:
                        captured_token['val'] = m.group(1)
                        hb(f'📌 short token captured from URL: {captured_token["val"][:30]}...')
            except: pass

        # Hook the listener on the main context so popup navigations are caught
        ctx.on('page', lambda new_page: new_page.on('framenavigated', on_frame_navigated))
        page.on('framenavigated', on_frame_navigated)

        # Click Generate Access Token
        try:
            gen = page.get_by_role('button', name='Generate Access Token').first
            await gen.scroll_into_view_if_needed(timeout=5000)
            await j(1, 2)
            await gen.click(timeout=8000)
            hb('Generate Access Token clicked')
        except Exception as e:
            hb(f'❌ Generate click fail: {e}'); return
        await j(6, 10)
        await shot(ctx, page, '5-after-generate')

        # Walk OAuth popups: Continue as → opt-in radio + Continue → Save → Got it
        for label in ['Continue as', 'Opt in to all current and future Pages', 'Continue', 'Save', 'Got it']:
            # Look across all pages (popups)
            handled = False
            for attempt in range(8):
                for p in list(ctx.pages):
                    try:
                        body = await p.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText.substring(0,500) : ''")
                    except: continue
                    if not body: continue
                    if label == 'Opt in to all current and future Pages' and label in body:
                        # Click the radio
                        try:
                            await p.get_by_role('radio', name=label).click(timeout=4000)
                            hb(f'  radio clicked: {label}'); handled = True; break
                        except:
                            try:
                                await p.locator(f'text="{label}"').first.click(timeout=4000)
                                hb(f'  radio (text) clicked: {label}'); handled = True; break
                            except: pass
                    else:
                        # Match button starting with label
                        if any(label in body for _ in [0]):
                            try:
                                btn = p.get_by_role('button', name=lambda n: label.lower() in (n or '').lower()).first
                                await btn.click(timeout=4000)
                                hb(f'  button clicked: {label}'); handled = True; break
                            except:
                                try:
                                    await p.locator(f'text="{label}"').first.click(timeout=4000)
                                    hb(f'  text clicked: {label}'); handled = True; break
                                except Exception as e: pass
                if handled: break
                await j(2, 3)
            if not handled:
                hb(f'  ⚠️ {label} not handled (might not appear on this account)')
            await j(2, 4)

        await j(8, 14)
        await shot(ctx, page, '6-post-oauth')

        # Token should be in captured_token now, or in main URL
        short_token = captured_token['val']
        if not short_token:
            # Try to read from URL fragment of main page
            url = page.url
            m = re.search(r'access_token=([^&]+)', url)
            if m: short_token = m.group(1); hb(f'short token from main URL: {short_token[:30]}...')
        # Also try reading from Access Token input field
        if not short_token:
            try:
                val = await page.evaluate("""() => {
                    for (const inp of document.querySelectorAll('input')) {
                        const v = (inp.value || '').trim();
                        if (v.startsWith('EAA') && v.length > 100) return v;
                    }
                    return null;
                }""")
                if val: short_token = val; hb(f'short token from input field: {short_token[:30]}...')
            except: pass
        if not short_token:
            hb('❌ short user token NOT captured'); return
        hb(f'✅ short user token: {short_token[:30]}... ({len(short_token)} chars)')

        # =============================================================
        # STEP F: Extend programmatically
        # =============================================================
        r = requests.get('https://graph.facebook.com/v25.0/oauth/access_token', params={
            'grant_type': 'fb_exchange_token',
            'client_id': APP_ID,
            'client_secret': APP_SECRET,
            'fb_exchange_token': short_token,
        }, timeout=30)
        try:
            data = r.json()
        except Exception as e: hb(f'extend response parse err: {e}; raw={r.text[:300]}'); return
        long_token = data.get('access_token')
        if not long_token:
            hb(f'❌ extend failed: {json.dumps(data)[:300]}'); return
        hb(f'✅ long user token: {long_token[:30]}... ({len(long_token)} chars, expires_in={data.get("expires_in")})')

        # =============================================================
        # STEP G: Debug — verify scopes
        # =============================================================
        r2 = requests.get('https://graph.facebook.com/v25.0/debug_token', params={
            'input_token': long_token,
            'access_token': f'{APP_ID}|{APP_SECRET}',
        }, timeout=30)
        debug = r2.json().get('data', {})
        scopes = debug.get('scopes', [])
        hb(f'token scopes ({len(scopes)}): {scopes}')

        out = {
            'app_id': APP_ID,
            'app_secret': APP_SECRET,
            'short_user_token': short_token,
            'long_user_token': long_token,
            'long_token_expires_in_seconds': data.get('expires_in'),
            'scopes': scopes,
            'debug_token': debug,
        }
        open('/tmp/shard2_result.json','w').write(json.dumps(out, indent=2))
        hb(f'🎉 SHARD 2 COMPLETE — result saved to /tmp/shard2_result.json')
        print(f'\nLONG_USER_TOKEN={long_token}\nSCOPES={scopes}')

asyncio.run(main())
