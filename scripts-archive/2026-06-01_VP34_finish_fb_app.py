"""ARCHIVED 2026-06-03 — verbatim recovery from conversation transcript line 17408.

Original /tmp script that ran on 2026-06-01 against VP34 and created the
LaunchLy FB app (app id 2107024670234005). Sent via `railway ssh` heredoc
+ base64 + python3 -u nohup.

Hardcoded values are VP34-specific (kept for fidelity). For reusable version
see /Users/alxparaschiv/Desktop/cloack_priv_prox/force_complete_app_wizard.py
which generalizes APP_NAME / USE_CASE_TEXT / PID via env vars.
"""
import asyncio, os, sys, requests, base64
sys.path.insert(0, '/app')
import human_behavior as hbeh
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
TOK = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = 1984534885
PID = '6a1ca48c5cdf7b25a1f4f876'
FB_PW = '3um1zng8'  # from blob position 2


def hb(t):
    print(f'HB: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id': CHAT, 'text': f'⚙️ {t[:300]}'}, timeout=15)
    except: pass


async def shot(ctx, page, label):
    try:
        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(c.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 55}), timeout=15)
        png = base64.b64decode(res['data'])
        requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo': ('s.jpg', png, 'image/jpeg')}, data={'chat_id': CHAT, 'caption': f'[F34-{label}]'}, timeout=30)
    except Exception as e: hb(f'shot err: {e}')


async def main():
    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={PID}'
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = br.contexts[0]
        wiz = next((pg for pg in ctx.pages if 'developers.facebook.com' in pg.url), None) or ctx.pages[-1]
        await wiz.bring_to_front()
        state = {}
        hb('━━━ VP34 finish FB app wizard ━━━')
        await shot(ctx, wiz, '1-start')
        body = await wiz.evaluate("() => document.body.innerText")
        # STEP 1: select "Manage everything on your Page" use case via x+800 offset
        if 'Manage everything on your Page' in body and 'Use cases' in body:
            hb('on Use Cases page → selecting "Manage everything on your Page"')
            try:
                # Click All (19) first to ensure full list
                try: await wiz.locator('text="All (19)"').first.click(timeout=4000); await asyncio.sleep(2)
                except: pass
                loc = wiz.locator('text="Manage everything on your Page"').last
                await loc.scroll_into_view_if_needed(timeout=8000)
                await asyncio.sleep(2)
                box = await loc.bounding_box()
                if box:
                    target_x = box['x'] + 800
                    target_y = box['y'] + box['height']/2
                    hb(f'clicking radio at ({target_x:.0f}, {target_y:.0f})')
                    await hbeh.click(wiz, target_x, target_y, state)
                await asyncio.sleep(3)
                await shot(ctx, wiz, '2-after-radio')
            except Exception as e: hb(f'radio click err: {e}')
            try:
                await wiz.get_by_role('button', name='Next').click(timeout=8000)
                hb('Next clicked → advancing')
                await asyncio.sleep(8)
                await shot(ctx, wiz, '3-after-use-case-next')
            except Exception as e: hb(f'Next err: {e}'); return
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

        # STEP 3: Requirements page → just Next
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

        # STEP 5: Password popup
        hb('waiting for password popup')
        for i in range(15):
            await asyncio.sleep(3)
            try:
                pwin = await wiz.query_selector('input[type="password"]')
                if pwin and await pwin.is_visible():
                    hb(f'password input visible at attempt {i+1} → filling')
                    await pwin.click(); await asyncio.sleep(1)
                    await pwin.fill(''); await pwin.type(FB_PW, delay=120)
                    await asyncio.sleep(3)
                    await shot(ctx, wiz, '8-password-filled')
                    try: await wiz.get_by_role('button', name='Submit').click(timeout=8000); hb('Submit clicked')
                    except Exception as e: hb(f'submit err: {e}')
                    await asyncio.sleep(15)
                    break
            except: pass
        await shot(ctx, wiz, '9-final-state')

        await asyncio.sleep(6)
        await wiz.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(6)
        await shot(ctx, wiz, '10-apps-list')
        body = await wiz.evaluate("() => document.body.innerText")
        if 'LaunchLy' in body: hb('🎉 FB app LaunchLy visible in apps list')
        else: hb(f'⚠️ FB app not visible. body sample: {body[:300]}')

asyncio.run(main())
