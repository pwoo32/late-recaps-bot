# Clip trend agent

Scans recent YouTube Shorts-length clips across 12 clip niches and ranks which niche is the best
money opportunity right now, plus the specific topics driving views inside each niche.

## Setup (once)

1. Get a free YouTube Data API key: https://console.cloud.google.com/apis/library/youtube.googleapis.com
   → Enable → Credentials → Create credentials → API key.
2. `pip install -r requirements.txt`

## Run

```
export YOUTUBE_API_KEY=your_key
python agent.py                          # all niches, last 7 days, US
python agent.py --days 2                 # only very fresh clips
python agent.py --niches streamer gaming # just some niches
```

The report lands in `reports/<date>/report.md`. Run it daily: each report shows how each niche's
view velocity changed since the previous run, which is how you spot a trend while it's rising.

## What it measures

- **Demand**: views per hour of the 25 fastest clips in the niche.
- **Money**: median views per clip x payout (Shorts ad RPM + clipping-campaign CPM, set in `niches.py`).
- **Breakout**: how often channels under 100k subs hit 100k+ views, and how spread out views are across channels.
  This tells you whether a new channel can actually win there.
- **Safety**: how exposed plain clips in the niche are to YouTube's reused-content demonetization.

A full run uses about 3,600 of the 10,000 free daily quota units.

To tweak scoring weights or money numbers without spending quota, rebuild a saved report:
`python agent.py --rerender reports/<run folder>`

By default only English videos are kept (`--lang any` to keep all languages).

## Channel study agent

Once you know the niche, `channel_agent.py` finds the channels earning the most in it and studies their playbook:
format and length, posting cadence and times, title patterns of their breakout videos vs their flops, the
creators/topics they clip, how they monetize, and a step-by-step plan to replicate it.

```
python channel_agent.py                    # niche = #1 from the latest trend report
python channel_agent.py --niche streamer   # or pick one
python channel_agent.py --niche streamer --from-run channels_streamer_<date>   # re-study the same channels, no searches
```

Report lands in `reports/channels_<niche>_<date>/report.md`. About 420 quota units a run (120 with `--from-run`).
It studies what works; it never downloads or reuploads anyone's videos (that gets copyright strikes and demonetized).

## Script agent

