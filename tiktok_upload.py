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
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

API = "https://open.tiktokapis.com/v2"
REDIRECT_PORT = 8723
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
SCOPES = {"inbox": "user.info.basic,video.upload", "direct": "user.info.basic,video.upload,video.publish"}
TOKEN_FILE = Path.home() / ".config" / "clip-builder" / "tiktok_token.json"


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


def login(mode="inbox"):
    """One-time sign-in: opens TikTok in the browser and catches the redirect on localhost."""
    key, secret = _client()
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok desktop PKCE wants hex, not base64url
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "scope": SCOPES[mode], "response_type": "code", "redirect_uri": REDIRECT_URI,
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
    print("Signed in to TikTok." + ("" if "video.upload" in granted else " Warning: video.upload wasn't granted."))
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
    """TikTok ranks and searches on the caption's words, so it says who and what in plain words (the first line of
    the description) and uses topic hashtags, not #fyp/#viral, which tell TikTok nothing about the clip."""
    skip = {"#shorts", "#fyp", "#foryou", "#foryoupage", "#viral"}
    tags = [t for t in clip.get("hashtags", []) if t.startswith("#") and t.lower() not in skip]
    if not tags:
        tags = [w for w in clip.get("description", "").split() if w.startswith("#") and w.lower() not in skip]
    where = {"twitch": "#twitchclips", "twitch_clip": "#twitchclips", "kick_clip": "#kickclips"}.get(clip.get("platform"))
    tags = list(dict.fromkeys(tags + [t for t in (where, "#streamer", "#streamerclips") if t]))[:5]
    about = (clip.get("description") or "").split("\n", 1)[0].strip()
    about = "" if about.lower() in clip["title"].lower() else about[:150]
    return " ".join(p for p in (clip["title"], about, " ".join(tags)) if p).strip()[:2200]


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
