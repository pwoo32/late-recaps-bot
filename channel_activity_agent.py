"""Channel activity agent: keeps Late Recaps looking alive between uploads.

Every run it:
- checks the upload schedule: when the last Short went public, what's scheduled next, and any clip stuck private
  or rejected. If the channel has gone quiet it says so loudly (and pops a Windows notification with --notify).
- reads new comments on the channel and drafts a short reply to each real one, in the channel's voice. Spam
  (links, "check my channel", crypto/telegram scams) is marked to hold for review instead.
- drafts a first comment for each new public Short (a question that gets people replying), for you to pin.
- every few days drafts 2 community posts (a poll and a text post) built from the clips that did best.

Nothing posts unless you pass --post (or run `post` after reading the drafts). With --post it replies to the
comments, posts the first comments and holds the spam. YouTube's API has no way to heart or pin a comment or to
make a community post, so the report links each of those for a one-click finish in YouTube/Studio.

Usage:
    python channel_activity_agent.py auth          # once: sign in with comment access (opens a browser)
    python channel_activity_agent.py               # check + draft only; report in reports/activity_<date>/report.md
    python channel_activity_agent.py post          # post the latest drafts (edit drafts.json first if you like)
    python channel_activity_agent.py --post        # check, draft and post in one go (what run_activity.cmd does)
    python channel_activity_agent.py --no-llm      # schedule check only, no drafting

Drafts are written by a free writer: Gemini or Groq (GEMINI_API_KEY / GROQ_API_KEY, as the script agent uses), else a
local Ollama model. Claude Code or ANTHROPIC_API_KEY only if picked with --writer. The cloud daily run
(.github/workflows/daily.yml) runs it with --post after the review; its login goes up as the ACTIVITY_TOKEN secret.
Quota: reading costs ~3-6 units; each posted reply/comment/hold costs 50, capped by --max-replies (default 20).
Its login is its own file (~/.config/clip-builder/activity_token.json), so uploading and reviewing are never affected.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).parent
REPORTS_DIR = HERE / "reports"
ACTIVITY_DIR = REPORTS_DIR / "activity"
STATE_FILE = ACTIVITY_DIR / "state.json"
BUILDER_STATE = REPORTS_DIR / "builder" / "state.json"  # read only: video ids the builder uploaded
LOCAL_TZ = ZoneInfo("America/New_York")
TOKEN_DIR = Path.home() / ".config" / "clip-builder"
ACTIVITY_TOKEN = TOKEN_DIR / "activity_token.json"
READ_TOKENS = [TOKEN_DIR / "review_token.json", TOKEN_DIR / "youtube_token.json"]  # fallback for check-only runs
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]  # read the channel + post/moderate comments

SPAM = re.compile(r"https?://|www\.|\.com\b|t\.me|telegram|whats ?app|crypto|bitcoin|forex|invest(ing|ment)|"
                  r"check (out )?my (channel|page)|sub(scribe)? (to me|back)|sub4sub|\+\d{7,}|onlyfans|giveaway", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "replies": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "action": {"type": "string", "enum": ["reply", "skip", "hold"]},
            "text": {"type": "string"}}, "required": ["id", "action", "text"]}},
        "first_comments": {"type": "array", "items": {"type": "object", "properties": {
            "video_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["video_id", "text"]}},
        "community": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["poll", "text"]}, "text": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}}, "required": ["kind", "text", "options"]}},
    },
    "required": ["replies", "first_comments", "community"],
}


# ---------- state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"replied": [], "first_commented": [], "held": [], "last_comment_at": None, "community_at": None}


def save_state(state):
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    state["replied"] = state["replied"][-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def local(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%a %b %d %I:%M %p ET") if dt else "n/a"


def hours(td):
    return td.total_seconds() / 3600


# ---------- signing in ----------

def google():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        sys.exit("pip install google-api-python-client google-auth-oauthlib")
    return dict(RefreshError=RefreshError, Request=Request, Credentials=Credentials, Flow=InstalledAppFlow,
                build=build, HttpError=HttpError)


def signed_in(g):
    """(youtube client, can_write). Never opens a browser, so the scheduled run can't hang on a sign-in."""
    for f in [ACTIVITY_TOKEN] + READ_TOKENS:
        if not f.exists():
            continue
        creds = g["Credentials"].from_authorized_user_info(json.loads(f.read_text(encoding="utf-8-sig")))
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(g["Request"]())
            except g["RefreshError"]:
                print(f"Saved sign-in {f.name} has expired."
                      + (" Run `python channel_activity_agent.py auth` again." if f == ACTIVITY_TOKEN else ""))
                continue
            f.write_text(creds.to_json(), encoding="utf-8")
        if creds.valid:
            can_write = any("force-ssl" in s for s in (creds.scopes or []))
            return g["build"]("youtube", "v3", credentials=creds, cache_discovery=False), can_write
    sys.exit("Not signed in. Run `python channel_activity_agent.py auth` once.")


