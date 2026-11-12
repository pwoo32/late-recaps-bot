"""Edit tuner: lets the channel's own analytics change how clips are edited.

Every clip goes out with an edit "recipe": clip length, how hard and how often the frame punches in, how many words
each caption shows, how much silence the jump cuts remove, and the framing. The default recipe is the current
format (face-tracked 3:4 frame, jump cuts, a punch-in every 1.5-2.5s, 3-word captions, narrator setup line and closing payoff line).

Each day the best clip gets the best recipe found so far, and the other clips each try one change, so the channel
keeps testing without betting the whole day on an experiment. review_agent.py scores every clip once it is two days
old (views, how much of it people watched, how many stayed past the first swipe) and credits the score to each
setting that clip used. A setting replaces the default once it has beaten it on at least 3 clips.

State lives in reports/builder/state.json under "edit". No API calls, no model, nothing to pay for.
"""

import math
import statistics
import zlib

# name: (default, options). Lengths are seconds of source before jump cuts.
PARAMS = {
    "length": (30, [20, 30, 45]),
    "punch": (0.14, [0.08, 0.14, 0.2]),        # how far each zoom punch goes in
    "beat": ("normal", ["fast", "normal", "slow"]),  # how often the frame changes
    "caption_words": (3, [2, 3]),
    "gap": (0.6, [0.35, 0.6, 1.0]),            # silence longer than this gets jump-cut
    "layout": ("face34", ["face34", "crop43"]),
    # No "narrate" setting any more: the narrator is always on. It is the channel's own layer over the stream, and
    # testing "off" meant posting bare reuploads, which keeps the whole channel out of the Shorts feed (Nov 2026).
}
BEATS = {"fast": (1.0, 1.8), "normal": (1.5, 2.5), "slow": (2.2, 3.5)}
MIN_TRIES = 3     # a setting needs this many scored clips before it can replace the default
KEEP = 20         # scores kept per setting (newest), so old results fade as the channel grows


def default_recipe():
    return {k: d for k, (d, _) in PARAMS.items()}


def _stats(state):
    return state.setdefault("edit", {}).setdefault("stats", {})


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def best_recipe(state):
    """Per setting: the best-scoring option with at least MIN_TRIES clips, if it beats the default's average."""
    stats, out = _stats(state), default_recipe()
    for k, (d, opts) in PARAMS.items():
        s = stats.get(k, {})
        means = {o: _mean(s.get(str(o), [])) for o in opts if len(s.get(str(o), [])) >= MIN_TRIES}
        if not means:
            continue
        top = max(means, key=means.get)
        base = _mean(s.get(str(d), []))
        if top != d and (base is None or means[top] > base + 0.1):
            out[k] = top
    return out


def recipes(state, n, day_seed=""):
    """Recipes for today's n clips: #1 gets the best known, each other clip tests one change (least-tried first)."""
    best = best_recipe(state)
    stats = _stats(state)
    out = [dict(best, test=None)]
    keys = list(PARAMS)
    start = zlib.crc32(day_seed.encode()) % len(keys)
    for i in range(1, n):
        k = keys[(start + i - 1) % len(keys)]
        opts = [o for o in PARAMS[k][1] if o != best[k]]
        o = min(opts, key=lambda o: (len(stats.get(k, {}).get(str(o), [])), zlib.crc32(f"{day_seed}{i}{o}".encode())))
        out.append(dict(best, **{k: o}, test=k))
    return out


def score(views, avg_viewed=None, stayed=None, base_views=None):
    """One number per clip: views on a log scale against the channel's median, plus retention when known."""
    s = math.log1p(views) - math.log1p(base_views or 0)
    if avg_viewed is not None:
        s += (avg_viewed - 70) / 30
    if stayed is not None:
        s += (stayed - 0.6) * 2
    return round(s, 3)


def credit(state, video_id, recipe, value):
    """Add one clip's score to every setting it used. Each video is scored once."""
    edit = state.setdefault("edit", {})
    done = edit.setdefault("scored", [])
    if not recipe or video_id in done:
        return False
    stats = _stats(state)
    for k in PARAMS:
        if k in recipe:
            xs = stats.setdefault(k, {}).setdefault(str(recipe[k]), [])
            xs.append(value)
            del xs[:-KEEP]
    done.append(video_id)
    del done[:-500]
    edit["best"] = best_recipe(state)
    return True


def summary(state):
    """Readable lines for reports: each setting's options with average score and clip count."""
    stats, best = _stats(state), best_recipe(state)
    lines = []
    for k, (d, opts) in PARAMS.items():
        parts = []
        for o in opts:
            xs = stats.get(k, {}).get(str(o), [])
            tag = " (in use)" if o == best[k] else ""
            parts.append(f"{o}{tag}: " + (f"{_mean(xs):+.2f} over {len(xs)}" if xs else "untested"))
        lines.append(f"- {k}: " + "; ".join(parts))
    return lines
