"""業務で流す録音の音の置き場（core/recording_audio。表の作り 0275。✅ 2026-11-21 管理者「ア　データベースの中に置く」）の試験。音は架空（試験の中で作る無音の WAV）。"""

from __future__ import annotations

import hashlib
import struct
import unittest

from salescall.core import common_phrases, recording_audio, recordings
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, World


def wav(seed: int = 0, frames: int = 160) -> bytes:
    """架空の WAV（16bit・1チャンネル・16000Hz。中身は seed で変わる）。"""
    body = struct.pack(f"<{frames}h", *([seed] * frames))
    head = b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    return head + b"data" + struct.pack("<I", len(body)) + body


class RecordingAudio(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.other = self.w.setup()

    def system_tail(self, data: bytes):
        """システムの名前の録音の続く言葉を1つ、置き場の音で作る（承認の前＝ほかの試験の流す録音を変えない）。"""
        def work(tx):
            stored = recording_audio.put(tx, data)
            part = recordings.Part("target", common_phrases.NAME_TAILS["target"], stored.audio_ref, stored.audio_sha256)
            return stored, recordings.add_name_tail(tx, voice_name="Leda", tail="target", part=part)
        return self.w.a.in_system(ADMIN, work)

    def test_a_system_recording_is_stored_once_and_heard_by_any_contractor_with_a_record(self):
        """システムの録音の音は、どの契約者の取引からも聞ける（中身は置いたまま）。同じ音は1つだけ置く。聞いた記録が残る（だれが・どの部分）。"""
        data = wav(seed=1)
        stored, set_id = self.system_tail(data)
        self.assertEqual((stored.audio_sha256, stored.audio_ref), (hashlib.sha256(data).hexdigest(), "audio:" + stored.audio_sha256))
        again, _ = self.system_tail(data)
        self.assertEqual(again, stored)
        count = self.w.a.in_system(ADMIN, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.recording_audio WHERE audio_sha256 = %s",
                                                              (stored.audio_sha256,))["n"])
        self.assertEqual(count, 1)
        for owner in (self.s, self.other):
            heard = self.w.a.in_tenant(OPERATOR, owner["tenant"], lambda tx: recording_audio.listen(tx, set_id, "target"))
            self.assertEqual(heard, data)
        log = self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.all(
            "SELECT part, listened_by FROM salescall.recording_listens WHERE set_id = %s", (set_id,)))
        self.assertEqual([r["part"] for r in log], ["target"])                    # ほかの契約者の聞いた記録は見えない
        self.assertTrue(log[0]["listened_by"])

    def test_a_contractor_recording_is_not_readable_by_another_contractor(self):
        """契約者の取引で置いた音は、その契約者からだけ読める（ほかの契約者には「置き場に無い」）。システムの録音として置けるのは管理者のシステムの取引だけ。"""
        data = wav(seed=2)
        stored = self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: recording_audio.put(tx, data))
        got = self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: recording_audio.read(tx, stored.audio_sha256))
        self.assertEqual(got, data)
        with self.assertRaises(recording_audio.AudioMissing):
            self.w.a.in_tenant(OPERATOR, self.other["tenant"], lambda tx: recording_audio.read(tx, stored.audio_sha256))
        with self.assertRaises(Rejected):                                          # 契約者の取引から、システムの録音の音は置けない
            self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.execute(
                "INSERT INTO salescall.recording_audio (tenant_id, client_id, audio_sha256, audio) VALUES (NULL, NULL, %s, %s)",
                (hashlib.sha256(data).hexdigest(), data)))

    def test_a_wrong_fingerprint_or_a_missing_sound_is_never_played(self):
        """指紋が中身と違う音は置けない（データベースが守る）。置き場に無い音（置き場ができる前の試験の録音）は、聞かせない・流さない。
        WAV でない音は置かない。"""
        data = wav(seed=3)
        with self.assertRaises(Rejected) as e:
            self.w.a.in_system(ADMIN, lambda tx: tx.execute(
                "INSERT INTO salescall.recording_audio (audio_sha256, audio) VALUES (%s, %s)", ("0" * 64, data)))
        self.assertEqual(e.exception.constraint, "recording_audio_fingerprint")
        stored, _ = self.system_tail(data)
        self.assertEqual(self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: recording_audio.read(tx, stored.audio_sha256)), data)
        with self.assertRaises(recording_audio.AudioMissing):                     # 置き場に無い指紋
            self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: recording_audio.read(tx, "1" * 64))
        legacy = self.w.a.in_system(ADMIN, lambda tx: recordings.add_name_tail(
            tx, voice_name="Leda", tail="target",
            part=recordings.Part("target", common_phrases.NAME_TAILS["target"], "test://Leda/target", "0" * 64)))
        with self.assertRaises(recording_audio.AudioMissing):
            self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: recording_audio.listen(tx, legacy, "target"))
        none = self.w.a.in_tenant(OPERATOR, self.s["tenant"], lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.recording_listens WHERE set_id = %s", (legacy,))["n"])
        self.assertEqual(none, 0)                                                  # 聞けなかったものは、聞いた記録にしない
        for bad in (b"not a wav file at all, just some text bytes here.....", b"RIFF"):
            with self.subTest(bad=bad[:8]), self.assertRaises(recording_audio.AudioRefused):
                self.w.a.in_system(ADMIN, lambda tx: recording_audio.put(tx, bad))


if __name__ == "__main__":
    unittest.main()
