"""承認待ちの、行ごとの種類の試験（持ち主 CL-3。web/inbox.py）。状態は CL-2 の試験の土台で、本物の流れから作る（架空のデータだけ）。

前の担当の方との約束（old_promise）：選びを待つ行が受け箱に出る・「守る」を出すかは core の1か所（old_promise_keepable）に従う・
画面のボタンで選ぶと受け箱から消え、行の予定が core の決めたとおりになる。
1か月以上先のかけ直し（month_ahead）：伺っている行が受け箱に出る（お電話する日時つき）・画面のボタンで答えると消え、もう答えられない。
直しでかける電話（correction）：確かめ待ちの行が受け箱と1行の画面に出る・1行の画面から確かめると、その画面へ戻り、確かめ待ちが無くなる（CL-2 4-422）。
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from salescall.core import attempts, call_report, calling_hours, health, tasks
from salescall.web.app import create_app
from salescall.web.context import Person
from tests.support import CLEAN, OPERATOR, app_access, callback
from tests.test_gate import FakeDriver, dial_exit
from tests.test_settle import Corrections, DialFixAfterCalls, rep
from tests.test_unwelcome import MonthAhead
from tests.test_approval import Helpers as ApprovalHelpers, TAKEN
from tests.test_apology import MoreCalls

SECRET = b"test-only-secret"


def form_of(page: str, action: str) -> dict[str, str]:
    """画面の中の、その送り先のフォームが送る隠れた値（ブラウザが送るものと同じ。受け箱の鍵・二度押しの札・戻る先）。"""
    m = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page, re.S)
    assert m is not None, action
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', m.group(1)))


class OldPromise(DialFixAfterCalls):
    def _waiting_row(self):
        """名前を「違う」で直した通話で、前の担当の方が新しい約束をした行（CL-2 の試験と同じ作り方）。"""
        from tests.support import SYSTEM
        s = self.ready()
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.person_fix(s, "target_name", "田中", "たなか", same=True)
        c = self.claim(s)
        self.assertTrue(dial_exit.dial(self.w.a, SYSTEM, s["tenant"], s["client"], c.attempt_id, FakeDriver()))
        for st in ("answered", "in_conversation"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.person_fix(s, "target_name", "鈴木", "すずき", same=False)
        for st in ("ending", "ended"):
            self.tx(s, lambda tx: attempts.move(tx, c.attempt_id, st))
        self.tx(s, lambda tx: call_report.save(tx, c.attempt_id, rep(callback=callback("later", time="あとで"))))
        self.tx(s, lambda tx: attempts.record_verification(tx, c.attempt_id, summary=CLEAN))
        self.tx(s, lambda tx: health.after_call(tx, c.attempt_id))
        self.settle(s, c.attempt_id)
        self.assertEqual(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])), c.attempt_id)
        return s

    def _http(self, s):
        person = Person("試験の契約者", False, s["tenant"], "架空の契約者")
        return TestClient(create_app(app_access(), lambda r: person, SECRET))

    def test_a_row_waiting_for_the_choice_is_in_the_inbox_and_the_choice_takes_it_out(self):
        s = self._waiting_row()
        http = self._http(s)
        page = http.get("/t/inbox").text
        self.assertIn("前の担当の方との約束", page)
        self.assertIn(f'/c/{s["client"]}/inbox/old_promise/keep', page)        # 守れる約束なので「守る」も出る（core の1か所の答え）
        self.assertIn(f'/c/{s["client"]}/inbox/old_promise/new_person', page)
        sent = form_of(page, f'/c/{s["client"]}/inbox/old_promise/new_person')
        r = http.post(f'/c/{s["client"]}/inbox/old_promise/new_person', data=sent, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tasks.old_promise_waiting(tx, s["task"])))
        self.assertEqual(self.plan(s), self.handed_over(s["contact"]))         # 予定は core の決めたとおり
        self.assertNotIn("前の担当の方との約束", http.get("/t/inbox").text)
        again = http.post(f'/c/{s["client"]}/inbox/old_promise/keep', data=sent, follow_redirects=False)   # もう選んだ行（古い画面から押した）
        self.assertIn("ok=0", again.headers["location"])                       # 断りの文が出る（500 にならない・何も書かない）




def _http(tenant):
    person = Person("試験の契約者", False, tenant, "架空の契約者")
    return TestClient(create_app(app_access(), lambda r: person, SECRET))


class MonthAheadAnswer(MonthAhead):
    def test_a_row_asked_about_is_in_the_inbox_and_the_answer_takes_it_out(self):
        s = self.ready()
        self.choose(s, "with_approval")
        words, first, last, repeat = self.monthly_rounds()[0]
        w, d, p = self.passed(s, first(s), last(s), repeat)
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])), p["call_at"])
        http = _http(s["tenant"])
        page = http.get("/t/inbox").text
        self.assertIn("1か月以上先のかけ直し", page)
        self.assertIn(calling_hours.label(p["call_at"]), page)                 # お電話する日時（書き方は core の1か所）
        sent = form_of(page, f'/c/{s["client"]}/inbox/month_ahead/approve')
        moved = http.post(f'/c/{s["client"]}/inbox/month_ahead/approve', data={**sent, "call_at": "2030-01-01T00:00:00+00:00"},
                          follow_redirects=False)                              # 見せた日時と違う日時では決められない（中身が変わった）
        self.assertIn("ok=0", moved.headers["location"])
        self.assertEqual(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])), p["call_at"])
        r = http.post(f'/c/{s["client"]}/inbox/month_ahead/approve', data=sent, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tasks.month_ahead_answerable(tx, s["task"])))
        self.assertNotIn("1か月以上先のかけ直し", http.get("/t/inbox").text)
        again = http.post(f'/c/{s["client"]}/inbox/month_ahead/refuse', data=sent, follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                       # もう答えた（古い画面）＝断りの文・何も書かない


class CorrectionCall(Corrections):
    def test_a_correction_call_waits_in_the_inbox_and_on_the_row_until_confirmed(self):
        from salescall.core import next_action
        from tests.test_judge import goal_refused
        s = self.ready()
        got = self.settle(s, self.call(s, report=goal_refused(rep(), goals=("Q1",)), items=(("Q1", "accepted", "いいえ"),)))
        self.put_client(s, "gate.same_contact_min_interval", 1)
        self.correct(s, got["version_no"], "open")                              # 「断られた」を「未決」に直した＝かける（CL-2 の試験と同じ）
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：予定をいまに"))
        http = _http(s["tenant"])
        self.assertIn("直しでかける電話の確かめ", http.get("/t/inbox").text)
        someone = TestClient(create_app(app_access(), lambda r: Person("同じ契約者の別の方", False, s["tenant"], "架空の契約者"), SECRET))
        theirs = someone.get("/t/inbox").text                                   # 直した方でなければ、ボタンは出ず、だれを待つかを出す
        self.assertNotIn(f'/c/{s["client"]}/inbox/correction/confirm', theirs)
        self.assertIn("直した方（試験の契約者）ご本人の確かめを待っています", theirs)
        row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
        page = http.get(row).text
        self.assertIn("直した内容で、このお客様にお電話してよいか", page)      # 受け箱と同じ部品で、この行の承認待ちを出す
        sent = form_of(page, f'/c/{s["client"]}/inbox/correction/confirm')
        self.assertEqual(sent["back"], row)
        r = http.post(f'/c/{s["client"]}/inbox/correction/confirm', data=sent, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith(row + "?"))            # 1行の画面へ戻る
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: tx.one("SELECT salescall.task_correction_unconfirmed(%s) AS v", (s["task"],))["v"]))
        self.assertNotIn("直しでかける電話の確かめ", http.get("/t/inbox").text)
        outside = http.post(f'/c/{s["client"]}/inbox/correction/confirm', data={**sent, "back": "https://example.invalid/"},
                            follow_redirects=False)
        self.assertTrue(outside.headers["location"].startswith("/t/inbox"))      # 依頼者の中の住所でなければ戻さない（ok=0：もう確かめた）
        self.assertIn("ok=0", outside.headers["location"])


class MoreCallsButton(MoreCalls):
    """「さらにかける」の承認（CL-2 4-427。✅ 2026-11-19 管理者「ダッシュボードの取り込んだ行に承認ボタンを出します」）：受け箱の種類 more_calls の1か所。
    お客様リストの行・1行の画面・受け箱に同じ鍵・同じ受け口で出る。押すとお客様リストへ戻り、その行だけ、もう一度かけ始める。古い画面からは押せない。"""

    def test_the_row_in_the_list_has_the_button_and_pressing_it_starts_the_row_again(self):
        from salescall.core import next_action
        from salescall.web import inbox
        from tests.support import callback
        s = self.fresh(stage_cap=20)
        day = self.tx(s, lambda tx: tx.one("SELECT (salescall.jst(now()) + interval '1 day')::date AS d")["d"])
        self.settle(s, self.call(s, report=rep(callback=callback("later", time=f"{day.month}月{day.day}日の15時"))))
        promise = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        http = _http(s["tenant"])
        rows = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}'
        act = f'/c/{s["client"]}/inbox/more_calls/approve'
        self.assertNotIn(act, http.get(rows).text)                              # かけ直す予定のあいだは出さない
        self.tx(s, lambda tx: next_action.end(tx, s["task"], "callback_exact_unreached", cause="試験：決めた日数・回数かけても話せなかった"))
        page = http.get(rows).text
        self.assertIn("1 行が「さらにかける」の承認を待っています", page)
        self.assertIn(f'form="more-{s["task"]}"', page)                         # 行の中のボタンは、表の外の送り先を指す（form を入れ子にしない）
        part = page[page.index('<details class="adder more-act">'):]
        self.assertIn("に、この行だけ、もう一度おかけします", part[:800])        # 押す前に、どのお客様かを名前つきで見せる（計画書 5-1）
        self.assertRegex(page, r'class="tag wait"[^>]*>' + inbox.MORE_CALLS_TITLE + "：")   # 終えた行でも緑の「済み」にせず、待ちの色（色の決まり）
        sent = form_of(page, act)
        self.assertEqual(sent["back"], rows)
        self.assertIn(inbox.MORE_CALLS_TITLE, http.get("/t/inbox").text)       # 受け箱にも同じ承認待ち（ホームの数もここから）
        row = http.get(f"{rows}/rows/{s['task']}").text
        self.assertIn(f'action="{act}"', row)                                   # 1行の画面にも同じ部品で出る
        notices = http.get("/t/notices").text                                   # 知らせから、その行へ飛べる（links の1か所）
        self.assertIn(f"{rows}/rows/{s['task']}", notices[notices.index("話せずに終えた行"):])
        r = http.post(act, data=sent, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith(rows + "?"))           # お客様リストへ戻る
        self.assertIn("ok=1", r.headers["location"])
        p = self.tx(s, lambda tx: next_action.current(tx, s["task"]))
        self.assertEqual((p["kind"], p["promised"], p["callee_at"]), ("call", True, promise["callee_at"]))   # 約束の時刻のまま（core の決めたとおり）
        after = http.get(rows).text
        self.assertNotIn(act, after)
        self.assertNotIn("「さらにかける」の承認を待っています", after)
        again = http.post(act, data=sent, follow_redirects=False)               # 古い画面からもう1回押しても
        self.assertIn("ok=0", again.headers["location"])                        # 断りの文・何も書かない
        self.tx(s, lambda tx: next_action.end(tx, s["task"], "callback_exact_unreached", cause="試験：また話せなかった"))
        fresh = form_of(http.get(rows).text, act)                               # また話せなければ、またボタン（新しい終わりの鍵）
        self.assertNotEqual(fresh["end_seq"], sent["end_seq"])
        stale = http.post(act, data=sent, follow_redirects=False)               # 前の終わりの鍵では押せない（見せた中身と決める中身を同じに）
        self.assertIn("ok=0", stale.headers["location"])
        self.assertEqual(self.tx(s, lambda tx: next_action.current(tx, s["task"]))["kind"], "end")


class VerificationMode(ApprovalHelpers):
    """検証モードの2つの承認（発信前の行の承認・結果の確かめ）と、結果を直す画面（CL-2 の承認の試験と同じ作り方）。"""

    def _post(self, http, path, data):
        token = re.search(r'<meta name="form-token" content="([^"]+)"', http.get("/t").text).group(1)
        return http.post(path, data={"form_token": token, **data}, follow_redirects=False)

    def test_rows_wait_for_approval_in_one_item_per_run_and_are_approved_together(self):
        from salescall.core import approval
        s = self.fresh(stage_cap=100)
        self.put_client(s, "approval.human", True)
        http = _http(s["tenant"])
        biz = http.get(f'/c/{s["client"]}/b/{s["business"]}').text                     # 業務のタブ：検証モードと品質の段（core の答え）
        self.assertIn("検証モード", biz)
        self.assertIn("言ってはいけないことを言った", biz)
        page = http.get("/t/inbox").text
        self.assertIn("発信前の行の承認（検証モード）", page)
        self.assertIn(f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}"', page)     # 決める前に見る（お客様リスト）
        sent = form_of(page, f'/c/{s["client"]}/inbox/task_approval/approve_all')
        self.assertIn("ok=0", self._post(http, f'/c/{s["client"]}/inbox/task_approval/approve_all',
                                         {"run_id": str(s["run"])}).headers["location"])   # 見せた行の印（digest）が無ければ決めない
        r = http.post(f'/c/{s["client"]}/inbox/task_approval/approve_all', data=sent, follow_redirects=False)
        self.assertIn("ok=1", r.headers["location"])
        self.assertIsNone(self.tx(s, lambda tx: approval.uncleared(tx, s["task"])))
        self.assertNotIn("発信前の行の承認（検証モード）", http.get("/t/inbox").text)
        again = http.post(f'/c/{s["client"]}/inbox/task_approval/approve_all', data=sent, follow_redirects=False)
        self.assertIn("ok=0", again.headers["location"])                         # もう待っている行は無い（何も書かない）

    def test_a_result_waits_for_the_check_and_can_be_approved_or_corrected(self):
        from salescall.core import approval, quality_stage
        from salescall.core.attempts import VerificationSummary
        for how in ("approve", "correct"):
            with self.subTest(how=how):
                s = self.fresh(stage_cap=100)
                self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, None, evidence="試験"), OPERATOR)
                self.put_client(s, "approval.human", True)
                self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
                self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
                http = _http(s["tenant"])
                self.assertIn("結果の確かめ（検証モード）", http.get("/t/inbox").text)
                if how == "approve":
                    r = self._post(http, f'/c/{s["client"]}/inbox/result_check/approve', {"task_id": str(s["task"]), "seq": "1"})
                else:
                    row = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}'
                    self.assertIn("結果を直す", http.get(row).text)
                    self.assertIn("ok=0", self._post(http, row + "/result", {"version": "1", "state": "incomplete", "reason": ""}).headers["location"])
                    r = self._post(http, row + "/result", {"version": "1", "state": "incomplete", "reason": "試験：録音を聞き直した"})
                self.assertIn("ok=1", r.headers["location"])
                self.assertEqual(len(self.releases(s)), 1)                          # 確定した（1つだけ）
                self.assertNotIn("結果の確かめ（検証モード）", http.get("/t/inbox").text)


    def test_the_result_file_waits_for_the_check_and_is_kept_once(self):
        from salescall.core import approval, quality_stage
        from salescall.core.attempts import VerificationSummary
        s = self.fresh(stage_cap=100)
        self.tx(s, lambda tx: quality_stage.open_stage(tx, s["version"], 2, None, evidence="試験"), OPERATOR)
        self.put_client(s, "approval.human", True)
        self.tx(s, lambda tx: approval.approve_task(tx, s["task"]), OPERATOR)
        self.verified_call(s, VerificationSummary(2, 0, 0), items=TAKEN)
        http = _http(s["tenant"])
        csv_url = f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/results.csv'
        kept = lambda: self.tx(s, lambda tx: tx.one("SELECT count(*) AS n FROM salescall.submission_files")["n"])
        lines = http.get(csv_url).content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines), 1)                                          # 確かめ待ちの行は出さない（見出しだけ）
        self.assertIn("ok=0", self._post(http, csv_url, {}).headers["location"])  # 渡せる結果が無い＝渡さない
        biz = http.get(f'/c/{s["client"]}/b/{s["business"]}').text                 # 業務のタブの数（counts.business_figures の1か所）
        self.assertRegex(biz, r"<b class=\"num\">1</b><span>確かめ待ち</span>")   # 確かめ待ち＝受け箱の「結果の確かめ」と同じ式
        self.assertEqual(kept(), 0)
        # 「この結果でよい」を押す前に、何が依頼者へ渡るか（答えの欄）を、渡す書き方のまま見られる（2026-11-20 の計画書の読み直し）
        from salescall.output import result_rows as _rr
        check = self.tx(s, lambda tx: _rr.answers_for_check(tx, s["task"]))
        self.assertTrue(check)
        inbox_page = http.get("/t/inbox").text
        row_page = http.get(f'/c/{s["client"]}/b/{s["business"]}/runs/{s["run"]}/rows/{s["task"]}').text
        self.assertIn("答え（依頼者へ渡す書き方のまま）", row_page)
        # 「よい」と「違う（直す）」は同じ重さで並ぶ（よいだけを目立たせない）。違うは、行の画面の「結果を直す」の窓へ移る（決めずに移るだけ）
        self.assertNotIn('class="btn main" type="submit">この結果でよい', inbox_page)
        self.assertIn(f'/rows/{s["task"]}#result-fix">違う（直す）</a>', inbox_page)
        self.assertIn('id="result-fix"', row_page)
        self.assertIn("（空欄）", inbox_page)                                     # この試験の土台は値を入れないので、答えの欄は空＝空と見せる
        self.assertEqual(self._post(http, f'/c/{s["client"]}/inbox/result_check/fix',
                                    {"task_id": str(s["task"]), "seq": "1"}).status_code, 404)   # 移るだけのボタンは送れない（決める鍵が無い）
        for q, _a, _said in check:
            self.assertIn(f"答え：{q}", inbox_page)
            self.assertIn(q, row_page)
        self.assertIn("ok=1", self._post(http, f'/c/{s["client"]}/inbox/result_check/approve',
                                         {"task_id": str(s["task"]), "seq": "1"}).headers["location"])
        r = self._post(http, csv_url, {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content.decode("utf-8-sig").strip().splitlines()), 2)   # 確定した行が出る
        import csv as _csv
        import io as _io
        from salescall.output import result_rows
        head, row = list(_csv.reader(_io.StringIO(r.content.decode("utf-8-sig"))))
        asked = [i for i, h in enumerate(head) if h.endswith(result_rows.WORDS_SUFFIX)]
        self.assertTrue(asked)                                                    # 質問ごとの2列（答え／お客様の言葉）が出る
        self.assertEqual([(head[i - 1], row[i - 1], row[i]) for i in asked], check)   # 確かめた答えと、渡したファイルの答えは同じ1か所
        for i in asked:
            self.assertNotIn(row[i - 1], ("-", "✕"))                              # 答えの欄は値か、お客様の言葉か、状態の短い言葉（「-」「✕」ではない。
            #                                                                          この試験の土台は「取れた」の印だけで値を入れないので、値の欄は空）
        self.assertEqual(kept(), 1)
        self.assertRegex(http.get(f"/c/{s['client']}/b/{s['business']}").text, r"<b class=\"num\">0</b><span>確かめ待ち</span>")   # 確かめたので0
        self.assertEqual(self._post(http, csv_url, {}).status_code, 200)          # 同じ中身をもう1回渡しても
        self.assertEqual(kept(), 1)                                               # 控えは重ねない


def load_tests(loader, tests, pattern):
    """この試験だけを読む（受け継いだ CL-2 の組の試験を、ここで流し直さない）。"""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{__name__}.{c.__name__}.{n}" for c in (OldPromise, MonthAheadAnswer, CorrectionCall, MoreCallsButton, VerificationMode) for n in c.__dict__ if n.startswith("test_")]))


if __name__ == "__main__":
    unittest.main()
