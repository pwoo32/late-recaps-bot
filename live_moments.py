"""Finds the moments that went nuts on a creator's live stream, without any AI model or paid service.

Three signals, all free:
- Chat spikes:    the stream's live chat replay (yt-dlp). When chat suddenly floods ("LMAOOO", "NO WAY", "CLIP IT",
                  💀💀💀), something just happened. Chat reacts a few seconds late, so the moment is set just before the
                  flood starts. This is how the big auto-clippers find moments.
- Most replayed:  YouTube's "most replayed" graph for the VOD (yt-dlp reads it from the watch page). Used to boost
                  moments that sit in a hot part of the stream, and as moments of its own on shorter videos.
- Twitch clips:   viewers clip what's going off while the stream is still live. A Twitch clip racking up views fast is
                  a live moment going viral; it can be downloaded straight away, before the VOD exists.

All of it runs through yt-dlp and Twitch's free API, so it uses no YouTube Data API quota.
"""

import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

HYPE = re.compile(r"\b(?:lma+o+|lmfao|omegalul|kekw|lul|no way|na+h+|bro+|wha+t+|wtf|holy|clip (?:it|that)|clipped|"
                  r"insane|crazy|w+|l+|ayo+|yo+)\b|😂|🤣|💀|😭|💯|🔥|😱|😳|\?\?\?|!!!", re.I)
BIN = 5  # seconds per chat bin


def _yt_dlp():
    return shutil.which("yt-dlp")


# ---------- chat spikes ----------

def chat_messages(video_id, budget_s=240):
    """[(seconds, text)] from a YouTube stream's live chat replay. Stops after budget_s and keeps what it got, so a
    10-hour stream with a huge chat doesn't stall the run (you get the first part of the stream)."""
    if not _yt_dlp():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "c"
        try:
            subprocess.run([_yt_dlp(), "-q", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs",
                            "live_chat", "-o", str(base), f"https://www.youtube.com/watch?v={video_id}"],
                           capture_output=True, timeout=budget_s)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return []
        files = sorted(Path(tmp).glob("c*live_chat*"))
        out = []
        for f in files:
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # the last line of a cut-off download
                r = d.get("replayChatItemAction") or {}
                t = int(r.get("videoOffsetTimeMsec") or 0) / 1000
                for a in r.get("actions") or []:
                    item = (a.get("addChatItemAction") or {}).get("item") or {}
                    msg = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                    if not msg:
                        continue
                    text = "".join(run.get("text") or (run.get("emoji") or {}).get("shortcuts", [""])[0]
                                   for run in (msg.get("message") or {}).get("runs", []))
                    out.append((t, text))
        return out


