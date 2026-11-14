"""Review agent: finds out what makes our clips spread, and steers tomorrow's clips toward it.

It answers the questions that change what we post next:
- is the Shorts feed testing our clips at all (a clip it never shows can't go viral, whatever its content)
- which creators, moment types, hook styles, title styles, lengths, posting times and stream freshness get clips
  pushed, compared with our typical clip, with how many clips each finding rests on
- the first seconds: how many viewers stay past the swipe, and the second where most of them leave
- what to make more of and less of, in plain words

Sources: public stats, YouTube Analytics (engaged views, retention curve, traffic from the Shorts feed) and the
builder's queue entry for each clip (creator, hook, title, narrator line, length, post time, how old the stream was,
which signal found the moment). TikTok numbers come from `reports/tiktok_stats.csv`, a sheet this agent keeps with a
row per clip sent to TikTok. With `tiktok-auth --stats` it reads the views from TikTok itself; otherwise fill in
the numbers from TikTok Studio and the next review uses them.

It feeds the findings back through reports/builder/state.json ("review"):
- channel_builder.py daily ranks creators and posting hours by how their clips spread, and cuts to the length
  that spread best
- script_agent.py writes titles, hooks and narrator lines with the make-more / make-less findings in its prompt

It only reads. It never uploads, edits, retitles or deletes a video; suggested new titles are for you to apply.

Usage:
    python review_agent.py auth          # once: sign in so it can read YouTube Analytics (opens a browser)
    python review_agent.py               # review every Short on the channel
    python review_agent.py --days 14     # only Shorts posted in the last 14 days
    python review_agent.py --no-llm      # rules only, skip the written review

Without `auth` it still runs on public stats (YOUTUBE_API_KEY, 2-3 quota units), but can't see retention or
private clips. The written review uses a free writer (Gemini, Groq or a local Ollama model) when one is available;
without one you get the rule-based review only, which is what the daily cloud run uses.
"""

import argparse
import json
import math
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
import council
import originality
import tuner

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

TESTED_VIEWS = 10  # fewer views than this after 2 days: the Shorts feed never really showed the clip
MIN_GROUP = 3      # a finding needs at least this many settled clips behind it
TIKTOK_STATS = REPORTS_DIR / "tiktok_stats.csv"


# ---------- shared state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"source_weight": {}, "slot_weight": {}, "used_moments": [], "queued": {}, "channel": None, "handles": {}}


def save_state(state):
    BUILDER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def latest(pattern):
    runs = sorted(REPORTS_DIR.glob(pattern), key=lambda p: (p.stat().st_mtime, str(p)))  # a fresh checkout gives every file the same mtime: the dated name breaks ties
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
            # utf-8-sig: a token pasted through PowerShell or a secret can start with a byte-order mark
            creds = g["Credentials"].from_authorized_user_info(json.loads(f.read_text(encoding="utf-8-sig")))
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
    for qfile in sorted(BUILDER_DIR.glob("*_20*/queue.json")):
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
        v["upload_status"], v["rejection"], v["failure"] = st.get("uploadStatus"), st.get("rejectionReason"), st.get("failureReason")
        rr = it.get("contentDetails", {}).get("regionRestriction", {})
        v["blocked_in"] = len(rr.get("blocked", [])) or (249 - len(rr["allowed"]) if "allowed" in rr else 0)
        v["age_h"] = v["age_days"] * 24
        if args.days and v["age_days"] > args.days:
            continue
        if v["id"] in queued:
            qdir, c = queued[v["id"]]
            v["clip"] = {k: c.get(k) for k in ("source", "creator", "video_id", "video_title", "moment", "start", "end", "hook",
                                               "context", "take", "post_utc", "post_local", "quotes", "rank", "writer", "recipe",
                                               "signals", "spike", "council", "signal", "stream_age_h", "platform",
                                               "draft", "script_source")}
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
                if v["age_h"] >= 24 and max(v["views"], int((a or {}).get("views") or 0)):  # no curve on day 1 or at 0 views
                    v["curve"] = an.retention(v["id"], start, end)
                    v["traffic"] = an.traffic(v["id"], start, end)
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
         "avp": a.get("averageViewPercentage"), "avd": a.get("averageViewDuration"),
         "shares": a.get("shares"),
         "stayed": (a["engagedViews"] / a["views"]) if a.get("engagedViews") is not None and a.get("views") else None,
         "hold_3s": None, "end": None, "feed_share": None, "drop_s": None}
    curve, secs = v.get("curve") or [], v["seconds"] or 30
    if curve:
        m["hold_3s"] = watch_at(curve, min(3 / secs, 1))
        m["end"] = watch_at(curve, 1.0)
        m["drop_s"] = drop_second(curve, secs)
    t = v.get("traffic") or {}
    if sum(t.values()):
        m["feed_share"] = t.get("SHORTS", 0) / sum(t.values())
    return m


def drop_second(curve, secs):
    """The second where the biggest share of viewers leaves (the steepest fall in the retention curve)."""
    pts = sorted(curve)
    if len(pts) < 3:
        return None
    falls = [(pts[i][1] - pts[i + 1][1], pts[i + 1][0]) for i in range(len(pts) - 1)]
    fall, at = max(falls)
    return round(at * secs, 1) if fall > 0.05 else None


def settled_clips(vids, ms):
    """Public clips old enough to judge: a Short gets most of its feed test in the first two days."""
    return [(v, m) for v, m in zip(vids, ms) if v["privacy"] == "public" and v["age_h"] >= SETTLED_H]


