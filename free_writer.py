"""Free script writer: titles, hooks, context lines, descriptions and tags with no AI model at all.

The script agent tries a free local model through Ollama first (the cloud run installs one), and falls back to this
when Ollama isn't there or fails, so a day's clips always get a postable script and nothing costs Claude tokens.

It works from what the other agents already collected for each clip:
- the stream title                     -> what the stream is ("$1000 1v1", "Halloween party")
- viewer comments at the timestamp     -> what happened, in viewers' words
- the transcript of the clip           -> the line that was said (questions and exclamations make the best hooks)
- the trend scan's fastest clips       -> which names (guests, rivals) are hot with this creator right now
- the review's lessons                 -> name the guest, state the stake, emoji only at the end

Output matches script_agent's SCHEMA, so it plugs in as another writer.
"""

import re
import zlib

STOP = set("""a an the and or but if then so to of in on at by for with from up down out over under again about into is
are was were be been being am i im i'm you your he him his she her they them their we us our it its it's this that these
those what which who whom whose when where why how all any both each few more most other some such no nor not only own
same than too very can will just don dont don't should now yes yeah oh ok okay like got get gets go going gonna wanna
bro lmao lol lmfao fr ngl bruh bro's chat stream live clip full part day night today tonight new vs w l ayo nah na yo
man guys guy dude haha hahaha lmaooo omg wtf deadass really thing things stuff know think said say says""".split())

SLANG = re.compile(r"\b(bro|lmao+|lol+|lmfao|fr|ngl|bruh|deadass|ong|frfr|af|tbh|istg|💀+)\b", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿‍️\U0001F000-\U0001F2FF\U000E0000-\U000E007F]")
MONEY = re.compile(r"\$\s?\d[\d,.]*\s?(?:k|m|million|thousand)?\b", re.I)
# Words TikTok's and YouTube's moderation reads as crime, violence, weapons, drugs or sex. A clip's title, hook, context
# line or caption never uses them, even when the streamer says them as a joke: on-screen text claiming a crime got a
# clip removed from TikTok (Nov 2026: "STORE ROBBERY!" on a clip where nothing was robbed).
RISKY = re.compile(r"\b(rob|robs|robbed|robber(?:y|ies|s)?|robbing|steal(?:s|ing)?|stole|stolen|shoplift\w*|theft|thie(?:f|ves)|"
                   r"burglar\w*|loot(?:ed|ing)?|heist|scam(?:s|med|mer)?|shoot(?:s|ing|er)?|shot|guns?|glock|pistol|rifle|"
                   r"firearm|weapon\w*|knife|knives|stab\w*|kill(?:s|ed|ing|er)?|murder\w*|dead|dies?|died|death|suicid\w*|"
                   r"kys|assault\w*|attack(?:s|ed|ing)?|beat(?:s|ing)? up|beaten|punch(?:es|ed|ing)?|brawl\w*|jumped|"
                   r"fights?|fighting|arrest\w*|cops?|police|jail(?:ed)?|prison|handcuff\w*|crim(?:e|es|inal)|illegal|"
                   r"drugs?|weed|cocaine|crack|meth|high af|drunk|vap(?:e|es|ing)|alcohol|sex\w*|nude|naked|porn\w*|strip\w*|"
                   r"kidnap\w*|hostage|bomb\w*|terror\w*|blood\w*|racis\w*|hate crime|swat(?:ted|ting)?)\b", re.I)


def risky(text):
    """True when text uses a word moderation reads as crime, violence, weapons, drugs or sex."""
    return bool(RISKY.search(text or ""))


VERSUS = re.compile(r"\b(1v1|1 v 1|vs\.?|versus|race|battle|contest|challenge|bet|duel|fight|match)\b", re.I)

