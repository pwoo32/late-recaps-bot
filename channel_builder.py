"""Channel builder: turns the trend agent and channel study agent's reports into a running
clips channel, then learns from your own channel's results.

Usage:
    export YOUTUBE_API_KEY=...
    python channel_builder.py plan                 # channel kit: niche, name, format, titles, schedule, money plan
    python channel_builder.py daily                # today's clip queue: exact source moments + titles + post times
    python channel_builder.py cut 1                # make queue item 1 into a captioned vertical Short
    python channel_builder.py track --channel @yourhandle   # score your uploads, re-weight sources and time slots
    python channel_builder.py publish              # cut + voice + upload every clip in the latest queue, scheduled
    python channel_builder.py run --publish        # the whole day hands-off: refresh reports, track, queue, cut, upload

How it picks moments: it reads the full streams/videos your sources posted in the last few days and
finds the seconds viewers timestamp in the comments ("1:33:18 please Cole enough 😂"). Many likes on
a timestamp = a moment people want to rewatch. With Twitch keys it also uses each creator's most
viewed Twitch clips. Moments are weighted by how hot the creator is in the latest trend report and
by how that creator's clips did on your own channel.

Scripts (title, hook, context line, narrator's take, description, tags) come from script_agent.py, which daily
and cut run for you. It uses a free local Ollama model, and free_writer.py (no model at all) when Ollama isn't
running; Claude only if you ask for it with SCRIPT_WRITER. `cut` needs ffmpeg + yt-dlp, faster-whisper for
captions, and Chatterbox or Kokoro for the narrator voice (see narrate). `publish` needs a Google OAuth client (see README).

Quota: plan ~30 units, daily ~30-60 units, track ~3 units. Outputs land in reports/builder/.
"""

import argparse
import difflib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from agent import REPORTS_DIR, iso_duration_seconds
from channel_agent import ChannelYouTube, analyze_channel, study, video_record
from niches import NICHES
from sources import SOURCES
import council
import live_moments as LM
import originality
import script_agent
import tuner

BUILDER_DIR = REPORTS_DIR / "builder"
STATE_FILE = BUILDER_DIR / "state.json"
LOCAL_TZ = ZoneInfo("America/New_York")  # where most of this niche's audience is
TS = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿‍️]")
MIN_SOURCE_SECONDS = 15 * 60


# ---------- shared state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"source_weight": {}, "slot_weight": {}, "used_moments": [], "queued": {}, "channel": None,
            "handles": {}}


def save_state(state):
    BUILDER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def fmt_ts(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def latest(pattern):
    runs = sorted(REPORTS_DIR.glob(pattern), key=lambda p: (p.stat().st_mtime, str(p)))  # a fresh checkout gives every file the same mtime: the dated name breaks ties
    return runs[-1] if runs else None


def run_age_days(run_dir):
    m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{4})", run_dir.name)
    if not m:
        return 999
    t = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


class BuilderYouTube(ChannelYouTube):
    def videos(self, ids):  # same 1 unit per 50, plus when a stream started/ended
        out = []
        for i in range(0, len(ids), 50):
            out += self._get("videos", 1, part="snippet,statistics,contentDetails,liveStreamingDetails",
                             id=",".join(ids[i:i + 50])).get("items", [])
        return out

    def channel_id(self, ref, state):
        if ref.startswith("UC"):
            return ref
        if ref in state["handles"]:
            return state["handles"][ref]
        items = self._get("channels", 1, part="id", forHandle=ref).get("items", [])
        if not items:
            raise RuntimeError(f"No YouTube channel for {ref}")
        state["handles"][ref] = items[0]["id"]
        return items[0]["id"]

    def handle_taken(self, handle):
        return bool(self._get("channels", 1, part="id", forHandle=handle).get("items"))

    def comments(self, video_id, pages=3):
        out, token = [], None
        for _ in range(pages):
            params = dict(part="snippet", videoId=video_id, maxResults=100, order="relevance",
                          textFormat="plainText")
            if token:
                params["pageToken"] = token
            try:
                data = self._get("commentThreads", 1, **params)
            except RuntimeError:  # comments off
                break
            for it in data.get("items", []):
                s = it["snippet"]["topLevelComment"]["snippet"]
                out.append({"text": s["textDisplay"], "likes": int(s.get("likeCount", 0))})
            token = data.get("nextPageToken")
            if not token:
                break
        return out


# ---------- inputs from the other two agents ----------

def trend_inputs():
    """Latest trend run: niche rows, and how much clip demand each source has right now."""
    run = latest("[0-9]*_*/niches.json")
    if not run:
        sys.exit("No trend report yet. Run `python agent.py` first.")
    rows = json.loads(run.read_text(encoding="utf-8"))
    videos = json.loads((run.parent / "videos.json").read_text(encoding="utf-8"))
    return run.parent, rows, videos


def source_demand(videos, niche):
    """Views/hour of recent clips that name each source, from the trend run's raw videos."""
    demand = {}
    pool = videos.get(niche, [])
    for key, src in SOURCES.items():
        if src["niche"] != niche:
            continue
        pats = [re.compile(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])") for a in src["aliases"]]
        tot = 0.0
        for v in pool:
            text = (v["title"] + " " + " ".join(v.get("tags", [])[:15])).lower()
            if any(p.search(text) for p in pats):
                tot += v["views_per_hour"]
        demand[key] = tot
    return demand


def playbook(yt, niche, state):
    """The channel study agent's playbook for this niche, recomputed from its saved channel list
    (cached per study run, so it's ~25 units once and free afterwards)."""
    run = latest(f"channels_{niche}_*/channels.json")
    if not run:
        sys.exit(f"No channel study for {niche}. Run `python channel_agent.py --niche {niche}` first.")
    cache = BUILDER_DIR / f"playbook_{run.parent.name}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")), run.parent
    ranked = json.loads(run.read_text(encoding="utf-8"))
    ids = [c["id"] for c in ranked[:10]]
    now = datetime.now(timezone.utc)
    cfg = NICHES[niche]
    chans = []
    for c in yt.channels(ids):
        pl = c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        vids = yt.recent_uploads(pl) if pl else []
        uploads = [video_record(it, now) for it in yt.videos(vids)] if vids else []
        if uploads:
            chans.append(analyze_channel(c, uploads, now, cfg))
    chans.sort(key=lambda c: ids.index(c["id"]))
    s = study(chans, cfg["queries"])
    young = [c for c in ranked if c["age_months"] <= 12 and c["median_views"]]
    pb = {
        "study_run": run.parent.name,
        "median_seconds": s["median_seconds"],
        "length": s["length"],
        "cadence_median": s["cadence_median"],
        "cadence_p75": s["cadence_p75"],
        "top_hours": s["top_hours"],
        "hashtags": s["hashtags"],
        "hooks": s["hooks"],
        "title_chars": s["chars_cmp"][0],
        "title_cmp": s["title_cmp"],
        "sources": s["sources"],
        "winners": [{"title": v["title"], "views": v["views"], "lift": round(v["lift"], 1),
                     "seconds": v["seconds"], "channel": v["channel"]} for v in s["winners"]],
        "young_channels": [{"name": c["name"], "age_months": c["age_months"], "median_views": c["median_views"],
                            "views_30d": c["views_30d"], "uploads_per_day": c["uploads_per_day"]}
                           for c in young[:6]],
        "benchmark_median_views": int(statistics.median(c["median_views"] for c in young)) if young else None,
    }
    BUILDER_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pb, indent=1), encoding="utf-8")
    return pb, run.parent


def best_length(pb, state=None):
    """Clip length to aim for: what held viewers best on your channel (review_agent), else the length bucket
    with the best lift that most studied channels use."""
    hint = ((state or {}).get("review") or {}).get("length_hint")
    if hint:
        return hint
    good = [b for b in pb["length"] if b["channels"] >= 4]
    if not good:
        return 45
    b = max(good, key=lambda b: b["median_lift"])
    lo, hi = (int(x) for x in re.findall(r"\d+", b["bucket"])[:2])
    return (lo + hi) // 2


POST_WITHIN_H = 12  # a day's clips go up within this many hours of the build, while the stream is still fresh


def next_at(hour, now):
    """The next time it's `hour`:00 UTC after now."""
    when = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def post_slots(pb, state, n, now=None, within_h=None):
    """UTC hours to post at: the study's best-lift hours, re-weighted by your own results. With `within_h`, only
    hours coming up in the next `within_h` hours count (topped up with the soonest others if too few do), so a
    one-off hot hour in the study can't hold a clip back most of a day."""
    hours = {int(h): lift for h, lift in pb["top_hours"]}
    for h, w in state["slot_weight"].items():
        hours[int(h)] = hours.get(int(h), 1.0) * w
    if not hours:
        hours = {21: 1.0, 17: 1.0}
    if within_h:
        now = now or datetime.now(timezone.utc)
        ahead = lambda h: (next_at(h, now) - now).total_seconds() / 3600
        hours.update({h: 1.0 for h in range(24) if h not in hours})  # unscored hours count as average
        near = {h: w for h, w in hours.items() if ahead(h) <= within_h}
        hours = near if len(near) >= n else hours
    ranked = sorted(hours, key=hours.get, reverse=True)
    picked = []
    for h in ranked:  # spread posts at least 3 hours apart
        if all(min(abs(h - p), 24 - abs(h - p)) >= 3 for p in picked):
            picked.append(h)
        if len(picked) == n:
            break
    return picked  # best hour first, so the best pick gets it


# ---------- finding moments ----------

def comment_moments(comments, duration):
    """Cluster timestamped comments into moments, scored by likes."""
    marks = []
    for c in comments:
        stamps = list(TS.finditer(c["text"]))
        if not stamps or len(stamps) >= 4:  # 4+ stamps = a chapter list, not a reaction
            continue
        for i, m in enumerate(stamps):
            h, mi, s = (int(x or 0) for x in m.groups())
            t = h * 3600 + mi * 60 + s
            after, before = c["text"][m.end():m.end() + 6].lower(), c["text"][max(0, m.start() - 12):m.start()]
            if re.match(r"\s*[ap]\.?m\b", after) or re.search(r"\b[A-Z][a-z]+ $", before) and not re.search(r"\b(At|Around|From)\s$", before):
                continue  # a clock time ("3:07 pm") or a verse ("Acts 1:07"), not a moment
            if 60 <= t <= duration - 5:
                # With several stamps, each one's label is the text right after it.
                seg = c["text"] if len(stamps) == 1 else c["text"][m.end():stamps[i + 1].start() if i + 1 < len(stamps) else None]
                label = re.sub(r"\s+", " ", EMOJI.sub("", TS.sub("", seg))).strip(" -|:.,@")
                marks.append({"t": t, "likes": c["likes"], "text": label[:140]})
    marks.sort(key=lambda m: m["t"])
    clusters = []
    for m in marks:
        if clusters and m["t"] - clusters[-1][-1]["t"] <= 40:
            clusters[-1].append(m)
        else:
            clusters.append([m])
    out = []
    for cl in clusters:
        w = [math.log2(2 + m["likes"]) for m in cl]
        t = sum(m["t"] * wi for m, wi in zip(cl, w)) / sum(w)
        quotes = [m["text"] for m in sorted(cl, key=lambda m: m["likes"], reverse=True) if m["text"]]
        out.append({"t": int(t), "score": round(sum(w), 2), "comments": len(cl),
                    "likes": sum(m["likes"] for m in cl), "quotes": quotes[:5]})
    return out