def cmd_auth(args):
    g = google()
    secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", HERE / "client_secret.json"))
    if not secrets.exists():
        sys.exit(f"No OAuth client at {secrets}. It's the same client_secret.json the uploader uses (README: Uploading).")
    creds = g["Flow"].from_client_secrets_file(str(secrets), SCOPES).run_local_server(port=0)
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVITY_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(ACTIVITY_TOKEN, 0o600)
    yt = g["build"]("youtube", "v3", credentials=creds, cache_discovery=False)
    me = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    print(f"Signed in. Managing comments on: {me[0]['snippet']['title']}" if me else
          "Signed in, but this Google account has no YouTube channel. Sign in with the one that owns Late Recaps.")


# ---------- reading the channel ----------

def channel_videos(yt):
    ch = yt.channels().list(part="id,snippet,statistics,contentDetails", mine=True).execute()["items"][0]
    pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [it["contentDetails"]["videoId"] for it in
           yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50).execute().get("items", [])]
    if BUILDER_STATE.exists():  # scheduled clips can be missing from the uploads list until they go public
        queued = json.loads(BUILDER_STATE.read_text(encoding="utf-8")).get("queued", {})
        ids += [q["video_id"] for q in queued.values() if q.get("video_id") and q["video_id"] not in ids]
    vids = []
    for i in range(0, len(ids), 50):
        vids += yt.videos().list(part="snippet,statistics,status", id=",".join(ids[i:i + 50])).execute().get("items", [])
    return ch, vids


def schedule_check(vids, now, quiet_hours):
    """Alerts (channel quiet, nothing scheduled, stuck or rejected clips) and the facts behind them."""
    public, scheduled, stuck, broken = [], [], [], []
    for v in vids:
        st, sn = v["status"], v["snippet"]
        if st.get("uploadStatus") in ("rejected", "failed", "deleted"):
            broken.append((v, st.get("rejectionReason") or st.get("failureReason") or st["uploadStatus"]))
        elif st["privacyStatus"] == "public":
            public.append((parse_time(sn["publishedAt"]), v))
        elif st.get("publishAt") and parse_time(st["publishAt"]) > now:
            scheduled.append((parse_time(st["publishAt"]), v))
        elif st["privacyStatus"] == "private":
            stuck.append(v)  # past its publish time but still private: the unaudited-API lock, or a missed schedule
    public.sort(key=lambda x: x[0], reverse=True)
    scheduled.sort(key=lambda x: x[0])
    last = public[0][0] if public else None
    alerts = []
    if not last or hours(now - last) > quiet_hours:
        alerts.append(f"Channel has gone quiet: last public Short {local(last)}"
                      + (f" ({hours(now - last):.0f}h ago)." if last else ". No public Shorts at all."))
    if not scheduled:
        alerts.append("Nothing is scheduled to go public. Check builder.log / the latest reports/builder/daily_*/HOLD.md.")
    elif hours(scheduled[0][0] - now) > 24:
        alerts.append(f"Next scheduled Short isn't until {local(scheduled[0][0])}.")
    if stuck:
        alerts.append(f"{len(stuck)} Short(s) still private past their post time: flip them to public in Studio.")
    for v, why in broken:
        alerts.append(f"Upload problem on \"{v['snippet']['title']}\": {why}.")
    last24 = sum(1 for t, _ in public if hours(now - t) <= 24)
    return dict(public=public, scheduled=scheduled, stuck=stuck, broken=broken, last=last, last24=last24, alerts=alerts)


def latest_hold():
    holds = sorted((REPORTS_DIR / "builder").glob("*_20*/HOLD.md"), key=lambda p: p.stat().st_mtime)
    if holds and datetime.now().timestamp() - holds[-1].stat().st_mtime < 36 * 3600:
        return holds[-1]
    return None


