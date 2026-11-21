"""名前の録音の審査の画面の試験（持ち主 CL-3。docs/DASHBOARD.md 5-15・83 のあと。✅ 2026-11-04 管理者「まとめて流して、私がレ点を入れたものを
のみ登録、もしくは除外を入れたもののみ、登録されないという登録方法に」・「どの契約者・どの業務のものかは出るように」）。
一覧・聞く・決める・チェック不要は core の recordings / recording_audio の1か所。画面は、聞かずに決める形を作らない。音は架空（無音の WAV）。"""

from __future__ import annotations

import re
import unittest
import uuid

from fastapi.testclient import TestClient

from salescall.core import recording_audio, recordings
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import ADMIN, OPERATOR, World, app_access
from tests.test_recording_audio import wav

SECRET = b"test-only-secret"


class NameReviews(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.admin = TestClient(create_app(app_access(), lambda r: Person("試験の管理者", True, self.s["tenant"], "架空の審査の契約者"), SECRET))
        self.role = f"審{uuid.uuid4().hex[:6]}"
        data = wav(seed=21)

        def make(tx):
            stored = recording_audio.put(tx, data)
            set_id = recordings.add_name_part(tx, voice_name="Leda", key=recordings.NameKey(role=self.role),
                                              part=recordings.Part("name", f"{self.role}様", stored.audio_ref, stored.audio_sha256),
                                              business_id=self.s["business"])
            recordings.approve(tx, set_id)
            return set_id
        self.set_id = self.w.a.in_client(OPERATOR, self.s["tenant"], self.s["client"], make)
        self.data = data
        self.tenant_name = next(i.tenant_name for i in self.w.a.in_system(ADMIN, recordings.name_reviews) if i.set_id == self.set_id)

    def token(self):
        return re.search(r'<meta name="form-token" content="([^"]+)"', self.admin.get("/t").text).group(1)

    def section(self, page):
        """その契約者の札（同じ名前の契約者が並びうるので、契約者の番号で見分ける）。"""
        return next(x for x in page.split('<section class="card review-group">') if f'form="skip-{self.s["tenant"]}"' in x)

    def decide(self, marks="admit", marked=True):
        form = {"form_token": self.token(), "marks": marks, "shown": [str(self.set_id)]}
        if marked:
            form["marked"] = [str(self.set_id)]
        r = self.admin.post("/admin/name-reviews", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return self.admin.get(r.headers["location"]).text

    def test_the_admin_hears_all_and_then_decides_with_marks_in_one_go(self):
        page = self.admin.get("/admin/name-reviews").text
        self.assertIn("名前の録音の審査", page)
        mine = self.section(page)
        self.assertIn(self.tenant_name, mine)
        self.assertIn(self.role, mine)                                               # どの契約者・どの業務のものかが分かる
        self.assertIn(f'/admin/name-reviews/{self.set_id}/name.wav', mine)
        self.assertIn('data-needs-heard disabled', page)                             # 聞くまで「決める」は押せない形
        refused = self.decide()
        self.assertIn("まだ聞いていない録音", refused)                               # 送られても、サーバーが聞いたかを確かめて断る
        heard = self.admin.get(f"/admin/name-reviews/{self.set_id}/name.wav")
        self.assertEqual((heard.status_code, heard.content), (200, self.data))      # 聞く（core の review_listen が記録を残す）
        after = self.admin.get("/admin/name-reviews").text
        row = after[after.index(str(self.set_id)) - 400:]
        self.assertIn('data-heard="1"', row[:600])
        done = self.decide(marks="admit")
        self.assertIn("1件を決めました：システムの共通の録音に入れた 1件", done)
        self.assertNotIn(f'/admin/name-reviews/{self.set_id}/name.wav', self.admin.get("/admin/name-reviews").text)
        self.assertEqual(self.admin.get(f"/admin/name-reviews/{self.set_id}/name.wav").status_code, 404)   # 決めたあとは審査で聞けない

    def test_the_inbox_shows_the_reviews_as_one_bundle_for_the_admin(self):   # 設計書 4章の (1)：1件ずつのカードにしない
        inbox = self.admin.get("/t/inbox").text
        self.assertIn("名前の録音の審査", inbox)
        self.assertIn('href="/admin/name-reviews">まとめて聞いて決める</a>', inbox)
        self.assertNotIn(str(self.set_id), inbox)                                    # 束で出す（組ごとのカードは出さない）

    def test_marks_can_mean_exclude(self):
        self.admin.get(f"/admin/name-reviews/{self.set_id}/name.wav")
        done = self.decide(marks="exclude")
        self.assertIn("入れた 0件・入れなかった 1件", done)

    def test_a_contractor_cannot_open_the_review(self):
        operator = TestClient(create_app(app_access(), lambda r: Person("試験の契約者", False, self.s["tenant"], "架空の審査の契約者"), SECRET))
        self.assertEqual(operator.get("/admin/name-reviews").status_code, 404)
        self.assertEqual(operator.get(f"/admin/name-reviews/{self.set_id}/name.wav").status_code, 404)
        self.assertNotIn("名前の録音の審査", operator.get("/t").text)                # 道しるべにも出さない

    def test_a_contractor_can_be_trusted_and_untrusted_with_a_confirmation(self):
        page = self.admin.get("/admin/name-reviews").text
        form = page[page.index(f'id="skip-{self.s["tenant"]}"'):]
        self.assertIn("data-confirm=", form[:600])                                   # 名前つきの確かめの窓を通る
        r = self.admin.post("/admin/name-reviews/skip", data={"form_token": self.token(), "tenant_id": str(self.s["tenant"]), "skip": "1"},
                            follow_redirects=False)
        self.assertIn("管理者のチェックなしで入ります", self.admin.get(r.headers["location"]).text)
        page = self.admin.get("/admin/name-reviews").text
        self.assertIn("チェック不要の契約者", self.section(page))


if __name__ == "__main__":
    unittest.main()
