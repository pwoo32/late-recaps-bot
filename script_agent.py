"""Script agent: writes everything that goes out with each clip in the channel builder's queue.

It pulls from every other agent:
- trend agent (agent.py):            which clips of this creator / niche are getting the most views per hour right now
- channel study (channel_agent.py):  breakout titles of the top channels, their title length and hashtags
- channel builder (daily):           the exact moment: stream title, what viewers said at that timestamp
- channel builder (track):           which of your own uploads beat or missed your channel's median
- the clip itself:                   YouTube's auto captions for those seconds, or the whisper transcript once `cut`
                                     has run (cut re-scripts each clip from it right before rendering)

and writes, per clip:
- title:        a real Shorts title in the breakout pattern ("Speed Crashes Out After Cole Palmer Scores Again 😭")
- hook:         the big on-screen text pinned for the whole clip
- context:      the setup line shown for the first 3 seconds
- narrator:     the line the AI voice says over the opening (saved as `take`, which cut narrates)
- description:  a short summary, the credit/link to the full stream, and hashtags
- tags:         YouTube tags for the upload

Everything is written into the queue's queue.json, which cut and publish read. The first run keeps the old file as
queue.before_scripts.json, and a readable copy lands in scripts.md. `channel_builder.py daily` runs this
automatically, so you only run it by hand to redo scripts.

Usage:
    python script_agent.py                     # latest queue, best writer available
    python script_agent.py --writer claude-code
    python script_agent.py --ids 1 3           # only some clips
    python script_agent.py --dry-run           # print, don't save
    python script_agent.py --prompt-only       # save script_prompt.txt to paste into claude.ai yourself...
    python script_agent.py --import answer.json   # ...then save the JSON it gives back as answer.json

Writers. By default only free ones run, in this order:
- ollama:      a free local model through Ollama (https://ollama.com, then `ollama pull qwen2.5:7b`). The cloud run
               installs it. OLLAMA_MODEL picks the model, OLLAMA_HOST the server.
- free:        free_writer.py, rules and templates built from the transcript, comments and trend scan. No model at
               all, never fails, so the day's clips always post.
Only if you pick them with --writer or SCRIPT_WRITER (they use your Claude plan or API credit):
- claude-code: the Claude Code CLI on this computer (`claude -p`), on your Claude plan.
- api:         the Anthropic API with ANTHROPIC_API_KEY.
- template:    the old fill-in-the-blank draft.

No YouTube API quota is used. Captions need yt-dlp (already in requirements.txt).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from niches import NICHES
from sources import SOURCES

REPORTS_DIR = Path(__file__).parent / "reports"
BUILDER_DIR = REPORTS_DIR / "builder"

SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "title": {"type": "string"}, "hook": {"type": "string"},
                       "context": {"type": "string"}, "narrator": {"type": "string"}, "description": {"type": "string"},
                       "hashtags": {"type": "array", "items": {"type": "string"}},
                       "tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["index", "title", "hook", "context", "narrator", "description", "hashtags", "tags"]}}},
    "required": ["clips"],
}

TEMPLATE_TAKES = ["{n} had no idea what was about to happen.", "Keep your eyes on {n} right here.",
                  "This is why chat can't stop clipping {n}.", "Nobody in the room saw this coming.",
                  "{n} really thought this was a good idea."]


def fmt_ts(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def latest(pattern):
    runs = sorted(REPORTS_DIR.glob(pattern), key=lambda p: (p.stat().st_mtime, str(p)))  # a fresh checkout gives every file the same mtime: the dated name breaks ties
    return runs[-1] if runs else None


def src_of(c):
    return SOURCES.get(c["source"], {"name": c["creator"], "short": c["creator"], "aliases": [c["creator"].lower()],
                                     "hashtags": []})


# ---------- what the other agents know ----------

def playbook(niche):
    """Channel study agent's playbook (the builder caches it in reports/builder/)."""
    f = latest(f"builder/playbook_channels_{niche}_*.json")
    if not f:
        sys.exit(f"No channel study playbook for {niche}. Run `python channel_builder.py plan --niche {niche}` first.")
    return json.loads(f.read_text(encoding="utf-8"))


