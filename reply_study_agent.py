"""Reply study agent: learns how the top streamer-clip channels run their comment sections, so the channel activity
agent replies the way the channels that win do.

It takes the top clip channels the channel study agent found (latest reports/channels_<niche>_*/channels.json),
reads the comment sections of their recent Shorts and measures:
- how often each channel replies, and which comments it picks (liked ones, questions, early ones)
- what its replies look like: length, questions back, emoji, lowercase, jokes, thanks, @mentions
- the channel's own top comment on each Short (the pinned one, usually a question) and how it does
- what goes with more talk: comments per 1k views on Shorts where the channel shows up vs where it doesn't, and how
  many replies a thread gets once the channel has replied in it
- what to avoid: spam-looking replies, links, sub begging, arguments

Then a free writer (Gemini or Groq, as the script agent uses) turns the numbers and real examples into a short
reply guide. Without a writer the guide is built from the numbers alone.

Writes reports/replies_<niche>_<date>/report.md + replies.json, and reports/activity/reply_playbook.json, which
channel_activity_agent.py reads into its drafting prompt. This agent only reads public comments with
YOUTUBE_API_KEY: it never signs in and never posts anything.

Usage:
    export YOUTUBE_API_KEY=...
    python reply_study_agent.py                     # niche streamer, top 12 channels, 6 Shorts each
    python reply_study_agent.py --channels 20 --videos 8
    python reply_study_agent.py --if-older 7        # skip if the playbook is under 7 days old (the daily run)
    python reply_study_agent.py --no-llm            # numbers-only guide

Quota: ~2 units per channel + 1 per Short read + 1 per long thread opened, so ~120 units a run.
"""

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import MAX_SHORT_SECONDS, REPORTS_DIR, iso_duration_seconds
from channel_agent import EMOJI, ChannelYouTube

PLAYBOOK = REPORTS_DIR / "activity" / "reply_playbook.json"

LAUGH = re.compile(r"😭|😂|🤣|💀|\blol+\b|\blmao+\b|\blmfao\b|\bha(ha)+\b|\bdead\b|\bcrying\b", re.I)
THANKS = re.compile(r"\b(thank(s| you)|ty|appreciate|glad you)\b", re.I)
AGREE = re.compile(r"\b(fr+|facts|real|true|exactly|agreed?|100|no cap|on god|deadass|literally)\b", re.I)
SUB_ASK = re.compile(r"\bsub(scribe|bed)?\b|\bfollow (us|me|for)\b|turn on (post )?notifications|\blike and\b", re.I)
LINK = re.compile(r"https?://|www\.|\.com\b|discord\.gg|linktr", re.I)
PROMISE = re.compile(r"\b(part 2|pt ?2|coming soon|next video|tomorrow|we('|’)ll post|will post|stay tuned)\b", re.I)
HOSTILE = re.compile(r"\b(stfu|shut up|idiot|stupid|dumb|loser|cry about it|ratio|cope|l \+|nobody asked|clown)\b", re.I)
STOP = set("""the a an and or but to of in on at for with is are was were be been it its this that i you he she they we
me my your his her our their so just not no yes do does did dont don't im i'm it's thats that's what when how why who
like get got have has had all if as from up out about can will would there here too very really one""".split())


# ---------- reading ----------

class ReplyYouTube(ChannelYouTube):
    def comment_threads(self, video_id, n=100):
        """Top threads by relevance (YouTube's own order, which puts the pinned comment first). [] if comments are off."""
        try:
            data = self._get("commentThreads", 1, part="snippet,replies", videoId=video_id, order="relevance",
                             maxResults=n, textFormat="plainText")
        except RuntimeError as e:
            if "commentsDisabled" in str(e) or "403" in str(e) or "404" in str(e):
                return []
            raise
        return data.get("items", [])

    def all_replies(self, parent_id, n=100):
        try:
            data = self._get("comments", 1, part="snippet", parentId=parent_id, maxResults=n, textFormat="plainText")
        except RuntimeError:
            return []
        return data.get("items", [])


