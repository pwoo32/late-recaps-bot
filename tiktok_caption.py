"""TikTok captions built for TikTok search and the For You feed (Nov 2026).

What the clipping guides and TikTok SEO write-ups agree on for streamer clips:
- Front-load the words people search: the creator's full name (not a nickname), any guest, the game or event.
  TikTok indexes the caption text, and the first ~80 characters carry the most weight.
- Keep the caption copy around 100-150 characters: one line saying what happens, then a question that gets comments.
- 3-5 specific hashtags (creator, guest, game, platform). #fyp / #viral / #foryou don't help distribution.

The old captions read "Speed in Can I Return a." (a stream title cut off at four words) or "Jynxzi in evan burger."
This builds a full sentence from what the other agents already found: the script's title, the stream or clip title,
the guest named in it, and the game.
"""

import re
import zlib

from free_writer import EMOJI, STOP, risky

GENERIC_TAGS = {"#shorts", "#fyp", "#foryou", "#foryoupage", "#viral", "#trending", "#xyzbca", "#explore"}

# keyword in stream/clip title or quotes -> (readable name, hashtag)
GAMES = [
    (r"\bgta\b|grand theft auto", "GTA", "#gta"),
    (r"\bfortnite\b", "Fortnite", "#fortnite"),
    (r"rainbow six|\br6\b|siege", "Rainbow Six Siege", "#r6"),
    (r"\bminecraft\b", "Minecraft", "#minecraft"),
    (r"\bvalorant\b", "Valorant", "#valorant"),
    (r"arc raiders", "Arc Raiders", "#arcraiders"),
    (r"\btarkov\b", "Tarkov", "#tarkov"),
    (r"elden ring|radahn|nightreign", "Elden Ring", "#eldenring"),
    (r"call of duty|\bwarzone\b|\bcod\b|black ops", "Call of Duty", "#warzone"),
    (r"\bfifa\b|\bfc ?2[5-7]\b|ultimate team", "EA FC", "#eafc"),
    (r"\b2k2[5-7]\b|\bnba 2k\b", "NBA 2K", "#nba2k"),
    (r"\bmadden\b", "Madden", "#madden"),
    (r"\broblox\b", "Roblox", "#roblox"),
    (r"\bmarvel rivals\b", "Marvel Rivals", "#marvelrivals"),
    (r"\bfall guys\b", "Fall Guys", "#fallguys"),
    (r"\bfreddy|fnaf\b", "FNAF", "#fnaf"),
    (r"\bhorror\b", "a horror game", "#horrorgaming"),
    (r"\bschedule i\b", "Schedule I", "#schedule1"),
    (r"\bpeak\b(?= game|$)", "Peak", "#peakgame"),
    (r"\brepo\b|r\.e\.p\.o", "REPO", "#repo"),
    (r"\bchess\b", "chess", "#chess"),
    (r"\btennis\b|serena|serve", "tennis", "#tennis"),
    (r"\bbaseball\b|\bmlb\b|jeter", "baseball", "#baseball"),
    (r"\bfootball\b|\bsoccer\b|ronaldo|messi|palmer", "football", "#football"),
    (r"\bbasketball\b|\bnba\b|lebron", "basketball", "#basketball"),
    (r"\bboxing\b|\bufc\b|\bmma\b", "boxing", "#boxing"),
]
PLATFORM_TAG = {"twitch_clip": "#twitch", "kick_clip": "#kick", "youtube": "#livestream"}

