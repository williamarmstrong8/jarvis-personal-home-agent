#!/usr/bin/env python3
"""Summarize Jarvis latency traces by routing path."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def median_delta(rows: list[dict], start: str, end: str) -> float:
    values = []
    for row in rows:
        marks = row.get("marks_ms", {})
        if marks.get(start) is not None and marks.get(end) is not None:
            values.append(float(marks[end]) - float(marks[start]))
    return statistics.median(values) if values else float("nan")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path("data/logs/latency.jsonl"))
    args = parser.parse_args()
    if not args.path.exists():
        parser.error(f"trace file not found: {args.path}")

    groups: dict[str, list[dict]] = defaultdict(list)
    for line in args.path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = row.get("intent_tool") or row.get("intent_mode") or row.get("source", "unknown")
        groups[str(key)].append(row)

    print(
        f"{'path':24} {'n':>5} {'total':>9} {'p95':>9} "
        f"{'post-speech':>12} {'stt':>9} {'stt-tail':>10} {'response':>10} {'tts':>9}"
    )
    for key, rows in sorted(groups.items()):
        totals = [r.get("marks_ms", {}).get("turn_complete") for r in rows]
        totals = [float(v) for v in totals if v is not None]
        if not totals:
            continue
        median = statistics.median(totals)
        p95 = percentile(totals, .95)
        post_speech = median_delta(rows, "speech_ended", "first_audio")
        stt = median_delta(rows, "stt_started", "stt_completed")
        stt_tail = median_delta(rows, "speech_ended", "stt_completed")
        response = median_delta(rows, "stt_completed", "response_ready")
        tts = median_delta(rows, "tts_started", "first_audio")
        print(
            f"{key[:24]:24} {len(totals):5d} {median:8.0f}ms {p95:8.0f}ms "
            f"{post_speech:11.0f}ms {stt:8.0f}ms {stt_tail:9.0f}ms "
            f"{response:9.0f}ms {tts:8.0f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