def trending(niche, clips):
    """Trend agent's latest run: the niche's fastest clips this week, and the fastest ones naming each creator."""
    f = latest("[0-9]*_*/videos.json")
    if not f:
        return [], {}
    vids = json.loads(f.read_text(encoding="utf-8")).get(niche, [])
    vids = sorted(vids, key=lambda v: -v.get("views_per_hour", 0))
    by_creator = {}
    for c in clips:
        aliases = [a.lower() for a in src_of(c)["aliases"]]
        hits = [v for v in vids if any(re.search(rf"\b{re.escape(a)}\b", v["title"].lower()) for a in aliases)]
        by_creator[c["source"]] = hits[:6]
    return vids[:10], by_creator


def own_results():
    """What `channel_builder.py track` learned about your own uploads."""
    f = BUILDER_DIR / "state.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8")).get("own_results", [])


def review_notes():
    """What review_agent.py learned from how your posted clips held viewers: lessons plus per-clip verdicts."""
    f = BUILDER_DIR / "state.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text(encoding="utf-8")).get("review") or {}


# ---------- what's said in the clip ----------

def cut_transcript(qdir, clip):
    """Transcript `channel_builder.py cut` saved, if this clip was already cut."""
    for txt in (qdir / "clips").glob(f"{clip['id']:02d}_*.txt"):
        m = re.search(r"\nTRANSCRIPT\n(.+)", txt.read_text(encoding="utf-8"), re.S)
        if m:
            return m.group(1).strip()
    return None


def caption_transcript(qdir, clip):
    """YouTube's auto captions for the clip's seconds (plus a few before, for the setup). Cached per video."""
    if clip.get("platform") != "youtube" or not shutil.which("yt-dlp"):
        return None
    cache = qdir / "captions" / f"{clip['video_id']}.json3"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                subprocess.run(["yt-dlp", "-q", "--no-warnings", "--skip-download", "--write-auto-subs", "--write-subs",
                                "--sub-langs", "en.*,en", "--sub-format", "json3", "-o", str(Path(tmp) / "c"),
                                f"https://www.youtube.com/watch?v={clip['video_id']}"],
                               check=True, capture_output=True, timeout=120)
            except (subprocess.SubprocessError, OSError):
                return None
            subs = sorted(Path(tmp).glob("c*.json3"), key=lambda p: (".en-orig" not in p.name, len(p.name)))
            if not subs:
                cache.write_text("{}", encoding="utf-8")  # no captions; don't ask again
                return None
            shutil.copy(subs[0], cache)
    try:
        events = json.loads(cache.read_text(encoding="utf-8")).get("events", [])
    except json.JSONDecodeError:
        return None
    lo, hi = (clip["start"] - 8) * 1000, clip["end"] * 1000
    words = []
    for ev in events:
        t0 = ev.get("tStartMs", 0)
        for seg in ev.get("segs") or []:
            t = t0 + seg.get("tOffsetMs", 0)
            if lo <= t <= hi:
                words.append(seg.get("utf8", ""))
    text = re.sub(r"\s+", " ", "".join(words)).strip()
    return text or None


# ---------- the prompt ----------