QUESTIONS = {  # one is picked per clip so captions don't repeat word for word (TikTok reads repeats as spam)
    "versus": ["Who won this one?", "Who you got?", "Who takes it?"],
    "guest": ["Who was funnier here?", "Rate this duo 1-10", "Who won this exchange?"],
    "funny": ["I can't stop replaying this", "Rate this 1-10", "Who else replayed this?"],
    "shock": ["Did you see that coming?", "Would you have reacted the same?", "Was this real?"],
    "rage": ["Was he wrong for this?", "Valid or overreacting?", "Who's side are you on?"],
    "hype": ["Is this the cleanest one yet?", "Rate this 1-10", "Could you do this?"],
    "sweet": ["This one hit different", "Who else felt this?", "Rate this moment 1-10"],
}
MOOD_EMOJI = {"funny": "😭", "shock": "😳", "rage": "😤", "hype": "🔥", "sweet": "❤️"}


def _clean(s):
    s = EMOJI.sub("", s or "")
    s = re.sub(r"grand theft auto", "GTA", s, flags=re.I)  # the game's name, not a crime (moderation filter)
    s = re.sub(r"https?://\S+|[#@]\w+|\[[^\]]*\]|\([^)]*\)", "", s)  # links, tags, [LIVE], (FULL VOD)
    s = re.sub(r"\b(?:full vod|vod|irl stream|live ?stream|stream highlights?|day \d+)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" .,!-|:\"'")


def _sentence_case(s):
    """Shouty ALL CAPS titles read as spam in a caption: 'BIGGEST ANNOUNCEMENT EVER' -> 'Biggest announcement ever'."""
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        keep = {"GTA", "AI", "NBA", "NFL", "UFC", "MPH", "USA", "IRL", "CEO", "KD", "AMP", "WWE", "MLB", "FC", "I", "TV"}
        s = " ".join(w if w.strip("?!.,:") in keep or re.search(r"\d|\$", w) else w.lower() for w in s.split())
        s = s[:1].upper() + s[1:]
    return s


def trim_words(s, n):
    """At most n words, never ending on a dangling 'a', 'the', 'from'... ('Can I Return a' -> 'Can I Return')."""
    words = s.split()
    if len(words) <= n:
        return s
    words = words[:n]
    while words and words[-1].lower().strip(",.") in STOP:
        words.pop()
    return " ".join(words)


def topic(clip, creator_words):
    """What the stream or clip is about, as a readable phrase without the creator's own name."""
    t = _clean(clip.get("video_title", ""))
    if re.search(r"\s[-|]\s", t):
        parts = [p for p in re.split(r"\s[-|]\s", t) if p.strip()]
        t = max(parts, key=lambda p: len(p.split())) if parts else t
    pat = "|".join(map(re.escape, sorted(creator_words, key=len, reverse=True)))
    t = re.sub(rf"^(?:{pat})\b[\s:,'s-]*", "", t, flags=re.I).strip() if pat else t
    t = _sentence_case(t)
    if len(t.split()) < 2 or not re.search(r"[a-z]{3}", t, re.I) or risky(t):
        return ""
    return trim_words(t, 12)


def guests(clip, creator_words):
    """People named in the stream/clip title: 'Can I Return a 120 MPH Serve From Serena Williams?' -> Serena Williams."""
    title = _clean(clip.get("video_title", ""))
    found = re.findall(r"(?:\bft\.?|\bfeat\.?|\bwith|\bw/|\bvs\.?|\bfrom|\band|\bx)\s+([A-Z][\w']+(?:\s[A-Z][\w']+)?)", title)
    found += re.findall(r"\b([A-Z][a-z]{2,}\s[A-Z][a-z]{2,})\b", title)  # two capitalised words: a full name
    out = []
    for g in found:
        g = re.sub(r"'s$", "", g)
        if any(w.lower() in creator_words or w.lower() in STOP for w in g.split()) or risky(g) or (game(g)[0] or "").lower() == g.lower():
            continue
        if len(g.split()) == 1 and len(g) < 4:
            continue
        out.append(g)
    return list(dict.fromkeys(out))


def game(text):
    for pat, name, tag in GAMES:
        if re.search(pat, text, re.I):
            return name, tag
    return None, None


def mood(text):
    from free_writer import mood as fw_mood
    return fw_mood(text)[0]


