# AGENTS.md: how to run and change the Late Recaps bot

Instructions for any coding agent (OpenAI Codex, Claude Code, others) working in this repo. Read this first, then
`README.md` for the long version of each part.

## What this is

A hands-off YouTube Shorts / TikTok / Instagram / Facebook clip channel ("Late Recaps") for streamer moments.
Every day it finds a moment on a creator's stream, writes the channel's own commentary for it, cuts a 1080x1920
Short with captions and an AI narrator, and posts it. It runs on GitHub Actions on its own schedule. No Claude,
Codex or any paid service is needed for the daily run.

## The one rule that matters for views

YouTube's Oct 1 2026 Shorts update cuts feed reach for channels that mostly re-upload other creators' footage. It
names template edits and voice-over that only describes the screen as adding nothing. Every Short must carry a real
take of our own: context the viewer lacks plus an opinion, a joke or a prediction. The publish gate
(`originality.py` via `channel_builder.clip_problem`) holds clips that don't. **Do not weaken it to get clips
posted.** If clips are being held, fix the writer (usually a missing or failing `GEMINI_API_KEY`) or the clip pick.

## Layout

| File | What it does |
|---|---|
| `channel_builder.py` | Main CLI: `daily` (queue), `cut`, `publish`, `run`, `spike`, `track`, `auth`, `tiktok-auth`, `tiktok-status` |
| `live_moments.py` | Finds moments: chat spikes, most-replayed, Twitch and Kick clips, comment timestamps |
| `council.py` | Every agent scores candidate moments; a model or the weighted vote picks the day's clip |
| `script_agent.py` | Writes title, hook, narrator take, payoff, description, tags. Writers: gemini, groq, ollama, free |
| `free_writer.py` | No-model fallback writer. Its narration is template-based, so the gate holds those clips for YouTube |
| `originality.py` | Reused-content and copyright checks (template narration, stock lines, music, short clips, repeats) |
| `tuner.py` | A/B tests edit settings (length, zoom, captions) and keeps the winners |
| `review_agent.py` | Finds what makes our clips spread (feed tests, creators, hooks, titles, length, timing) from YouTube Analytics and `reports/tiktok_stats.csv`, and feeds creator/hour/length weights and make-more/less findings into `reports/builder/state.json` |
| `tiktok_upload.py`, `tiktok_caption.py`, `meta_upload.py` | Cross-posting |
| `sources.py` | Creators we clip, where their footage lives, and `crowd` (saturation penalty) |
| `agent.py`, `channel_agent.py`, `niches.py` | Trend scan and channel study reports |
| `channel_activity_agent.py` | Comment replies, first comments and community post drafts; runs in the cloud daily run after the review; drafts only until the repo variable `ACTIVITY_POST` is `true` |
| `cloud_setup.py` | Copies the PC's keys and sign-in tokens into the GitHub repo secrets |
| `reports/` | Every run's output, committed by the workflow. `reports/builder/state.json` is the bot's memory |
| `.github/workflows/daily.yml` | The schedule: daily run ~7 AM ET with backups through the day |
| `.github/workflows/retry-interrupted.yml` | Re-runs a daily run killed by a runner shutdown |

## Running it

The schedule in `daily.yml` runs everything. To run by hand: GitHub > Actions > "Late Recaps daily" > Run workflow
(untick "Post the clips" for a dry run that builds and cuts but uploads nothing). From a terminal with the GitHub CLI:

```
gh workflow run daily.yml -f mode=daily -f post=false   # dry run
gh run list --workflow daily.yml --limit 5
gh run view <id> --log | grep -E "Scripts written by|Posting held|uploaded https"
```

Locally (Python 3.12, ffmpeg on PATH):

