"""承認待ちの受け箱の試験（持ち主 CL-3。web/inbox.py）。本物の門・通話の締めを通して承認待ちを作る（CL-2 の試験の土台を使う）。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from salescall.web import inbox
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import app_access
from tests.test_apology import ApologyBase


class ApologyInInbox(ApologyBase):
    def page(self, s):
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        return TestClient(create_app(app_access(), lambda r: person, b"t")).get("/t/inbox").text

    def test_an_apology_waits_in_the_inbox_and_cannot_be_approved_without_hearing_the_recording(self):
        """録音の音が置き場にそろっていなければ、承認のボタンを出さない（聞かずに承認できる形を作らない）。"""
        s = self.fresh(stage_cap=1)
        aid = self.incident(s)                               # 言ってはいけないことを言った通話（システムがお詫びの案を作る）
        order_id = self.draft(s, aid)["order_id"]
        self.recorded(s, order_id)                           # 案の録音の組（試験の参照＝音は置き場に無い）
        page = self.page(s)
        self.assertIn("お詫びの電話の承認", page)
        self.assertIn("承認するまで、このお客様へは、お約束の電話もかけません。", page)
        self.assertNotIn("/inbox/apology/approve", page)
        self.assertIn("お詫びの録音の音がまだそろっていません", page)

    def test_the_recording_is_heard_on_the_card_and_approval_needs_every_part_heard(self):
        """録音がそろえば、札で部分ごとに聞ける（音と聞いた記録は core の recording_audio の1か所＝CL-2 の 4-460）。承認は、押した方が
        すべての部分を聞いてから（2026-10-24「承認のときに録音を一緒に聞いていただく」。ボタンを隠すだけでなく、受ける側で確かめる）。"""
        import re
        from salescall.core import apology, recording_audio, recordings
        from tests.support import SYSTEM
        from tests.test_web_inbox_rows import form_of
        s = self.fresh(stage_cap=1)
        aid = self.incident(s)
        order_id = self.draft(s, aid)["order_id"]
        wav = lambda tag: b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + tag.ljust(32, b"\0")   # noqa: E731 架空の音（WAV の頭だけ）

        def work(tx):
            o = tx.one("SELECT o.text_same_day, o.text_later, t.voice_name FROM salescall.apology_orders o"
                       " JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id"
                       " JOIN salescall.voice_types t ON t.code = a.voice_type WHERE o.order_id = %s", (order_id,))
            same, later = recording_audio.put(tx, wav(b"same")), recording_audio.put(tx, wav(b"later"))
            parts = [recordings.Part(apology.PARTS[True], o["text_same_day"], same.audio_ref, same.audio_sha256),
                     recordings.Part(apology.PARTS[False], o["text_later"], later.audio_ref, later.audio_sha256)]
            return recordings.create_set(tx, unit="apology", voice_name=o["voice_name"], parts=parts, order_id=order_id)
        self.tx(s, work, SYSTEM)
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        http = TestClient(create_app(app_access(), lambda r: person, b"t"))
        page = http.get("/t/inbox").text
        act = f'/c/{s["client"]}/inbox/apology/approve'
        self.assertIn(act, page)
        heard = re.findall(r'<audio controls preload="none" src="([^"]+)"', page)
        self.assertEqual(len(heard), 2)
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        refused = http.post(act, data={**form_of(page, act), "form_token": token}, follow_redirects=False)
        self.assertIn("ok=0", refused.headers["location"])                     # 聞かずに押しても承認しない
        self.assertIn("お詫びの電話の承認", self.page(s))
        for url in heard:
            r = http.get(url)
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/wav"))
        self.assertEqual(http.get(f'/c/{s["client"]}/recordings/{aid}/same.wav').status_code, 404)   # 無い組は聞けない
        page = http.get("/t/inbox").text
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        done = http.post(act, data={**form_of(page, act), "form_token": token}, follow_redirects=False)
        self.assertIn("ok=1", done.headers["location"])
        self.assertNotIn("お詫びの電話の承認", self.page(s))

    def test_a_recording_is_approved_after_hearing_only_its_unapproved_parts(self):
        """録音の承認（計画書 6-2）：まだ承認していない部分だけを札で聞き、すべて聞いてから承認する（お詫びの電話と同じ部品）。"""
        import re
        from salescall.core import recording_audio, recordings
        from tests.support import OPERATOR, World
        from tests.test_gate import unique_role
        from tests.test_web_inbox_rows import form_of
        s = World().setup()
        role = unique_role()
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + role.encode("utf-8")[:32].ljust(32, b"\0")

        def work(tx):
            got = recording_audio.put(tx, wav)
            return recordings.add_name_part(tx, voice_name="Leda", key=recordings.NameKey(role=role),
                                            part=recordings.Part("name", f"{role}様", got.audio_ref, got.audio_sha256), business_id=s["business"])
        set_id = app_access().in_client(OPERATOR, s["tenant"], s["client"], work)
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        http = TestClient(create_app(app_access(), lambda r: person, b"t"))
        page = http.get("/t/inbox").text
        self.assertIn("録音の承認", page)
        self.assertIn(f"{role}様", page)                                       # 聞く録音の文
        act = f'/c/{s["client"]}/inbox/recording/approve'
        heard = re.findall(r'<audio controls preload="none" src="([^"]+)"', page)
        self.assertEqual(heard, [f'/c/{s["client"]}/recordings/{set_id}/name.wav'])
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        self.assertIn("ok=0", http.post(act, data={**form_of(page, act), "form_token": token}, follow_redirects=False).headers["location"])
        self.assertEqual(http.get(heard[0]).status_code, 200)
        page = http.get("/t/inbox").text
        token = re.search(r'<meta name="form-token" content="([^"]+)"', page).group(1)
        self.assertIn("ok=1", http.post(act, data={**form_of(page, act), "form_token": token}, follow_redirects=False).headers["location"])
        self.assertNotIn(f"{role}様", http.get("/t/inbox").text)

    def test_an_approved_apology_leaves_the_inbox(self):
        s = self.fresh(stage_cap=1)
        aid = self.incident(s)
        self.order(s, aid)                                   # 契約者が承認（CL-2 の流れ）
        self.assertNotIn("お詫びの電話の承認", self.page(s))


if __name__ == "__main__":
    unittest.main()