def headline(clip, src):
    """The script's title with the creator's full (searchable) name, no emoji: 'Speed: "Oh I'm Not..."' ->
    'IShowSpeed: "Oh I'm Not...'."""
    t = EMOJI.sub("", clip.get("title") or "").strip()
    short, full = src.get("short", ""), src.get("name", "")
    if short and full and short != full:
        t = re.sub(rf"^{re.escape(short)}\b", full, t)
    if full and full.lower() not in t.lower():
        t = f"{full}: {t}" if t else full
    return re.sub(r"\s+", " ", t).strip(" .,")


def build(clip, src):
    """Caption copy + 3-5 hashtags. `clip` is a queue item (title, video_title, platform, quotes...), `src` its
    sources.py entry."""
    full, short = src.get("name") or clip.get("creator") or "", src.get("short") or ""
    creator_words = {a.lower() for a in src.get("aliases", [])} | {w.lower() for w in full.split()} | {short.lower()}
    text = " ".join([clip.get("video_title", ""), clip.get("title", ""), " ".join(clip.get("quotes") or []),
                     clip.get("transcript") or ""])
    seed = f"{clip.get('video_id', '')}{clip.get('moment', '')}{clip.get('id', '')}"
    head = headline(clip, src)
    if risky(head) or len(head.split()) < 2:
        head = full
    about = topic(clip, creator_words)
    people = guests(clip, creator_words)
    # the creator's own name never counts as a game or sport ("Stable Ronaldo" isn't football)
    pat = "|".join(map(re.escape, sorted((w for w in creator_words if w), key=len, reverse=True)))
    game_name, game_tag = game(re.sub(rf"\b(?:{pat})\b", " ", text, flags=re.I) if pat else text)
    md = mood(text)
    versus = re.search(r"\b(1v1|vs\.?|versus|race|battle|bet|challenge)\b", clip.get("video_title", ""), re.I)

    line = head
    words_in_line = set(re.findall(r"[a-z0-9]+", line.lower()))
    about_words = [w for w in re.findall(r"[a-z0-9]+", about.lower()) if w not in STOP]
    new_info = about_words and sum(w not in words_in_line for w in about_words) / len(about_words) > 0.5
    if new_info:
        line = f"{line} | {about.rstrip('.')}"
    elif game_name and game_name.lower() not in line.lower():
        line = f"{line} in {game_name}"
    if people and people[0].lower() not in line.lower():
        line += f" with {people[0]}"
    kind = "versus" if versus else "guest" if people else md
    q = QUESTIONS[kind][zlib.crc32(seed.encode()) % len(QUESTIONS[kind])]
    copy = f"{line} {MOOD_EMOJI.get(md, '😭')} {q}"
    if len(copy) > 160:  # keep the searchable front, drop the topic tail
        copy = f"{head} {MOOD_EMOJI.get(md, '😭')} {q}"
    if risky(copy):
        copy = f"{full} live {MOOD_EMOJI.get(md, '😭')} {q}"

    tags = [t for t in src.get("hashtags", [])[:1]]
    tags += [f"#{re.sub(r'[^a-z0-9]', '', p.lower())}" for p in people[:1]]
    tags += [game_tag] if game_tag else []
    tags += [PLATFORM_TAG.get(clip.get("platform"), "#livestream")]
    tags += [f"#{re.sub(r'[^a-z0-9]', '', full.lower())}clips", "#streamerclips"]
    tags += [t for t in clip.get("hashtags") or [] if isinstance(t, str) and t.startswith("#")]
    tags = [t.lower() for t in tags if len(t) > 2 and t.lower() not in GENERIC_TAGS and not risky(t[1:])]
    tags = list(dict.fromkeys(tags))[:5]
    return copy, tags


def caption(clip, src):
    copy, tags = build(clip, src)
    return f"{copy} {' '.join(tags)}".strip()[:2200]
