"""Randomness helpers for Meta Dev account creation wizard scripts.

Source of truth: [[feedback-jitter-and-uniqueness]] memory.
Replaces fixed `asyncio.sleep()` + `delay=140` calls with jittered versions
so each account session has a unique timing fingerprint.

USAGE in pipeline scripts:
    from jitter import jitter_sleep, type_delay, pre_step_pause, human_click_near

    await jitter_sleep(8)                    # 8s ± jitter (5.6 to 11.2s)
    await el.type(code, delay=type_delay())  # 90-250ms per char, random
    await pre_step_pause()                   # 3-8s "read the page" pause
    await human_click_near(page, x, y)       # offset + pause + click
"""
import asyncio as _asyncio
import random as _random


async def jitter_sleep(base_seconds: float, low_mult: float = 0.7, high_mult: float = 1.4):
    """Replace `await asyncio.sleep(X)` → `await jitter_sleep(X)`.
    Sleeps for base * uniform(low_mult, high_mult)."""
    await _asyncio.sleep(_random.uniform(base_seconds * low_mult, base_seconds * high_mult))


def type_delay() -> int:
    """Jittered per-keystroke delay for `await el.type(s, delay=type_delay())`.
    Returns ms in 90-250 range. Different distribution than fixed 140."""
    return _random.randint(90, 250)


async def pre_step_pause():
    """A 'real user reads the new screen' pause: 3-8 seconds."""
    await _asyncio.sleep(_random.uniform(3.0, 8.0))


async def micro_pause():
    """Small pause between micro-actions like field-clear and field-type. 0.3-1.2s."""
    await _asyncio.sleep(_random.uniform(0.3, 1.2))


async def human_click_near(page, x: float, y: float, *,
                           offset_radius: int = 4, pre_pause: bool = True):
    """Click at (x, y) with a small random pixel offset + optional pre-click pause.
    Simulates hand approach: bots teleport, humans drift."""
    if pre_pause:
        await _asyncio.sleep(_random.uniform(0.5, 2.0))
    ox = _random.randint(-offset_radius, offset_radius)
    oy = _random.randint(-offset_radius, offset_radius)
    await page.mouse.move(x + ox, y + oy)
    await _asyncio.sleep(_random.uniform(0.1, 0.3))
    await page.mouse.click(x + ox, y + oy)


async def human_type(element, text: str):
    """Type each char with variable per-char delay. More variation than a single
    `delay=N`."""
    for ch in text:
        await element.type(ch, delay=_random.randint(70, 280))
        if _random.random() < 0.04:  # 4% chance of a brief mid-word pause
            await _asyncio.sleep(_random.uniform(0.2, 0.7))


# ─── Role + name randomization ──────────────────────────────────────────
ROLE_POOL = ['Developer', 'Marketer', 'Analyst', 'Product manager']
ADJ_POOL_BASE = ['Tester','Test','Demo','Sample','My','First','New','Trial','Quick','Simple']
ADJ_POOL_EXTRA = ['Smart','Easy','Fresh','Bright','Cool','Clean','Solid','Lite','Pro','Basic']
NOUN_POOL = ['App','Project','Build','Tool','Studio','Lab','Kit','Box']

def pick_role() -> str:
    """Randomly pick a role for About You step. Don't always Developer."""
    return _random.choice(ROLE_POOL)

def random_app_name() -> str:
    adj = _random.choice(ADJ_POOL_BASE + ADJ_POOL_EXTRA)
    noun = _random.choice(NOUN_POOL)
    return f'{adj} {noun} {_random.randint(10, 99)}'

def maybe_skip(probability: float = 0.5) -> bool:
    """Roll the dice for optional actions. e.g. should we tick the marketing-email box?"""
    return _random.random() < probability