def build_prompt(clips, pb, niche):
    winners = "\n".join(f"- {w['title']} ({w['views']:,} views, {w['lift']}x the channel's usual)" for w in pb["winners"][:15])
    hot, by_creator = trending(niche, clips)
    hot_lines = "\n".join(f"- {v['title']} ({v.get('views_per_hour', 0):,.0f} views/hour)" for v in hot)
    mine = own_results()
    mine_lines = "\n".join(f"- {r['title']} ({r['views']:,} views, {r['lift']}x your median)"
                           for r in sorted(mine, key=lambda r: -r["lift"])[:12])
    tags_seen = ", ".join(h for h, _ in pb.get("hashtags", [])[:12])
    review = review_notes()
    lesson_lines = "\n".join(f"- {x}" for k in ("titles", "hooks", "length_and_pacing")
                             for x in (review.get("lessons") or {}).get(k, []))
    verdict_lines = "\n".join(
        f"- \"{r['title']}\": {r['views']:,} views"
        + (f", {r['avg_viewed']:.0f}% watched on average" if r.get("avg_viewed") is not None else "")
        + (f", {r['stayed']:.0%} stayed past the first swipe" if r.get("stayed") is not None else "")
        + (f". {r['verdict']}" if r.get("verdict") else "")
        for r in (review.get("clips") or [])[:8])

    blocks = []
    for i, c in enumerate(clips):
        src = src_of(c)
        b = [f"[{i}] Creator: {src['name']} (call them \"{src['short']}\" in titles)",
             f"    Stream/video title: {c['video_title']}",
             f"    Clip: {fmt_ts(c['start'])} to {fmt_ts(c['end'])}, the moment viewers timestamped is {c['moment']}"]
        if c.get("quotes"):
            b.append("    Viewer comments about this moment: " + " / ".join(f'"{q}"' for q in c["quotes"][:5]))
        if c.get("transcript"):
            b.append(f"    What is said in the clip (auto transcript, may have errors): \"{c['transcript'][:1800]}\"")
        if by_creator.get(c["source"]):
            b.append(f"    {src['short']} clips blowing up this week: "
                     + " / ".join(f"\"{v['title']}\" ({v.get('views_per_hour', 0):,.0f}/h)" for v in by_creator[c["source"]]))
        blocks.append("\n".join(b))

    return f"""You are the script writer for a YouTube Shorts clip channel in the {NICHES[niche]['label']} niche.
Each clip is a 20-60 second cut from a creator's livestream or video, with the channel's own narrator voice over
the first seconds. You write everything that gets uploaded with it.

Breakout Shorts from the top channels in this niche (from our channel study). Study their pattern:
{winners}

Fastest-growing clips in the niche this week (from our trend scan):
{hot_lines or '- (no trend scan yet)'}
""" + (f"""
Our own channel's uploads so far, best first. Lean toward what worked, away from what didn't:
{mine_lines}
""" if mine_lines else "") + (f"""
Our clip reviewer's lessons from how our posted clips held viewers. Follow them:
{lesson_lines}
""" if lesson_lines else "") + (f"""
How our recent clips held viewers:
{verdict_lines}
""" if verdict_lines else "") + f"""
Title pattern: the creator's name first, then exactly what happens in plain, punchy words, about
{min(pb['title_chars'], 55)} characters and never over 70, title case, one or two fitting emoji at the end. The shape to aim
for (the names here are placeholders, never copy them): "<Creator> Crashes Out After <Person> Scores Again 😭". No questions, no hashtags, no ALL-CAPS words, no clickbait the
clip doesn't deliver.

Use the stream title, the viewer comments and the transcript together to work out what actually happens in each clip.
Use only what they support. Don't invent names, scores or events. If what happens is unclear, describe the
reaction (e.g. "<Creator> Can't Believe What Just Happened 😭"). Only name a person who appears in that clip's stream
title, comments or transcript.

For every clip write:
- title: in that pattern.
- hook: big text pinned on screen for the whole clip. Max 6 words. Makes a scroller wait for the payoff without
  spoiling it.
- context: one short line shown for the first 3 seconds so a new viewer gets the setup (who, where, what's going on).
- narrator: one sentence (8-16 words) the narrator voice says over the opening. The channel's own voice: a setup,
  joke or reaction about THIS clip that adds something, written to be spoken aloud. Never generic ("watch this",
  "you won't believe"). No emoji, no hashtags, no "subscribe".
- description: 1-2 plain sentences for the YouTube description saying what happens and who's in it, with the names
  people search for. Describe the clip itself; don't mention viewers, chat or comments. No hashtags, no links (the
  credit and link are added for you).
- hashtags: 3 hashtags for the description (the top channels use: {tags_seen}). Include the creator.
- tags: 8-12 YouTube search tags: creator names and nicknames, other people in the clip, the game/event/topic.

Clips:

""" + "\n\n".join(blocks) + """

Return only JSON: {"clips": [{"index": 0, "title": "...", "hook": "...", "context": "...", "narrator": "...",
"description": "...", "hashtags": ["#..."], "tags": ["..."]}, ...]} with one entry per clip."""


def parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


# ---------- writers ----------

