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

import re

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

MIN_SPEECH = 0.25        # share of the clip with someone talking
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


def clip_problems(clip, posted, speech=None, transcript=""):
    """Reasons this clip adds too little of our own or risks a claim (empty list = fine)."""
    out = []
    phrase = stock_phrase(clip.get("title"), clip.get("hook"), clip.get("take"), clip.get("payoff"))
    if phrase:
        out.append(f"originality: stock line \"{phrase}\" could go on any clip; the script needs something specific to this one")
    if norm_title(clip.get("title")) in posted:
        out.append("originality: this title was already used on an earlier upload (near-duplicate)")
    if not (clip.get("take") or "").strip():
        out.append("originality: no narrator line, so nothing of the channel's own is added")
    hit = music_hit(transcript, " ".join(clip.get("quotes") or []), clip.get("title"), clip.get("video_title"))
    if hit:
        out.append(f"copyright: music in this moment (\"{hit}\"); songs get Content ID claims")
    if speech is not None and speech < MIN_SPEECH:
        out.append(f"copyright: only {speech:.0%} of the clip is speech (likely music or noise, and nothing to react to)")
    return out
