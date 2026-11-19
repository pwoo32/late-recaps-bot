"""Clip council: every agent weighs in on today's candidate moments before anything is picked.

The builder finds candidate moments; then each agent looks at the shortlist from its own angle, scores every
candidate 0-1 and says why in a line:

- Trend researcher (agent.py):      is this creator hot right now, rising since the last scan, and does the moment
                                    match what's going viral in their clips this week?
- Channel study (channel_agent.py): do the top channels in the niche clip this creator, and does the moment fit the
                                    kind of clip that breaks out for them?
- Analytics reviewer (review_agent): how did this creator's clips spread on OUR channel, do moments found this way
                                    (Twitch clip, chat spike...) get tested by the feed, and does
                                    the moment follow the review's lessons (a spoken premise: a bet, a question, a
                                    challenge; fresh, so it posts within hours of the stream)?
- Live scout (live_moments.py):     how hard did the stream react: chat flood, most-replayed, Twitch clip velocity,
                                    timestamp comments and likes?
- Script writer (script_agent.py):  can it write a strong title and hook for it: is a guest named, is there a line
                                    worth quoting, does it have captions to work from?
- Audience (channel_activity_agent): are people commenting on our channel asking for this creator?

The votes are combined with each agent's weight. Then a free local model (Ollama) chairs the meeting: it reads
every agent's comments on the top candidates and picks the final clips, saying why. Without Ollama, the vote
decides. Everything is written to council.md in the day's queue folder.

The weights learn: once a clip has settled, review_agent.py credits the agents whose votes matched how the clip
actually did and discounts the ones that didn't (`learn`). No Claude, nothing paid.
"""

import json
import math
import os
import re
import tempfile
from pathlib import Path

import requests

import free_writer
import script_agent
from sources import SOURCES

AGENTS = {
    "trend": "Trend researcher",
    "study": "Channel study",
    "analytics": "Analytics reviewer",
    "live": "Live scout",
    "script": "Script writer",
    "audience": "Audience",
}
DEFAULT_WEIGHTS = {"trend": 1.0, "study": 0.7, "analytics": 1.0, "live": 1.3, "script": 0.8, "audience": 0.5}
PREMISE = re.compile(r"\$\d|\b(1v1|bet|challenge|vs\.?|versus|race|guess|rate|first time|if (?:i|he|you) (?:lose|win)|"
                     r"loser|winner|dare|prank|reacts?|meets?|tries|test)\b|\?", re.I)


# Moments tied to news people search for: our only Short past 50 views (Nov 13, "Jynxzi Wins Gamer Of The Year At
# The Streamer Awards") named an event and got 85% of its views from outside the Shorts feed; the runner-up named a
# guest (Frost Children). Vague reaction moments ("Kai Cenat Goes Crazy") never got a feed test.
NEWS = re.compile(r"\b(awards?|wins?|won|winner|champions?|championship|record|world record|announce\w*|reveal\w*|"
                  r"debut|finale|tournament|final|birthday|wedding|signs?|signed|banned|unbanned|returns to|comeback|first ever|"
                  r"collab|meets?|ft\.?|feat\.?|with|x|vs\.?|versus|interview\w*|premiere|launch\w*|"
                  r"gamer of the year|streamer of the year|goty)\b", re.I)
_TITLE_FILLER = set("""I A The And Or But Of In On At To For With Is Are Was My Me He His She Her They We You It This That
What Why How When Who LIVE Live Stream Day Part Chat W L Ep EP Clip IRL Can Return Serve From MPH Poor
Pushing Legends Div Gamer Year Wins Of Interviews""".split())


def news_hook(m):
    """What makes this moment news people would look up, or None: an event word, or a named guest (a full name,
    two capitalised words in a row, in the source title that isn't the creator's)."""
    title = m.get("video_title") or ""
    hit = NEWS.search(title) or NEWS.search(" ".join(m.get("quotes") or []))
    word = hit.group(0).lower() if hit else None
    if word and word not in ("with", "x", "meets", "meet", "vs", "vs.", "versus", "ft", "ft.", "feat", "feat."):
        return word
    own = {a.lower() for a in SOURCES.get(m.get("source"), {}).get("aliases", []) for a in a.split()}
    filler = {w.lower() for w in _TITLE_FILLER} | script_agent.REACTION_WORDS | free_writer.STOP
    filler |= {"asks", "plays", "reacts", "watches", "tries", "pushing", "update", "new", "agent"}
    collab = word is not None  # "x", "with", "meets", "vs": an all-caps title only counts a guest after one of these
    words = re.findall(r"[A-Za-z][A-Za-z']*", title)
    for a, b in zip(words, words[1:]):
        if (a.isupper() or b.isupper()) and not collab:
            continue
        if a[0].isupper() and b[0].isupper() and not {a.lower(), b.lower()} & (filler | own) and len(a) > 2 and len(b) > 2:
            return f"guest {a} {b}"
    return None


