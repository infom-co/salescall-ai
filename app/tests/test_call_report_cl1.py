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
        self.assertEqual((r.reached_target, r.talked, r.connects[0].who, r.substitutes[0].policy), (True, True, "substitute", "anyone"))
        r = from_json(SCENES_428["後任の方に繋いだ"])
        self.assertEqual((r.reached_target, r.connects[0].who, r.departures[0].successor), (True, "successor", False))
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
