"""会話の担当 CL-1 がいま出している会話の報告（main の会話の試作 flowctl が作る報告。CL-1 の連絡帳 2-2・4-26・4-27）を、
受け口（core/call_report.py）が受け取り、判定（core/judge.py）がユーザーの決定どおりに読むかの試験。

場面の報告は、main の flowctl で通話を流して出た JSON をそのまま写したもの（架空のデータ。切れ方の材料 cut は電話接続の側が
足すので、判定の試験では足して渡す）。ユーザーの決定（2026-10-22。Intent.md・DESIGN.md 7-1・7-1e）：取り次いでもらえなかったとき、
営業拒否＝発信の拒否（もうかけない）／事情があり取り次げなかった（not_reached=not_transferred）＝システムが日を改めて1回／
約束した時刻のかけ直し＝約束の時刻。
"""

from __future__ import annotations

import copy
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from salescall.core import gate
from salescall.core.business_rules import check
from salescall.core.call_report import Cut, ReportInvalid, from_json
from salescall.core.judge import Facts, Item, JudgeInput, judge
from salescall.core.next_action import Target, decide_after_call
from tests.support import rules_raw

V = uuid.uuid4()
RULES = check(V, ("Q1", "Q2"), ("Q1", "Q2"), rules_raw(("Q1", "Q2")))
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=timezone.utc)      # 日本の 2030-01-07（月）19:00
TALKED = Facts("ended", None, True, False, NOW)
CLOSED = Cut("ai", True, False)                             # 締めの言葉に入っていた（ふつうに締めた）
P_NONE = gate.Pacing(3, NOW, None, False)

# ── main の flowctl が出した報告（場面ごと）
NOT_TRANSFERRED = {   # (1) 受付の方が、電話口の種別の報告なしに「取り次げない」と言った
    "version": 3, "facts": {
        "not_transferred": [{"turn": 1, "said": "担当が分からないのでお繋ぎできかねます。"}],
        "end": [{"turn": 1, "said": "担当が分からないのでお繋ぎできかねます。", "reason": "not_transferred"}]},
    "not_reached": "not_transferred", "cut": None, "apology_delivered": None}
ABSENT_CUT = {   # (2) 「席を外しております」（種別の報告なし）・かけ直しの質問の途中で切れた
    "version": 3, "facts": {
        "callback": [{"turn": 1, "said": "あいにく岡田は席を外しております。", "why": "absent", "read": ["ai", "judge", "judge2"],
                      "time": None, "number": None}]},
    "not_reached": "absent", "cut": None, "apology_delivered": None}
BACK_NOT_TARGET = {   # (3) かけ直しの質問の途中で「戻った」・確かめたら別の方だった・かけ直しの続きへ
    "version": 3, "facts": {
        "callback": [{"turn": 1, "said": "あいにく岡田は席を外しております。", "why": "absent", "read": ["ai", "judge", "judge2"],
                      "time": None, "number": None}],
        "party": [{"turn": 2, "said": "あ、ちょうど戻ってまいりました。少々お待ちください。", "party": "reception", "by": "ai"},
                  {"turn": 3, "said": "お電話代わりました。岡田です。", "party": "target", "by": "ai"}],
        "target_back": [{"turn": 3, "said": "お電話代わりました。岡田です。", "why": "absent"}],
        "callback_resume": [{"turn": 4, "said": "いえ、山田です。", "why": "absent", "read": ["judge", "judge2"]}]},
    "not_reached": "absent", "cut": None, "apology_delivered": None}
BACK_CONNECTED = {   # (4) かけ直しの質問の途中で戻ってきたご本人に繋いだ（そのかけ直しは要らない）
    "version": 3, "facts": {
        "callback": [{"turn": 1, "said": "あいにく岡田は席を外しております。", "why": "absent", "read": ["ai", "judge", "judge2"],
                      "time": None, "number": None, "connected": True}],
        "party": [{"turn": 2, "said": "あ、ちょうど戻ってまいりました。少々お待ちください。", "party": "reception", "by": "ai"},
                  {"turn": 3, "said": "お電話代わりました。岡田です。", "party": "target", "by": "ai"},
                  {"turn": 4, "said": "はい、そうです。", "party": "target", "by": "ai"}],
        "target_back": [{"turn": 3, "said": "お電話代わりました。岡田です。", "why": "absent"}],
        "connect": [{"turn": 4, "said": "はい、そうです。", "read": ["ai", "judge", "judge2"], "judging": True}]},
    "not_reached": None, "cut": None, "apology_delivered": None}
