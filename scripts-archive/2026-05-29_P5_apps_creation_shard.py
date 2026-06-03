"""ARCHIVED — verbatim recovery of /tmp/p5_apps.py from conversation transcript line 6733.

THE original app-creation shard from late May / early June. Created both FB
and IG apps for Profile 5 (PID 6a1837178cdef3f2a8bbb1c2) — Sepurcia Dignus
account → META APP 5. Ran via `railway ssh` ON Railway container (not
local Mac via `railway run`).

This is the script architecture the user wants restored for Phase H — the
app-creation portion that worked across META APPS 5/6/7/8/9 before the
ed299d8 merge.

Key features:
  - Self-contained (no imports from master_account_create.py)
  - Inline click_btn (get_by_role + is_enabled check)
  - Inline find_input_in_y (avoids search bar via y-range filter)
  - +800 offset for use-case checkbox click (note: page.mouse.click — direct, no hbeh)
  - 20-iter password popup loop
  - Telegram screenshots at each milestone
  - Sequential FB app → IG app creation in one process
"""
import os, sys, time, asyncio, requests, random, pickle, base64
sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

H_GL = {'Authorization': f'Bearer {os.environ["GOLOGIN_API_KEY"]}', 'Content-Type': 'application/json'}
PID = '6a1837178cdef3f2a8bbb1c2'
TOK = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = 1984534885
FB_PW = 'pcsryl4u'

ADJ = ['Tester', 'Test', 'Demo', 'Sample', 'My', 'First', 'New', 'Trial', 'Quick', 'Simple']
NOUN = ['App', 'Project', 'Build']
FB_APP_NAME = f'{random.choice(ADJ)} {random.choice(NOUN)} {random.randint(10,99)}'
IG_APP_NAME = f'{random.choice(ADJ)} {random.choice(NOUN)} {random.randint(10,99)}'
while IG_APP_NAME == FB_APP_NAME:
    IG_APP_NAME = f'{random.choice(ADJ)} {random.choice(NOUN)} {random.randint(10,99)}'

print(f'FB app: {FB_APP_NAME}')
print(f'IG app: {IG_APP_NAME}')


def tg_msg(t):
    print(f'TG: {t}')
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage',
                       json={'chat_id': CHAT, 'text': f'🚀 P5: {t[:300]}'}, timeout=15)
    except: pass


def tg_shot(b, c):
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto',
                       files={'photo': ('s.png', b, 'image/png')},
                       data={'chat_id': CHAT, 'caption': f'P5: {c[:1024]}'}, timeout=30)
    except: pass


cdp = f'wss://cloudbrowser.gologin.com/connect?token={os.environ["GOLOGIN_API_KEY"]}&profile={PID}'


async def find_input_in_y(page, y_min, y_max, skip_with_value=False):
    for f in page.frames:
        for el in await f.query_selector_all('input'):
            if not await el.is_visible(): continue
            typ = await el.get_attribute('type') or 'text'
            if typ in ('hidden', 'checkbox', 'radio', 'submit', 'file', 'password', 'email'): continue
            ph = (await el.get_attribute('placeholder') or '').lower()
            if 'search' in ph: continue
            val = await el.input_value()
            if skip_with_value and val: continue
            box = await el.bounding_box()
            if box and y_min <= box['y'] <= y_max:
                return (f, el)
    return None


async def click_btn(page, name):
    for f in page.frames:
        try:
            loc = f.get_by_role('button', name=name, exact=False)
            if await loc.count() > 0 and await loc.first.is_enabled():
                await loc.first.click(); return True
        except: pass
    return False


async def create_app(page, app_name, use_case):
    tg_msg(f'create_app START name={app_name!r} usecase={use_case!r}')
    # 1) On /apps/, click Create App
    await page.goto('https://developers.facebook.com/apps/', wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(8)
    tg_shot(await page.screenshot(type='png'), f'1️⃣ on /apps before Create App ({app_name})')
    ok = await click_btn(page, 'Create App') or await click_btn(page, 'Create app')
    tg_msg(f'Create App click: {ok}')
    await asyncio.sleep(8)
    # Dismiss "new way" modal
    await page.keyboard.press('Escape')
    await asyncio.sleep(2)
    # Clear search bar
    for f in page.frames:
        try:
            for el in await f.query_selector_all('input[placeholder*="Search" i]'):
                if await el.is_visible():
                    await el.click(); await asyncio.sleep(0.3)
                    await el.press('Control+a'); await el.press('Delete')
                    break
        except: pass
    await page.mouse.click(100, 100); await asyncio.sleep(2)
    # 2) Type App name
    nin = await find_input_in_y(page, 200, 400, skip_with_value=False)
    if not nin: tg_msg('❌ no name input'); return False
    await nin[1].click(); await asyncio.sleep(1)
    await nin[1].press('Control+a'); await nin[1].press('Delete'); await asyncio.sleep(0.5)
    await nin[1].type(app_name, delay=140); await asyncio.sleep(4)
    tg_shot(await page.screenshot(type='png'), f'2️⃣ name typed: {app_name}')
    await click_btn(page, 'Next'); await asyncio.sleep(10)
    # 3) Use cases — click All (19) then target via +800 offset
    for f in page.frames:
        try:
            loc = f.locator('text="All (19)"').first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await asyncio.sleep(3)
    for f in page.frames:
        try:
            loc = f.locator(f'text="{use_case}"').last
            if await loc.count() > 0:
                await loc.scroll_into_view_if_needed(timeout=8000)
                await asyncio.sleep(2)
                box = await loc.bounding_box()
                if box: await page.mouse.click(box['x']+800, box['y']+box['height']/2)
                break
        except: pass
    await asyncio.sleep(4)
    tg_shot(await page.screenshot(type='png'), f'3️⃣ Use case selected ({use_case})')
    await click_btn(page, 'Next'); await asyncio.sleep(10)
    # 4) Business: "I don't want"
    for f in page.frames:
        try:
            loc = f.locator('text=/I don.t want to connect a business portfolio/').last
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await asyncio.sleep(4)
    tg_shot(await page.screenshot(type='png'), f'4️⃣ Business: I don\'t want')
    await click_btn(page, 'Next'); await asyncio.sleep(10)
    # 5) Requirements: Next
    await click_btn(page, 'Next'); await asyncio.sleep(10)
    # 6) Overview: Create app
    await click_btn(page, 'Create app'); await asyncio.sleep(5)
    tg_shot(await page.screenshot(type='png'), f'5️⃣ Create app clicked — password popup pending')
    # 7) Password popup
    for attempt in range(20):
        await asyncio.sleep(2)
        found = False
        for f in page.frames:
            try:
                el = await f.query_selector('input[type="password"]')
                if el and await el.is_visible():
                    await el.click(); await asyncio.sleep(1); await el.fill('')
                    await el.type(FB_PW, delay=140)
                    await asyncio.sleep(3)
                    await click_btn(page, 'Submit')
                    await asyncio.sleep(15)
                    found = True; break
            except: pass
        if found: break
    tg_shot(await page.screenshot(type='png'), f'6️⃣ Password submitted')
    # 8) Verify
    await asyncio.sleep(5)
    await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(8)
    tg_shot(await page.screenshot(type='png'), f'7️⃣ /apps list after creating {app_name}')
    return True


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await create_app(page, FB_APP_NAME, 'Manage everything on your Page')
        tg_msg(f'✅ FB app done: {FB_APP_NAME}')
        await create_app(page, IG_APP_NAME, 'Manage messaging & content on Instagram')
        tg_msg(f'✅ IG app done: {IG_APP_NAME}')


asyncio.run(main())
print('DONE — apps creation finished')
