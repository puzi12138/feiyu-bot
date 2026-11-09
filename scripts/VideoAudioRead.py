"""Transcribe up to ten minutes of local sampled speech using CPU int8."""

import json
import re
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

root = Path("D:/QQBot")
bvid = sys.argv[1]
if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
    raise ValueError("INVALID_BVID")
folder = root / "data/video-reading" / bvid
target = folder / "asr.json"
if target.exists():
    print(
        json.dumps(json.loads(target.read_text(encoding="utf-8")), ensure_ascii=True)
    )
    sys.exit()
assets = json.loads((folder / "assets.json").read_text(encoding="utf-8"))
if assets["audio_seconds"] > 600.01:
    raise ValueError("AUDIO_BUDGET")
started = time.monotonic()
model = WhisperModel(
    str(root / "data/models/faster-whisper-small"),
    device="cpu",
    compute_type="int8",
    cpu_threads=6,
    num_workers=1,
    local_files_only=True,
)
rows = []
for sample in assets["audio_samples"]:
    path = Path(sample["path"]).resolve()
    if path.parent != folder.resolve():
        raise ValueError("UNEXPECTED_SAMPLE_PATH")
    segments, _ = model.transcribe(
        str(path),
        language="zh",
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    for segment in segments:
        if time.monotonic() - started > 900:
            raise TimeoutError("ASR_TIME_BUDGET")
        rows.append(
            {
                "start": round(sample["start"] + segment.start, 3),
                "end": round(sample["start"] + min(segment.end, sample["seconds"]), 3),
                "text": segment.text.strip(),
            }
        )
result = {
    "ok": True,
    "bvid": bvid,
    "basis": "audio_asr_sampled",
    "audio_seconds": assets["audio_seconds"],
    "video_seconds": assets["duration"],
    "windows": [
        {"start": r["start"], "seconds": r["seconds"]} for r in assets["audio_samples"]
    ],
    "segments": rows,
    "limitation": "ASR from actual sampled audio, may mishear names; does not assess music, vocal emotion or unsampled audio.",
    "elapsed_seconds": round(time.monotonic() - started, 2),
}
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
