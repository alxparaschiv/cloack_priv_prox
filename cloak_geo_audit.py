#!/usr/bin/env python3
"""Per-link traffic-geography audit for the cloaking links.

Reads the Worker's daily analytics blobs from Cloudflare KV
(`stats:<slug>:<YYYY-MM-DD>` → {views, countries{}, ...}), aggregates
country counts per slug across all retained days (90-day TTL), and flags any
link whose share of TARGET-region traffic (US/UK/AU/CA) falls below a
threshold — i.e. links with predominantly non-US / non-English traffic.

Reads creds from env (same names as the bot):
  CLOAK_CF_ACCOUNT_ID, CLOAK_CF_API_TOKEN, CLOAK_CF_KV_NAMESPACE_ID

Run with the Railway secrets injected, so the token is never printed:
  railway run python3 cloak_geo_audit.py
or locally if you export those three vars yourself.

Flags:
  --target-share 0.90   minimum acceptable TARGET share (default 0.90)
  --min-views 25        ignore (as low-confidence) slugs below this many views
  --days 90             only look back this many UTC days (default: all retained)
"""
import os
import sys
import json
import argparse
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

API = "https://api.cloudflare.com/client/v4"

# The user's acceptance bar: traffic should be ~90%+ from these four.
TARGET = {"US", "GB", "AU", "CA"}
# Shown separately for context — arguably fine, but not in the strict 4.
ENGLISH_EXT = {"IE", "NZ"}

# Country code → friendly name + region, for readable rollups. Not exhaustive;
# unmapped codes fall back to the raw code and region "Other".
COUNTRY = {
    "US": ("United States", "North America (target)"),
    "GB": ("United Kingdom", "UK (target)"),
    "AU": ("Australia", "Oceania (target)"),
    "CA": ("Canada", "North America (target)"),
    "IE": ("Ireland", "Europe (English)"),
    "NZ": ("New Zealand", "Oceania (English)"),
    # Latin America
    "MX": ("Mexico", "Latin America"), "BR": ("Brazil", "Latin America"),
    "AR": ("Argentina", "Latin America"), "CO": ("Colombia", "Latin America"),
    "CL": ("Chile", "Latin America"), "PE": ("Peru", "Latin America"),
    "VE": ("Venezuela", "Latin America"), "EC": ("Ecuador", "Latin America"),
    "GT": ("Guatemala", "Latin America"), "BO": ("Bolivia", "Latin America"),
    "DO": ("Dominican Rep.", "Latin America"), "HN": ("Honduras", "Latin America"),
    "PY": ("Paraguay", "Latin America"), "SV": ("El Salvador", "Latin America"),
    "NI": ("Nicaragua", "Latin America"), "CR": ("Costa Rica", "Latin America"),
    "PA": ("Panama", "Latin America"), "UY": ("Uruguay", "Latin America"),
    "PR": ("Puerto Rico", "Latin America"), "CU": ("Cuba", "Latin America"),
    # Europe (non-UK)
    "DE": ("Germany", "Europe"), "FR": ("France", "Europe"),
    "ES": ("Spain", "Europe"), "IT": ("Italy", "Europe"),
    "NL": ("Netherlands", "Europe"), "PL": ("Poland", "Europe"),
    "RO": ("Romania", "Europe"), "PT": ("Portugal", "Europe"),
    "SE": ("Sweden", "Europe"), "NO": ("Norway", "Europe"),
    "DK": ("Denmark", "Europe"), "FI": ("Finland", "Europe"),
    "BE": ("Belgium", "Europe"), "AT": ("Austria", "Europe"),
    "CH": ("Switzerland", "Europe"), "GR": ("Greece", "Europe"),
    "CZ": ("Czechia", "Europe"), "HU": ("Hungary", "Europe"),
    "UA": ("Ukraine", "Europe"), "RU": ("Russia", "Europe/Asia"),
    "BG": ("Bulgaria", "Europe"), "HR": ("Croatia", "Europe"),
    "RS": ("Serbia", "Europe"), "SK": ("Slovakia", "Europe"),
    # MENA
    "TR": ("Turkey", "MENA"), "SA": ("Saudi Arabia", "MENA"),
    "AE": ("UAE", "MENA"), "EG": ("Egypt", "MENA"),
    "IL": ("Israel", "MENA"), "IQ": ("Iraq", "MENA"),
    "MA": ("Morocco", "MENA"), "DZ": ("Algeria", "MENA"),
    "JO": ("Jordan", "MENA"), "KW": ("Kuwait", "MENA"),
    "QA": ("Qatar", "MENA"), "LB": ("Lebanon", "MENA"),
    # Asia
    "IN": ("India", "Asia"), "PK": ("Pakistan", "Asia"),
    "BD": ("Bangladesh", "Asia"), "ID": ("Indonesia", "Asia"),
    "PH": ("Philippines", "Asia"), "VN": ("Vietnam", "Asia"),
    "TH": ("Thailand", "Asia"), "MY": ("Malaysia", "Asia"),
    "SG": ("Singapore", "Asia"), "JP": ("Japan", "Asia"),
    "KR": ("South Korea", "Asia"), "CN": ("China", "Asia"),
    "HK": ("Hong Kong", "Asia"), "TW": ("Taiwan", "Asia"),
    "NP": ("Nepal", "Asia"), "LK": ("Sri Lanka", "Asia"),
    # Africa (sub-Saharan)
    "NG": ("Nigeria", "Africa"), "ZA": ("South Africa", "Africa"),
    "KE": ("Kenya", "Africa"), "GH": ("Ghana", "Africa"),
    "ET": ("Ethiopia", "Africa"), "TZ": ("Tanzania", "Africa"),
    "UG": ("Uganda", "Africa"),
    "XX": ("Unknown/Hidden", "Unknown (VPN/proxy?)"),
}


