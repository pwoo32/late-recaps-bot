"""Clip trend agent: finds which YouTube clip niches are hot right now and pay best.

Usage:
    export YOUTUBE_API_KEY=...        # free key from Google Cloud Console
    python agent.py                   # scan all niches, last 7 days, US
    python agent.py --days 3 --region GB --niches streamer gaming

Writes reports/<date>/report.md plus raw data (videos.json, niches.json).
Each run is kept, so the next run reports which niches sped up or slowed down.

Quota: one search costs 100 units of the 10,000/day free quota; a full run is
about 35 searches (~3,600 units), so you can run it twice a day.
"""

import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from niches import NICHES

API = "https://www.googleapis.com/youtube/v3"
MAX_SHORT_SECONDS = 180
CAMPAIGN_CAP = 500  # most clipping campaigns stop paying a single clip at $100-$500
REPORTS_DIR = Path(__file__).parent / "reports"

STOPWORDS = set("""
a an the and or but of to in on for with at by from is are was were be been it its this that
these those i you he she we they me my your his her our their what who how why when where not
no yes so if just all any can will do does did has have had get got vs x shorts short clip clips
podcast video full new episode ep part de la el en que y le les des un une et
""".split())


# ---------- YouTube API ----------

class YouTube:
    def __init__(self, key):
        self.key = key
        self.units = 0

    def _get(self, path, cost, **params):
        params["key"] = self.key
        r = requests.get(f"{API}/{path}", params=params, timeout=30)
        if r.status_code != 200:
            if "API_KEY_SERVICE_BLOCKED" in r.text:
                sys.exit("Your API key isn't allowed to call the YouTube Data API. In Google Cloud Console → "
                         "Credentials, edit the key and add 'YouTube Data API v3' under API restrictions.")
            raise RuntimeError(f"YouTube API {path} failed ({r.status_code}): {r.text[:300]}")
        self.units += cost
        return r.json()

    def search(self, query, published_after, region, max_results=50):
        data = self._get(
            "search", 100, part="id", q=query, type="video", videoDuration="short",
            order="viewCount", publishedAfter=published_after, regionCode=region,
            relevanceLanguage="en", maxResults=max_results,
        )
        return [item["id"]["videoId"] for item in data.get("items", [])]

    def videos(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", 1, part="snippet,statistics,contentDetails",
                             id=",".join(ids[i:i + 50]))
            out.extend(data.get("items", []))
        return out

    def most_popular(self, region, max_results=50):
        data = self._get("videos", 1, part="snippet,statistics,contentDetails",
                         chart="mostPopular", regionCode=region, maxResults=max_results)
        return data.get("items", [])

    def channel_subs(self, ids):
        subs = {}
        for i in range(0, len(ids), 50):
            data = self._get("channels", 1, part="statistics", id=",".join(ids[i:i + 50]))
            for ch in data.get("items", []):
                stats = ch.get("statistics", {})
                subs[ch["id"]] = None if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount", 0))
        return subs


# ---------- helpers ----------

def iso_duration_seconds(d):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mins, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mins * 60 + s


