"""Creators the channel builder clips, and where their long-form footage lives.

Each source needs at least one place to find full streams/videos:
- youtube: channel ID (UC...) or @handle. Full live-stream VODs or long videos there are mined
  for the moments viewers timestamp in the comments.
- twitch: login name. The builder pulls the creator's most-viewed Twitch clips (what viewers clipped live).
  With TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET set (free at dev.twitch.tv) they point at the exact second in
  the VOD; without keys, yt-dlp reads the public clips page and each clip is used whole.

permission: what you know about clipping rights. Set it to "blocked" to stop the builder from ever
picking or downloading that creator. Update it when you join a creator's clipping program.
campaign_cpm: $ per 1,000 views a clipping program/campaign pays you for this creator (None = none known).

Edit freely: add creators, drop ones that flop for you (`track` tells you which).
"""

SOURCES = {
    "ishowspeed": {
        "name": "IShowSpeed", "short": "Speed", "niche": "streamer",
        "youtube": "UCWsDFcIhY2DBi3GB5uykGXA",
        "aliases": ["ishowspeed", "speed"],
        "hashtags": ["#ishowspeed", "#speed"],
        "permission": "fan clipping is widespread and tolerated; no official paid program found (Oct 2026)",
        "campaign_cpm": None,
    },
    "kai_cenat": {
        "name": "Kai Cenat", "short": "Kai Cenat", "niche": "streamer",
        "youtube": "UCvCfpQXRXdJdL07pzTIA6Cw",   # Kai Cenat Live: mostly his own Shorts; full streams are on Twitch
        "twitch": "kaicenat",
        "aliases": ["kai cenat", "kai", "kaicenat"],
        "hashtags": ["#kaicenat", "#kai"],
        "permission": "fan clipping is widespread and tolerated; no official paid program found (Oct 2026)",
        "campaign_cpm": None,
    },
    "jynxzi": {
        "name": "Jynxzi", "short": "Jynxzi", "niche": "streamer",
        "youtube": "UCjiXtODGCCulmhwypZAWSag",
        "twitch": "jynxzi",
        "aliases": ["jynxzi"],
        "hashtags": ["#jynxzi"],
        "permission": "fan clipping tolerated; check for a Whop campaign",
        "campaign_cpm": None,
    },
    "adin_ross": {
        "name": "Adin Ross", "short": "Adin", "niche": "streamer",
        "youtube": "UCey-eDTR5J6xU6pZ2f4guoA",   # Adin Live: full Brand Risk streams
        "aliases": ["adin ross", "adin", "brand risk"],
        "hashtags": ["#adinross", "#brandrisk"],
        "permission": "runs clipping groups on Discord; join to get paid",
        "campaign_cpm": None,
    },
    "n3on": {
        "name": "N3on", "short": "N3on", "niche": "streamer",
        "youtube": "UCsiqlKIUDHZJVG4smMvhF4w",
        "aliases": ["n3on", "neon"],
        "hashtags": ["#n3on"],
        "permission": "official paid clipping program via his Discord (creatordb.app, 2026)",
        "campaign_cpm": 0.40,   # reported $40 per 100k views
    },
    # Self-improvement runner-up niche: long podcasts with timestamped comments clip well.
    "modern_wisdom": {
        "name": "Chris Williamson", "short": "Chris Williamson", "niche": "self_improvement",
        "youtube": "@ChrisWillx",
        "aliases": ["chris williamson", "modern wisdom"],
        "hashtags": ["#modernwisdom", "#selfimprovement"],
        "permission": "unknown: check for a clipping campaign before posting",
        "campaign_cpm": None,
    },
    "diary_of_a_ceo": {
        "name": "Diary Of A CEO", "short": "Steven Bartlett", "niche": "self_improvement",
        "youtube": "@TheDiaryOfACEO",
        "aliases": ["steven bartlett", "diary of a ceo", "doac"],
        "hashtags": ["#diaryofaceo", "#selfimprovement"],
        "permission": "unknown: check for a clipping campaign before posting",
        "campaign_cpm": None,
    },
}
