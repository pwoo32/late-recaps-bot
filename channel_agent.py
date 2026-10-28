"""Channel study agent: finds the highest-earning clip channels in a niche and
reverse-engineers their playbook (formats, titles, hooks, length, cadence,
sources they clip, how they monetize) into a plan you can run yourself.

Usage:
    export YOUTUBE_API_KEY=...
    python channel_agent.py                    # niche = #1 from the latest trend report
    python channel_agent.py --niche streamer   # pick a niche yourself
    python channel_agent.py --niche crypto --study 15

Writes reports/channels_<niche>_<date>/report.md plus channels.json.
Quota: ~5 searches (500 units) + ~2 units per candidate channel, so ~700 units a run.

This studies what works; it does not download or reupload anyone's videos.
Reuploading other channels' clips gets copyright strikes and is excluded from
monetization as reused content, so the plan points you at the same sources and
formats, not the same files.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from agent import (MAX_SHORT_SECONDS, REPORTS_DIR, STOPWORDS, YouTube, is_language,
                   iso_duration_seconds, pct)
from niches import NICHES

MONETIZATION_SIGNALS = {
    "clipping campaign / Whop": r"whop\.com|clipping|vyro|clipaffiliates|contentrewards",
    "affiliate links": r"amzn\.to|amazon\.com|affiliate|\?ref=|/ref/|referral|use code|promo code|discount code",
    "Discord / community": r"discord\.(gg|com)|skool\.com|patreon\.com",
    "own product / course": r"course|ebook|coaching|mentorship|stan\.store|linktr\.ee|beacons\.ai|gumroad",
    "sponsor": r"sponsored by|thanks to .{0,40} for sponsoring|#ad\b|paid promotion",
    "credits the source": r"credit|full (video|episode|podcast|stream)|watch the full|original video|source:|from @",
}
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


class ChannelYouTube(YouTube):
    def _get(self, path, cost, **params):
        # Google also caps searches per minute; wait it out instead of failing the run.
        for wait in (20, 40, 60, None):
            try:
                return super()._get(path, cost, **params)
            except RuntimeError as e:
                if wait is None or "Quota exceeded" not in str(e) and "rateLimit" not in str(e):
                    raise
                print(f"rate limited, retrying in {wait}s")
                time.sleep(wait)

    def channels(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", 1, part="snippet,statistics,contentDetails",
                             id=",".join(ids[i:i + 50]))
            out.extend(data.get("items", []))
        return out

    def recent_uploads(self, playlist_id, n=50):
        try:
            data = self._get("playlistItems", 1, part="contentDetails", playlistId=playlist_id, maxResults=n)
        except RuntimeError:
            return []
        return [it["contentDetails"]["videoId"] for it in data.get("items", [])]


# ---------- picking the niche ----------

def latest_top_niche():
    runs = sorted(REPORTS_DIR.glob("*/niches.json"))
    if not runs:
        return None, None
    rows = json.loads(runs[-1].read_text())
    return (rows[0]["niche"], runs[-1].parent.name) if rows else (None, None)


# ---------- per-video features ----------

def video_record(item, now):
    sn, st = item["snippet"], item.get("statistics", {})
    published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    views = int(st.get("viewCount", 0))
    return {
        "id": item["id"],
        "title": sn["title"],
        "description": sn.get("description", ""),
        "tags": sn.get("tags", []),
        "published": published,
        "age_days": max((now - published).total_seconds() / 86400, 0.04),
        "seconds": iso_duration_seconds(item.get("contentDetails", {}).get("duration")),
        "views": views,
        "likes": int(st.get("likeCount", 0)),
        "comments": int(st.get("commentCount", 0)),
    }


def title_features(title):
    letters = [c for c in title if c.isalpha()]
    words = title.split()
    return {
        "chars": len(title),
        "caps_words": sum(1 for w in words if len(w) > 2 and w.isupper()),
        "emoji": bool(EMOJI.search(title)),
        "question": "?" in title,
        "exclaim": "!" in title,
        "number": bool(re.search(r"\d", title)),
        "quote": bool(re.search(r"[\"“”']", title)) and title.count('"') + title.count("“") >= 1,
        "hashtags": len(re.findall(r"#\w+", title)),
        "pipe_or_dash": bool(re.search(r"\s[|\-–—:]\s", title)),
        "upper_ratio": sum(c.isupper() for c in letters) / len(letters) if letters else 0,
    }


def hook_opener(title):
    """First 3 words with hashtags stripped, lowercased: the pattern a viewer reads first."""
    words = re.sub(r"#\w+", "", title).split()
    return " ".join(words[:3]).lower().strip(" .,!?:\"'")


GENERIC = set("""wait when bro why what this that watch never always every dont don't stop real true best
most top day life god man men women girl girls boy boys guy guys king queen gym fitness motivation motivational
discipline mindset success money brain aura sigma alpha edit edits editz funny moment moments reaction react viral
shorts short youtube yt fyp foryou trending speech tips quotes quote self improvement highlights highlight live stream
streamer streams twitch kick clips clip comedy fail fails prank challenge emotional respect love sad grind workout
reels tiktok status whatsapp lifelessons life lessons subscribe family mother father mom dad friends friendship
vlog grwm power bros video videos success""".split())


def _norm(text):
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "


def proper_names(channels, exclude):
    """The people/shows being clipped. Candidates are names creators tag with (tags,
    hashtags, @mentions); each is scored by how many titles name it, and must show up
    on 2+ channels so one channel's boilerplate tags don't count."""
    cands = set()
    for c in channels:
        for v in c["uploads"]:
            cands |= {t.lower().strip() for t in v["tags"][:20] if len(t.split()) <= 2}
            cands |= {m.lower() for m in re.findall(r"[#@](\w+)", v["title"])}
    generic_long = [g for g in GENERIC if len(g) >= 4]
    cands = {n for n in cands if len(n) > 3 and n not in exclude
             and not set(n.split()) <= GENERIC | STOPWORDS
             # hashtag mashups like #selfimprovement / #mindsetminute
             and not (" " not in n and any(g in n for g in generic_long))}
    titles = [(c["id"], _norm(v["title"]), _norm(v["title"]).replace(" ", "")) for c in channels for v in c["uploads"]]
    counts = {}
    for n in cands:
        key, squashed = f" {n} ", n.replace(" ", "")
        hits = [cid for cid, t, sq in titles if key in t or (len(squashed) > 5 and squashed in sq)]
        if len(set(hits)) >= 2:
            counts[n] = len(hits)
    # "kai cenat" and "kaicenat" are the same name: keep the most-hit spelling.
    best = {}
    for n, k in counts.items():
        sq = n.replace(" ", "")
        if sq not in best or k > counts[best[sq]]:
            best[sq] = n
    kept = {n: counts[n] for n in best.values()}
    # "cenat" is just part of "kai cenat": drop a fragment when the full name covers most of its hits.
    return Counter({n: k for n, k in kept.items()
                    if not any(m != n and n.replace(" ", "") in m.replace(" ", "") and km >= 0.8 * k
                               for m, km in kept.items())})


# ---------- channel analysis ----------

def analyze_channel(ch, uploads, now, payout):
    st, sn = ch.get("statistics", {}), ch["snippet"]
    created = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    shorts = [v for v in uploads if 0 < v["seconds"] <= MAX_SHORT_SECONDS]
    last30 = [v for v in uploads if v["age_days"] <= 30]
    span_days = max((now - min(v["published"] for v in uploads)).total_seconds() / 86400, 1) if uploads else 1
    views_30d = sum(v["views"] for v in last30)
    median_views = statistics.median(v["views"] for v in uploads) if uploads else 0
    # If the 50 newest uploads go back < 30 days, views_30d misses nothing; if they go back further,
    # it's exact for the window. Views keep coming after 30 days, so this undercounts a little.
    return {
        "id": ch["id"],
        "name": sn["title"],
        "url": f"https://www.youtube.com/channel/{ch['id']}",
        "country": sn.get("country"),
        "created": created.date().isoformat(),
        "age_months": round((now - created).days / 30.4, 1),
        "subs": subs,
        "total_views": int(st.get("viewCount", 0)),
        "video_count": int(st.get("videoCount", 0)),
        "sampled": len(uploads),
        "shorts_share": round(len(shorts) / len(uploads), 2) if uploads else 0,
        "uploads_per_day": round(len(uploads) / span_days, 2),
        "views_30d": views_30d,
        "median_views": int(median_views),
        "hit_rate_100k": round(sum(v["views"] >= 100_000 for v in uploads) / len(uploads), 2) if uploads else 0,
        "views_per_sub": round(median_views / subs, 2) if subs else None,
        "est_month_shorts": round(views_30d / 1000 * payout["shorts_rpm"], 0),
        "est_month_campaign": round(views_30d / 1000 * (payout["campaign_cpm"] or 0), 0),
        "description": sn.get("description", ""),
        "uploads": uploads,
    }


def study(channels, niche_queries):
    """Pool the studied channels' uploads and pull out the repeatable patterns."""
    vids = [v for c in channels for v in c["uploads"]]
    shorts = [v for v in vids if 0 < v["seconds"] <= MAX_SHORT_SECONDS]
    if not vids:
        return {}
    # A video's "lift" = views vs its own channel's median, so big channels don't drown out small ones.
    for c in channels:
        base = max(c["median_views"], 1)
        for v in c["uploads"]:
            v["lift"] = v["views"] / base
            v["channel"] = c["name"]
    winners = sorted(vids, key=lambda v: v["lift"], reverse=True)[:max(10, len(vids) // 5)]
    losers = sorted(vids, key=lambda v: v["lift"])[:max(10, len(vids) // 5)]

    def share(group, key):
        return round(sum(title_features(v["title"])[key] for v in group) / len(group), 2)

    title_cmp = {k: (share(winners, k), share(losers, k))
                 for k in ["emoji", "question", "exclaim", "number", "quote", "pipe_or_dash"]}
    caps_cmp = (round(statistics.mean(title_features(v["title"])["caps_words"] for v in winners), 1),
                round(statistics.mean(title_features(v["title"])["caps_words"] for v in losers), 1))
    chars_cmp = (int(statistics.median(len(v["title"]) for v in winners)),
                 int(statistics.median(len(v["title"]) for v in losers)))

    # Length: which duration bucket beats its channel's median most (lift), so one big channel can't skew it.
    buckets = [(0, 15), (15, 30), (30, 45), (45, 60), (60, 90), (90, 180), (180, 10**9)]
    length = []
    for lo, hi in buckets:
        g = [v for v in vids if lo < v["seconds"] <= hi]
        if len(g) >= max(5, len(vids) // 20):
            length.append({"bucket": f"{lo}-{hi}s" if hi < 10**9 else "3min+", "videos": len(g),
                           "channels": len({v["channel"] for v in g}),
                           "median_views": int(statistics.median(v["views"] for v in g)),
                           "median_lift": round(statistics.median(v["lift"] for v in g), 2)})

    # Cadence: when they post (UTC) and how much lift each slot gets.
    by_hour = Counter(v["published"].hour for v in vids)
    lift_by_hour = {h: statistics.median([v["lift"] for v in vids if v["published"].hour == h])
                    for h in by_hour if by_hour[h] >= 3}
    by_dow = Counter(v["published"].strftime("%a") for v in vids)

    hooks = Counter(hook_opener(v["title"]) for v in winners)
    hashtags = Counter(t.lower() for v in shorts for t in re.findall(r"#\w+", v["title"] + " " + v["description"][:300]))

    # Search queries can be names (kai cenat), so only the channels' own names are excluded.
    exclude = {c["name"].lower().strip() for c in channels}
    sources = proper_names(channels, exclude)

    monet = Counter()
    for c in channels:
        text = (c["description"] + " " + " ".join(v["description"] for v in c["uploads"][:15])).lower()
        for label, pat in MONETIZATION_SIGNALS.items():
            if re.search(pat, text):
                monet[label] += 1

    return {
        "videos": len(vids),
        "shorts_share": round(len(shorts) / len(vids), 2),
        "title_cmp": title_cmp, "caps_cmp": caps_cmp, "chars_cmp": chars_cmp,
        "length": length,
        "median_seconds": int(statistics.median(v["seconds"] for v in shorts)) if shorts else None,
        "cadence_median": statistics.median(c["uploads_per_day"] for c in channels),
        "cadence_p75": pct([c["uploads_per_day"] for c in channels], 0.75),
        "top_hours": sorted(lift_by_hour.items(), key=lambda kv: kv[1], reverse=True)[:4],
        "busiest_hours": by_hour.most_common(4),
        "by_dow": by_dow,
        "hooks": [(h, n) for h, n in hooks.most_common(12) if n >= 2 and h],
        "hashtags": hashtags.most_common(12),
        "sources": [(s, n) for s, n in sources.most_common(15) if n >= 3],
        "studied": len(channels),
        "monetization": monet,
        "winners": winners[:12],
    }


# ---------- report ----------

def money(x):
    return f"${x:,.0f}"


def write_report(out_dir, niche, cfg, ranked, studied, s, args, units, source_note, official=()):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Top channels in {cfg['label']}, {now}", "",
         f"Niche: **{niche}** ({source_note}). Found {len(ranked)} channels posting in this niche in the last "
         f"{args.days} days, studied the top {len(studied)} by estimated monthly earnings. API quota used: {units} units.", "",
         "Earnings are estimates: last-30-day views on each channel's 50 newest uploads x the niche's payout in niches.py "
         f"(Shorts ${cfg['shorts_rpm']}/1k, campaigns {('$' + str(cfg['campaign_cpm']) + '/1k') if cfg['campaign_cpm'] else 'n/a'}). "
         "YouTube doesn't publish real revenue. Ranking is by 30-day views. Shorts $/mo is roughly what the channel's ad share is; "
         "campaign potential is what those views would pay inside a clipping campaign, which most of these channels are not in "
         "and which cap payouts per clip and per creator in practice, so treat it as an upper bound.", ""]

    L += ["## Most profitable channels", "",
          "| # | Channel | Subs | Age | Uploads/day | Views (30d) | Median views | 100k+ hit rate | Shorts $/mo | Campaign potential $/mo |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(ranked[:25], 1):
        subs = f"{c['subs']:,}" if c["subs"] is not None else "hidden"
        L.append(f"| {i} | [{c['name']}]({c['url']}) | {subs} | {c['age_months']:.0f} mo | {c['uploads_per_day']} | "
                 f"{c['views_30d']:,} | {c['median_views']:,} | {c['hit_rate_100k']:.0%} | "
                 f"{money(c['est_month_shorts'])} | {money(c['est_month_campaign']) if cfg['campaign_cpm'] else 'n/a'} |")
    if official:
        L += ["", "Left out as the creators being clipped rather than clip channels: "
              + ", ".join(f"[{c['name']}]({c['url']})" for c in official) + "."]
    young = [c for c in ranked[:25] if c["age_months"] <= 12]
    if young:
        L += ["", "**Proof a new channel can win here:** " + ", ".join(
            f"[{c['name']}]({c['url']}) ({c['age_months']:.0f} mo old, {c['views_30d']:,} views last 30d)" for c in young[:5]) + "."]
    L.append("")

    if not s:
        (out_dir / "report.md").write_text("\n".join(L))
        return

    L += ["## The playbook the top channels share", ""]
    L += ["### Format and length", "",
          f"- {s['shorts_share']:.0%} of their uploads are Shorts; median Short is **{s['median_seconds']}s**.",
          "- By length (lift = views vs the channel's own median; 1.0x is a normal upload): "
          + ", ".join(f"{b['bucket']}: {b['median_lift']}x, {b['median_views']:,} median views "
                      f"({b['videos']} vids, {b['channels']} channels)" for b in s["length"]) + ".",
          ""]
    best_len = max((b for b in s["length"] if b["channels"] >= max(4, s["studied"] // 3)), key=lambda b: b["median_lift"], default=None)
    L += ["### Posting cadence", "",
          f"- Median top channel posts **{s['cadence_median']:.1f} uploads/day**; the busiest quarter post {s['cadence_p75']:.1f}+/day.",
          "- Hours (UTC) whose uploads beat the channel's own median the most: "
          + ", ".join(f"{h:02d}:00 ({lift:.1f}x)" for h, lift in s["top_hours"]) + ".",
          "- Hours they post most: " + ", ".join(f"{h:02d}:00 ({n})" for h, n in s["busiest_hours"]) + ".",
          "- By weekday: " + ", ".join(f"{d} {s['by_dow'].get(d, 0)}" for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) + ".",
          ""]
    L += ["### Titles: what breakout videos do differently", "",
          "Breakouts are each channel's top 20% of uploads vs its own median; flops are the bottom 20%.", "",
          "| Title trait | Breakouts | Flops |", "|---|---|---|"]
    names = {"emoji": "Has emoji", "question": "Asks a question", "exclaim": "Has !", "number": "Has a number",
             "quote": "Quotes someone", "pipe_or_dash": "Uses | or - separator"}
    for k, (w, l) in s["title_cmp"].items():
        L.append(f"| {names[k]} | {w:.0%} | {l:.0%} |")
    L.append(f"| ALL-CAPS words per title | {s['caps_cmp'][0]} | {s['caps_cmp'][1]} |")
    L.append(f"| Median title length (chars) | {s['chars_cmp'][0]} | {s['chars_cmp'][1]} |")
    L.append("")
    if s["hooks"]:
        L += ["Recurring openers on breakouts: " + ", ".join(f"`{h}…` ({n})" for h, n in s["hooks"]), ""]
    if s["hashtags"]:
        L += ["Hashtags they use: " + ", ".join(f"`{h}` ({n})" for h, n in s["hashtags"]), ""]

    L += ["### Who and what they clip", ""]
    if s["sources"]:
        L += ["Creators, shows and topics that keep coming up in titles: "
              + ", ".join(f"**{n}** ({k})" for n, k in s["sources"]) + " (number = titles naming them).", "",
              "Check whether each one runs a clipping program (search \"<name> clipping\" on Whop or their Discord) "
              "before you clip them: that's permission plus a per-view payout.", ""]
    else:
        L += ["No recurring source names: these channels make their own edits from stock/viral footage, music and text overlays rather than clipping one creator.", ""]

    L += ["### How they make money", ""]
    if s["monetization"]:
        for label, n in s["monetization"].most_common():
            L.append(f"- {label}: {n} of {len(studied)} channels")
    else:
        L.append("- No visible links or sponsor mentions; likely relying on Shorts ads and off-platform campaign payouts.")
    L.append("")

    L += ["### Their biggest breakouts (study these)", ""]
    for v in s["winners"]:
        L.append(f"- [{v['title']}](https://www.youtube.com/shorts/{v['id']}) by {v['channel']}: "
                 f"{v['views']:,} views, {v['lift']:.1f}x the channel's median, {v['seconds']}s")
    L.append("")

    # ---- the plan ----
    tc = s["title_cmp"]
    title_tips = [names[k].lower() for k, (w, l) in tc.items() if w - l >= 0.1]
    avoid = [names[k].lower() for k, (w, l) in tc.items() if l - w >= 0.1]
    top_hour = s["top_hours"][0][0] if s["top_hours"] else None
    L += ["## Your plan to replicate it", "",
          f"1. **Niche:** {cfg['label']}. Pick 3-5 of the sources above that allow clipping or pay for it.",
          f"2. **Volume:** post {max(1, round(s['cadence_median']))}-{max(2, round(s['cadence_p75']))} Shorts a day"
          + (f", with your best one around {top_hour:02d}:00 UTC" if top_hour is not None else "") + ".",
          f"3. **Length:** aim for the {best_len['bucket']} range" if best_len else "3. **Length:** keep Shorts under 60s",
          "4. **Titles:** " + (("lean on " + ", ".join(title_tips)) if title_tips else "match the openers above")
          + (("; avoid " + ", ".join(avoid)) if avoid else "")
          + f"; keep them around {s['chars_cmp'][0]} characters.",
          "5. **Make each clip yours:** your own captions, a hook line in the first second, cuts and commentary or context. "
          "Reuploads of other channels' clips get copyright strikes and are excluded from monetization as reused content.",
          "6. **Money:** apply to clipping campaigns for your sources first; they pay per view from day one, while Shorts ads "
          "need YPP and (from Feb 1, 2027) 10M qualified Shorts views per 90 days.",
          "7. **Rerun weekly** to track which channels are rising and which formats are working now.", ""]
    (out_dir / "report.md").write_text("\n".join(L))


# ---------- main ----------

def find_channels(yt, cfg, since, args):
    """Channels that own the most-viewed recent Shorts for the niche's search queries."""
    hits = Counter()
    for q in cfg["queries"]:
        print(f"searching: {q}")
        for vid in yt.search(q, since, args.region):
            hits[vid] += 1
    items = yt.videos(list(hits))
    if args.lang != "any":
        items = [it for it in items if is_language(it, args.lang)]
    seed_views = Counter()
    for it in items:
        seed_views[it["snippet"]["channelId"]] += int(it.get("statistics", {}).get("viewCount", 0))
    candidate_ids = [cid for cid, _ in seed_views.most_common(args.candidates)]
    return candidate_ids


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", choices=list(NICHES), help="default: #1 niche from the latest trend report")
    ap.add_argument("--days", type=int, default=30, help="find channels that posted in the niche in the last N days")
    ap.add_argument("--region", default="US")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--candidates", type=int, default=60, help="channels to size up (2 units each)")
    ap.add_argument("--study", type=int, default=10, help="top channels to study in depth")
    ap.add_argument("--min-subs", type=int, default=1000)
    ap.add_argument("--from-run", help="re-study the channels from an earlier run folder (skips searches, ~120 units)")
    args = ap.parse_args()

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY first.")

    niche, source_note = args.niche, "picked with --niche"
    if not niche:
        niche, run = latest_top_niche()
        if not niche:
            sys.exit("No trend report found. Run agent.py first or pass --niche.")
        source_note = f"#1 in trend report {run}"
    cfg = NICHES[niche]

    yt = ChannelYouTube(key)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Find channels: whoever owns the most-viewed recent Shorts for the niche's queries.
    if args.from_run:
        candidate_ids = [c["id"] for c in json.loads((REPORTS_DIR / args.from_run / "channels.json").read_text())]
    else:
        candidate_ids = find_channels(yt, cfg, since, args)
    print(f"{len(candidate_ids)} candidate channels")

    # 2. Size each channel up from its 50 newest uploads.
    chans = [c for c in yt.channels(candidate_ids)
             if int(c.get("statistics", {}).get("subscriberCount", 0) or 0) >= args.min_subs
             or c.get("statistics", {}).get("hiddenSubscriberCount")]
    ranked = []
    for c in chans:
        playlist = c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        ids = yt.recent_uploads(playlist) if playlist else []
        uploads = [video_record(it, now) for it in yt.videos(ids)] if ids else []
        if uploads:
            ranked.append(analyze_channel(c, uploads, now, cfg))
    ranked.sort(key=lambda c: c["views_30d"], reverse=True)
    # The creators being clipped (IShowSpeed, Kai Cenat Live) show up too; they're sources, not competitors.
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    official = [c for c in ranked if (c["subs"] or 0) >= 5_000_000
                and any(squash(q) in squash(c["name"]) for q in cfg["queries"])]
    ranked = [c for c in ranked if c not in official]

    # 3. Study the top channels.
    studied = ranked[:args.study]
    s = study(studied, cfg["queries"])

    out_dir = REPORTS_DIR / f"channels_{niche}_{now.strftime('%Y-%m-%d_%H%M')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    slim = [{k: v for k, v in c.items() if k != "uploads"} for c in ranked]
    (out_dir / "channels.json").write_text(json.dumps(slim, indent=1))
    write_report(out_dir, niche, cfg, ranked, studied, s, args, yt.units, source_note, official)
    print(f"Done. Report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
