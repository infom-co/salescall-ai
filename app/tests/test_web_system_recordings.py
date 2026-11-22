"""システムの録音（共通の一言・標準の想定問答）の承認の試験（持ち主 CL-3。CL-2 4-194「共通の一言の録音を聞いて承認する画面は、管理者の画面
だけに置いてください」）。受け箱の種類 system_recording（全体の取引）。聞くのも承認も core の recording_audio・recordings の1か所で、
画面は聞かずに承認できる形を作らない。音は架空（作った WAV）。"""

from __future__ import annotations

import re
import unittest
from urllib.parse import unquote

from fastapi.testclient import TestClient

from salescall.core import common_phrases, recording_audio, recordings
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import ADMIN, World, app_access
from tests.test_recording_audio import wav

SECRET = b"test-only-secret"


class SystemRecordings(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.admin = TestClient(create_app(app_access(), lambda r: Person("試験の管理者", True, self.s["tenant"], "架空の契約者"), SECRET))
        self.operator = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, self.s["tenant"], "架空の契約者"), SECRET))
        seed = 37 + len(self._testMethodName)   # 試験ごとに違う音（同じ中身の承認は、どの組でも済んだことになるため）

        def make(tx):
            stored = recording_audio.put(tx, wav(seed=seed))
            return recordings.create_set(tx, unit="contractor", voice_name="Leda",   # 共通の一言の組は、部分の全部がそろう決まり
                                         parts=[recordings.Part(p, t, stored.audio_ref, stored.audio_sha256) for p, t in common_phrases.TEXTS.items()])
        self.set_id = self.w.a.in_system(ADMIN, make)
        self.parts = self.w.a.in_system(ADMIN, lambda tx: recordings.unapproved_parts(tx, self.set_id))
        self.part = self.parts[0]

    def token(self, who):
        return re.search(r'<meta name="form-token" content="([^"]+)"', who.get("/t").text).group(1)

    def approved(self):
        return self.w.a.in_system(ADMIN, lambda tx: tx.one(
            "SELECT 1 AS x FROM salescall.recording_approvals WHERE set_id = %s", (self.set_id,))) is not None

    def test_the_admin_listens_to_a_common_phrase_then_approves_it(self):
        page = self.admin.get("/t/inbox").text
        self.assertIn("システムの録音の承認", page)
        self.assertIn(f"/admin/recordings/{self.set_id}/{self.part}.wav", page)    # 聞く入口は管理者の全体の取引
        self.assertNotIn(f"/admin/recordings/{self.set_id}/", self.operator.get("/t/inbox").text)   # 契約者には出ない
        self.assertEqual(self.operator.get(f"/admin/recordings/{self.set_id}/{self.part}.wav").status_code, 404)
        form = {"form_token": self.token(self.admin), "set_id": str(self.set_id)}
        early = self.admin.post("/admin/inbox/system_recording/approve", data=form, follow_redirects=False)
        self.assertIn("ok=0", early.headers["location"])
        self.assertIn("聞いてから承認", unquote(early.headers["location"]))       # 聞く前は承認できない
        self.assertFalse(self.approved())
        heard = self.admin.get(f"/admin/recordings/{self.set_id}/{self.part}.wav")
        self.assertEqual((heard.status_code, heard.headers["content-type"]), (200, "audio/wav"))
        for p in self.parts[1:]:                                                 # まだ承認していない部分を、すべて聞く
            self.assertEqual(self.admin.get(f"/admin/recordings/{self.set_id}/{p}.wav").status_code, 200)
        done = self.admin.post("/admin/inbox/system_recording/approve", data={**form, "form_token": self.token(self.admin)},
                               follow_redirects=False)
        self.assertIn("ok=1", done.headers["location"])
        self.assertTrue(self.approved())
        self.assertNotIn(f"/admin/recordings/{self.set_id}/", self.admin.get("/t/inbox").text)   # 決めたら受け箱から消える

    def test_a_contractor_cannot_approve_a_system_recording(self):
        r = self.operator.post("/admin/inbox/system_recording/approve", data={"form_token": self.token(self.operator), "set_id": str(self.set_id)},
                               follow_redirects=False)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(self.approved())


if __name__ == "__main__":
    unittest.main()