def latest_channel_study(niche):
    runs = sorted(REPORTS_DIR.glob(f"channels_{niche}_*/channels.json"))
    return runs[-1] if runs else None


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pick_videos(yt, playlist, now, k):
    """Half the channel's most-viewed recent Shorts, half its newest ones that have had 2+ days to collect comments.
    Mixing them gives Shorts where the channel did and didn't show up in the comments to compare."""
    ids = yt.recent_uploads(playlist) if playlist else []
    vids = []
    for it in yt.videos(ids) if ids else []:
        secs = iso_duration_seconds(it.get("contentDetails", {}).get("duration"))
        pub = parse_time(it["snippet"]["publishedAt"])
        st = it.get("statistics", {})
        if not 0 < secs <= MAX_SHORT_SECONDS or now - pub < timedelta(days=2) or "commentCount" not in st:
            continue
        vids.append(dict(id=it["id"], title=it["snippet"]["title"], published=pub.isoformat(), seconds=secs,
                         views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                         comments=int(st.get("commentCount", 0))))
    top = sorted(vids, key=lambda v: v["views"], reverse=True)[:(k + 1) // 2]
    rest = [v for v in sorted(vids, key=lambda v: v["published"], reverse=True) if v not in top]
    return top + rest[:k - len(top)]


def read_video(yt, video, owner_id, deep):
    """Every comment the owner wrote on this Short, plus the threads around them."""
    threads = yt.comment_threads(video["id"])
    out = dict(video, threads=[], owner_top=None, owner_replies=[])
    opened = 0
    for pos, t in enumerate(threads):
        top = t["snippet"]["topLevelComment"]
        s = top["snippet"]
        author = (s.get("authorChannelId") or {}).get("value")
        th = dict(id=top["id"], pos=pos, text=s.get("textDisplay", "")[:400], likes=s.get("likeCount", 0),
                  at=s["publishedAt"], replies=t["snippet"].get("totalReplyCount", 0), owner=author == owner_id,
                  owner_replied=False)
        if th["owner"]:
            if out["owner_top"] is None:
                out["owner_top"] = dict(th, likely_pinned=pos == 0)
            continue
        reps = t.get("replies", {}).get("comments", [])
        # the API inlines at most 5 replies; open a few longer threads in full so owner replies there aren't missed
        if th["replies"] > len(reps) and opened < deep:
            reps = yt.all_replies(top["id"]) or reps
            opened += 1
        for r in reps:
            rs = r["snippet"]
            if (rs.get("authorChannelId") or {}).get("value") != owner_id:
                continue
            th["owner_replied"] = True
            mins = (parse_time(rs["publishedAt"]) - parse_time(s["publishedAt"])).total_seconds() / 60
            out["owner_replies"].append(dict(comment=th["text"][:300], comment_likes=th["likes"], thread_replies=th["replies"],
                                             reply=re.sub(r"^@\S+\s*", "", rs.get("textDisplay", ""))[:300],
                                             mentions=rs.get("textDisplay", "").startswith("@"),
                                             reply_likes=rs.get("likeCount", 0), minutes_after=round(max(mins, 0))))
        out["threads"].append(th)
    return out


# ---------- measuring ----------

def features(text, mentions=False):
    words = text.split()
    letters = [c for c in text if c.isalpha()]
    return dict(
        words=len(words),
        question="?" in text,
        emoji=bool(EMOJI.search(text)),
        emoji_only=bool(text.strip()) and not letters,
        lowercase=bool(letters) and not any(c.isupper() for c in letters),
        laugh=bool(LAUGH.search(text)),
        thanks=bool(THANKS.search(text)),
        agree=bool(AGREE.search(text)),
        mention=mentions,
        sub_ask=bool(SUB_ASK.search(text)),
        link=bool(LINK.search(text)),
        promise=bool(PROMISE.search(text)),
        hostile=bool(HOSTILE.search(text)),
    )


def share(rows, key):
    return round(sum(1 for r in rows if r[key]) / len(rows), 2) if rows else 0


def med(xs, default=0):
    xs = list(xs)
    return statistics.median(xs) if xs else default


def per_k(v):
    return 1000 * v["comments"] / v["views"] if v["views"] else 0


def measure(channels):
    """Pool every studied channel's comment sections into the numbers the guide is built from."""
    replies, tops, vids = [], [], []
    for c in channels:
        base = med([per_k(v) for v in c["videos"]], 0) or 1e-9
        for v in c["videos"]:
            v["talk_lift"] = per_k(v) / base  # comments per view vs the channel's own median, so channel size drops out
            v["owner_active"] = bool(v["owner_top"] or v["owner_replies"])
            vids.append(dict(v, channel=c["name"]))
            for r in v["owner_replies"]:
                replies.append(dict(r, channel=c["name"], video=v["title"], **features(r["reply"], r["mentions"])))
            if v["owner_top"]:
                t = v["owner_top"]
                others = [th["likes"] for th in v["threads"]]
                tops.append(dict(t, channel=c["name"], video=v["title"], views=v["views"],
                                 like_rank=sum(1 for x in others if x > t["likes"]) + 1, **features(t["text"])))
    if not vids:
        return {}

    by_channel = []
    for c in channels:
        threads = sum(len(v["threads"]) for v in c["videos"])
        n_rep = sum(len(v["owner_replies"]) for v in c["videos"])
        by_channel.append(dict(name=c["name"], url=f"https://www.youtube.com/channel/{c['id']}", subs=c.get("subs"),
                               views_30d=c.get("views_30d", 0), videos=len(c["videos"]), threads=threads,
                               replies=n_rep, reply_rate=round(n_rep / threads, 3) if threads else 0,
                               top_comment_rate=round(sum(1 for v in c["videos"] if v["owner_top"]) / len(c["videos"]), 2)
                               if c["videos"] else 0,
                               comments_per_1k=round(med(per_k(v) for v in c["videos"]), 2)))
    repliers = [c for c in by_channel if c["replies"] >= 3]
    silent = [c for c in by_channel if c["replies"] == 0]

    active = [v for v in vids if v["owner_active"]]
    quiet = [v for v in vids if not v["owner_active"]]
    with_top = [v for v in vids if v["owner_top"]]
    without_top = [v for v in vids if not v["owner_top"]]

    # threads the owner replied in vs comparable threads it skipped (same Shorts, so the same audience)
    threads = [th for v in vids for th in v["threads"]]
    replied = [th for th in threads if th["owner_replied"]]
    skipped = [th for v in vids if any(t["owner_replied"] for t in v["threads"]) for th in v["threads"]
               if not th["owner_replied"]]
    picked = [features(th["text"]) for th in replied]
    passed = [features(th["text"]) for th in skipped]

    words = Counter(w for r in replies for w in re.findall(r"[a-z']+", r["reply"].lower()) if w not in STOP and len(w) > 1)
    openers = Counter(" ".join(r["reply"].lower().split()[:2]) for r in replies if r["reply"].strip())

    feat_keys = ["question", "emoji", "emoji_only", "lowercase", "laugh", "thanks", "agree", "mention",
                 "sub_ask", "link", "promise", "hostile"]
    # replies that got liked themselves are the ones viewers bought; compare them to the rest
    liked = sorted(replies, key=lambda r: r["reply_likes"], reverse=True)
    best = liked[:max(5, len(liked) // 4)]
    rest = liked[len(best):]
    return dict(
        channels=by_channel, videos=len(vids), threads=len(threads), replies_n=len(replies), tops_n=len(tops),
        replier_channels=len(repliers), silent_channels=len(silent),
        talk_repliers=round(med(c["comments_per_1k"] for c in repliers), 2) if repliers else None,
        talk_silent=round(med(c["comments_per_1k"] for c in silent), 2) if silent else None,
        lift_active=round(med(v["talk_lift"] for v in active), 2) if active else None,
        lift_quiet=round(med(v["talk_lift"] for v in quiet), 2) if quiet else None,
        lift_top=round(med(v["talk_lift"] for v in with_top), 2) if with_top else None,
        lift_no_top=round(med(v["talk_lift"] for v in without_top), 2) if without_top else None,
        active_n=len(active), quiet_n=len(quiet), top_n=len(with_top), no_top_n=len(without_top),
        thread_replies_replied=med(th["replies"] for th in replied) if replied else None,
        thread_replies_skipped=med(th["replies"] for th in skipped) if skipped else None,
        picked_likes=med(th["likes"] for th in replied) if replied else None,
        passed_likes=med(th["likes"] for th in skipped) if skipped else None,
        picked_pos=med(th["pos"] for th in replied) if replied else None,
        picked_question=share(picked, "question"), passed_question=share(passed, "question"),
        picked_laugh=share(picked, "laugh"), passed_laugh=share(passed, "laugh"),
        reply_words=med((r["words"] for r in replies), None),
        reply_words_p75=sorted(r["words"] for r in replies)[int(0.75 * (len(replies) - 1))] if replies else None,
        reply_minutes=med((r["minutes_after"] for r in replies), None),
        reply_traits={k: share(replies, k) for k in feat_keys},
        best_traits={k: (share(best, k), share(rest, k)) for k in feat_keys} if len(replies) >= 10 else {},
        best_words=(med(r["words"] for r in best), med(r["words"] for r in rest)) if len(replies) >= 10 else None,
        top_traits={k: share(tops, k) for k in ["question", "emoji", "lowercase", "laugh", "sub_ask", "link"]},
        top_words=med((t["words"] for t in tops), None),
        top_pinned=share(tops, "likely_pinned"),
        top_like_rank=med((t["like_rank"] for t in tops), None),
        common_words=words.most_common(20),
        openers=[(o, n) for o, n in openers.most_common(12) if n >= 2],
        example_replies=sorted(replies, key=lambda r: (r["reply_likes"], r["thread_replies"]), reverse=True)[:60],
        example_tops=sorted(tops, key=lambda t: t["likes"], reverse=True)[:25],
    )


# ---------- the guide ----------

GUIDE_KEYS = ["summary", "reply_rules", "first_comment_rules", "avoid", "reply_examples", "first_comment_examples"]


def llm_prompt(m, our_voice):
    stats = {k: v for k, v in m.items() if k not in ("example_replies", "example_tops", "channels")}
    stats["channels"] = [{k: c[k] for k in ("name", "reply_rate", "top_comment_rate", "comments_per_1k")} for c in m["channels"]]
    ex = [{k: r[k] for k in ("channel", "comment", "comment_likes", "reply", "reply_likes", "thread_replies")}
          for r in m["example_replies"][:45]]
    tops = [{k: t[k] for k in ("channel", "text", "likes", "replies", "likely_pinned")} for t in m["example_tops"][:20]]
    return (
        "You coach the comment section of Late Recaps, a YouTube Shorts channel of streamer clips (IShowSpeed, Kai Cenat, "
        f"Jynxzi, Adin Ross, N3on) with a narrator take on each. Its current reply voice: {our_voice}\n\n"
        "Below is what the most-viewed streamer clip channels actually do in their comment sections: numbers measured "
        "across their recent Shorts, their real replies to viewers (with how many likes each reply got and how many "
        "replies the thread got), and their own top/pinned comments. 'lift' values compare a Short's comments per view to "
        "the channel's own median (1.0 = normal). Treat these as correlations, not proof.\n\n"
        "Write a reply guide that makes Late Recaps' replies and pinned first comments get more people talking. Base every "
        "rule on the data; say which number backs it. Mind the sample sizes (replies_n, tops_n, active_n, top_n): "
        f"{'there are too few channel replies (' + str(m['replies_n']) + ') to set reply length or style from, so keep the current reply voice and say the top channels mostly do not reply; ' if m['replies_n'] < MIN_REPLIES else ''}"
        "never turn a pattern from a handful of examples into a hard rule. Keep what works for these channels even if it differs from the "
        "current voice. Never recommend asking for subs, links, promises of clips, arguing, or pretending to be the "
        "streamer.\n\n"
        "Return JSON with: summary (2-3 sentences: what the top channels do with comments), reply_rules (4-8 short "
        "rules for replies), first_comment_rules (2-5 rules for the pinned first comment), avoid (3-6 things), "
        "reply_examples (8-12 objects {comment, reply}: real or lightly adapted ones that fit Late Recaps; no channel "
        "names), first_comment_examples (5-8 strings).\n\n"
        "NUMBERS (JSON):\n" + json.dumps(stats, ensure_ascii=False, default=str)
        + "\n\nTHEIR REPLIES (JSON):\n" + json.dumps(ex, ensure_ascii=False)
        + "\n\nTHEIR OWN TOP COMMENTS (JSON):\n" + json.dumps(tops, ensure_ascii=False)
        + "\n\nReturn only JSON.")


def write_guide(m, writer):
    """The writer's guide if one answers, else one built from the numbers."""
    if writer != "none":
        import script_agent
        for name in ([writer] if writer in ("gemini", "groq") else ["gemini", "groq"]):
            try:
                out = getattr(script_agent, f"_{name}")(llm_prompt(m, OUR_VOICE))
            except Exception as e:
                print(f"Writer {name} failed ({type(e).__name__}: {str(e)[:200]}).")
                continue
            if isinstance(out, dict) and out.get("reply_rules"):
                print(f"Reply guide written by {name}.")
                return safe_examples({k: out.get(k) or ([] if k != "summary" else "") for k in GUIDE_KEYS}), name
    return rule_guide(m), "numbers only"


BAD_EXAMPLE = re.compile(r"\bwtf\b|\bf+u+c+k|\bshit\b", re.I)


def safe_examples(guide):
    """Drop example replies the activity agent must never copy: promises, sub asks, links, hostility, swearing."""
    bad = lambda t: any(p.search(t or "") for p in (PROMISE, SUB_ASK, LINK, HOSTILE, BAD_EXAMPLE))
    guide["reply_examples"] = [e for e in guide.get("reply_examples") or []
                               if isinstance(e, dict) and e.get("reply") and not bad(e["reply"])]
    guide["first_comment_examples"] = [e for e in guide.get("first_comment_examples") or []
                                       if isinstance(e, str) and not bad(e)]
    return guide


MIN_REPLIES = 30  # fewer channel replies than this and reply style isn't learned from them

OUR_VOICE = ("a fan who runs a clip page; under 15 words, casual, lowercase is fine, 0-1 emoji; agree, joke or ask a "
             "quick follow-up question.")


def rule_guide(m):
    t, tt = m["reply_traits"], m["top_traits"]
    rules = []
    if m["replies_n"] < MIN_REPLIES:
        rules.append(f"The top channels barely reply ({m['replies_n']} replies in {m['threads']} threads), so there's "
                     "no reply style to copy: keep our own voice and put the effort into the pinned first comment.")
        t = {}
    elif m["reply_words"] is not None:
        rules.append(f"Keep replies short: the top channels' median reply is {m['reply_words']:.0f} words "
                     f"(three quarters are {m['reply_words_p75']} or fewer).")
    for k, label in [("lowercase", "write in lowercase"), ("emoji", "use an emoji"), ("laugh", "laugh along (😭, lol)"),
                     ("agree", "agree with the viewer (fr, facts, real)"), ("question", "ask a question back"),
                     ("mention", "start with the viewer's @name")]:
        if t.get(k, 0) >= 0.3:
            rules.append(f"{label[0].upper() + label[1:]}: {t[k]:.0%} of their replies do.")
    for k, (b, r) in ((m.get("best_traits") or {}) if t else {}).items():
        if k in ("question", "laugh", "emoji", "agree", "lowercase") and b - r >= 0.15:
            rules.append(f"Their most-liked replies {k if k != 'question' else 'ask questions'} more "
                         f"({b:.0%} vs {r:.0%} of the rest).")
    if m["picked_likes"] is not None and m["passed_likes"] is not None and m["picked_likes"] > m["passed_likes"]:
        rules.append(f"Answer the comments people already like: the ones they reply to have a median "
                     f"{m['picked_likes']:.0f} likes vs {m['passed_likes']:.0f} for the ones they skip.")
    if m["picked_question"] - m["passed_question"] >= 0.1:
        rules.append("Answer viewers' questions first: they reply to questions far more than to other comments.")
    first = []
    if m["tops_n"]:
        first.append(f"Post a comment of our own on every Short: {m['tops_n']} of {m['videos']} of theirs have one, "
                     f"{m['top_pinned']:.0%} of those sit at the top (pinned).")
        if tt.get("question", 0) >= 0.3:
            first.append(f"Make it a question: {tt['question']:.0%} of their own comments are.")
        if m["top_words"] is not None:
            first.append(f"Keep it around {m['top_words']:.0f} words.")
    avoid = ["Asking for subs or likes in a reply.", "Links of any kind.", "Promising a part 2 or a clip.",
             "Arguing with or insulting a viewer, even a hater."]
    if t.get("emoji_only", 0) >= 0.2:
        avoid.append(f"Emoji-only replies add little ({t['emoji_only']:.0%} of theirs are; they read as a heart).")
    seen, examples = set(), []
    for r in m["example_replies"]:
        key = r["reply"].lower().strip()
        if r["emoji_only"] or r["link"] or r["sub_ask"] or r["hostile"] or key in seen:
            continue
        seen.add(key)
        examples.append({"comment": r["comment"][:200], "reply": r["reply"][:200]})
    tops = list(dict.fromkeys(t["text"][:200] for t in m["example_tops"] if not (t["link"] or t["sub_ask"])))
    return dict(summary="Built from the measured numbers (no writer available).", reply_rules=rules,
                first_comment_rules=first, avoid=avoid, reply_examples=examples[:10], first_comment_examples=tops[:6])


# ---------- report ----------

def fmt(x, suffix=""):
    return "n/a" if x is None else f"{x}{suffix}"


def write_report(out_dir, niche, src, m, guide, writer, units):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# How top {niche} clip channels reply to comments, {now}", "",
         f"Studied {len(m['channels'])} channels from `{src}`, {m['videos']} of their Shorts and {m['threads']} comment "
         f"threads. Found {m['replies_n']} replies by the channels themselves and {m['tops_n']} of their own top-level "
         f"comments. Quota used: {units} units. Guide written by: {writer}.", "",
         "All of this is correlation from public comments, not a controlled test. Read it as what winning channels do, "
         "not proof that doing it causes the views.", ""]

    L += ["## Reply guide (fed to the channel activity agent)", "", guide.get("summary") or "", ""]
    for title, key in [("Replies", "reply_rules"), ("Pinned first comment", "first_comment_rules"), ("Avoid", "avoid")]:
        if guide.get(key):
            L += [f"**{title}**", ""] + [f"- {r}" for r in guide[key]] + [""]
    if guide.get("reply_examples"):
        L += ["**Example replies**", ""] + [f"- \"{e.get('comment', '')[:120]}\" → {e.get('reply', '')}"
                                            for e in guide["reply_examples"] if isinstance(e, dict)] + [""]
    if guide.get("first_comment_examples"):
        L += ["**Example first comments**", ""] + [f"- {e}" for e in guide["first_comment_examples"]] + [""]

    L += ["## Who replies", "", "| Channel | Shorts read | Threads | Their replies | Reply rate | Own comment on | Comments / 1k views |",
          "|---|---|---|---|---|---|---|"]
    for c in sorted(m["channels"], key=lambda c: c["views_30d"], reverse=True):
        L.append(f"| [{c['name']}]({c['url']}) | {c['videos']} | {c['threads']} | {c['replies']} | {c['reply_rate']:.1%} | "
                 f"{c['top_comment_rate']:.0%} of Shorts | {c['comments_per_1k']} |")
    L += ["", f"- {m['replier_channels']} channels reply regularly (3+ replies found), {m['silent_channels']} never did. "
          f"Median comments per 1k views: {fmt(m['talk_repliers'])} for the repliers vs {fmt(m['talk_silent'])} for the silent ones.", ""]

    L += ["## What goes with more comments", "",
          f"- Shorts where the channel showed up in the comments: {fmt(m['lift_active'], 'x')} its usual comments per view "
          f"({m['active_n']} Shorts) vs {fmt(m['lift_quiet'], 'x')} where it didn't ({m['quiet_n']}).",
          f"- Shorts with the channel's own top comment: {fmt(m['lift_top'], 'x')} ({m['top_n']}) vs "
          f"{fmt(m['lift_no_top'], 'x')} without ({m['no_top_n']}).",
          f"- Threads the channel replied in have a median {fmt(m['thread_replies_replied'])} replies vs "
          f"{fmt(m['thread_replies_skipped'])} for threads on the same Shorts it skipped (busy threads also attract the "
          "channel, so this runs both ways).", ""]

    L += ["## Which comments they answer", "",
          f"- Median likes on comments they answered: {fmt(m['picked_likes'])}; on ones they skipped: {fmt(m['passed_likes'])}.",
          f"- Median position of an answered comment in YouTube's top-comments order: {fmt(m['picked_pos'])}.",
          f"- Questions: {m['picked_question']:.0%} of answered comments vs {m['passed_question']:.0%} of skipped ones. "
          f"Jokes/laughs: {m['picked_laugh']:.0%} vs {m['passed_laugh']:.0%}.",
          f"- Median time to reply: {fmt(m['reply_minutes'])} minutes after the comment.", ""]

    L += ["## What their replies look like", "",
          f"- Median length {fmt(m['reply_words'])} words (75th percentile {fmt(m['reply_words_p75'])}).",
          "- " + ", ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in m["reply_traits"].items()) + ".", ""]
    if m["best_traits"]:
        L += ["Their most-liked replies (top quarter by likes) vs the rest:", "", "| Trait | Most-liked | Rest |", "|---|---|---|"]
        L += [f"| {k.replace('_', ' ')} | {b:.0%} | {r:.0%} |" for k, (b, r) in m["best_traits"].items()]
        L += [f"| median words | {m['best_words'][0]:.0f} | {m['best_words'][1]:.0f} |", ""]
    if m["openers"]:
        L += ["Common openers: " + ", ".join(f"`{o}` ({n})" for o, n in m["openers"]), ""]
    if m["common_words"]:
        L += ["Words they use most: " + ", ".join(f"{w} ({n})" for w, n in m["common_words"]), ""]

    L += ["## Their own top comments", "",
          f"- {m['tops_n']} of {m['videos']} Shorts have one; {m['top_pinned']:.0%} sit first (pinned). Median "
          f"{fmt(m['top_words'])} words, median like rank {fmt(m['top_like_rank'])} among the Short's comments.",
          "- " + ", ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in m["top_traits"].items()) + ".", ""]
    for t in m["example_tops"][:10]:
        L.append(f"- {t['channel']}: \"{t['text'][:160]}\" ({t['likes']:,} likes, {t['replies']} replies"
                 + (", pinned" if t["likely_pinned"] else "") + ")")
    L.append("")

    L += ["## Their best-received replies", ""]
    for r in m["example_replies"][:20]:
        L.append(f"- {r['channel']}: \"{r['comment'][:120]}\" → **{r['reply'][:160]}** "
                 f"({r['reply_likes']} likes on the reply, thread {r['thread_replies']} replies)")
    L.append("")
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", default="streamer")
    ap.add_argument("--channels", type=int, default=12, help="top channels from the channel study to read")
    ap.add_argument("--videos", type=int, default=6, help="Shorts read per channel (1 unit each)")
    ap.add_argument("--deep", type=int, default=3, help="long threads opened in full per Short (1 unit each)")
    ap.add_argument("--writer", choices=["gemini", "groq", "none"], help="who writes the guide (default: first available)")
    ap.add_argument("--no-llm", action="store_true", help="numbers-only guide")
    ap.add_argument("--if-older", type=float, help="skip if the reply playbook is newer than this many days")
    args = ap.parse_args()

    if args.if_older is not None and PLAYBOOK.exists():
        age = (datetime.now(timezone.utc) - parse_time(json.loads(PLAYBOOK.read_text(encoding="utf-8"))["generated_at"]))
        if age < timedelta(days=args.if_older):
            print(f"Reply playbook is {age.days} day(s) old; next study after {args.if_older:g} days.")
            return
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY first.")
    src = latest_channel_study(args.niche)
    if not src:
        sys.exit(f"No channel study for {args.niche}. Run `python channel_agent.py --niche {args.niche}` first.")
    ranked = json.loads(src.read_text(encoding="utf-8"))[:args.channels]

    yt = ReplyYouTube(key)
    now = datetime.now(timezone.utc)
    info = {c["id"]: c for c in yt.channels([c["id"] for c in ranked])}
    channels = []
    for c in ranked:
        ch = info.get(c["id"])
        if not ch:
            continue
        playlist = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        vids = pick_videos(yt, playlist, now, args.videos)
        read = [read_video(yt, v, c["id"], args.deep) for v in vids]
        n = sum(len(v["owner_replies"]) for v in read)
        print(f"{c['name']}: {len(read)} Shorts, {n} replies by the channel, "
              f"{sum(1 for v in read if v['owner_top'])} own comments")
        channels.append(dict(id=c["id"], name=c["name"], subs=c.get("subs"), views_30d=c.get("views_30d", 0), videos=read))

    m = measure(channels)
    if not m:
        sys.exit("No comments could be read (comments off or quota out).")
    guide, writer = write_guide(m, "none" if args.no_llm else (args.writer or os.environ.get("SCRIPT_WRITER")))

    out_dir = REPORTS_DIR / f"replies_{args.niche}_{now:%Y-%m-%d_%H%M}"
    out_dir.mkdir(parents=True, exist_ok=True)
    slim = [dict(c, videos=[{k: v for k, v in vid.items() if k != "threads"} for vid in c["videos"]]) for c in channels]
    (out_dir / "replies.json").write_text(json.dumps({"measured": m, "channels": slim}, indent=1, ensure_ascii=False,
                                                     default=str), encoding="utf-8")
    write_report(out_dir, args.niche, src.parent.name, m, guide, writer, yt.units)

    PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK.write_text(json.dumps(dict(
        generated_at=now.isoformat(), study_run=out_dir.name, writer=writer,
        reply_words=m["reply_words"], reply_words_p75=m["reply_words_p75"], top_words=m["top_words"],
        reply_traits=m["reply_traits"], **guide), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Done. Report: {out_dir / 'report.md'}  Playbook: {PLAYBOOK}")


if __name__ == "__main__":
    main()
