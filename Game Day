#!/usr/bin/env python3
"""Montreal Canadiens game-day dashboard.

Pulls data from the NHL web API (api-web.nhle.com, the API behind nhl.com)
and writes one static HTML file. Standard library only. Python 3.9+.

Usage:
    python3 habs_gameday.py                  # next Habs game
    python3 habs_gameday.py --opponent TOR   # next Habs game against TOR
    python3 habs_gameday.py --refresh        # ignore cached current-season data
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

API = "https://api-web.nhle.com/v1"
HABS = "MTL"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nhl_cache")
LIVE_TTL = 3 * 3600          # current-season data: refresh after 3 h
FOREVER = None               # past seasons do not change
WORKERS = 6                  # keep low; the API rate-limits bursts
H2H_COUNT = 10
LEADER_COUNT = 5
HOT_WINDOW = 5
HOT_MIN_STREAK = 2           # "hot" if point streak >= this ...
HOT_MIN_GOALS = 2            # ... or goals in last HOT_WINDOW games >= this
MAX_LOOKBACK_SEASONS = 15
LOCAL_TZ = ZoneInfo("America/Toronto")
# Relocated franchises. Remove an entry if you do not want to merge history.
ALIASES = {"UTA": {"UTA", "ARI", "PHX"}}

_refresh = False


# ---------------------------------------------------------------- data access
def get(path, ttl=LIVE_TTL):
    url = f"{API}/{path.lstrip('/')}"
    fp = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")
    fresh = ttl is None or (time.time() - os.path.getmtime(fp) < ttl if os.path.exists(fp) else False)
    if os.path.exists(fp) and fresh and not (_refresh and ttl is not None):
        with open(fp) as f:
            return json.load(f)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "habs-gameday/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = f"{fp}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, fp)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


def prev_season(s):
    y = s // 10000
    return (y - 1) * 10000 + y


def aliases(abbrev):
    return ALIASES.get(abbrev, {abbrev})


def habs_side(g):
    return g["homeTeam"] if g["homeTeam"]["abbrev"] == HABS else g["awayTeam"]


def opp_side(g):
    return g["awayTeam"] if g["homeTeam"]["abbrev"] == HABS else g["homeTeam"]


def team_name(t):
    place = (t.get("placeName") or {}).get("default", "")
    common = (t.get("commonName") or {}).get("default", "")
    return f"{place} {common}".strip() or t["abbrev"]


def full_name(p):
    return f"{p['firstName']['default']} {p['lastName']['default']}"


# ---------------------------------------------------------------- analysis
def find_game(opponent=None):
    sched = get(f"club-schedule-season/{HABS}/now")
    games = sched.get("games", [])
    season = sched.get("currentSeason") or (games[0]["season"] if games else None)
    for g in sorted(games, key=lambda g: g["startTimeUTC"]):
        if g["gameType"] not in (2, 3) or g["gameState"] in ("OFF", "FINAL"):
            continue
        if opponent and opp_side(g)["abbrev"] not in aliases(opponent):
            continue
        return g, season
    return None, season


def head_to_head(opp, season):
    out, s = [], season
    for _ in range(MAX_LOOKBACK_SEASONS):
        data = get(f"club-schedule-season/{HABS}/{s}", LIVE_TTL if s == season else FOREVER) or {}
        games = [g for g in data.get("games", [])
                 if g["gameType"] in (2, 3) and g["gameState"] in ("OFF", "FINAL")
                 and opp_side(g)["abbrev"] in aliases(opp)]
        out.extend(sorted(games, key=lambda g: g["startTimeUTC"], reverse=True))
        if len(out) >= H2H_COUNT:
            break
        s = prev_season(s)
    return out[:H2H_COUNT]


def result(g):
    h, o = habs_side(g).get("score", 0), opp_side(g).get("score", 0)
    extra = (g.get("gameOutcome") or {}).get("lastPeriodType", "")
    extra = extra if extra in ("OT", "SO") else ""
    code = "W" if h > o else ("OTL" if extra else "L")
    return code, h, o, extra


def analyze(player, vs_set, season):
    """Returns career line vs the opponent set, plus current-season form."""
    pid = player["id"]
    land = get(f"player/{pid}/landing") or {}
    pairs = sorted({(t["season"], t["gameTypeId"]) for t in land.get("seasonTotals", [])
                    if t.get("leagueAbbrev") == "NHL" and t.get("gameTypeId") in (2, 3)})
    logs = []
    for s, gt in pairs:
        d = get(f"player/{pid}/game-log/{s}/{gt}", LIVE_TTL if s >= season else FOREVER) or {}
        for x in d.get("gameLog", []):
            x["_s"], x["_gt"] = s, gt
            logs.append(x)
    vs = [x for x in logs if x.get("opponentAbbrev") in vs_set]
    cur = sorted((x for x in logs if x["_s"] == season and x["_gt"] == 2),
                 key=lambda x: x["gameDate"], reverse=True)
    streak = 0
    for x in cur:
        if x.get("points", 0) > 0:
            streak += 1
        else:
            break
    last = cur[:HOT_WINDOW]
    return {
        "id": pid, "name": full_name(player), "num": player.get("sweaterNumber", ""),
        "pos": player.get("positionCode", ""), "headshot": player.get("headshot", ""),
        "gp": len(vs), "g": sum(x.get("goals", 0) for x in vs),
        "a": sum(x.get("assists", 0) for x in vs), "p": sum(x.get("points", 0) for x in vs),
        "streak": streak, "last": [x.get("points", 0) for x in last],
        "lg": sum(x.get("goals", 0) for x in last), "lp": sum(x.get("points", 0) for x in last),
    }


def skaters(team):
    r = get(f"roster/{team}/current") or {}
    return r.get("forwards", []) + r.get("defensemen", [])


def run_all(players, vs_set, season, label):
    out = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(analyze, p, vs_set, season) for p in players]
        for i, f in enumerate(cf.as_completed(futs), 1):
            print(f"\r{label}: {i}/{len(futs)} players", end="", file=sys.stderr)
            try:
                out.append(f.result())
            except Exception as e:  # one bad player must not stop the build
                print(f"\n  skipped one player: {e}", file=sys.stderr)
    print(file=sys.stderr)
    return out


def leaders(rows):
    rows = [r for r in rows if r["p"] > 0]
    return sorted(rows, key=lambda r: (-r["p"], -r["g"], r["gp"]))[:LEADER_COUNT]


def hot(rows):
    rows = [r for r in rows if r["streak"] >= HOT_MIN_STREAK or r["lg"] >= HOT_MIN_GOALS]
    return sorted(rows, key=lambda r: (-r["streak"], -r["lg"], -r["lp"]))


# ---------------------------------------------------------------- rendering
E = html.escape

CSS = """
:root{--bg:#F2F2F7;--card:#FFF;--l1:#000;--l2:rgba(60,60,67,.6);--l3:rgba(60,60,67,.3);
--sep:rgba(60,60,67,.29);--fill:rgba(120,120,128,.12);--accent:#AF1E2D;--green:#34C759;
--red:#FF3B30;--orange:#FF9500;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1C1C1E;--l1:#FFF;
--l2:rgba(235,235,245,.6);--l3:rgba(235,235,245,.3);--sep:rgba(84,84,88,.6);
--fill:rgba(118,118,128,.24);--accent:#FF4D5E;--green:#30D158;--red:#FF453A;--orange:#FF9F0A}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--l1);font:17px/1.29 -apple-system,BlinkMacSystemFont,
"SF Pro Text","Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;
padding:env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,0)}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
.hero{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:28px}
.logos{display:flex;align-items:center;gap:6px}.logos img{width:64px;height:64px}
.logos span{color:var(--l2);font-size:15px}
h1{font-size:34px;line-height:41px;font-weight:700;letter-spacing:.37px}
.sub{color:var(--l2);font-size:15px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:24px 20px;align-items:start}
h2{font-size:20px;line-height:25px;font-weight:600;margin:0 4px 4px}
.note{color:var(--l2);font-size:13px;margin:0 4px 8px}
.group{background:var(--card);border-radius:10px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;min-height:44px;padding:8px 16px;position:relative}
.row+.row::before{content:"";position:absolute;top:0;left:16px;right:0;height:.5px;background:var(--sep)}
.row.ph+.row::before,.row+.row.ph::before{left:68px}
.grow{flex:1;min-width:0}.name{font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--l2);font-size:13px}
.shot{width:40px;height:40px;border-radius:50%;background:var(--fill);object-fit:cover;flex:none}
.num{font-variant-numeric:tabular-nums}
.stats{display:grid;grid-template-columns:repeat(4,32px);text-align:right;gap:4px;font-variant-numeric:tabular-nums}
.stats.head{color:var(--l2);font-size:12px;padding:6px 16px 0;justify-content:end}
.stats b{font-weight:600}
.pill{font-size:13px;font-weight:600;border-radius:6px;padding:2px 0;width:44px;text-align:center;color:#FFF;flex:none}
.W{background:var(--green)}.L{background:var(--red)}.OTL{background:var(--orange)}
.score{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.dots{display:flex;gap:4px;justify-content:flex-end;margin-top:4px}
.dot{width:10px;height:10px;border-radius:50%;border:1.5px solid var(--l3)}
.dot.on{background:var(--accent);border-color:var(--accent)}
.badge{color:var(--accent);font-weight:600;font-size:15px;text-align:right;white-space:nowrap}
.empty{padding:14px 16px;color:var(--l2);font-size:15px}
footer{color:var(--l2);font-size:12px;margin-top:32px;line-height:1.5}
"""


def logo(t):
    light, dark = E(t.get("logo", "")), E(t.get("darkLogo") or t.get("logo", ""))
    return (f'<picture><source srcset="{dark}" media="(prefers-color-scheme: dark)">'
            f'<img src="{light}" alt="{E(t["abbrev"])}"></picture>')


def player_row(r, right):
    img = f'<img class="shot" src="{E(r["headshot"])}" alt="" loading="lazy">' if r["headshot"] else '<div class="shot"></div>'
    return (f'<div class="row ph">{img}<div class="grow"><div class="name">{E(r["name"])}</div>'
            f'<div class="meta">#{E(str(r["num"]))} · {E(r["pos"])}</div></div>{right}</div>')


def leader_section(title, note, rows):
    if not rows:
        body = '<div class="empty">No points recorded in this matchup.</div>'
    else:
        head = '<div class="stats head"><span>GP</span><span>G</span><span>A</span><span>P</span></div>'
        body = head + "".join(player_row(r, f'<div class="stats"><span>{r["gp"]}</span><span>{r["g"]}</span>'
                                            f'<span>{r["a"]}</span><b>{r["p"]}</b></div>') for r in rows)
    return f'<section><h2>{E(title)}</h2><p class="note">{E(note)}</p><div class="group">{body}</div></section>'


def render(game, opp, h2h, habs_lead, opp_lead, hot_rows, habs_gp):
    if game:
        start = dt.datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        home = game["homeTeam"]["abbrev"] == HABS
        when = start.strftime("%A, %B %-d · %-I:%M %p ET")
        venue = (game.get("venue") or {}).get("default", "")
        sub = f"{when} · {venue}" if venue else when
        title = f'Canadiens {"vs" if home else "at"} {team_name(opp)}'
    else:
        sub, title = "No scheduled game found against this opponent", f"Canadiens vs {opp['abbrev']}"
    habs_t = {"abbrev": HABS, "logo": f"https://assets.nhle.com/logos/nhl/svg/{HABS}_light.svg",
              "darkLogo": f"https://assets.nhle.com/logos/nhl/svg/{HABS}_dark.svg"}
    if game:
        habs_t = habs_side(game)

    # Head to head
    w = sum(1 for g in h2h if result(g)[0] == "W")
    l = sum(1 for g in h2h if result(g)[0] == "L")
    otl = len(h2h) - w - l
    rows = []
    for g in h2h:
        code, h, o, extra = result(g)
        d = dt.date.fromisoformat(g["gameDate"]).strftime("%b %-d, %Y")
        where = "vs" if g["homeTeam"]["abbrev"] == HABS else "at"
        po = " · Playoffs" if g["gameType"] == 3 else ""
        rows.append(f'<div class="row"><span class="pill {code}">{code}</span><div class="grow">'
                    f'<div class="name">{where} {E(opp_side(g)["abbrev"])}</div><div class="meta">{d}{po}</div></div>'
                    f'<span class="score">{h}–{o}{(" " + extra) if extra else ""}</span></div>')
    h2h_html = "".join(rows) or '<div class="empty">No completed games found.</div>'
    h2h_note = f"Habs record: {w}–{l}–{otl}" if h2h else "No history in the lookback window."

    # Hot players
    if hot_rows:
        hot_html = ""
        for r in hot_rows:
            dots = "".join(f'<span class="dot{" on" if p > 0 else ""}"></span>' for p in reversed(r["last"]))
            badge = f'{r["streak"]}-game point streak' if r["streak"] >= HOT_MIN_STREAK else f'{r["lg"]} G in last {len(r["last"])}'
            right = (f'<div><div class="badge">{badge}</div><div class="meta" style="text-align:right">'
                     f'{r["lg"]} G · {r["lp"]} P</div><div class="dots" title="Oldest to newest; filled = point">{dots}</div></div>')
            hot_html += player_row(r, right)
    else:
        hot_html = '<div class="empty">No active point streaks or multi-goal runs right now.</div>'
    hot_note = (f"Point streak of {HOT_MIN_STREAK}+ games, or {HOT_MIN_GOALS}+ goals in the last {HOT_WINDOW}. "
                f"Regular season only.")
    if habs_gp < HOT_WINDOW:
        hot_note += f" Only {habs_gp} games played this season."

    gen = dt.datetime.now(LOCAL_TZ).strftime("%B %-d, %Y at %-I:%M %p ET")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(title)}</title><style>{CSS}</style></head><body><main>
<header class="hero"><div class="logos">{logo(habs_t)}<span>vs</span>{logo(opp)}</div>
<div><h1>{E(title)}</h1><p class="sub">{E(sub)}</p></div></header>
<div class="grid">
<section><h2>Hot Habs</h2><p class="note">{E(hot_note)}</p><div class="group">{hot_html}</div></section>
<section><h2>Last {len(h2h)} meetings</h2><p class="note">{E(h2h_note)}</p><div class="group">{h2h_html}</div></section>
{leader_section(f"Habs who produce against {opp['abbrev']}", "Active Habs skaters. Career totals vs this opponent, regular season and playoffs, with any team.", habs_lead)}
{leader_section(f"{opp['abbrev']} players who produce against the Habs", "Active opponent skaters. Career totals vs Montreal, regular season and playoffs, with any team.", opp_lead)}
</div>
<footer>Data: NHL web API (api-web.nhle.com), the source behind nhl.com. Generated {gen}.
Skaters only. "Active" means the current NHL roster, so players on injured reserve can be missing.</footer>
</main></body></html>"""


# ---------------------------------------------------------------- main
def main():
    global _refresh
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opponent", help="Three-letter team code, e.g. TOR. Default: next opponent.")
    ap.add_argument("--out", default="habs_gameday.html")
    ap.add_argument("--refresh", action="store_true", help="Refetch current-season data.")
    args = ap.parse_args()
    _refresh = args.refresh

    opponent = args.opponent.upper() if args.opponent else None
    game, season = find_game(opponent)
    if game:
        opp = opp_side(game)
    elif opponent:
        opp = {"abbrev": opponent, "logo": f"https://assets.nhle.com/logos/nhl/svg/{opponent}_light.svg",
               "darkLogo": f"https://assets.nhle.com/logos/nhl/svg/{opponent}_dark.svg"}
    else:
        sys.exit("No upcoming Habs game found. Use --opponent to pick a team.")
    opp_code = opp["abbrev"]
    print(f"Season {season}: Habs vs {opp_code}", file=sys.stderr)

    h2h = head_to_head(opp_code, season)
    habs_rows = run_all(skaters(HABS), aliases(opp_code), season, "Habs")
    opp_rows = run_all(skaters(opp_code), {HABS}, season, opp_code)
    habs_gp = max((len(r["last"]) for r in habs_rows), default=0)

    page = render(game, opp, h2h, leaders(habs_rows), leaders(opp_rows), hot(habs_rows), habs_gp)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {os.path.abspath(args.out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
