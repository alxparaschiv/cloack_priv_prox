#!/usr/bin/env python3
"""Force-finish the Create App wizard — line-for-line port of the F34 script
that created LaunchLy app ID 2107024670234005 on June 1.

Recovered from conversation transcript line 17408. Original ran via
`railway ssh` (on Railway). Same code runs identically here via `railway run`
(which injects env vars but executes locally).

F34's logic:
  - Body-keyword detection for each step ("Use cases" → "Business" → "Requirements" → "Overview")
  - +800px offset from text bounding box for use-case checkbox click
  - get_by_role('button', name='Next').click(timeout=8000) for Next clicks (NOT click_btn)
  - Tolerant of errors (try/except around each step)
  - Password popup loop (15 iter × 3s)
  - Final verification via /apps body keyword check

Usage:
    BLOB=<blob>
    APP_NAME=<name>            (e.g. LaunchLy)
    USE_CASE_TEXT=<exact text> (e.g. "Manage everything on your Page")
    railway run python3 force_complete_app_wizard.py "Validated Profile 1"

Output: APP_ID=<id> on stdout if the app was created.
"""
import asyncio, os, sys, requests, base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import human_behavior as hbeh
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
TOK = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = int(os.environ['TELEGRAM_CHAT_ID'])
BLOB = os.environ['BLOB']
APP_NAME = os.environ['APP_NAME']
USE_CASE_TEXT = os.environ.get('USE_CASE_TEXT', 'Manage everything on your Page')
FB_PW = BLOB.split(':')[1]  # blob position 2 — same as F34


def hb(t):
    print(f'HB: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage',
                       json={'chat_id': CHAT, 'text': f'⚙️ {t[:300]}'}, timeout=15)
    except: pass


async def shot(ctx, page, label):
    """Direct CDP screenshot — matches F34's shot() exactly."""
    try:
        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(
            c.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 55}),
            timeout=15)
        png = base64.b64decode(res['data'])
        requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto',
                      files={'photo': ('s.jpg', png, 'image/jpeg')},
                      data={'chat_id': CHAT, 'caption': f'[F-{label}]'}, timeout=30)
    except Exception as e: hb(f'shot err: {e}')


