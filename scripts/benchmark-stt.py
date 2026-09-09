#!/usr/bin/env python3
"""Compare Jarvis STT backends against recorded commands.

Place ``.wav`` files in a directory. Optional same-name ``.txt`` files provide
reference transcripts and enable word-error-rate reporting.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        cur = [i]
        for j, right in enumerate(b, 1):
            cur.append(min(
                cur[-1] + 1,
                prev[j] + 1,
                prev[j - 1] + (left != right),
            ))
        prev = cur
    return prev[-1]


def _wer(reference: str, hypothesis: str) -> float:
    expected = reference.lower().split()
    actual = hypothesis.lower().split()
    return _distance(expected, actual) / max(1, len(expected))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_dir", type=Path)
    parser.add_argument(
        "--backends", nargs="+", default=["openai-whisper", "whisper.cpp"]
    )
    args = parser.parse_args()

    wavs = sorted(args.audio_dir.glob("*.wav"))
    if not wavs:
        parser.error(f"no .wav files found in {args.audio_dir}")

    from core.speech import _whisper_cpp_available, transcribe, warmup_stt

    rows = []
    for backend in args.backends:
        if backend == "whisper.cpp" and not _whisper_cpp_available():
            print("Skipping whisper.cpp: configure WHISPER_CPP_BIN and WHISPER_CPP_MODEL")
            continue
        warmup_stt(backend)
        for wav in wavs:
            t0 = time.perf_counter()
            text = transcribe(str(wav), backend=backend)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            reference_path = wav.with_suffix(".txt")
            reference = reference_path.read_text().strip() if reference_path.exists() else ""
            rows.append({
                "backend": backend,
                "file": wav.name,
                "elapsed_ms": elapsed_ms,
                "transcript": text,
                "wer": round(_wer(reference, text), 4) if reference else None,
            })

    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    for backend in args.backends:
        selected = [row for row in rows if row["backend"] == backend]
        if not selected:
            continue
        latencies = [row["elapsed_ms"] for row in selected]
        wers = [row["wer"] for row in selected if row["wer"] is not None]
        summary = {
            "backend": backend,
            "files": len(selected),
            "median_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 1),
            "mean_wer": round(statistics.mean(wers), 4) if wers else None,
        }
        print("SUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