def typical_views(settled):
    """The channel's typical clip: the geometric mean of views, so a run of 0s and one 45 doesn't read as a 45x hit."""
    if not settled:
        return None
    return math.expm1(statistics.fmean(math.log1p(m["views"]) for _, m in settled))


def lift(m, typical):
    """How a clip did against the typical clip, on a log scale (0 = typical, +0.69 = about twice as many views)."""
    return math.log1p(m["views"]) - math.log1p(typical or 0)


# ----- what each clip was, in the terms we can change -----

GENERIC_HOOK = re.compile(r"^(wait for (it|the end)|watch (this|till the end|his face)|this went wrong fast|"
                          r"he didn'?t think this through|no way|bro what|you won'?t believe)\b", re.I)
STAKE = re.compile(r"\$|\d|\b(if|bet|lose|loses|win|wins|or|until|first|last|challenge|vs|against)\b", re.I)
QUESTION = re.compile(r"\?\s*$|^(who|what|why|how|can|is|does|did|will|would|should)\b", re.I)
QUOTE_TITLE = re.compile(r"^[^:]{2,30}:\s*[\"“]")


def hook_style(hook):
    h = (hook or "").strip()
    if not h:
        return "no hook text"
    if GENERIC_HOOK.search(h):
        return "generic tease (WAIT FOR IT...)"
    if QUESTION.search(h):
        return "question"
    if STAKE.search(h):
        return "stake or challenge"
    return "says what happens"


def title_style(title):
    if originality.STOCK.search(title or ""):
        return "stock reaction (Goes Crazy, Can't Believe...)"
    if QUOTE_TITLE.search(title or ""):
        return "quote title (Creator: \"line\")"
    return "says what happens"


def length_bucket(secs):
    return "under 20s" if secs < 20 else "20-30s" if secs < 31 else "31-45s" if secs < 46 else "over 45s"


def time_block(published):
    h = published.astimezone(LOCAL_TZ).hour
    return ("morning (6am-noon ET)" if 6 <= h < 12 else "afternoon (noon-5pm ET)" if 12 <= h < 17
            else "evening (5-10pm ET)" if 17 <= h < 22 else "late night (10pm-6am ET)")


def freshness(age_h):
    if age_h is None:
        return None
    return "under 12h" if age_h < 12 else "12-48h" if age_h < 48 else "over 48h"


def found_by(c):
    """Which moment finder picked the clip, from the queue's signal line ("chat went 5x...", "Twitch clip, ...")."""
    sig = (c.get("signal") or "").lower()
    if c.get("platform") == "kick_clip" or "kick clip" in sig:
        return "Kick clip"
    if c.get("platform") == "twitch_clip" or "twitch clip" in sig:
        return "Twitch clip"
    if c.get("spike") or "chat went" in sig:
        return "chat spike"
    if "replayed" in sig or "heat" in sig:
        return "most-replayed"
    if "timestamp" in sig:
        return "viewer timestamp comments"
    return None


def features(v):
    """The levers behind one clip: who, how it opens, how it's titled, how long, when, how fresh, how it was found."""
    c = v.get("clip") or {}
    f = {"creator": c.get("creator"),
         "title": title_style(v["title"]),
         "length": length_bucket(v["seconds"] or 0),
         "post time": time_block(v["published"])}
    if c.get("hook") is not None or c.get("take") is not None:  # one of our builder's clips
        f["hook"] = hook_style(c.get("hook"))
    if c.get("stream_age_h") is not None:
        f["stream age at post"] = freshness(c["stream_age_h"])
    f["found by"] = found_by(c)
    return {k: x for k, x in f.items() if x}


FACTOR_ORDER = ["creator", "found by", "stream age at post", "hook", "title", "length", "post time"]


def factor_findings(settled, typical, platform="YouTube"):
    """Per lever and value: how many clips, how many the feed tested, typical views, retention, and the lift over the
    channel's typical clip (shrunk toward 'no effect' when it rests on few clips)."""
    groups = defaultdict(list)
    for v, m in settled:
        for k, x in features(v).items():
            groups[(k, x)].append((v, m))
    out = []
    for (k, x), vm in groups.items():
        n = len(vm)
        lifts = [lift(m, typical) for _, m in vm]
        shrunk = sum(lifts) / (n + MIN_GROUP)  # 3 clips at 2x read as ~1.4x; 10 clips at 2x read as ~1.7x
        stayed = [m["stayed"] for _, m in vm if m["stayed"] is not None]
        out.append({"factor": k, "value": x, "n": n, "platform": platform,
                    "tested": sum(1 for _, m in vm if m["views"] >= TESTED_VIEWS),
                    "views": math.expm1(statistics.fmean(math.log1p(m["views"]) for _, m in vm)),
                    "stayed": statistics.fmean(stayed) if stayed else None, "stayed_n": len(stayed),
                    "mult": math.exp(shrunk), "best": max(vm, key=lambda p: p[1]["views"])[0]["title"]})
    return sorted(out, key=lambda r: (FACTOR_ORDER.index(r["factor"]) if r["factor"] in FACTOR_ORDER else 99, -r["mult"]))


def finding_text(r):
    yt = r["platform"] == "YouTube"
    s = (f"{r['value']} ({r['factor']}): {r['n']} clips, " + (f"{r['tested']} got a real feed test, " if yt else "")
         + f"typically {r['views']:.0f} views, {r['mult']:.1f}x our typical {r['platform'] if not yt else ''} clip".replace("  ", " "))
    if r["stayed"] is not None:
        s += f", {r['stayed']:.0%} " + ("stayed past the swipe" if yt else "watched to the end")
    return s + ("" if yt else f" (on {r['platform']})")


