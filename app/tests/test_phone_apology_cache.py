"""おわびの録音の PC の上の置き場の試験（持ち主 CL-3。docs/PHONE.md 2-5）。作った音だけを使う（本物の録音・番号は使わない）。"""

from __future__ import annotations

import hashlib
import io
import math
import tempfile
import unittest
import wave
from pathlib import Path

from salescall.agent.phones import apology_cache, audio
from salescall.agent.phones.apology_cache import ApologyCache, Rejected


def wav(rate: int = 24000, seconds: float = 0.2, width: int = 2, channels: int = 1) -> bytes:
    n = int(rate * seconds)
    values = [0.3 * math.sin(2 * math.pi * 440 * i / rate) for i in range(n)]
    pcm = audio.pcm_of(values)
    if width != 2 or channels != 1:
        pcm = bytes(len(pcm) * channels * width // 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setsampwidth(width)
        w.setnchannels(channels)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Cache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = ApologyCache(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def put_both(self, voice: str = "S2") -> tuple[bytes, bytes]:
        ask, bye = wav(seconds=0.2), wav(seconds=0.3)
        self.cache.put(voice, "line_trouble_ask", ask, sha(ask))
        self.cache.put(voice, "line_trouble_bye", bye, sha(bye))
        return ask, bye

    def test_both_parts_are_read_in_the_device_shape(self):
        ask, bye = self.put_both()
        got = self.cache.recordings(48000)("S2")
        self.assertIsNotNone(got)
        self.assertEqual(len(got[0]), len(audio.resample(wave.open(io.BytesIO(ask)).readframes(4800), 24000, 48000)))   # 24kHz → 48kHz（1か所で変える）
        self.assertGreater(len(got[1]), len(got[0]))
        self.assertEqual(self.cache.held("S2"), {"line_trouble_ask": sha(ask), "line_trouble_bye": sha(bye)})

    def test_without_both_parts_there_is_no_recording(self):   # 片方だけ・無い声の型・声の型が分からない＝None（電話の部品はすぐ切る）
        ask = wav()
        self.cache.put("S2", "line_trouble_ask", ask, sha(ask))
        get = self.cache.recordings(48000)
        self.assertIsNone(get("S2"))
        self.assertIsNone(get("L4"))
        self.assertIsNone(get(None))

    def test_a_recording_whose_fingerprint_does_not_match_is_not_kept(self):
        ask = wav()
        with self.assertRaises(Rejected):
            self.cache.put("S2", "line_trouble_ask", ask, sha(ask + b"x"))
        self.assertEqual(self.cache.held("S2"), {})

    def test_names_outside_the_box_and_unknown_parts_are_refused(self):
        ask = wav()
        for voice, part in (("../x", "line_trouble_ask"), ("S2/..", "line_trouble_ask"), ("", "line_trouble_ask"), ("S2", "other")):
            with self.subTest(voice=voice, part=part), self.assertRaises(Rejected):
                self.cache.put(voice, part, ask, sha(ask))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])                 # 置き場の外にも中にも、何も書いていない

    def test_only_16bit_mono_wav_is_kept(self):
        for data in (wav(width=1), wav(channels=2), b"not a wav"):
            with self.subTest(size=len(data)), self.assertRaises(Rejected):
                self.cache.put("S2", "line_trouble_ask", data, sha(data))

    def test_a_new_recording_replaces_the_old_one(self):
        self.put_both()
        new = wav(seconds=0.5)
        self.cache.put("S2", "line_trouble_ask", new, sha(new))
        self.assertEqual(self.cache.held("S2")["line_trouble_ask"], sha(new))
        self.assertEqual([p.name for p in (Path(self.tmp.name) / "S2").iterdir() if p.suffix == ".tmp"], [])   # 一時のファイルは残らない

    def test_the_part_names_are_the_common_parts(self):   # 名前の正本は core の共通の一言（ずれたら、配った録音を読めない）
        from salescall.core import recordings
        self.assertLessEqual(set(apology_cache.PARTS), set(recordings.COMMON_PARTS))


if __name__ == "__main__":
    unittest.main()