def weights(state):
    w = dict(DEFAULT_WEIGHTS)
    w.update((state.get("council") or {}).get("weights") or {})
    return w


def _clip01(x):
    return max(0.0, min(1.0, x))


# ---------- the agents ----------

def trend_vote(m, ctx):
    demand, top = ctx["demand"].get(m["source"], 0), max(ctx["demand"].values() or [0]) or 1
    rise = ctx["change"].get(m["source"], 1.0)
    text = " ".join(m["quotes"] + [m["video_title"]]).lower()
    hits = sorted(w for w in ctx["words"].get(m["source"], ()) if w in text)
    s = _clip01(0.15 + 0.55 * demand / top + 0.2 * (rise - 1) + 0.1 * min(len(hits), 3))
    why = f"{SOURCES[m['source']]['short']} clips pull {demand:,.0f} views/hour this week" + (
        f", {'up' if rise >= 1 else 'down'} {abs(rise - 1):.0%} since the last scan" if abs(rise - 1) >= 0.1 else "")
    if hits:
        why += f"; matches what's viral now: {', '.join(hits[:3])}"
    return s, why


def study_vote(m, ctx):
    pb = ctx["pb"]
    src = SOURCES[m["source"]]
    names = [a.lower() for a in src["aliases"]]
    srcs = [x for x in pb.get("sources") or [] if isinstance(x, (list, tuple))]
    clipped = sum(n for name, n in srcs if any(a in str(name).lower() for a in names))
    most = max((n for _, n in srcs), default=0) or 1
    wins = [w for w in pb.get("winners", []) if any(a in w["title"].lower() for a in names)]
    s = _clip01(0.25 + 0.45 * clipped / most + 0.1 * min(len(wins), 3))
    why = (f"{len(wins)} of the top channels' breakout Shorts are {src['short']} clips" if wins
           else f"top channels rarely break out with {src['short']}")
    if clipped:
        why += f"; {src['short']} is in {clipped} of their recent titles"
    return s, why


def analytics_vote(m, ctx):
    st = ctx["state"]
    import review_agent  # here, not at the top: review_agent imports this module
    review = st.get("review") or {}
    # the review's creator score (views vs our typical clip) when it has one, else `track`'s views-only weight
    spread = (review.get("source_score") or {}).get(m["source"]) or st.get("source_weight", {}).get(m["source"], 1.0)
    levers = review.get("levers") or {}
    how = review_agent.found_by_moment(m)
    found = (levers.get("found by") or {}).get(how, 1.0)  # e.g. Twitch clips get tested by the feed more than others
    text = " ".join(m["quotes"] + [m["video_title"], m.get("captions") or ""])
    premise = bool(PREMISE.search(text))
    fresh = 0.5 ** (m["age_h"] / 24)
    news = news_hook(m)
    s = _clip01(0.25 * math.log2(1 + spread * found) + 0.2 * premise + 0.3 * fresh + 0.25 * bool(news) + 0.05)
    why = (f"{SOURCES[m['source']]['short']} clips spread {spread:.1f}x our typical clip" if spread != 1.0
           else f"no track record for {SOURCES[m['source']]['short']} on our channel yet")
    if found != 1.0:
        why += f"; {how} moments do {found:.1f}x"
    why += "; has a spoken premise (bet/question/challenge)" if premise else "; no clear premise, may be visual-only"
    if news:
        why += f"; news people search for ({news}), like our only 50+ view Short"
    why += f"; stream is {m['age_h']:.0f}h old" + (" (fresh)" if m["age_h"] <= 12 else " (other clippers had time)" if m["age_h"] > 48 else "")
    return s, why


