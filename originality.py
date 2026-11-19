"""Originality and copyright checks: what keeps a clip out of the Shorts feed's "reused content" bin.

Nov 2026 review: 47 Shorts, median 1 view after two days, one Content ID claim. The clips were mostly raw stream
audio from the creators' own YouTube uploads, with one stock narrator sentence over the opening and stock titles
("Kai Cenat Goes Crazy 🔥", posted twice). YouTube matches that footage to the creator's upload and treats our copy as
a duplicate, so the feed never tests it. These checks stop the patterns that got us there:

- stock titles, hooks and narrator lines that could sit on any clip (nothing of our own added)
- a title we already used (near-duplicate uploads)
- music in the moment (songs and DJ sets are what Content ID claims hardest, e.g. the Speed IRL claim)
- clips with hardly any speech (music, crowd noise or gameplay audio: nothing for our voice to work with)
- produced, edited uploads from the creator's main channel (their own edit, registered in Content ID), as opposed
  to raw stream VODs
- streams that already earned a claim, and streams we've already clipped twice

No API calls, no model. channel_builder.py uses these when ranking moments and right before upload.
"""

import os
import re
from datetime import datetime, timedelta, timezone

# Lines that could go on any clip. Matched case-insensitively against the title, hook and narrator lines.
STOCK = re.compile(r"\b(goes crazy|goes off|can'?t believe (?:what just happened|it|he pulled this off)|didn'?t see this coming|"
                   r"is left speechless|loses it|completely loses it|can'?t stop laughing|can'?t hold it in|crashes out|"
                   r"is fuming|gets emotional|has a wholesome moment|shows his soft side|wait for it|wait for the end|"
                   r"watch his face|watch the last second|nobody saw this coming|did he really do that|he called it|"
                   r"this went wrong fast|he didn'?t think this through|this one hits different|"
                   r"listen to .{1,30}'s exact words|had no idea what was about to happen|keep your eyes on|"
                   r"this is why chat can'?t stop clipping|nobody in the room saw this coming|really thought this was a good idea|"
                   r"you won'?t believe|watch (?:this|till the end)|going viral)\b", re.I)

MUSIC = re.compile(r"♪|♫|\[music\]|\(music\)|\b(song|songs|music|dubstep|edm|singing|sings|sang|singer|concert|"
                   r"karaoke|dj set|playlist|spotify|beat drop|the beat|rapping|freestyle|lyrics|album|"
                   r"chorus|band)\b", re.I)

STREAM_WORDS = re.compile(r"\b(stream|streams|streaming|live|vod|irl|marathon|subathon|day \d+|hour \d+|until i)\b", re.I)

# The free writer's fill-in narration ("This is about X; Speed's reaction is what makes the moment land."). YouTube's
# Oct 1 2026 Shorts update names template-based changes and voice-over that only describes the screen as adding
# nothing, so these count as no narration at all.
TEMPLATE_VO = re.compile(r"(sets up the moment|listen to what follows|is the story here|what makes the moment land|"
                         r"the line everything turns on|you'?re about to see why|watch how .{1,30} handles it|"
                         r"this exchange turns on|rate (?:that|how) .{1,40} (?:out of ten|one to ten)|comments decide|"
                         r"drop your answer below|or not even close|worth it or not)", re.I)

MIN_SPEECH = 0.25        # share of the clip with someone talking
MIN_TAKE_WORDS = 12      # the opening take has to be a real sentence of our own, not a label
MIN_SAID_WORDS = 20      # a 5-second clip with 7 words in it gives the narrator nothing to comment on
MAX_CLIPS_PER_STREAM = 2  # posted clips from one source video, ever


def stock_phrase(*texts):
    """The first stock phrase in any of the texts, or None."""
    for t in texts:
        m = STOCK.search(t or "")
        if m:
            return m.group(0)
    return None