def _name(cc):
    return COUNTRY.get(cc, (cc, None))[0]


def _region(cc):
    return COUNTRY.get(cc, (cc, "Other"))[1] or "Other"


def _req(path, token, method="GET", body=None):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.read()


def list_stats_keys(acct, ns, token):
    """All KV keys with prefix 'stats:' (paginated)."""
    keys, cursor = [], ""
    while True:
        q = {"limit": "1000", "prefix": "stats:"}
        if cursor:
            q["cursor"] = cursor
        path = (f"/accounts/{acct}/storage/kv/namespaces/{ns}/keys?"
                + urllib.parse.urlencode(q))
        body = json.loads(_req(path, token))
        if not body.get("success"):
            raise RuntimeError(f"KV list failed: {body.get('errors')}")
        keys.extend(k["name"] for k in body.get("result", []))
        cursor = (body.get("result_info") or {}).get("cursor") or ""
        if not cursor:
            break
    return keys


def get_value(acct, ns, token, key):
    path = (f"/accounts/{acct}/storage/kv/namespaces/{ns}/values/"
            + urllib.parse.quote(key, safe=""))
    try:
        return key, _req(path, token).decode()
    except Exception:
        return key, None


# Countries that are almost certainly NOT real audience: operator + setup
# pipeline (RO = you, AT = proxy/setup), datacenter/VPN exits (NL), and
# country-hidden hits (XX = VPN/proxy). These shouldn't count against you.
INFRA = {"AT", "RO", "NL", "XX"}


