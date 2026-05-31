from __future__ import annotations


def apply_pcm16le_gain(audio: bytes, gain: float) -> bytes:
    clamped_gain = max(0.0, min(1.0, gain))
    if clamped_gain >= 0.999:
        return audio
    data = bytearray(audio)
    even_length = len(data) - (len(data) % 2)
    for index in range(0, even_length, 2):
        sample = int.from_bytes(data[index : index + 2], "little", signed=True)
        sample = int(round(sample * clamped_gain))
        sample = max(-32768, min(32767, sample))
        data[index : index + 2] = sample.to_bytes(2, "little", signed=True)
    return bytes(data)