AI_SAID_TARGET = {   # (5) AIは「目的の人」と報告・判定は別の方（繋がない）・そのまま「取り次げない」
    "version": 3, "facts": {
        "party": [{"turn": 1, "said": "はい、営業部です。", "party": "target", "by": "ai"}],
        "not_transferred": [{"turn": 2, "said": "申し訳ございません、お繋ぎできかねます。"}],
        "end": [{"turn": 2, "said": "申し訳ございません、お繋ぎできかねます。", "reason": "not_transferred"}]},
    "not_reached": "not_transferred", "cut": None, "apology_delivered": None}
VOICEMAIL = {   # (6) 留守番電話（業務設定に伝言の文がある通話。CL-1 は事実 voicemail_message をまだ出さない）
    "version": 3, "facts": {
        "party": [{"turn": 1, "said": "ただいま電話に出ることができません。", "party": "voicemail", "by": "ai"}],
        "end": [{"turn": 1, "said": "ただいま電話に出ることができません。", "reason": "voicemail"}]},
    "not_reached": None, "cut": None, "apology_delivered": None}
ANSWERED = {   # (7) 受付の方の質問に「答えられない質問」の録音で答えた・そのあと「取り次げない」
    "version": 3, "facts": {
        "answered": [{"turn": 1, "said": "この番号はどちらでお知りになりましたか？", "part": "unknown"}],
        "not_transferred": [{"turn": 2, "said": "そうですか。担当が分からないのでお繋ぎできかねます。"}],
        "end": [{"turn": 2, "said": "そうですか。担当が分からないのでお繋ぎできかねます。", "reason": "not_transferred"}]},
    "not_reached": "not_transferred", "cut": None, "apology_delivered": None}
SCENES = {"取り次げない": NOT_TRANSFERRED, "不在・途中で切れた": ABSENT_CUT, "戻ったが別の方": BACK_NOT_TARGET,
          "戻ってご本人に繋いだ": BACK_CONNECTED, "AIは目的の人・判定は別の方": AI_SAID_TARGET, "留守番電話": VOICEMAIL,
          "答えられない質問に答えた": ANSWERED}


def run(raw, *, items=None, facts=TALKED, cut=CLOSED):
    rep = replace(from_json(raw), cut=cut)
    return judge(JudgeInput(facts, rep, items or {}, RULES, False, True))


class Accepted(unittest.TestCase):
    def test_every_report_from_the_conversation_is_accepted(self):
        """どの場面の報告も受け取り、置いて読み直しても同じ（save・load は from_json(to_json) を通る）。"""
        for name, raw in SCENES.items():
            with self.subTest(name):
                r = from_json(raw)
                self.assertEqual(from_json(r.to_json()), r)

    def test_what_the_report_says(self):
        """繋がったか・話したか・判定が読むかけ直しは、call_report の1か所で読む。新しい事実は受けて残す（判定には使わない）。"""
        cases = {   # 名前 → (目的のお客様のある通話と分かるか, 繋がった, 話した, 判定が読むかけ直しの理由)
            "取り次げない": (True, False, True, None),
            "不在・途中で切れた": (True, False, True, "absent"),
            "戻ったが別の方": (True, False, True, "absent"),
            "戻ってご本人に繋いだ": (True, True, True, None),           # 繋がったかけ直しは、もう要らない
            "AIは目的の人・判定は別の方": (True, False, True, None),   # 電話口の種別の target は AI の報告。繋いだ事実が無い
            "留守番電話": (False, False, False, None),
            "答えられない質問に答えた": (True, False, True, None)}
        for name, (for_target, reached, talked, cb) in cases.items():
            with self.subTest(name):
                r = from_json(SCENES[name])
                self.assertEqual((r.for_target, r.reached_target, r.talked, r.callback and r.callback.reason),
                                 (for_target, reached, talked, cb))
        r = from_json(BACK_NOT_TARGET)
        self.assertEqual(([b.reason for b in r.target_backs], [(b.reason, b.read) for b in r.callback_resumes]),
                         (["absent"], [("absent", ("judge", "judge2"))]))
        self.assertEqual((r.callbacks[0].read, r.callbacks[0].connected), (("ai", "judge", "judge2"), False))
        self.assertTrue(from_json(BACK_CONNECTED).callbacks[0].connected)
        r = from_json(ANSWERED)
        self.assertEqual(([a.part for a in r.answered], len(r.not_transferred), r.end_reason, r.hurried),
                         (["unknown"], 1, "not_transferred", False))
        # 目的のお客様の無い業務（誰でも）：繋いだ事実も理由も無い ⇒ 電話口の種別の目的のお客様で見る（前のまま）
        anyone = from_json({"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "party": [{"turn": 1, "said": "はい、私です。", "party": "target", "by": "ai"}]}})
        self.assertEqual((anyone.for_target, anyone.reached_target, anyone.talked), (False, True, True))
        # 目的のお客様の無い業務で、種別の報告なしに「あとでかけて」：かけ直しの申し出があれば話した
        later = from_json({"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "callback": [{"turn": 1, "said": "いま手が離せないので、あとでかけてください。", "why": "later", "read": ["ai"],
                          "time": None, "number": None}]}})
        self.assertEqual((later.for_target, later.reached_target, later.talked), (False, False, True))
        # 確かめに戻った方が「もうかけないで」（発信の拒否＝not_reached は無い）：確かめに戻った事実で、目的のお客様のある通話と分かる
        refused = copy.deepcopy(BACK_NOT_TARGET)
        refused["not_reached"] = None
        refused["facts"]["do_not_call"] = [{"turn": 5, "said": "もうかけてこないでください。"}]
        r = from_json(refused)
        self.assertEqual((r.for_target, r.reached_target, r.do_not_call), (True, False, True))