def _optimize_report(rows, fleet, fleet_total, f_target, args, pct, top_str):
    """How to push the fleet target share to args.goal by geo-gating the
    highest-leak links. Greedy: gate the links carrying the most non-target
    hits first, since those move the average most per link touched."""
    TARGET_CC = TARGET

    # Fleet totals (no exclusion — apples-to-apples with the manager bot).
    T = sum(n for cc, n in fleet.items() if cc in TARGET_CC)  # target hits
    F = fleet_total - T                                       # non-target hits
    infra_total = sum(n for cc, n in fleet.items() if cc in INFRA)
    # "Real audience" view: drop infra/self-traffic from the denominator.
    real_den = fleet_total - infra_total
    real_target = T / real_den if real_den else 0

    print("\n" + "=" * 78)
    print(" OPTIMIZATION PLAN — push fleet toward "
          f"{pct(args.goal)} US/UK/AU/CA")
    print("=" * 78)
    print(f" Fleet now (all traffic)      : {pct(f_target)} target  "
          f"({T:,} / {fleet_total:,})")
    print(f" Non-target hits to deal with : {F:,}")
    print(f"   of which infra/self (AT/RO/NL/XX): {infra_total:,} "
          f"({pct(infra_total/fleet_total)} of all traffic)")
    print(f"   of which real foreign audience   : {F-infra_total:,} "
          f"({pct((F-infra_total)/fleet_total)} of all traffic)")
    print(f"\n If infra/self-traffic simply didn't count, your REAL-audience")
    print(f" share is already {pct(real_target)}. Two levers to 92-95%:")
    print(f"   (A) stop logging your own pipeline hits  → +{pct(real_target-f_target)} for free")
    print(f"   (B) geo-gate real foreign audience on the leakiest links ↓")

    # Distribution buckets (by link count and by volume).
    bands = [("90%+", 0.90, 1.01), ("80-90%", 0.80, 0.90),
             ("70-80%", 0.70, 0.80), ("50-70%", 0.50, 0.70),
             ("<50%", -0.01, 0.50)]
    print("\n DISTRIBUTION (links with >= "
          f"{args.min_opt_views} views):")
    big = [r for r in rows if r["geo_total"] >= args.min_opt_views]
    big_vol = sum(r["geo_total"] for r in big) or 1
    for label, lo, hi in bands:
        grp = [r for r in big if lo <= r["target_share"] < hi]
        vol = sum(r["geo_total"] for r in grp)
        print(f"   {label:<7} {len(grp):>3} links   {pct(vol/big_vol):>4} of volume")

    # Greedy geo-gate plan: gate links by non-target hit count until goal met.
    # Gating a link removes ALL its non-target hits from F (allowlist = only
    # US/UK/AU/CA pass), target hits stay. Skip links already >= goal.
    cand = []
    for r in big:
        nt = r["geo_total"] - sum(n for cc, n in r["countries"].items()
                                  if cc in TARGET_CC)
        if r["target_share"] < args.goal and nt > 0:
            cand.append((r, nt))
    cand.sort(key=lambda x: -x[1])

    # How many non-target hits must be removed to reach the goal?
    # T / (T + F - x) = goal  ->  x = T + F - T/goal
    need_remove = max(0, (T + F) - (T / args.goal))
    print(f"\n TO REACH {pct(args.goal)} FLEET: remove {need_remove:,.0f} "
          f"non-target hits by gating links below {pct(args.goal)}.")
    print(" Greedy order (gate these, biggest leak first):\n")
    removed, F_run = 0, F
    hit_goal_at = None
    print(f"   {'#':>2}  {'slug':<22} {'tgt%':>5} {'views':>6} "
          f"{'non-tgt':>7}  {'fleet% after':>12}  top non-target")
    for i, (r, nt) in enumerate(cand, 1):
        removed += nt
        F_run -= nt
        fleet_after = T / (T + F_run) if (T + F_run) else 1
        flag = ""
        if hit_goal_at is None and fleet_after >= args.goal:
            hit_goal_at = i
            flag = "  ← goal reached"
        print(f"   {i:>2}  {r['slug']:<22} {pct(r['target_share']):>5} "
              f"{r['geo_total']:>6,} {nt:>7,}  {pct(fleet_after):>12}  "
              f"{top_str(r['non_target'], r['geo_total'], 2)}{flag}")
        if hit_goal_at and i >= hit_goal_at + 3:
            break
    if hit_goal_at:
        print(f"\n → Gate the top {hit_goal_at} links above and the fleet "
              f"hits {pct(args.goal)}. Volume cost: {removed:,} non-target "
              f"clicks dropped (you keep every US/UK/AU/CA visitor).")
    else:
        print(f"\n → Even gating all {len(cand)} candidate links only reaches "
              f"{pct(T/(T+F_run))}. (Goal may need a lower bar or upstream "
              f"distribution fixes.)")

    # 95% stretch
    need95 = max(0, (T + F) - (T / 0.95))
    print(f"\n For 95%: remove {need95:,.0f} non-target hits "
          f"({pct(need95/F) if F else '0%'} of all non-target traffic).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-share", type=float, default=0.90)
    ap.add_argument("--min-views", type=int, default=25)
    ap.add_argument("--days", type=int, default=0, help="0 = all retained")
    ap.add_argument("--exclude-cc", default="",
                    help="comma CCs to drop as infra/self-traffic (e.g. AT,RO,NL,XX)")
    ap.add_argument("--optimize", action="store_true",
                    help="print geo-gate optimization plan to reach --goal")
    ap.add_argument("--goal", type=float, default=0.92,
                    help="target fleet share for the optimization plan")
    ap.add_argument("--min-opt-views", type=int, default=100,
                    help="ignore links below this volume in the optimization plan")
    args = ap.parse_args()
    exclude = {c.strip().upper() for c in args.exclude_cc.split(",") if c.strip()}

    acct = os.getenv("CLOAK_CF_ACCOUNT_ID", "").strip()
    ns = os.getenv("CLOAK_CF_KV_NAMESPACE_ID", "").strip()
    token = os.getenv("CLOAK_CF_API_TOKEN", "").strip()
    if not (acct and ns and token):
        sys.exit("Missing CLOAK_CF_ACCOUNT_ID / CLOAK_CF_KV_NAMESPACE_ID / "
                 "CLOAK_CF_API_TOKEN in env. Run via `railway run python3 "
                 "cloak_geo_audit.py`.")

    cutoff = None
    if args.days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).date()

    print("Listing stats keys…", file=sys.stderr)
    keys = list_stats_keys(acct, ns, token)
    if cutoff:
        def _keep(k):
            parts = k.split(":")
            try:
                d = datetime.strptime(parts[2], "%Y-%m-%d").date()
                return d >= cutoff
            except Exception:
                return True
        keys = [k for k in keys if _keep(k)]
    print(f"Fetching {len(keys)} daily blobs…", file=sys.stderr)

    # Per-slug aggregation.
    slug_countries = defaultdict(lambda: defaultdict(int))
    slug_views = defaultdict(int)
    slug_go = defaultdict(int)
    slug_days = defaultdict(set)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = ex.map(lambda k: get_value(acct, ns, token, k), keys)
        for key, val in results:
            if not val:
                continue
            parts = key.split(":")  # stats:<slug>:<date>  (slug may contain '-' not ':')
            if len(parts) < 3:
                continue
            slug = ":".join(parts[1:-1])
            date = parts[-1]
            try:
                blob = json.loads(val)
            except Exception:
                continue
            slug_views[slug] += blob.get("views", 0)
            slug_go[slug] += blob.get("go", 0)
            slug_days[slug].add(date)
            for cc, n in (blob.get("countries") or {}).items():
                slug_countries[slug][cc.upper()] += n

    if not slug_views:
        print("\nNo analytics data found in KV yet (no stats:* keys with "
              "country data). Either the Worker analytics build isn't live, "
              "or no human traffic has hit the links since 2026-05-14.")
        return

    # Build per-slug report rows.
    rows = []
    for slug, views in slug_views.items():
        countries = {cc: n for cc, n in slug_countries[slug].items()
                     if cc not in exclude}
        total_geo = sum(countries.values()) or 1
        target_n = sum(n for cc, n in countries.items() if cc in TARGET)
        ext_n = sum(n for cc, n in countries.items() if cc in ENGLISH_EXT)
        target_share = target_n / total_geo
        # Region rollup
        region_n = defaultdict(int)
        for cc, n in countries.items():
            region_n[_region(cc)] += n
        # Top non-target countries
        non_target = sorted(
            ((cc, n) for cc, n in countries.items() if cc not in TARGET),
            key=lambda x: -x[1])
        rows.append({
            "slug": slug, "views": views, "geo_total": total_geo,
            "go": slug_go[slug], "days": len(slug_days[slug]),
            "target_share": target_share, "ext_share": (target_n + ext_n) / total_geo,
            "region_n": dict(region_n), "non_target": non_target,
            "countries": dict(countries),
        })

    rows.sort(key=lambda r: r["target_share"])  # worst first

    def pct(x):
        return f"{x*100:.0f}%"

    def top_str(non_target, total, k=4):
        out = []
        for cc, n in non_target[:k]:
            out.append(f"{_name(cc)} {pct(n/total)}")
        return ", ".join(out) if out else "—"

    flagged = [r for r in rows
               if r["target_share"] < args.target_share and r["geo_total"] >= args.min_views]
    low_data = [r for r in rows
                if r["target_share"] < args.target_share and r["geo_total"] < args.min_views]
    clean = [r for r in rows if r["target_share"] >= args.target_share]

    # ── Report ──────────────────────────────────────────────────────────────
    all_dates = set()
    for s in slug_days.values():
        all_dates |= s
    dr = f"{min(all_dates)} → {max(all_dates)}" if all_dates else "n/a"
    print("\n" + "=" * 78)
    print(" CLOAK LINK — TRAFFIC GEOGRAPHY AUDIT")
    print("=" * 78)
    print(f" Slugs with traffic : {len(rows)}")
    print(f" Date range covered : {dr}")
    print(f" Total geo-tagged hits: {sum(r['geo_total'] for r in rows):,}")
    print(f" Target regions     : US, UK, AU, CA  (bar: {pct(args.target_share)}+)")
    print(f" Min views for flag : {args.min_views}")

    # Fleet-wide aggregate (volume-weighted) — should match the posting-bot
    # manager's geo summary. Proves we're reading the same signal.
    fleet = defaultdict(int)
    for slug in slug_countries:
        for cc, n in slug_countries[slug].items():
            if cc in exclude:
                continue
            fleet[cc] += n
    fleet_total = sum(fleet.values()) or 1
    fleet_region = defaultdict(int)
    for cc, n in fleet.items():
        fleet_region[_region(cc)] += n
    f_target = sum(n for cc, n in fleet.items() if cc in TARGET) / fleet_total
    f_latam = sum(n for cc, n in fleet.items()
                  if _region(cc) == "Latin America") / fleet_total
    print(f"\n FLEET-WIDE (volume-weighted, excl {sorted(exclude) or 'none'}):")
    for cc in ("US", "GB", "CA", "AU"):
        print(f"   {_name(cc):<18} {pct(fleet.get(cc,0)/fleet_total)}")
    print(f"   {'→ TARGET total':<18} {pct(f_target)}")
    print(f"   {'Latin America':<18} {pct(f_latam)}")
    for name, n in sorted(fleet_region.items(), key=lambda x: -x[1]):
        if "target" in name or name == "Latin America":
            continue
        print(f"   {name:<18} {pct(n/fleet_total)}")

    if args.optimize:
        _optimize_report(rows, fleet, fleet_total, f_target, args, pct, top_str)
        return

    print("\n" + "-" * 78)
    print(f" 🚩 FLAGGED — below {pct(args.target_share)} target traffic "
          f"({len(flagged)} links)")
    print("-" * 78)
    if not flagged:
        print(" none — every link with enough traffic is at/above the bar. 🎉")
    for r in flagged:
        print(f"\n  • {r['slug']}   "
              f"target {pct(r['target_share'])}  |  {r['views']:,} views, "
              f"{r['days']}d")
        print(f"      top non-target: {top_str(r['non_target'], r['geo_total'])}")
        reg = sorted(r["region_n"].items(), key=lambda x: -x[1])
        reg_str = ", ".join(f"{name} {pct(n/r['geo_total'])}"
                            for name, n in reg if "target" not in name)[:120]
        print(f"      regions: {reg_str}")

    if low_data:
        print("\n" + "-" * 78)
        print(f" ⚠️  LOW-DATA (off-target but < {args.min_views} views — "
              f"treat as noise) ({len(low_data)})")
        print("-" * 78)
        for r in low_data:
            print(f"  • {r['slug']}  target {pct(r['target_share'])}  "
                  f"({r['views']} views) — top: "
                  f"{top_str(r['non_target'], r['geo_total'], 2)}")

    print("\n" + "-" * 78)
    print(f" ✅ CLEAN — at/above {pct(args.target_share)} ({len(clean)} links)")
    print("-" * 78)
    for r in sorted(clean, key=lambda r: -r["views"]):
        print(f"  • {r['slug']:<24} target {pct(r['target_share'])}  "
              f"({r['views']:,} views)")

    # CSV dump for the full picture.
    out_csv = "/tmp/cloak_geo_audit.csv"
    try:
        import csv
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "views", "geo_hits", "days", "target_share",
                        "ext_share", "top_countries"])
            for r in rows:
                top = "; ".join(f"{cc}:{n}" for cc, n in
                                sorted(r["countries"].items(), key=lambda x: -x[1])[:10])
                w.writerow([r["slug"], r["views"], r["geo_total"], r["days"],
                            f"{r['target_share']:.3f}", f"{r['ext_share']:.3f}", top])
        print(f"\n Full per-slug breakdown written to {out_csv}")
    except Exception as e:
        print(f"\n (CSV dump skipped: {e})")


if __name__ == "__main__":
    main()