def youtube_candidates(yt, key, src, state, since, now, chat_budget=240, live_streams=2):
    """Moments in a creator's recent long videos/stream VODs: timestamps viewers comment, plus, for their newest
    live streams, where chat flooded and what YouTube's "most replayed" graph shows."""
    cid = yt.channel_id(src["youtube"], state)
    ids = yt.recent_uploads("UU" + cid[2:], n=15)
    out, streams_read = [], 0
    for it in yt.videos(ids):
        if not it.get("contentDetails", {}).get("duration"):  # live right now or upcoming: no VOD to cut from yet
            continue
        sec = iso_duration_seconds(it["contentDetails"]["duration"])
        published = datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z", "+00:00"))
        if sec < MIN_SOURCE_SECONDS or published < since:
            continue
        views = int(it.get("statistics", {}).get("viewCount", 0))
        live = it.get("liveStreamingDetails") or {}
        ended = live.get("actualEndTime")
        if originality.produced_upload(it["snippet"]["title"], sec, bool(ended)) and not src.get("allow_edited"):
            continue  # an edited main-channel video, not a stream: cutting it up is a reupload Content ID matches
        age_h = (now - datetime.fromisoformat((ended or it["snippet"]["publishedAt"]).replace("Z", "+00:00"))).total_seconds() / 3600
        base = dict(source=key, platform="youtube", video_id=it["id"], video_title=it["snippet"]["title"],
                    video_views=views, age_h=round(age_h, 1), was_live=bool(ended))
        moments = [dict(m, **base, signals=["comments"]) for m in comment_moments(yt.comments(it["id"]), sec)]
        if ended and age_h <= 72 and streams_read < live_streams:  # the creator's newest streams: read the room
            streams_read += 1
            print(f"  reading the room on \"{it['snippet']['title'][:60]}\" (chat replay, most replayed)")
            cache = state.setdefault("chat_spikes", {})
            spikes = cache.get(it["id"])
            if spikes is None:  # a finished stream's chat never changes: read it once
                spikes = LM.chat_spikes(LM.chat_messages(it["id"], chat_budget), sec)
                cache[it["id"]] = spikes
                for k in list(cache)[:-40]:
                    del cache[k]
            for sp in spikes:
                near = next((m for m in moments if abs(m["t"] - sp["t"]) <= 45), None)
                if near:  # viewers timestamped it AND chat went off: strongest signal there is
                    near["score"] = round(near["score"] + LM.spike_score(sp["ratio"]), 2)
                    near["chat_ratio"] = sp["ratio"]
                    near["signals"].append("chat spike")
                else:
                    moments.append(dict(base, t=sp["t"], score=LM.spike_score(sp["ratio"]), comments=0, likes=sp["msgs"],
                                        quotes=sp["sample"], chat_ratio=sp["ratio"], signals=["chat spike"]))
            heat = (LM.video_info(it["id"]) or {}).get("heatmap") or []
            for m in moments:
                h = LM.heat_at(heat, m["t"])
                if h is not None:
                    m["heat"] = round(h, 2)
            for pk in LM.heat_peaks(heat):
                if all(abs(m["t"] - pk["t"]) > 45 for m in moments):
                    moments.append(dict(base, t=pk["t"], score=round(8 * pk["value"], 2), comments=0, likes=0, quotes=[],
                                        heat=pk["value"], signals=["most replayed"]))
            if spikes or heat:
                print(f"    {len(spikes)} chat spikes" + (", most-replayed graph read" if heat else ""))
        for m in moments:
            m["url"] = f"https://www.youtube.com/watch?v={it['id']}&t={m['t']}s"
        out += moments
    return out


MIN_WHOLE_CLIP_S = 15  # Twitch/Kick clips used whole must be at least this long (Nov 12: a 5s clip posted)
TWITCH_BOOST = 1.3  # favour moments viewers clipped live on Twitch (asked for Nov 2026)


def twitch_candidates(key, src, since, now, h=None):
    """The creator's most-viewed Twitch clips: what viewers clipped while the stream was live. Clips whose VOD is
    up are cut from the VOD (so the length can be tuned); fresh ones from a stream still running are used as is."""
    clips = []
    if src.get("twitch"):
        if h:
            uid = LM.twitch_user(h, src["twitch"])
            tw = LM.twitch_clips(h, uid, since) if uid else []
        else:  # no Twitch API keys: read the public clips page with yt-dlp instead
            tw = LM.twitch_clips_keyless(src["twitch"], since)
        print(f"  {len(tw)} Twitch clips from {src['twitch']}")
        clips += [dict(c, _site="twitch") for c in tw]
    if src.get("kick"):
        kk = LM.kick_clips(src["kick"], since)
        print(f"  {len(kk)} Kick clips from {src['kick']}")
        clips += [dict(c, _site="kick") for c in kk]
    out = []
    for c in clips:
        if c.get("video_id") is None or c.get("vod_offset") is None or c["_site"] == "kick":
            if (c.get("duration") or 30) < MIN_WHOLE_CLIP_S:  # used whole: a 5s clip leaves no room for our take
                continue
        created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        vph = LM.clip_velocity(c, now)
        score = round(math.log2(2 + c["view_count"]) * 1.5 * (1 + min(vph / 20000, 1)) * TWITCH_BOOST, 2)
        common = dict(score=score, comments=0, likes=c["view_count"], quotes=[c["title"]], source=key,
                      video_title=c["title"], video_views=c["view_count"], clip_vph=round(vph),
                      age_h=round((now - created).total_seconds() / 3600, 1), signals=["twitch clip"], was_live=True)
        if c["_site"] == "kick":
            out.append(dict(common, t=0, platform="kick_clip", video_id=c["id"], duration=c["duration"], url=c["url"]))
            continue
        if c.get("video_id") and c.get("vod_offset") is not None:
            t = int(c["vod_offset"] + c["duration"] * 0.7)  # clips end on the payoff; centre near it
            out.append(dict(common, t=t, platform="twitch", video_id=c["video_id"],
                            url=f"https://www.twitch.tv/videos/{c['video_id']}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"))
        else:  # still live (or no VOD): the clip itself is the footage
            out.append(dict(common, t=0, platform="twitch_clip", video_id=c["id"], duration=c["duration"], url=c["url"]))
    return out


