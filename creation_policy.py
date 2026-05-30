"""Hard caps + warm-up gates for Meta Dev account creation.

Source of truth: [[feedback-creation-rate-framework]] memory. Updated 2026-05-30
per user direct guidance: push daily to 5, keep min-gap + 4h-window strict, 
shorten warm-up to 2 days before page connect (user judgment: 1-2d is enough).
"""
import datetime as _dt
import random as _random
import os as _os

# ─── HARD CAPS (user-adjusted 2026-05-30) ──────────────────────────────
DAILY_RECOMMENDED = 5    # was 3 — user wants to push
DAILY_HARD_CAP = 5       # absolute ceiling
WEEKLY_RECOMMENDED = 25  # was 15
WEEKLY_HARD_CAP = 25     # 5/day × 5 days; weekend pause optional
MONTHLY_RECOMMENDED = 60
MONTHLY_HARD_CAP = 75

# ─── 4-HOUR WINDOW ──────────────────────────────────────────────────────
WINDOW_4H_HARD_CAP = 3   # ENFORCED — today violated this, won't again

# ─── MIN GAPS ───────────────────────────────────────────────────────────
MIN_GAP_BETWEEN_CREATIONS_SECONDS = 15 * 60   # hard min
RECOMMENDED_GAP_SECONDS = 30 * 60
MAX_GAP_SECONDS = 90 * 60

# ─── TIME-OF-DAY ────────────────────────────────────────────────────────
ALLOWED_HOUR_RANGES = [(8, 23)]
DEFAULT_TZ_OFFSET_HOURS = -5

# ─── WARM-UP (user-adjusted 2026-05-30) ────────────────────────────────
WARMUP_DAYS_BEFORE_PAGE_CONNECT = 2   # was 7 — user said 1-2d is fine
WARMUP_DAYS_BEFORE_TOKEN_USE = 7      # was 14 — proportionally reduced
# (kept the spread so post-creation activity still ramps)


class PolicyViolation(Exception):
    pass


def check_can_create_today(today_count, week_count, month_count=0):
    if today_count >= DAILY_HARD_CAP:
        return False, (f"❌ Daily hard cap reached: {today_count}/{DAILY_HARD_CAP}. "
                       f"Wait until tomorrow.")
    if week_count >= WEEKLY_HARD_CAP:
        return False, f"❌ Weekly hard cap reached: {week_count}/{WEEKLY_HARD_CAP}."
    if month_count >= MONTHLY_HARD_CAP:
        return False, f"❌ Monthly hard cap reached: {month_count}/{MONTHLY_HARD_CAP}."
    return True, ""


def time_of_day_ok(now=None, tz_offset_hours=DEFAULT_TZ_OFFSET_HOURS):
    now = now or _dt.datetime.utcnow()
    local = now + _dt.timedelta(hours=tz_offset_hours)
    h = local.hour
    for lo, hi in ALLOWED_HOUR_RANGES:
        if lo <= h < hi: return True, ""
    return False, f"❌ Local time {local.strftime('%H:%M')} outside allowed hours."


def min_gap_seconds():
    return int(_random.uniform(MIN_GAP_BETWEEN_CREATIONS_SECONDS,
                                min(MAX_GAP_SECONDS, RECOMMENDED_GAP_SECONDS * 1.5)))


def check_4h_window(creations_in_last_4h):
    if creations_in_last_4h >= WINDOW_4H_HARD_CAP:
        return False, (f"❌ 4h-window cap reached: {creations_in_last_4h}/{WINDOW_4H_HARD_CAP}. "
                       f"Spread creations across the day, not bursts.")
    return True, ""


def can_use_for_page_connect(creation_dt):
    age_days = (_dt.datetime.utcnow() - creation_dt).days
    if age_days < WARMUP_DAYS_BEFORE_PAGE_CONNECT:
        return False, f"❌ Account too fresh: {age_days}d, need {WARMUP_DAYS_BEFORE_PAGE_CONNECT}d."
    return True, ""


def can_use_for_posting(creation_dt):
    age_days = (_dt.datetime.utcnow() - creation_dt).days
    if age_days < WARMUP_DAYS_BEFORE_TOKEN_USE:
        return False, f"❌ Too fresh for posting: {age_days}d, need {WARMUP_DAYS_BEFORE_TOKEN_USE}d."
    return True, ""


def count_creations_from_csv(csv_text):
    today = _dt.datetime.utcnow().date()
    week_start = today - _dt.timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    today_n = week_n = month_n = 0
    for line in csv_text.splitlines()[1:]:
        cols = line.split(',')
        if not cols or not cols[0]: continue
        try: dt = _dt.datetime.strptime(cols[0][:19], '%Y-%m-%d %H:%M:%S').date()
        except: continue
        if dt == today: today_n += 1
        if dt >= week_start: week_n += 1
        if dt >= month_start: month_n += 1
    return today_n, week_n, month_n


def count_in_last_4h(csv_text):
    now = _dt.datetime.utcnow()
    cutoff = now - _dt.timedelta(hours=4)
    n = 0
    for line in csv_text.splitlines()[1:]:
        cols = line.split(',')
        if not cols or not cols[0]: continue
        try: dt = _dt.datetime.strptime(cols[0][:19], '%Y-%m-%d %H:%M:%S')
        except: continue
        if dt >= cutoff: n += 1
    return n
