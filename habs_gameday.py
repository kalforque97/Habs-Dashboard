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


def txt(v):
    return v.get("default", "") if isinstance(v, dict) else str(v or "")


def period_label(pd):
    n, t = pd.get("number", 0), pd.get("periodType", "REG")
    if t == "OT":
        return "OT" if n <= 4 else f"{n - 3}OT"
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"P{n}")


def game_detail(g):
    """Scoring, goalies and team stats for one finished game."""
    gid = g["id"]
    age = (dt.date.today() - dt.date.fromisoformat(g["gameDate"])).days
    ttl = FOREVER if age > 7 else LIVE_TTL   # the NHL can correct recent scoring
    land = get(f"gamecenter/{gid}/landing", ttl) or {}
    box = get(f"gamecenter/{gid}/boxscore", ttl) or {}
    rail = get(f"gamecenter/{gid}/right-rail", ttl) or {}
    home, away = g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]
    summ = land.get("summary") or {}

    goals, score, lead = [], {home: 0, away: 0}, {home: 0, away: 0}
    for per in summ.get("scoring", []):
        label = period_label(per.get("periodDescriptor", {}))
        for gl in per.get("goals", []):
            team = txt(gl.get("teamAbbrev"))
            if team not in score:
                team = home if gl.get("isHome") else away
            other = away if team == home else home
            score[team] += 1
            lead[team] = max(lead[team], score[team] - score[other])
            name = txt(gl.get("name")) or f'{txt(gl.get("firstName"))} {txt(gl.get("lastName"))}'.strip()
            assists = [txt(a.get("name")) or f'{txt(a.get("firstName"))} {txt(a.get("lastName"))}'.strip()
                       for a in gl.get("assists", [])]
            tags = []
            if (gl.get("strength") or "").lower() in ("pp", "sh"):
                tags.append(gl["strength"].upper())
            if "empty" in (gl.get("goalModifier") or ""):
                tags.append("EN")
            goals.append({"period": label, "time": gl.get("timeInPeriod", ""), "team": team,
                          "name": name, "n": gl.get("goalsToDate"), "assists": assists, "tags": tags})

    goalies = []
    pbg = box.get("playerByGameStats") or {}
    for side, abbr in (("awayTeam", away), ("homeTeam", home)):
        for gk in (pbg.get(side) or {}).get("goalies", []):
            if gk.get("toi") in ("00:00", "0:00"):
                continue
            goalies.append({"team": abbr, "name": txt(gk.get("name")), "sv": gk.get("saveShotsAgainst", ""),
                            "pct": gk.get("savePctg"), "dec": gk.get("decision", "")})

    raw = rail.get("teamGameStats") or (box.get("summary") or {}).get("teamGameStats") or []
    stats = {s.get("category"): (s.get("homeValue"), s.get("awayValue")) for s in raw}
    return {"goals": goals, "goalies": goalies, "stats": stats, "lead": lead,
            "home": home, "away": away, "shootout": bool(summ.get("shootout"))}


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
--fill:rgba(118,118,128,.24);--accent:#E0384B;--green:#30D158;--red:#FF453A;--orange:#FF9F0A}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--l1);font:17px/1.29 -apple-system,BlinkMacSystemFont,
"SF Pro Text","Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;
padding:env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,0)}
main{max-width:1240px;margin:0 auto;padding:24px 16px 48px}
.hero{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:28px}
.logos{display:flex;align-items:center;gap:6px}.logos img{width:64px;height:64px}
.logos span{color:var(--l2);font-size:15px}
h1{font-size:34px;line-height:41px;font-weight:700;letter-spacing:.37px}
.sub{color:var(--l2);font-size:15px;margin-top:2px}
/* Phone: one column, hot players first. Desktop: games left, players right. */
.layout{display:grid;gap:28px}
.side{display:contents}
.hot{order:1}.games{order:2}.lead1{order:3}.lead2{order:4}
@media (min-width:900px){
 main{padding:40px 32px 64px}
 .hero{margin-bottom:36px}.logos img{width:80px;height:80px}
 .layout{grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:32px;align-items:start}
 .games{order:1}.side{display:grid;gap:28px;order:2}
}
h2{font-size:20px;line-height:25px;font-weight:600;margin:0 4px 4px}
.note{color:var(--l2);font-size:13px;margin:0 4px 8px}
.group{background:var(--card);border-radius:10px;overflow:hidden}
.group>*{position:relative}
.group>*+*::before{content:"";position:absolute;top:0;left:16px;right:0;height:.5px;background:var(--sep)}
.group>.ph+.ph::before{left:68px}
.row{display:flex;align-items:center;gap:12px;min-height:44px;padding:8px 16px}
.grow{flex:1;min-width:0}.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--l2);font-size:13px}
.shot{width:40px;height:40px;border-radius:50%;background:var(--fill);object-fit:cover;flex:none}
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
/* Expandable game rows */
summary{list-style:none;cursor:pointer;-webkit-tap-highlight-color:transparent}
summary::-webkit-details-marker{display:none}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:10px}
@media (hover:hover){summary:hover{background:var(--fill)}}
.chev{width:8px;height:13px;color:var(--l3);flex:none;transition:transform .2s ease}
details[open] .chev{transform:rotate(90deg)}
@media (prefers-reduced-motion:reduce){.chev{transition:none}}
.detail{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:8px 28px;padding:0 16px 16px}
.detail h3{font-size:15px;font-weight:600;margin:12px 0 4px}
.per{font-size:13px;color:var(--l2);margin:8px 0 2px}
.gl{display:flex;gap:10px;padding:3px 0;font-size:15px;align-items:baseline}
.gl .t{width:40px;color:var(--l2);font-variant-numeric:tabular-nums;flex:none;font-size:13px}
.tm{font-size:11px;font-weight:600;border-radius:4px;padding:1px 5px;background:var(--fill);flex:none}
.tm.h{background:var(--accent);color:#FFF}
.tag{font-size:11px;font-weight:600;color:var(--accent);margin-left:6px}
.dec{font-size:12px;font-weight:600;color:var(--l2);margin-left:6px}
.sh{display:grid;grid-template-columns:52px 1fr 52px;font-size:13px;font-weight:600;padding:4px 0}
.sh span{text-align:center}.sh b:last-child{text-align:right}
.srow{display:grid;grid-template-columns:52px 1fr 52px;align-items:center;min-height:30px;
border-top:.5px solid var(--sep);font-size:15px;font-variant-numeric:tabular-nums}
.srow .lbl{text-align:center;color:var(--l2);font-size:13px}
.v{justify-self:start;min-width:30px;text-align:center;padding:1px 7px;border-radius:999px}
.v:last-child{justify-self:end}
.v.hb{background:var(--accent);color:#FFF;font-weight:600}
.v.ob{background:var(--l1);color:var(--card);font-weight:600}
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


# (category, label, +1 = higher is better, -1 = lower is better)
STAT_ROWS = [("sog", "Shots", 1), ("faceoffWinningPctg", "Faceoff %", 1),
             ("powerPlayPctg", "Power play %", 1), ("ppOpps", "Power play opps", 1),
             ("pim", "Penalty minutes", -1), ("hits", "Hits", 1), ("blockedShots", "Blocked shots", 1),
             ("takeaways", "Takeaways", 1), ("giveaways", "Giveaways", -1), ("lead", "Biggest lead", 1)]


def stat_value(cat, stats, side, lead, team):
    """Returns (number for comparison, display text) for one team."""
    i = 0 if side == "home" else 1
    pp = str((stats.get("powerPlay") or (None, None))[i] or "")
    if cat == "lead":
        x = lead.get(team, 0)
        return x, str(x)
    if cat == "ppOpps":
        if "/" not in pp:
            return None, "–"
        x = int(pp.split("/")[1])
        return x, str(x)
    v = (stats.get(cat) or (None, None))[i]
    if v is None and cat == "powerPlayPctg" and "/" in pp:
        g, o = (int(n) for n in pp.split("/"))
        v = g / o if o else 0
    if v is None:
        return None, "–"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None, str(v)
    if cat in ("faceoffWinningPctg", "powerPlayPctg"):
        x = x * 100 if x <= 1 else x
        return x, f"{x:.1f}"
    return x, str(int(x)) if x == int(x) else f"{x:g}"


def detail_html(d, opp_code):
    habs_home = d["home"] == HABS
    opp_team = d["away"] if habs_home else d["home"]

    # Scoring, grouped by period
    sc, last_per = [], None
    for gl in d["goals"]:
        if gl["period"] != last_per:
            sc.append(f'<div class="per">{E(gl["period"])}</div>')
            last_per = gl["period"]
        n = f' ({gl["n"]})' if gl["n"] else ""
        tags = "".join(f'<span class="tag">{t}</span>' for t in gl["tags"])
        ast = ", ".join(E(a) for a in gl["assists"]) or "Unassisted"
        cls = " h" if gl["team"] == HABS else ""
        sc.append(f'<div class="gl"><span class="t">{E(gl["time"])}</span><span class="tm{cls}">{E(gl["team"])}</span>'
                  f'<div><div>{E(gl["name"])}{n}{tags}</div><div class="meta">{ast}</div></div></div>')
    if not sc:
        sc.append('<p class="meta">No goals in regulation or overtime.</p>' if d["stats"] or d["goalies"]
                  else '<p class="meta">Scoring data is not available for this game.</p>')
    if d["shootout"]:
        sc.append('<p class="meta" style="margin-top:6px">Decided by shootout.</p>')

    # Goalies
    gk = []
    for g in d["goalies"]:
        sv = str(g["sv"])
        line = f"{sv.split('/')[0]} saves on {sv.split('/')[1]}" if "/" in sv else sv
        if isinstance(g["pct"], (int, float)):
            line += " · " + f"{g['pct']:.3f}".lstrip("0")
        dec = f'<span class="dec">{E(g["dec"])}</span>' if g["dec"] else ""
        cls = " h" if g["team"] == HABS else ""
        gk.append(f'<div class="gl"><span class="tm{cls}">{E(g["team"])}</span>'
                  f'<div><div>{E(g["name"])}{dec}</div><div class="meta">{E(line)}</div></div></div>')
    gk_html = "".join(gk) or '<p class="meta">Goalie data is not available.</p>'

    # Team stats, Habs always on the left
    hs, os_ = ("home", "away") if habs_home else ("away", "home")
    rows = []
    for cat, label, sign in STAT_ROWS:
        if cat not in ("lead", "ppOpps", "powerPlayPctg") and cat not in d["stats"]:
            continue
        hx, ht = stat_value(cat, d["stats"], hs, d["lead"], HABS)
        ox, ot = stat_value(cat, d["stats"], os_, d["lead"], opp_team)
        if hx is None and ox is None:
            continue
        hc = oc = ""
        if hx is not None and ox is not None and hx != ox:
            if (hx - ox) * sign > 0:
                hc = " hb"
            else:
                oc = " ob"
        rows.append(f'<div class="srow"><span class="v{hc}">{ht}</span><span class="lbl">{label}</span>'
                    f'<span class="v{oc}">{ot}</span></div>')
    st_html = (f'<div class="sh"><b>{HABS}</b><span>Team stats</span><b>{E(opp_team)}</b></div>' + "".join(rows)
               if rows else '<p class="meta">Team stats are not available.</p>')

    return (f'<div class="detail"><div><h3>Scoring</h3>{"".join(sc)}</div>'
            f'<div><h3>Goalies</h3>{gk_html}<div style="margin-top:14px">{st_html}</div></div></div>')


CHEVRON = ('<svg class="chev" viewBox="0 0 8 13" aria-hidden="true"><path d="M1.5 1.5 6.5 6.5 1.5 11.5" '
           'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def leader_section(title, note, rows, cls=""):
    if not rows:
        body = '<div class="empty">No points recorded in this matchup.</div>'
    else:
        head = '<div class="stats head"><span>GP</span><span>G</span><span>A</span><span>P</span></div>'
        body = head + "".join(player_row(r, f'<div class="stats"><span>{r["gp"]}</span><span>{r["g"]}</span>'
                                            f'<span>{r["a"]}</span><b>{r["p"]}</b></div>') for r in rows)
    return f'<section class="{cls}"><h2>{E(title)}</h2><p class="note">{E(note)}</p><div class="group">{body}</div></section>'


def render(game, opp, h2h, details, habs_lead, opp_lead, hot_rows, habs_gp):
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
        body = detail_html(details[g["id"]], opp["abbrev"]) if g["id"] in details else \
            '<div class="detail"><p class="meta">Game details are not available.</p></div>'
        rows.append(f'<details><summary class="row"><span class="pill {code}">{code}</span><div class="grow">'
                    f'<div class="name">{where} {E(opp_side(g)["abbrev"])}</div><div class="meta">{d}{po}</div></div>'
                    f'<span class="score">{h}–{o}{(" " + extra) if extra else ""}</span>{CHEVRON}</summary>{body}</details>')
    h2h_html = "".join(rows) or '<div class="empty">No completed games found.</div>'
    h2h_note = f"Habs record: {w}–{l}–{otl}. Select a game to see the summary." if h2h else "No history in the lookback window."

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
<div class="layout">
<section class="games"><h2>Last {len(h2h)} meetings</h2><p class="note">{E(h2h_note)}</p><div class="group">{h2h_html}</div></section>
<div class="side">
<section class="hot"><h2>Hot Habs</h2><p class="note">{E(hot_note)}</p><div class="group">{hot_html}</div></section>
{leader_section(f"Habs who produce against {opp['abbrev']}", "Active Habs skaters. Career totals vs this opponent, regular season and playoffs, with any team.", habs_lead, "lead1")}
{leader_section(f"{opp['abbrev']} players who produce against the Habs", "Active opponent skaters. Career totals vs Montreal, regular season and playoffs, with any team.", opp_lead, "lead2")}
</div>
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
    details = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(game_detail, g): g["id"] for g in h2h}
        for f in cf.as_completed(futs):
            try:
                details[futs[f]] = f.result()
            except Exception as e:
                print(f"  skipped game {futs[f]} details: {e}", file=sys.stderr)
    habs_rows = run_all(skaters(HABS), aliases(opp_code), season, "Habs")
    opp_rows = run_all(skaters(opp_code), {HABS}, season, opp_code)
    habs_gp = max((len(r["last"]) for r in habs_rows), default=0)

    page = render(game, opp, h2h, details, leaders(habs_rows), leaders(opp_rows), hot(habs_rows), habs_gp)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {os.path.abspath(args.out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