```
pip install -r requirements.txt
python channel_builder.py daily --niche streamer --count 1       # build today's queue
python channel_builder.py cut 1                                   # render item 1 (reports/builder/daily_*/clips/)
python channel_builder.py publish                                 # cut and upload the queue
python review_agent.py --days 14                                  # review posted clips
python script_agent.py --writer gemini --dry-run                  # re-write the latest queue's scripts, print only
```

On Windows, `run_daily.cmd` and `run_activity.cmd` are for Task Scheduler (both off now: everything runs in the cloud). They read keys from user environment
variables. The cloud workflow is the main path, so don't run both or the channel posts twice.

## Channel

Everything posts to the **Late Recaps** YouTube channel (`UCbK9EejOk5UVzI_l8F3f8kA`) and its linked TikTok,
Instagram and Facebook accounts. Never create, switch to or upload to another channel. `publish` checks the
signed-in channel before any upload and holds the run if it isn't Late Recaps (`CHANNEL_ID` in `channel_builder.py`).

## Secrets (GitHub > Settings > Secrets and variables > Actions)

| Secret | Needed for |
|---|---|
| `YOUTUBE_API_KEY` | Search and read quota (trend scan, moments) |
| `YOUTUBE_TOKEN` | Uploader OAuth token (`channel_builder.py auth` on the PC, then `cloud_setup.py`) |
| `REVIEW_TOKEN` | Analytics OAuth token (`review_agent.py auth`, then `cloud_setup.py`) |
| `GEMINI_API_KEY` | Script and review writer (free, aistudio.google.com/apikey). Without it most clips are held |
| `GROQ_API_KEY` | Optional backup writer (free, console.groq.com/keys) |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Twitch clips with exact VOD offsets |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_TOKEN` | TikTok posting |
| `META_ACCESS_TOKEN`, `IG_USER_ID`, `FB_PAGE_ID` | Instagram and Facebook Reels |
| `ACTIVITY_TOKEN` | Comment access for replies and first comments (`channel_activity_agent.py auth`, then `cloud_setup.py`) |
| `YT_COOKIES` or `YT_PROXY` | Lets yt-dlp download from YouTube on cloud runners |

Never print, log or commit a secret or token. OAuth tokens live in `~/.config/clip-builder/` on the PC.

## Troubleshooting

- **"Posting held" in the log**: read `reports/builder/<queue>/HOLD.md`. "fill-in writer" or "template narration"
  means no model wrote the script: check `GEMINI_API_KEY` and look for "Writer gemini failed" in the run log.
- **Nothing posted and no hold**: check the run log for yt-dlp download errors (cookies expired: refresh
  `YT_COOKIES`) or an out-of-memory kill (`[mem]` lines).
- **"invalid_grant" or "Token has been expired or revoked"**: the Google sign-in expired. On the PC run
  `python channel_builder.py auth` and `python review_agent.py auth`, then `python cloud_setup.py`. Publishing the
  OAuth app (Google Cloud Console > Google Auth Platform > Audience > Publish app) stops the 7-day expiry.
- **Review says "can't read YouTube Analytics"**: `REVIEW_TOKEN` is missing or expired (see above).
- **TikTok numbers**: the review reads TikTok views through the Display API (`video.list`) when the TikTok login has
  it (`channel_builder.py tiktok-auth --stats`, then `cloud_setup.py`), matching posts to our drafts by caption, and
  writes them to `reports/tiktok_stats.csv`. Without it, Philip types the numbers into that sheet.
- **Copyright claim**: the review report names the clip. Its stream is added to `claimed_streams` in `state.json`
  and is never clipped again.

## Making changes

- Work on a branch and open a pull request. The workflow commits run reports straight to `main`, so pull first.
- Check syntax before pushing: `python -m py_compile *.py`.
- Test a change with a dry run of the workflow on your branch (`gh workflow run daily.yml --ref <branch> -f post=false`).
- Keep `README.md` in step with any change to commands, secrets or behaviour, and update this file too.
- Add or drop creators in `sources.py`. Use `"permission": "blocked"` to stop clipping someone.