def make_more_less(findings):
    """The findings strong enough to act on: enough clips and a clear gap from typical."""
    usable = [r for r in findings if r["n"] >= MIN_GROUP]
    more = sorted((r for r in usable if r["mult"] >= 1.25), key=lambda r: -r["mult"])
    less = sorted((r for r in usable if r["mult"] <= 0.8), key=lambda r: r["mult"])
    return more, less


def lever_scores(findings, factor):
    """{value: multiplier} for one lever, only values with enough clips."""
    return {r["value"]: round(min(max(r["mult"], 0.5), 2.0), 2) for r in findings
            if r["factor"] == factor and r["n"] >= MIN_GROUP}


def creator_scores(settled, typical):
    """Per sources.py key: how that creator's clips spread on our channel, shrunk toward 1.0 with few clips."""
    by = defaultdict(list)
    for v, m in settled:
        if (v.get("clip") or {}).get("source"):
            by[v["clip"]["source"]].append(lift(m, typical))
    return {s: round(min(max(math.exp(sum(xs) / (len(xs) + MIN_GROUP)), 0.5), 2.0), 2) for s, xs in by.items()}


def hour_scores(settled, typical):
    """Per UTC posting hour: the same, so the builder posts when our clips get tested."""
    by = defaultdict(list)
    for v, m in settled:
        by[v["published"].astimezone(timezone.utc).hour].append(lift(m, typical))
    return {str(h): round(min(max(math.exp(sum(xs) / (len(xs) + MIN_GROUP)), 0.5), 2.0), 2)
            for h, xs in by.items() if len(xs) >= 2}