def norm_title(t):
    """'Kai Cenat Goes Crazy 🔥' and 'kai cenat goes crazy!' compare equal."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).split())


def music_hit(*texts):
    m = MUSIC.search(" ".join(t or "" for t in texts))
    return m.group(0) if m else None


def speech_share(words, length):
    """Share of the clip covered by spoken words (whisper timings), 0-1."""
    if not words or not length:
        return 0.0
    spans = sorted((max(w["start"], 0), min(w["end"], length)) for w in words if w["end"] > w["start"])
    total, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return round(min(total / length, 1.0), 2)


def produced_upload(title, seconds, was_live):
    """An edited video from the creator's main channel rather than a stream VOD: never live, under 90 minutes,
    and nothing in the title says stream. Those edits are registered in Content ID and are the creator's own
    product; cutting them up is a reupload, not a clip."""
    return not was_live and seconds < 90 * 60 and not STREAM_WORDS.search(title or "")


def posted_titles(state):
    return {norm_title(q.get("title")) for q in (state.get("queued") or {}).values() if q.get("video_id")}


_COMMON = set("""that this with what when they them then from have just will your about after before still really
going cant can't didn't doesn't don't won't believe reacts reaction moment stream streamer clip clips chat live gets
goes makes takes finally again completely totally actually instantly can't never ever every crazy insane wild while
tries trying watches meets says said tells asks finds sees hears sudden suddenly right after first time last""".split())


def moment_words(clip):
    """The distinctive words of a moment (names, things, places) from the source title, quotes and our script:
    what stays the same when the same moment is found again under another clip, VOD or upload."""
    from sources import SOURCES
    names = {w.lower() for w in (SOURCES.get(clip.get("source")) or {}).get("aliases", []) for w in w.split()}
    text = " ".join([clip.get(k) or "" for k in ("title", "hook", "context", "video_title")] + list(clip.get("quotes") or []))
    return sorted({w for w in re.findall(r"[a-z][a-z']{3,}", text.lower()) if w not in _COMMON and w not in names})


def same_moment(clip, state, days=21):
    """Title of an upload from the last `days` days that is this same moment (same creator, most of the distinctive
    words shared), else None. Oct-Nov 2026: the Speed dubstep moment went up 4 times from different VODs and clips,
    which is exactly the repeat-upload pattern YouTube's reused-content rule throttles."""
    mine = set(moment_words(clip))
    if len(mine) < 2:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    for q in (state.get("queued") or {}).values():
        if not q.get("video_id") or q.get("source") != clip.get("source") or (q.get("post_utc") or "9") < cutoff:
            continue
        theirs = set(q.get("words") or moment_words({"title": q.get("title"), "source": q.get("source")}))
        shared = mine & theirs
        if len(shared) >= 2 and len(shared) >= 0.4 * min(len(mine), len(theirs)):
            return q.get("title")
    return None


def clip_problems(clip, posted, speech=None, transcript="", state=None):
    """Reasons this clip adds too little of our own or risks a claim (empty list = fine)."""
    out = []
    phrase = stock_phrase(clip.get("title"), clip.get("hook"), clip.get("take"), clip.get("payoff"))
    if phrase:
        out.append(f"originality: stock line \"{phrase}\" could go on any clip; the script needs something specific to this one")
    if norm_title(clip.get("title")) in posted:
        out.append("originality: this title was already used on an earlier upload (near-duplicate)")
    twin = same_moment(clip, state) if state else None
    if twin:
        out.append(f"originality: this looks like the same moment as an earlier upload (\"{twin}\"); posting it twice "
                   "reads as reused content")
    take = (clip.get("take") or "").strip()
    if not take:
        out.append("originality: no narrator line, so nothing of the channel's own is added")
    elif len(take.split()) < MIN_TAKE_WORDS:
        out.append(f"originality: the narrator's take is only {len(take.split())} words; it needs a real opinion or context")
    tv = TEMPLATE_VO.search(" ".join([take, clip.get("payoff") or ""]))
    if tv:
        out.append(f"originality: template narration (\"{tv.group(0)}\"); YouTube counts template voice-overs as adding nothing")
    if clip.get("writer") in ("free", "template") and not os.environ.get("ALLOW_TEMPLATE_NARRATION"):
        out.append("originality: the script came from the fill-in writer, not a model, so the narration is a template "
                   "(set GEMINI_API_KEY or GROQ_API_KEY, both free)")
    if transcript and len(transcript.split()) < MIN_SAID_WORDS:
        out.append(f"originality: only {len(transcript.split())} words are said in the clip, too little to comment on")
    hit = music_hit(transcript, " ".join(clip.get("quotes") or []), clip.get("title"), clip.get("video_title"))
    if hit:
        out.append(f"copyright: music in this moment (\"{hit}\"); songs get Content ID claims")
    if speech is not None and speech < MIN_SPEECH:
        out.append(f"copyright: only {speech:.0%} of the clip is speech (likely music or noise, and nothing to react to)")
    return out