def _claude_code(prompt):
    exe = shutil.which("claude")
    if not exe:
        return None
    r = subprocess.run([exe, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--tools", ""],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=600)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(r.stderr.strip()[:300] or r.stdout.strip()[:300] or f"exit {r.returncode}")
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    return out.get("structured_output") or parse_json(out["result"])


def _ollama(prompt):
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        tags = requests.get(f"{host}/api/tags", timeout=3).json().get("models", [])
    except requests.RequestException:
        return None
    if not tags:
        return None
    model = os.environ.get("OLLAMA_MODEL") or tags[0]["name"]
    r = requests.post(f"{host}/api/generate", timeout=900, json={
        "model": model, "prompt": prompt, "format": SCHEMA, "stream": False, "options": {"temperature": 0.7, "num_ctx": 8192}})
    r.raise_for_status()
    return parse_json(r.json()["response"])


def _api(prompt):
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic
    from pydantic import BaseModel

    class Script(BaseModel):
        index: int
        title: str
        hook: str
        context: str
        narrator: str
        description: str
        hashtags: list[str]
        tags: list[str]

    class Batch(BaseModel):
        clips: list[Script]

    resp = anthropic.Anthropic().messages.parse(
        model="claude-opus-5-5", max_tokens=16000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}], output_format=Batch)
    if resp.stop_reason == "refusal" or not resp.parsed_output:
        return None
    return resp.parsed_output.model_dump()


WRITERS = {"claude-code": _claude_code, "ollama": _ollama, "api": _api}
AUTO = ["ollama", "free"]  # what runs when no writer is picked: both free. Claude only when you ask for it by name.


def template_script(c, pb):
    src = src_of(c)
    quote = c["quotes"][0] if c.get("quotes") else "moment"
    quote = re.sub(r"\b(bro|lmao|lol|lmfao|fr|ngl|bruh)\b", "", quote, flags=re.I)
    quote = re.sub(r"\s+", " ", quote).strip(" .,!?\"'")
    emoji = "😭" if re.search(r"laugh|funny|😂|crying|dead|lol", " ".join(c.get("quotes", [])), re.I) else "🤯"
    return {"title": f"{src['short']} {quote[:pb['title_chars'] - len(src['short']) - 3]}".strip() + f" {emoji}",
            "hook": quote[:40].upper() or f"{src['short'].upper()} LOST IT",
            "context": f"{src['name']} live, {c['video_title'][:60]}",
            "take": TEMPLATE_TAKES[sum(map(ord, c.get("video_id", "x"))) % len(TEMPLATE_TAKES)].format(n=src["short"]),
            "summary": f"{src['name']} live: {c['video_title']}", "hashtags": [], "tags": [],
            "draft": True, "writer": "template"}


def clean(s, limit):
    s = re.sub(r"#[A-Za-z]\w*", "", s or "").strip().strip('"')
    return s[:limit].rstrip()


def to_scripts(out, clips, pb, writer):
    by_i = {c["index"]: c for c in (out or {}).get("clips", []) if isinstance(c, dict) and "index" in c}
    scripts = []
    for i, c in enumerate(clips):
        s = by_i.get(i)
        if not s or not s.get("title"):
            scripts.append(template_script(c, pb))
            continue
        title = clean(s["title"], 100)
        if len(title) > 80:  # long titles get cut off in the Shorts feed: end on a whole word
            title = title[:80].rsplit(" ", 1)[0]
        scripts.append({"title": title, "hook": clean(s.get("hook"), 50).upper(),
                        "context": clean(s.get("context"), 140),
                        "take": re.sub(r"[#@]\w+", "", s.get("narrator") or s.get("take") or "").strip(),
                        "summary": re.sub(r"https?://\S+|#[A-Za-z]\w*", "", s.get("description") or "").strip()[:600],
                        "hashtags": ["#" + re.sub(r"\W", "", h).lower() for h in s.get("hashtags") or [] if re.sub(r"\W", "", h)][:3],
                        "tags": [t.strip()[:60] for t in s.get("tags") or [] if t.strip()][:12],
                        "draft": False, "writer": writer})
    return scripts


def free_scripts(clips, pb, niche):
    """The no-model writer (free_writer.py): never fails, never costs anything."""
    import free_writer
    _, by_creator = trending(niche, clips)
    out = free_writer.write_batch(clips, src_of, by_creator, (review_notes() or {}).get("lessons"))
    return to_scripts(out, clips, pb, "free")


