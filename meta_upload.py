"""Posts finished clips as Instagram Reels and Facebook Page Reels through Meta's official Graph API. Free.

Off until these are set (GitHub secrets for the cloud run, or environment variables on a PC):
- META_ACCESS_TOKEN: a long-lived Page access token (it doesn't expire) with instagram_basic,
                     instagram_content_publish, pages_show_list, pages_read_engagement and pages_manage_posts.
- IG_USER_ID:        the Instagram professional (Business or Creator) account linked to that Page. Optional.
- FB_PAGE_ID:        the Facebook Page to post Reels on. Optional.
Setup steps are in the README (Instagram and Facebook).

Both use resumable uploads straight from the file, so nothing needs to be hosted anywhere.
"""

import os
import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v21.0"


def enabled():
    return bool(os.environ.get("META_ACCESS_TOKEN") and (os.environ.get("IG_USER_ID") or os.environ.get("FB_PAGE_ID")))


def _token():
    return os.environ["META_ACCESS_TOKEN"]


def _check(r):
    try:
        d = r.json()
    except ValueError:
        r.raise_for_status()
        return {}
    if r.status_code >= 400 or "error" in d:
        raise RuntimeError(str(d.get("error", d))[:300])
    return d


def caption(clip):
    tags = list(dict.fromkeys((clip.get("hashtags") or []) + ["#reels", "#clips"]))[:5]
    return f"{clip['title']}\n\n{clip.get('summary') or clip.get('context', '')}\n\nCredit: {clip['creator']}\n" + " ".join(tags)


def instagram(path, clip, wait_s=600):
    """Upload as a Reel, wait for Instagram to process it, publish. Returns the media id."""
    ig = os.environ["IG_USER_ID"]
    size = Path(path).stat().st_size
    c = _check(requests.post(f"{GRAPH}/{ig}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption(clip)[:2200], "share_to_feed": "true",
        "access_token": _token()}))
    with open(path, "rb") as f:
        _check(requests.post(c["uri"], timeout=600, data=f, headers={
            "Authorization": f"OAuth {_token()}", "offset": "0", "file_size": str(size)}))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        st = _check(requests.get(f"{GRAPH}/{c['id']}", timeout=30, params={"fields": "status_code,status",
                                                                            "access_token": _token()}))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram is still processing the video; it'll be retried next run")
    return _check(requests.post(f"{GRAPH}/{ig}/media_publish", timeout=60, data={
        "creation_id": c["id"], "access_token": _token()}))["id"]


def facebook(path, clip):
    """Upload as a Facebook Page Reel and publish it. Returns the video id."""
    page = os.environ["FB_PAGE_ID"]
    size = Path(path).stat().st_size
    s = _check(requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "start", "access_token": _token()}))
    with open(path, "rb") as f:
        _check(requests.post(s["upload_url"], timeout=600, data=f, headers={
            "Authorization": f"OAuth {_token()}", "offset": "0", "file_size": str(size)}))
    _check(requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "finish", "video_id": s["video_id"], "video_state": "PUBLISHED",
        "description": caption(clip)[:2200], "access_token": _token()}))
    return s["video_id"]


def post(path, clip):
    """Post to whichever of Instagram/Facebook is set up and not done yet. Returns {platform: id or error}."""
    out = {}
    for key, env, fn in [("instagram_id", "IG_USER_ID", instagram), ("facebook_id", "FB_PAGE_ID", facebook)]:
        if not os.environ.get(env) or clip.get(key):
            continue
        try:
            out[key] = fn(path, clip)
        except Exception as e:  # one platform's trouble never blocks the others
            out[key + "_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out