MOODS = [  # (mood, pattern over comments + transcript, emoji)
    ("funny", r"😂|🤣|😭|💀|laugh|funny|hilarious|crying|dead|lmao|lol|bruh|ayo", "😭"),
    ("shock", r"😱|😳|no way|what the|wtf|insane|crazy|unreal|scared|jump ?scare|how\b", "😱"),
    ("rage", r"rage|mad|angry|crash(?:ed|ing)? out|tweak|pressed|heated|throw|broke", "😡"),
    ("hype", r"🔥|goat|clutch|ace|win|won|let'?s go|w\b|cold|clean|nasty|beast", "🔥"),
    ("sweet", r"❤|🥹|wholesome|cute|sweet|love|hug|cried|tears|emotional", "❤️"),
]


def _clean(s):
    s = EMOJI.sub("", s or "")
    s = SLANG.sub("", s)
    s = re.sub(r"https?://\S+|[#@]\w+", "", s)
    return re.sub(r"\s+", " ", s).strip(" .,!-|:\"'")


def _title_case(s):
    small = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "vs"}
    out = []
    for i, w in enumerate(s.split()):
        if re.fullmatch(r"[$\d].*|[A-Z0-9]{2,3}", w):  # $1000, 1v1, DDG, AMP
            out.append(w)
        elif i and w.lower() in small:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower() if not re.search(r"[a-z][A-Z]", w) else w)
    return " ".join(out)


def _pick(options, seed):
    return options[zlib.crc32(seed.encode()) % len(options)]


def _cap_words(s, n):
    return " ".join(s.split()[:n])


def stream_event(title):
    """'Beat Me In a 1v1, Get $1000' -> 'Beat Me In a 1v1, Get $1000'; '🎃AMP HALLOWEEN PARTY🎃' -> 'AMP Halloween Party'."""
    t = _clean(title)
    t = re.split(r"\s[-|]\s", t)[-1] if re.search(r"\s[-|]\s", t) else t   # "FIND ME WIN $250,000 - Going To My Premiere" -> the event
    t = re.sub(r"\b(IRL STREAM|LIVE|STREAM|FULL VOD|VOD)\b", "", t, flags=re.I)
    return _title_case(re.sub(r"\s+", " ", t).strip(" ,.-")) or "Stream"


COMMON = set("""movie premiere random halloween party christmas birthday house world cup game games football soccer
basketball stream vlog japan america american china london paris dubai tour irl spider man spiderman fans fan
everyone everybody nobody somebody someone god jesus lord bro sir mom dad mama brother sister girl girls boy boys
first last next one two three yes no please thank thanks okay wait look stop come let what why when where""".split())


def names(text, creator_words, hot_titles, avoid="", title=""):
    """People named in the clip: capitalised words in comments/transcript (never the stream title's own words),
    kept only when they show up in this creator's fastest clips this week or are named at least twice."""
    avoid = {w.lower() for w in re.findall(r"[A-Za-z]+", avoid)}
    lower_used = {w for w in re.findall(r"\b[a-z]{3,}\b", text)}  # written in lowercase somewhere = an ordinary word
    cands = {}
    hot = " ".join(hot_titles).lower()
    for m in re.finditer(r"\b([A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})?|[A-Z]{3,4})\b", text):
        w = m.group(1)
        if w.isupper() and w.lower() not in hot.split():  # chat shouts in caps; only known acronyms (DDG) count
            continue
        parts = [p.lower() for p in w.split()]
        if any(p in STOP or p in COMMON or p in creator_words or p in avoid or p in lower_used for p in parts):
            continue
        cands[w] = cands.get(w, 0) + 1
    for w in list(cands):
        if w.lower() in hot:
            cands[w] += 3
    named = [w for w, n in sorted(cands.items(), key=lambda kv: -kv[1]) if n >= 2]
    feat = re.findall(r"(?:\bw/|\bwith|\bft\.?|\bfeat\.?|\bvs\.?|\bx)\s+([A-Z][\w']+(?:\s[A-Z][\w']+)?)", title)
    return list(dict.fromkeys([f for f in feat if f.lower() not in creator_words] + named))


def mood(text):
    best, emoji, hits = "shock", "😱", 0
    for name, pat, e in MOODS:
        n = len(re.findall(pat, text, re.I))
        if n > hits:
            best, emoji, hits = name, e, n
    return best, emoji