def write_scripts(clips, pb, niche, writer=None):
    if writer == "template":
        return [template_script(c, pb) for c in clips]
    if writer == "free":
        return free_scripts(clips, pb, niche)
    prompt = build_prompt(clips, pb, niche)
    order = [writer] if writer in WRITERS else AUTO
    for name in order:
        if name == "free":
            break
        try:
            out = WRITERS[name](prompt)
        except Exception as e:  # a writer failing should never stop the day's clips
            print(f"Writer {name} failed ({type(e).__name__}: {str(e)[:200]}).")
            continue
        if out:
            print(f"Scripts written by {name}.")
            scripts = to_scripts(out, clips, pb, name)
            missing = [i for i, s in enumerate(scripts) if s.get("draft")]
            if missing:  # the model skipped some clips: the free writer fills those in
                free = free_scripts([clips[i] for i in missing], pb, niche)
                for i, f in zip(missing, free):
                    scripts[i] = f
            return scripts
        if writer:
            print(f"Writer {name} isn't set up here (see the top of script_agent.py).")
    print("Scripts written by the free writer (no AI model).")
    return free_scripts(clips, pb, niche)


# ---------- applying and saving ----------

def description(c):
    src = src_of(c)
    kind = "stream" if c["platform"].startswith("twitch") or c.get("signals") or "live" in c["video_title"].lower() else "video"
    tags = list(dict.fromkeys(c.get("hashtags", []) + src.get("hashtags", []) + ["#shorts"]))[:6]
    return (f"{c.get('summary') or c['context']}\n\nFull {kind} by {src['name']}: {c['url']}\n"
            f"Go support {src['name']}, all credit to them.\n\n" + " ".join(tags))


def upload_tags(c):
    """YouTube tags: the writer's, then the creator's names; YouTube caps the total at 500 characters."""
    out, total = [], 0
    for t in list(dict.fromkeys(c.get("tags", []) + src_of(c).get("aliases", []) + ["shorts"])):
        if total + len(t) + 2 > 480:
            break
        out.append(t)
        total += len(t) + 2
    return out


def apply(clip, script, source):
    clip.update(script)
    clip["script_source"] = source  # what the writer saw: "transcript", "captions" or "comments"
    clip["description"] = description(clip)
    clip["upload_tags"] = upload_tags(clip)


def save(qdir, queue):
    qfile = qdir / "queue.json"
    backup = qdir / "queue.before_scripts.json"
    if qfile.exists() and not backup.exists():
        shutil.copy(qfile, backup)
    qfile.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")

    state_file = BUILDER_DIR / "state.json"  # keep the builder's record of queued titles in sync for `track`
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for c in queue:
            if c.get("key") in state.get("queued", {}):
                state["queued"][c["key"]]["title"] = c["title"]
        state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    lines = [f"# Clip scripts, {qdir.name}", ""]
    for c in queue:
        lines += [f"## {c['id']}. {c['title']}", "",
                  f"- **Source:** {c['creator']}, [{c['video_title']}]({c['url']}), cut {fmt_ts(c['start'])} to {fmt_ts(c['end'])}",
                  f"- **On-screen hook:** {c['hook']}",
                  f"- **Context line (first 3s):** {c['context']}",
                  f"- **Narrator:** {c['take']}",
                  f"- **Tags:** {', '.join(c.get('upload_tags', []))}",
                  f"- **Written by:** {c.get('writer', 'template')} from {c.get('script_source', 'comments')}"
                  + (" (draft: edit before posting)" if c.get("draft") else ""),
                  f"- **Post:** {c.get('post_local', '')}", "", "Description:", "", "```", c["description"], "```", ""]
    (qdir / "scripts.md").write_text("\n".join(lines), encoding="utf-8")


