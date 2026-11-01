"""Copies this PC's keys and sign-ins into the GitHub repo's secrets, so the daily run works in the cloud.

Run it once on the PC after you've signed in here (`channel_builder.py auth` and `tiktok-auth`), and again
any time you sign in again. Values go straight from this PC to GitHub and are never printed.

Needs the GitHub CLI, signed in:  winget install GitHub.cli   then   gh auth login
Usage:  python cloud_setup.py            (repo defaults to <your GitHub user>/late-recaps-bot)
        Twitch and Meta (Instagram/Facebook) keys are read from the environment variables of the same names.
"""

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOKENS = Path.home() / ".config" / "clip-builder"


def gh(*args, value=None):
    return subprocess.run(["gh", *args], input=value, text=True, capture_output=True)


def main():
    if not shutil.which("gh"):
        sys.exit("Install the GitHub CLI first: winget install GitHub.cli   then: gh auth login")
    if gh("auth", "status").returncode:
        sys.exit("Sign in to GitHub first: gh auth login")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repo = args[0] if args else f"{gh('api', 'user', '--jq', '.login').stdout.strip()}/late-recaps-bot"
    values = {
        "YOUTUBE_API_KEY": os.environ.get("YOUTUBE_API_KEY"),
        "TIKTOK_CLIENT_KEY": os.environ.get("TIKTOK_CLIENT_KEY"),
        "TIKTOK_CLIENT_SECRET": os.environ.get("TIKTOK_CLIENT_SECRET"),
        "TWITCH_CLIENT_ID": os.environ.get("TWITCH_CLIENT_ID"),
        "TWITCH_CLIENT_SECRET": os.environ.get("TWITCH_CLIENT_SECRET"),
        "META_ACCESS_TOKEN": os.environ.get("META_ACCESS_TOKEN"),
        "IG_USER_ID": os.environ.get("IG_USER_ID"),
        "FB_PAGE_ID": os.environ.get("FB_PAGE_ID"),
        "YOUTUBE_TOKEN": (TOKENS / "youtube_token.json").read_text() if (TOKENS / "youtube_token.json").exists() else None,
        "REVIEW_TOKEN": (TOKENS / "review_token.json").read_text() if (TOKENS / "review_token.json").exists() else None,
        "TIKTOK_TOKEN": (TOKENS / "tiktok_token.json").read_text() if (TOKENS / "tiktok_token.json").exists() else None,
    }
    if "--with-claude" in sys.argv:  # not needed: the cloud run writes scripts with a free local model
        print("For the script writer: run `claude setup-token` in another window, then paste the token here.")
        values["CLAUDE_CODE_OAUTH_TOKEN"] = getpass.getpass("Claude token (hidden, Enter to skip): ").strip() or None
    for name, value in values.items():
        if not value:
            print(f"  {name}: not found on this PC, skipped")
            continue
        r = gh("secret", "set", name, "--repo", repo, value=value)
        print(f"  {name}: " + ("saved" if r.returncode == 0 else f"failed ({r.stderr.strip()[:120]})"))
    print(f"Done. Start a test run at https://github.com/{repo}/actions (Late Recaps daily -> Run workflow).")


if __name__ == "__main__":
    main()