`script_agent.py` writes everything that goes out with each clip: a real title in the breakout pattern
("Speed Crashes Out After Cole Palmer Scores Again 😭"), the on-screen hook, the 3-second context line, the narrator
line, the description and the YouTube tags. It pulls from every other agent: the trend report (which clips of that
creator are blowing up this week), the channel study (breakout titles, title length, hashtags), the builder's queue
(stream title, what viewers said at that timestamp), `track` (which of your own titles beat your median), and the clip
itself (YouTube captions, then the whisper transcript once it's cut).

You don't need to run it: `channel_builder.py daily` writes the scripts when it builds the queue, and `cut`/`publish`
re-write each clip from its real transcript right before rendering, so the hook and narrator match what's said.
`cut --no-script` keeps a script you edited by hand. Run it yourself to redo a queue: `python script_agent.py`
(or `--ids 2`, `--dry-run`). Everything lands in the queue's `queue.json` plus a readable `scripts.md`.

Writers, tried in order (pick one with `--writer` or `SCRIPT_WRITER`): `claude-code` (Claude Code installed on this
PC, uses your Claude plan, no API key), `ollama` (free local model), `api` (`ANTHROPIC_API_KEY`). No writer at all?
`--prompt-only` saves a prompt to paste into claude.ai and `--import answer.json` loads its reply. No YouTube quota used.

## Channel builder

`channel_builder.py` takes both reports and runs the channel: what to clip today, how to cut it, what to call it, when
to post it, and it learns from your own uploads.

```
pip install -r requirements.txt          # plus ffmpeg on your machine
python channel_builder.py plan           # channel kit: niche, free @handle ideas, format, title rules, who to clip, schedule, money
python channel_builder.py daily          # today's queue: exact stream moments + cut points, titles, hooks, descriptions, post times
python channel_builder.py cut 1          # queue item 1 -> 1080x1920 Short: captions, hook, AI narrator over the opening
python channel_builder.py track --channel @yourhandle   # score your uploads; re-weights creators and post hours for tomorrow
python channel_builder.py publish        # cut + upload every queued clip, scheduled for its post time
python channel_builder.py run --publish  # the whole day hands-off: refresh reports, track, queue, cut, upload
```

- Moments come from the comments on each creator's recent full streams/videos: timestamps viewers leave and like
  ("1:33:18 please Cole enough") mark what people want to rewatch. Ranked by likes, how hot the creator is in the trend
  report, stream freshness, whether a paid clipping program covers them, and how that creator did on your channel.
- Creators and their clipping permissions live in `sources.py`. Mark one `blocked` to never pick or download them.
- Scripts come from `script_agent.py` (above). Optional keys: `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` (dev.twitch.tv, free; adds Twitch-only creators like
  Kai Cenat via their most-viewed Twitch clips).
- `cut` downloads only the needed seconds with yt-dlp (or `--video` a file you already have) and captions with faster-whisper.
  It edits for retention: a tall 3:4 frame that follows the streamer's face (OpenCV), jump cuts over dead air,
  a punch-in/out zoom every 1.5-2.5s, and big captions with the spoken word highlighted. `--plain` turns off the
  cuts and zooms; `--layout crop43` gives the old wide crop.
- No narrator by default: 33 of the 36 top streamer-clip Shorts we audited open straight on the stream's audio.
  `--narrate` adds one, a free Microsoft neural voice via edge-tts (`--voice en-US-BrianMultilingualNeural`, about
  170 words a minute; list others with `edge-tts --list-voices`). Falls back to macOS `say` or espeak-ng. Pass
  `--voiceover file.m4a` to use your own.
- Quota: plan ~25 units, daily ~20-60, track ~2. Outputs: `reports/builder/`.

### Uploading (one-time setup, ~10 minutes)

1. In the same Google Cloud project as your API key: APIs & Services → OAuth consent screen → External, add your
   Google account as a test user. Then Credentials → Create credentials → OAuth client ID → Desktop app → download
   the JSON and save it as `client_secret.json` in this folder (or point `YOUTUBE_CLIENT_SECRETS` at it).
2. Run `python channel_builder.py auth`. A browser opens once: sign in with the account that owns the channel and
   allow access. The login is saved in `~/.config/clip-builder/` and refreshed automatically after that.
3. **Unaudited projects upload as private.** YouTube locks videos uploaded through an API project that hasn't passed
   its compliance audit to private. Apply once (free, usually 1-3 weeks):
   https://support.google.com/youtube/contact/yt_api_form . Until it's approved, clips upload privately and you flip
   them to public in YouTube Studio. After approval they go public on schedule with no clicks.
4. Run it every morning: `crontab -e` and add
   `0 13 * * * cd /path/to/clip-trend-agent && YOUTUBE_API_KEY=... python channel_builder.py run --publish >> builder.log 2>&1`

Each clip uploads `private` with `publishAt` set to its post time (public immediately if that time has passed).
Uploads use YouTube's separate upload quota, not the 10,000 search/read units.

## Review agent

`review_agent.py` checks how every Short on your channel is doing after it's posted and tells you how to get more views
and money out of it. Per clip: views and views/hour, average % watched, how many plays stayed past the first swipe,
the retention curve at 3 seconds and at the end, Shorts-feed traffic, likes, shares and subscribers gained, next to what
was posted (creator, stream moment, title, hook, narrator line, length, post time, transcript). It flags what hurt each
clip (hook, length, title/topic, clip pick, still private) and writes a better title and hook for it, then tracks money:
YPP progress and ETA at your current pace, what the views are worth, which clips fit a paid clipping campaign, and
affiliate/sponsor/long-form ideas.

```
python review_agent.py auth       # once: sign in so it can read YouTube Analytics
python review_agent.py            # review every Short (report in reports/review_<date>/report.md)
python review_agent.py --days 14 --no-llm
```

What it learns goes into `reports/builder/state.json` and the other agents use it: `script_agent.py` puts the
title/hook/pacing lessons and how your recent clips held viewers in its prompt, and `channel_builder.py daily` weights
creators by how their clips held viewers (`track` already weights them by views) and cuts to the length that held best.
It also fills in your channel for the builder, so `run` starts its daily `track`. It never uploads, edits, retitles or
deletes a video: suggested titles are for you to apply in Studio. Uses 0-3 Data API units; Analytics has its own quota.
The written review uses the same writer as the script agent (Claude Code, Ollama or ANTHROPIC_API_KEY).

### Analytics sign-in (one time, ~3 minutes)

1. In the same Google Cloud project as your OAuth client, enable the **YouTube Analytics API**:
   https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com
2. Run `python review_agent.py auth`. A browser opens: sign in with the account that owns the channel, click
   Continue past "Google hasn't verified this app" (it's your own app), and allow read access to YouTube and
   YouTube Analytics. The login is saved as `~/.config/clip-builder/review_token.json`, separate from the uploader's,
   so publishing is never affected.
3. **Stop logins expiring every 7 days.** While the OAuth consent screen is in "Testing", Google expires its logins
   after a week (the uploader's too). Google Cloud Console > OAuth consent screen (Google Auth Platform > Audience) >
   Publish app. You don't need Google's verification for your own account; run `channel_builder.py auth` and
   `review_agent.py auth` once more afterwards.

Without the sign-in it still runs: it uses the uploader's login (sees private clips, no retention) or your API key
(public clips only). Analytics lag a day or two, so judge a clip after about 48 hours.

### TikTok (optional cross-posting)

Every clip that goes to YouTube can also go to TikTok through TikTok's official Content Posting API. It's off until
you turn it on.

1. Create an app at https://developers.tiktok.com (Manage apps → Connect an app). Add the **Login Kit** and
   **Content Posting API** products, with the scopes `user.info.basic`, `video.upload` and `video.publish`. Under
   Login Kit → Desktop, add the redirect URI `http://localhost:8723/callback/`. Use the app's Sandbox (with your
   TikTok account added under Target Users) to test before review; review needs a demo video of it working.
2. Put the app's Client key and Client secret in your user environment variables, as `TIKTOK_CLIENT_KEY` and
   `TIKTOK_CLIENT_SECRET`. Don't put them in any file.
3. Sign in once and turn cross-posting on: `python channel_builder.py tiktok-auth --enable`
   (`--mode direct` to post straight to the profile, `--disable` to turn it off).

Modes:
- `inbox` (default): each clip lands in your TikTok inbox as a draft. Tap the notification, paste the caption
  (`publish` prints it and saves it in queue.json as `tiktok_caption`), and post. This works before TikTok's audit.
  TikTok allows 5 pending drafts per 24 hours.
- `direct`: posts on its own, with the caption. Until TikTok audits the app, posts are private-only, and the
  TikTok account itself must be private. Apply for the audit from the app's page, then switch modes.