def live_vote(m, ctx):
    parts, s = [], 0.1
    if m.get("chat_ratio"):
        s += min(m["chat_ratio"] / 8, 0.45)
        parts.append(f"chat went {m['chat_ratio']}x")
    if m.get("heat") is not None:
        s += 0.25 * m["heat"]
        parts.append(f"most-replayed {m['heat']:.0%}")
    if m.get("clip_vph"):
        s += min(m["clip_vph"] / 20000, 0.45)
        parts.append(f"Twitch clip at {m['clip_vph']:,}/h")
    if m.get("comments"):
        s += min(math.log2(1 + m["likes"]) / 20, 0.35)
        parts.append(f"{m['comments']} timestamp comments, {m['likes']:,} likes")
    return _clip01(s), (", ".join(parts) or "weak signal") + ("; it went off" if s >= 0.7 else "")


def script_vote(m, ctx):
    src = SOURCES[m["source"]]
    draft = free_writer.write({**m, "transcript": m.get("captions") or ""}, src, [v["title"] for v in ctx["hot"].get(m["source"], [])])
    s = 0.2 + 0.3 * bool(m.get("captions")) + 0.25 * bool(re.search(r'"|\bvs\b| And | Asks |Leaves|Has|Makes', draft["title"])) \
        + 0.15 * bool(m["quotes"]) + 0.1 * (len(m.get("captions") or "") > 200)
    why = f"draft title: \"{draft['title']}\"" + ("" if m.get("captions") else "; no captions yet, writing blind")
    return _clip01(s), why


def audience_vote(m, ctx):
    asks = ctx["asks"].get(m["source"], 0)
    total = sum(ctx["asks"].values())
    if not total:
        return 0.5, "no comments on our channel asking for anyone yet"
    return _clip01(0.3 + 0.7 * asks / total), f"{asks} of {total} creator mentions in our comments"


VOTERS = {"trend": trend_vote, "study": study_vote, "analytics": analytics_vote, "live": live_vote,
          "script": script_vote, "audience": audience_vote}


# ---------- context the agents share ----------

def audience_asks(yt, state):
    """Creator names mentioned in the latest comments on our own channel (1 quota unit)."""
    cid = state.get("channel")
    asks = {k: 0 for k in SOURCES}
    if not cid or not str(cid).startswith("UC"):
        return asks
    try:
        data = yt._get("commentThreads", 1, part="snippet", allThreadsRelatedToChannelId=cid, maxResults=100,
                       textFormat="plainText")
    except Exception:
        return asks
    for it in data.get("items", []):
        text = it["snippet"]["topLevelComment"]["snippet"]["textDisplay"].lower()
        for k, s in SOURCES.items():
            if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text) for a in s["aliases"]):
                asks[k] += 1
    return asks


def add_captions(cands):
    """YouTube captions for each candidate's seconds, so the analytics and script agents hear what's said."""
    with tempfile.TemporaryDirectory() as tmp:
        for m in cands:
            if m["platform"] != "youtube":
                continue
            clip = {"platform": "youtube", "video_id": m["video_id"], "start": max(0, m["t"] - 25), "end": m["t"] + 10}
            try:
                m["captions"] = script_agent.caption_transcript(Path(tmp), clip)
            except Exception:
                m["captions"] = None


# ---------- the meeting ----------

def ollama_up():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    host = host if host.startswith("http") else "http://" + host
    try:
        tags = requests.get(f"{host}/api/tags", timeout=3).json().get("models", [])
        return (host, os.environ.get("OLLAMA_MODEL") or tags[0]["name"]) if tags else None
    except (requests.RequestException, ValueError):
        return None


