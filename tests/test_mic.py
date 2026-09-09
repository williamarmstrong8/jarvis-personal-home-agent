#!/usr/bin/env python3
"""Interactive microphone diagnostic; safe for unittest discovery."""

import sys
import time


def _exercise(pa, device_index: int, seconds: float = 5.0) -> None:
    import numpy as np
    import pyaudio

    info = pa.get_device_info_by_index(device_index)
    print(f"\nTesting device [{device_index}] {info['name']} for {seconds:g} seconds...")
    print("Speak into your mic — RMS should rise above the room-noise level:\n")
    stream = pa.open(
        rate=16000,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=1280,
    )
    try:
        for _ in range(round(seconds / 0.08)):
            data = stream.read(1280, exception_on_overflow=False)
            pcm = np.frombuffer(data, dtype=np.int16)
            rms = int(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
            bar = "█" * min(40, rms // 50)
            print(f"  RMS: {rms:5d}  {bar}")
            time.sleep(0.08)
    finally:
        stream.stop_stream()
        stream.close()


def main() -> int:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        print("\n=== Available input devices ===")
        default_index = int(pa.get_default_input_device_info()["index"])
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                marker = " ← DEFAULT" if i == default_index else ""
                print(f"  [{i}] {info['name']}{marker}")

        chosen = int(sys.argv[1]) if len(sys.argv) > 1 else default_index
        _exercise(pa, chosen)
    finally:
        pa.terminate()
    print("\nDone. If RMS stayed near zero, try: python tests/test_mic.py <device_index>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
