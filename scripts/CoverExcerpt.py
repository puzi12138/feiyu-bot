"""Limit every locally generated cover to a short vocal-rich excerpt."""

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf


def find_chorus(vocal: Path, speed: float) -> tuple[float, float, float]:
    """Locate one repeated vocal passage using pitch-class recurrence.

    Args:
        vocal: Isolated singing in original source time.
        speed: Output playback speed.

    Returns:
        Output start time, excerpt duration, and recurrence similarity.

    Raises:
        ValueError: No sufficiently repeated vocal passage can be located.
    """
    import librosa

    audio, rate = librosa.load(vocal, sr=8000, mono=True)
    duration = len(audio) / rate / speed
    if duration <= 30:
        return 0.0, duration, 1.0
    chroma = librosa.feature.chroma_stft(y=audio, sr=rate, n_fft=2048, hop_length=256)
    step = rate // 2
    count = len(audio) // step
    bins = np.array(
        [
            np.mean(
                chroma[
                    :,
                    int(i * step / 256) : max(
                        int((i + 1) * step / 256), int(i * step / 256) + 1
                    ),
                ],
                axis=1,
            )
            for i in range(count)
        ]
    ).T
    energies = np.array(
        [np.sqrt(np.mean(audio[i * step : (i + 1) * step] ** 2)) for i in range(count)]
    )
    if np.max(energies) < 0.002 or np.mean(np.std(bins, axis=1)) < 0.015:
        raise ValueError("chorus_not_found: 无法可靠定位副歌，请直接提供一段副歌音频。")
    bins -= 0.6 * bins.mean(axis=1, keepdims=True)
    bins /= np.maximum(np.linalg.norm(bins, axis=0, keepdims=True), 1e-8)
    window = max(16, round(48 * speed))
    positions = np.arange(20, count - window - 4, 2)
    if len(positions) < 2:
        raise ValueError("chorus_not_found: 重复段不足，请直接提供一段副歌音频。")
    features = np.array([bins[:, i : i + window].ravel() for i in positions])
    features /= np.maximum(np.linalg.norm(features, axis=1, keepdims=True), 1e-8)
    similarity = features @ features.T
    similarity[np.abs(positions[:, None] - positions[None, :]) < window + 12] = -1
    recurring = similarity.max(axis=1)
    threshold = max(float(np.percentile(energies, 70)) * 0.18, 0.001)
    coverage = np.array(
        [np.mean(energies[i : i + window] > threshold) for i in positions]
    )
    scores = recurring + coverage * 0.25
    scores[(recurring < 0.55) | (coverage < 0.65)] = -1
    best = int(np.argmax(scores))
    if scores[best] < 0:
        raise ValueError(
            "chorus_not_found: 没有可靠识别到副歌，请直接提供一段副歌音频。"
        )
    start = float(positions[best]) * 0.5 / speed
    return start, min(24.0, duration - start), float(recurring[best])


def make_excerpt(result: Path, vocal: Path, speed: float) -> tuple[Path, dict]:
    """Send one inferred chorus, never an arbitrary high-volume passage.

    Args:
        result: Completed mixed MP3 in the owned native job directory.
        vocal: Separated dry vocals, in original source time.
        speed: Native playback speed used when mixing the cover.

    Returns:
        Original short audio or trimmed MP3, with excerpt timing metadata.
    """
    start, duration, confidence = find_chorus(vocal, speed)
    if start == 0 and sf.info(vocal).duration / speed <= 30:
        return result, {
            "excerpt_start": 0.0,
            "excerpt_seconds": duration,
            "selection": "provided_short_clip",
        }
    target = result.with_name("ai_cover_excerpt.mp3")
    subprocess.run(
        [
            "D:/QQBot/runtime/ffmpeg/bin/ffmpeg.exe",
            "-y",
            "-v",
            "error",
            "-ss",
            str(start),
            "-i",
            str(result),
            "-t",
            str(duration),
            "-af",
            f"afade=t=in:d=0.12,afade=t=out:st={max(0, duration - 0.35)}:d=0.35",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            str(target),
        ],
        check=True,
        timeout=45,
    )
    return target, {
        "excerpt_start": round(start, 3),
        "excerpt_seconds": duration,
        "selection": "inferred_repeated_vocal_chorus",
        "recurrence_similarity": round(confidence, 3),
    }