def chair(cands, n, timeout=420):
    """A free local model reads every agent's notes on the shortlist and picks n, with reasons. None if unavailable."""
    up = ollama_up()
    if not up:
        return None
    host, model = up
    lines = []
    for i, m in enumerate(cands):
        lines.append(f"[{i}] {SOURCES[m['source']]['name']} at {script_agent.fmt_ts(m['t'])} of \"{m['video_title'][:70]}\". "
                     f"Viewers said: {' / '.join(q[:80] for q in m['quotes'][:3]) or '-'}. Council score {m['council']:.2f}.")
        for k, name in AGENTS.items():
            lines.append(f"    {name} ({m['votes'][k]['score']:.2f}): {m['votes'][k]['why']}")
    prompt = (f"You chair the daily meeting of a YouTube Shorts clip channel's agents. Each candidate moment from a "
              f"streamer's live stream has notes from every agent. Pick the {n} clips most likely to get the most views "
              f"today. Weigh how hard the stream reacted, whether the creator is hot right now, whether the moment has a "
              f"clear premise a scroller gets in 2 seconds, and how our channel's own results point. Avoid two picks "
              f"from the same minute of the same stream, and prefer variety across creators when scores are close.\n\n"
              + "\n".join(lines) +
              f"\n\nReturn only JSON: {{\"picks\": [{{\"index\": 0, \"why\": \"one sentence\"}}, ...]}} with exactly {n} picks, best first.")
    schema = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "why": {"type": "string"}}, "required": ["index", "why"]}}}, "required": ["picks"]}
    try:
        r = requests.post(f"{host}/api/generate", timeout=timeout, json={
            "model": model, "prompt": prompt, "format": schema, "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192}})
        r.raise_for_status()
        out = json.loads(re.search(r"\{.*\}", r.json()["response"], re.S).group(0))
    except Exception as e:
        print(f"  the chair (Ollama) couldn't decide ({type(e).__name__}); the vote decides.")
        return None
    picks, seen = [], set()
    for p in out.get("picks", []):
        i = p.get("index")
        if isinstance(i, int) and 0 <= i < len(cands) and i not in seen:
            seen.add(i)
            picks.append((i, str(p.get("why", ""))[:300]))
    return picks or None


def decide(cands, n, ctx, cap=None, use_chair=True, out_dir=None):
    """Every agent votes on the shortlist, the chair (or the vote) picks n. Returns the picked moments, best first,
    each carrying its votes. `cap(m, picks)` says whether m may still be picked (per-creator limits etc.)."""
    w = weights(ctx["state"])
    add_captions(cands)
    for m in cands:
        m["votes"] = {}
        for k, fn in VOTERS.items():
            try:
                s, why = fn(m, ctx)
            except Exception as e:  # one agent tripping shouldn't sink the meeting
                s, why = 0.5, f"(couldn't judge: {type(e).__name__})"
            m["votes"][k] = {"score": round(s, 2), "why": why}
        m["council"] = round(sum(w[k] * m["votes"][k]["score"] for k in VOTERS) / sum(w.values()), 3)
        m["council_rank"] = round(m["council"] * (1 + 0.15 * math.log10(1 + m["rank"])), 3)  # the builder's rank too
    order = sorted(cands, key=lambda m: -m["council_rank"])
    shortlist = order[:max(n * 3, 8)]

    ok = cap or (lambda m, picks: True)
    picks, reasons = [], {}
    chaired = chair(shortlist, n) if use_chair else None
    for i, why in chaired or []:
        m = shortlist[i]
        if ok(m, picks) and len(picks) < n:
            picks.append(m)
            reasons[id(m)] = why
    for m in order:  # the vote fills whatever the chair left
        if len(picks) >= n:
            break
        if m not in picks and ok(m, picks):
            picks.append(m)
    for m in picks:
        m["council_why"] = reasons.get(id(m)) or "highest combined vote"
        m["council_chair"] = bool(chaired)
    if out_dir:
        write_minutes(out_dir, shortlist, picks, w, bool(chaired))
    return picks


def write_minutes(out_dir, shortlist, picks, w, chaired):
    L = ["# Clip council", "",
         "Every agent scored each candidate 0-1. Combined score uses each agent's weight (learned from how past picks "
         "did): " + ", ".join(f"{AGENTS[k]} {w[k]:.2f}" for k in AGENTS) + ".",
         f"Final picks by {'the chair (free local model) after reading every agent' if chaired else 'the combined vote'}.", ""]
    for m in shortlist:
        tag = f"PICKED #{picks.index(m) + 1}" if m in picks else "passed"
        L += [f"## {SOURCES[m['source']]['name']}, \"{m['video_title'][:70]}\" at {script_agent.fmt_ts(m['t'])} "
              f"({tag}, council {m['council']:.2f})", ""]
        L += [f"- **{AGENTS[k]}** ({m['votes'][k]['score']:.2f}): {m['votes'][k]['why']}" for k in AGENTS]
        if m in picks:
            L.append(f"- **Decision:** {m['council_why']}")
        L.append("")
    Path(out_dir, "council.md").write_text("\n".join(L), encoding="utf-8")


def learn(state, votes, outcome, rate=0.08):
    """After a clip settles: agents that rated it high when it did well (or low when it flopped) gain weight.
    `outcome` is the clip's score centred on the channel's usual (positive = beat it)."""
    if not votes:
        return
    c = state.setdefault("council", {})
    wts = c.setdefault("weights", dict(DEFAULT_WEIGHTS))
    sign = max(-1.0, min(1.0, outcome))
    for k, v in votes.items():
        if k in wts:
            wts[k] = round(min(2.0, max(0.3, wts[k] * (1 + rate * sign * (v - 0.5) * 2))), 3)
