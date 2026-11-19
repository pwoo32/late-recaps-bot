"""TikTok posting for the clip channel, through TikTok's official Content Posting API.

Two modes (state.json "tiktok.mode", set with `tiktok-auth --mode`):
- inbox  (default): each clip lands in your TikTok inbox as a draft. Tap the notification in the TikTok app,
                    paste the caption (printed + saved in queue.json), and post. Works before TikTok's audit.
                    TikTok allows at most 5 pending drafts per 24 hours.
- direct:           posts straight to your profile with the caption. Until TikTok audits your app, these posts
                    are private (only you see them) and your TikTok account must be set to private.

Setup: see README "TikTok". Needs TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET as environment variables.
The login is kept in ~/.config/clip-builder/tiktok_token.json, outside the project folder.
"""

import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://open.tiktokapis.com/v2"
REDIRECT_PORT = 8723
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
SCOPES = {"inbox": "user.info.basic,video.upload", "direct": "user.info.basic,video.upload,video.publish"}
STATS_SCOPE = "video.list"  # Login Kit scope: view/like/comment/share counts of your public videos, for review_agent.py
TOKEN_FILE = Path.home() / ".config" / "clip-builder" / "tiktok_token.json"
MAX_PENDING_INBOX_DRAFTS = 4  # TikTok allows 5 pending drafts per 24h; leave one slot as a safety buffer.


def _client():
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (key and secret):
        sys.exit("Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET (from your app on developers.tiktok.com) first.")
    return key, secret


