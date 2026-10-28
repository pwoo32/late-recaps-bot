"""Review agent: checks how your posted clips are doing and tells you how to get more views and money out of them.

For every Short on your channel it gathers:
- public stats:        views, likes, comments, views per hour, privacy (a clip stuck on private gets 0 views)
- YouTube Analytics:   average % viewed, how many viewers stayed past the first seconds (engaged views and the
                       retention curve), shares, subscribers gained, how much traffic came from the Shorts feed
- what we posted:      the builder's queue entry for it: creator, stream moment, title, hook, narrator line, length,
                       post time and the transcript

then diagnoses each clip (hook losing people, clip too long, weak title/topic, wrong time, still private), learns which
creators, lengths and title styles work on your channel, and tracks money: YouTube Partner Program progress, what the
views would be worth, which clips fit a paid clipping campaign, and affiliate/sponsor ideas that fit the channel.

It feeds what it learns back into the other agents through reports/builder/state.json:
- script_agent.py writes titles, hooks and narrator lines with the review's lessons in its prompt
- channel_builder.py daily ranks creators by how their clips held viewers on your channel and cuts to the length
  that held best

It only reads. It never uploads, edits, retitles or deletes a video; suggested new titles are for you to apply.

Usage:
    python review_agent.py auth          # once: sign in so it can read YouTube Analytics (opens a browser)
    python review_agent.py               # review every Short on the channel
    python review_agent.py --days 14     # only Shorts posted in the last 14 days
    python review_agent.py --no-llm      # rules only, skip the written review

Without `auth` it still runs on public stats (YOUTUBE_API_KEY, 2-3 quota units), but can't see retention, private
clips or subscribers gained. The written review uses the same writers as script_agent.py (Claude Code on this
PC, Ollama, or ANTHROPIC_API_KEY); without one you get the rule-based review only.
"""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from agent import REPORTS_DIR
from channel_agent import ChannelYouTube, video_record
from niches import NICHES
from sources import SOURCES
import script_agent

BUILDER_DIR = REPORTS_DIR / "builder"
STATE_FILE = BUILDER_DIR / "state.json"
LOCAL_TZ = ZoneInfo("America/New_York")
DEFAULT_CHANNEL = "@LateRecaps"
TOKEN_DIR = Path.home() / ".config" / "clip-builder"
REVIEW_TOKEN = TOKEN_DIR / "review_token.json"   # separate from the uploader's token so `publish` is never affected
UPLOAD_TOKEN = TOKEN_DIR / "youtube_token.json"  # channel_builder's; has youtube.readonly, so it can see private clips
REVIEW_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly",
                 "https://www.googleapis.com/auth/yt-analytics.readonly",
                 "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"]  # revenue, once monetized
SETTLED_H = 48  # a Short gets most of its views in the first 1-3 days

# YouTube Partner Program, Shorts route (Oct 2026). Views are public Shorts views in the last 90 days.
YPP_TIERS = [
    {"name": "Fan funding (memberships, Super Thanks, Shopping)", "subs": 500, "shorts_views": 3_000_000, "uploads": 3},
    {"name": "Full YPP (Shorts ad revenue share)", "subs": 1000, "shorts_views": 10_000_000, "uploads": 0},
]

# Ideas that fit a streamer-clips channel at any size. The written review adds ones specific to your clips.
MONEY_IDEAS = [
    ("Paid clipping campaigns", "Whop Content Rewards, Vyro, ClipAffiliates and creators' own programs pay $0.50-$3 per "
     "1,000 views for clips of their streamers, from day one and with no YPP needed. Join the ones that cover creators "
     "you already clip, then set campaign_cpm in sources.py so the builder favors them."),
    ("Link Shorts to long videos", "A Short can link to one of your own long videos (Studio > the Short > Related video). "
     "A weekly 8-10 minute 'best of the week' with real commentary earns long-form ad money (much higher RPM than "
     "Shorts) and counts watch hours toward YPP."),
    ("Affiliate links in the channel bio and pinned comments", "Links in Shorts descriptions aren't clickable. Put one "
     "link hub in the channel header and pin a comment on breakout clips (gaming chairs, headsets, the games streamed, "
     "Amazon Associates or a game-key store). Only worth it once clips pass ~50k views."),
    ("Sponsor shoutouts", "Once you post daily and pass ~10k subs, energy-drink, VPN, mobile-game and betting-free "
     "gaming brands pay clip pages $50-500 per pinned comment or end card. List the channel on creator marketplaces "
     "(YouTube BrandConnect once eligible, Passionfroot, Collabstr)."),
]


# ---------- shared state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"source_weight": {}, "slot_weight": {}, "used_moments": [], "queued": {}, "channel": None, "handles": {}}


