"""ARCHIVED 2026-06-03 — verbatim recovery from conversation transcript line 17519.

Original /tmp script that ran on 2026-06-01 against VP34 to create the IG-side
app (counterpart to the LaunchLy FB app). Continues from use-case-Next onward —
assumes the IG use-case checkbox is already selected (typically by VP34's
preceding script that clicked "Manage messaging & content on Instagram" via +800
offset, then this picks up at the Next click).

Hardcoded for VP34 / IG app name "team app 40". For reusable version see
force_complete_app_wizard.py.
"""
import asyncio, os, sys, requests, base64, re
sys.path.insert(0, '/app')
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
TOK = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = 1984534885
PID = '6a1ca48c5cdf7b25a1f4f876'
FB_PW = '3um1zng8'
IG_APP_NAME = 'team app 40'


def hb(t):
    print(f'HB: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id': CHAT, 'text': f'⚙️ {t[:300]}'}, timeout=15)
    except: pass


async def shot(ctx, page, label):
    try:
        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(c.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 55}), timeout=15)
        png = base64.b64decode(res['data'])
        requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo': ('s.jpg', png, 'image/jpeg')}, data={'chat_id': CHAT, 'caption': f'[K34-{label}]'}, timeout=30)
    except: pass


async def main():
    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={PID}'
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = br.contexts[0]
        wiz = next((pg for pg in ctx.pages if 'developers.facebook.com' in pg.url), None) or ctx.pages[-1]
        await wiz.bring_to_front()

        hb('━━━ K34: IG app continue from use-case-Next ━━━')
        await wiz.get_by_role('button', name='Next').click(timeout=8000)
        await asyncio.sleep(8)
        await shot(ctx, wiz, '1-after-use-case-next')

        try:
            loc = wiz.locator('text=/I don.t want to connect a business portfolio/').last
            if await loc.count() > 0: await loc.click(timeout=5000); hb('business choice clicked')
        except Exception as e: hb(f'biz err: {e}')
        await asyncio.sleep(3)
        await wiz.get_by_role('button', name='Next').click(timeout=8000); await asyncio.sleep(8)
        await shot(ctx, wiz, '2-after-business')

        try: await wiz.get_by_role('button', name='Next').click(timeout=8000); await asyncio.sleep(8); hb('req next')
        except Exception as e: hb(f'req err: {e}')
        await shot(ctx, wiz, '3-after-req')

        try: await wiz.get_by_role('button', name='Create app').click(timeout=8000); await asyncio.sleep(6); hb('Create app clicked')
        except Exception as e: hb(f'create app err: {e}')
        await shot(ctx, wiz, '4-after-create')

        for i in range(15):
            await asyncio.sleep(3)
            try:
                pwin = await wiz.query_selector('input[type="password"]')
                if pwin and await pwin.is_visible():
                    hb(f'pw popup #{i+1}')
                    await pwin.click(); await asyncio.sleep(1)
                    await pwin.fill(''); await pwin.type(FB_PW, delay=120)
                    await asyncio.sleep(3)
                    await shot(ctx, wiz, '5-pw-filled')
                    try: await wiz.get_by_role('button', name='Submit').click(timeout=8000); hb('Submit clicked')
                    except Exception as e: hb(f'submit err: {e}')
                    await asyncio.sleep(15)
                    break
            except: pass

        await wiz.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(6)
        await shot(ctx, wiz, '6-final-list')
        apps = await wiz.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/apps/"]')).map(a => ({href: a.href, txt: (a.innerText||'').slice(0,100)}))""")
        ig_id = None
        for a in apps:
            if IG_APP_NAME in a['txt']:
                m = re.search(r'/apps/(\d{10,})', a['href'])
                if m: ig_id = m.group(1); break
        hb(f'IG app ID: {ig_id}')


asyncio.run(main())