def chat_spikes(messages, duration=None, top=6, lag=6):
    """Moments where chat floods: messages per 5s, weighted up for hype words/emoji, compared with the stream's
    usual pace around that time. Returns [{t, ratio, msgs, sample}] best first."""
    if len(messages) < 200:
        return []
    end = max(t for t, _ in messages)
    n = int(end // BIN) + 1
    counts, hype, samples = [0.0] * n, [0.0] * n, [[] for _ in range(n)]
    for t, text in messages:
        i = int(t // BIN)
        if i < 0 or i >= n:
            continue
        counts[i] += 1
        if HYPE.search(text):
            hype[i] += 1
        if len(samples[i]) < 30:
            samples[i].append(text)
    heat = [c + 1.5 * h for c, h in zip(counts, hype)]
    # 20s rolling sum, against the median of the surrounding 10 minutes (streams get busier as they go)
    roll = [sum(heat[i:i + 4]) for i in range(n)]
    out, w = [], 60
    for i in range(n):
        lo, hi = max(0, i - w), min(n, i + w)
        base = statistics.median(roll[lo:hi]) or 1
        ratio = roll[i] / base
        if ratio >= 2.5 and sum(counts[i:i + 4]) >= 12:
            out.append((ratio, i))
    out.sort(reverse=True)
    picked = []
    for ratio, i in out:
        t = max(0, i * BIN - lag)  # chat floods a few seconds after the moment
        if all(abs(t - p["t"]) > 90 for p in picked):
            sample = [s for s in samples[i] + (samples[i + 1] if i + 1 < n else []) if HYPE.search(s)][:5]
            picked.append({"t": int(t), "ratio": round(ratio, 1), "msgs": int(sum(counts[i:i + 4])), "sample": sample})
        if len(picked) == top:
            break
    if duration:
        picked = [p for p in picked if 30 <= p["t"] <= duration - 10]
    return picked


# ---------- most replayed ----------

def video_info(video_id, timeout=90):
    """yt-dlp's metadata for a video: heatmap (most replayed), live status, duration. {} if it fails."""
    if not _yt_dlp():
        return {}
    try:
        r = subprocess.run([_yt_dlp(), "-J", "--no-warnings", "--skip-download", f"https://www.youtube.com/watch?v={video_id}"],
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


def heat_at(heatmap, t):
    """Most-replayed value (0..1) at second t, or None without a heatmap."""
    for seg in heatmap or []:
        if seg["start_time"] <= t < seg["end_time"]:
            return seg["value"]
    return None


def heat_peaks(heatmap, top=3, max_seg=90):
    """Peaks of the most-replayed graph, as moments, when its segments are short enough to cut from."""
    if not heatmap or heatmap[0]["end_time"] - heatmap[0]["start_time"] > max_seg:
        return []
    segs = sorted(heatmap[3:], key=lambda s: -s["value"])  # the first seconds are always "replayed"
    out = []
    for s in segs:
        if s["value"] < 0.5:
            break
        t = int((s["start_time"] + s["end_time"]) / 2)
        if all(abs(t - o["t"]) > 60 for o in out):
            out.append({"t": t, "value": s["value"]})
        if len(out) == top:
            break
    return out


# ---------- Twitch ----------

def twitch_headers():
    cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        return None
    tok = requests.post("https://id.twitch.tv/oauth2/token", timeout=30, data=dict(
        client_id=cid, client_secret=secret, grant_type="client_credentials")).json()["access_token"]
    return {"Client-Id": cid, "Authorization": f"Bearer {tok}"}


def twitch_user(h, login):
    users = requests.get("https://api.twitch.tv/helix/users", params={"login": login}, headers=h, timeout=30).json()
    return users["data"][0]["id"] if users.get("data") else None


def twitch_live(h, user_id):
    """The stream if the creator is live right now, else None."""
    d = requests.get("https://api.twitch.tv/helix/streams", params={"user_id": user_id}, headers=h, timeout=30).json()
    return (d.get("data") or [None])[0]


def twitch_clips(h, user_id, since, first=20):
    d = requests.get("https://api.twitch.tv/helix/clips", headers=h, timeout=30, params=dict(
        broadcaster_id=user_id, started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"), first=first)).json()
    return d.get("data", [])


_GAME_NAMES = {}


def twitch_game_names(h, ids):
    """{game_id: name} for Twitch category ids (cached), so a clip's game can be matched by name ("Grand Theft Auto VI")."""
    want = [i for i in {i for i in ids if i} if i not in _GAME_NAMES]
    for k in range(0, len(want), 100):
        try:
            d = requests.get("https://api.twitch.tv/helix/games", params={"id": want[k:k + 100]}, headers=h, timeout=30).json()
        except Exception:
            break
        _GAME_NAMES.update({g["id"]: g["name"] for g in d.get("data", [])})
    return {i: _GAME_NAMES[i] for i in ids if i in _GAME_NAMES}


def twitch_clips_keyless(login, since, timeout=120):
    """The same clip list without Twitch API keys: yt-dlp reads the channel's public clips page (top clips of the last
    7 days), and we keep the ones made since `since`. Same fields as twitch_clips(), minus the VOD link, so each one
    is used as-is (downloaded whole)."""
    return clips_page(f"https://www.twitch.tv/{login}/clips?filter=clips&range=7d", since, timeout)


def kick_clips(slug, since, timeout=120):
    """A Kick channel's most-viewed clips of the last week (yt-dlp, no keys; needs curl_cffi to get past Kick's
    bot check). Same fields as twitch_clips_keyless()."""
    return clips_page(f"https://kick.com/{slug}/clips?sort=view&range=week", since, timeout)


def clips_page(url, since, timeout=120):
    """[{id, url, title, view_count, created_at, duration}] from a public clips page, newer than `since`."""
    if not _yt_dlp():
        return []
    try:
        r = subprocess.run([_yt_dlp(), "-J", "--flat-playlist", "--no-warnings", "--playlist-end", "40", url],
                           capture_output=True, text=True, timeout=timeout)
        entries = (json.loads(r.stdout).get("entries") or []) if r.returncode == 0 and r.stdout.strip() else []
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    out = []
    for e in entries:
        if not e or not e.get("url") or not e.get("timestamp"):
            continue
        made = datetime.fromtimestamp(e["timestamp"], timezone.utc)
        if made < since:
            continue
        out.append({"id": e.get("id") or e["url"].rstrip("/").rsplit("/", 1)[-1], "url": e["url"],
                    "title": e.get("title") or "", "view_count": e.get("view_count") or 0,
                    "created_at": made.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration": e.get("duration") or 30})
    return out


def clip_velocity(clip, now=None):
    """Views per hour since the clip was made: how fast a moment is spreading."""
    now = now or datetime.now(timezone.utc)
    made = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    return clip["view_count"] / max((now - made).total_seconds() / 3600, 0.25)


def spike_score(ratio):
    """Chat spike ratio on the same scale as comment-timestamp scores (sum of log2 likes, usually 5-30)."""
    return round(6 * math.log2(1 + ratio), 2)
