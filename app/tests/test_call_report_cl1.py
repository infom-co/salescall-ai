"""会話の担当 CL-1 がいま出している会話の報告（main の会話の試作 flowctl が作る報告。CL-1 の連絡帳 2-2・4-26・4-27）を、
受け口（core/call_report.py）が受け取り、判定（core/judge.py）がユーザーの決定どおりに読むかの試験。

場面の報告は、main の flowctl で通話を流して出た JSON をそのまま写したもの（架空のデータ。切れ方の材料 cut は CL-1 が
出す＝2026-10-24 ユーザー決定・CL-2 の連絡帳 4-151。いまの main の報告にはまだ無いので、判定の試験では足して渡す）。ユーザーの決定（2026-10-22。Intent.md・DESIGN.md 7-1・7-1e）：取り次いでもらえなかったとき、
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
from tests.support import CALLEE, rules_raw

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
    return judge(JudgeInput(facts, rep, items or {}, RULES, False, True, callee=CALLEE, anyone_target=False))


class Accepted(unittest.TestCase):
    def test_every_report_from_the_conversation_is_accepted(self):
        """どの場面の報告も受け取り、置いて読み直しても同じ（save・load は from_json(to_json) を通る）。"""
        for name, raw in SCENES.items():
            with self.subTest(name):
                r = from_json(raw)
                self.assertEqual(from_json(r.to_json()), r)

    def test_what_the_report_says(self):
        """繋がったか・話したか・判定が読むかけ直しは、call_report の1か所で読む。新しい事実は受けて残す（判定には使わない）。"""
        cases = {   # 名前 → (目的のお客様のいる業務で繋がった, 話した, 判定が読むかけ直しの理由)
            "取り次げない": (False, True, None),
            "不在・途中で切れた": (False, True, "absent"),
            "戻ったが別の方": (False, True, "absent"),
            "戻ってご本人に繋いだ": (True, True, None),           # 繋がったかけ直しは、もう要らない
            "AIは目的の人・判定は別の方": (False, True, None),   # 電話口の種別の target は AI の報告。繋いだ事実が無い
            "留守番電話": (False, False, None),
            "答えられない質問に答えた": (False, True, None)}
        for name, (reached, talked, cb) in cases.items():
            with self.subTest(name):
                r = from_json(SCENES[name])
                self.assertEqual((r.reached(False), r.talked, r.callback and r.callback.reason), (reached, talked, cb))
                self.assertEqual(r.reached(True), talked)          # 「誰でも」の業務：話した＝繋がった（見直し18b の (3)）
        r = from_json(BACK_NOT_TARGET)
        self.assertEqual(([b.reason for b in r.target_backs], [(b.reason, b.read) for b in r.callback_resumes]),
                         (["absent"], [("absent", ("judge", "judge2"))]))
        self.assertEqual((r.callbacks[0].read, r.callbacks[0].connected), (("ai", "judge", "judge2"), False))
        self.assertTrue(from_json(BACK_CONNECTED).callbacks[0].connected)
        r = from_json(ANSWERED)
        self.assertEqual(([a.part for a in r.answered], len(r.not_transferred), r.end_reason, r.hurried),
                         (["unknown"], 1, "not_transferred", False))
        # 「誰でも」の業務（目的のお客様のいない業務）：繋いだ事実が無くても、話していれば繋がった（業務設定から判定の入力に入れる。
        # ⛔ 見直し18b の (3)：前は報告の印だけで推し、種別 target が無いと「繋がらなかった」になった）
        anyone = from_json({"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "party": [{"turn": 1, "said": "はい、私です。", "party": "person", "by": "ai"}]}})
        self.assertEqual((anyone.reached(True), anyone.reached(False), anyone.talked), (True, False, True))
        # 種別の報告なしに「あとでかけて」：かけ直しの申し出があれば話した（「誰でも」の業務なら繋がった）
        later = from_json({"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "callback": [{"turn": 1, "said": "いま手が離せないので、あとでかけてください。", "why": "later", "read": ["ai"],
                          "time": None, "number": None}]}})
        self.assertEqual((later.reached(True), later.reached(False), later.talked), (True, False, True))
        # 確かめに戻った方が「もうかけないで」（発信の拒否＝not_reached は無い）：繋いだ事実が無いので繋がっていない
        refused = copy.deepcopy(BACK_NOT_TARGET)
        refused["not_reached"] = None
        refused["facts"]["do_not_call"] = [{"turn": 5, "said": "もうかけてこないでください。"}]
        r = from_json(refused)
        self.assertEqual((r.reached(False), r.talked, r.do_not_call), (False, True, True))

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
        # 切れ方の材料が無い：話した通話なので、決めつけずに判定できない（出すのは CL-1＝2026-10-24 ユーザー決定）
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


# ── main の flowctl（2026-10-22 の3回目。CL-1 の連絡帳 4-28・2-2 の版 0.5）で通話を流して出た報告。相手の申し出（代わりの方・後任の方・
# 在籍していない・ほかの手段・長期の不在・伝言）と、相手からの質問
SCENES_428 = {'相手からの質問': {'version': 3,
             'facts': {'callee_question': [{'turn': 1, 'said': 'はい、予約しています。当日はどなたが担当されるんですか？', 'answered': None}]},
             'not_reached': None,
             'cut': None,
             'apology_delivered': None},
 '後任の方に繋いだ': {'version': 3,
              'facts': {'departed': [{'turn': 1,
                                      'said': '岡田は先月退職いたしました。',
                                      'policy': 'successor',
                                      'read': ['ai', 'judge'],
                                      'successor': False}],
                        'party': [{'turn': 1, 'said': '岡田は先月退職いたしました。', 'party': 'target', 'by': 'ai'},
                                  {'turn': 2, 'said': '後任は田中になります。少々お待ちください。', 'party': 'reception', 'by': 'ai'},
                                  {'turn': 3, 'said': 'お電話代わりました。田中です。', 'party': 'target', 'by': 'ai'}],
                        'connect': [{'turn': 3,
                                     'said': 'お電話代わりました。田中です。',
                                     'read': ['ai', 'judge', 'judge2'],
                                     'judging': True,
                                     'who': 'successor'}]},
              'not_reached': None,
              'cut': None,
              'apology_delivered': None},
 '在籍していない（終える）': {'version': 3,
                  'facts': {'departed': [{'turn': 1,
                                          'said': '岡田は退職いたしました。',
                                          'policy': 'successor',
                                          'read': ['ai', 'judge'],
                                          'successor': False},
                                         {'turn': 2,
                                          'said': '後任はおりません。',
                                          'policy': 'successor',
                                          'read': ['ai', 'judge'],
                                          'successor': True}],
                            'end': [{'turn': 2, 'said': '後任はおりません。', 'reason': 'departed'}]},
                  'not_reached': 'departed',
                  'cut': None,
                  'apology_delivered': None},
 '長期の不在': {'version': 3,
           'facts': {'callback': [{'turn': 1,
                                   'said': '岡田は来週いっぱい出張で不在にしております。',
                                   'why': 'long_absent',
                                   'read': ['ai', 'judge'],
                                   'time': '再来週の月曜',
                                   'number': None}],
                     'end': [{'turn': 3, 'said': 'はい。', 'reason': 'callback'}]},
           'not_reached': 'absent',
           'cut': None,
           'apology_delivered': None},
 '伝言を残した': {'version': 3,
            'facts': {'callback': [{'turn': 1,
                                    'said': '岡田はただいま外出しております。ご伝言を承りましょうか？',
                                    'why': 'absent',
                                    'read': ['ai', 'judge'],
                                    'offer': 'message',
                                    'message_left': True,
                                    'time': None,
                                    'number': None,
                                    'message': 'はい'}]},
            'not_reached': 'absent',
            'cut': None,
            'apology_delivered': None},
 'ほかの手段（メール）': {'version': 3,
                'facts': {'callback': [{'turn': 1,
                                        'said': '営業のお電話はすべてメールでお願いしております。',
                                        'why': 'other_means',
                                        'read': ['ai', 'judge'],
                                        'offer': 'email',
                                        'email': 'info@example.co.jp'}],
                          'end': [{'turn': 3, 'said': 'はい。', 'reason': 'other_means'}]},
                'not_reached': 'other_means',
                'cut': None,
                'apology_delivered': None},
 '代わりの方（お立場を伺って繋いだ）': {'version': 3,
                       'facts': {'party': [{'turn': 1,
                                            'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                            'party': 'substitute',
                                            'by': 'ai'}],
                                 'substitute': [{'turn': 1,
                                                 'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                                 'policy': 'anyone',
                                                 'read': ['ai', 'judge']}],
                                 'connect': [{'turn': 1,
                                              'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                              'read': ['ai', 'judge'],
                                              'judging': False,
                                              'who': 'substitute'}]},
                       'not_reached': None,
                       'cut': None,
                       'apology_delivered': None},
 '代わりの方（目的のお客様だけ・戻る時刻）': {'version': 3,
                          'facts': {'party': [{'turn': 1,
                                               'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                               'party': 'substitute',
                                               'by': 'ai'}],
                                    'substitute': [{'turn': 1,
                                                    'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                                    'policy': 'target_only',
                                                    'read': ['ai', 'judge']}],
                                    'callback': [{'turn': 1,
                                                  'said': '岡田はおりませんので代わりにお話をお聞きします。',
                                                  'why': 'absent',
                                                  'read': ['ai', 'judge'],
                                                  'time': None,
                                                  'number': None}]},
                          'not_reached': 'absent',
                          'cut': None,
                          'apology_delivered': None}}


class Offers428(unittest.TestCase):
    """CL-1 の連絡帳 4-28 の報告を受け取り、ユーザーの決定（2026-10-22）どおりに読む。"""

    def test_every_report_is_accepted_and_kept(self):
        for name, raw in SCENES_428.items():
            with self.subTest(name):
                r = from_json(raw)
                self.assertEqual(from_json(r.to_json()), r)
                kept = r.to_json()["facts"]                                    # 受けた事実の項目と値を落とさない
                self.assertEqual(set(kept), set(raw["facts"]))
                for kind, rows in raw["facts"].items():
                    for got, sent in zip(kept[kind], rows, strict=True):
                        self.assertEqual({k: got.get(k) for k in sent}, {k: sorted(v) if k == "read" else v for k, v in sent.items()})

    def test_what_the_report_says(self):
        r = from_json(SCENES_428["代わりの方（お立場を伺って繋いだ）"])
        self.assertEqual((r.reached(False), r.talked, r.connects[0].who, r.substitutes[0].policy), (True, True, "substitute", "anyone"))
        r = from_json(SCENES_428["後任の方に繋いだ"])
        self.assertEqual((r.reached(False), r.connects[0].who, r.departures[0].successor), (True, "successor", False))
        r = from_json(SCENES_428["ほかの手段（メール）"])
        self.assertEqual((r.callback.reason, r.callback.offer, dict(r.callback.means)), ("other_means", "email", {"email": "info@example.co.jp"}))
        r = from_json(SCENES_428["伝言を残した"])
        self.assertEqual((r.callback.offer, r.callback.message_left, dict(r.callback.means)), ("message", True, {"message": "はい"}))
        r = from_json(SCENES_428["相手からの質問"])
        self.assertEqual([q.answered for q in r.callee_questions], [None])

    def test_shapes_that_are_still_refused(self):
        base = SCENES_428["ほかの手段（メール）"]
        for name, change in (("申し出の名前が違う", lambda f: f["callback"][0].update(offer="sms")),
                             ("宛先が文字でない", lambda f: f["callback"][0].update(email=1)),
                             ("伝言の印が true でない", lambda f: f["callback"][0].update(message_left=False)),
                             ("代わりの方の応じ方が違う", lambda f: f.update(substitute=[{"turn": 1, "said": "x", "policy": "boss", "read": ["ai"]}])),
                             ("在籍していないの後任の印が無い", lambda f: f.update(departed=[{"turn": 1, "said": "x", "policy": "end", "read": ["ai"]}])),
                             ("質問の答えた印が無い", lambda f: f.update(callee_question=[{"turn": 1, "said": "x"}])),
                             ("繋いだ方の名前が違う", lambda f: f.update(connect=[{"turn": 1, "said": "x", "read": ["ai"], "judging": False,
                                                                                  "who": "boss"}]))):
            with self.subTest(name):
                raw = copy.deepcopy(base)
                change(raw["facts"])
                with self.assertRaises(ReportInvalid):
                    from_json(raw)

    def test_judged_as_decided(self):
        """長期の不在は不在の仲間（戻る日が取れたら約束・取れなければ不在の方針）。ほかの手段・在籍していないは、電話ではかけ直さず
        終える（約束の電話でも先に終える）。代わりの方・後任の方に繋いだ通話は、繋がった通話として業務の質問で決める。"""
        self.assertEqual(run(SCENES_428["長期の不在"]).state, "callback_scheduled")
        j = run(SCENES_428["伝言を残した"])
        self.assertEqual((j.state, j.detail["policy_key"]), ("not_reached", "not_reached_absent"))
        j = run(SCENES_428["代わりの方（目的のお客様だけ・戻る時刻）"])
        self.assertEqual((j.state, j.detail["policy_key"]), ("not_reached", "not_reached_absent"))
        for name in ("代わりの方（お立場を伺って繋いだ）", "後任の方に繋いだ"):
            with self.subTest(name):
                self.assertEqual(run(SCENES_428[name]).state, "incomplete")
        for name, key in (("ほかの手段（メール）", "other_means"), ("在籍していない（終える）", "not_reached_departed")):
            with self.subTest(name):
                j = run(SCENES_428[name])
                self.assertEqual((j.state, j.detail["policy_key"]), ("not_reached", key))
                promise = decide_after_call(run(SCENES_428["伝言を残した"]), normal=P_NONE, promised=P_NONE, rules=RULES,
                                            times_with_key=1, now=NOW, retry_at=NOW, target=Target(uuid.UUID(int=1)))
                for k in (None, promise):                                 # 約束の電話（約束を守る次の動きがある）でも先に終える
                    d = decide_after_call(j, normal=P_NONE, promised=P_NONE, rules=RULES, times_with_key=1, now=NOW,
                                          retry_at=NOW, target=Target(uuid.UUID(int=1)), kept=k)
                    self.assertEqual((d.kind, d.end_reason), ("end", key))
        # 目的のお客様に繋がっていて、ほかの手段を望まれた：取れていない質問があっても、電話ではかけ直さず終える
        reached = copy.deepcopy(SCENES_428["代わりの方（お立場を伺って繋いだ）"])
        reached["facts"]["callback"] = copy.deepcopy(SCENES_428["ほかの手段（メール）"]["facts"]["callback"])
        j = run(reached)
        self.assertEqual((j.state, j.detail["policy_key"]), ("incomplete", "other_means"))


class Later1022(unittest.TestCase):
    """main の会話の 10-22 夜の直し（自分の言葉で答えた文・代わりの方のあとの確かめ）を受け取る。"""

    def raw(self, facts, not_reached="not_transferred"):
        return {"version": 3, "facts": facts, "not_reached": not_reached, "cut": None, "apology_delivered": None}

    def test_own_words_are_kept_in_one_shape(self):
        """お客様からの質問（callee_question）と受け答えの一言（talk。CL-1 の 4-30）の、AI の自分の言葉（相手に言った文 answer・
        確かめに通らなかった文と理由 drafts・中身を確かめた判定 checked）を、同じ1つの形で受け取り、落とさない。"""
        rec = {"turn": 2, "said": "御社はどちらにあるんですか？"}
        draft = [{"text": "東京目黒でございます。", "problem": "業務の情報に無い言葉"}]
        for extra in ({"answer": "東京の品川でございます。", "checked": "Claude Sonnet 5.5（考える）"},
                      {"answer": "かしこまりました。いただいたご質問は担当の者に申し伝えます。", "drafts": draft, "checked": None},
                      {"checked": None}, {}):
            with self.subTest(extra=extra):
                r = from_json(self.raw({"party": [{**rec, "party": "target", "by": "ai"}],
                                        "callee_question": [{**rec, "answered": "drafts" not in extra and "answer" in extra, **extra}],
                                        "talk": [{**rec, **extra}]}, None))
                self.assertEqual(from_json(r.to_json()), r)
                for own in (r.callee_questions[0].own, r.talks[0].own):
                    self.assertEqual((own.answer, [(d.text, d.problem) for d in own.drafts], own.checked),
                                     (extra.get("answer"), [(d["text"], d["problem"]) for d in extra.get("drafts", [])],
                                      extra.get("checked")))
        for bad in ({"answer": ""}, {"problem": "前の形"}, {"drafts": [{"text": "文"}]}, {"drafts": "文"}, {"checked": ""}):
            with self.subTest(bad=bad), self.assertRaises(ReportInvalid):
                from_json(self.raw({"callee_question": [{**rec, "answered": True, **bad}]}, None))
            with self.subTest(bad=bad, kind="talk"), self.assertRaises(ReportInvalid):
                from_json(self.raw({"talk": [{**rec, **bad}]}, None))

    def test_a_talk_answered_with_a_recorded_line_keeps_its_name(self):
        """受け答えの一言で AI の文が使えず、AI が選んだ録音の一言を流した（talk の fallback＝録音の名前。main の fda54b8。記録だけ）。
        受け取って書き戻しても落とさない。無くてもよい。文字でなければ断る。前は「知らない項目」として報告ごと断り、取れた答えも発信禁止の
        申告も読めなかった（見直し17d）。"""
        rec = {"turn": 1, "said": "いつもお世話になってます。", "answer": "こちらこそお世話になっております。", "checked": None,
               "drafts": [{"text": "こちらこそ、先日はご来店いただきありがとうございました。", "problem": "知らない事実"}]}
        for given, expected in (({"fallback": "talk_osewa"}, "talk_osewa"), ({}, None)):
            with self.subTest(given=given):
                r = from_json(self.raw({"talk": [{**rec, **given}]}, None))
                self.assertEqual(r.talks[0].fallback, expected)
                self.assertEqual(from_json(r.to_json()), r)
        for bad in ("", 1):
            with self.subTest(bad=bad), self.assertRaises(ReportInvalid):
                from_json(self.raw({"talk": [{**rec, "fallback": bad}]}, None))

    def test_a_time_beyond_the_limit_agreed_in_the_call_is_kept(self):
        """お客様指定の歯止め時間の外の時刻（「23時以降なら」）を通話の中で確かめて「はい」と言われた印 callback.beyond_limit_ok（2026-10-23
        ユーザー：「案B＆案A(確かめたうえで案A)」）。受け取って書き戻しても落とさない。無ければ確かめていない。真偽でなければ断る。"""
        rec = {"turn": 2, "said": "今日の23時以降なら大丈夫です。", "why": "later", "time": "今日の23時以降"}
        for given, expected in (({}, False), ({"beyond_limit_ok": True}, True), ({"beyond_limit_ok": False}, False)):
            with self.subTest(given=given):
                r = from_json(self.raw({"callback": [{**rec, **given}]}, None))
                self.assertEqual(r.callbacks[0].beyond_limit_ok, expected)
                self.assertEqual(from_json(r.to_json()), r)
        with self.assertRaises(ReportInvalid):
            from_json(self.raw({"callback": [{**rec, "beyond_limit_ok": "はい"}]}, None))

    def test_the_reason_behind_until_is_kept_in_one_place(self):
        """「〇時まで」の言い方の、いま電話を続けられなかった理由（2026-10-23 ユーザー）。かけ直しの理由だけで決まるもの（別の番号へ・
        分かるお客様＝別の方へ／不在・長期の不在・お休み＝それまでは出られない／分かる時＝それまでに確かめておく）には until_why を書かせない
        （同じことを2つの項目で表さない）。あとで などは until_why（無ければ分からない）。書き戻しても落とさない。"""
        rec = {"turn": 2, "said": "3時までなら大丈夫です。", "time": "3時まで"}
        for why, extra, expected in (("later", {"until_why": "unavailable"}, "unavailable"),
                                     ("later", {"until_why": "preparing"}, "preparing"), ("later", {}, None),
                                     ("other_number", {"number": "03-1234-5678"}, "other_party"), ("absent", {}, "unavailable"),
                                     ("long_absent", {}, "unavailable"), ("day_off", {}, "unavailable"),
                                     ("ask_when", {}, "preparing")):
            with self.subTest(why=why, extra=extra):
                r = from_json(self.raw({"callback": [{**rec, "why": why, **extra}]}, None))
                self.assertEqual(r.callbacks[0].until_reason, expected)
                self.assertEqual(from_json(r.to_json()), r)
        for why, bad in (("absent", "unavailable"), ("other_number", "other_party"), ("later", "soon"), ("later", None)):
            with self.subTest(why=why, bad=bad), self.assertRaises(ReportInvalid):
                extra = {"number": "03-1234-5678"} if why == "other_number" else {}
                from_json(self.raw({"callback": [{**rec, "why": why, "until_why": bad, **extra}]}, None))

    def test_a_check_after_a_substitute_is_accepted_but_not_without_one(self):
        """代わりの方と話している途中にご本人が出て確かめに戻った（target_back。かけ直しの理由はまだ無い＝why が null）は受け取る。
        かけ直しの申し出も代わりの方の申し出も無い確かめ・かけ直しの申し出の無い「続きへ戻った」は、これまでどおり断る。"""
        sub = {"turn": 2, "said": "お電話代わりました。山田です。", "policy": "decision_maker", "read": ["judge2"]}
        back = {"turn": 4, "said": "お電話代わりました。岡田です。", "why": None}
        r = from_json(self.raw({"substitute": [sub], "target_back": [back]}))
        self.assertEqual((r.target_backs[0].reason, from_json(r.to_json())), (None, r))
        for facts in ({"target_back": [back]},
                      {"substitute": [sub], "callback_resume": [{**back, "why": "absent", "read": []}]}):
            with self.subTest(facts=list(facts)), self.assertRaises(ReportInvalid):
                from_json(self.raw(facts))


# ── 「誰でも」の業務の報告（main の flowctl：目的のお客様のいない業務では電話口の種別を残さない。見直し18b）
ANYONE_DONE = {   # 質問に答えて「質問が終わった」で締めた（お客様からの質問と受け答えもある）
    "version": 3, "facts": {
        "callee_question": [{"turn": 2, "said": "それはいくらですか？", "answered": True, "answer": "初月は半額です。",
                             "checked": "judge"}],
        "end": [{"turn": 4, "said": "はい、ありがとうございます。", "reason": "done"}]},
    "not_reached": None, "cut": None, "apology_delivered": None}
ANYONE_REFUSED = {   # 「結構です」で通話を断られて締めた
    "version": 3, "facts": {"end": [{"turn": 1, "said": "結構です。", "reason": "callee_refused_call"}]},
    "not_reached": None, "cut": None, "apology_delivered": None}
ANYONE_SILENT = {   # 名乗りのあと、何も言わずに切られた（締めた理由も無い）
    "version": 3, "facts": {}, "not_reached": None, "cut": None, "apology_delivered": None}


def judged(raw, *, anyone: bool, items=None, cut=CLOSED):
    return judge(JudgeInput(TALKED, replace(from_json(raw), cut=cut), items or {}, RULES, False, True, callee=CALLEE,
                            anyone_target=anyone))


class Anyone18b(unittest.TestCase):
    def test_which_facts_show_a_person_is_decided_in_one_table(self):
        """人の証の表（call_report の HUMAN_ENDS・HUMAN_FACTS）：締めた理由は、人の言葉の締めと機械の締めにちょうど分かれる。
        人の締め・お客様からの質問・受け答えだけの報告も「話した」。留守番電話・自動音声・FAX の締めだけでは話していない。"""
        from salescall.core import call_report as cr
        self.assertEqual((cr.HUMAN_ENDS | cr.MACHINE_ENDS, cr.HUMAN_ENDS & cr.MACHINE_ENDS), (set(cr.END_REASONS), set()))
        base = {"version": 3, "not_reached": None, "cut": None, "apology_delivered": None}
        rec = {"turn": 1, "said": "試験の発話"}
        for reason in cr.END_REASONS:
            with self.subTest(reason):
                r = from_json({**base, "facts": {"end": [{**rec, "reason": reason}]}})
                self.assertEqual(r.talked, reason in cr.HUMAN_ENDS)
        for kind, record in [("callee_question", {"answered": None, "checked": None}), ("talk", {"answer": "恐れ入ります。",
                                                                                                  "checked": "judge"}),
                             ("answered", {"part": "unknown"}), ("ai_question", {}), ("complaint", {})]:
            with self.subTest(kind):
                self.assertTrue(from_json({**base, "facts": {kind: [{**rec, **record}]}}).talked)
        self.assertFalse(from_json({**base, "facts": {"do_not_call": [rec]}}).talked)   # 自動音声の「お断り」もありうる
        self.assertFalse(from_json(ANYONE_SILENT).talked)

    def test_a_talk_in_an_anyone_business_is_judged_as_a_conversation(self):
        """「誰でも」の業務で話した通話は「会話に至らなかった」にならない（⛔ 見直し18b：main の報告 685 件のうち、締めまで進んだ
        「誰でも」の通話がすべて open＝会話に至らなかった、だった）。取れていれば完了・取れていなければ一部だけ取れた。"""
        taken = {"Q1": Item("confirmed", None, None), "Q2": Item("accepted", "はい", None)}
        self.assertEqual(judged(ANYONE_DONE, anyone=True, items=taken).state, "completed")
        self.assertEqual(judged(ANYONE_DONE, anyone=True, items={"Q1": taken["Q1"]}).state, "incomplete")
        self.assertEqual(judged(ANYONE_REFUSED, anyone=True).state, "incomplete")
        self.assertEqual(judged(ANYONE_SILENT, anyone=True).state, "open")     # 何も起きていない＝会話に至らなかった
        # 目的のお客様のいる業務で、繋がらずに締めた（繋がらなかった理由が無い）は判定できない（繋がったとは読まない）
        self.assertEqual(judged(ANYONE_DONE, anyone=False, items={"Q1": taken["Q1"]}).detail.get("unknown_cause"),
                         "not_reached_reason_missing")

    def test_a_silent_hang_up_in_an_anyone_business_follows_the_cut_rules(self):
        """「誰でも」の業務でも、話したお客様が AI の話の途中・話し終えたあとの間に黙って切ったら拒否（7-1c）。切れ方の材料が
        無ければ判定できない（cut_missing。⛔ 前は「話していない」と読み、材料が無くても拒否も見なかった）。"""
        j = judged(ANYONE_DONE, anyone=True, cut=Cut("ai", False, False))
        self.assertEqual((j.state, j.dnc_reason, j.detail.get("refusal")), ("do_not_call", "refused_in_call", "cut"))
        self.assertEqual(judged(ANYONE_DONE, anyone=True, cut=None).detail.get("unknown_cause"), "cut_missing")

    def test_a_person_of_unknown_role_in_an_anyone_business_is_the_target(self):
        """「誰でも」の業務で、CL-1 が役の分からないお客様（person）を出しても、繋がった（判定待ちにならない）。目的のお客様のいる
        業務では、繋いだ事実が無ければ繋がっていない。"""
        raw = copy.deepcopy(ANYONE_DONE)
        raw["facts"]["party"] = [{"turn": 1, "said": "はい、テスト商店です。", "party": "person", "by": "ai"}]
        self.assertEqual(judged(raw, anyone=True, items={"Q1": Item("confirmed", None, None)}).state, "incomplete")
        self.assertEqual(judged(raw, anyone=False, items={"Q1": Item("confirmed", None, None)}).state, "unknown")


# ── 保留で待ちきった（CL-1 の連絡帳 4-42・4-44）・取り次げなかったあとの確かめ（4-47）
HOLD = {"turn": 2, "said": "少々お待ちください。", "waited_s": 300}
HOLD_BEFORE_TARGET = {   # 目的のお客様に繋がる前の保留で待ちきった：繋がらなかった理由は「取り次げなかった」
    "version": 3, "facts": {"hold_timeout": [HOLD], "end": [{"turn": 2, "said": "少々お待ちください。", "reason": "hold_timeout"}]},
    "not_reached": "not_transferred", "cut": None, "apology_delivered": None}


class Hold1024(unittest.TestCase):
    def test_a_hold_timeout_is_accepted_and_read_by_the_judgement_already_there(self):
        """保留で待ちきった（締めた理由・事実 hold_timeout）を受け取る。判定は今の1か所のまま：目的のお客様に繋がる前なら、繋がらなかった
        理由 not_transferred（日を改めて1回）。繋がったあと・「誰でも」の業務なら「取れていない」（2026-10-24 CL-1 の 4-44）。
        保留を頼むのは人の言葉なので「話した」（人の証の表）。"""
        r = from_json(HOLD_BEFORE_TARGET)
        self.assertEqual((r.end_reason, r.hold_timeouts[0].waited_s, r.talked, from_json(r.to_json())),
                         ("hold_timeout", 300.0, True, r))
        j = run(HOLD_BEFORE_TARGET)
        self.assertEqual((j.state, j.detail["not_reached"], j.detail["policy_key"]),
                         ("not_reached", "not_transferred", "not_reached_not_transferred"))
        after = copy.deepcopy(HOLD_BEFORE_TARGET)   # 繋がったあとの「確認しますので少々お待ちください」
        after["not_reached"] = None
        after["facts"]["connect"] = [{"turn": 1, "said": "はい、岡田です。", "read": ["ai", "judge"], "judging": True}]
        self.assertEqual(run(after).state, "incomplete")
        anyone = copy.deepcopy(HOLD_BEFORE_TARGET)   # 目的のお客様のいない業務の保留
        anyone["not_reached"] = None
        self.assertEqual(judged(anyone, anyone=True).state, "incomplete")
        for bad in (0, -5, "300", True, None):
            with self.subTest(waited_s=bad), self.assertRaises(ReportInvalid):
                from_json({**HOLD_BEFORE_TARGET, "facts": {"hold_timeout": [{**HOLD, "waited_s": bad}]}})

    def test_a_check_after_could_not_be_put_through_is_accepted(self):
        """約束の電話で「取り次げない」と言われ、約束の一言を伺っている間に取り次がれて確かめに戻った（target_back のあと connect）を
        受け取り、繋いだ事実で「繋がった」と読む（CL-1 の連絡帳 4-47）。"""
        raw = {"version": 3, "not_reached": None, "cut": None, "apology_delivered": None, "facts": {
            "not_transferred": [{"turn": 2, "said": "あいにく本日はお繋ぎできかねます。"}],
            "target_back": [{"turn": 4, "said": "あ、いま戻りました。代わります。", "why": None}],
            "connect": [{"turn": 5, "said": "お電話代わりました。岡田です。", "read": ["ai", "judge"], "judging": True}]}}
        r = from_json(raw)
        self.assertEqual((r.reached(False), from_json(r.to_json())), (True, r))
        self.assertEqual(run(raw).state, "incomplete")
