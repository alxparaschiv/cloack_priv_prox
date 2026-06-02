"""Human-behavior helpers — mouse movement noise, typing cadence variance, sleep distribution.

Use in place of raw page.mouse.click / asyncio.sleep / locator.fill in any
flow that touches Meta/Facebook surfaces. Matches the patterns documented in
project-self-critique-and-warmup-hypotheses.md (Multilogin/Undetectable/BHW
consensus): real users produce noisy, non-linear mouse paths + irregular pauses.

Each public helper tracks the last cursor position in a context dict so
successive clicks form a continuous path, not teleports.
"""
import asyncio
import random
import math
import time


def _curve_points(x1, y1, x2, y2, steps=3, jitter=40):
    pts = []
    for i in range(1, steps + 1):
        t = i / (steps + 1)
        bx = x1 + (x2 - x1) * t
        by = y1 + (y2 - y1) * t
        jx = random.randint(-jitter, jitter)
        jy = random.randint(-jitter, jitter)
        pts.append((bx + jx, by + jy))
    return pts


async def move(page, x, y, state=None):
    cur_x, cur_y = (state or {}).get('pos', (random.randint(200, 1500), random.randint(200, 700)))
    dx, dy = x - cur_x, y - cur_y
    dist = math.hypot(dx, dy)
    n_inter = max(1, min(4, int(dist // 250)))
    jitter = max(8, min(60, int(dist * 0.08)))
    for mx, my in _curve_points(cur_x, cur_y, x, y, steps=n_inter, jitter=jitter):
        await page.mouse.move(mx, my, steps=random.choice([6, 10, 14, 18]))
        await asyncio.sleep(random.uniform(0.03, 0.14))
    fx = x + random.randint(-3, 3)
    fy = y + random.randint(-3, 3)
    await page.mouse.move(fx, fy, steps=random.choice([5, 8, 12]))
    await asyncio.sleep(random.uniform(0.08, 0.32))
    if state is not None:
        state['pos'] = (fx, fy)
    return fx, fy


async def click(page, x, y, state=None):
    await move(page, x, y, state)
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.045, 0.18))
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.1, 0.5))


async def sleep(min_s, max_s=None):
    if max_s is None:
        max_s = min_s * 1.5
        min_s = min_s * 0.6
    if random.random() < 0.18:
        delay = random.uniform(max_s, max_s * 2.4)
    else:
        delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


async def type_text(locator, text, page=None):
    await locator.click()
    await asyncio.sleep(random.uniform(0.2, 0.7))
    for i, ch in enumerate(text):
        await locator.press_sequentially(ch, delay=random.randint(60, 240))
        if random.random() < 0.05 and page:
            await asyncio.sleep(random.uniform(0.4, 1.6))
        if random.random() < 0.012 and ch.isalpha() and page:
            wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
            await page.keyboard.press(wrong if random.random()<0.5 else "Backspace")
            await asyncio.sleep(random.uniform(0.15, 0.55))
            if random.random() < 0.5:
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.4))


async def fb_warmup_browse(page, ctx, seconds=120, state=None):
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return
    await sleep(3, 7)
    deadline = time.time() + seconds
    while time.time() < deadline:
        action = random.choices(
            ["scroll_down", "scroll_down", "pause", "hover", "scroll_up"],
            weights=[5, 5, 3, 2, 1],
        )[0]
        try:
            if action == "scroll_down":
                await page.mouse.wheel(0, random.randint(200, 900))
                await sleep(0.6, 3.2)
            elif action == "scroll_up":
                await page.mouse.wheel(0, -random.randint(100, 400))
                await sleep(0.4, 2.0)
            elif action == "pause":
                await sleep(2.5, 7.5)
            elif action == "hover":
                x = random.randint(200, 1300)
                y = random.randint(200, 700)
                await move(page, x, y, state)
                await sleep(0.5, 2.2)
        except Exception:
            await sleep(1, 2)


APP_NAME_PATTERNS = [
    # Pattern A: Concept + brandable suffix → LaunchLy, PulseHub, ForgeWave
    # Per user 2026-06-03: real-product-sounding names, NO numbers, NO "test app".
    lambda: random.choice(['Launch','Pulse','Forge','Beacon','Spark','Atlas','Quill','Nimbus','Drift',
                            'Echo','Lumen','Pivot','Helper','Compass','Anchor','Orbit','Crest','Glide',
                            'Bloom','Notes','Pages','Insights','Posts','Tide','Mosaic','Cinder'])
              + random.choice(['Ly','Hub','Wave','Lab','HQ','Pad','Bay','Loop','Spot','Den','ify','er','io']),

    # Pattern B: Adjective + Noun, CamelCase, no spaces → BlueRidge, OpenSky, SharpPixel
    lambda: random.choice(['Blue','Sharp','Open','Bright','Quiet','Bold','Pure','Wild','Sunny','Calm',
                            'Swift','Tiny','Quick','Soft','True','Even','Steady','Clear','Quiet','Lucky'])
              + random.choice(['Ridge','Sky','Pixel','Cloud','Wave','Forest','Stack','Pulse','Brook',
                                'Path','Stone','Light','Echo','Reef','Field','Meadow','Bay','Harbor','Trail']),

    # Pattern C: Single invented brandable word
    lambda: random.choice(['Forge','Spark','Atlas','Beacon','Nimbus','Pivot','Drift','Echo','Quill','Lumen',
                            'Crest','Glide','Bloom','Anchor','Compass','Orbit','Halo','Ember','Solace','Vista',
                            'Cinder','Plover','Larkly','Tessa','Mosaic','Wren','Saga','Veld','Plum','Skylark']),

    # Pattern D: Concept + Studio/Co/Works (light corporate feel, no numbers)
    lambda: random.choice(['Beacon','Forge','Quill','Atlas','Pivot','Crest','Lumen','Anchor','Tide','Echo'])
              + ' ' + random.choice(['Studio','Co','Works','Labs','Bureau','Bench','House']),
]


def random_app_name():
    return random.choice(APP_NAME_PATTERNS)()


PRIVACY_HOSTS = ["telegra.ph", "telegra.ph", "telegra.ph"]


def pick_privacy_host():
    return random.choice(PRIVACY_HOSTS)