def trend_words(videos, niche):
    """Per creator: the words in their fastest-growing clips this week (who they're with, what they're doing)."""
    from free_writer import STOP as stop
    out = {}
    pool = sorted(videos.get(niche, []), key=lambda v: -v.get("views_per_hour", 0))
    for key, src in SOURCES.items():
        hits = [v for v in pool if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", v["title"].lower()) for a in src["aliases"])][:15]
        words = Counter(w for v in hits for w in set(re.findall(r"[a-z0-9$]{3,}", v["title"].lower()))
                        if w not in stop and not any(w in a for a in src["aliases"]))
        out[key] = {w for w, n in words.items() if n >= 2}
    return out


def demand_change(niche):
    """Per creator: clip demand in the newest trend run vs the one before (2.0 = doubled). Who's spiking."""
    runs = sorted(REPORTS_DIR.glob("[0-9]*_*/videos.json"), key=lambda p: (p.stat().st_mtime, str(p)))[-2:]
    if len(runs) < 2:
        return {}
    old, new = (source_demand(json.loads(r.read_text(encoding="utf-8")), niche) for r in runs)
    return {k: (new.get(k, 0) + 50) / (old.get(k, 0) + 50) for k in new}


def rank_moments(moments, demand, state, words=None, change=None):
    top_demand = max(demand.values() or [0]) or 1
    used = set(state["used_moments"])
    claimed = set(state.get("claimed_streams") or [])  # source videos one of our clips got a copyright claim from
    posted_per_stream = {}
    for k, q in (state.get("queued") or {}).items():
        if q.get("video_id"):
            vid = k.rpartition("@")[0]
            posted_per_stream[vid] = posted_per_stream.get(vid, 0) + 1
    used_at = {}  # video_id -> 30s buckets already clipped, so a moment a minute away from an old post counts as used
    for k in used:
        vid, _, b = k.rpartition("@")
        if b.isdigit():
            used_at.setdefault(vid, set()).add(int(b))
    for m in moments:
        src = SOURCES[m["source"]]
        hot = 0.6 + 0.8 * demand.get(m["source"], 0) / top_demand
        learned = state["source_weight"].get(m["source"], 1.0)  # views on your channel (track)
        learned *= (state.get("review") or {}).get("source_score", {}).get(m["source"], 1.0)  # views x retention (review_agent)
        fresh = 0.5 ** (m["age_h"] / 72)  # older streams are more picked over by other clippers
        reach = 0.5 + min(math.log10(max(m["video_views"], 10)) / 7, 1)
        paid = 1.25 if src.get("campaign_cpm") else 1.0
        rising = min(max((change or {}).get(m["source"], 1.0), 0.7), 1.8)  # creator spiking in today's trend scan
        text = " ".join(m["quotes"] + [m["video_title"]]).lower()
        topical = 1 + 0.15 * min(sum(1 for w in (words or {}).get(m["source"], ()) if w in text), 3)  # what's viral now
        heat = 0.8 + 0.6 * m["heat"] if m.get("heat") is not None else 1.0  # most-replayed part of the stream
        m["music_risk"] = originality.music_hit(" ".join(m["quotes"]), m["video_title"])
        music = 0.2 if m["music_risk"] else 1.0  # songs are what Content ID claims; keep them for last resort only
        # Creators whose every moment is already posted by hundreds of clip channels (and by their own Shorts
        # channels and YouTube's auto highlights): our copy is one of many near-duplicates (sources.py "crowd").
        crowd = src.get("crowd", 1.0)
        m["rank"] = round(m["score"] * hot * learned * fresh * reach * paid * rising * topical * heat * music * crowd, 2)
        m["key"] = f"{m['video_id']}@{m['t'] // 30}"
        b = m["t"] // 30  # same scene: within 3 buckets (~90s) of something already posted from this stream
        m["used"] = (m["key"] in used or any(abs(b - u) <= 3 for u in used_at.get(m["video_id"], ()))
                     or m["video_id"] in claimed  # this stream already got us a claim
                     or posted_per_stream.get(m["video_id"], 0) >= originality.MAX_CLIPS_PER_STREAM)  # near-duplicates
    return sorted((m for m in moments if not m["used"]), key=lambda m: m["rank"], reverse=True)


# ---------- titles and hooks ----------

def description(m, meta, pb):
    src = SOURCES[m["source"]]
    tags = list(dict.fromkeys(src["hashtags"] + [h for h, _ in pb["hashtags"][:4]] + ["#shorts"]))[:6]
    return (f"{meta['context']}\n\n"
            f"Full {'stream' if m['platform'].startswith(('twitch', 'kick')) or m.get('was_live') or 'live' in m['video_title'].lower() else 'video'} "
            f"by {src['name']}: {m['url']}\n"
            f"Go support {src['name']}, all credit to them.\n\n" + " ".join(tags))


# ---------- commands ----------

def is_spike(m, min_chat=4.0, min_vph=8000):
    """A moment that's going nuts right now: chat flooded several times its normal pace, or a Twitch clip of it is
    pulling thousands of views an hour."""
    return m["age_h"] <= 18 and ((m.get("chat_ratio") or 0) >= min_chat or (m.get("clip_vph") or 0) >= min_vph)


def cmd_daily(args, yt=None, state=None):
    yt = yt or BuilderYouTube(os.environ["YOUTUBE_API_KEY"])
    state = state or load_state()
    trend_dir, rows, videos = trend_inputs()
    niche = args.niche
    pb, study_dir = playbook(yt, niche, state)
    demand = source_demand(videos, niche)
    change = demand_change(niche)
    now = datetime.now(timezone.utc)
    spike = getattr(args, "spike", False)
    since = now - timedelta(hours=18 if spike else args.hours)

    moments = []
    try:
        th = LM.twitch_headers() if any(s.get("twitch") for s in SOURCES.values()) else None
    except Exception as e:  # bad keys: fall back to reading the public clips pages
        print(f"Twitch API login failed ({type(e).__name__}); reading public clips pages instead")
        th = None
    for key, src in SOURCES.items():
        if src["niche"] != niche or "blocked" in src["permission"].lower():
            continue
        print(f"[{key}] reading recent streams/videos")
        try:
            if src.get("youtube"):
                moments += youtube_candidates(yt, key, src, state, since, now, live_streams=1 if spike else 2)
            moments += twitch_candidates(key, src, since, now, th)
        except Exception as e:  # one creator's trouble shouldn't stop the day
            print(f"  skipped {key}: {type(e).__name__}: {str(e)[:200]}")
    ranked = rank_moments(moments, demand, state, trend_words(videos, niche), change)
    if spike:
        ranked = [m for m in ranked if is_spike(m)]
        if not ranked:
            print("Nothing is spiking right now.")
            return None

    def allowed(m, picks):  # max 2 per creator a day so the channel isn't one-note (3 when they're blowing up)
        cap = 3 if change.get(m["source"], 1) >= 1.5 else 2
        return (sum(p["source"] == m["source"] for p in picks) < cap and
                all(m["video_id"] != p["video_id"] or abs(m["t"] - p["t"]) > 90 for p in picks))

    # Every agent weighs in on the top candidates; a free local model chairs and picks (council.py).
    out_dir = BUILDER_DIR / f"{'spike' if spike else 'daily'}_{now.strftime('%Y-%m-%d_%H%M')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    shortlist = []
    for m in ranked:  # a few per creator, so the meeting sees the whole field, not one stream
        if sum(x["source"] == m["source"] for x in shortlist) < 4 and allowed(m, [x for x in shortlist if x["video_id"] == m["video_id"]]):
            shortlist.append(m)
        if len(shortlist) >= 16:
            break
    print(f"Clip council: {len(shortlist)} candidates, every agent votes...")
    ctx = {"state": state, "pb": pb, "demand": demand, "change": change, "words": trend_words(videos, niche),
           "hot": script_agent.trending(niche, [{"source": k, "creator": s["name"]} for k, s in SOURCES.items()])[1],
           "asks": council.audience_asks(yt, state)}
    picks = council.decide(shortlist, args.count, ctx, cap=allowed, use_chair=True, out_dir=out_dir)
    for m in picks:
        print(f"  picked {SOURCES[m['source']]['short']} at {fmt_ts(m['t'])} (council {m['council']:.2f}): {m['council_why'][:120]}")
    if not picks:
        sys.exit(f"No moments found in the last {args.hours}h. Try --hours 168, or add sources in sources.py.")

    edits = tuner.recipes(state, len(picks), now.strftime("%Y-%m-%d") + ("spike" if spike else ""))
    slots = post_slots(pb, state, len(picks), now, POST_WITHIN_H)
    queue = []
    for i, m in enumerate(picks):
        recipe = edits[i]
        length = args.length or recipe["length"]
        meta = script_agent.template_script({**m, "creator": SOURCES[m["source"]]["name"]}, pb)  # placeholder; script_agent writes the real script below
        if spike:
            when = now  # it's blowing up now: post now
        else:
            when = next_at(slots[i % len(slots)], now)
        if m["platform"] in ("twitch_clip", "kick_clip"):
            start, end = 0, min(m.get("duration") or length, 60)
        else:
            start = max(0, m["t"] - round(length * 0.65))
            end = start + length
        signal = (f"{m['comments']} timestamp comments, {m['likes']:,} likes" if m["comments"] else
                  f"Twitch clip, {m['likes']:,} views ({m.get('clip_vph', 0):,}/h)" if "twitch clip" in m.get("signals", [])
                  else "")
        if m.get("chat_ratio"):
            signal += ("; " if signal else "") + f"chat went {m['chat_ratio']}x its normal pace"
        if m.get("heat") is not None:
            signal += ("; " if signal else "") + f"most-replayed {m['heat']:.0%}"
        clip = {
            "id": i + 1, "source": m["source"], "creator": SOURCES[m["source"]]["name"],
            "platform": m["platform"], "video_id": m["video_id"], "video_title": m["video_title"],
            "url": m["url"], "moment": fmt_ts(m["t"]), "start": start, "end": end,
            "rank": m["rank"], "signal": signal or "most replayed", "signals": m.get("signals", []),
            "quotes": m["quotes"][:3], "stream_age_h": m["age_h"], "spike": spike,
            "recipe": recipe, "council": {k: v["score"] for k, v in m.get("votes", {}).items()},
            "council_votes": m.get("votes", {}), "council_chair": bool(m.get("council_chair")),
            "council_meeting_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "council_why": m.get("council_why"),
            "post_utc": when.strftime("%Y-%m-%d %H:%M"), "post_local": when.astimezone(LOCAL_TZ).strftime("%a %I:%M %p ET"),
            **meta, "description": description(m, meta, pb), "key": m["key"],
            "permission": SOURCES[m["source"]]["permission"],
        }
        queue.append(clip)
        state["queued"][m["key"]] = {"title": clip["title"], "source": m["source"], "post_utc": clip["post_utc"],
                                     "recipe": recipe, "council": clip["council"]}
        state["used_moments"].append(m["key"])
    state["used_moments"] = state["used_moments"][-500:]

    (out_dir / "queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
    print("Writing scripts...")
    script_agent.script_queue(out_dir, queue)
    for c in queue:
        state["queued"][c["key"]]["title"] = c["title"]
    write_daily(out_dir, queue, ranked, demand, niche, pb, trend_dir, study_dir, args, yt.units,
                not any(c.get("draft") for c in queue))
    save_state(state)
    print(f"Done. Queue: {out_dir / 'queue.md'}  (quota used: {yt.units})")
    return out_dir


def write_daily(out_dir, queue, ranked, demand, niche, pb, trend_dir, study_dir, args, units, llm):
    now = datetime.now(timezone.utc)
    L = [f"# Clip queue, {now.astimezone(LOCAL_TZ):%a %b %d %Y}", "",
         f"Niche **{NICHES[niche]['label']}**. Inputs: trend report `{trend_dir.name}`, channel study `{study_dir.name}`. "
         f"Looked at streams/videos from the last {args.hours}h; found {len(ranked)} unused moments. "
         f"API quota used: {units} units." + ("" if llm else " Some scripts are templates: edit before posting."), "",
         "Scripts written by: " + ", ".join(sorted({c.get("writer") or "template" for c in queue})) + ".", "",
         "Creator demand right now (views/hour on recent clips naming them): "
         + ", ".join(f"{SOURCES[k]['name']} {v:,.0f}" for k, v in sorted(demand.items(), key=lambda kv: -kv[1])), ""]
    for c in queue:
        L += [f"## {c['id']}. {c['title']}", "",
              f"- **Post:** {c['post_local']} ({c['post_utc']} UTC)",
              f"- **Source:** {c['creator']}, [{c['video_title']}]({c['url']}) at **{c['moment']}** "
              f"(stream is {c['stream_age_h']:.0f}h old). Cut {fmt_ts(c['start'])} to {fmt_ts(c['end'])}.",
              f"- **Why:** {c['signal']}. Viewers said: " + " / ".join(f"\"{q}\"" for q in c["quotes"]),
              f"- **Council:** {c.get('council_why') or '-'} (every agent's notes in council.md)",
              f"- **On-screen hook:** {c['hook']}",
              f"- **Context line (first 3s):** {c['context']}",
              f"- **Narrator take (AI voice over the opening):** {c['take']}",
              f"- **Payoff (AI voice over the held last frame):** {c.get('payoff') or '(none)'}",
              f"- **TikTok caption:** {c.get('tiktok_caption', '')}",
              f"- **Edit:** {c['recipe']['length']}s, punch {c['recipe']['punch']}, {c['recipe']['beat']} zooms, "
              f"{c['recipe']['caption_words']}-word captions, cut silences over {c['recipe']['gap']}s, {c['recipe']['layout']}"
              + (f" (testing {c['recipe']['test']})" if c['recipe'].get('test') else " (best settings so far)"),
              f"- **Permission:** {c['permission']}",
              f"- **Make it:** `python channel_builder.py cut {c['id']}` (or `publish` to cut and upload all)",
              "", "Description:", "", "```", c["description"], "```", ""]
    if len(ranked) > len(queue):
        L += ["## Backups", ""]
        for m in ranked[len(queue):len(queue) + 6]:
            L.append(f"- {SOURCES[m['source']]['name']}: [{m['video_title'][:60]}]({m['url']}) at {fmt_ts(m['t'])}, "
                     f"rank {m['rank']}: " + " / ".join(f"\"{q[:60]}\"" for q in m["quotes"][:2]))
        L.append("")
    L += ["## Before you post", "",
          "- Watch the moment first; comment timestamps can be a few seconds off. Trim to the setup + payoff.",
          "- Keep your own layer in every clip (hook text, context line, your voiced take, captions). Since Oct 1, 2026 "
          "YouTube cuts Shorts reach for re-uploads with nothing original added.",
          "- `python channel_builder.py publish` cuts, voices and uploads every clip, scheduled for its post time.",
          "- Then run `python channel_builder.py track` the next day so tomorrow's picks learn from it.", ""]
    (out_dir / "queue.md").write_text("\n".join(L), encoding="utf-8")


def cmd_plan(args):
    yt = BuilderYouTube(os.environ["YOUTUBE_API_KEY"])
    state = load_state()
    trend_dir, rows, videos = trend_inputs()
    by = {r["niche"]: r for r in rows}
    niche = args.niche
    pb, study_dir = playbook(yt, niche, state)
    demand = source_demand(videos, niche)
    cfg = NICHES[niche]
    length = best_length(pb, state)
    slots = post_slots(pb, state, 2)
    per_day = max(2, round(pb["cadence_median"] * 2))

    # Name ideas, checked against existing YouTube handles (1 unit each); only free ones are shown.
    first = ["Late", "Chat", "Vod", "Peak", "Aura", "Loud", "Stream", "Bando", "Lobby", "Crashout"]
    second = ["Recaps", "Rewind", "Replay", "Daily", "Desk", "Files", "Moments", "Vault"]
    names = []
    for i in range(len(first) * 2):
        n = f"{first[i % len(first)]} {second[(i * 3 + i // len(first)) % len(second)]}"
        if n not in [x for x, _ in names]:
            names.append((n, yt.handle_taken("@" + n.replace(" ", "").lower())))
    free = [n for n, taken in names if not taken][:6]

    bench = pb["benchmark_median_views"] or 50_000
    camp = [s for s in SOURCES.values() if s["niche"] == niche and s.get("campaign_cpm")]
    camp_cpm = max((s["campaign_cpm"] for s in camp), default=1.0)
    scen = []
    for label, frac in [("slow start", 0.05), ("typical new channel", 0.25), ("like the young winners", 1.0)]:
        views = per_day * 30 * bench * frac
        scen.append((label, int(bench * frac), int(views), views / 1000 * cfg["shorts_rpm"],
                     views * 0.3 / 1000 * camp_cpm))

    now = datetime.now(timezone.utc)
    others = [r for r in rows[:4] if r["niche"] != niche]
    r = by[niche]
    L = [f"# Channel plan: {cfg['label']}", "",
         f"Built {now:%Y-%m-%d} from trend report `{trend_dir.name}` and channel study `{study_dir.name}`. "
         "Rerun `plan` after new reports to refresh it.", "",
         "## 1. Niche", "",
         f"**{cfg['label']}**: score {r['score']}/100 (#{rows.index(r) + 1} of {len(rows)}), top clips pull "
         f"{r['median_vph_top25']:,.0f} views/hour, {r['small_channel_hit_rate']:.0%} of small channels in the sample hit 100k+, "
         f"reuse risk {r['reuse_risk']:.0%}.",
         "Why this one: the creators stream for hours almost daily and post full VODs, so there is a steady supply of fresh "
         "moments you can clip before anyone else, and fans search the creators by name.", ""]
    if others:
        L += ["Alternatives from the same report: " + "; ".join(
            f"{o['label']} (score {o['score']}, {o['median_vph_top25']:,.0f} views/hr)" for o in others)
            + ". Run `plan --niche <name>` to build for one of them.", ""]

    L += ["## 2. Channel identity", "",
          "Name ideas whose @handle was free on YouTube today: " + (", ".join(f"**{n}** (@{n.replace(' ', '').lower()})" for n in free) or "none, edit the word lists in cmd_plan") + ".",
          "Short, two words, \"<word> Recaps\"-style like the leading channel Nexxo Recaps. Grab the handle the same day you pick it.", "",
          "- **Bio:** \"The best moments from " + ", ".join(SOURCES[k]["name"] for k in sorted(demand, key=lambda k: -demand[k])[:3])
          + " streams, clipped daily. All credit to the creators.\"",
          "- **Look:** one bold font and one accent color across every hook, captions in the same style, "
          "a simple logo (initials), banner listing who you clip.",
          "- **Voice:** a recurring signature, e.g. a 2-4s spoken take at the start of each clip. It's what makes the channel "
          "yours rather than a re-upload account, and YouTube now ranks that difference.", ""]

    top_hours = ", ".join(f"{h:02d}:00 UTC ({datetime.now(timezone.utc).replace(hour=h, minute=0).astimezone(LOCAL_TZ).strftime('%I %p').lstrip('0')} ET)"
                          for h in slots)
    L += ["## 3. Format", "",
          f"- **Length:** about {length}s. The studied channels' median Short is {pb['median_seconds']}s; by lift: "
          + ", ".join(f"{b['bucket']} {b['median_lift']}x" for b in pb["length"]) + ".",
          "- **Structure:** hook text on screen from frame 1, a one-line context caption for the first 3s, your spoken take over "
          "the opening, then setup and payoff. End right on the payoff so it loops.",
          "- **Frame:** 9:16, the stream cropped big in the middle over a blurred copy of itself, word-by-word captions. "
          "`cut` does all of this.", "",
          "## 4. Titles and tags", "",
          f"- Creator's name first, what happens in plain words, ~{pb['title_chars']} characters, 1-2 emoji at the end, "
          "no questions, no ALL CAPS.",
          "- Breakouts to model:"] + [f"  - {w['title']} ({w['views']:,} views, {w['lift']}x)" for w in pb["winners"][:6]] + [
          "- Hashtags: " + ", ".join(h for h, _ in pb["hashtags"][:8]) + ".", ""]

    L += ["## 5. Who to clip", "",
          "| Creator | Demand now (views/hr on clips naming them) | Where full streams are | Permission | Campaign $/1k |",
          "|---|---|---|---|---|"]
    for k in sorted(demand, key=lambda k: -demand[k]):
        s = SOURCES[k]
        where = ", ".join(p for p in ["YouTube" if s.get("youtube") else "", "Twitch" if s.get("twitch") else "", "Kick" if s.get("kick") else ""] if p)
        L.append(f"| {s['name']} | {demand[k]:,.0f} | {where} | {s['permission']} | "
                 f"{'$' + format(s['campaign_cpm'], '.2f') if s.get('campaign_cpm') else '-'} |")
    if pb["sources"]:
        L += ["", "The top channels also clip: " + ", ".join(f"{n} ({k})" for n, k in pb["sources"][:10])
              + ". Add any of them to sources.py."]
    L.append("")

    L += ["## 6. Schedule", "",
          f"- Post {per_day} Shorts a day (top channels post {pb['cadence_median']:.1f}/day; you need volume early to find what works).",
          f"- Best hours by lift in the study: {top_hours}. `track` re-weights these from your own results.",
          "- Daily routine (about an hour): `python channel_builder.py run` in the morning, review the queue, record the takes, "
          "`cut` each pick, schedule them in YouTube Studio.", ""]

    L += ["## 7. Money", "",
          "Two income streams, and early on only one of them is real:", "",
          "1. **Clipping programs and campaigns** pay per view from day one (Whop Content Rewards, Vyro, creators' own "
          "Discords; reported rates $0.40-$3 per 1k views). Join every one that covers your creators and route those clips through it. "
          "Campaigns count views on TikTok and Instagram Reels too, so post every cut on all three.",
          f"2. **Shorts ad revenue** needs the YouTube Partner Program (1,000 subs + 10M Shorts views in 90 days) and pays about "
          f"${cfg['shorts_rpm']} per 1k views in this niche (niches.py estimate).", "",
          f"Scenarios at {per_day} Shorts/day, using {bench:,} median views (young channels in the study) as the top case:", "",
          "| Scenario | Median views per clip | Views / month | Shorts ads $/mo (after YPP) | Campaign $/mo (30% of clips in a campaign) |",
          "|---|---|---|---|---|"]
    for label, mv, views, ads, cmp_ in scen:
        L.append(f"| {label} | {mv:,} | {views:,} | ${ads:,.0f} | ${cmp_:,.0f} |")
    L += ["", "These are scenarios, not forecasts: the views come from channels that already won. Expect weeks of low views "
          "while the algorithm learns the channel. Costs: $0 for the API keys; scripts come from a free local model.", ""]

    L += ["## 8. Rules that keep the channel alive", "",
          "- Clip from the creator's own full stream/VOD, never re-upload another clip channel's Short.",
          "- Add your own layer to every clip (take, hook, context, captions) and credit + link the creator in the description.",
          "- Prefer creators with a clipping program; if a creator or rights holder asks you to take something down, do it. "
          "Three copyright strikes in 90 days ends the channel.",
          "- No music-heavy segments (music claims), no clips of other people's TV/sports broadcasts inside streams.", ""]

    out_dir = BUILDER_DIR / f"plan_{niche}_{now.strftime('%Y-%m-%d_%H%M')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.md").write_text("\n".join(L), encoding="utf-8")
    save_state(state)
    print(f"Done. Plan: {out_dir / 'plan.md'}  (quota used: {yt.units})")


def cmd_track(args):
    yt = BuilderYouTube(os.environ["YOUTUBE_API_KEY"])
    state = load_state()
    ref = args.channel or state.get("channel")
    if not ref:
        sys.exit("Pass --channel @yourhandle the first time.")
    cid = yt.channel_id(ref, state)
    state["channel"] = ref
    now = datetime.now(timezone.utc)
    items = yt.videos(yt.recent_uploads("UU" + cid[2:], n=50))
    vids = [video_record(it, now) for it in items]
    if not vids:
        sys.exit("No uploads on that channel yet.")
    queued = state["queued"]
    titles = {k: v["title"] for k, v in queued.items()}
    by_id = {q["video_id"]: q["source"] for q in queued.values() if q.get("video_id")}
    for v in vids:
        v["vph"] = v["views"] / max(v["age_days"] * 24, 1)
        if v["id"] in by_id:
            v["source"] = by_id[v["id"]]
            continue
        best = max(titles, key=lambda k: difflib.SequenceMatcher(None, titles[k].lower(), v["title"].lower()).ratio(), default=None)
        ratio = difflib.SequenceMatcher(None, titles[best].lower(), v["title"].lower()).ratio() if best else 0
        v["source"] = queued[best]["source"] if ratio >= 0.6 else next(
            (k for k, s in SOURCES.items() if any(a in v["title"].lower() for a in s["aliases"])), None)
    settled = [v for v in vids if v["age_days"] >= 1] or vids  # Shorts get most views in the first 1-3 days
    base = statistics.median(v["views"] for v in settled) or 1
    by_src, by_hour = defaultdict(list), defaultdict(list)
    for v in settled:
        lift = v["views"] / base
        if v["source"]:
            by_src[v["source"]].append(lift)
        by_hour[v["published"].hour].append(lift)
    for k, lifts in by_src.items():
        if len(lifts) >= 2:
            old = state["source_weight"].get(k, 1.0)
            state["source_weight"][k] = round(0.6 * old + 0.4 * min(max(statistics.median(lifts), 0.3), 3), 2)
    for h, lifts in by_hour.items():
        if len(lifts) >= 2:
            old = state["slot_weight"].get(str(h), 1.0)
            state["slot_weight"][str(h)] = round(0.6 * old + 0.4 * min(max(statistics.median(lifts), 0.3), 3), 2)
    state["own_results"] = [{"title": v["title"], "views": v["views"], "lift": round(v["views"] / base, 2)}
                            for v in settled[:30]]  # script_agent writes toward the titles that worked
    save_state(state)

    pb_file = latest(f"builder/playbook_channels_{args.niche}_*.json")
    bench = json.loads(pb_file.read_text(encoding="utf-8")).get("benchmark_median_views") if pb_file else None
    trend = latest("[0-9]*_*/niches.json")
    study_run = latest("channels_*/channels.json")
    L = [f"# Channel check, {now:%Y-%m-%d}", "",
         f"{ref}: {len(vids)} recent uploads, median {base:,.0f} views (uploads at least a day old)"
         + (f" vs {bench:,} for young channels that are winning in the niche." if bench else "."), "",
         "## Your uploads", "", "| Title | Creator | Age | Views | Views/hr | vs your median |", "|---|---|---|---|---|---|"]
    for v in sorted(vids, key=lambda v: v["published"], reverse=True)[:25]:
        L.append(f"| [{v['title']}](https://www.youtube.com/shorts/{v['id']}) | "
                 f"{SOURCES[v['source']]['name'] if v['source'] else '?'} | {v['age_days']:.1f}d | {v['views']:,} | "
                 f"{v['vph']:,.0f} | {v['views'] / base:.1f}x |")
    L += ["", "## What the builder learned", ""]
    for k, lifts in sorted(by_src.items(), key=lambda kv: -statistics.median(kv[1])):
        L.append(f"- {SOURCES[k]['name']}: {len(lifts)} clips, median {statistics.median(lifts):.1f}x your median; "
                 f"weight now {state['source_weight'].get(k, 1.0)}")
    good_h = sorted(((h, statistics.median(l)) for h, l in by_hour.items() if len(l) >= 2), key=lambda x: -x[1])[:3]
    if good_h:
        L.append("- Best posting hours so far (UTC): " + ", ".join(f"{h:02d}:00 ({x:.1f}x)" for h, x in good_h))
    if bench and base < bench * 0.05 and len(settled) >= 20:
        L.append("- After 20+ clips you're under 5% of the niche benchmark: rerun `plan` and consider the runner-up niche.")
    L += ["", "## Are the inputs fresh?", ""]
    for name, run, limit, cmd in [("Trend report", trend, 3, f"python agent.py --niches {args.niche} --days 3"),
                                  ("Channel study", study_run, 7, f"python channel_agent.py --niche {args.niche}")]:
        age = run_age_days(run.parent) if run else 999
        L.append(f"- {name}: {age:.0f} days old" + (f". Refresh: `{cmd}`" if age > limit else ", fine."))
    out = BUILDER_DIR / f"track_{now.strftime('%Y-%m-%d_%H%M')}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Done. Report: {out}  (quota used: {yt.units})")