class Judged(unittest.TestCase):
    def test_could_not_be_put_through_means_one_more_call_on_another_day(self):
        """事情があり取り次げなかった ⇒ 日を改めて1回（システムの方針 not_reached_not_transferred）。受付の方の種別の報告が無くても、
        AIが「目的の人」と報告して繋がずに終わっても、答えられない質問に答えたあとでも同じ。"""
        tomorrow = NOW + timedelta(days=1, hours=2)
        for name in ("取り次げない", "AIは目的の人・判定は別の方", "答えられない質問に答えた"):
            with self.subTest(name):
                j = run(SCENES[name])
                self.assertEqual((j.state, j.detail["not_reached"], j.detail["policy_key"], j.dnc_reason),
                                 ("not_reached", "not_transferred", "not_reached_not_transferred", None))
                d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1, now=NOW,
                                      retry_at=tomorrow, target=Target(uuid.UUID(int=1)))
                self.assertEqual((d.kind, d.at), ("call", tomorrow))
                d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=2, now=NOW,
                                      retry_at=tomorrow, target=Target(uuid.UUID(int=1)))
                self.assertEqual((d.kind, d.end_reason), ("end", "policy_max:not_reached_not_transferred"))

    def test_absent_without_a_time_is_absent_and_a_said_time_is_kept(self):
        """不在のかけ直しの申し出：戻る時刻が取れなかった ⇒ 約束ではなく「不在」（再発信の方針 not_reached_absent＝かけ直しの回数の
        上限まで。2026-10-13 ユーザー：「『不在』のときは、かけ直しの回数の上限までかけ直す案です。⇒OK」）。「いつでも」など時刻を任せる
        言葉も同じ。時刻が取れた ⇒ 約束の時刻（「戻る時刻が聞けたときは『折り返し予定』…⇒はい」）。確かめに戻って別の方だった通話も、
        同じ（電話口の種別に target があっても繋がっていない）。"""
        for name in ("不在・途中で切れた", "戻ったが別の方"):
            with self.subTest(name):
                j = run(SCENES[name])
                self.assertEqual((j.state, j.detail.get("not_reached"), j.detail["policy_key"], j.detail.get("callback")),
                                 ("not_reached", "absent", "not_reached_absent", None))
        anytime = copy.deepcopy(ABSENT_CUT)
        anytime["facts"]["callback"][0]["time"] = "いつでも大丈夫です"
        self.assertEqual((run(anytime).state, run(anytime).detail["policy_key"]), ("not_reached", "not_reached_absent"))
        timed = copy.deepcopy(ABSENT_CUT)
        timed["facts"]["callback"][0]["time"] = "明日の午後3時"   # 受付の方が戻る時刻を言った（callback の time に入る。CL-1 の 4-27）
        j = run(timed)
        self.assertEqual((j.state, j.detail["callback"]["at"], j.detail["callback"]["exact"], j.detail["policy_key"]),
                         ("callback_scheduled", "2030-01-08T15:00:00+09:00", True, None))
        later = copy.deepcopy(ABSENT_CUT)   # お客様ご自身の「あとで」（時刻なし）は、これまでどおり時刻の決まっていない約束
        later["facts"]["callback"][0]["why"] = "later"
        self.assertEqual((run(later).state, run(later).detail["policy_key"]), ("callback_scheduled", "callback_unspecified_time"))

    def test_a_callback_that_reached_the_target_is_not_used(self):
        """かけ直しの質問の途中で目的のお客様に繋がった（connected）：そのかけ直しは使わず、繋がった通話として業務の質問で決める。"""
        self.assertEqual(run(BACK_CONNECTED).state, "incomplete")
        taken = {"Q1": Item("confirmed", None, None), "Q2": Item("accepted", "はい", None)}
        self.assertEqual(run(BACK_CONNECTED, items=taken).state, "completed")

    def test_a_silent_hang_up_at_the_reception_is_a_refusal(self):
        """種別の報告が無い受付の方の通話も「話した」なので、切れ方を見る（7-1c。種別があった通話と同じ）：AIが話している途中に
        黙って切った ⇒ 発信の拒否（原則14）。お客様の言葉の途中で切れた ⇒ 途中で切れた（戻る時刻が取れていないので不在のまま）。"""
        dropped = Facts("dropped", "callee", True, False, NOW)
        j = run(ABSENT_CUT, facts=dropped, cut=Cut("ai", False, False))
        self.assertEqual((j.state, j.dnc_reason, j.disconnect), ("do_not_call", "refused_in_call", "refused"))
        j = run(ABSENT_CUT, facts=dropped, cut=Cut("callee", False, False))
        self.assertEqual((j.state, j.disconnect), ("not_reached", "interrupted"))
        # 切れ方の材料が無い：話した通話なので、決めつけずに判定できない（直すのは電話接続の側＝CL-1）
        self.assertEqual(run(NOT_TRANSFERRED, cut=None).detail["unknown_cause"], "cut_missing")

    def test_voicemail_is_open(self):
        self.assertEqual(run(VOICEMAIL, cut=None).state, "open")