def spoken_line(transcript, max_words=7):
    """The punchiest short line said in the clip: a question or exclamation, else the shortest full sentence."""
    if not transcript:
        return None, None
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript) if s.strip()]
    sents = [s for s in sents if 2 <= len(s.split()) <= max_words and not re.search(r"subscribe|like and", s, re.I)]
    q = [s for s in sents if s.endswith("?")]
    ex = [s for s in sents if s.endswith("!")]
    pick = (q or ex or sorted(sents, key=len)[:1] or [None])[0]
    return (_clean(pick) or None), ("question" if q and pick in q else "exclaim" if pick in ex else "line")


def write(clip, src, hot_titles=(), lessons=None):
    """One script for one clip. `clip` has video_title, quotes, transcript, url; `src` is its sources.py entry."""
    short, full = src["short"], src["name"]
    creator_words = {a.lower() for a in src.get("aliases", [])} | {w.lower() for w in full.split()} | {short.lower()}
    quotes = [_clean(q) for q in clip.get("quotes") or []]
    quotes = [q for q in quotes if len(q.split()) >= 2]
    transcript = clip.get("transcript") or ""
    raw_text = " ".join((clip.get("quotes") or []) + [transcript])
    event = stream_event(clip.get("video_title", ""))
    people = names(" ".join(quotes) + " " + transcript, creator_words, hot_titles, clip.get("video_title", ""),
                   _clean(clip.get("video_title", "")))
    guest = people[0] if people else None
    versus = VERSUS.search(clip.get("video_title", "") + " " + " ".join(quotes))
    money = MONEY.search(raw_text) or (versus and MONEY.search(clip.get("video_title", "")))  # a stake, not a giveaway banner
    money = money.group(0).replace(" ", "") if money else None
    md, emoji = mood(raw_text)
    line, kind = spoken_line(transcript)
    if risky(line):
        line, kind = None, None
    quotes = [q for q in quotes if not risky(q)]
    people = [p for p in people if not risky(p)]
    guest = people[0] if people else None
    if risky(event):
        event = "stream"
    seed = clip.get("video_id", "") + str(clip.get("moment", ""))

    # title: creator first, then what happens, guest named when there is one, emoji at the end
    q = quotes[0] if quotes else ""
    said = re.search(r'"([^"]{6,60})"', " ".join(clip.get("quotes") or []))  # a viewer quoting what was said
    said = _clean(said.group(1)) if said and not risky(said.group(1)) else None
    react = {"funny": ["Can't Stop Laughing", "Loses It", "Can't Hold It In"],
             "shock": ["Can't Believe What Just Happened", "Is Left Speechless", "Didn't See This Coming"],
             "rage": ["Crashes Out", "Completely Loses It", "Is Fuming"],
             "hype": ["Goes Crazy", "Can't Believe He Pulled This Off", "Goes Off"],
             "sweet": ["Gets Emotional", "Has A Wholesome Moment", "Shows His Soft Side"]}[md]
    with_guest = {"funny": ["{g} Has {s} Crying Laughing", "{s} Can't Handle {g}", "{g} Leaves {s} In Tears"],
                  "shock": ["{g} Leaves {s} Speechless", "{s} Can't Believe What {g} Just Did", "{g} Shocks {s}"],
                  "rage": ["{g} Makes {s} Crash Out", "{s} Is Done With {g}", "{s} Snaps At {g}"],
                  "hype": ["{s} And {g} Go Crazy", "{s} And {g} Can't Believe It", "{g} Hypes Up {s}"],
                  "sweet": ["{s} Gets Emotional With {g}", "{s} And {g} Share A Moment", "{g} Gets {s} Emotional"]}[md]
    short_event = re.sub(r"^(?:going to|at|in|my|me|the|a|we|i)\s+(?:(?:my|the|a)\s+)?", "", event, flags=re.I)
    short_event = " ".join(w for w in short_event.split() if w.lower() not in creator_words and w.lower() != "vs")
    short_event = " ".join(short_event.split()[:4]).rstrip(" ,.\"")
    if guest and versus:
        title = f"{short} vs {guest}" + (f" For {money}" if money else "") + f": {_pick(react, seed)}"
    elif guest and line and kind == "question":
        title = f"{short} Asks {guest} \"{_title_case(line.rstrip('?'))}?\""
    elif guest:
        title = _pick(with_guest, seed).format(s=short, g=guest)
    elif money and versus:
        title = f"{short} Plays For {money} And {_pick(react, seed)}"
    elif said and len(said.split()) <= 9:
        title = f"{short}: \"{_title_case(said)}\""
    elif line and kind in ("question", "exclaim"):
        title = f"{short}: \"{_title_case(line)}\""
    else:
        title = f"{short} {_pick(react, seed)} At {short_event}" if 2 <= len(short_event.split()) <= 4 else f"{short} {_pick(react, seed)}"
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 68:
        title = title[:68].rsplit(" ", 1)[0].rstrip(" ,:-\"")
        if title.count('"') % 2:
            title += '"'
    title = f"{title} {emoji}"

    # hook: what's on the line or the question, never the outcome; max 6 words
    if money and versus:
        hook = f"{money} ON THE LINE"
    elif line and kind == "question":
        hook = _cap_words(line, 6).upper()
    elif guest:
        hook = _pick([f"WAIT FOR {guest.upper()}", f"{guest.upper()} WASN'T READY", f"WATCH {guest.upper()}'S FACE"], seed)
    else:
        hook = _pick({"funny": ["WAIT FOR IT", "HE DIDN'T THINK THIS THROUGH", "THIS WENT WRONG FAST"],
                      "shock": ["NOBODY SAW THIS COMING", "WATCH HIS FACE", "WAIT FOR THE END"],
                      "rage": ["HE'S ABOUT TO SNAP", "ONE MORE AND HE'S DONE", "WATCH HIM BREAK"],
                      "hype": ["DID HE REALLY DO THAT", "WATCH THE LAST SECOND", "HE CALLED IT"],
                      "sweet": ["THIS ONE HITS DIFFERENT", "WAIT FOR HIS ANSWER", "HE MEANT EVERY WORD"]}[md], seed)
    hook = _cap_words(hook, 6)

    context = f"{full} live: {event}" + (f", with {guest}" if guest else "")
    narrator = _pick([f"{short} had no idea what was about to happen.", f"Keep your eyes on {short} right here.",
                      f"This is why chat can't stop clipping {short}.", "Nobody in the room saw this coming."], seed)
    summary = f"{full} during \"{_clean(clip.get('video_title', ''))[:80]}\"" + (f" with {', '.join(people[:2])}" if people else "") + "."
    if q:
        summary += f" {q[:120].rstrip('.')}." if not q.endswith("?") else f" {q[:120]}"
    tags = list(dict.fromkeys(src.get("aliases", []) + [p.lower() for p in people[:3]]
                              + [w.lower() for w in re.findall(r"[A-Za-z0-9$]{4,}", event) if w.lower() not in STOP][:4]
                              + [f"{short.lower()} clips", "streamer clips"]))[:12]
    hashtags = list(dict.fromkeys(src.get("hashtags", [])[:2] + ([f"#{re.sub(r'[^a-z0-9]', '', people[0].lower())}"] if people else [])
                                  + ["#funny" if md == "funny" else "#viral"]))[:3]
    tags = [t for t in tags if not risky(t)]
    if risky(" ".join([title, hook, context, summary])):  # e.g. a risky word in the stream title: say nothing specific
        title, hook = f"{short} {_pick(react, seed)} {emoji}", "WAIT FOR IT"
        context, summary = f"{full} live", f"{full} live."
    return {"title": title, "hook": hook, "context": context[:140], "narrator": narrator, "description": summary,
            "hashtags": hashtags, "tags": tags}


def write_batch(clips, src_of, hot_by_creator=None, lessons=None):
    """SCHEMA-shaped output for a list of clips, like the LLM writers return."""
    out = []
    for i, c in enumerate(clips):
        hot = [v["title"] for v in (hot_by_creator or {}).get(c.get("source"), [])]
        out.append({"index": i, **write(c, src_of(c), hot, lessons)})
    return {"clips": out}