def refresh_inputs(niche):
    """Refresh trend and channel-study reports when stale, for daily and spike runs."""
    here = Path(__file__).parent
    trend = latest("[0-9]*_*/niches.json")
    if not trend or run_age_days(trend.parent) > 0.8:  # every day: what's viral and who's spiking right now
        r = subprocess.run([sys.executable, str(here / "agent.py"), "--niches", niche, "--days", "3"])
        if r.returncode and not trend:
            sys.exit("Trend scan failed and there's no earlier one to use.")
    study_run = latest(f"channels_{niche}_*/channels.json")
    if not study_run or run_age_days(study_run.parent) > 0.8:  # refresh the channel study every day for current viral examples
        subprocess.run([sys.executable, str(here / "channel_agent.py"), "--niche", niche], check=True)


def cmd_run(args):
    """Daily driver: refresh the other agents' reports when they're stale, then build the queue."""
    refresh_inputs(args.niche)
    state = load_state()
    if state.get("channel"):
        cmd_track(argparse.Namespace(channel=None, niche=args.niche))
    cmd_daily(args)
    if args.publish:
        cmd_publish(argparse.Namespace(queue=None, ids=None, layout=None, whisper="small.en", voice=args.voice,
                                       narrate=args.narrate, skip_gate=False))


# ---------- cutting ----------