def new_comments(yt, cid, since, titles, state):
    """Top-level comments since `since` that the channel hasn't answered yet (newest first)."""
    out, token, done = [], None, False
    answered = set(state["replied"]) | set(state["held"])
    mine_on = set()
    while not done:
        kw = dict(part="snippet,replies", allThreadsRelatedToChannelId=cid, maxResults=100, order="time",
                  textFormat="plainText")
        if token:
            kw["pageToken"] = token
        resp = yt.commentThreads().list(**kw).execute()
        for t in resp.get("items", []):
            top = t["snippet"]["topLevelComment"]
            s = top["snippet"]
            author = (s.get("authorChannelId") or {}).get("value")
            if author == cid:
                mine_on.add(t["snippet"].get("videoId"))
                continue
            if parse_time(s["publishedAt"]) < since:
                done = True
                break
            if top["id"] in answered:
                continue
            if any((r["snippet"].get("authorChannelId") or {}).get("value") == cid
                   for r in t.get("replies", {}).get("comments", [])):
                continue
            out.append(dict(id=top["id"], video_id=t["snippet"].get("videoId"), author=s.get("authorDisplayName", ""),
                            text=s.get("textDisplay", "")[:500], likes=s.get("likeCount", 0),
                            at=s["publishedAt"], video=titles.get(t["snippet"].get("videoId"), ""),
                            spam=bool(SPAM.search(s.get("textDisplay", "")))))
        token = resp.get("nextPageToken")
        done = done or not token
    return out, mine_on


# ---------- drafting ----------

def build_prompt(comments, fresh, top, ch_name):
    rules = (
        f"You run the comment section of {ch_name}, a YouTube Shorts channel that posts short captioned clips of "
        "streamers (IShowSpeed, Kai Cenat, Jynxzi, Adin Ross, N3on) and podcasts, with a narrator line at the start. "
        "Goal: make the channel feel active and get viewers talking so the Shorts feed keeps pushing the clips.\n\n"
        "Voice: a fan who runs a clip page. Short (under 15 words), casual, lowercase is fine, 0-1 emoji. Agree, joke, "
        "or ask a quick follow-up question. Never argue, insult, promise anything, ask for subs in a reply, post links, "
        "or pretend to be the streamer. Never repeat the same reply twice.\n\n"
        "1. replies: one per comment below, by id. action 'reply' with the text; 'hold' for spam, scams, self-promo, "
        "slurs or harassment (text empty); 'skip' when there's nothing worth saying (lone emoji, gibberish, a bare "
        "'first') or it's hateful toward someone (text empty). Comments flagged spam=true are almost always 'hold'. "
        "If a viewer suggests a clip or streamer, thank them and say you'll look at it.\n"
        "2. first_comments: one per new video below: a question or hot take about that exact moment that people will "
        "want to answer (e.g. 'was speed wrong for this or nah 😭'). Under 15 words. It gets pinned.\n"
        "3. community: " + ("2 community posts: one poll (text = the question, 2-4 short options) about which "
                            "creator or moment to clip next, built from the top clips below, and one text post "
                            "(a teaser or question, options empty). Under 30 words each."
                            if top is not None else "return an empty list.") + "\n\n")
    body = "COMMENTS (JSON):\n" + json.dumps([{k: c[k] for k in ("id", "video", "author", "text", "likes", "spam")}
                                             for c in comments], ensure_ascii=False)
    body += "\n\nNEW VIDEOS (JSON):\n" + json.dumps([{"video_id": v["id"], "title": v["snippet"]["title"],
                                                      "description": v["snippet"].get("description", "")[:300]}
                                                     for v in fresh], ensure_ascii=False)
    if top is not None:
        body += "\n\nTOP CLIPS LAST 2 WEEKS (JSON):\n" + json.dumps(top, ensure_ascii=False)
    return rules + body + "\n\nReturn only JSON matching the schema."


def parse_json(text):
    m = re.search(r"\{.*\}", text.strip(), re.S)
    return json.loads(m.group(0) if m else text)