async def main(profile_name):
    import meta_dev as mdm
    profs = mdm._list_validated_profiles()
    target = next((p for p in profs if p['name'] == profile_name), None)
    if not target: sys.exit(f'profile {profile_name!r} not found')
    PID = target['id']
    hb(f'━━━ F34-style finish for "{APP_NAME}" ({USE_CASE_TEXT}) on {PID[:12]}… ━━━')

    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={PID}'
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = br.contexts[0]
        wiz = next((pg for pg in ctx.pages if 'developers.facebook.com' in pg.url), None) or ctx.pages[-1]
        await wiz.bring_to_front()
        state = {}
        await shot(ctx, wiz, '1-start')
        body = await wiz.evaluate("() => document.body.innerText")

        # PRE-STEP A: If we're at /apps (My Apps page), click Create App
        if 'No apps yet' in body or ('My Apps' in body and 'Create App' in body and 'Create an app' not in body):
            hb('on My Apps page → clicking Create App')
            try:
                await wiz.get_by_role('button', name='Create App').click(timeout=8000)
            except Exception as e:
                try:
                    await wiz.locator('text="Create App"').first.click(timeout=8000)
                except Exception as e2: hb(f'Create App click err: {e2}')
            await asyncio.sleep(8)
            # Dismiss "new way" modal if present
            try: await wiz.keyboard.press('Escape')
            except: pass
            await asyncio.sleep(3)
            try: await wiz.get_by_role('button', name='Close').first.click(timeout=3000)
            except: pass
            await asyncio.sleep(2)
            await shot(ctx, wiz, '1a-after-create-app-modal')
            body = await wiz.evaluate("() => document.body.innerText")

        # PRE-STEP B: If we're on the App Name form, type name + Next
        if 'App name' in body and 'Use cases' not in body and APP_NAME not in body:
            hb(f'on App name form → typing "{APP_NAME}"')
            try:
                # Find the visible app name input (NOT the search bar)
                target = None
                for f in wiz.frames:
                    for inp in await f.query_selector_all('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])'):
                        try:
                            if not await inp.is_visible(): continue
                            box = await inp.bounding_box()
                            if not box or box['y'] < 200 or box['y'] > 500: continue
                            val = await inp.input_value()
                            if val: continue
                            target = inp
                            break
                        except: pass
                    if target: break
                if target:
                    await target.click(); await asyncio.sleep(1)
                    await target.press('Control+a'); await target.press('Delete'); await asyncio.sleep(0.5)
                    await target.type(APP_NAME, delay=140)
                    hb(f'name typed')
                    await asyncio.sleep(3)
                    await shot(ctx, wiz, '1b-name-typed')
                else:
                    hb('⚠️ could not find app name input')
            except Exception as e: hb(f'name type err: {e}')
            try:
                await wiz.get_by_role('button', name='Next').click(timeout=8000)
                hb('app-name Next clicked')
            except Exception as e: hb(f'app-name Next err: {e}')
            await asyncio.sleep(8)
            await shot(ctx, wiz, '1c-after-app-name-next')
            body = await wiz.evaluate("() => document.body.innerText")

        # STEP 1: select use case via x+800 offset (F34's exact approach)
        if USE_CASE_TEXT in body and 'Use cases' in body:
            hb(f'on Use Cases page → selecting "{USE_CASE_TEXT}"')
            try:
                # Click All (19) first to ensure full list
                try:
                    await wiz.locator('text="All (19)"').first.click(timeout=4000)
                    await asyncio.sleep(2)
                except: pass
                loc = wiz.locator(f'text="{USE_CASE_TEXT}"').last
                await loc.scroll_into_view_if_needed(timeout=8000)
                await asyncio.sleep(2)
                box = await loc.bounding_box()
                if box:
                    target_x = box['x'] + 800
                    target_y = box['y'] + box['height'] / 2
                    hb(f'clicking radio at ({target_x:.0f}, {target_y:.0f}) [F34 +800 offset]')
                    await hbeh.click(wiz, target_x, target_y, state)
                await asyncio.sleep(3)
                await shot(ctx, wiz, '2-after-radio')
            except Exception as e: hb(f'radio click err: {e}')
            # Click Next (F34's get_by_role pattern)
            try:
                await wiz.get_by_role('button', name='Next').click(timeout=8000)
                hb('Next clicked → advancing')
                await asyncio.sleep(8)
                await shot(ctx, wiz, '3-after-use-case-next')
            except Exception as e:
                hb(f'Next err: {e}')
                await shot(ctx, wiz, '3a-NEXT-FAILED-likely-no-use-case-selected')
                return
            body = await wiz.evaluate("() => document.body.innerText")

        # STEP 2: Business page — "I don't want to connect"
        if 'Business' in body and ('business portfolio' in body.lower() or "don't want to connect" in body.lower()):
            hb('on Business page → selecting "I dont want"')
            try:
                loc = wiz.locator('text=/I don.t want to connect a business portfolio/').last
                if await loc.count() > 0: await loc.click(timeout=5000)
                else:
                    await wiz.get_by_role('radio', name=lambda n: 'don' in n.lower() and 'want' in n.lower()).click(timeout=5000)
            except Exception as e: hb(f'business choice err: {e}')
            await asyncio.sleep(3)
            await shot(ctx, wiz, '4-after-business-choice')
            try:
                await wiz.get_by_role('button', name='Next').click(timeout=8000)
                hb('Business Next clicked'); await asyncio.sleep(8)
            except Exception as e: hb(f'business next err: {e}'); return
            await shot(ctx, wiz, '5-after-business-next')
            body = await wiz.evaluate("() => document.body.innerText")

        # STEP 3: Requirements → Next
        if 'Requirements' in body and 'Next' in body:
            hb('on Requirements page → Next')
            try:
                await wiz.get_by_role('button', name='Next').click(timeout=8000)
                await asyncio.sleep(8)
            except Exception as e: hb(f'req next err: {e}'); return
            await shot(ctx, wiz, '6-after-requirements')
            body = await wiz.evaluate("() => document.body.innerText")

        # STEP 4: Overview → Create app
        if 'Overview' in body or 'Create app' in body:
            hb('on Overview → Create app')
            try:
                await wiz.get_by_role('button', name='Create app').click(timeout=8000)
                await asyncio.sleep(6)
            except Exception as e: hb(f'create app err: {e}'); return
            await shot(ctx, wiz, '7-after-create-app')

        # STEP 5: Password popup — F34 uses 15 iter × 3s = 45s
        hb('waiting for password popup')
        for i in range(15):
            await asyncio.sleep(3)
            try:
                pwin = await wiz.query_selector('input[type="password"]')
                if pwin and await pwin.is_visible():
                    hb(f'password input visible at attempt {i+1} → filling')
                    await pwin.click(); await asyncio.sleep(1)
                    await pwin.fill('')
                    await pwin.type(FB_PW, delay=120)
                    await asyncio.sleep(3)
                    await shot(ctx, wiz, '8-password-filled')
                    try: await wiz.get_by_role('button', name='Submit').click(timeout=8000); hb('Submit clicked')
                    except Exception as e: hb(f'submit err: {e}')
                    await asyncio.sleep(15)
                    break
            except: pass
        await shot(ctx, wiz, '9-final-state')

        # STEP 6: Verify via /apps
        await asyncio.sleep(6)
        await wiz.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(6)
        await shot(ctx, wiz, '10-apps-list')
        body = await wiz.evaluate("() => document.body.innerText")
        if APP_NAME in body:
            hb(f'🎉 FB app {APP_NAME} visible in apps list')
            # Extract App ID
            apps = await wiz.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/apps/"]')).map(a => ({href: a.href, txt: (a.innerText||'').slice(0,100)}))""")
            import re
            app_id = None
            for a in apps:
                if APP_NAME in a['txt']:
                    m = re.search(r'/apps/(\d{10,})', a['href'])
                    if m: app_id = m.group(1); break
            hb(f'app_id: {app_id}')
            print(f'\nAPP_ID={app_id}')
        else:
            hb(f'⚠️ {APP_NAME} not visible. body sample: {body[:300]}')
            print(f'\nAPP_ID=None')


if __name__ == '__main__':
    profile_name = sys.argv[1]
    asyncio.run(main(profile_name))