def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_escape(s):
    s = EMOJI.sub("", s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " "))
    return re.sub(r"[\U000E0000-\U000E007F\U0001F000-\U0001F2FF]", "", s).strip()  # flags/tags render as boxes


def caption_chunks(words, size=3):
    """1-`size` word chunks, broken on pauses and punctuation."""
    chunks, chunk = [], []
    for i, w in enumerate(words):
        chunk.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if len(chunk) >= size or not nxt or nxt["start"] - w["end"] > 0.35 or re.search(r"[.!?,]$", w["word"]):
            chunks.append((chunk, nxt))
            chunk = []
    return chunks


def write_ass(path, words, hook, context, length, offset=0.0, size=3, payoff=None):
    """Captions: 1-3 word chunks, big with a heavy outline, the word being spoken in yellow; hook pinned top;
    context line for the first 3.5s."""
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,8,4,2,50,50,640,1
Style: Hook,Arial,74,&H0000E5FF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,18,0,8,60,60,150,1
Style: Ctx,Arial,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,0,0,0,0,100,100,0,0,3,12,0,8,80,80,215,1
Style: Pay,Arial,66,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,16,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Hook,,0,0,0,,{ass_escape(hook.upper())}")
    if context:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.5, length))},Ctx,,0,0,0,,{ass_escape(context)}")
    if payoff and payoff[0]:  # our closing line, shown over the held last frame while the narrator says it
        ev.append(f"Dialogue: 1,{ass_time(payoff[1])},{ass_time(length)},Pay,,0,0,0,,{ass_escape(payoff[0])}")
    for chunk, nxt in caption_chunks(words, size):
        texts = [ass_escape(c["word"].strip().upper()) for c in chunk]
        chunk_end = min((nxt["start"] if nxt else chunk[-1]["end"] + 0.3) + offset, length)
        for j, w in enumerate(chunk):  # one event per word, so the highlight moves with the speech
            s = max(w["start"] + offset, 0) if j else max(chunk[0]["start"] + offset, 0)
            e = min(chunk[j + 1]["start"] + offset, chunk_end) if j + 1 < len(chunk) else chunk_end
            line = " ".join(r"{\c&H00E5FF&}" + t + r"{\c&HFFFFFF&}" if k == j else t for k, t in enumerate(texts) if t)
            if e > s and line:
                ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{line}")
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


def dead_air(words, length, gap=0.6, pad=0.15):
    """Jump cuts: the stretches of silence longer than `gap` between words (keeping `pad` either side).
    Returns the (start, end) spans to keep. Silence before the first and after the last word stays, since
    that is usually the action or the reaction."""
    keep, s = [], 0.0
    for a, b in zip(words, words[1:]):
        if b["start"] - a["end"] > gap:
            keep.append((s, a["end"] + pad))
            s = b["start"] - pad
    keep.append((s, length))
    return [(a, min(b, length)) for a, b in keep if min(b, length) > a]


def retime(t, keep):
    """Where second t of the source lands once only the `keep` spans are left."""
    out = 0.0
    for a, b in keep:
        if t <= b:
            return out + max(t - a, 0)
        out += b - a
    return out


def zoom_expr(beats, length, push=0.06, punch=0.14):
    """Scale factor over time: a slow push-in across the clip, punching in/out on each beat so the frame
    changes every couple of seconds and jump cuts don't look like glitches."""
    toggles = "+".join(f"gte(t,{b:.2f})" for b in beats) or "0"
    return f"1+{push}*t/{max(length, 1):.2f}+{punch}*mod({toggles},2)"


def zoom_beats(words, cuts, min_gap=1.5, max_gap=2.5, size=3):
    """Moments to punch in or out: every jump cut, plus a caption-chunk start whenever the frame has held
    for `max_gap` seconds. Never closer than `min_gap`."""
    cuts = {round(c, 2) for c in cuts}
    starts = sorted(cuts | {round(ch[0]["start"], 2) for ch, _ in caption_chunks(words, size)})
    beats, last = [], 0.0
    for t in starts:
        if t - last < min_gap:
            continue
        if t in cuts or t - last >= max_gap:
            beats.append(t)
            last = t
    return beats


def whisper_model_dir(name):
    """Download the whisper model with plain requests (huggingface_hub's own downloader crashes on some
    Python 3.14 setups) into ~/.cache/clip-builder/whisper/<name>; returns the folder."""
    if Path(name).is_dir():
        return name
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(name, f"Systran/faster-whisper-{name}")
    except ImportError:
        repo = f"Systran/faster-whisper-{name}"
    out = Path.home() / ".cache" / "clip-builder" / "whisper" / name
    out.mkdir(parents=True, exist_ok=True)
    for f in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json", "preprocessor_config.json"):
        dest = out / f
        if dest.exists():
            continue
        r = requests.get(f"https://huggingface.co/{repo}/resolve/main/{f}", stream=True, timeout=60)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    if not (out / "model.bin").exists():
        sys.exit(f"Couldn't download the whisper model {name} from huggingface.co/{repo}.")
    return str(out)


def transcribe(path, model_name):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed: no captions (pip install faster-whisper).")
        return []
    print(f"Transcribing with whisper {model_name} (first run downloads the model)...")
    model = WhisperModel(whisper_model_dir(model_name), device="cpu", compute_type="int8")  # GPU needs CUDA libs most PCs lack
    # decode with ffmpeg ourselves: faster-whisper's own decoder breaks with newer PyAV releases
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    segs, _ = model.transcribe(np.frombuffer(pcm, np.float32), word_timestamps=True, vad_filter=True)
    return [{"word": w.word, "start": w.start, "end": w.end} for s in segs for w in (s.words or [])]


KOKORO_DIR = Path.home() / ".cache" / "clip-builder" / "kokoro"
KOKORO_FILES = {  # Kokoro v1.0 (Apache-2.0): an 82M-parameter open voice model, the most human-sounding free one
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
NARRATOR_VOICE = os.environ.get("NARRATOR_VOICE", "am_fenrir")  # Kokoro voice; an en-US-... name means edge-tts


SAY_AS = {"N3on": "Neon", "xQc": "X Q C", "Jynxzi": "Jinxy", "CaseOh": "Case Oh", "PlaqueBoyMax": "Plaque Boy Max",
          "IShowSpeed": "I Show Speed", "Kai Cenat": "Kai Senat"}


# Voice polish: cut rumble and hiss, a little low-mid warmth and presence like a real mic, gentle compression, a very
# small room so it isn't studio-dry, then loudness matched to the stream.
VOICE_CHAIN = ("highpass=f=85,lowpass=f=11500,equalizer=f=180:t=q:w=1:g=2,equalizer=f=3200:t=q:w=1.2:g=2.5,"
               "acompressor=threshold=-20dB:ratio=3:attack=5:release=120,aecho=0.9:0.5:18|31:0.1|0.06,"
               "loudnorm=I=-15:TP=-1.5:LRA=7")


def speakable(text):
    """The take as a person would say it: no emoji or hashtags, no ALL-CAPS words (voices spell those out),
    symbols said as words, and a full stop so the voice lands the line instead of trailing off."""
    text = EMOJI.sub("", re.sub(r"[#@]\w+", "", text or ""))
    text = re.sub(r"\b[A-Z]{2,}\b", lambda m: m.group(0) if m.group(0) in ("OK", "TV", "NBA", "NFL", "GTA", "IRL")
                  else m.group(0).lower(), text)
    text = re.sub(r"\$(\d[\d,]*)\s?[kK]\b", lambda m: f"{m.group(1)} thousand dollars", text)
    text = re.sub(r"\$(\d[\d,]*)", lambda m: f"{m.group(1)} dollars", text)
    for name, said in SAY_AS.items():  # streamer names voices get wrong
        text = re.sub(rf"\b{re.escape(name)}\b", said, text, flags=re.I)
    text = re.sub(r"\bvs\.?\b", "versus", text, flags=re.I).replace("&", " and ").replace('"', "")
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text + ("" if not text or text[-1] in ".!?" else ".")


def kokoro_voice(text, out, voice, speed=1.08):
    """Kokoro on the CPU (a couple of seconds a line). Downloads the model (~350 MB) once into the cache."""
    import soundfile
    from kokoro_onnx import Kokoro
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        f = KOKORO_DIR / name
        if not f.exists() or f.stat().st_size < 1_000_000:
            print(f"Downloading the narrator voice model ({name})...")
            tmp = f.with_suffix(".part")
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            tmp.replace(f)
    k = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
    samples, sr = human_read(k, text, voice, speed)
    soundfile.write(str(out), samples, sr)
    return out


def human_read(k, text, voice, speed=1.08):
    """Read the line phrase by phrase the way a person talks: each phrase at a slightly different pace (a touch quicker
    on a question or exclamation, slower on the last words), with short, uneven breaths between phrases instead of
    the model's evenly spaced pauses. Seeded by the text, so the same line always comes out the same."""
    import random
    import numpy as np
    rnd = random.Random(text)
    lang = "en-us" if voice[:1] == "a" else "en-gb"
    phrases = [p for p in re.split(r"(?<=[,.!?;:])\s+", text) if p.strip()]
    out, sr = [], 24000
    for i, ph in enumerate(phrases):
        last = i == len(phrases) - 1
        pace = speed * rnd.uniform(0.96, 1.06) * (1.04 if ph[-1:] in "!?" else 1.0) * (0.96 if last else 1.0)
        audio, sr = k.create(ph, voice=voice, speed=round(pace, 3), lang=lang)
        loud = np.flatnonzero(np.abs(audio) > 0.01)  # trim the model's own padding, then add our own gap
        if loud.size:
            audio = audio[max(0, loud[0] - int(0.02 * sr)): loud[-1] + int(0.06 * sr)]
        out.append(audio)
        if not last:
            gap = rnd.uniform(0.28, 0.42) if ph[-1] in ".!?" else rnd.uniform(0.1, 0.2)
            out.append(np.zeros(int(gap * sr), dtype=audio.dtype))
    return np.concatenate(out), sr


REF_LINE = ("Okay so, this is the part everybody's been talking about. Honestly, I did not see this coming, "
            "and neither did chat. Just watch.")
_TURBO = []


def chatterbox_voice(text, out, voice):
    """Chatterbox Turbo (MIT, runs on the CPU in about 30s a line): natural rises and falls, and it can chuckle.
    It speaks in the Kokoro voice picked for the channel, cloned from a reference line rendered once by Kokoro, so
    the channel keeps one recognisable voice."""
    import random
    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    ref = KOKORO_DIR / f"ref_{voice}.wav"
    if not ref.exists():
        one = kokoro_voice(REF_LINE, KOKORO_DIR / f"ref1_{voice}.wav", voice)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(one), "-i", str(one), "-filter_complex",
                        "[0:a][1:a]concat=n=2:v=0:a=1", "-ar", "24000", "-ac", "1", str(ref)], check=True)
    if not _TURBO:
        _TURBO.append(ChatterboxTurboTTS.from_pretrained(device="cpu"))
    m = _TURBO[0]
    rnd = random.Random(text)
    said = text
    if "," in text and rnd.random() < 0.4:  # a chuckle after the first phrase now and then, like a person reacting
        head, tail = text.split(",", 1)
        said = f"{head}, [chuckle]{tail}"
    wav = m.generate(said, audio_prompt_path=str(ref))
    torchaudio.save(str(out), wav, m.sr)
    return out


