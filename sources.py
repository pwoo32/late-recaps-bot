"""Creators the channel builder clips, and where their long-form footage lives.

Each source needs at least one place to find full streams/videos:
- youtube: channel ID (UC...) or @handle. Full live-stream VODs or long videos there are mined
  for the moments viewers timestamp in the comments.
- twitch: login name. The builder pulls the creator's most-viewed Twitch clips (what viewers clipped live).
  With TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET set (free at dev.twitch.tv) they point at the exact second in
  the VOD; without keys, yt-dlp reads the public clips page and each clip is used whole.
- kick: channel slug. The builder pulls the most-viewed Kick clips of the week (yt-dlp, no keys) and uses each whole.

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
        "kick": "adinross",                       # streams live on Kick
        "aliases": ["adin ross", "adin", "brand risk"],
        "hashtags": ["#adinross", "#brandrisk"],
        "permission": "runs clipping groups on Discord; join to get paid",
        "campaign_cpm": None,
    },
    "n3on": {
        "name": "N3on", "short": "N3on", "niche": "streamer",
        "youtube": "UCsiqlKIUDHZJVG4smMvhF4w",
        "kick": "n3on",
        "aliases": ["n3on", "neon"],
        "hashtags": ["#n3on"],
        "permission": "official paid clipping program via his Discord (creatordb.app, 2026)",
        "campaign_cpm": 0.40,   # reported $40 per 100k views
    },
    # Big live streamers with no YouTube VOD channel: clips come from what viewers clip on Twitch/Kick, ranked by views,
    # so whoever is pulling the most views that day gets clipped.
    "caseoh": {
        "name": "CaseOh", "short": "CaseOh", "niche": "streamer",
        "twitch": "caseoh_",
        "aliases": ["caseoh", "case oh"],
        "hashtags": ["#caseoh"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
    },
    "xqc": {
        "name": "xQc", "short": "xQc", "niche": "streamer",
        "twitch": "xqc", "kick": "xqc",
        "aliases": ["xqc"],
        "hashtags": ["#xqc"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
    },
    "lacy": {
        "name": "Lacy", "short": "Lacy", "niche": "streamer",
        "twitch": "lacy",
        "aliases": ["lacy"],
        "hashtags": ["#lacy"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
    },
    "plaqueboymax": {
        "name": "PlaqueBoyMax", "short": "Max", "niche": "streamer",
        "twitch": "plaqueboymax",
        "aliases": ["plaqueboymax", "plaque boy max"],
        "hashtags": ["#plaqueboymax"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
    },
    # Fastest-rising streamer of 2025-26 (animated peanut avatar, Arc Raiders/Tarkov/horror games); a less
    # saturated clip lane than Speed/Kai, and his deadpan voice lines are very clippable.
    "theburntpeanut": {
        "name": "TheBurntPeanut", "short": "Peanut", "niche": "streamer",
        "twitch": "theburntpeanut",
        "aliases": ["theburntpeanut", "burnt peanut", "the burnt peanut", "peanut"],
        "hashtags": ["#theburntpeanut", "#burntpeanut"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
    },
    "stableronaldo": {
        "name": "Stable Ronaldo", "short": "Ronaldo", "niche": "streamer",
        "twitch": "stableronaldo",
        "aliases": ["stableronaldo", "stable ronaldo"],
        "hashtags": ["#stableronaldo"],
        "permission": "fan clipping is widespread and tolerated; no official program checked (Nov 2026)",
        "campaign_cpm": None,
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