def save_state(state):
    BUILDER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def latest(pattern):
    runs = sorted(REPORTS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


# ---------- signing in ----------

def google_libs():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return None
    return dict(RefreshError=RefreshError, Request=Request, Credentials=Credentials, Flow=InstalledAppFlow,
                build=build, HttpError=HttpError)


def credentials(g, interactive=False):
    """Saved sign-in, best first: the review token (analytics), then the uploader's (read-only channel data).
    Only `auth` opens a browser, so the scheduled run never hangs waiting for a sign-in."""
    if not interactive:
        for f in (REVIEW_TOKEN, UPLOAD_TOKEN):
            if not f.exists():
                continue
            creds = g["Credentials"].from_authorized_user_file(str(f))
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(g["Request"]())
                except g["RefreshError"]:
                    print(f"Saved sign-in {f.name} has expired. Run `python review_agent.py auth` again "
                          "(README: Review agent explains how to stop it expiring weekly).")
                    continue
                f.write_text(creds.to_json(), encoding="utf-8")
            if creds.valid:
                return creds
        return None
    secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", Path(__file__).parent / "client_secret.json"))
    if not secrets.exists():
        sys.exit(f"No OAuth client at {secrets}. It's the same client_secret.json the uploader uses (README: Uploading).")
    creds = g["Flow"].from_client_secrets_file(str(secrets), REVIEW_SCOPES).run_local_server(port=0)
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(REVIEW_TOKEN, 0o600)
    return creds


def has_analytics(creds):
    return bool(creds) and any("yt-analytics.readonly" in s for s in (creds.scopes or []))


def cmd_auth(args):
    g = google_libs()
    if not g:
        sys.exit("pip install google-api-python-client google-auth-oauthlib")
    creds = credentials(g, interactive=True)
    yt = g["build"]("youtube", "v3", credentials=creds, cache_discovery=False)
    me = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not me:
        sys.exit("Signed in, but this Google account has no YouTube channel. Sign in with the one that owns the channel.")
    print(f"Signed in. Reviewing: {me[0]['snippet']['title']}")
    try:
        yta = g["build"]("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        today = datetime.now(timezone.utc).date().isoformat()
        yta.reports().query(ids="channel==MINE", startDate=today, endDate=today, metrics="views").execute()
        print("YouTube Analytics works.")
    except g["HttpError"] as e:
        if "accessNotConfigured" in str(e) or "has not been used" in str(e) or "disabled" in str(e):
            print("Enable the YouTube Analytics API for this Google Cloud project, then rerun: "
                  "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com")
        else:
            print(f"YouTube Analytics check failed: {str(e)[:300]}")


# ---------- collecting ----------

def channel_and_uploads(args, state, g, creds):
    """(channel dict, [video items]) for your channel. Signed in sees private/scheduled clips; an API key sees public."""
    if creds:
        yt = g["build"]("youtube", "v3", credentials=creds, cache_discovery=False)
        ch = yt.channels().list(part="id,snippet,statistics,contentDetails", mine=True).execute()["items"][0]
        ids = []
        pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        resp = yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50).execute()
        ids = [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        ids += [q["video_id"] for q in state["queued"].values() if q.get("video_id") and q["video_id"] not in ids]
        items = []
        for i in range(0, len(ids), 50):
            items += yt.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(ids[i:i + 50])).execute().get("items", [])
        return ch, items, 0
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY, or run `python review_agent.py auth` once.")
    yt = ChannelYouTube(key)
    ref = args.channel or state.get("channel") or DEFAULT_CHANNEL
    by = {"id": ref} if ref.startswith("UC") else {"forHandle": ref}
    found = yt._get("channels", 1, part="id,snippet,statistics,contentDetails", **by).get("items", [])
    if not found:
        sys.exit(f"No YouTube channel for {ref}. Pass --channel @yourhandle.")
    ch = found[0]
    ids = yt.recent_uploads(ch["contentDetails"]["relatedPlaylists"]["uploads"], n=50)
    ids += [q["video_id"] for q in state["queued"].values() if q.get("video_id") and q["video_id"] not in ids]
    return ch, yt.videos(ids), yt.units


def queue_entries():
    """Every clip the builder queued that made it to YouTube, by video id, with the queue folder it lives in."""
    out = {}
    for qfile in sorted(BUILDER_DIR.glob("daily_*/queue.json")):
        try:
            for c in json.loads(qfile.read_text(encoding="utf-8")):
                if c.get("youtube_id"):
                    out[c["youtube_id"]] = (qfile.parent, c)
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def rows(resp):
    cols = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(cols, r)) for r in resp.get("rows", []) or []]


