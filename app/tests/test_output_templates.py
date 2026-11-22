"""出力の型の置き場の試験（core/output_templates。表の作り 0280。CL-3 の連絡帳 4-1c の 66。設計書 7-3-5・7-3-5b）。"""

from __future__ import annotations

import unittest
import uuid

from salescall.core import backup, output_templates
from salescall.core.access import Rejected
from salescall.core.output_templates import TemplateRejected
from tests.support import OPERATOR, SYSTEM, World, owner_access

OK = lambda tx, business_id, body: []   # noqa: E731  確かめで問題の無い型（中身の確かめは CL-3 の持ち物＝ここでは呼ばれ方だけを見る）


class Templates(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()
        self.other_biz, _ = self.w.business_with_version(self.s["tenant"], self.s["client"])

    def tx(self, work, actor=OPERATOR, client=None):
        return self.w.a.in_client(actor, self.s["tenant"], client or self.s["client"], work)

    def body_of(self, business_id):
        got = self.tx(lambda t: output_templates.current(t, business_id))
        return None if got is None else got.body

    def test_the_business_template_replaces_the_client_template_whole_and_versions_only_add(self):
        """使う型は1か所（current）：業務のいまの版に中身があればそれ・無ければ依頼者の型・どちらも無ければ None（既定の型）。業務の型は依頼者の型を
        まるごと置き換える（混ぜない＝CL-3 の答え）。版は持ち主ごとに1から足すだけ。型を持たない形に戻す・前の版へ戻すも、新しい版として足す。"""
        biz, other = self.s["business"], self.other_biz
        self.assertEqual((self.body_of(biz), self.body_of(other)), (None, None))          # 型が無ければ既定の型
        client_v1 = self.tx(lambda t: output_templates.save(t, business_id=None, body={"columns": ["結果"], "encoding": "utf-8"},
                                                            reason="依頼者の指定の形", check=OK))
        self.assertEqual((client_v1.version_no, client_v1.business_id), (1, None))
        self.assertEqual((self.body_of(biz), self.body_of(other)), (client_v1.body, client_v1.body))   # 依頼者の型が、その業務の初めの値
        own = self.tx(lambda t: output_templates.save(t, business_id=biz, body={"columns": ["結果", "日時"]}, reason="この業務だけ", check=OK))
        self.assertEqual(own.version_no, 1)                                                # 版の番号は持ち主ごと
        self.assertEqual((self.body_of(biz), self.body_of(other)), ({"columns": ["結果", "日時"]}, client_v1.body))   # 混ぜない（encoding は入らない）
        client_v2 = self.tx(lambda t: output_templates.save(t, business_id=None, body={"columns": ["結果"], "encoding": "shift_jis"},
                                                            reason="文字コードの指定が変わった", check=OK))
        self.assertEqual((client_v2.version_no, self.body_of(biz), self.body_of(other)), (2, own.body, client_v2.body))
        self.tx(lambda t: output_templates.stop_using(t, business_id=biz, reason="依頼者の型に戻す"))
        self.assertEqual(self.body_of(biz), client_v2.body)                                # 業務が型を持たない＝依頼者の型に従う
        back = self.tx(lambda t: output_templates.restore(t, client_v1.template_id, reason="前の版へ戻す", check=OK))
        self.assertEqual((back.version_no, back.body, self.body_of(other)), (3, client_v1.body, client_v1.body))
        self.assertEqual([v.version_no for v in self.tx(lambda t: output_templates.history(t))], [3, 2, 1])        # 新しい順・消さない
        self.assertEqual([(v.version_no, v.body is None) for v in self.tx(lambda t: output_templates.history(t, biz))], [(2, True), (1, False)])
        self.tx(lambda t: output_templates.stop_using(t, business_id=None, reason="既定の型に戻す"))
        self.assertEqual((self.body_of(biz), self.body_of(other)), (None, None))
        for name, sql in (("書き換える", "UPDATE salescall.output_templates SET reason = '直した' WHERE template_id = %s"),
                          ("消す", "DELETE FROM salescall.output_templates WHERE template_id = %s")):
            # 版は書き換えない・消さない（アプリの役には、もとから書き換える権限が無い。表の持ち主の役で試し、守りの関数そのものを見張る）
            with self.subTest(name=name), self.assertRaises(Rejected):
                owner_access().in_system(SYSTEM, lambda t: t.execute(sql, (own.template_id,)))
        with self.assertRaises(LookupError):   # 無い業務を既定の型と取り違えない
            self.tx(lambda t: output_templates.current(t, uuid.uuid4()))

    def test_a_template_is_saved_only_after_the_check_passes_and_only_by_a_person_in_its_client(self):
        """登録は確かめ（CL-3 の check）を必ず通し、問題が1つでもあれば何も書かない。前の版へ戻すときも、いまの確かめで確かめ直す。書けるのは管理者か
        契約者（いま操作している方）だけ・その依頼者の業務だけ・中身は JSON の物だけ（データベースが守る）。"""
        biz = self.s["business"]
        seen = []
        def picky(tx, business_id, body):
            seen.append((business_id, body))
            return ["「-」と「✕」が同じ言葉になります"] if body.get("same") else []
        with self.assertRaises(TemplateRejected) as e:
            self.tx(lambda t: output_templates.save(t, business_id=biz, body={"same": True}, reason="試し", check=picky))
        self.assertEqual((e.exception.problems, seen), (("「-」と「✕」が同じ言葉になります",), [(biz, {"same": True})]))
        self.assertEqual(self.tx(lambda t: output_templates.history(t, biz)), [])               # 何も書いていない
        ok = self.tx(lambda t: output_templates.save(t, business_id=biz, body={"same": False}, reason="直した", check=picky))
        stricter = lambda tx, business_id, body: ["Shift_JIS で書けない字があります"]   # noqa: E731  あとで確かめの決まりが増えた
        with self.assertRaises(TemplateRejected):
            self.tx(lambda t: output_templates.restore(t, ok.template_id, reason="戻す", check=stricter))
        with self.assertRaises(TypeError):
            self.tx(lambda t: output_templates.save(t, business_id=biz, body=["列"], reason="試し", check=OK))
        other_client = self.w.client(self.s["tenant"])
        other_biz, _ = self.w.business_with_version(self.s["tenant"], other_client)
        for name, run in (
                ("システムの処理が登録する", lambda: self.tx(lambda t: output_templates.save(t, business_id=biz, body={}, reason="試し", check=OK),
                                                    SYSTEM)),
                ("ほかの依頼者の業務に登録する", lambda: self.tx(lambda t: output_templates.save(t, business_id=other_biz, body={}, reason="試し",
                                                                                         check=OK))),
                ("中身が JSON の物でない", lambda: self.tx(lambda t: t.execute(
                    "INSERT INTO salescall.output_templates (tenant_id, client_id, body, reason, made_by)"
                    " VALUES (salescall.cur_tenant(), salescall.cur_client(), '[1]'::jsonb, '試し', salescall.cur_actor())"))),
                ("理由が空", lambda: self.tx(lambda t: output_templates.save(t, business_id=biz, body={}, reason=" ", check=OK)))):
            with self.subTest(name=name), self.assertRaises(Rejected):
                run()
        with self.assertRaises(Rejected) as e:
            self.tx(lambda t: output_templates.stop_using(t, business_id=biz, reason="試し"), SYSTEM)
        self.assertEqual(e.exception.constraint, "output_template_by_person")
        with self.assertRaises(LookupError):   # ほかの依頼者の版は読めない（戻せない）
            self.tx(lambda t: output_templates.restore(t, ok.template_id, reason="戻す", check=OK), client=other_client)

    def test_a_submission_keeps_the_template_version_it_was_made_with(self):
        """提出用の控えに、どの型の版で作ったか（None＝既定の型）を残す。その依頼者の版だけを指せる・あとから変えない（0280 の (4)）。"""
        t = self.tx(lambda x: output_templates.save(x, business_id=self.s["business"], body={"columns": ["結果"]}, reason="指定の形", check=OK))
        sub = self.tx(lambda x: backup.record_submission(x, "結果.csv", "架空の結果".encode(), template_id=t.template_id), SYSTEM)
        plain = self.tx(lambda x: backup.record_submission(x, "既定.csv", "既定の形の結果".encode(), template_id=None), SYSTEM)
        got = self.tx(lambda x: {r["submission_id"]: r["template_id"] for r in x.all(
            "SELECT submission_id, template_id FROM salescall.submission_files")})
        self.assertEqual(got, {sub: t.template_id, plain: None})
        with self.assertRaises(Rejected):   # あとから変えない
            self.tx(lambda x: x.execute("UPDATE salescall.submission_files SET template_id = NULL WHERE submission_id = %s", (sub,)), SYSTEM)
        other_client = self.w.client(self.s["tenant"])
        with self.assertRaises(Rejected):   # ほかの依頼者の型の版は指せない
            self.tx(lambda x: backup.record_submission(x, "よそ.csv", "よその結果".encode(), template_id=t.template_id), SYSTEM, client=other_client)


if __name__ == "__main__":
    unittest.main()