def release_voice():
    """Drop the Chatterbox model before the render: the cloud runner has ~7 GB of RAM, and holding the voice model
    while ffmpeg encodes (with Ollama loaded too) got the runner killed mid-render on Nov 6-7."""
    if _TURBO:
        import gc
        _TURBO.clear()
        gc.collect()


def audio_seconds(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(path)], capture_output=True, text=True).stdout.strip() or 0)


def narrate(text, out_base, voice=None):
    """Speak the take in the most human free voice available: Chatterbox Turbo in the channel's voice, then Kokoro
    (an open model that sounds like a person talking, read phrase by phrase), then edge-tts (Microsoft neural voices),
    then macOS `say`, then espeak-ng. Returns the audio path, or None if no voice is available. The mix (EQ,
    compression, ducking) happens in cut. NARRATOR_ENGINE=kokoro skips Chatterbox."""
    text = speakable(text)
    if len(text) < 3:
        return None
    voice = voice or NARRATOR_VOICE
    if not voice.startswith("en-") and os.environ.get("NARRATOR_ENGINE", "chatterbox").lower() == "chatterbox":
        try:
            out = chatterbox_voice(text, out_base.with_suffix(".take.wav"), voice)
            if out.exists() and out.stat().st_size > 1000:
                return out
        except Exception as e:
            print(f"Chatterbox voice failed ({type(e).__name__}: {str(e)[:120]}); using Kokoro.")
            import traceback
            traceback.print_exc()
    if not voice.startswith("en-"):
        try:
            out = kokoro_voice(text, out_base.with_suffix(".take.wav"), voice)
            if out.exists() and out.stat().st_size > 1000:
                return out
        except Exception as e:
            print(f"Kokoro voice failed ({type(e).__name__}: {str(e)[:120]}); trying edge-tts.")
        voice = "en-US-AndrewMultilingualNeural"
    try:
        import asyncio
        import edge_tts
        out = out_base.with_suffix(".take.mp3")
        asyncio.run(edge_tts.Communicate(text, voice, rate="+6%").save(str(out)))
        if out.exists() and out.stat().st_size > 1000:
            return out
    except Exception as e:
        print(f"edge-tts failed ({type(e).__name__}); trying a local voice.")
    for tool, cmd in [("say", lambda o: ["say", "-o", str(o), text]),
                      ("espeak-ng", lambda o: ["espeak-ng", "-s", "165", "-w", str(o), text])]:
        if shutil.which(tool):
            out = out_base.with_suffix(".take.aiff" if tool == "say" else ".take.wav")
            if subprocess.run(cmd(out)).returncode == 0 and out.exists():
                return out
    print("No text-to-speech available (pip install kokoro-onnx soundfile): clip will have no narrator.")
    return None


def face_x(path, samples=15):
    """Where the main face sits across the clip, as a fraction of the width (median of the biggest face in
    a few sampled frames), or None without OpenCV or a face."""
    try:
        import cv2
    except ImportError:
        return None
    if not hasattr(cv2, "CascadeClassifier"):  # OpenCV 5 moved the face cascades out
        return None
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = det.detectMultiScale(gray, 1.1, 6, minSize=(gray.shape[0] // 12, gray.shape[0] // 12))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + w / 2) / gray.shape[1])
    cap.release()
    return statistics.median(xs) if len(xs) >= max(3, samples // 4) else None


def cmd_cut(args):
    for tool in ["ffmpeg", "ffprobe"]:
        if not shutil.which(tool):
            sys.exit(f"{tool} is not installed.")
    qdir = Path(args.queue) if args.queue else max(BUILDER_DIR.glob("daily_*"), key=lambda p: p.stat().st_mtime, default=None)
    if not qdir:
        sys.exit("No queue yet. Run `python channel_builder.py daily` first.")
    queue = json.loads((qdir / "queue.json").read_text(encoding="utf-8"))
    clip = next((c for c in queue if c["id"] == args.id), None)
    if not clip:
        sys.exit(f"No item {args.id} in {qdir / 'queue.json'}")
    if "blocked" in SOURCES.get(clip["source"], {}).get("permission", "").lower():
        sys.exit(f"{clip['creator']} is marked blocked in sources.py.")
    start = args.start if args.start is not None else clip["start"]
    end = args.end if args.end is not None else clip["end"]
    length = end - start
    out_dir = qdir / "clips"
    out_dir.mkdir(exist_ok=True)
    base = out_dir / f"{clip['id']:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', clip['key'])}"
    raw = base.with_suffix(".source.mp4")

    if raw.exists():
        pass  # already downloaded on an earlier try
    elif args.video:  # you already have the footage (e.g. downloaded through a clipping program)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-i", args.video, "-t", str(length),
                        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(raw)], check=True)
    else:
        if not shutil.which("yt-dlp"):
            sys.exit("yt-dlp is not installed (pip install yt-dlp), or pass --video with the source file.")
        if clip["platform"] in ("twitch_clip", "kick_clip"):  # a clip from a stream that may still be live: take it whole
            print(f"Downloading {clip['platform'].split('_')[0].title()} clip {clip['url']}")
            subprocess.run(["yt-dlp", "-q", "-f", "b[height<=1080]/b", "--merge-output-format", "mp4", "-o", str(raw),
                            clip["url"]], check=True)
        else:
            src_url = (f"https://www.youtube.com/watch?v={clip['video_id']}" if clip["platform"] == "youtube"
                       else f"https://www.twitch.tv/videos/{clip['video_id']}")
            print(f"Downloading {fmt_ts(start)}-{fmt_ts(end)} of {src_url}")
            subprocess.run(["yt-dlp", "-q", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--download-sections",
                            f"*{start}-{end}", "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
                            "-o", str(raw), src_url], check=True)

    words = json.loads(Path(args.transcript).read_text(encoding="utf-8")) if args.transcript else (
        [] if args.no_captions else transcribe(raw, args.whisper))
    if words and not args.no_script and clip.get("script_source") != "transcript":
        print("Re-writing the script from what's actually said in the clip...")  # hook/context get burned in below
        script_agent.script_queue(qdir, queue, ids=[clip["id"]], transcripts={clip["id"]: "".join(w["word"] for w in words).strip()})
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json",
                                       str(raw)], capture_output=True, text=True).stdout or "{}").get("streams", [])
    vs = next((st for st in probe if st.get("codec_type") == "video"), {})
    has_audio = any(st.get("codec_type") == "audio" for st in probe)

    # Retention edit: cut dead air between lines, and punch the frame in/out every couple of seconds so the
    # picture keeps changing. --plain turns both off.
    plain = getattr(args, "plain", False)
    recipe = {**tuner.default_recipe(), **(clip.get("recipe") or {})}  # the edit tuner's settings for this clip
    speech = originality.speech_share(words, length) if words else None  # measured before jump cuts remove silence
    layout = args.layout or recipe["layout"]
    keep = dead_air(words, length, gap=recipe["gap"]) if words and not plain else [(0.0, length)]
    if len(keep) > 1:
        words = [dict(w, start=retime(w["start"], keep), end=retime(w["end"], keep)) for w in words]
        cuts = [retime(b, keep) for _, b in keep[:-1]]
        print(f"Cut {length - sum(b - a for a, b in keep):.1f}s of dead air ({len(cuts)} jump cuts).")
        length = sum(b - a for a, b in keep)
    else:
        cuts = []
    # The channel's own voice is what makes a stream cut a clip rather than a reupload: a setup line over the
    # opening and, after the moment lands, a payoff line (our verdict or question) over the held last frame, so the
    # take never talks over the moment itself. Always on unless --no-narrate.
    voices, tail = [], 0.0
    if has_audio and not getattr(args, "no_narrate", False):
        if not args.voiceover and clip.get("take"):
            args.voiceover = narrate(clip["take"], base, args.voice)
        if args.voiceover:
            voices.append((args.voiceover, 0.25))
        if clip.get("payoff"):
            pay = narrate(clip["payoff"], base.with_name(base.name + "_payoff"), args.voice)
            if pay:
                voices.append((pay, length + 0.2))
                tail = audio_seconds(pay) + 0.6
        release_voice()  # free the voice model's RAM before ffmpeg renders (runner OOM, Nov 6-11)
    ass = base.with_suffix(".ass")
    write_ass(ass, words, clip["hook"], clip["context"], length + tail, size=recipe["caption_words"],
              payoff=(clip.get("payoff"), length) if tail else None)

    n = len(keep)
    if n > 1:
        trims = "".join(f"[v{i}]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[s{i}];" for i, (a, b) in enumerate(keep))
        src = f"[0:v]fps=30,split={n}" + "".join(f"[v{i}]" for i in range(n)) + ";" + trims
        if has_audio:
            src += f"[0:a]asplit={n}" + "".join(f"[a{i}]" for i in range(n)) + ";" + "".join(
                f"[a{i}]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.02,"
                f"afade=t=out:st={max(b - a - 0.02, 0):.3f}:d=0.02[t{i}];" for i, (a, b) in enumerate(keep))
        src += "".join(f"[s{i}]" + (f"[t{i}]" if has_audio else "") for i in range(n))
        src += f"concat=n={n}:v=1:a={int(has_audio)}[src]" + ("[srca];" if has_audio else ";")
        vin, ain = "[src]", "[srca]"
    else:
        src, vin, ain = "", "[0:v]", "[0:a]"

    # 9:16: blurred full-frame background + the stream cropped/scaled big in the middle. face34 (default)
    # is a tall 3:4 crop that follows the streamer's face and fills three quarters of the screen.
    sw, sh = vs.get("width") or 1920, vs.get("height") or 1080
    cw = {"face34": min(sw, sh * 3 / 4), "crop43": min(sw, sh * 4 / 3), "fit": sw, "fill": min(sw, sh * 9 / 16)}[layout]
    fw, fh = (1080, 1920) if layout == "fill" else (1080, int(round(1080 * sh / cw / 2)) * 2)
    if layout == "face34":
        fx = face_x(raw)
        cx = (fx if fx is not None else 0.5) * sw - cw / 2
        fg = f"crop={int(cw) // 2 * 2}:ih:{int(min(max(cx, 0), sw - cw))}:0"
        print(f"Framing on the face at {fx:.0%} across." if fx is not None else "No face found; framing the centre.")
    else:
        fg = {"crop43": "crop='min(iw,ih*4/3)':ih", "fit": "null", "fill": "crop='min(iw,ih*9/16)':ih"}[layout]
    fg += f",scale={fw}:{fh}"
    if not plain:
        lo, hi = tuner.BEATS.get(recipe["beat"], (1.5, 2.5))
        z = zoom_expr(zoom_beats(words, cuts, lo, hi, recipe["caption_words"]), length, punch=recipe["punch"])
        fg += f",scale=w='2*trunc({fw}*({z})/2)':h='2*trunc({fh}*({z})/2)':eval=frame,crop={fw}:{fh}"
    vf = (src + f"{vin}split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=40,"
          f"eq=brightness=-0.12[bg];[b]{fg}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
          + (f"tpad=stop_mode=clone:stop_duration={tail:.2f}," if tail else "") +  # hold the last frame for the payoff
          f"subtitles='{ass.resolve().as_posix().replace(':', chr(92) + ':')}'[v]")  # escape C: on Windows
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw)]
    if voices:  # our voice over the stream, stream audio ducked under the opening line
        for path, _ in voices:
            cmd += ["-i", str(path)]
        # Sound like someone talking over the stream, not a voice pasted on top: the voice comes in a beat late,
        # gets the low rumble cut, gentle compression and a level matched to the stream, and the stream dips
        # under it smoothly and swells back over half a second instead of snapping.
        lead = voices[0][1] if voices[0][1] < 1 else 0.0
        d = (audio_seconds(voices[0][0]) + lead) if lead else 0.0
        duck = (f"if(lt(t,{lead}),1-0.7*t/{lead},if(lt(t,{d:.2f}),0.3,if(lt(t,{d + 0.5:.2f}),0.3+0.7*(t-{d:.2f})/0.5,1)))"
                if lead else "1")
        af = f"{ain}volume='{duck}':eval=frame" + (f",apad=pad_dur={tail:.2f}" if tail else "") + "[s];"
        for i, (_, at) in enumerate(voices, 1):
            ms = int(at * 1000)
            af += f"[{i}:a]aresample=48000,{VOICE_CHAIN},adelay={ms}|{ms},volume=1.1[vo{i}];"
        af += "[s]" + "".join(f"[vo{i}]" for i in range(1, len(voices) + 1)) + \
              f"amix=inputs={len(voices) + 1}:duration=first:normalize=0[aud]"
        cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[aud]"]
    elif n > 1 and has_audio:
        cmd += ["-filter_complex", vf, "-map", "[v]", "-map", ain]
    else:
        cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "0:a?"]
    out = base.with_suffix(".mp4")
    cmd += ["-t", str(length + tail), "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    meta = base.with_suffix(".txt")
    meta.write_text(f"TITLE\n{clip['title']}\n\nDESCRIPTION\n{clip['description']}\n\nPOST AT\n{clip['post_local']} "
                    f"({clip['post_utc']} UTC)\n" + (f"\nSPEECH\n{speech}\n" if speech is not None else "") +
                    f"\nVOICES\n{len(voices)}\n" + (f"\nTRANSCRIPT\n{''.join(w['word'] for w in words).strip()}\n" if words else ""), encoding="utf-8")
    print(f"Done. Video: {out}\nTitle/description: {meta}")
    return out


# ---------- uploading ----------

UPLOAD_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILE = Path.home() / ".config" / "clip-builder" / "youtube_token.json"


def youtube_uploader():
    """Signed-in YouTube client for your channel. First run opens a browser to approve; the token is
    kept in ~/.config/clip-builder/ (outside the project folder) and refreshed automatically."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("pip install google-api-python-client google-auth-oauthlib")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), UPLOAD_SCOPES) if TOKEN_FILE.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # Google ends sign-ins after 7 days while the OAuth app is in Testing
            if not sys.stdin or not sys.stdin.isatty():
                sys.exit(f"YouTube sign-in expired ({str(e)[:80]}). Run `python channel_builder.py auth` once to sign in again.")
            creds = None
    if not creds or not creds.valid:
        secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", Path(__file__).parent / "client_secret.json"))
        if not secrets.exists():
            sys.exit(f"No OAuth client at {secrets}. Create one (README: Uploading) and save it there.")
        creds = InstalledAppFlow.from_client_secrets_file(str(secrets), UPLOAD_SCOPES).run_local_server(port=0)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_clip(api, path, clip):
    from googleapiclient.http import MediaFileUpload
    src = SOURCES.get(clip["source"], {})
    publish_at = datetime.strptime(clip["post_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    scheduled = publish_at > datetime.now(timezone.utc) + timedelta(minutes=20)
    status = {"privacyStatus": "private" if scheduled else "public", "selfDeclaredMadeForKids": False}
    if scheduled:
        status["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "snippet": {"title": clip["title"][:100], "description": clip["description"][:4900],
                    "tags": clip.get("upload_tags") or list(dict.fromkeys(src.get("aliases", []) + ["streamer clips", "shorts"]))[:15],
                    "categoryId": "24"},  # Entertainment
        "status": status,
    }
    req = api.videos().insert(part="snippet,status", body=body,
                              media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 << 20))
    resp, tries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except OSError as e:  # dropped connection mid-upload: the upload is resumable, so wait and pick up where it left off
            tries += 1
            if tries > 5:
                raise
            print(f"  upload hiccup ({type(e).__name__}), retrying...")
            time.sleep(5 * tries)
    return resp["id"], scheduled


def cmd_auth(args):
    """One-time YouTube sign-in (opens a browser), then shows which channel uploads will go to."""
    api = youtube_uploader()
    me = api.channels().list(part="snippet", mine=True).execute().get("items", [])
    print(f"Signed in. Uploads go to: {me[0]['snippet']['title']}" if me else
          "Signed in, but this Google account has no YouTube channel yet: create one at youtube.com first.")


# ---------- pre-post check ----------
# Nothing posts until every agent has done its job: the trend scan and channel study are fresh, the review agent
# went over the channel today, and each clip has a real script and clean content.

SLURS = re.compile(r"\b(n[i1!]gg(?:a|as|az|er|ers|uh)|f[a@]gg?(?:ot|ots|s)?|k[i1]kes?|sp[i1]c(?:k)?s?|ch[i1]nks?"
                   r"|tr[a@]nn(?:y|ies)|r[e3]tard(?:s|ed)?|wetbacks?)\b", re.I)


def agents_ready(niche):
    """What's missing before anything may post (empty list = every agent did its job)."""
    problems = []
    trend = latest("[0-9]*_*/niches.json")
    if not trend or run_age_days(trend.parent) > 3:
        problems.append("trend agent: no trend report from the last 3 days")
    study = latest(f"channels_{niche}_*/channels.json")
    if not study or run_age_days(study.parent) > 10:  # the daily run refreshes it after 5 days
        problems.append("channel study: no channel study from the last 10 days")
    at = (load_state().get("review") or {}).get("at")
    age_h = ((datetime.now(timezone.utc) - datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc))
             .total_seconds() / 3600) if at else None
    if age_h is None or age_h > 36:
        problems.append("review agent: hasn't reviewed the channel in the last 36 hours (see review.log)")
    return problems