def _claude_code(prompt):
    exe = shutil.which("claude")
    if not exe:
        return None
    r = subprocess.run([exe, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--tools", ""],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or r.stdout.strip()[:300])
    out = json.loads(r.stdout)
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
    r = requests.post(f"{host}/api/generate", timeout=900, json={
        "model": os.environ.get("OLLAMA_MODEL") or tags[0]["name"], "prompt": prompt, "format": SCHEMA,
        "stream": False, "options": {"temperature": 0.8}})
    r.raise_for_status()
    return parse_json(r.json()["response"])


def _api(prompt):
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=8000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    if resp.stop_reason == "refusal":
        return None
    return parse_json("".join(b.text for b in resp.content if b.type == "text"))


def _free(name):
    def run(prompt):  # the script agent's free writers (GEMINI_API_KEY / GROQ_API_KEY); skipped without a key
        import script_agent
        out = getattr(script_agent, name)(prompt)
        return parse_json(out) if isinstance(out, str) else out
    return run


WRITERS = {"gemini": _free("_gemini"), "groq": _free("_groq"), "claude-code": _claude_code, "ollama": _ollama, "api": _api}
AUTO = ["gemini", "groq", "ollama"]  # free by default, so the cloud run can draft without a local model


def draft(prompt, writer=None):
    for name in ([writer] if writer in WRITERS else AUTO):  # Claude only if picked
        try:
            out = WRITERS[name](prompt)
        except Exception as e:
            print(f"Writer {name} failed ({type(e).__name__}: {str(e)[:200]}).")
            continue
        if isinstance(out, dict) and "replies" in out:
            print(f"Drafts written by {name}.")
            return out, name
    print("No writer available: spam is still held, but no replies or comments were drafted.")
    return None, None


def clean(s, limit=300):
    return re.sub(r"\s+", " ", (s or "")).strip()[:limit]


def to_drafts(out, comments, fresh, max_replies):
    by_id = {c["id"]: c for c in comments}
    drafts = {"replies": [], "holds": [], "first_comments": [], "community": []}
    # writer missing or silent on a comment: still hold the obvious spam
    actions = {r["id"]: r for r in (out or {}).get("replies", []) if r.get("id") in by_id}
    for c in comments:
        r = actions.get(c["id"]) or {"action": "hold" if c["spam"] else "skip", "text": ""}
        if r["action"] == "hold" or c["spam"] and r["action"] == "reply":
            drafts["holds"].append(dict(c, posted=False))
        elif r["action"] == "reply" and clean(r["text"]) and not SPAM.search(r["text"]):
            drafts["replies"].append(dict(c, reply=clean(r["text"], 200), posted=False))
    drafts["replies"].sort(key=lambda c: c["likes"], reverse=True)
    drafts["replies"] = drafts["replies"][:max_replies]
    fresh_ids = {v["id"] for v in fresh}
    for f in (out or {}).get("first_comments", []):
        if f.get("video_id") in fresh_ids and clean(f.get("text")) and not SPAM.search(f["text"]):
            drafts["first_comments"].append(dict(video_id=f["video_id"], text=clean(f["text"], 200), posted=False))
    for p in (out or {}).get("community", [])[:3]:
        drafts["community"].append(dict(kind=p.get("kind", "text"), text=clean(p.get("text"), 500),
                                        options=[clean(o, 65) for o in p.get("options", [])][:5]))
    return drafts


# ---------- posting ----------

def post_drafts(yt, drafts, state, budget):
    """Post replies, first comments and spam holds until the per-run cap. Marks each one so reruns never double-post."""
    used = 0
    for c in drafts["holds"]:
        if c["posted"] or used >= budget:
            continue
        try:
            yt.comments().setModerationStatus(id=c["id"], moderationStatus="heldForReview").execute()
            c["posted"] = True
            state["held"].append(c["id"])
            used += 1
        except Exception as e:
            print(f"  couldn't hold {c['id']}: {str(e)[:200]}")
    for c in drafts["replies"]:
        if c["posted"] or used >= budget:
            continue
        try:
            yt.comments().insert(part="snippet", body={"snippet": {"parentId": c["id"], "textOriginal": c["reply"]}}).execute()
            c["posted"] = True
            state["replied"].append(c["id"])
            used += 1
            print(f"  replied to {c['author']}: {c['reply']}")
        except Exception as e:
            if "quotaExceeded" in str(e):
                print("  Out of API quota for today; the rest post on the next run.")
                return used
            print(f"  couldn't reply to {c['id']}: {str(e)[:200]}")
    for f in drafts["first_comments"]:
        if f["posted"] or used >= budget or f["video_id"] in state["first_commented"]:
            continue
        try:
            resp = yt.commentThreads().insert(part="snippet", body={"snippet": {
                "videoId": f["video_id"], "topLevelComment": {"snippet": {"textOriginal": f["text"]}}}}).execute()
            f["posted"] = True
            f["comment_id"] = resp["id"]
            state["first_commented"].append(f["video_id"])
            used += 1
            print(f"  commented on {f['video_id']}: {f['text']} (pin it: https://youtube.com/shorts/{f['video_id']})")
        except Exception as e:
            print(f"  couldn't comment on {f['video_id']}: {str(e)[:200]}")
    return used


def notify(title, text):
    """Windows tray notification (no extra installs). Silently does nothing elsewhere."""
    if os.name != "nt":
        return
    esc = lambda s: s.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
          f"$n.ShowBalloonTip(15000, '{esc(title)}', '{esc(text[:250])}', 'Warning'); Start-Sleep 16; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps])
    except OSError:
        pass


# ---------- report ----------

def write_report(out_dir, ch, sched, drafts, posted, writer, args, can_write, now, hold_file):
    vid_title = {}
    for _, v in sched["public"] + sched["scheduled"]:
        vid_title[v["id"]] = v["snippet"]["title"]
    L = [f"# Channel activity, {ch['snippet']['title']}, {local(now)}", ""]
    stats = ch.get("statistics", {})
    L += [f"{int(stats.get('subscriberCount', 0)):,} subscribers, {int(stats.get('videoCount', 0))} videos. "
          f"{sched['last24']} Short(s) went public in the last 24h.", ""]
    L += ["## Alerts", ""] + ([f"- **{a}**" for a in sched["alerts"]] or ["- None. Uploads are on schedule."])
    if hold_file:
        L += [f"- The builder held posting: see `{hold_file.relative_to(HERE)}`."]
    L += ["", "## Schedule", "",
          f"- Last public: {local(sched['last'])}" + (f", \"{sched['public'][0][1]['snippet']['title']}\"" if sched["public"] else "")]
    for t, v in sched["scheduled"][:6]:
        L.append(f"- Scheduled {local(t)}: \"{v['snippet']['title']}\"")
    for v in sched["stuck"]:
        L.append(f"- **Stuck private:** \"{v['snippet']['title']}\" (https://studio.youtube.com/video/{v['id']}/edit)"
                 " > Visibility > Public")
    mode = "posted" if args.post and can_write else "drafted (not posted)"
    L += ["", f"## Comment replies ({len(drafts['replies'])} {mode})", ""]
    for c in drafts["replies"]:
        L.append(f"- [{c['video'][:50]}](https://www.youtube.com/watch?v={c['video_id']}&lc={c['id']}) "
                 f"**{c['author']}**: \"{c['text'][:140]}\"  \n  → {c['reply']}" + ("" if c["posted"] else "  _(not posted)_"))
    if drafts["replies"]:
        L += ["", "Hearting isn't in YouTube's API: open a comment link above and tap the heart on the good ones "
                  "(a creator heart gets people to comment more)."]
    if drafts["holds"]:
        L += ["", f"## Held as spam ({len(drafts['holds'])})", ""]
        L += [f"- **{c['author']}**: \"{c['text'][:120]}\"" + ("" if c["posted"] else " _(not held yet)_")
              for c in drafts["holds"]]
        L += ["", "Review them in Studio > Comments > Held for review."]
    if drafts["first_comments"]:
        L += ["", "## First comments to pin", ""]
        for f in drafts["first_comments"]:
            L.append(f"- \"{vid_title.get(f['video_id'], f['video_id'])}\": {f['text']}  \n  "
                     + ("Posted. Pin it: open https://www.youtube.com/shorts/" + f["video_id"] + " > comments > ⋮ on "
                        "your comment > Pin." if f["posted"] else "_(not posted)_"))
    if drafts["community"]:
        L += ["", "## Community post drafts", "",
              "Paste into YouTube: Create > Create post (https://www.youtube.com/channel/" + ch["id"] + "/community). "
              "The API can't post these for you.", ""]
        for p in drafts["community"]:
            L.append(f"- **{p['kind'].title()}:** {p['text']}" + (" | Options: " + " / ".join(p["options"]) if p["options"] else ""))
    if not can_write:
        L += ["", "_Signed in read-only, so nothing can post. Run `python channel_activity_agent.py auth` once._"]
    L += ["", f"_Drafts by {writer or 'no writer'}. API calls that posted or held: {posted} (50 units each)._", ""]
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


# ---------- commands ----------

def cmd_run(args):
    g = google()
    yt, can_write = signed_in(g)
    state = load_state()
    now = datetime.now(timezone.utc)
    ch, vids = channel_videos(yt)
    sched = schedule_check(vids, now, args.quiet_hours)
    hold_file = latest_hold()
    print(f"{ch['snippet']['title']}: last public {local(sched['last'])}, {len(sched['scheduled'])} scheduled.")
    for a in sched["alerts"]:
        print("  ALERT: " + a)

    drafts, writer = {"replies": [], "holds": [], "first_comments": [], "community": []}, None
    if not args.no_llm:
        since = parse_time(state["last_comment_at"]) or now - timedelta(days=7)
        titles = {v["id"]: v["snippet"]["title"] for v in vids}
        comments, mine_on = new_comments(yt, ch["id"], since - timedelta(hours=1), titles, state)
        fresh = [v for t, v in sched["public"] if hours(now - t) <= 72
                 and v["id"] not in state["first_commented"] and v["id"] not in mine_on]
        community_age = hours(now - parse_time(state["community_at"])) if state["community_at"] else 999
        top = None
        if community_age >= args.community_every * 24:
            recent = [v for t, v in sched["public"] if hours(now - t) <= 14 * 24]
            recent.sort(key=lambda v: int(v["statistics"].get("viewCount", 0)), reverse=True)
            top = [{"title": v["snippet"]["title"], "views": int(v["statistics"].get("viewCount", 0))} for v in recent[:8]]
        print(f"{len(comments)} new comment(s), {len(fresh)} new Short(s) without a first comment.")
        out = None
        if comments or fresh or top is not None:
            out, writer = draft(build_prompt(comments, fresh, top, ch["snippet"]["title"]),
                                args.writer or os.environ.get("SCRIPT_WRITER"))
        drafts = to_drafts(out, comments, fresh, args.max_replies)
        if drafts["community"]:
            state["community_at"] = now.isoformat()
        if comments and (out or not any(not c["spam"] for c in comments)):  # writer down: retry them next run
            state["last_comment_at"] = max(c["at"] for c in comments)

    posted = 0
    if args.post:
        if can_write:
            posted = post_drafts(yt, drafts, state, args.max_replies + len(drafts["first_comments"]) + len(drafts["holds"]))
        else:
            print("Signed in read-only, so nothing was posted. Run `python channel_activity_agent.py auth` once.")
    save_state(state)
    out_dir = REPORTS_DIR / f"activity_{datetime.now():%Y-%m-%d_%H%M}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drafts.json").write_text(json.dumps(drafts, indent=1, ensure_ascii=False), encoding="utf-8")
    write_report(out_dir, ch, sched, drafts, posted, writer, args, can_write, now, hold_file)
    if sched["alerts"] and args.notify:
        notify("Late Recaps needs you", " ".join(sched["alerts"]))
    print(f"Done. Report: {out_dir / 'report.md'}")


def cmd_post(args):
    runs = sorted(REPORTS_DIR.glob("activity_*/drafts.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit("No drafts yet. Run `python channel_activity_agent.py` first.")
    f = Path(args.drafts) if args.drafts else runs[-1]
    drafts = json.loads(f.read_text(encoding="utf-8"))
    yt, can_write = signed_in(google())
    if not can_write:
        sys.exit("Signed in read-only. Run `python channel_activity_agent.py auth` once.")
    state = load_state()
    n = post_drafts(yt, drafts, state, 10_000)
    save_state(state)
    f.write_text(json.dumps(drafts, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Done. {n} posted from {f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="run", choices=["run", "auth", "post"])
    ap.add_argument("--post", action="store_true", help="post replies/first comments and hold spam (default: draft only)")
    ap.add_argument("--no-llm", action="store_true", help="schedule check only")
    ap.add_argument("--writer", choices=list(WRITERS), help="who drafts (default: first available)")
    ap.add_argument("--max-replies", type=int, default=20, help="most replies posted per run (50 quota units each)")
    ap.add_argument("--quiet-hours", type=float, default=30, help="alert when nothing went public for this long")
    ap.add_argument("--community-every", type=float, default=3, help="days between community post drafts")
    ap.add_argument("--notify", action="store_true", help="Windows notification when there's an alert")
    ap.add_argument("--drafts", help="post: a drafts.json other than the latest")
    args = ap.parse_args()
    {"run": cmd_run, "auth": cmd_auth, "post": cmd_post}[args.command](args)


if __name__ == "__main__":
    main()
