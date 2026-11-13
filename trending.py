"""Finds whoever is going off on Twitch and Kick right now, beyond the fixed creator list in sources.py.

Why (Nov 2026): our Speed/Kai/Jynxzi Shorts sat at a median of 1 view while each trend scan found 34-36 other
channels clipping Speed and Kai. The small channels that win clip someone who is blowing up before the clip farms
pile in (a 448-sub channel got 34k views on a TheBurntPeanut Short). So each daily run:

1. Twitch (needs the free TWITCH_CLIENT_ID/SECRET): the most-viewed clips made in the last day in Just Chatting and
   the top live categories, from any English streamer. Clips are grouped by streamer and scored by how fast they're
   gaining views (views per hour), so a streamer having a moment today beats a big name having a normal day.
2. Kick (no keys): the week's top clips of a watchlist of big Kick channels (Kick has no public site-wide feed).
3. Competition: how many channels in the latest YouTube trend scan already clip that streamer. Nobody clipping
   them yet is a bonus; a crowded name is marked down.

The top few new streamers are saved to reports/builder/trending_sources.json. sources.py loads that file, so every
agent (builder, council, scripts, review) treats them like any hand-added creator; they drop out of daily scans
once they stop trending, and a creator you mark "blocked" in sources.py is never auto-added.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import live_moments as LM

FILE = Path(__file__).parent / "reports" / "builder" / "trending_sources.json"
JUST_CHATTING = "509658"
KICK_WATCH = ["xqc", "adinross", "n3on", "clix", "iceposeidon", "cuffem"]  # English, clip-friendly; edit freely
ACTIVE_DAYS = 3        # a discovered streamer is scanned while they trended in the last 3 days
MAX_NEW = 6            # new streamers added per run, so the day's meeting isn't flooded with unknowns
MIN_CLIP_VPH = 1500    # their best clip must be gaining at least this many views/hour to count as "going off"


def load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def active(entries=None, now=None):
    """Discovered sources still trending (seen in the last ACTIVE_DAYS), in sources.py's shape."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for key, e in (entries if entries is not None else load()).items():
        try:
            seen = datetime.fromisoformat(e["last_seen"])
        except (KeyError, ValueError):
            continue
        if now - seen <= timedelta(days=ACTIVE_DAYS):
            out[key] = e
    return out


def crowd(name_words, videos):
    """Distinct channels in the trend scan already posting clips that mention this streamer."""
    pats = [re.compile(rf"\b{re.escape(w)}\b", re.I) for w in name_words if len(w) >= 3]
    chans = set()
    for v in videos:
        text = " ".join([v.get("title", ""), v.get("channel", ""), " ".join(v.get("tags") or [])])
        if any(p.search(text) for p in pats):
            chans.add(v.get("channel_id") or v.get("channel"))
    return len(chans)


def crowd_factor(n):
    """Rank multiplier: nobody clipping them yet is the opening we want; a clip farm pile-on is not."""
    return 1.3 if n == 0 else 1.1 if n <= 3 else 0.9 if n <= 10 else 0.7


def _twitch_get(h, path, **params):
    r = requests.get(f"https://api.twitch.tv/helix/{path}", headers=h, params=params, timeout=30)
    return r.json().get("data", []) if r.ok else []