def clip_problem(qdir, c):
    """Why this cut clip can't post (None = fine): the script agent's work and the clip's content."""
    if c.get("draft"):
        return "script agent: only a fill-in template, no writer ran"
    missing = [k for k in ("title", "hook", "take") if not (c.get(k) or "").strip()]
    if missing:
        return f"script agent: no {', '.join(missing)}"
    said, speech = "", None
    for txt in (qdir / "clips").glob(f"{c['id']:02d}_*.txt"):
        body = txt.read_text(encoding="utf-8")
        said += body.split("\nTRANSCRIPT\n", 1)[1] if "\nTRANSCRIPT\n" in body else ""
        m = re.search(r"\nSPEECH\n([0-9.]+)", body)
        speech = float(m.group(1)) if m else speech
        v = re.search(r"\nVOICES\n(\d+)", body)
        if v and v.group(1) == "0":
            return "originality: the narrator voice didn't render, so this would be a bare reupload of the stream"
    text = " ".join([c.get(k) or "" for k in ("title", "hook", "context", "take", "description")] + [said])
    if SLURS.search(text):
        return "content: a slur is said or shown in this clip"
    import free_writer
    shown = " ".join(c.get(k) or "" for k in ("title", "hook", "context", "take", "tiktok_caption"))
    if free_writer.risky(shown):  # last check before posting: our own text never claims a crime, violence or drugs
        return f"content: the title/hook/caption says \"{free_writer.RISKY.search(shown).group(0)}\", which gets clips taken down"
    # Reused-content and copyright checks: our own words on it, not a repeat, no music (originality.py)
    problems = originality.clip_problems(c, originality.posted_titles(load_state()), speech, said)
    return "; ".join(problems) or None


def hold(qdir, problems, next_step="Fix these and run `python channel_builder.py publish` again."):
    note = qdir / "HOLD.md"
    note.write_text("# Posting held\n\n" + "\n".join(f"- {p}" for p in problems) +
                    f"\n\n{next_step}\n", encoding="utf-8")
    print("Posting held, nothing uploaded:\n  " + "\n  ".join(problems) + f"\n  ({note})")