class Rejected(unittest.TestCase):
    def test_wrong_shapes_of_the_new_facts(self):
        rec = {"turn": 1, "said": "あいにく岡田は席を外しております。"}
        cb = {**rec, "why": "absent", "time": None, "number": None}

        def facts(**kinds):
            return {**ABSENT_CUT, "facts": {"callback": [cb], **kinds}}
        for name, raw in [
                ("かけ直しの知らない読み", facts(callback=[{**cb, "read": ["jev"]}])),
                ("かけ直しの読みが重なる", facts(callback=[{**cb, "read": ["ai", "ai"]}])),
                ("かけ直しの読みが並びでない", facts(callback=[{**cb, "read": "ai"}])),
                ("繋がったかが真偽でない", facts(callback=[{**cb, "connected": 1}])),
                ("繋がったのに繋いだ事実が無い", {**ABSENT_CUT, "not_reached": None,
                                                  "facts": {"callback": [{**cb, "connected": True}]}}),
                ("知らない確かめに戻った理由", facts(target_back=[{**rec, "why": "soon"}])),
                ("続きへ戻った読みが無い", facts(callback_resume=[{**rec, "why": "absent"}])),
                ("かけ直しが無いのに確かめに戻った", {**ABSENT_CUT, "facts": {"target_back": [{**rec, "why": "absent"}]}}),
                ("答えた部分が空", facts(answered=[{**rec, "part": " "}])),
                ("答えた部分が無い", facts(answered=[rec])),
                ("取り次げないの記録に知らない項目", facts(not_transferred=[{**rec, "why": "absent"}])),
                ("知らない繋がらなかった理由", {**NOT_TRANSFERRED, "not_reached": "busy"})]:
            with self.subTest(name), self.assertRaises(ReportInvalid):
                from_json(raw)
        self.assertEqual(from_json(facts(callback=[{**cb, "read": []}])).callbacks[0].read, ())   # 空＝プログラムが進めた


if __name__ == "__main__":
    unittest.main()