def twitch_trending(h, since, now, top_games=5):
    """{login: {name, best_vph, views, clips, broadcaster_id}} from the day's fastest clips across categories."""
    games = [JUST_CHATTING] + [g["id"] for g in _twitch_get(h, "games/top", first=top_games + 1)
                               if g["id"] != JUST_CHATTING][:top_games]
    by, seen = {}, set()
    for gid in games:
        for c in _twitch_get(h, "clips", game_id=gid, started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"), first=50):
            if c.get("language", "en") not in ("en", "en-gb") or (c.get("id") and c["id"] in seen):
                continue
            seen.add(c.get("id"))
            vph = LM.clip_velocity(c, now)
            b = by.setdefault(c["broadcaster_id"], {"name": c["broadcaster_name"], "best_vph": 0, "views": 0, "clips": 0})
            b["best_vph"] = max(b["best_vph"], vph)
            b["views"] += c["view_count"]
            b["clips"] += 1
    if not by:
        return {}
    out = {}
    ids = list(by)
    for i in range(0, len(ids), 100):  # clips carry the display name; the clips page and API want the login
        for u in _twitch_get(h, "users", id=ids[i:i + 100]):
            out[u["login"]] = dict(by[u["id"]], broadcaster_id=u["id"])
    return out


def kick_trending(since, now):
    out = {}
    for slug in KICK_WATCH:
        clips = LM.kick_clips(slug, since, timeout=60)
        if clips:
            vph = max(LM.clip_velocity(c, now) for c in clips)
            out[slug] = {"name": slug, "best_vph": vph, "views": sum(c["view_count"] for c in clips), "clips": len(clips)}
    return out


def discover(known, videos=(), h=None, now=None, hours=24, verbose=True):
    """Find streamers going off right now who aren't in `known` (sources.py), save them, return the active set.
    `known` is SOURCES; `videos` is the latest trend scan's clip list (for the competition count)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    have_tw = {s["twitch"].lower() for s in known.values() if s.get("twitch")}
    have_kick = {s["kick"].lower() for s in known.values() if s.get("kick")}
    blocked = {w for s in known.values() if "blocked" in s.get("permission", "").lower()
               for w in [s.get("twitch", ""), s.get("kick", "")] + list(s.get("aliases", [])) if w}
    found = []
    try:
        tw = twitch_trending(h, since, now) if h else {}
        if verbose:
            print("  trending: Twitch-wide scan skipped: no TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET secrets (README)" if not h
                  else f"  trending: {len(tw)} Twitch streamers with clips in the last {hours}h")
    except Exception as e:  # Twitch trouble never stops the day
        print(f"  trending: Twitch scan failed ({type(e).__name__})")
        tw = {}
    for login, d in tw.items():
        if login.lower() not in have_tw:
            found.append(("twitch", login, d))
    try:
        kk = kick_trending(since, now)
        if verbose:
            print(f"  trending: {len(kk)} of {len(KICK_WATCH)} Kick watchlist channels with clips in the last {hours}h")
    except Exception as e:
        print(f"  trending: Kick scan failed ({type(e).__name__})")
        kk = {}
    for slug, d in kk.items():
        if slug.lower() not in have_kick:
            found.append(("kick", slug, d))

    entries = load()
    scored = []
    for site, login, d in found:
        if login.lower() in blocked or d["best_vph"] < MIN_CLIP_VPH:
            continue
        name = d["name"]
        words = {login.lower(), name.lower()} | set(re.findall(r"[a-z0-9]{4,}", name.lower()))
        n = crowd(words, videos)
        scored.append((d["best_vph"] * crowd_factor(n), site, login, d, n))
    scored.sort(reverse=True)
    if verbose and not scored:
        print(f"  trending: nobody new above {MIN_CLIP_VPH:,} views/hour on their best clip today")
    stamp = now.isoformat(timespec="seconds")
    for score, site, login, d, n in scored[:MAX_NEW]:
        key = f"{site}_{re.sub(r'[^a-z0-9_]', '', login.lower())}"
        e = entries.get(key) or {
            "name": d["name"], "short": d["name"], "niche": "streamer", site: login,
            "aliases": sorted({login.lower(), d["name"].lower()}),
            "hashtags": [f"#{re.sub(r'[^a-z0-9]', '', d['name'].lower())}"],
            "permission": "auto-added: trending on " + site.title() + " (clipping policy not checked)",
            "campaign_cpm": None, "first_seen": stamp,
        }
        # "crowd" is the same rank multiplier sources.py uses for saturated names (rank_moments applies it)
        e.update(last_seen=stamp, trending=True, crowd_channels=n, crowd=crowd_factor(n),
                 best_clip_vph=round(d["best_vph"]), clips_24h=d["clips"])
        entries[key] = e
        if verbose:
            print(f"  trending: {d['name']} ({site}) best clip {round(d['best_vph']):,} views/h, "
                  f"{n} channels already clipping them")
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")
    return active(entries, now)
