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
("Speed Crashes Out After Cole Palmer Scores Again 😭"), the on-screen hook, the 3-second context line, a clip-specific
narrator take, YouTube description/tags, and a TikTok caption with searchable context. It pulls from every other agent: the trend report (which clips of that
creator are blowing up this week), the channel study (breakout titles, title length, hashtags), the builder's queue
(stream title, what viewers said at that timestamp), `track` (which of your own titles beat your median), and the clip
itself (YouTube captions, then the whisper transcript once it's cut).

You don't need to run it: `channel_builder.py daily` writes the scripts when it builds the queue, and `cut`/`publish`
re-write each clip from its real transcript right before rendering, so the hook and narrator match what's said.
`cut --no-script` keeps a script you edited by hand. Run it yourself to redo a queue: `python script_agent.py`
(or `--ids 2`, `--dry-run`). Everything lands in the queue's `queue.json` plus a readable `scripts.md`.

Writers. Only free ones run unless you ask: `ollama` (a free local model; the cloud run installs Ollama and
`qwen2.5:7b`, change it with the repo variable `OLLAMA_MODEL`), then `free` (`free_writer.py`: rules and templates built
from the transcript, viewer comments, chat and the trend scan, no model at all, never fails). `claude-code` (your
Claude plan) and `api` (`ANTHROPIC_API_KEY`) only run if you pick them with `--writer` or `SCRIPT_WRITER`.
`--prompt-only` saves a prompt to paste into claude.ai and `--import answer.json` loads its reply. No YouTube quota used.

## Hands-off in the cloud, no Claude

`.github/workflows/daily.yml` runs everything on GitHub Actions (free for this public repo) without using Claude or
anything paid:

- **Daily run (~7 AM ET, retried at ~1:47 PM ET if nothing posted):** the review agent scores yesterday's clips and
  feeds the edit tuner, the trend scan refreshes (who's spiking, what's viral), the builder finds today's moments, a
  free local model writes the scripts, and each clip is cut and posted to YouTube, TikTok, Instagram and Facebook.
- **Spike checks (every 3 hours):** if something on a creator's stream is going nuts right now (chat flooding, a
  Twitch clip racing up), it's clipped and posted straight away. At most 2 a day (`spike --max-per-day`), because
  YouTube's API quota caps uploads.
- Run either by hand from the Actions tab (Run workflow). Untick "Post the clips" for a test that cuts but doesn't
  upload.

Where moments come from (`live_moments.py`, all free, no YouTube quota):
- comment timestamps on the creator's recent streams and videos (as before)
- **chat spikes** on their newest stream VODs: the live chat replay, where chat suddenly floods with LMAO/NO WAY/💀
- **most replayed**: YouTube's replay graph, which boosts moments in the hottest part of a stream
- **Twitch clips**, including ones from a stream that's still live (cut straight from the clip), weighted by how fast
  they're gaining views. Needs the free `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` secrets (dev.twitch.tv → Register
  your application, any redirect URL, Confidential).
- the daily trend scan boosts creators whose clip demand jumped since the last scan (and allows them 3 clips that
  day) and moments that match words in their fastest-growing clips this week.

**Trending streamers** (`trending.py`): besides the fixed list in `sources.py`, each daily run looks for whoever is
going off right now: the day's fastest-gaining Twitch clips in Just Chatting and the top live categories (any English
streamer; needs the Twitch secrets) and the top clips of a watchlist of big Kick channels. Streamers are scored by
how fast their best clip is gaining views, times a bonus when few channels in the trend scan clip them yet (x1.3 for
nobody, down to x0.7 for a crowded name). Up to 6 a day are added to `reports/builder/trending_sources.json`, which
`sources.py` loads, and they're scanned while they keep trending (3 days). Mark one `blocked` in `sources.py` to keep
them out. Clip speed now counts for up to 4x in ranking (60k+ views/hour), so the moment spreading fastest wins.