def council_problem(clip):
    """A post needs a recent, completed vote from every council agent in its queue record."""
    meeting_at = clip.get("council_meeting_at")
    try:
        meeting_time = datetime.strptime(meeting_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "council: no timestamped meeting for this clip"
    age = (datetime.now(timezone.utc) - meeting_time).total_seconds()
    if age < -300 or age > 24 * 60 * 60:
        return "council: this clip's meeting is more than 24 hours old; convene again before posting"
    votes = clip.get("council_votes")
    if not isinstance(votes, dict):
        return "council: this queue has no recorded meeting from every agent"
    missing = [council.AGENTS[k] for k in council.AGENTS if not isinstance(votes.get(k), dict)]
    if missing:
        return "council: missing votes from " + ", ".join(missing)
    incomplete, failed = [], []
    for key, agent in council.AGENTS.items():
        vote = votes[key]
        score, why = vote.get("score"), vote.get("why")
        if not isinstance(score, (int, float)) or not 0 <= score <= 1 or not isinstance(why, str) or not why.strip():
            incomplete.append(agent)
        elif why.startswith("(couldn't judge:"):
            failed.append(agent)
    if incomplete:
        return "council: incomplete vote from " + ", ".join(incomplete)
    if failed:
        return "council: agent couldn't evaluate this clip: " + ", ".join(failed)
    return None


def cmd_publish(args):
    """Cut (narrate with --narrate) and upload every clip in the queue that isn't on YouTube yet."""
    qdir = Path(args.queue) if args.queue else max(BUILDER_DIR.glob("daily_*"), key=lambda p: p.stat().st_mtime, default=None)
    if not qdir:
        sys.exit("No queue yet. Run `python channel_builder.py daily` first.")
    qfile = qdir / "queue.json"
    queue = json.loads(qfile.read_text(encoding="utf-8"))
    state = load_state()
    tt = state.get("tiktok", {})
    picked = [c for c in queue if not args.ids or c["id"] in args.ids]
    council_problems = [f"[{c['id']}] {problem}" for c in picked if (problem := council_problem(c))]
    if council_problems:
        hold(qdir, council_problems,
             "Regenerate the queue with `daily` or `spike` so every agent can meet, then publish the new queue.")
        sys.exit(1)
    todo = [c for c in picked if not c.get("youtube_id")]
    if not todo:
        print("Everything in this queue is already on YouTube.")
        if tt.get("enabled"):
            post_tiktok(qdir, queue, picked, tt)
        post_meta(qdir, queue, picked)
        return
    niche = SOURCES.get(todo[0]["source"], {}).get("niche", "streamer")
    problems = [] if getattr(args, "skip_gate", False) else agents_ready(niche)
    if problems:
        hold(qdir, problems)
        sys.exit(1)
    api = youtube_uploader()
    for c in todo:
        print(f"[{c['id']}] {c['title']}")
        try:
            path = cmd_cut(argparse.Namespace(
                id=c["id"], queue=str(qdir), start=None, end=None, video=None, voiceover=None, layout=args.layout,
                whisper=args.whisper, transcript=None, no_captions=False, voice=args.voice, narrate=args.narrate, no_script=False))
            c.update(next(x for x in json.loads(qfile.read_text(encoding="utf-8")) if x["id"] == c["id"]))  # cut's new script
            problem = None if getattr(args, "skip_gate", False) else clip_problem(qdir, c)
            if problem:
                c["held"] = problem
                qfile.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
                print(f"  held, not posted: {problem}")
                continue
            c.pop("held", None)
            vid, scheduled = upload_clip(api, path, c)
        except subprocess.CalledProcessError as e:
            print(f"  skipped: cutting failed ({e}). Try the backups in queue.md.")
            continue
        except Exception as e:
            if "uploadLimitExceeded" in str(e) or "quotaExceeded" in str(e):
                print("  YouTube's daily upload limit is hit; the rest go up on the next run.")
                break
            print(f"  skipped: upload failed ({type(e).__name__}: {str(e)[:200]}). It goes up on the next run.")
            continue
        c["youtube_id"] = vid
        state["queued"].setdefault(c["key"], {}).update(video_id=vid, title=c["title"], source=c["source"])
        if qdir.name.startswith("spike_"):  # counts toward the spike checks' daily limit
            day = datetime.now(LOCAL_TZ).date().isoformat()
            state.setdefault("spike_posts", {})[day] = state["spike_posts"].get(day, 0) + 1
        qfile.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
        save_state(state)
        print(f"  uploaded https://youtube.com/shorts/{vid} "
              + (f"(goes public {c['post_local']})" if scheduled else "(public now)"))
    if tt.get("enabled"):
        post_tiktok(qdir, queue, picked, tt)
    post_meta(qdir, queue, picked)


def post_meta(qdir, queue, clips):
    """Instagram Reels and Facebook Reels for every clip that went to YouTube (on when the META_* secrets are set)."""
    import meta_upload
    if not meta_upload.enabled():
        return
    qfile = qdir / "queue.json"
    for c in clips:
        path = qdir / "clips" / f"{c['id']:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', c['key'])}.mp4"
        if not c.get("youtube_id") or not path.exists():
            continue
        for k, v in meta_upload.post(path, c).items():
            if k.endswith("_error"):
                print(f"  {k.split('_')[0].title()} skipped for [{c['id']}]: {v}")
            else:
                c[k] = v
                print(f"  {k.split('_')[0].title()}: [{c['id']}] posted ({v})")
        qfile.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")


def post_tiktok(qdir, queue, clips, tt):
    """Send every rendered clip that isn't on TikTok yet (enabled with `tiktok-auth --enable`)."""
    import tiktok_upload
    qfile = qdir / "queue.json"
    inbox = tt.get("mode", "inbox") == "inbox"
    pending = 0
    if inbox:
        pending, unknown = tiktok_upload.pending_inbox_drafts(BUILDER_DIR)
        if unknown:
            print("  TikTok inbox paused: couldn't verify recent draft status; clear or check the drafts, then retry.")
            return
        if pending >= tiktok_upload.MAX_PENDING_INBOX_DRAFTS:
            print(f"  TikTok inbox paused: {pending} recent drafts are still pending. Finish or clear drafts before sending more.")
            return
    for c in clips:
        path = qdir / "clips" / f"{c['id']:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', c['key'])}.mp4"
        # TikTok is an independent destination: a YouTube upload failure or quota
        # hold must not prevent an approved, rendered clip from reaching TikTok.
        if c.get("tiktok_id") or not path.exists():
            continue
        if inbox and pending >= tiktok_upload.MAX_PENDING_INBOX_DRAFTS:
            print(f"  TikTok inbox paused: safety limit is {tiktok_upload.MAX_PENDING_INBOX_DRAFTS} pending drafts; finish some before sending more.")
            break
        try:
            c["tiktok_id"] = tiktok_upload.upload(path, c, tt.get("mode", "inbox"))
        except Exception as e:  # TikTok trouble never blocks YouTube
            print(f"  TikTok skipped for [{c['id']}]: {str(e)[:200]}")
            if "spam_risk" in str(e) or "rate_limit" in str(e):
                break
            continue
        c["tiktok_caption"] = tiktok_upload.caption(c)
        qfile.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
        if inbox:
            pending += 1
        print(f"  TikTok: [{c['id']}] " + ("posted" if tt.get("mode") == "direct" else
              "draft in your TikTok inbox. Open the notification, paste this caption, post:") +
              f"\n    {c['tiktok_caption']}")


def cmd_spike(args):
    """Every few hours: if something on a creator's stream is going nuts right now (chat flood, a Twitch clip
    racing up), clip it and post it straight away instead of waiting for tomorrow's run."""
    state = load_state()
    today = datetime.now(LOCAL_TZ).date().isoformat()
    done = state.setdefault("spike_posts", {})
    for d in [d for d in done if d < today]:
        del done[d]
    left = args.max_per_day - done.get(today, 0)
    if left <= 0:
        print(f"Already posted {done[today]} spike clips today; the limit is {args.max_per_day}.")
        return
    refresh_inputs(args.niche)
    qdir = cmd_daily(argparse.Namespace(niche=args.niche, count=min(left, args.count), hours=18, length=args.length, spike=True),
                     state=state)
    if not qdir:
        return
    if args.publish:
        cmd_publish(argparse.Namespace(queue=str(qdir), ids=None, layout=None, whisper="small.en", voice=args.voice,
                                       narrate=False, skip_gate=False))


def cmd_tiktok_auth(args):
    """Sign in to TikTok once; --enable turns cross-posting on in the daily run, --disable turns it off."""
    state = load_state()
    tt = state.setdefault("tiktok", {"enabled": False, "mode": "inbox"})
    if args.mode:
        tt["mode"] = args.mode
    if args.disable:
        tt["enabled"] = False
    else:
        import tiktok_upload
        if not tiktok_upload.TOKEN_FILE.exists() or args.relogin:
            tiktok_upload.login(tt["mode"])
        if args.enable:
            tt["enabled"] = True
    save_state(state)
    print(f"TikTok cross-posting: {'ON' if tt['enabled'] else 'off'}, mode {tt['mode']}.")


def cmd_tiktok_status(args):
    """What TikTok did with each clip sent from the last few queues."""
    import tiktok_upload
    queues = sorted((q for q in BUILDER_DIR.glob("*_20*") if (q / "queue.json").exists()), key=lambda p: p.stat().st_mtime)[-args.days:]
    sent = 0
    for qdir in queues:
        for c in json.loads((qdir / "queue.json").read_text(encoding="utf-8")):
            if not c.get("tiktok_id"):
                print(f"{qdir.name} [{c['id']}] not sent to TikTok" + (f" (held: {c['held']})" if c.get("held") else ""))
                continue
            sent += 1
            try:
                st = tiktok_upload.status(c["tiktok_id"])
                print(f"{qdir.name} [{c['id']}] {st.get('status')}" + (f" - {st['fail_reason']}" if st.get("fail_reason") else ""))
            except Exception as e:
                print(f"{qdir.name} [{c['id']}] status check failed: {str(e)[:200]}")
    if not sent:
        print("Nothing has been sent to TikTok from these queues.")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["plan", "daily", "run", "track"]:
        p = sub.add_parser(name)
        p.add_argument("--niche", default="streamer", choices=sorted({s["niche"] for s in SOURCES.values()}))
        if name in ("daily", "run"):
            p.add_argument("--count", type=int, default=2, help="clips to queue (2 a day: fewer, better clips get more reach)")
            p.add_argument("--hours", type=int, default=168, help="look at streams/videos from the last N hours")
            p.add_argument("--length", type=int, help="clip length in seconds (default: from the channel study)")
        if name == "run":
            p.add_argument("--publish", action="store_true", help="also cut and upload the queue")
            p.add_argument("--narrate", action="store_true", help="add the AI narrator over the opening (off by default)")
            p.add_argument("--voice", default=None, help="narrator voice (Kokoro name, or en-US-... for edge-tts)")
        if name == "track":
            p.add_argument("--channel", help="your channel @handle or UC... id (remembered after the first run)")
    p = sub.add_parser("spike", help="clip and post anything going nuts on a stream right now")
    p.add_argument("--niche", default="streamer", choices=sorted({s["niche"] for s in SOURCES.values()}))
    p.add_argument("--count", type=int, default=1, help="clips per check")
    p.add_argument("--max-per-day", type=int, default=2, help="spike clips per day at most (YouTube upload quota)")
    p.add_argument("--length", type=int, help="clip length in seconds (default: the edit tuner's)")
    p.add_argument("--publish", action="store_true", help="cut and post right away")
    p.add_argument("--voice", default=None)
    p = sub.add_parser("cut")
    p.add_argument("id", type=int, help="queue item number")
    p.add_argument("--queue", help="queue folder (default: latest daily_*)")
    p.add_argument("--start", type=float, help="override start second in the source")
    p.add_argument("--end", type=float, help="override end second in the source")
    p.add_argument("--video", help="use a local source file instead of downloading")
    p.add_argument("--voiceover", help="audio file with your take, mixed over the opening")
    p.add_argument("--layout", choices=["face34", "crop43", "fit", "fill"], help="framing (default: the edit tuner's pick, face34)")
    p.add_argument("--whisper", default="small.en", help="faster-whisper model for captions")
    p.add_argument("--transcript", help="word timings JSON [{word,start,end}] instead of whisper")
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--no-narrate", action="store_true", help="no narrator lines (the clip is then a bare reupload; don't post it)")
    p.add_argument("--narrate", action="store_true", help="(always on now; kept so old commands still work)")
    p.add_argument("--voice", default=None, help="narrator voice: a Kokoro voice (am_michael, am_puck, af_heart...) or an edge-tts name (en-US-...)")
    p.add_argument("--no-script", action="store_true", help="keep the queue's script, don't re-write it from the transcript")
    p.add_argument("--plain", action="store_true", help="no jump cuts over dead air and no punch-in zooms")
    sub.add_parser("auth")
    p = sub.add_parser("tiktok-auth")
    p.add_argument("--mode", choices=["inbox", "direct"], help="inbox = drafts you post in the app; direct = posts itself")
    p.add_argument("--enable", action="store_true", help="post every published clip to TikTok too")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--relogin", action="store_true")
    p = sub.add_parser("tiktok-status")
    p.add_argument("--days", type=int, default=7, help="how many recent queues to check")
    p = sub.add_parser("publish")
    p.add_argument("ids", type=int, nargs="*", help="queue items (default: all not yet uploaded)")
    p.add_argument("--queue", help="queue folder (default: latest daily_*)")
    p.add_argument("--layout", choices=["face34", "crop43", "fit", "fill"], help="framing (default: the edit tuner's pick, face34)")
    p.add_argument("--whisper", default="small.en")
    p.add_argument("--narrate", action="store_true", help="add the AI narrator over the opening")
    p.add_argument("--voice", default=None)
    p.add_argument("--skip-gate", action="store_true", help="skip freshness/content checks; council votes are still required")
    args = ap.parse_args()

    if args.cmd not in ("cut", "publish", "auth", "tiktok-auth", "tiktok-status") and not os.environ.get("YOUTUBE_API_KEY"):
        sys.exit("Set YOUTUBE_API_KEY first.")
    {"plan": cmd_plan, "daily": cmd_daily, "track": cmd_track, "run": cmd_run, "cut": cmd_cut, "spike": cmd_spike,
     "publish": cmd_publish, "auth": cmd_auth,
     "tiktok-auth": cmd_tiktok_auth, "tiktok-status": cmd_tiktok_status}[args.cmd](args)


if __name__ == "__main__":
    main()