def _save(tok):
    tok["expires_at"] = time.time() + tok.get("expires_in", 86400) - 120
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok))
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def login(mode="inbox", stats=False):
    """One-time sign-in: opens TikTok in the browser and catches the redirect on localhost. `stats` also asks for
    video.list, so the review can read your posted videos' views (needs the Display API product on the app)."""
    key, secret = _client()
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok desktop PKCE wants hex, not base64url
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "scope": SCOPES[mode] + ("," + STATS_SCOPE if stats else ""), "response_type": "code", "redirect_uri": REDIRECT_URI,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("TikTok sign-in received. You can close this tab.".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    print("Opening TikTok sign-in in your browser...")
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    if got.get("error") or got.get("state") != state:
        sys.exit(f"TikTok sign-in failed: {got.get('error_description') or got.get('error') or 'state mismatch'}")
    r = requests.post(f"{API}/oauth/token/", timeout=30, data={
        "client_key": key, "client_secret": secret, "code": got["code"], "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    tok = r.json()
    if "access_token" not in tok:
        sys.exit(f"TikTok token exchange failed: {tok}")
    _save(tok)
    granted = set(got.get("scopes", "").split(","))
    print("Signed in to TikTok." + ("" if "video.upload" in granted else " Warning: video.upload wasn't granted.")
          + ("" if not stats or STATS_SCOPE in granted else " Warning: video.list wasn't granted, so the review can't "
             "read TikTok views. Add the video.list scope to the app (or its Sandbox) on developers.tiktok.com, and use that "
             "app's client key and secret, then sign in again."))
    return tok


def token():
    if not TOKEN_FILE.exists():
        return None
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    key, secret = _client()
    r = requests.post(f"{API}/oauth/token/", timeout=30, data={
        "client_key": key, "client_secret": secret, "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"]}).json()
    if "access_token" not in r:
        raise RuntimeError(f"TikTok login expired, run `python channel_builder.py tiktok-auth` again ({r})")
    return _save(r)["access_token"]


def _post(path, access, body):
    r = requests.post(f"{API}{path}", timeout=60, json=body,
                      headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8"})
    data = r.json()
    if data.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok {path}: {data['error'].get('code')} {data['error'].get('message', '')}")
    return data.get("data", {})


def caption(clip):
    """Search-first caption (tiktok_caption.py): the creator's full name, what happens, the topic/guest/game, a
    question for the comments, and 3-5 specific hashtags. A writer's own tiktok_caption is kept when it's a real
    sentence that names the creator; otherwise it's rebuilt."""
    import tiktok_caption
    from sources import SOURCES
    src = SOURCES.get(clip.get("source")) or {"name": clip.get("creator", ""), "short": clip.get("creator", "")}
    copy, tags = tiktok_caption.build(clip, src)
    own = (clip.get("tiktok_caption") or "").strip()
    own = re.sub(r"(?:\s#\w+)+\s*$", "", own).strip()
    names = {src.get("name", "").lower(), src.get("short", "").lower()} - {""}
    import originality
    if originality.stock_phrase(own) or re.search(r"\bAt [A-Za-z]+ing\b", own):
        own = ""  # a stock or cut-off line: use the rebuilt caption
    if (len(own.split()) >= 7 and any(n in own.lower() for n in names) and not re.search(r"\b(?:in|a|the|of)\.$|reacts during a live stream", own)
            and src.get("name", "").lower() in own.lower()):
        copy = own
    return f"{copy} {' '.join(tags)}".strip()[:2200]


def upload(path, clip, mode="inbox"):
    """Returns TikTok's publish_id. Clips here are small (<64MB), so the file goes in one chunk."""
    access = token()
    if not access:
        raise RuntimeError("Not signed in to TikTok: run `python channel_builder.py tiktok-auth`.")
    size = Path(path).stat().st_size
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    if mode == "direct":
        info = _post("/post/publish/creator_info/query/", access, {})
        options = info.get("privacy_level_options", [])
        privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else "SELF_ONLY"
        data = _post("/post/publish/video/init/", access, {
            "post_info": {"title": caption(clip), "privacy_level": privacy, "disable_duet": False,
                          "disable_stitch": False, "disable_comment": False, "video_cover_timestamp_ms": 1000},
            "source_info": source})
    else:
        data = _post("/post/publish/inbox/video/init/", access, {"source_info": source})
    with open(path, "rb") as f:
        r = requests.put(data["upload_url"], data=f.read(), timeout=600, headers={
            "Content-Type": "video/mp4", "Content-Length": str(size), "Content-Range": f"bytes 0-{size - 1}/{size}"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"TikTok upload failed ({r.status_code}): {r.text[:200]}")
    return data["publish_id"]


def status(publish_id):
    """TikTok's processing state for an upload: SEND_TO_USER_INBOX / PUBLISH_COMPLETE mean it arrived,
    FAILED comes with a fail_reason."""
    access = token()
    if not access:
        raise RuntimeError("Not signed in to TikTok: run `python channel_builder.py tiktok-auth`.")
    return _post("/post/publish/status/fetch/", access, {"publish_id": publish_id})


def pending_inbox_drafts(builder_dir, exclude_queue=None, now=None, stop_at=MAX_PENDING_INBOX_DRAFTS):
    """Count recent, tracked inbox uploads that still need the creator to finish posting them.

    Queue folder timestamps are UTC. Unknown status is reported separately so the caller can pause safely instead
    of sending more drafts while it cannot verify the pending count.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    excluded = Path(exclude_queue).resolve() if exclude_queue else None
    pending, unknown, seen = 0, [], set()
    for qdir in sorted(Path(builder_dir).glob("*_20*"), key=lambda p: p.name, reverse=True):
        if excluded and qdir.resolve() == excluded:
            continue
        stamp = re.search(r"(\d{4}-\d{2}-\d{2}_\d{4})$", qdir.name)
        if not stamp:
            continue
        try:
            created = datetime.strptime(stamp.group(1), "%Y-%m-%d_%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff or created > now + timedelta(minutes=5):
            continue
        qfile = qdir / "queue.json"
        if not qfile.exists():
            continue
        try:
            queue = json.loads(qfile.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for clip in queue:
            publish_id = clip.get("tiktok_id")
            if not publish_id or publish_id in seen:
                continue
            seen.add(publish_id)
            try:
                state = status(publish_id).get("status")
            except Exception as exc:
                unknown.append(f"{publish_id}: {type(exc).__name__}")
                return pending, unknown
            if state not in ("PUBLISH_COMPLETE", "FAILED"):
                pending += 1
                if pending >= stop_at:
                    return pending, unknown
    return pending, unknown


def can_read_stats():
    """True when the saved login includes video.list (TikTok returns the granted scopes with the token)."""
    if not TOKEN_FILE.exists():
        return False
    return STATS_SCOPE in (json.loads(TOKEN_FILE.read_text(encoding="utf-8-sig")).get("scope") or "")


def my_videos(since=None, limit=200):
    """Your public TikTok posts, newest first, with view/like/comment/share counts (Display API video.list).
    Stops at posts older than `since` (a datetime). Drafts and private posts aren't returned."""
    access = token()
    if not access:
        raise RuntimeError("Not signed in to TikTok: run `python channel_builder.py tiktok-auth --stats`.")
    fields = "id,create_time,title,video_description,duration,view_count,like_count,comment_count,share_count"
    out, body = [], {"max_count": 20}
    while len(out) < limit:
        r = requests.post(f"{API}/video/list/?fields={fields}", timeout=30, json=body,
                          headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
        data = r.json()
        if data.get("error", {}).get("code", "ok") != "ok":
            raise RuntimeError(f"TikTok /video/list/: {data['error'].get('code')} {data['error'].get('message', '')}")
        page = (data.get("data") or {})
        vids = page.get("videos") or []
        out += vids
        if not page.get("has_more") or not vids:
            break
        if since and datetime.fromtimestamp(vids[-1].get("create_time", 0), timezone.utc) < since:
            break
        body = {"max_count": 20, "cursor": page["cursor"]}
    return [v for v in out if not since or datetime.fromtimestamp(v.get("create_time", 0), timezone.utc) >= since]
