"""Clip niches the agent scans, with what each one pays.

Money figures are 2026 third-party estimates, not YouTube-published numbers:
- shorts_rpm: what a monetized channel earns per 1,000 Shorts views from the
  Shorts creator pool (growcreator.pro, miraflow.ai, fluxnote.io).
- campaign_cpm: what clipping campaigns (Whop, Vyro, ClipAffiliates, etc.) pay
  per 1,000 views after typical marketplace/agency cuts (ascynd.io, clipaffiliates).
  None means campaigns are rare for this niche.
- reuse_risk: 0..1, how likely plain clips get hit by YouTube's reused /
  inauthentic content policy (compilations and unlicensed sports/TV are worst).

Edit freely: add queries, tweak numbers as your own analytics come in.
"""

NICHES = {
    "business_podcast": {
        "label": "Business & money podcast clips",
        "queries": ["business podcast", "entrepreneur advice", "how to make money"],
        "shorts_rpm": 0.08, "campaign_cpm": 4.0, "reuse_risk": 0.3,
    },
    "finance_trading": {
        "label": "Finance, investing & trading clips",
        "queries": ["stock market", "personal finance tips", "investing advice"],
        "shorts_rpm": 0.10, "campaign_cpm": 4.5, "reuse_risk": 0.3,
    },
    "crypto": {
        "label": "Crypto clips",
        "queries": ["bitcoin", "crypto news"],
        "shorts_rpm": 0.09, "campaign_cpm": 5.0, "reuse_risk": 0.35,
    },
    "tech_ai": {
        "label": "Tech & AI clips",
        "queries": ["ai tools", "tech news"],
        "shorts_rpm": 0.07, "campaign_cpm": 3.5, "reuse_risk": 0.3,
    },
    "self_improvement": {
        "label": "Motivation, fitness & self-improvement clips",
        "queries": ["motivation speech", "gym motivation", "self improvement"],
        "shorts_rpm": 0.04, "campaign_cpm": 2.5, "reuse_risk": 0.45,
    },
    "streamer": {
        "label": "Streamer clips (Twitch/Kick/YouTube live)",
        "queries": ["streamer clips", "kai cenat", "ishowspeed", "twitch clips"],
        "shorts_rpm": 0.02, "campaign_cpm": 2.0, "reuse_risk": 0.35,
    },
    "gaming": {
        "label": "Gaming clips & highlights",
        "queries": ["gaming clips", "fortnite", "gta 6"],
        "shorts_rpm": 0.02, "campaign_cpm": 1.5, "reuse_risk": 0.35,
    },
    "comedy_standup": {
        "label": "Stand-up & comedy podcast clips",
        "queries": ["stand up comedy", "comedy podcast"],
        "shorts_rpm": 0.02, "campaign_cpm": 1.5, "reuse_risk": 0.4,
    },
    "sports": {
        "label": "Sports highlights & sports podcast clips",
        "queries": ["nba highlights", "nfl highlights", "sports podcast"],
        "shorts_rpm": 0.03, "campaign_cpm": 1.5, "reuse_risk": 0.7,
    },
    "movie_tv": {
        "label": "Movie & TV scene clips",
        "queries": ["movie clip", "tv show scene"],
        "shorts_rpm": 0.02, "campaign_cpm": None, "reuse_risk": 0.85,
    },
    "anime": {
        "label": "Anime clips & edits",
        "queries": ["anime clip", "anime edit"],
        "shorts_rpm": 0.015, "campaign_cpm": None, "reuse_risk": 0.8,
    },
    "irl_reality": {
        "label": "IRL, reality & interview clips",
        "queries": ["street interview", "reality show clip"],
        "shorts_rpm": 0.025, "campaign_cpm": 2.0, "reuse_risk": 0.4,
    },
}