**Clip council** (`council.py`): before anything is picked, every agent weighs in on the top ~16 candidate moments
and scores each 0-1 with a one-line reason: the trend researcher (is the creator hot and rising, does it match what's
viral), the channel study (do top channels break out with this creator), the analytics reviewer (how this creator does
on our channel, does the moment have a spoken premise, is it fresh), the live scout (chat flood, most replayed, Twitch
clip speed, timestamp comments), the script writer (can it write a strong title from what's said) and the audience
(our own comments asking for a creator). A free local model chairs the meeting, reads every agent's notes and picks
the day's clips with a reason; without it the weighted vote decides. The whole meeting is saved as `council.md` in the
day's queue folder. Each agent's weight learns: when a clip settles, agents that rated it in line with how it did
gain weight and the others lose some (shown at the bottom of the review report).

Before any platform posts a clip, the publish gate requires a timestamped meeting from the last 24 hours with a
completed vote and reason from every council agent. Spike queues also invite the local model chair. Queues without
complete votes are held and must be rebuilt through `daily` or `spike` before publishing.

**Edit tuner** (`tuner.py`): the new format (face-tracked frame, jump cuts, zoom punches, word-highlight captions, no
narrator setup and payoff lines) stays the default. Each day the best clip gets the best settings found so far and the others each test one
change: length (20/30/45s), zoom strength and pace, 2- or 3-word captions, how much silence gets cut, face-tracked vs
wide framing. After 48 hours the review agent scores every clip (views vs your median plus retention) and credits its
settings. A setting takes over once it beats the default on 3+ clips. The review report shows the scores.

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
- The narrator is always on (`--no-narrate` turns it off, and the publish gate then holds the clip): a setup line over
  the opening and a payoff line (our verdict or a question for the comments) over a held last frame after the moment.
  That voice is what makes a stream cut our own clip instead of a reupload. Pass `--voiceover file.m4a` to use your own
  opening take.
- Originality and copyright gate (`originality.py`, Nov 2026, after 47 Shorts sat at a median of 1 view and one got a
  Content ID claim). Moments are never cut from a creator's edited main-channel videos (only stream VODs and
  Twitch/Kick clips), music moments rank last, a stream that earned a claim is never clipped again, and at most 2
  clips ever come from one stream. Before upload a clip is held if its title, hook or narrator line is a stock line
  that could sit on any clip ("Goes Crazy", "Wait For It"...), its title repeats an earlier upload, music is heard
  or named, under a quarter of it is speech, or the narrator voice failed to render.