def best_length(settled, typical):
    """The cut length (seconds) whose clips spread best, once there are enough clips to compare."""
    buckets = defaultdict(list)
    for v, m in settled:
        buckets[min((v["seconds"] or 0) // 10, 5)].append(lift(m, typical))
    good = {b: sum(xs) / (len(xs) + MIN_GROUP) for b, xs in buckets.items() if len(xs) >= MIN_GROUP}
    if len(good) < 2:
        return None
    b = max(good, key=good.get)
    return int(min(max(b * 10 + 5, 15), 55))


def distribution(vids, ms, now):
    """Is the Shorts feed testing our clips? Per week: clips posted, how many passed TESTED_VIEWS, best clip."""
    weeks = []
    for w in range(3):
        lo, hi = now - timedelta(days=7 * (w + 1)) - timedelta(hours=SETTLED_H), now - timedelta(days=7 * w) - timedelta(hours=SETTLED_H)
        vm = [(v, m) for v, m in zip(vids, ms) if v["privacy"] == "public" and lo <= v["published"] < hi]
        if vm:
            best = max(vm, key=lambda p: p[1]["views"])
            weeks.append({"from": lo, "to": hi, "n": len(vm), "tested": sum(1 for _, m in vm if m["views"] >= TESTED_VIEWS),
                          "views": sum(m["views"] for _, m in vm), "best": best[0], "best_views": best[1]["views"]})
    return weeks


def diagnose(v, m, typical):
    """What to change for one clip, worst first. Only findings that point at something we can do differently."""
    out = []
    if v["privacy"] != "public":
        if v.get("publish_at") and datetime.fromisoformat(v["publish_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc):
            out.append(("info", "Scheduled, not public yet."))
        else:
            out.append(("fix", f"Still {v['privacy']}: nobody can see it. Flip it to Public in YouTube Studio."))
            return out
    if v.get("upload_status") in ("rejected", "failed", "deleted"):
        out.append(("fix", f"YouTube {v['upload_status']} this upload ({v.get('rejection') or v.get('failure') or 'no reason given'})."))
    if v.get("blocked_in"):
        out.append(("fix", f"Copyright claim (blocked in {v['blocked_in']} countr{'ies' if v['blocked_in'] > 1 else 'y'}): claimed Shorts get little feed reach. "
                           "The builder won't clip this stream again."))
    if v["age_h"] < SETTLED_H:
        return out
    if m["views"] < TESTED_VIEWS:
        out.append(("reach", f"The feed never tested it ({m['views']} views in {v['age_h'] / 24:.0f} days). Nothing here "
                             "says the clip was bad; it says YouTube didn't show it. Usually reused-content signals "
                             "(a moment many channels posted, too little of our own take) or a stale stream."))
        return out
    if m["stayed"] is not None and m["stayed"] < 0.6:
        out.append(("hook", f"Only {m['stayed']:.0%} stayed past the swipe"
                            + (f"; the biggest drop is at {m['drop_s']:.0f}s" if m["drop_s"] is not None else "")
                            + ". Open on the reaction or the stake, not the setup, and make the hook promise the payoff."))
    elif m["hold_3s"] is not None and m["hold_3s"] < 0.65:
        out.append(("hook", f"{1 - m['hold_3s']:.0%} are gone by second 3. Start closer to the moment."))
    elif m["stayed"] is not None and m["stayed"] >= 0.75:
        out.append(("good", f"{m['stayed']:.0%} stayed past the swipe: this opening works, copy it."))
    if m["drop_s"] is not None and m["stayed"] is not None and m["stayed"] >= 0.6 and m["drop_s"] > 3:
        out.append(("length", f"Most viewers leave at {m['drop_s']:.0f}s of {v['seconds']}s: end the clip there or move "
                              "the payoff earlier."))
    if m["avp"] is not None:
        if m["avp"] < 60:
            out.append(("length", f"Watched {m['avp']:.0f}% on average: trim dead air and end on the punchline."))
        elif m["avp"] >= 90:
            out.append(("good", f"{m['avp']:.0f}% average view" + (" and it loops" if (m["end"] or 0) >= 0.9 else "")
                                + ": keep this length and pacing."))
    if m["feed_share"] is not None and m["feed_share"] < 0.5:
        out.append(("reach", f"Only {m['feed_share']:.0%} of its views came from the Shorts feed, so the feed tried it and "
                             "stopped. Watch the swipe rate on the next clips like it."))
    if typical is not None and lift(m, typical) >= math.log(3):
        out.append(("good", f"{(m['views'] + 1) / (typical + 1):.0f}x our typical clip: make more like this."))
    if (m["shares"] or 0) >= 3 and m["views"] and m["shares"] / m["views"] >= 0.01:
        out.append(("good", f"{m['shares']:.0f} shares: people send this one around, which is what makes Shorts travel."))
    return out


# ----- TikTok: numbers come from a sheet, since our TikTok posts are manual drafts -----

TT_COLS = ["clip", "posted", "creator", "caption", "views", "likes", "comments", "shares", "avg_watch_s", "watched_full_pct",
           "tiktok_video", "source"]


def tiktok_rows():
    """Every clip we sent to TikTok, with the numbers Philip filled in (blank until then). Keeps what's already typed."""
    import csv
    have = {}
    if TIKTOK_STATS.exists():
        with TIKTOK_STATS.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("clip"):
                    have[r["clip"]] = r
    for qfile in sorted(BUILDER_DIR.glob("*_20*/queue.json")):
        try:
            queue = json.loads(qfile.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for c in queue:
            if not c.get("tiktok_id"):
                continue
            key = f"{qfile.parent.name}#{c['id']}"
            row = have.setdefault(key, {})
            row.update({"clip": key, "posted": (c.get("post_local") or c.get("post_utc") or "")[:16],
                        "creator": c.get("creator") or "", "caption": (c.get("tiktok_caption") or c.get("title") or "")[:80]})
            row["_c"] = c
    return [have[k] for k in sorted(have, key=lambda k: have[k].get("posted") or "", reverse=True)]


def _words(text):
    return re.sub(r"#\w+|[^a-z0-9 ]", " ", (text or "").lower()).split()


def fill_tiktok_from_api(rs):
    """Fill views/likes/comments/shares from TikTok itself when the login has video.list (`tiktok-auth --stats`).
    Our posts are drafts Philip finishes in the app, so a post is matched to its draft by caption (he pastes ours)
    and by being posted after the draft was sent. Rows nobody posted stay blank; numbers he typed for a post the API
    can't match are kept. Returns a note for the report."""
    import difflib
    try:
        import tiktok_upload
        if not tiktok_upload.can_read_stats():
            return None
        times = [datetime.strptime(r["_c"]["post_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                 for r in rs if r.get("_c") and r["_c"].get("post_utc")]
        posts = tiktok_upload.my_videos(since=min(times) - timedelta(days=2) if times else None)
    except Exception as e:  # TikTok trouble never stops the review
        return f"Couldn't read TikTok views ({type(e).__name__}: {str(e)[:160]}); using the numbers in the sheet."
    taken, filled = set(), 0
    for r in rs:
        c = r.get("_c")
        if not c:
            continue
        mine = " ".join(_words(c.get("tiktok_caption") or c.get("title")))
        try:
            sent = datetime.strptime(c.get("post_utc") or "", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) - timedelta(hours=14)
        except ValueError:
            sent = None
        best, score = None, 0.0
        for p in posts:
            if p["id"] in taken or (sent and datetime.fromtimestamp(p.get("create_time", 0), timezone.utc) < sent):
                continue
            theirs = " ".join(_words(p.get("video_description") or p.get("title")))
            ratio = difflib.SequenceMatcher(None, mine[:120], theirs[:120]).ratio() if mine and theirs else 0
            if ratio > score:
                best, score = p, ratio
        if best and score >= 0.6:
            taken.add(best["id"])
            r.update({"views": best.get("view_count", ""), "likes": best.get("like_count", ""),
                      "comments": best.get("comment_count", ""), "shares": best.get("share_count", ""),
                      "tiktok_video": best["id"], "source": "TikTok API"})
            filled += 1
    return f"Read {len(posts)} public TikTok posts and matched {filled} of them to our drafts."


def save_tiktok_rows(rs):
    import csv
    with TIKTOK_STATS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TT_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rs:
            w.writerow({k: r.get(k, "") for k in TT_COLS})


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def tiktok_clips(rs, now):
    """(video-like, metrics-like) pairs for TikTok rows with views filled in, so the same lever analysis runs on them."""
    out = []
    for r in rs:
        views, c = _num(r.get("views")), r.get("_c")
        if views is None or not c:
            continue
        try:
            when = datetime.strptime(c.get("post_utc") or "", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        secs = int((c.get("end") or 0) - (c.get("start") or 0)) or 30
        full = _num(r.get("watched_full_pct"))
        v = {"id": r["clip"], "title": c.get("title") or r.get("caption"), "seconds": secs, "published": when,
             "age_h": (now - when).total_seconds() / 3600, "privacy": "public",
             "clip": {k: c.get(k) for k in ("source", "creator", "hook", "take", "signal", "spike", "platform",
                                            "stream_age_h", "draft", "writer")}}
        m = {"views": int(views), "stayed": (full / 100) if full is not None else None,
             "shares": _num(r.get("shares")), "avg_watch_s": _num(r.get("avg_watch_s"))}
        out.append((v, m))
    return out


# ---------- the written review ----------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {"type": "array", "items": {"type": "object", "properties": {
            "video_id": {"type": "string"}, "verdict": {"type": "string"}, "why": {"type": "string"},
            "better_title": {"type": "string"}, "better_hook": {"type": "string"}},
            "required": ["video_id", "verdict", "why", "better_title", "better_hook"]}},
        "lessons": {"type": "object", "properties": {
            "titles": {"type": "array", "items": {"type": "string"}},
            "hooks": {"type": "array", "items": {"type": "string"}},
            "clip_picks": {"type": "array", "items": {"type": "string"}},
            "length_and_pacing": {"type": "array", "items": {"type": "string"}},
            "posting": {"type": "array", "items": {"type": "string"}}},
            "required": ["titles", "hooks", "clip_picks", "length_and_pacing", "posting"]},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["clips", "lessons", "next_actions"],
}


def fmt(x, spec, none="n/a"):
    return none if x is None else format(x, spec)


def review_prompt(data, judged, typical, findings, weeks, pb, niche, previous):
    """`judged` is [(video, metrics, diagnosis)] for the clips worth writing about (best and worst tested, newest)."""
    blocks = []
    for v, m, d in judged:
        c = v["clip"]
        b = [f"[{v['id']}] \"{v['title']}\" ({v['seconds']}s, posted {v['published'].astimezone(LOCAL_TZ):%a %I:%M %p} ET, "
             f"{v['age_h']:.0f}h ago)",
             f"    Creator: {c.get('creator') or '?'}" + (f"; source stream \"{c['video_title']}\" at {c['moment']}" if c.get("video_title") else "")
             + (f"; stream was {c['stream_age_h']:.0f}h old when posted" if c.get("stream_age_h") is not None else ""),
             f"    Views {m['views']:,}, stayed past the swipe {fmt(m['stayed'], '.0%')}, avg viewed {fmt(m['avp'], '.0f')}%, "
             f"biggest drop at {fmt(m['drop_s'], '.0f')}s, from Shorts feed {fmt(m['feed_share'], '.0%')}, shares {fmt(m['shares'], '.0f')}"]
        if c.get("hook"):
            b.append(f"    On-screen hook: \"{c['hook']}\"; narrator said: \"{c.get('take')}\"")
        if v.get("transcript"):
            b.append(f"    Transcript: \"{v['transcript'][:700]}\"")
        if d:
            b.append("    Rule checks: " + " | ".join(t for _, t in d))
        blocks.append("\n".join(b))
    more, less = make_more_less(findings)
    winners = "\n".join(f"- {w['title']} ({w['views']:,} views, {w['seconds']}s)" for w in (pb or {}).get("winners", [])[:10])
    prev = "\n".join(f"- {x}" for xs in (previous or {}).values() for x in xs)
    feed = "; ".join(f"week to {w['to']:%b %d}: {w['tested']} of {w['n']} clips got a real feed test" for w in weeks)
    return f"""You coach a young YouTube Shorts channel, "{data['channel']['title']}", that posts {NICHES[niche]['label'].lower()}:
20-60 second cuts of streamers' live moments with an AI narrator line and on-screen hook text. The only goal is
clips that go viral. Be specific to these clips and this data, no generic advice, and say when the data is too thin.

Is the Shorts feed testing our clips: {feed or 'no settled clips yet'}. Our typical clip gets {typical or 0:.0f} views.

What the numbers already show (rule-based, by lever):
Make more: {'; '.join(finding_text(r) for r in more) or 'nothing clear yet'}
Make less: {'; '.join(finding_text(r) for r in less) or 'nothing clear yet'}

Clips to judge:

""" + "\n\n".join(blocks) + f"""

Breakout Shorts from the top channels in this niche, for comparison:
{winners or '- (no channel study yet)'}

Lessons from earlier reviews (keep the ones still true, drop or fix the rest):
{prev or '- (first review)'}

Write:
- clips: one entry per clip id above. verdict (one line: did it spread and the main reason), why (what in the hook,
  the moment, the title or the timing made viewers stay or swipe), better_title (creator's name first, what happens
  in plain punchy words, under 60 characters, 1-2 emoji at the end; only facts the transcript/hook support),
  better_hook (max 6 words, on-screen text that makes a scroller wait for the payoff).
- lessons: short rules the script writer and clip picker follow from now on, grounded in this data. 1-4 each.
- next_actions: the 3-5 changes most likely to get the next clips pushed by the feed, most important first.

Return only JSON matching: {{"clips": [{{"video_id", "verdict", "why", "better_title", "better_hook"}}],
"lessons": {{"titles": [], "hooks": [], "clip_picks": [], "length_and_pacing": [], "posting": []}}, "next_actions": []}}"""


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
        "model": model, "prompt": prompt, "format": REVIEW_SCHEMA, "stream": False, "options": {"temperature": 0.4, "num_ctx": 8192}})
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


WRITERS = {"claude-code": _claude_code, "gemini": script_agent._gemini, "groq": script_agent._groq,
           "ollama": _ollama, "api": _api}
# Free by default (a writer without its key is skipped); Claude only when picked with --writer or SCRIPT_WRITER.
AUTO = ["gemini", "groq", "ollama"]


def written_review(prompt, writer=None):
    order = [writer] if writer in WRITERS else AUTO
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

KIND_LABEL = {"fix": "Fix", "hook": "First seconds", "length": "Length", "reach": "Reach", "good": "Working", "info": "Note"}


def clip_lines(v, m, d, r, typical):
    c = v["clip"]
    L = [f"### [{v['title']}](https://www.youtube.com/shorts/{v['id']})", ""]
    meta = [c.get("creator") or "creator unknown", f"{v['seconds']}s", f"posted {v['published'].astimezone(LOCAL_TZ):%a %I:%M %p} ET"]
    if c.get("stream_age_h") is not None:
        meta.append(f"stream {c['stream_age_h']:.0f}h old")
    L.append("- " + ", ".join(meta))
    nums = [f"{m['views']:,} views" + (f" ({(m['views'] + 1) / (typical + 1):.1f}x typical)" if typical is not None and v["age_h"] >= SETTLED_H else "")]
    if m["stayed"] is not None:
        nums.append(f"{m['stayed']:.0%} stayed past the swipe")
    if m["drop_s"] is not None:
        nums.append(f"biggest drop at {m['drop_s']:.0f}s")
    if m["feed_share"] is not None:
        nums.append(f"{m['feed_share']:.0%} from the Shorts feed")
    if m["shares"]:
        nums.append(f"{m['shares']:.0f} share{'s' if m['shares'] != 1 else ''}")
    L.append("- " + ", ".join(nums))
    if c.get("hook"):
        L.append(f"- Hook: \"{c['hook']}\" ({hook_style(c['hook'])}) / narrator: \"{c.get('take')}\"")
    for k, t in d:
        L.append(f"- **{KIND_LABEL[k]}:** {t}")
    if r:
        L.append(f"- **Review:** {r['verdict']}" + (f" {r['why']}" if r.get("why") else ""))
        L.append(f"  - Try instead: \"{r['better_title']}\", hook \"{r['better_hook']}\" (not applied)")
    return L + [""]


def write_report(out_dir, data, vids, ms, diags, typical, findings, weeks, tt, llm, writer, steer):
    now = datetime.now(timezone.utc)
    ch = data["channel"]
    by_id = {c["video_id"]: c for c in (llm or {}).get("clips", [])}
    settled = settled_clips(vids, ms)
    L = [f"# What's spreading, {ch['title']}, {now.astimezone(LOCAL_TZ):%a %b %d %I:%M %p} ET", "",
         f"{len(settled)} clips old enough to judge (2+ days), our typical clip gets {typical or 0:.0f} views. "
         + ("With YouTube Analytics." if data["analytics"] else "Public stats only, so no swipe or retention data."), ""]

    fixes = [(v, t) for v, d in zip(vids, diags) for k, t in d if k == "fix"]
    if fixes:
        L += ["## Fix now", ""] + [f"- **{v['title']}**: {t}" for v, t in fixes] + [""]

    L += ["## Is the Shorts feed testing our clips?", "",
          f"A clip with under {TESTED_VIEWS} views after 2 days was never really shown. Until the feed tests our clips, "
          "hooks and titles can't make them go viral, so this is the first number to move.", ""]
    for w in weeks:
        L.append(f"- {w['from']:%b %d}-{w['to']:%b %d}: **{w['tested']} of {w['n']}** clips got a feed test, "
                 f"{w['views']:,} views in all; best: \"{w['best']['title']}\" ({w['best_views']:,})")
    if not weeks:
        L.append("- No clips are 2 days old yet.")
    elif weeks[0]["n"] >= 3 and weeks[0]["tested"] / weeks[0]["n"] < 0.3:
        L.append(f"- Most clips aren't getting a feed test. That's the bottleneck, ahead of hooks or titles: YouTube holds "
                 "back clips that look like reposts (a moment many channels already posted, a stock title or hook, "
                 "a narrator line that only describes the screen) and clips from streams other clippers already covered. "
                 "The levers below show which of ours get tested.")
    L.append("")

    more, less = make_more_less(findings)
    L += ["## Make more of", ""]
    L += [f"- {finding_text(r)}. Best: \"{r['best']}\"" for r in more] or ["- Nothing stands out yet (a finding needs "
                                                                          f"{MIN_GROUP}+ clips and a clear gap from typical)."]
    L += ["", "## Make less of", ""]
    L += [f"- {finding_text(r)}" for r in less] or ["- Nothing stands out yet."]
    thin = sorted({r["factor"] for r in findings} - {r["factor"] for r in more + less})
    if thin:
        L.append(f"- No clear winner or loser yet on: {', '.join(thin)}.")
    L.append("")

    tested = [(v, m) for v, m in settled if m["stayed"] is not None and m["views"] >= TESTED_VIEWS]
    L += ["## First seconds", ""]
    if tested:
        L.append(f"- Across {len(tested)} tested clips, a median {statistics.median(m['stayed'] for _, m in tested):.0%} "
                 "stayed past the swipe. Breakout Shorts usually hold 70%+; under 60% and the feed stops showing a clip.")
        drops = [m["drop_s"] for _, m in tested if m["drop_s"] is not None]
        if drops:
            L.append(f"- Viewers most often leave around {statistics.median(drops):.0f}s in.")
        by_hook = defaultdict(list)
        for v, m in tested:
            if (v["clip"] or {}).get("hook") is not None:
                by_hook[hook_style(v["clip"]["hook"])].append(m["stayed"])
        for k, xs in sorted(by_hook.items(), key=lambda kv: -statistics.fmean(kv[1])):
            L.append(f"- {k} hooks: {statistics.fmean(xs):.0%} stayed ({len(xs)} clip{'s' if len(xs) > 1 else ''})")
        worst = sorted(tested, key=lambda p: p[1]["stayed"])[:3]
        L.append("- Lost the most people at the start: " + "; ".join(
            f"\"{v['title']}\" {m['stayed']:.0%} (hook \"{(v['clip'] or {}).get('hook') or '-'}\")" for v, m in worst))
    else:
        L.append("- No tested clip has swipe data yet" + ("." if data["analytics"] else ": run `python review_agent.py auth`."))
    L.append("")

    L += ["## Clips worth a look", ""]
    ranked = sorted([(v, m, d) for v, m, d in zip(vids, ms, diags) if v["privacy"] == "public" and v["age_h"] >= SETTLED_H
                     and m["views"] >= TESTED_VIEWS], key=lambda x: -x[1]["views"])
    early = sorted([(v, m, d) for v, m, d in zip(vids, ms, diags) if v["age_h"] < SETTLED_H],
                   key=lambda x: x[0]["published"], reverse=True)
    shown = set()
    for label, group in [("Best tested clips", ranked[:4]),
                         ("Tested, but viewers swiped", sorted((x for x in ranked if x[1]["stayed"] is not None
                                                                and x[1]["stayed"] < 0.6), key=lambda x: x[1]["stayed"])[:3]),
                         ("Still early (under 2 days)", early[:4])]:
        group = [x for x in group if x[0]["id"] not in shown]
        if not group:
            continue
        L += [f"**{label}**", ""]
        for v, m, d in group:
            shown.add(v["id"])
            L += clip_lines(v, m, d, by_id.get(v["id"]), typical)
    untested = [v for (v, m) in settled if m["views"] < TESTED_VIEWS]
    if untested:
        L += [f"{len(untested)} other clips never got a feed test, so they say nothing about content.", ""]

    L += ["## TikTok", ""]
    if tt.get("note"):
        L.append(f"- {tt['note']}")
    filled = [r for r in tt["rows"] if _num(r.get("views")) is not None]
    if tt["findings"]:
        tm, tl = make_more_less(tt["findings"])
        L.append(f"- From the {len(filled)} TikTok posts with numbers:")
        L += [f"  - More: {finding_text(r)}" for r in tm] + [f"  - Less: {finding_text(r)}" for r in tl]
        if not (tm or tl):
            L.append("  - Nothing clear yet.")
    missing = [r for r in tt["rows"] if _num(r.get("views")) is None]
    if missing:
        L.append(f"- {len(missing)} clips went to your TikTok drafts with no numbers yet"
                 + (": either they were never posted, or the caption was changed so much the review couldn't match "
                    "them. Type the numbers into `reports/tiktok_stats.csv` for any you did post." if tt.get("note") and "matched" in tt["note"] else
                    ". To read them automatically, run `python channel_builder.py tiktok-auth --stats` once (README: "
                    "TikTok numbers). Or type views (and shares, watched full %) from TikTok Studio into "
                    "`reports/tiktok_stats.csv` for the ones you posted."))
    if not tt["rows"]:
        L.append("- No clips sent to TikTok yet.")
    L.append("")

    lessons = (llm or {}).get("lessons") or {}
    if (llm or {}).get("next_actions") or any(lessons.values()):
        L += ["## Written review", ""] + [f"- {a}" for a in (llm or {}).get("next_actions") or []]
        for k, label in [("titles", "Titles"), ("hooks", "Hooks"), ("clip_picks", "Clip picks"),
                         ("length_and_pacing", "Length and pacing"), ("posting", "Posting")]:
            L += [f"- {label}: {x}" for x in lessons.get(k, [])]
        L.append("")

    L += ["## What changes tomorrow", ""]
    if steer["source_score"]:
        L.append("- Creator weight in the clip pick: " + ", ".join(
            f"{SOURCES.get(k, {}).get('short', k)} {w:.2f}" for k, w in sorted(steer["source_score"].items(), key=lambda kv: -kv[1])))
    if steer["hour_score"]:
        et = lambda h: datetime.now(timezone.utc).replace(hour=int(h), minute=0).astimezone(LOCAL_TZ).strftime("%I%p").lstrip("0").lower()
        L.append("- Posting hours (ET) weight: " + ", ".join(
            f"{et(h)} {w:.2f}" for h, w in sorted(steer["hour_score"].items(), key=lambda kv: -kv[1])))
    for k, vals in (steer.get("levers") or {}).items():
        if vals:
            L.append(f"- {k.capitalize()} weight in the clip pick: " + ", ".join(f"{x} {w:.2f}" for x, w in vals.items()))
    if steer["length_hint"]:
        L.append(f"- Cut length: ~{steer['length_hint']}s")
    if steer["make_more"] or steer["make_less"]:
        L.append(f"- The script writer gets the {len(steer['make_more'])} make-more and {len(steer['make_less'])} "
                 "make-less findings above in its prompt.")
    L.append(f"- Edit settings on the top clip: {tuner.best_recipe(data['state'])}")
    if data["notes"]:
        L += ["", "## Data problems", ""] + [f"- {n}" for n in data["notes"]]
    L += ["", f"_Review by {writer or 'rules only'}. This agent never changes your videos._", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# ---------- main ----------

def pick_levers(findings):
    """Lever scores the clip council can use on a candidate before it's cut: how it was found and how fresh it is."""
    return {k: lever_scores(findings, k) for k in ("found by", "stream age at post")}


def found_by_moment(m):
    """found_by() for a candidate moment (before it becomes a queue entry)."""
    return found_by({"platform": m.get("platform"), "spike": m.get("chat_ratio"),
                     "signal": ("chat went" if m.get("chat_ratio") else "") + (" heat" if m.get("heat") is not None else "")
                     + (" timestamp" if m.get("comments") else "") or "most replayed"})


def cmd_review(args):
    state = load_state()
    data = collect(args, state)
    data["state"] = state
    vids = data["videos"]
    now = datetime.now(timezone.utc)
    ms = [metrics(v) for v in vids]
    settled = settled_clips(vids, ms)
    typical = typical_views(settled)
    diags = [diagnose(v, m, typical) for v, m in zip(vids, ms)]
    findings = factor_findings(settled, typical)
    weeks = distribution(vids, ms, now)

    rows_tt = tiktok_rows()
    tt_note = fill_tiktok_from_api(rows_tt)
    tt_pairs = [(v, m) for v, m in tiktok_clips(rows_tt, now) if v["age_h"] >= SETTLED_H]
    tt = {"rows": rows_tt, "note": tt_note, "findings": factor_findings(tt_pairs, typical_views(tt_pairs), "TikTok") if tt_pairs else []}
    save_tiktok_rows(rows_tt)

    llm, writer = (None, None)
    if vids and not args.no_llm:
        pb_file = latest(f"builder/playbook_channels_{args.niche}_*.json")
        pb = json.loads(pb_file.read_text(encoding="utf-8")) if pb_file else None
        tested = sorted([x for x in zip(vids, ms, diags) if x[0] in [v for v, _ in settled] and x[1]["views"] >= TESTED_VIEWS],
                        key=lambda x: -x[1]["views"])
        early = [x for x in zip(vids, ms, diags) if x[0]["age_h"] < SETTLED_H]
        judged = list({x[0]["id"]: x for x in tested[:5] + tested[-3:] + early[:3]}.values())
        if judged:
            prompt = review_prompt(data, judged, typical, findings + tt["findings"], weeks, pb, args.niche,
                                   (state.get("review") or {}).get("lessons"))
            llm, writer = written_review(prompt, args.writer or os.environ.get("SCRIPT_WRITER"))

    more, less = make_more_less(findings)
    tt_more, tt_less = make_more_less(tt["findings"])
    steer = {"source_score": creator_scores(settled, typical), "hour_score": hour_scores(settled, typical),
             "length_hint": best_length(settled, typical), "levers": pick_levers(findings),
             "make_more": [finding_text(r) for r in more + tt_more], "make_less": [finding_text(r) for r in less + tt_less]}

    out_dir = REPORTS_DIR / f"review_{now:%Y-%m-%d_%H%M}"
    write_report(out_dir, data, vids, ms, diags, typical, findings, weeks, tt, llm, writer, steer)
    snapshot = [{"id": v["id"], "title": v["title"], "age_h": round(v["age_h"], 1), "privacy": v["privacy"],
                 "seconds": v["seconds"], "source": v["clip"].get("source"), "features": features(v), **{k: m[k] for k in m},
                 "findings": [{"kind": k, "text": t} for k, t in d]} for v, m, d in zip(vids, ms, diags)]
    (out_dir / "review.json").write_text(json.dumps(
        {"channel": data["channel"], "typical_views": typical, "feed_tests": [{**w, "from": w["from"].isoformat(), "to": w["to"].isoformat(),
                                                                              "best": w["best"]["title"]} for w in weeks],
         "findings": [{k: x for k, x in r.items()} for r in findings + tt["findings"]], "steer": steer,
         "clips": snapshot, "written": llm, "writer": writer}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")

    # feed the other agents
    state = load_state()  # re-read: another agent may have written while we waited on the reviewer
    prev = state.get("review") or {}
    lessons = (llm or {}).get("lessons") or prev.get("lessons") or {}
    verdict = {c["video_id"]: c.get("verdict") for c in (llm or {}).get("clips", [])}
    best_first = sorted((s for s in snapshot if s["age_h"] >= SETTLED_H and s["privacy"] == "public" and s["views"] >= TESTED_VIEWS),
                        key=lambda s: -s["views"])
    state["review"] = {
        "at": now.strftime("%Y-%m-%d %H:%M"), "report": str(out_dir / "report.md"), "lessons": lessons,
        "typical_views": round(typical, 1) if typical is not None else None,
        "feed_tested": f"{weeks[0]['tested']} of {weeks[0]['n']}" if weeks else None,
        "make_more": steer["make_more"], "make_less": steer["make_less"],
        "source_score": steer["source_score"] or prev.get("source_score", {}),
        "hour_score": steer["hour_score"] or prev.get("hour_score", {}),
        "levers": steer["levers"],
        "length_hint": steer["length_hint"] or prev.get("length_hint"),
        "clips": [{"title": s["title"], "views": s["views"], "avg_viewed": s["avp"], "stayed": s["stayed"],
                   "verdict": verdict.get(s["id"])} for s in best_first[:6] + best_first[6:][-4:]],
    }
    # score each clip's edit recipe once it's settled, so tomorrow's edits follow what held viewers
    # and each council agent's vote on it, so the agents whose judgment matches real results count for more
    recipe_of = {v["id"]: (v.get("clip") or {}).get("recipe") for v in vids}
    votes_of = {v["id"]: (v.get("clip") or {}).get("council") for v in vids}
    scored = 0
    for snap in snapshot:
        if snap["age_h"] >= SETTLED_H and snap["privacy"] == "public" and recipe_of.get(snap["id"]):
            outcome = tuner.score(snap["views"], snap["avp"], snap["stayed"], typical)
            if tuner.credit(state, snap["id"], recipe_of[snap["id"]], outcome):
                scored += 1
                council.learn(state, votes_of.get(snap["id"]), outcome)
    if scored:
        print(f"Edit tuner: scored {scored} clip(s). Settings now: {tuner.best_recipe(state)}")
    # a claimed clip means that stream is in Content ID: the builder never cuts from it again (originality.py)
    claimed = [v["clip"]["video_id"] for v in vids if v.get("blocked_in") and (v.get("clip") or {}).get("video_id")]
    if claimed:
        state["claimed_streams"] = list(dict.fromkeys((state.get("claimed_streams") or []) + claimed))[-200:]
        print(f"Copyright claims on clips from {len(claimed)} stream(s); the builder won't clip those streams again.")
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
