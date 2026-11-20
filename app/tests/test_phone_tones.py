"""電話の部品の道具の試験（持ち主 CL-3。agent/phones/audio.py・tones.py。docs/PHONE.md 2-2・2-3）。

本物の電話の音の代わりに、作った音で確かめる（⏳ 本物の音は確かめ R-6 で録って、表の値を決めてから足す）。データベースは使わない。
"""

from __future__ import annotations

import math
import random
import time
import unittest

from salescall.agent.phones import audio, tones

RATE = 16000


def tone(hz: float, seconds: float, rate: int = RATE, amp: float = 8000.0) -> bytes:
    return audio.pcm_of([amp * math.sin(2 * math.pi * hz * i / rate) for i in range(int(seconds * rate))])


def silence(seconds: float, rate: int = RATE) -> bytes:
    return audio.pcm_of([0.0] * int(seconds * rate))


def noise(seconds: float, rate: int = RATE, amp: float = 6000.0, seed: int = 1) -> bytes:
    r = random.Random(seed)
    return audio.pcm_of([r.uniform(-amp, amp) for _ in range(int(seconds * rate))])


def feed(pcm: bytes) -> str | None:
    c = tones.Cadence(RATE)
    got = None
    for frame in audio.Framer(RATE).push(pcm):
        got = c.push(frame) or got
    return got


class Audio(unittest.TestCase):
    def test_resample_keeps_the_length_and_the_pitch(self):
        src = tone(400, 1.0, rate=48000)
        down = audio.resample(src, 48000, RATE)
        self.assertEqual(len(down), RATE * 2)                               # 1秒ぶん
        frame = down[2000:2000 + 640]
        self.assertGreater(tones.share(frame, RATE, 400), 0.8)              # 高さは変わらない
        up = audio.resample(down, RATE, 24000)
        self.assertEqual(len(up), 24000 * 2)

    def test_going_down_drops_sounds_too_high_for_the_new_rate(self):
        high = tone(7000, 0.5, rate=48000)                                  # 16kHz では表せない高さ（折り返すと 9kHz→7kHz に化ける）
        down = audio.resample(high, 48000, RATE)
        x = audio.samples(down)
        rms = math.sqrt(sum(v * v for v in x[200:-200]) / len(x[200:-200]))
        self.assertLess(rms, 8000 * 0.5)                                    # 大きく落ちる（そのまま残らない）

    def test_framer_cuts_into_20ms_and_keeps_the_rest(self):
        f = audio.Framer(RATE)
        self.assertEqual(f.push(b"\x00" * 1000), [b"\x00" * 640])
        self.assertEqual(len(f.push(b"\x00" * 300)), 1)                     # 残りの 360 と合わせて1つ

    def test_one_second_of_48k_converts_fast_enough_for_a_call(self):
        src = noise(1.0, rate=48000)
        t = time.perf_counter()
        audio.resample(src, 48000, RATE)
        self.assertLess(time.perf_counter() - t, 0.5)                       # 1秒の声を半秒より短く（通話を遅らせない）


class Tones(unittest.TestCase):
    def test_ringback_is_found(self):
        pcm = b"".join(tone(400, 1.0) + silence(2.0) for _ in range(3))
        self.assertEqual(feed(pcm), "ringback")

    def test_busy_is_found(self):
        pcm = b"".join(tone(400, 0.5) + silence(0.5) for _ in range(4))
        self.assertEqual(feed(pcm), "busy")

    def test_a_voice_or_noise_or_silence_is_not_a_tone(self):
        self.assertIsNone(feed(noise(6.0)))
        self.assertIsNone(feed(silence(6.0)))
        self.assertIsNone(feed(b"".join(tone(1000, 1.0) + silence(2.0) for _ in range(3))))   # 高さが違う
        self.assertIsNone(feed(tone(400, 1.0) + silence(2.0)))              # 1回だけでは決めない

    def test_a_quiet_line_with_a_little_hum_is_not_a_tone(self):
        self.assertEqual(tones.share(tone(400, 0.02, amp=50), RATE, 400), 0.0)   # 小さすぎる音で割合を出さない


if __name__ == "__main__":
    unittest.main()