- Commentary gate (Nov 12 2026). YouTube's Oct 1 2026 Shorts update cuts feed reach for channels that mostly
  re-upload other creators' footage, and names template-based edits and voice-over that only describes the screen as
  adding nothing. Our narration was fill-in-the-blank ("This is about X; Speed's reaction is what makes the moment
  land."), so the channel read as an aggregator. Now a clip is held for YouTube unless its narrator take is a real
  15-35 word opinion or context line from a model writer (free: `GEMINI_API_KEY`, or `GROQ_API_KEY`), not the
  free writer's templates; the clip has at least 20 spoken words; and whole Twitch/Kick clips are 15s or longer.
  Saturated creators (Speed, Kai Cenat) rank at half weight (`crowd` in `sources.py`). `ALLOW_TEMPLATE_NARRATION=1`
  turns the template check off.
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

`review_agent.py` works out what makes our clips spread and steers the next clips toward it. Each report
(`reports/review_<date>/report.md`) answers, in this order:

- **Is the Shorts feed testing our clips?** Per week, how many clips passed 10 views in their first 2 days. A clip the
  feed never shows can't go viral whatever its hook, so this is the first number to move.
- **Make more of / make less of.** Every settled clip is tagged by the levers we control: creator, how the moment
  was found (Twitch clip, chat spike, viewer timestamps, most-replayed), how old the stream was when we posted,
  hook style (stake, question, says what happens, generic "WAIT FOR IT"), title style (quote title, stock reaction,
  says what happens), length and posting time. Each lever value is compared with our typical clip (geometric mean of
  views, so a pile of 0s and one 45 doesn't read as a 45x hit), shrunk toward "no effect" when it rests on few clips.
  Only values with 3+ clips and a clear gap are called.
- **First seconds.** How many viewers stayed past the swipe, the second where most of them leave, and swipe rate by
  hook style.
- **Clips worth a look**: best tested clips, tested clips that lost viewers at the start, and clips still under 2
  days, each with what to change (and, when a written review runs, a better title and hook).
- **TikTok** (see below) and **what changes tomorrow**: the weights the builder will use.

Money/YPP tracking, subscriber counts and the full every-clip table were dropped: they didn't change what we post.

```
python review_agent.py auth       # once: sign in so it can read YouTube Analytics
python review_agent.py            # review every Short (report in reports/review_<date>/report.md)
python review_agent.py --days 14 --no-llm
```

What it learns goes into `reports/builder/state.json` (`review`) and the other agents use it:
- `channel_builder.py daily` ranks moments by the creator's score (this replaces `track`'s views-only weight when the
  review has one), by how moments found the same way did and by the stream's age, posts at the hours whose clips
  spread best, and cuts to the length that spread best.
- the clip council's analytics vote uses the same creator and "found by" scores.
- `script_agent.py` puts the make-more / make-less findings, the lessons and the best and worst recent clips in its
  prompt.

It also fills in your channel for the builder, so `run` starts its daily `track`. It never uploads, edits, retitles
or deletes a video: suggested titles are for you to apply in Studio. Uses 0-3 Data API units; Analytics has its own
quota. The daily cloud run uses the rule-based review (`--no-llm`); run it locally with a writer for the written part.

### TikTok numbers

The review reads your TikTok views itself once the TikTok login includes the `video.list` scope (TikTok's Display
API). Our TikTok posts are drafts you finish in the app, so it matches each public post to its draft by the caption
you pasted and by being posted after the draft was sent. One-time setup:

1. On developers.tiktok.com, open the app you already use for posting and add the `video.list` scope (under Login
   Kit). If the app is live, a new scope may send it back through TikTok's review; until then the app's Sandbox works
   for your own account (add it under Target Users). A Sandbox has its own client key and secret: the sign-in must use
   the keys of whichever one has `video.list`, with the redirect URI `http://localhost:8723/callback/` set there too.
2. On the PC: `python channel_builder.py tiktok-auth --stats`. TikTok asks you to approve the extra permission.
3. `python cloud_setup.py`, so the cloud run gets the new login.

Every clip sent to TikTok gets a row in `reports/tiktok_stats.csv`, filled from TikTok (`source` = "TikTok API").
Rows stay blank for drafts never posted, or when the caption was changed too much to match: type views (and
`shares`, `watched_full_pct`) from TikTok Studio there and commit it, outside the daily run (around 7 AM ET).
TikTok's API doesn't give watch time or completion rate, so those columns only fill in if you type them. The review
runs the same make-more / make-less analysis on TikTok and passes it to the script writer.

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

### Instagram and Facebook Reels (optional, free)

Every clip that goes to YouTube also goes up as an Instagram Reel and a Facebook Page Reel once these secrets exist
(`meta_upload.py`, Meta's official Graph API):

1. Switch your Instagram account to a Professional (Creator or Business) account and link it to a Facebook Page
   (Instagram app → Settings → Account type and tools; then Accounts Center → link the Page).
2. At https://developers.facebook.com create an app (type Business), add the **Instagram Graph API** /
   "Instagram API with Facebook Login" and **Facebook Login for Business** products.
3. In the Graph API Explorer pick your app, add the permissions `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, generate a User token, then exchange it for a
   long-lived one (Access Token Debugger → Extend). Call `/me/accounts` with it: the `access_token` there is a Page
   token that doesn't expire, and `id` is the Page ID. Call `/<page id>?fields=instagram_business_account` for the
   Instagram user ID.
4. Save them as repo secrets: `META_ACCESS_TOKEN` (the Page token), `FB_PAGE_ID`, `IG_USER_ID`. Leave either ID out to
   skip that platform. Your own app in development mode can post to accounts you admin; no app review needed.

### TikTok (optional cross-posting)

Every council-approved rendered clip can also go to TikTok through TikTok's official Content Posting API, even if
its YouTube upload fails or is held. It's off until
you turn it on.

1. Create an app at https://developers.tiktok.com (Manage apps → Connect an app). Add the **Login Kit** and
   **Content Posting API** products, with the scopes `user.info.basic`, `video.upload` and `video.publish`. Under
   Login Kit → Desktop, add the redirect URI `http://localhost:8723/callback/`. Use the app's Sandbox (with your
   TikTok account added under Target Users) to test before review; review needs a demo video of it working.
2. Put the app's Client key and Client secret in your user environment variables, as `TIKTOK_CLIENT_KEY` and
   `TIKTOK_CLIENT_SECRET`. Don't put them in any file.
3. Sign in once and turn cross-posting on: `python channel_builder.py tiktok-auth --enable` (add `--stats` after
   adding the `video.list` scope, so the review can read your views: see TikTok numbers)
   (`--mode direct` to post straight to the profile, `--disable` to turn it off).

Modes:
- `inbox` (default): each clip lands in your TikTok inbox as a draft. Tap the notification, paste the caption
  (`publish` prints it and saves it in queue.json as `tiktok_caption`), and post. This works before TikTok's audit.
  TikTok allows 5 pending drafts per 24 hours. The builder checks recent tracked draft statuses and pauses at 4
  pending drafts to leave a safety slot. If a status check fails, it pauses until drafts can be checked again.
  Finish or clear pending drafts in TikTok before retrying.
- Captions (`tiktok_caption.py`) are written for TikTok search: the creator's full name first (IShowSpeed, not
  Speed), what happens, the stream/clip topic, any guest named in the stream title, a question for the comments,
  then 3-5 specific hashtags (creator, guest, game, #twitch/#kick). Never `#fyp`/`#viral`/`#foryou`, which don't help.
  Example: `IShowSpeed: "Oh I'm Not Letting Him Win After This" | Can I Return a 120 MPH Serve From Serena Williams?
  🔥 Rate this duo 1-10 #ishowspeed #serenawilliams #tennis #livestream #ishowspeedclips`
- Clips under 9 seconds don't go to TikTok (they can't hold a viewer, and inbox drafts are capped at 5 a day).
- The on-screen hook sits 250px from the top so TikTok's "Following | For You" tabs don't cover it.
- `direct`: posts on its own, with the caption. Until TikTok audits the app, posts are private-only, and the
  TikTok account itself must be private. Apply for the audit from the app's page, then switch modes.

