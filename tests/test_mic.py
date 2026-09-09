#!/usr/bin/env python3
"""Quick mic test — lists audio devices and checks RMS levels."""
import time
import numpy as np
import pyaudio

pa = pyaudio.PyAudio()

print("\n=== Available input devices ===")
default_index = pa.get_default_input_device_info()["index"]
for i in range(pa.get_device_count()):
    info = pa.get_device_info_by_index(i)
    if info["maxInputChannels"] > 0:
        marker = " ← DEFAULT" if i == default_index else ""
        print(f"  [{i}] {info['name']}{marker}")

print(f"\nTesting default device [{default_index}] for 5 seconds...")
print("Speak into your mic — you should see RMS values rise above 200:\n")

stream = pa.open(
    rate=16000,
    channels=1,
    format=pyaudio.paInt16,
    input=True,
    input_device_index=default_index,
    frames_per_buffer=1280,
)

for _ in range(60):  # ~5 seconds
    data = stream.read(1280, exception_on_overflow=False)
    pcm  = np.frombuffer(data, dtype=np.int16)
    rms  = int(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
    bar  = "█" * min(40, rms // 50)
    print(f"  RMS: {rms:5d}  {bar}")
    time.sleep(0.08)

stream.stop_stream()
stream.close()
pa.terminate()
print("\nDone. If RMS stayed near 0, PyAudio is using the wrong device.")
print("Re-run with: python test_mic.py <device_index>  to try another device.")

import sys
if len(sys.argv) > 1:
    idx = int(sys.argv[1])
    pa2 = pyaudio.PyAudio()
    info = pa2.get_device_info_by_index(idx)
    print(f"\nTesting device [{idx}]: {info['name']}")
    s = pa2.open(rate=16000, channels=1, format=pyaudio.paInt16,
                 input=True, input_device_index=idx, frames_per_buffer=1280)
    for _ in range(60):
        data = s.read(1280, exception_on_overflow=False)
        pcm  = np.frombuffer(data, dtype=np.int16)
        rms  = int(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        bar  = "█" * min(40, rms // 50)
        print(f"  RMS: {rms:5d}  {bar}")
        time.sleep(0.08)
    s.stop_stream(); s.close(); pa2.terminate()
