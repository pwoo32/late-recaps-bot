"""Voice check, run in the cloud (voice-test workflow) with the same install as the daily run: renders narrator
lines through channel_builder.narrate() exactly as a clip would, and times them. Output goes to voice-samples/."""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2  # noqa: E402,F401  (the clip framing needs it; make sure the voice install didn't break it)
import channel_builder as cb  # noqa: E402

OUT = Path("voice-samples")
OUT.mkdir(exist_ok=True)
LINES = ["Bro, Jynxzi put a thousand dollars on this one v one, and he really thought he had it. Watch his face right here.",
         "Okay, keep your eyes on Speed right here.",
         "This is exactly why chat can't stop clipping Kai Cenat."]
log = []
for i, line in enumerate(LINES, 1):
    t = time.time()
    w = cb.narrate(line, OUT / f"line{i}")
    log.append(f"line {i}: {time.time() - t:.1f}s -> {w.name if w else None}")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(w), "-af", cb.VOICE_CHAIN, "-b:a", "128k",
                    str(OUT / f"final_{i}.mp3")], check=True)
    w.unlink()
(OUT / "timings.txt").write_text("\n".join(log) + "\n")
print("\n".join(log))