def script_queue(qdir, queue, ids=None, writer=None, captions=True, transcripts=None, dry_run=False):
    """Write scripts for the queue's clips in place and save. `transcripts` maps clip id -> text (from cut).
    Called by channel_builder.py daily and cut; also the CLI below."""
    qdir = Path(qdir)
    todo = [c for c in queue if not c.get("youtube_id") and (not ids or c["id"] in ids)]
    if not todo:
        return []
    niche = next((SOURCES[c["source"]]["niche"] for c in todo if c["source"] in SOURCES), "streamer")
    pb = playbook(niche)
    items, sources = [], []
    for c in todo:
        t, kind = (transcripts or {}).get(c["id"]), "transcript"
        if not t:
            t, kind = cut_transcript(qdir, c), "transcript"
        if not t and captions:
            t, kind = caption_transcript(qdir, c), "captions"
        kind = kind if t else "comments"
        print(f"  [{c['id']}] {c['creator']} at {c['moment']}: writing from {kind}" + (f" ({len(t.split())} words)" if t else ""))
        items.append({**c, "transcript": t})
        sources.append(kind)
    scripts = write_scripts(items, pb, niche, writer or os.environ.get("SCRIPT_WRITER"))
    for c, s, kind in zip(todo, scripts, sources):
        if s.get("draft") and c.get("draft") is False:
            continue  # the writer failed this time: keep the written script rather than a template
        apply(c, s, kind)
    if not dry_run:
        save(qdir, queue)
    return todo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", help="queue folder (default: latest reports/builder/daily_*)")
    ap.add_argument("--ids", type=int, nargs="*", help="only these queue items")
    ap.add_argument("--writer", choices=[*WRITERS, "free", "template"], default=os.environ.get("SCRIPT_WRITER"))
    ap.add_argument("--no-captions", action="store_true", help="don't fetch YouTube captions")
    ap.add_argument("--dry-run", action="store_true", help="print the scripts, don't save them")
    ap.add_argument("--prompt-only", action="store_true", help="save the prompt to paste into claude.ai")
    ap.add_argument("--import", dest="import_file", help="JSON answer pasted back from claude.ai")
    args = ap.parse_args()

    qdir = Path(args.queue) if args.queue else max(BUILDER_DIR.glob("daily_*"), key=lambda p: p.stat().st_mtime, default=None)
    if not qdir or not (qdir / "queue.json").exists():
        sys.exit("No queue yet. Run `python channel_builder.py daily` first.")
    queue = json.loads((qdir / "queue.json").read_text(encoding="utf-8"))
    todo = [c for c in queue if not c.get("youtube_id") and (not args.ids or c["id"] in args.ids)]
    if not todo:
        sys.exit("Nothing to write: those clips are already uploaded.")

    if args.prompt_only or args.import_file:
        niche = next((SOURCES[c["source"]]["niche"] for c in todo if c["source"] in SOURCES), "streamer")
        pb = playbook(niche)
        items = [{**c, "transcript": cut_transcript(qdir, c) or (None if args.no_captions else caption_transcript(qdir, c))}
                 for c in todo]
        if args.prompt_only:
            (qdir / "script_prompt.txt").write_text(build_prompt(items, pb, niche), encoding="utf-8")
            print(f"Paste {qdir / 'script_prompt.txt'} into claude.ai, save its JSON answer as answer.json, then run\n"
                  f"  python script_agent.py --queue \"{qdir}\" --import answer.json"
                  + (f" --ids {' '.join(map(str, args.ids))}" if args.ids else ""))
            return
        scripts = to_scripts(parse_json(Path(args.import_file).read_text(encoding="utf-8")), items, pb, "claude.ai")
        for c, it, s in zip(todo, items, scripts):
            apply(c, s, "captions" if it["transcript"] else "comments")
        if not args.dry_run:
            save(qdir, queue)
    else:
        script_queue(qdir, queue, args.ids, args.writer, not args.no_captions, dry_run=args.dry_run)

    for c in todo:
        print(f"\n[{c['id']}] {c['title']}\n    hook:     {c['hook']}\n    context:  {c['context']}\n    narrator: {c['take']}"
              f"\n    tags:     {', '.join(c['upload_tags'])}\n    description:\n      " + c["description"].replace("\n", "\n      "))
    if not args.dry_run:
        print(f"\nSaved to {qdir / 'queue.json'} (cut/publish use these). Readable copy: {qdir / 'scripts.md'}")


if __name__ == "__main__":
    main()
