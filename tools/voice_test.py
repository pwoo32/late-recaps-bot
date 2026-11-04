"""Voice bake-off, run in the cloud (voice-test workflow): renders the same narrator line with the current voice
(Kokoro Fenrir) and with Chatterbox Turbo (built-in voice, and cloned from Fenrir so it keeps the voice Philip
picked), and reports how long each takes on the runner's CPU. Output goes to voice-samples/."""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import channel_builder as cb  # noqa: E402

OUT = Path("voice-samples")
OUT.mkdir(exist_ok=True)
LINE = "Bro, Jynxzi put a thousand dollars on this one v one, and he really thought he had it. Watch his face right here."
TAGGED = "Bro, Jynxzi put a thousand dollars on this one v one [chuckle], and he really thought he had it. Watch his face right here."
log = []


def mp3(wav, name):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-af", cb.VOICE_CHAIN, "-b:a", "128k",
                    str(OUT / f"{name}.mp3")], check=True)


t = time.time()
ref = cb.narrate(LINE, OUT / "kokoro_fenrir", "am_fenrir")
log.append(f"kokoro fenrir: {time.time() - t:.1f}s")
mp3(ref, "1_kokoro_fenrir_now")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(ref), "-i", str(ref), "-filter_complex",
                "[0:a][1:a]concat=n=2:v=0:a=1", "-ar", "24000", "-ac", "1", str(OUT / "ref.wav")], check=True)

import torchaudio as ta  # noqa: E402
from chatterbox.tts_turbo import ChatterboxTurboTTS  # noqa: E402

t = time.time()
m = ChatterboxTurboTTS.from_pretrained(device="cpu")
log.append(f"turbo load: {time.time() - t:.1f}s")
for name, text, prompt in [("2_turbo_builtin", TAGGED, None), ("3_turbo_fenrir_clone", TAGGED, str(OUT / "ref.wav")),
                           ("4_turbo_fenrir_clone_plain", LINE, str(OUT / "ref.wav"))]:
    t = time.time()
    w = m.generate(text, audio_prompt_path=prompt) if prompt else m.generate(text)
    wav = OUT / f"{name}.wav"
    ta.save(str(wav), w, m.sr)
    log.append(f"{name}: {time.time() - t:.1f}s for {w.shape[-1] / m.sr:.1f}s of audio")
    mp3(wav, name)
for f in OUT.glob("*.wav"):
    f.unlink()
for f in OUT.glob("*.take.*"):
    f.unlink()
(OUT / "timings.txt").write_text("\n".join(log) + "\n")
print("\n".join(log))