def is_language(item, lang):
    """True if the video looks like it's in `lang`. Uses YouTube's language
    fields when set; otherwise, for English, checks the title is Latin script."""
    sn = item["snippet"]
    declared = sn.get("defaultAudioLanguage") or sn.get("defaultLanguage")
    if declared:
        return declared.lower().startswith(lang)
    if lang != "en":
        return True
    letters = [c for c in sn["title"] if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.9


def to_record(item, now):
    sn, st = item["snippet"], item.get("statistics", {})
    published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    hours = max((now - published).total_seconds() / 3600, 1)
    views = int(st.get("viewCount", 0))
    likes = int(st.get("likeCount", 0))
    comments = int(st.get("commentCount", 0))
    return {
        "id": item["id"],
        "url": f"https://www.youtube.com/shorts/{item['id']}",
        "title": sn["title"],
        "channel_id": sn["channelId"],
        "channel": sn["channelTitle"],
        "published": sn["publishedAt"],
        "tags": sn.get("tags", []),
        "seconds": iso_duration_seconds(item.get("contentDetails", {}).get("duration")),
        "views": views,
        "hours": round(hours, 1),
        "views_per_hour": round(views / hours, 1),
        "engagement": round((likes + comments) / views, 4) if views else 0,
    }


def pct(values, q):
    if not values:
        return 0
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def rank_normalize(values):
    """Map each value to 0..1 by rank, so one huge outlier niche can't dominate."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for r, i in enumerate(order):
        out[i] = r / (len(values) - 1) if len(values) > 1 else 1.0
    return out


def trending_terms(videos, niche_queries, top=8):
    """Words, phrases and hashtags that keep showing up in the fastest videos."""
    query_words = {w for q in niche_queries for w in re.findall(r"[a-z0-9]+", q.lower())}
    counts = Counter()
    for v in videos:
        text = v["title"].lower()
        tags = set(re.findall(r"#(\w+)", text)) | {t.lower() for t in v["tags"][:15]}
        words = [w for w in re.findall(r"[a-z0-9']+", re.sub(r"#\w+", " ", text))
                 if w not in STOPWORDS and w not in query_words and len(w) > 2]
        terms = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}
        terms |= {f"#{t}" for t in tags if t not in STOPWORDS and t not in query_words and t != "shorts"}
        counts.update(terms)  # count each term once per video
    min_videos = max(2, len(videos) // 10)
    return [(t, n) for t, n in counts.most_common(top * 3) if n >= min_videos][:top]


# ---------- scoring ----------

def score_niches(videos_by_niche, subs):
    rows = []
    for key, videos in videos_by_niche.items():
        cfg = NICHES[key]
        if not videos:
            continue
        for v in videos:
            s = subs.get(v["channel_id"])
            v["channel_subs"] = s
            # Outlier ratio: views relative to the channel's size. High = the topic,
            # not the channel's audience, drove the views.
            v["outlier"] = round(v["views"] / max(s or 1000, 1000), 2)
        top = sorted(videos, key=lambda v: v["views_per_hour"], reverse=True)[:25]
        vph = [v["views_per_hour"] for v in top]
        small = [v for v in videos if v["channel_subs"] is not None and v["channel_subs"] < 100_000]
        small_hits = [v for v in small if v["views"] >= 100_000]
        by_channel = Counter()
        for v in videos:
            by_channel[v["channel_id"]] += v["views"]
        total = sum(by_channel.values()) or 1
        top3_share = sum(n for _, n in by_channel.most_common(3)) / total
        payout_per_1k = cfg["shorts_rpm"] + (cfg["campaign_cpm"] or 0)
        median_views = statistics.median(v["views"] for v in videos)
        rows.append({
            "niche": key,
            "label": cfg["label"],
            "videos": len(videos),
            "channels": len(by_channel),
            "median_vph_top25": round(statistics.median(vph), 1),
            "median_views": int(median_views),
            "p75_outlier": round(pct([v["outlier"] for v in videos], 0.75), 2),
            "small_channel_hit_rate": round(len(small_hits) / len(small), 3) if small else 0,
            "top3_channel_share": round(top3_share, 3),
            "median_engagement": round(statistics.median(v["engagement"] for v in videos), 4),
            "shorts_rpm": cfg["shorts_rpm"],
            "campaign_cpm": cfg["campaign_cpm"],
            "reuse_risk": cfg["reuse_risk"],
            "est_earn_per_clip_shorts": round(median_views / 1000 * cfg["shorts_rpm"], 2),
            "est_earn_per_clip_campaign": round(min(median_views / 1000 * (cfg["campaign_cpm"] or 0), CAMPAIGN_CAP), 2),
            "payout_per_1k": payout_per_1k,
            "top_videos": top[:5],
        })

    if not rows:
        return rows
    demand = rank_normalize([math.log1p(r["median_vph_top25"]) for r in rows])
    money = rank_normalize([r["median_views"] * r["payout_per_1k"] for r in rows])
    breakout = rank_normalize([r["small_channel_hit_rate"] + 0.05 * math.log1p(r["p75_outlier"])
                               - 0.2 * r["top3_channel_share"] for r in rows])
    for r, d, m, b in zip(rows, demand, money, breakout):
        safety = 1 - r["reuse_risk"]
        r["score_parts"] = {"demand": round(d, 2), "money": round(m, 2),
                            "breakout": round(b, 2), "safety": round(safety, 2)}
        r["score"] = round(100 * (0.30 * d + 0.30 * m + 0.25 * b + 0.15 * safety), 1)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def previous_run(today_dir):
    runs = sorted(p for p in REPORTS_DIR.glob("*/niches.json") if p.parent.resolve() != today_dir.resolve())
    if not runs:
        return None
    return {r["niche"]: r for r in json.loads(runs[-1].read_text())}


# ---------- report ----------

def fmt_money(x):
    return f"${x:,.2f}"


def write_report(out_dir, rows, terms, popular, args, units, prev):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Clip trend report, {now}", "",
         f"Scanned Shorts-length clips (under {MAX_SHORT_SECONDS // 60} min) published in the last "
         f"{args.days} days, region {args.region}, language {args.lang}, {sum(r['videos'] for r in rows)} videos across "
         f"{len(rows)} niches." + (f" API quota used: {units} units." if units else ""), ""]

    if rows:
        best = rows[0]
        L += ["## Best bet right now", "",
              f"**{best['label']}** (score {best['score']}/100). Top clips are pulling "
              f"{best['median_vph_top25']:,.0f} views/hour; the median clip in the top search results has "
              f"{best['median_views']:,} views, "
              f"worth about {fmt_money(best['est_earn_per_clip_shorts'])} from Shorts ads"
              + (f" or {fmt_money(best['est_earn_per_clip_campaign'])} through a clipping campaign (capped at ${CAMPAIGN_CAP})"
                 if best["campaign_cpm"] else "") + ".", ""]
        if terms.get(best["niche"]):
            L += ["Topics driving it: " + ", ".join(f"`{t}`" for t, _ in terms[best["niche"]][:6]), ""]

    L += ["## Niche ranking", "",
          "| # | Niche | Score | Views/hr (top 25) | Median views | Small-channel hit rate | Shorts $/clip | Campaign $/clip (cap $500) | Reuse risk | vs last run |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        change = ""
        if prev and r["niche"] in prev and prev[r["niche"]]["median_vph_top25"]:
            delta = r["median_vph_top25"] / prev[r["niche"]]["median_vph_top25"] - 1
            change = f"{delta:+.0%}"
        L.append(f"| {i} | {r['label']} | {r['score']} | {r['median_vph_top25']:,.0f} | {r['median_views']:,} | "
                 f"{r['small_channel_hit_rate']:.0%} | {fmt_money(r['est_earn_per_clip_shorts'])} | "
                 f"{fmt_money(r['est_earn_per_clip_campaign']) if r['campaign_cpm'] else 'n/a'} | "
                 f"{r['reuse_risk']:.0%} | {change or 'first run'} |")
    L += ["",
          "Score = 30% demand (view velocity) + 30% money (median views x payout) + 25% breakout "
          "(small channels getting 100k+ views, low concentration) + 15% safety from reused-content demonetization.",
          "",
          "Note: search returns the most-viewed clips per query, so views and hit rates describe the winners in each "
          "niche, not an average upload. Compare niches against each other rather than reading the numbers as what "
          "one clip will earn.", ""]

    L += ["## What's trending inside each niche", ""]
    for r in rows:
        L += [f"### {r['label']}", ""]
        if terms.get(r["niche"]):
            L.append("Recurring topics: " + ", ".join(f"`{t}` ({n})" for t, n in terms[r["niche"]]))
            L.append("")
        for v in r["top_videos"]:
            subs = f"{v['channel_subs']:,} subs" if v["channel_subs"] is not None else "subs hidden"
            L.append(f"- [{v['title']}]({v['url']}) by {v['channel']} ({subs}): "
                     f"{v['views']:,} views in {v['hours']:.0f}h, {v['views_per_hour']:,.0f}/hr, {v['outlier']}x outlier")
        L.append("")

    if popular:
        L += [f"## Trending Shorts on YouTube's {args.region} trending chart", ""]
        for v in popular[:15]:
            L.append(f"- [{v['title']}]({v['url']}) by {v['channel']}: {v['views']:,} views, {v['views_per_hour']:,.0f}/hr")
        L.append("")

    L += ["## How to use this", "",
          "- Clip what the top niche's recurring topics are about, from long-form sources that allow clipping "
          "(many podcasts and streamers run paid clipping programs).",
          "- Add commentary, captions or context to every clip. Plain re-uploads get flagged as reused content "
          "and are excluded from YouTube monetization.",
          "- From Feb 1, 2027, Shorts ad payouts need 10M qualified Shorts views per 90 days, so early on, "
          "clipping campaigns usually pay far more than Shorts ads.",
          "- Money figures use third-party 2026 RPM/CPM estimates in niches.py; replace them with your own numbers once you have them.",
          ""]
    (out_dir / "report.md").write_text("\n".join(L))


# ---------- main ----------

def rerender(run_dir, args):
    videos_by_niche = json.loads((run_dir / "videos.json").read_text())
    subs = {v["channel_id"]: v.get("channel_subs") for vs in videos_by_niche.values() for v in vs}
    rows = score_niches(videos_by_niche, subs)
    terms = {n: trending_terms(sorted(vs, key=lambda v: v["views_per_hour"], reverse=True)[:30],
                               NICHES[n]["queries"])
             for n, vs in videos_by_niche.items()}
    (run_dir / "niches.json").write_text(json.dumps(rows, indent=1))
    popular_file = run_dir / "popular.json"
    popular = json.loads(popular_file.read_text()) if popular_file.exists() else []
    write_report(run_dir, rows, terms, popular, args, 0, previous_run(run_dir))
    print(f"Rebuilt {run_dir / 'report.md'}")



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="look at clips published in the last N days")
    ap.add_argument("--region", default="US")
    ap.add_argument("--lang", default="en", help="only keep videos in this language ('any' to keep all)")
    ap.add_argument("--niches", nargs="*", choices=list(NICHES), help="only scan these niches")
    ap.add_argument("--rerender", metavar="RUN_DIR", help="rebuild report.md from a saved run (no API calls)")
    ap.add_argument("--max-searches", type=int, default=40, help="quota guard; each search = 100 units")
    args = ap.parse_args()

    if args.rerender:
        rerender(Path(args.rerender), args)
        return

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY first (free key: console.cloud.google.com, enable YouTube Data API v3).")

    yt = YouTube(key)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    selected = args.niches or list(NICHES)

    ids_by_niche, searches = {}, 0
    for niche in selected:
        ids = []
        for q in NICHES[niche]["queries"]:
            if searches >= args.max_searches:
                print(f"Hit --max-searches={args.max_searches}; skipping remaining queries.")
                break
            print(f"[{niche}] searching: {q}")
            ids += yt.search(q, since, args.region)
            searches += 1
        ids_by_niche[niche] = list(dict.fromkeys(ids))

    all_ids = list(dict.fromkeys(i for ids in ids_by_niche.values() for i in ids))
    print(f"Fetching stats for {len(all_ids)} videos")
    items = yt.videos(all_ids)
    if args.lang != "any":
        items = [it for it in items if is_language(it, args.lang)]
    records = {it["id"]: to_record(it, now) for it in items}
    videos_by_niche = {
        n: [records[i] for i in ids if i in records and 0 < records[i]["seconds"] <= MAX_SHORT_SECONDS]
        for n, ids in ids_by_niche.items()
    }

    popular = [to_record(it, now) for it in yt.most_popular(args.region)
               if args.lang == "any" or is_language(it, args.lang)]
    popular = [v for v in popular if 0 < v["seconds"] <= MAX_SHORT_SECONDS]

    channel_ids = list({v["channel_id"] for vs in videos_by_niche.values() for v in vs})
    print(f"Fetching subscriber counts for {len(channel_ids)} channels")
    subs = yt.channel_subs(channel_ids)

    rows = score_niches(videos_by_niche, subs)
    terms = {n: trending_terms(sorted(vs, key=lambda v: v["views_per_hour"], reverse=True)[:30],
                               NICHES[n]["queries"])
             for n, vs in videos_by_niche.items()}

    out_dir = REPORTS_DIR / now.strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    prev = previous_run(out_dir)
    (out_dir / "videos.json").write_text(json.dumps(videos_by_niche, indent=1))
    (out_dir / "niches.json").write_text(json.dumps(rows, indent=1))
    (out_dir / "popular.json").write_text(json.dumps(popular, indent=1))
    write_report(out_dir, rows, terms, popular, args, yt.units, prev)
    print(f"Done. Report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
