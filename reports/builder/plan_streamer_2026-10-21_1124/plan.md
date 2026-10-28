# Channel plan: Streamer clips (Twitch/Kick/YouTube live)

Built 2026-10-21 from trend report `2026-10-21_1047` and channel study `channels_streamer_2026-10-21_1107`. Rerun `plan` after new reports to refresh it.

## 1. Niche

**Streamer clips (Twitch/Kick/YouTube live)**: score 82.9/100 (#2 of 12), top clips pull 41,370 views/hour, 90% of small channels in the sample hit 100k+, reuse risk 35%.
Why this one: the creators stream for hours almost daily and post full VODs, so there is a steady supply of fresh moments you can clip before anyone else, and fans search the creators by name.

Alternatives from the same report: Motivation, fitness & self-improvement clips (score 88.2, 20,664 views/hr); Gaming clips & highlights (score 72.5, 18,072 views/hr); Stand-up & comedy podcast clips (score 64.9, 12,184 views/hr). Run `plan --niche <name>` to build for one of them.

## 2. Channel identity

Name ideas whose @handle was free on YouTube today: **Late Recaps** (@laterecaps), **Bando Files** (@bandofiles), **Lobby Recaps** (@lobbyrecaps), **Bando Desk** (@bandodesk), **Crashout Replay** (@crashoutreplay).
Short, two words, "<word> Recaps"-style like the leading channel Nexxo Recaps. Grab the handle the same day you pick it.

- **Bio:** "The best moments from IShowSpeed, Kai Cenat, Jynxzi streams, clipped daily. All credit to the creators."
- **Look:** one bold font and one accent color across every hook, captions in the same style, a simple logo (initials), banner listing who you clip.
- **Voice:** a recurring signature, e.g. a 2-4s spoken take at the start of each clip. It's what makes the channel yours rather than a re-upload account, and YouTube now ranks that difference.

## 3. Format

- **Length:** about 52s. The studied channels' median Short is 25s; by lift: 0-15s 0.92x, 15-30s 1.01x, 30-45s 0.91x, 45-60s 1.17x.
- **Structure:** hook text on screen from frame 1, a one-line context caption for the first 3s, your spoken take over the opening, then setup and payoff. End right on the payoff so it loops.
- **Frame:** 9:16, the stream cropped big in the middle over a blurred copy of itself, word-by-word captions. `cut` does all of this.

## 4. Titles and tags

- Creator's name first, what happens in plain words, ~42 characters, 1-2 emoji at the end, no questions, no ALL CAPS.
- Breakouts to model:
  - Ray Thinks She Is a Grown Man❤️‍🩹😭 (19,296,777 views, 272.3x)
  - This Guy Made Speed’s Backflip Look Like Nothing🥶🤣 (16,495,553 views, 175.4x)
  - iShowSpeed Crashes OUT When Jamal Guesses Where Pizza Is From! 🍕😂 (11,466,665 views, 161.8x)
  - Rakai Breaks Up A Wholesome Moment Between Kai Cenat And Random Guy🥀🤣 (7,970,606 views, 112.5x)
  - Speed’s Strenght Evolution Year By Year🥶☠️ (7,941,121 views, 84.4x)
  - Kai Cenat Can't Believe Tota Ate Cake From The Floor!🎂😂 (5,939,847 views, 83.8x)
- Hashtags: #ishowspeed, #shorts, #edit, #kaicenat, #speed, #rakai, #tota, #jschlatt.

## 5. Who to clip

| Creator | Demand now (views/hr on clips naming them) | Where full streams are | Permission | Campaign $/1k |
|---|---|---|---|---|
| IShowSpeed | 1,530,149 | YouTube | fan clipping is widespread and tolerated; no official paid program found (Oct 2026) | - |
| Kai Cenat | 878,464 | YouTube, Twitch | fan clipping is widespread and tolerated; no official paid program found (Oct 2026) | - |
| Jynxzi | 36,198 | YouTube, Twitch | fan clipping tolerated; check for a Whop campaign | - |
| N3on | 14,523 | YouTube | official paid clipping program via his Discord (creatordb.app, 2026) | $0.40 |
| Adin Ross | 0 | YouTube | runs clipping groups on Discord; join to get paid | - |

The top channels also clip: ishowspeed (115), kaicenat (90), rakai (32), reggie (26), duke dennis (15), fanum (15), jamal (13), tota (11), agent00 (10), ronaldo (9). Add any of them to sources.py.

## 6. Schedule

- Post 2 Shorts a day (top channels post 1.0/day; you need volume early to find what works).
- Best hours by lift in the study: 21:00 UTC (5 PM ET), 17:00 UTC (1 PM ET). `track` re-weights these from your own results.
- Daily routine (about an hour): `python channel_builder.py run` in the morning, review the queue, record the takes, `cut` each pick, schedule them in YouTube Studio.

## 7. Money

Two income streams, and early on only one of them is real:

1. **Clipping programs and campaigns** pay per view from day one (Whop Content Rewards, Vyro, creators' own Discords; reported rates $0.40-$3 per 1k views). Join every one that covers your creators and route those clips through it. Campaigns count views on TikTok and Instagram Reels too, so post every cut on all three.
2. **Shorts ad revenue** needs the YouTube Partner Program (1,000 subs + 10M Shorts views in 90 days) and pays about $0.02 per 1k views in this niche (niches.py estimate).

Scenarios at 2 Shorts/day, using 52,818 median views (young channels in the study) as the top case:

| Scenario | Median views per clip | Views / month | Shorts ads $/mo (after YPP) | Campaign $/mo (30% of clips in a campaign) |
|---|---|---|---|---|
| slow start | 2,640 | 158,454 | $3 | $19 |
| typical new channel | 13,204 | 792,270 | $16 | $95 |
| like the young winners | 52,818 | 3,169,080 | $63 | $380 |

These are scenarios, not forecasts: the views come from channels that already won. Expect weeks of low views while the algorithm learns the channel. Costs: $0 for the API keys; Claude-written titles are a few cents a day.

## 8. Rules that keep the channel alive

- Clip from the creator's own full stream/VOD, never re-upload another clip channel's Short.
- Add your own layer to every clip (take, hook, context, captions) and credit + link the creator in the description.
- Prefer creators with a clipping program; if a creator or rights holder asks you to take something down, do it. Three copyright strikes in 90 days ends the channel.
- No music-heavy segments (music claims), no clips of other people's TV/sports broadcasts inside streams.