class Analytics:
    def __init__(self, g, creds):
        self.api = g["build"]("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        self.HttpError = g["HttpError"]
        self.notes = []

    def query(self, start, end, metrics, **kw):
        """Run a report; if a metric isn't available (engagedViews on older accounts), retry without it."""
        try:
            return rows(self.api.reports().query(ids="channel==MINE", startDate=start, endDate=end,
                                                 metrics=metrics, **kw).execute())
        except self.HttpError as e:
            if "engagedViews" in metrics:
                return self.query(start, end, metrics.replace("engagedViews,", "").replace(",engagedViews", ""), **kw)
            if "accessNotConfigured" in str(e) or "has not been used" in str(e):
                self.notes.append("Turn on the YouTube Analytics API in Google Cloud: "
                                  "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com")
                raise
            self.notes.append(f"Analytics query for {metrics} failed: {str(e)[:160]}")
            return []

    def per_video(self, ids, start, end):
        out = {}
        for i in range(0, len(ids), 200):
            for r in self.query(start, end, "views,engagedViews,estimatedMinutesWatched,averageViewDuration,"
                                "averageViewPercentage,likes,comments,shares,subscribersGained",
                                dimensions="video", filters="video==" + ",".join(ids[i:i + 200]),
                                sort="-views", maxResults=200):
                out[r["video"]] = r
        return out

    def retention(self, vid, start, end):
        return [(r["elapsedVideoTimeRatio"], r["audienceWatchRatio"]) for r in self.query(
            start, end, "audienceWatchRatio", dimensions="elapsedVideoTimeRatio", filters=f"video=={vid}")]

    def traffic(self, vid, start, end):
        return {r["insightTrafficSourceType"]: r["views"] for r in self.query(
            start, end, "views", dimensions="insightTrafficSourceType", filters=f"video=={vid}")}

    def shorts_90d(self):
        end = datetime.now(timezone.utc).date()
        got = self.query((end - timedelta(days=90)).isoformat(), end.isoformat(), "views,engagedViews",
                         filters="creatorContentType==SHORTS")
        return got[0] if got else None

    def revenue_28d(self):
        end = datetime.now(timezone.utc).date()
        try:
            got = rows(self.api.reports().query(ids="channel==MINE", startDate=(end - timedelta(days=28)).isoformat(),
                                                endDate=end.isoformat(), metrics="estimatedRevenue").execute())
        except self.HttpError:
            return None  # not monetized yet, or signed in without the revenue scope
        return got[0].get("estimatedRevenue") if got else None


def watch_at(curve, ratio):
    if not curve:
        return None
    return min(curve, key=lambda p: abs(p[0] - ratio))[1]


def collect(args, state):
    g = google_libs()
    creds = credentials(g) if g else None
    ch, items, units = channel_and_uploads(args, state, g, creds)
    now = datetime.now(timezone.utc)
    queued = queue_entries()
    vids = []
    for it in items:
        v = video_record(it, now)
        if v["seconds"] > 180:
            continue  # long videos aren't clips
        st = it.get("status", {})
        v["privacy"] = st.get("privacyStatus", "public")  # an API key only ever sees public videos
        v["publish_at"] = st.get("publishAt")
        v["age_h"] = v["age_days"] * 24
        if args.days and v["age_days"] > args.days:
            continue
        if v["id"] in queued:
            qdir, c = queued[v["id"]]
            v["clip"] = {k: c.get(k) for k in ("source", "creator", "video_title", "moment", "start", "end", "hook",
                                               "context", "take", "post_utc", "post_local", "quotes", "rank", "writer")}
            v["transcript"] = script_agent.cut_transcript(qdir, c)
        else:
            low = v["title"].lower()
            hits = {k: min(m.start() for a in s["aliases"] for m in [re.search(rf"\b{re.escape(a)}\b", low)] if m)
                    for k, s in SOURCES.items() if any(re.search(rf"\b{re.escape(a)}\b", low) for a in s["aliases"])}
            src = min(hits, key=hits.get) if hits else None  # the creator named first is who the clip is of
            v["clip"] = {"source": src, "creator": SOURCES[src]["name"] if src else None}
        vids.append(v)

    data = {"channel": {"id": ch["id"], "title": ch["snippet"]["title"],
                        "subs": int(ch["statistics"].get("subscriberCount", 0)),
                        "views": int(ch["statistics"].get("viewCount", 0)),
                        "videos": int(ch["statistics"].get("videoCount", 0))},
            "videos": vids, "analytics": False, "notes": [], "units": units, "signed_in": bool(creds)}
    if not creds:
        data["notes"].append("Not signed in: public stats only. Run `python review_agent.py auth` for retention and private clips.")
    elif not has_analytics(creds):
        data["notes"].append("Signed in with the uploader's login, which can't read YouTube Analytics. "
                             "Run `python review_agent.py auth` once for retention data.")
    if has_analytics(creds) and vids:
        an = Analytics(g, creds)
        start = min(v["published"] for v in vids).date().isoformat()
        end = now.date().isoformat()
        try:
            per = an.per_video([v["id"] for v in vids], start, end)
            for v in vids:
                a = per.get(v["id"])
                if a:
                    v["an"] = a
                if v["age_h"] >= 24:  # curves are empty for the first day
                    v["curve"] = an.retention(v["id"], start, end)
                    v["traffic"] = an.traffic(v["id"], start, end)
            data["shorts_90d"] = an.shorts_90d()
            data["revenue_28d"] = an.revenue_28d()
            data["analytics"] = True
        except an.HttpError:
            pass
        data["notes"] += an.notes
    return data


# ---------- judging ----------

def metrics(v):
    """Numbers the diagnosis uses. Analytics fields are None when not signed in or too fresh (data lags ~1-2 days)."""
    a = v.get("an") or {}
    views = max(v["views"], int(a.get("views") or 0))
    m = {"views": views, "vph": views / max(v["age_h"], 1),
         "like_rate": v["likes"] / views if views else None,
         "comment_rate": v["comments"] / views if views else None,
         "avp": a.get("averageViewPercentage"), "avd": a.get("averageViewDuration"),
         "shares": a.get("shares"), "subs": a.get("subscribersGained"),
         "stayed": (a["engagedViews"] / a["views"]) if a.get("engagedViews") is not None and a.get("views") else None,
         "hold_3s": None, "end": None, "feed_share": None}
    curve, secs = v.get("curve") or [], v["seconds"] or 30
    if curve:
        m["hold_3s"] = watch_at(curve, min(3 / secs, 1))
        m["end"] = watch_at(curve, 1.0)
    t = v.get("traffic") or {}
    if sum(t.values()):
        m["feed_share"] = t.get("SHORTS", 0) / sum(t.values())
    return m


def diagnose(v, m, base, bench):
    """Plain-language findings for one clip, worst first. Thresholds are rules of thumb for Shorts."""
    out = []
    if v["privacy"] != "public":
        if v.get("publish_at") and datetime.fromisoformat(v["publish_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc):
            out.append(("info", "Scheduled, not public yet."))
        else:
            out.append(("fix", f"Still {v['privacy']}: nobody can see it. Unaudited API projects lock uploads to private; "
                               "flip it to Public in YouTube Studio (and apply for the API audit, README: Uploading)."))
            return out
    if v["age_h"] < 24:
        out.append(("info", f"Only {v['age_h']:.0f}h old; judge it after {SETTLED_H}h."))
    if m["stayed"] is not None and m["stayed"] < 0.55:
        out.append(("hook", f"Only {m['stayed']:.0%} of plays turned into engaged views: people swipe in the first "
                            "second. Open on the loudest reaction, not the setup, and make the hook text promise the payoff."))
    elif m["hold_3s"] is not None and m["hold_3s"] < 0.65:
        out.append(("hook", f"{1 - m['hold_3s']:.0%} are gone by second 3. Start closer to the moment; cut the narrator "
                            "line shorter or put it over action instead of a still."))
    if m["avp"] is not None:
        if m["avp"] < 60:
            out.append(("length", f"Viewers watch {m['avp']:.0f}% on average: the clip runs long. Trim dead air and end "
                                  "right on the punchline."))
        elif m["avp"] >= 90:
            out.append(("good", f"{m['avp']:.0f}% average view: retention is strong"
                                + (", and it loops." if (m["end"] or 0) >= 0.9 else ".")))
    if m["feed_share"] is not None and v["age_h"] >= SETTLED_H and m["feed_share"] < 0.5:
        out.append(("reach", f"Only {m['feed_share']:.0%} of views came from the Shorts feed: YouTube isn't pushing it. "
                             "Usually the first seconds or the topic (is this creator hot this week?)."))
    if v["age_h"] >= SETTLED_H and base and m["views"] < 0.5 * base and not any(k == "hook" for k, _ in out):
        good_ret = m["avp"] is not None and m["avp"] >= 75
        out.append(("title" if good_ret else "pick",
                    f"{m['views'] / base:.1f}x your median."
                    + (" People who saw it watched it, so the title/topic or posting time held it back."
                       if good_ret else " Weak pick: the moment or the creator didn't land.")))
    if m["like_rate"] is not None and m["views"] >= 300:
        if m["like_rate"] >= 0.05:
            out.append(("good", f"{m['like_rate']:.1%} like rate: the payoff lands."))
        elif m["like_rate"] < 0.015:
            out.append(("pick", f"{m['like_rate']:.1%} like rate: the payoff is weak or the clip ends before it."))
    if bench and m["views"] >= bench:
        out.append(("good", f"Beat the niche benchmark ({bench:,} views for young winning channels)."))
    if (m["subs"] or 0) >= 5 and m["views"] and m["subs"] / m["views"] * 1000 >= 2:
        out.append(("good", f"{m['subs']} subscribers ({m['subs'] / m['views'] * 1000:.1f} per 1k views): make more like this."))
    return out


def shrink(x, n, k=2):
    """Lean a ratio toward 1 when it rests on few clips."""
    return 1 + (x - 1) * n / (n + k)


def learn(vids, ms, base):
    """What works on this channel: creators, length, title style. Only clips at least a day old count."""
    settled = [(v, m) for v, m in zip(vids, ms) if v["age_h"] >= 24 and v["privacy"] == "public"]
    out = {"source_score": {}, "length_hint": None, "title_style": [], "settled": len(settled)}
    if not settled or not base:
        return out

    def score(v, m):  # views relative to the median, nudged by how well it held viewers
        held = (m["avp"] / 80) if m["avp"] is not None else 1
        return (m["views"] / base) * min(max(held, 0.5), 1.5)

    # creator weights use retention only: `channel_builder.py track` already weights creators by views
    by_src = defaultdict(list)
    for v, m in settled:
        if v["clip"].get("source") and m["avp"] is not None:
            by_src[v["clip"]["source"]].append(min(max(m["avp"] / 80, 0.5), 1.5))
    for s, xs in by_src.items():
        out["source_score"][s] = round(min(max(shrink(statistics.median(xs), len(xs)), 0.5), 2.0), 2)

    if len(settled) >= 6:
        buckets = defaultdict(list)
        for v, m in settled:
            buckets[min(v["seconds"] // 15, 3)].append(score(v, m))
        good = {b: statistics.median(xs) for b, xs in buckets.items() if len(xs) >= 2}
        if len(good) >= 2:
            b = max(good, key=good.get)
            out["length_hint"] = int(b * 15 + 7)

        ranked = sorted(settled, key=lambda vm: -score(*vm))
        top, bottom = ranked[:len(ranked) // 2], ranked[len(ranked) // 2:]
        avg = lambda xs: sum(xs) / max(len(xs), 1)
        chars = avg([len(v["title"]) for v, _ in top]), avg([len(v["title"]) for v, _ in bottom])
        if abs(chars[0] - chars[1]) >= 8:
            out["title_style"].append(f"Your better clips have {'shorter' if chars[0] < chars[1] else 'longer'} titles "
                                      f"(~{chars[0]:.0f} vs ~{chars[1]:.0f} characters).")
    return out


def money(data, vids, ms, niche):
    ch = data["channel"]
    now = datetime.now(timezone.utc)
    recent = [(v, m) for v, m in zip(vids, ms) if v["age_days"] <= 90 and v["privacy"] == "public"]
    s90 = data.get("shorts_90d") or {}
    views90 = int(s90.get("engagedViews") or s90.get("views") or sum(m["views"] for _, m in recent))
    first = min((v["published"] for v, _ in recent), default=now)
    pace = views90 / max((now - first).total_seconds() / 86400, 1)
    tiers = []
    for t in YPP_TIERS:
        left = max(t["shorts_views"] - views90, 0)
        tiers.append({**t, "have_subs": ch["subs"], "have_views": views90, "have_uploads": len(recent),
                      "views_pct": min(views90 / t["shorts_views"], 1), "subs_pct": min(ch["subs"] / t["subs"], 1),
                      "days_at_pace": round(left / pace) if pace and left else (0 if not left else None)})
    total = sum(m["views"] for m in ms)
    rpm = NICHES.get(niche, {}).get("shorts_rpm", 0.03)
    campaign = []
    for v, m in zip(vids, ms):
        src = SOURCES.get(v["clip"].get("source") or "", {})
        if src.get("campaign_cpm"):
            campaign.append((v, m, src, m["views"] * src["campaign_cpm"] / 1000))
    paid_sources = [s["name"] for s in SOURCES.values() if s.get("campaign_cpm") and s.get("niche") == niche]
    return {"views90": views90, "pace": pace, "tiers": tiers, "total_views": total,
            "ad_value": total * rpm / 1000, "rpm": rpm, "campaign": campaign, "paid_sources": paid_sources,
            "niche_campaign_cpm": NICHES.get(niche, {}).get("campaign_cpm"),
            "revenue_28d": data.get("revenue_28d")}


# ---------- the written review ----------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {"type": "array", "items": {"type": "object", "properties": {
            "video_id": {"type": "string"}, "verdict": {"type": "string"}, "worked": {"type": "string"},
            "hurt": {"type": "string"}, "better_title": {"type": "string"}, "better_hook": {"type": "string"}},
            "required": ["video_id", "verdict", "worked", "hurt", "better_title", "better_hook"]}},
        "lessons": {"type": "object", "properties": {
            "titles": {"type": "array", "items": {"type": "string"}},
            "hooks": {"type": "array", "items": {"type": "string"}},
            "clip_picks": {"type": "array", "items": {"type": "string"}},
            "length_and_pacing": {"type": "array", "items": {"type": "string"}},
            "posting": {"type": "array", "items": {"type": "string"}}},
            "required": ["titles", "hooks", "clip_picks", "length_and_pacing", "posting"]},
        "money": {"type": "array", "items": {"type": "object", "properties": {
            "idea": {"type": "string"}, "why": {"type": "string"}, "first_step": {"type": "string"}},
            "required": ["idea", "why", "first_step"]}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["clips", "lessons", "money", "next_actions"],
}


def fmt(x, spec, none="n/a"):
    return none if x is None else format(x, spec)


def review_prompt(data, vids, ms, diags, learned, cash, pb, niche, previous):
    blocks = []
    for v, m, d in zip(vids, ms, diags):
        c = v["clip"]
        b = [f"[{v['id']}] \"{v['title']}\" ({v['seconds']}s, posted {v['published']:%a %H:%M} UTC, {v['age_h']:.0f}h ago, {v['privacy']})",
             f"    Creator: {c.get('creator') or '?'}" + (f"; source stream \"{c['video_title']}\" at {c['moment']}" if c.get("video_title") else ""),
             f"    Views {m['views']:,} ({m['vph']:,.0f}/h), likes {v['likes']}, comments {v['comments']}, "
             f"avg viewed {fmt(m['avp'], '.0f')}%, plays that became engaged views {fmt(m['stayed'], '.0%')}, "
             f"still watching at 3s {fmt(m['hold_3s'], '.0%')}, at the end {fmt(m['end'], '.0%')}, "
             f"from Shorts feed {fmt(m['feed_share'], '.0%')}, shares {fmt(m['shares'], '.0f')}, subs gained {fmt(m['subs'], '.0f')}"]
        if c.get("hook"):
            b.append(f"    On-screen hook: \"{c['hook']}\"; context line: \"{c.get('context')}\"; narrator said: \"{c.get('take')}\"")
        if v.get("transcript"):
            b.append(f"    Transcript: \"{v['transcript'][:900]}\"")
        if d:
            b.append("    Rule checks: " + " | ".join(t for _, t in d))
        blocks.append("\n".join(b))
    winners = "\n".join(f"- {w['title']} ({w['views']:,} views, {w['seconds']}s)" for w in (pb or {}).get("winners", [])[:10])
    prev = "\n".join(f"- {x}" for xs in (previous or {}).values() for x in xs)
    tiers = "; ".join(f"{t['name']}: {t['have_subs']}/{t['subs']} subs, {t['have_views']:,}/{t['shorts_views']:,} Shorts views"
                      for t in cash["tiers"])
    return f"""You review a young YouTube Shorts channel, "{data['channel']['title']}", that posts {NICHES[niche]['label'].lower()}:
20-60 second cuts of streamers' live moments with an AI narrator line over the opening and a pinned hook text.
It has {data['channel']['subs']:,} subscribers. Be a sharp, honest Shorts strategist: specific to these clips, no
generic advice. Clips under {SETTLED_H}h old or without analytics are early, so say what to watch rather than judging.

Its clips (rule-based checks already ran; build on them, don't repeat them):

""" + "\n\n".join(blocks) + f"""

Breakout Shorts from the top channels in this niche, for comparison:
{winners or '- (no channel study yet)'}

Lessons from earlier reviews (keep the ones still true, drop or fix the rest):
{prev or '- (first review)'}

Money: YPP progress {tiers}. {data['channel']['subs']} subs. Paid clipping campaigns known for: {', '.join(cash['paid_sources']) or 'none yet'}.

Write:
- clips: one entry per clip id above. verdict (one line), worked, hurt, better_title (the creator's name first, what
  happens in plain punchy words, under 60 characters, 1-2 emoji at the end; only facts the transcript/hook support),
  better_hook (max 6 words, on-screen text that makes a scroller wait for the payoff).
- lessons: short rules the script writer and clip picker should follow from now on, grounded in this data. 1-4 each.
- money: 2-4 ideas to earn from this channel now and as it grows (campaigns, affiliate, sponsors, long-form, merch),
  each with why it fits these clips and the concrete first step. No made-up programs or numbers.
- next_actions: the 3-5 most valuable things to do this week, most important first.

Return only JSON matching: {{"clips": [{{"video_id", "verdict", "worked", "hurt", "better_title", "better_hook"}}],
"lessons": {{"titles": [], "hooks": [], "clip_picks": [], "length_and_pacing": [], "posting": []}},
"money": [{{"idea", "why", "first_step"}}], "next_actions": []}}"""


def _claude_code(prompt):
    exe = shutil.which("claude")
    if not exe:
        return None
    r = subprocess.run([exe, "-p", "--output-format", "json", "--json-schema", json.dumps(REVIEW_SCHEMA), "--tools", ""],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or r.stdout.strip()[:300])
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    return out.get("structured_output") or script_agent.parse_json(out["result"])


def _ollama(prompt):
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        tags = requests.get(f"{host}/api/tags", timeout=3).json().get("models", [])
    except requests.RequestException:
        return None
    if not tags:
        return None
    model = os.environ.get("OLLAMA_MODEL") or tags[0]["name"]
    r = requests.post(f"{host}/api/generate", timeout=900, json={
        "model": model, "prompt": prompt, "format": REVIEW_SCHEMA, "stream": False, "options": {"temperature": 0.4}})
    r.raise_for_status()
    return script_agent.parse_json(r.json()["response"])


def _api(prompt):
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=16000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    if resp.stop_reason == "refusal":
        return None
    return script_agent.parse_json("".join(b.text for b in resp.content if b.type == "text"))


WRITERS = {"claude-code": _claude_code, "ollama": _ollama, "api": _api}


def written_review(prompt, writer=None):
    order = [writer] if writer in WRITERS else list(WRITERS)
    for name in order:
        try:
            out = WRITERS[name](prompt)
        except Exception as e:  # never let the writer stop the numbers from being saved
            print(f"Reviewer {name} failed ({type(e).__name__}: {str(e)[:200]}).")
            continue
        if out and isinstance(out, dict) and out.get("clips") is not None:
            print(f"Written review by {name}.")
            return out, name
    print("No reviewer available: rule-based review only (see the top of script_agent.py for writer options).")
    return None, None


# ---------- report ----------

KIND_LABEL = {"fix": "FIX", "hook": "Hook", "length": "Length", "reach": "Reach", "title": "Title/topic",
              "pick": "Clip pick", "good": "Working", "info": "Note"}


def write_report(out_dir, data, vids, ms, diags, learned, cash, llm, writer, base, bench, niche):
    now = datetime.now(timezone.utc)
    ch = data["channel"]
    by_id = {c["video_id"]: c for c in (llm or {}).get("clips", [])}
    L = [f"# Clip review, {ch['title']}, {now.astimezone(LOCAL_TZ):%a %b %d %I:%M %p} ET", "",
         f"{len(vids)} Shorts reviewed, {ch['subs']:,} subscribers, {cash['total_views']:,} views on these clips"
         + (f", median {base:,.0f} for clips a day or older." if base else ".")
         + (" With YouTube Analytics." if data["analytics"] else " Public stats only."), ""]
    for n in data["notes"]:
        L.append(f"> {n}")
    if data["notes"]:
        L.append("")

    fixes = [(v, t) for v, d in zip(vids, diags) for k, t in d if k == "fix"]
    actions = (llm or {}).get("next_actions") or []
    if fixes or actions:
        L += ["## Do this week", ""]
        L += [f"- **{v['title']}**: {t}" for v, t in fixes]
        L += [f"- {a}" for a in actions]
        L.append("")

    L += ["## Clips", "", "| Clip | Age | Views | Views/hr | Avg viewed | Stayed past swipe | Likes | Subs | vs median |",
          "|---|---|---|---|---|---|---|---|---|"]
    for v, m in sorted(zip(vids, ms), key=lambda vm: vm[0]["published"], reverse=True):
        L.append(f"| [{v['title']}](https://www.youtube.com/shorts/{v['id']}) | {v['age_h']:.0f}h | {m['views']:,} | "
                 f"{m['vph']:,.0f} | {fmt(m['avp'], '.0f')}{'%' if m['avp'] is not None else ''} | {fmt(m['stayed'], '.0%')} | "
                 f"{fmt(m['like_rate'], '.1%')} | {fmt(m['subs'], '.0f', '-')} | "
                 + (v["privacy"] if v["privacy"] != "public" else f"{m['views'] / base:.1f}x" if base and v["age_h"] >= 24
                    else "early") + " |")
    L.append("")

    for v, m, d in sorted(zip(vids, ms, diags), key=lambda x: x[0]["published"], reverse=True):
        c, r = v["clip"], by_id.get(v["id"], {})
        L += [f"### {v['title']}", ""]
        meta = [c.get("creator") or "creator unknown", f"{v['seconds']}s", f"posted {v['published'].astimezone(LOCAL_TZ):%a %I:%M %p} ET"]
        if c.get("moment"):
            meta.append(f"from \"{c['video_title']}\" at {c['moment']}")
        L += ["- " + ", ".join(meta)]
        if c.get("hook"):
            L.append(f"- Hook: \"{c['hook']}\" / narrator: \"{c.get('take')}\"")
        if m["hold_3s"] is not None:
            L.append(f"- Retention: {m['hold_3s']:.0%} still watching at 3s, {fmt(m['end'], '.0%')} at the end"
                     + (f", {m['feed_share']:.0%} of views from the Shorts feed" if m["feed_share"] is not None else ""))
        for k, t in d:
            L.append(f"- **{KIND_LABEL[k]}:** {t}")
        if r:
            L.append(f"- **Review:** {r['verdict']}")
            if r.get("worked"):
                L.append(f"  - Worked: {r['worked']}")
            if r.get("hurt"):
                L.append(f"  - Hurt: {r['hurt']}")
            L.append(f"  - Try instead: \"{r['better_title']}\", hook \"{r['better_hook']}\" (not applied; change it in Studio if you like it)")
        L.append("")

    L += ["## What the other agents learn from this", ""]
    lessons = (llm or {}).get("lessons") or {}
    for k, label in [("titles", "Titles"), ("hooks", "Hooks"), ("clip_picks", "Clip picks"),
                     ("length_and_pacing", "Length and pacing"), ("posting", "Posting")]:
        for x in lessons.get(k, []):
            L.append(f"- {label}: {x}")
    for x in learned["title_style"]:
        L.append(f"- Titles: {x}")
    for s, w in sorted(learned["source_score"].items(), key=lambda kv: -kv[1]):
        L.append(f"- {SOURCES.get(s, {}).get('name', s)}: clip-pick weight {w} from how well their clips held viewers (1.0 = neutral)")
    if learned["length_hint"]:
        L.append(f"- Cut length: aim for ~{learned['length_hint']}s, the length that held viewers best on this channel")
    if learned["settled"] < 3:
        L.append("- No clip is a day old yet, so the weights didn't change." if not learned["settled"] else
                 f"- Only {learned['settled']} clip{'s are' if learned['settled'] > 1 else ' is'} a day old and public, "
                 "so the weights barely move yet.")
    L += ["", "script_agent.py puts these lessons in its prompt; channel_builder.py daily uses the weights and length.", ""]

    L += ["## Money", ""]
    if cash["revenue_28d"] is not None:
        L.append(f"- YouTube revenue, last 28 days: ${cash['revenue_28d']:,.2f}")
    for t in cash["tiers"]:
        eta = ("views done" if t["days_at_pace"] == 0 else
               "too early for a pace" if cash["views90"] < 1000 or t["days_at_pace"] is None or t["days_at_pace"] > 3650
               else f"about {t['days_at_pace']} days at the current pace")
        L.append(f"- **{t['name']}**: {t['have_subs']:,}/{t['subs']:,} subs ({t['subs_pct']:.0%}), "
                 f"{t['have_views']:,}/{t['shorts_views']:,} Shorts views in 90 days ({t['views_pct']:.1%}; {eta})"
                 + (f", {t['have_uploads']}/{t['uploads']} uploads" if t["uploads"] else ""))
    L.append(f"- These clips' {cash['total_views']:,} views would be worth about ${cash['ad_value']:.2f} in Shorts ads once "
             f"monetized (${cash['rpm']:.2f} per 1k in this niche). Paid clipping campaigns pay far more per view"
             + (f" (~${cash['niche_campaign_cpm']:.2f} per 1k here)." if cash["niche_campaign_cpm"] else "."))
    for v, m, src, usd in cash["campaign"]:
        L.append(f"- {v['title']}: {src['name']} has a paid program (${src['campaign_cpm']}/1k): submit it for about ${usd:.2f}.")
    if not cash["campaign"]:
        L.append("- None of your clips are of a creator with a known paid program yet"
                 + (f"; ones that have one: {', '.join(cash['paid_sources'])}." if cash["paid_sources"] else
                    ". Find programs for the creators you clip and set campaign_cpm in sources.py."))
    L.append("")
    for x in (llm or {}).get("money") or []:
        L.append(f"- **{x['idea']}**: {x['why']} First step: {x['first_step']}")
    for name, text in MONEY_IDEAS:
        L.append(f"- **{name}**: {text}")
    L += ["", f"_Review by {writer or 'rules only'}. Quota: "
          + ("a few units of your signed-in quota" if data["signed_in"] else f"{data['units']} Data API units") + ". "
          "This agent never changes your videos._", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# ---------- main ----------

def cmd_review(args):
    state = load_state()
    data = collect(args, state)
    vids = data["videos"]
    niche = args.niche
    pb_file = latest(f"builder/playbook_channels_{niche}_*.json")
    pb = json.loads(pb_file.read_text(encoding="utf-8")) if pb_file else None
    bench = (pb or {}).get("benchmark_median_views")
    ms = [metrics(v) for v in vids]
    settled = [m["views"] for v, m in zip(vids, ms) if v["age_h"] >= 24 and v["privacy"] == "public"]
    base = statistics.median(settled) if settled else None
    diags = [diagnose(v, m, base, bench) for v, m in zip(vids, ms)]
    learned = learn(vids, ms, base)
    cash = money(data, vids, ms, niche)

    llm, writer = (None, None)
    if vids and not args.no_llm:
        prompt = review_prompt(data, vids, ms, diags, learned, cash, pb, niche, (state.get("review") or {}).get("lessons"))
        llm, writer = written_review(prompt, args.writer or os.environ.get("SCRIPT_WRITER"))

    now = datetime.now(timezone.utc)
    out_dir = REPORTS_DIR / f"review_{now:%Y-%m-%d_%H%M}"
    write_report(out_dir, data, vids, ms, diags, learned, cash, llm, writer, base, bench, niche)
    snapshot = [{"id": v["id"], "title": v["title"], "age_h": round(v["age_h"], 1), "privacy": v["privacy"],
                 "seconds": v["seconds"], "source": v["clip"].get("source"), **{k: m[k] for k in m},
                 "findings": [{"kind": k, "text": t} for k, t in d]} for v, m, d in zip(vids, ms, diags)]
    (out_dir / "review.json").write_text(json.dumps({"channel": data["channel"], "clips": snapshot, "learned": learned,
                                                     "written": llm, "writer": writer}, indent=1, ensure_ascii=False),
                                         encoding="utf-8")

    # feed the other agents
    state = load_state()  # re-read: another agent may have written while we waited on the reviewer
    prev = state.get("review") or {}
    lessons = (llm or {}).get("lessons") or prev.get("lessons") or {}
    if learned["title_style"]:
        lessons = {**lessons, "titles": list(dict.fromkeys((lessons.get("titles") or []) + learned["title_style"]))[:5]}
    state["review"] = {
        "at": now.strftime("%Y-%m-%d %H:%M"), "report": str(out_dir / "report.md"), "lessons": lessons,
        "source_score": learned["source_score"] or prev.get("source_score", {}),
        "length_hint": learned["length_hint"] or prev.get("length_hint"),
        "clips": [{"title": s["title"], "views": s["views"], "avg_viewed": s["avp"], "stayed": s["stayed"],
                   "verdict": next((c["verdict"] for c in (llm or {}).get("clips", []) if c["video_id"] == s["id"]), None)}
                  for s in snapshot if s["age_h"] >= 24 and s["privacy"] == "public"][:20],
    }
    if not state.get("channel"):
        state["channel"] = data["channel"]["id"]  # lets `channel_builder.py run` start its daily `track` too
    save_state(state)
    print(f"Done. Report: {out_dir / 'report.md'}" + ("" if data["signed_in"] else f"  (quota used: {data['units']} units)"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("auth", help="sign in once so the review can read YouTube Analytics")
    ap.add_argument("--channel", help=f"@handle or UC... id when not signed in (default: builder's channel or {DEFAULT_CHANNEL})")
    ap.add_argument("--niche", default="streamer", choices=sorted(NICHES))
    ap.add_argument("--days", type=int, help="only Shorts posted in the last N days")
    ap.add_argument("--no-llm", action="store_true", help="skip the written review")
    ap.add_argument("--writer", choices=list(WRITERS), help="who writes the review (default: first available)")
    args = ap.parse_args()
    cmd_auth(args) if args.cmd == "auth" else cmd_review(args)


if __name__ == "__main__":
    main()
