"""業務設定の版（引き継ぎの (10)。core/business_settings.py）の試験。"""

from __future__ import annotations

import copy
import unittest

from uuid import UUID

from salescall.core import business_rules
from salescall.core import business_settings as bs
from salescall.core.access import Rejected
from tests.support import OPERATOR, World, owner_access, rules_raw

FIELD_TYPES = frozenset({"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address", "yes_no",
                         "choice", "free_text"})


def name(text, reading, source, by="given", candidates=(), likely=None) -> dict:
    return {"text": text, "reading": reading, "source": source, "reading_by": by if reading else None,
            "candidates": list(candidates), "likely": likely}


def raw() -> dict:
    """試験の業務設定（架空）：予約するか（最終目的の申し出。しない＝断り＝終わり）→ 希望の日時。"""
    r = rules_raw(required=("Q1", "Q2"))
    return {
        "purpose": "予約の希望日時を伺う",
        "target_class": "corporate",
        "caller": {"company": {"text": "株式会社テスト物流", "reading": "かぶしきがいしゃてすとぶつりゅう"},
                   "person": {"text": "山田", "reading": "やまだ"},
                   "purpose_line": "ご予約の確認でお電話いたしました。"},
        "target_person": {"kind": "anyone", "role": None},
        "busy_line": None,
        "voicemail_message": None,
        "ai_answer": None,
        "answers": [],
        "voice": "S2",
        "questions": [
            {"id": "Q1", "text": "ご予約はなさいますか。", "ask_about": "ご予約", "type": "choice", "required": True,
             "choices": ["する", "しない"], "next": {"しない": "END", "*": "Q2"}, "unit": None, "meanings": None, "replies": None,
             "asks_fact": False, "do_words": None, "away": None, "intro": None, "ask_if": None, "approval": False, "final_goal": True,
             "declines": ["しない"], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False},
            {"id": "Q2", "text": "ご希望の日時をお聞かせください。", "ask_about": "ご希望の日時", "type": "datetime",
             "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None,
             "asks_fact": True, "do_words": None, "away": None, "intro": None, "ask_if": None, "approval": False, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False},
        ],
        "names": [name("株式会社テスト物流", "かぶしきがいしゃてすとぶつりゅう", "caller"), name("山田", "やまだ", "caller"),
                  name("テスト物流", "てすとぶつりゅう", "script")],
        "rules": r,
    }


def problems_of(r: dict) -> list[str]:
    with unittest.TestCase().assertRaises(bs.SettingsInvalid) as e:
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
    return e.exception.problems


class Check(unittest.TestCase):
    def test_valid_and_flow(self):
        s = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.questions[1].choices, ("答えた",))            # 値を聞く質問の選択肢は型で決まる
        # 分岐のシート（Q1 しない → 終わり）は置き換えて読む：「しない」はここで終える答え（依頼者が最終目的の列で決めた最終目的と断る答えは
        # そのまま＝両方。CL-1 の連絡帳 4-79）。ほかの答えは並びの次（Q2）なので、伺う条件は付かない
        self.assertEqual([(q.final_goal, q.declines, q.ends_on, q.ask_if, q.required) for q in s.questions],
                         [(True, ("しない",), ("しない",), None, True), (False, (), (), None, True)])

    def test_answers_the_ai_may_give(self):
        """想定問答（お客様に聞かれたら答えてよいこと。CL-1 の連絡帳 4-26）：問いの名前と答えの文の並び。問いの名前は重ならず、
        録音のファイル名に使え、AI が選ぶ録音の部分の名前と同じでない。"""
        r = raw()
        r["answers"] = [{"name": "番号の出どころ", "text": "以前ご予約の際にいただいたお電話番号にお掛けしております。"}]
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).answers,
                         (("番号の出どころ", "以前ご予約の際にいただいたお電話番号にお掛けしております。"),))
        for bad, word in (([{"name": "番号の出どころ", "text": "a"}, {"name": "番号の出どころ", "text": "b"}], "同じ問いの名前"),
                          ([{"name": "番号:出どころ", "text": "a"}], "ファイル名"), ([{"name": "あ" * 31, "text": "a"}], "ファイル名"),
                          ([{"name": "intro", "text": "a"}], "録音の部分の名前"), ([{"name": "用件", "text": "a"}], "録音の部分の名前"),
                          ([{"name": "自分で答える質問", "text": "a"}], "録音の部分の名前"),
                          ([{"name": "no_answer", "text": "a"}], "録音の部分の名前"),
                          ([{"name": "お呼びした方", "text": "a"}], "録音の部分の名前"),   # CL-1 の連絡帳 4-65 の (f)
                          ([{"name": "target", "text": "a"}], "録音の部分の名前"), ([{"name": "関係", "text": " "}], "答えの文"),
                          ([{"name": "関係"}], "問いの名前（name）と答えの文（text）"), ({"関係": "a"}, "並び")):
            with self.subTest(bad=bad):
                r = raw()
                r["answers"] = bad
                self.assertTrue(any(word in p for p in problems_of(r)), problems_of(r))

    def test_reserved_answer_names_are_one_literal_the_conversation_side_can_read(self):
        """使えない問いの名前の正本は会話の側（CL-1 の RESERVED_PART_NAMES＝PART_LABEL の鍵と値）。突き合わせる試験は、両方を読んでよい
        会話の側の試験に置く（app/ の試験は試作を読まない＝14章 I-30。CL-2.md 4-144 で CL-1 に頼んだ）。こちらは、会話の側の試験が
        読み込まずに読めるよう、ANSWER_RESERVED・ANSWER_PART_PREFIX を1つの書き下しの値で持つことを見張る（写しの一覧は持たない）。
        ⛔ 見直し17d：前はこの試験の中に写しを持っていたので、会話の側が「受け答え」（talk）を足しても気づかなかった。"""
        import ast
        from pathlib import Path
        tree = ast.parse(Path(bs.__file__).read_text(encoding="utf-8"))
        assigned = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                    if isinstance(t, ast.Name)}
        reserved = assigned["ANSWER_RESERVED"]
        self.assertEqual(ast.unparse(reserved.func), "frozenset")
        self.assertEqual(frozenset(ast.literal_eval(reserved.args[0])), bs.ANSWER_RESERVED)
        self.assertEqual(ast.literal_eval(assigned["ANSWER_PART_PREFIX"]), bs.ANSWER_PART_PREFIX)
        r = raw()
        r["answers"] = [{"name": "答えられない質問", "text": "a"}]
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).answers, (("答えられない質問", "a"),))

    def test_responses_to_callee_offers(self):
        """相手の申し出への応じ方・伝言の文・業務の情報・改めて連絡すると申し出てよいか（CL-1 の連絡帳 4-28・2026-10-22 ユーザー決定）：
        書かなければ既定（判断いただけるお立場の方なら話す・断って戻る時刻を聞く・後任の方をお呼びする・情報なし・いいえ）。選べない
        値・伝言を残すのに文が無い・情報の形の誤りは止める（CL-1 の flowctl.check_policies と同じ決まり）。"""
        s = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.substitute_policy, s.message_policy, s.message, s.departed_policy, s.info, s.follow_up),
                         ("decision_maker", "decline", None, "successor", (), False))
        r = raw()
        r.update(substitute_policy="anyone", message_policy="leave", message="お伝えいただけますでしょうか？",
                 departed_policy="end", info=["営業時間は9時から18時です"], follow_up=True)
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.substitute_policy, s.message_policy, s.message, s.departed_policy, s.info, s.follow_up),
                         ("anyone", "leave", "お伝えいただけますでしょうか？", "end", ("営業時間は9時から18時です",), True))
        for bad, word in (({"substitute_policy": "boss"}, "選べません"), ({"departed_policy": "x"}, "選べません"),
                          ({"message_policy": "leave"}, "伝言の文"), ({"info": "営業時間"}, "業務の情報"),
                          ({"info": [" "]}, "業務の情報"), ({"follow_up": "はい"}, "follow_up"), ({"unknown_key": 1}, "無くてもよい")):
            with self.subTest(bad=bad):
                r = raw()
                r.update(bad)
                self.assertTrue(any(word in p for p in problems_of(r)), problems_of(r))

    def test_meanings_and_replies_of_choice_questions(self):
        """はい／いいえ・選択肢の質問は、意味の言葉（選択肢 → 言葉）と曖昧な言葉の意味（言葉 → 選択肢）を持てる。宣言した選択肢に
        だけ付く（システムの答えには付けない。CL-1 の連絡帳 4-17）。"""
        r = raw()
        r["questions"][0]["meanings"] = {"する": "ご予約なさるということ", "しない": "ご予約はなさらないということ"}
        r["questions"][0]["replies"] = {"結構": "しない"}
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.questions[0].meanings["する"], s.questions[0].replies), ("ご予約なさるということ", {"結構": "しない"}))
        self.assertEqual((s.questions[1].meanings, s.questions[1].replies), ({}, {}))
        for key, bad in (("meanings", {"わからない": "わからないということ"}), ("meanings", {"する": " "}),
                         ("replies", {"結構": "どちらでも"}), ("replies", {"たぶん": "する"}), ("meanings", ["する"])):
            with self.subTest(key=key, bad=bad):
                r = raw()
                r["questions"][0][key] = bad
                self.assertTrue(any("意味" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][1]["meanings"] = {"答えた": "お答えいただいたということ"}
        self.assertTrue(any("はい／いいえ・決まった選択肢の質問だけ" in p for p in problems_of(r)), problems_of(r))

    def test_any_is_a_system_answer_of_choice_questions(self):
        """決まった選択肢の質問には、システムが「どちらでも」を足す（お客様がこちらに任せた＝取れた答え。2026-10-17 ユーザー。
        CL-1 の連絡帳 4-17）。同じ名前の選択肢は書けない。分岐を書くなら、その行き先も要る（受け皿でよい）。はい／いいえには足さない。"""
        s = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(bs.possible_answers(s.questions[0]), ("する", "しない", "どちらでも", "どれも要らない", "わからない", "答えたくない"))
        self.assertNotIn("どちらでも", s.questions[0].ends_on)             # 受け皿（Q2＝並びの次）へ
        self.assertIsNone(s.questions[1].ask_if)
        r = raw()
        r["questions"][0]["choices"] = ["する", "しない", "どちらでも"]
        self.assertTrue(any("選択肢「どちらでも」は、システムが足す答え" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0]["next"] = {"する": "Q2", "しない": "END", "わからない": "Q2", "答えたくない": "Q2"}
        self.assertTrue(any("受け皿がありません" in p and "どちらでも" in p for p in problems_of(r)), problems_of(r))
        yes_no = bs.Question("Q9", "ご予約はお済みですか。", None, "yes_no", True, bs.YES_NO_CHOICES, {})
        self.assertNotIn("どちらでも", bs.possible_answers(yes_no))

    def test_none_is_a_system_answer_of_choice_questions(self):
        """CL-1 の連絡帳 4-66 の (a)（✅ 2026-11-02 日本時間4時16分 ユーザー）：事実を伺わない決まった選択肢の質問には、システムが「どれも
        要らない」を足す（はい／いいえ・事実を伺う質問には足さない）。同じ名前の選択肢は書けない。分岐を書くなら、その行き先も要る。
        どの質問でも申し出を断る答え（`declined_answers` の1か所＝会話の側の Question.declined と同じ）。最終目的の申し出の質問では、断られると
        会話の側がその場で締めうる（その先の必須の質問を飛ばしてよい）。ほかの質問では、断ったまま分岐のとおり続ける（飛ばせない）。"""
        s = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertIn("どれも要らない", bs.possible_answers(s.questions[0]))
        self.assertNotIn("どれも要らない", s.questions[0].ends_on)          # 受け皿（Q2＝並びの次）へ
        yes_no = bs.Question("Q9", "ご予約はお済みですか。", None, "yes_no", True, bs.YES_NO_CHOICES)
        fact = bs.Question("Q8", "ご予約の方法は。", None, "choice", True, ("電話", "窓口"), asks_fact=True)
        self.assertEqual([bs.NONE_CHOICE in bs.possible_answers(q) for q in (yes_no, fact)], [False, False])
        r = raw()
        r["questions"][0]["choices"] = ["する", "どれも要らない"]
        self.assertTrue(any("選択肢「どれも要らない」は、システムが足す答え" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0]["next"] = {"する": "Q2", "しない": "END", "どちらでも": "Q2", "わからない": "Q2", "答えたくない": "Q2"}
        self.assertTrue(any("受け皿がありません" in p and "どれも要らない" in p for p in problems_of(r)), problems_of(r))
        # 「どれも要らない → 終わり」は、ここで終える答え（会話の側は、先にほかのご希望を1回伺う＝CL-1 の連絡帳 4-74 の (4)）。最終目的の質問でも
        # 断る答えとして数えるのは今までどおり（declined_answers の1か所）。必須の Q2 を伺わずに終わっても問題にしない（判定が「伺っていない
        # （対象外）」と数える）
        r = raw()
        r["questions"][0]["next"] = {"する": "Q2", "しない": "END", "どれも要らない": "END", "*": "Q2"}
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((bs.declined_answers(s.questions[0]), s.questions[0].ends_on), (frozenset({"しない", "どれも要らない"}), ("しない", "どれも要らない")))
        r["questions"][0].update(final_goal=False, declines=[])
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].ends_on, ("しない", "どれも要らない"))

    def test_final_goals_their_decline_answers_and_how_the_goal_is_met(self):
        """最終目的の申し出（final_goal）・断る答え（declines）・最終目的の果たし方（goal_mode）は業務設定の質問と業務に持つ（CL-1 の連絡帳
        4-69 の 4-182 の (e)。会話の側の Question.final_goal・declines・instruction.Business.goal_mode と同じ中身。⛔ 前は業務の決まりの
        断りの答え decline に持ち、判定が最終目的かを問わず「用件を断られた」と決め直していた）。断る答えは、その質問の選択肢から。最終目的の
        申し出には断る答えが要る（ご承認の質問の「いいえ」・「どれも要らない」も断る答え）。果たし方は、最終目的が2つ以上のときだけ書く
        （必ず書く＝こちらで推し量らない）。"""
        s = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual([(q.final_goal, q.declines) for q in s.questions], [(True, ("しない",)), (False, ())])
        self.assertIsNone(s.goal_mode)
        approval = bs.Question("Q9", "以上でよろしいでしょうか？", None, "yes_no", True, bs.YES_NO_CHOICES, approval=True)
        self.assertEqual(bs.declined_answers(approval), frozenset({"いいえ"}))
        two = raw()
        two["questions"][1].update(type="yes_no", text="ご案内の資料をお送りしてもよろしいでしょうか？", final_goal=True, declines=["いいえ"])
        self.assertTrue(any("最終目的の果たし方（goal_mode）を書きます" in p for p in problems_of(two)), problems_of(two))
        self.assertEqual(bs.check(dict(two, goal_mode="all"), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).goal_mode, "all")
        self.assertEqual(bs.check(dict(two, goal_mode="any"), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).goal_mode, "any")
        cases = []
        r = raw(); r["goal_mode"] = "any"; cases.append((r, "最終目的の果たし方（goal_mode）は書きません"))
        r = dict(two, goal_mode="どれか"); cases.append((r, "最終目的の果たし方（goal_mode）を書きます"))
        r = raw(); r["questions"][0]["declines"] = ["やめる"]; cases.append((r, "断る答え ['やめる'] は、その質問の選択肢にありません"))
        r = raw(); r["questions"][0]["declines"] = "しない"; cases.append((r, "断る答え（declines）は、選択肢の名前の並びです"))
        r = raw(); r["questions"][0]["declines"] = ["しない", "しない"]; cases.append((r, "断る答え（declines）は、選択肢の名前の並びです"))
        r = raw(); r["questions"][1]["declines"] = ["明日"]; cases.append((r, "選択肢で答える質問ではないので、断る答え（declines）は付けません"))
        r = raw(); r["questions"][0]["final_goal"] = None; cases.append((r, "最終目的の申し出か（final_goal）は true / false です"))
        r = raw(); r["questions"][1]["final_goal"] = True; cases.append((r, "最終目的の申し出ですが、断る答えがありません"))
        r = raw(); r["questions"][0]["asks_fact"] = True; r["questions"][0]["declines"] = []
        cases.append((r, "最終目的の申し出ですが、断る答えがありません"))   # 事実を伺う質問には「どれも要らない」も無い
        r = raw(); r["rules"]["decline"] = {"Q1": ["しない"]}; cases.append((r, "断りの答えは、業務設定の質問の断る答え declines に書きます"))
        for r, words in cases:
            with self.subTest(words):
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))

    def test_questions_asked_only_after_an_answer_and_approval_questions(self):
        """CL-1 の連絡帳 4-66 の (b)（✅ 2026-11-02 日本時間4時16分・4時36分 ユーザー）：伺う条件（ask_if）は、前に並ぶ質問の番号と、その
        質問の返りうる答えの並び（「わからない」なども書ける）。ご承認を伺う質問（approval）は、はい／いいえの質問だけ。必須の質問が
        すべて伺う条件つきなら、どれも伺わない通話が「すべて取れた」になるので通さない。"""
        r = raw()
        r["questions"][0]["choices"] = ["する", "しない", "迷っている"]
        r["questions"].append({"id": "Q3", "text": "以上の内容でよろしいでしょうか？", "ask_about": "ご承認", "type": "yes_no",
                               "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None,
                               "asks_fact": False, "do_words": None, "away": None, "intro": "それでは重要な点だけご説明させていただきます。",
                               "ask_if": {"after": "Q1", "answers": ["迷っている", "わからない"], "hedged": False}, "approval": True, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False})
        r["rules"] = rules_raw(required=("Q1", "Q2", "Q3"))
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.questions[2].ask_if, s.questions[2].approval, s.questions[0].ask_if, s.questions[0].approval),
                         (bs.AskIf("Q1", ("迷っている", "わからない")), True, None, False))
        for name, change, words in (
                ("前の質問が後ろ", lambda r: r["questions"][1].update(ask_if={"after": "Q3", "answers": ["はい"], "hedged": False}), "前に並ぶ質問ではありません"),
                ("前の質問が自分", lambda r: r["questions"][2].update(ask_if={"after": "Q3", "answers": ["はい"], "hedged": False}), "前に並ぶ質問ではありません"),
                ("前の質問が無い", lambda r: r["questions"][2].update(ask_if={"after": "Q7", "answers": ["する"], "hedged": False}), "前に並ぶ質問ではありません"),
                ("答えが空", lambda r: r["questions"][2].update(ask_if={"after": "Q1", "answers": [], "hedged": False}), "伺う条件の答え"),
                ("答えが重なる", lambda r: r["questions"][2].update(ask_if={"after": "Q1", "answers": ["する", "する"], "hedged": False}), "伺う条件の答え"),
                ("答えが前の質問に無い", lambda r: r["questions"][2].update(ask_if={"after": "Q1", "answers": ["はい"], "hedged": False}), "前の質問 Q1 の答えにありません"),
                ("形が違う", lambda r: r["questions"][2].update(ask_if={"after": "Q1"}), "伺う条件（ask_if）"),
                ("ご承認がはい／いいえでない", lambda r: r["questions"][0].update(approval=True), "ご承認を伺う質問（approval）にできません"),
                ("ご承認が真偽でない", lambda r: r["questions"][2].update(approval="はい"), "ご承認を伺う質問か（approval）は true / false"),
                ("いつも伺う必須が無い", lambda r: [r["questions"][0].update(required=False, next=None), r["questions"][1].update(required=False)],
                 "いつも伺う必須の質問が1つもありません")):
            with self.subTest(name):
                bad = copy.deepcopy(r)
                change(bad)
                if name == "いつも伺う必須が無い":
                    bad["rules"] = rules_raw(required=("Q3",))
                self.assertTrue(any(words in p for p in problems_of(bad)), problems_of(bad))

    def test_a_question_asking_a_fact_has_no_any_answer(self):
        """お客様に事実を伺う質問（CL-1 の連絡帳 2-1 の版 0.6。✅ 2026-11-01 ユーザー：(a)「OK」）には、決まった選択肢の質問でも
        「どちらでも」を足さない（「お任せします」は答えにならない＝聞き直す）。返りうる答えの1か所なので、分岐の行き先も求めず、
        会話の入口も「どちらでも」を受け取らない。真偽でない値は通さない。"""
        r = raw()
        r["questions"][0]["asks_fact"] = True
        r["questions"][0]["next"] = {"する": "Q2", "しない": "END", "わからない": "Q2", "答えたくない": "Q2"}   # 受け皿なし
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertTrue(s.questions[0].asks_fact)
        self.assertEqual(bs.possible_answers(s.questions[0]), ("する", "しない", "わからない", "答えたくない"))
        self.assertFalse(bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].asks_fact)
        for bad in (None, "はい", 1):
            with self.subTest(bad=bad):
                r = raw()
                r["questions"][0]["asks_fact"] = bad
                self.assertTrue(any("事実を伺う質問か" in p for p in problems_of(r)), problems_of(r))

    def test_do_words_intro_and_client_does(self):
        """CL-1 の連絡帳 4-65 の (a)(b)(c)：こちらがすることの言葉（選択肢 → 言葉）は、事実を伺う質問ではない選択肢の質問だけが持てる
        （宣言した選択肢にだけ・事実を伺う質問には付けない＝会話の側が使わない）。入る一言は最初の質問には付けない（名乗りと一緒に流す）。
        依頼者がすることは無くてもよい（無ければ会話の側の既定「記録」）。"""
        r = raw()
        r["questions"][0]["do_words"] = {"する": "ご予約をお取りさせていただきます", "しない": "ご予約は控えさせていただきます"}
        r["questions"][1]["intro"] = "それではご希望の日時の確認からさせていただきます。"
        r["client_does"] = "手配"
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.questions[0].do_words, s.questions[1].intro, s.client_does),
                         ({"する": "ご予約をお取りさせていただきます", "しない": "ご予約は控えさせていただきます"},
                          "それではご希望の日時の確認からさせていただきます。", "手配"))
        self.assertIsNone(bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).client_does)
        for name, change, words in (("事実を伺う質問", lambda r: r["questions"][0].update(asks_fact=True,
                                                                                       do_words={"する": "ご予約をお取りさせていただきます"}),
                                     "事実を伺う質問"),
                                    ("宣言に無い選択肢", lambda r: r["questions"][0].update(do_words={"わからない": "x"}), "こちらがすることの言葉"),
                                    ("値を聞く質問", lambda r: r["questions"][1].update(do_words={"答えた": "x"}), "こちらがすることの言葉"),
                                    ("最初の質問の入る一言", lambda r: r["questions"][0].update(intro="それでは"), "入る一言"),
                                    ("入る一言が空", lambda r: r["questions"][1].update(intro=" "), "入る一言"),
                                    ("依頼者がすることが空", lambda r: r.update(client_does=""), "依頼者がすること")):
            with self.subTest(name):
                r = raw()
                change(r)
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))

    def test_away_choices(self):
        """CL-1 の連絡帳 4-71 の (a)・4-72 の (a)：目的から遠ざかる向きの選択肢（away）は、事実を伺う質問ではない選択肢の質問だけが持てる
        （その質問の選択肢の並び・重ならない）。⛔ 2026-11-03 日本時間8時10分 ユーザー：取込の AI が作る「目的につなげて伺う言葉」（keep_words）は
        取り下げ＝業務設定の形に無い（書けば形の誤り）。"""
        r = raw()
        r["questions"][0].update(away=["しない"])
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].away, ("しない",))
        self.assertEqual(bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].away, ())
        self.assertNotIn("keep_words", bs.QUESTION_KEYS)
        for name, change, words in (
                ("事実を伺う質問", lambda r: r["questions"][0].update(asks_fact=True, away=["しない"]), "事実を伺う質問"),
                ("選択肢に無い", lambda r: r["questions"][0].update(away=["わからない"]), "その質問の選択肢にありません"),
                ("重なる", lambda r: r["questions"][0].update(away=["しない", "しない"]), "目的から遠ざかる向きの選択肢（away）は"),
                ("並びでない", lambda r: r["questions"][0].update(away="しない"), "目的から遠ざかる向きの選択肢（away）は"),
                ("値を聞く質問", lambda r: r["questions"][1].update(asks_fact=False, away=["答えた"]), "はい／いいえ・決まった選択肢の質問だけ"),
                ("取り下げた言葉", lambda r: r["questions"][0].update(keep_words={"しない": "x"}), "です")):
            with self.subTest(name):
                r = raw()
                change(r)
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))

    def test_question_text_does_not_start_with_a_lead_word(self):
        """CL-1 の連絡帳 4-72 の (c)：2つ目からの質問の文は、つなぎの言葉（それでは・続きまして・では・はい・ありがとうございます）で始めない
        （会話の側が、つなぎの言葉か入る一言を頭に付けて流す＝重なる）。最初の質問（名乗りと一緒に流す・頭に何も付かない）は止めない。"""
        # 会話の側が質問の頭に付ける言葉（つなぎの言葉と相づち。CL-1 の連絡帳 4-72 の (c)・4-75。正本は会話の側の phrases.QUESTION_HEADS＝比べる試験は
        # CL-1 の test_question_heads_match_cl2）が、どれも並びにある
        self.assertLessEqual({"それでは", "続きまして", "では", "はい", "ありがとうございます", "承知しました", "かしこまりました", "失礼いたしました"},
                             set(bs.QUESTION_LEADS))
        for lead in bs.QUESTION_LEADS:
            with self.subTest(lead=lead):
                r = raw()
                r["questions"][1]["text"] = f"{lead}、ご希望の日時をお聞かせください。"
                self.assertTrue(any(f"「{lead}」で始めません" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0]["text"] = "それでは、ご予約はなさいますか。"
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        r = raw()
        r["questions"][1]["text"] = "ご希望の日時をお聞かせください。それでは承ります。"   # 頭でなければよい
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)

    def test_do_words_run_to_the_end_of_the_sentence(self):
        """CL-1 の連絡帳 4-72 の答えの (2)・(g)（✅ 2026-11-03 ユーザー (D) の案への答え）：こちらがすることの言葉は文の終わりまで持ち、終わりは
        「させていただきます」か「承ります」だけ（会話の側は「それでは（言葉）ね？」で言う）。「。」で終える・途中で止める・へりくだりを重ねるは止める
        （決まりは `do_words_ok` の1か所。会話の側の meanings.check の kind "do" と同じ）。"""
        for words in ("予定通りの方向で進めさせていただきます", "当日はお車でお越しということで承ります", "お支払いはカードとさせていただきます"):
            with self.subTest(ok=words):
                r = raw()
                r["questions"][0]["do_words"] = {"する": words}
                self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].do_words, {"する": words})
        for words in ("予定通りの方向で進め", "ご予約をお取りさせていただきます。", "承ります。", "担当者からお電話を差し上げさせていただきます",
                      "ご訪問をお伺いさせていただきます", "させていただきますか", "それではパンフレットをお送りさせていただきます",
                      "当日は、お車でお越しということで承ります", "「青葉ホール」でのご予約を承ります"):
            with self.subTest(ng=words):
                r = raw()
                r["questions"][0]["do_words"] = {"する": words}
                self.assertTrue(any("「させていただきます」か「承ります」" in p for p in problems_of(r)), problems_of(r))
        self.assertEqual(bs.valid_answer_words({"する": "ご予約をお取り", "しない": "ご予約は控えさせていただきます"}, ("する", "しない"),
                                               replies=False, form="do"), {"しない": "ご予約は控えさせていただきます"})   # 取込の AI の答えは合う言葉だけ受ける

    def test_ways_questions_and_the_ways_the_business_can_offer(self):
        """CL-1 の連絡帳 4-91：ご案内の方法を伺う質問か（asks_way。真偽）と、業務の「ご案内できる方法」（offer_ways。業務で1つ・無くてもよい）。
        どの名前も、ご連絡の方法がちょうど1つに当たり（means_in＝会話の側の flowctl.means_in と同じ決まり）、同じ方法を2つ書かない。質問ごとに
        渡すのは `offer_ways_of` の1か所（ご案内の方法を伺う質問だけ）。"""
        r = raw()
        r["questions"][1]["asks_way"] = True
        r["offer_ways"] = ["メール", "LINE", "Zoom"]
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual([s.offer_ways_of(q) for q in s.questions], [(), ("メール", "LINE", "Zoom")])
        self.assertEqual(bs.means_in("ｚｏｏｍ"), ["online"])                     # 大文字・半角にそろえて見る
        for ways, word in ((["パンフレット"], "どれか1つ"), (["メールかLINE"], "2つ以上"), (["メール", "MAIL"], "同じ方法"),
                           ("メール", "名前の並び"), ([" "], "名前の並び")):
            with self.subTest(ways=ways):
                r2 = raw()
                r2["offer_ways"] = ways
                self.assertTrue(any(word in x for x in problems_of(r2)), problems_of(r2))
        r3 = raw()
        r3["questions"][0]["asks_way"] = None
        self.assertTrue(any("asks_way" in x for x in problems_of(r3)))

    def test_doubt_words_are_asking_words_on_wish_questions(self):
        """CL-1 の連絡帳 4-90（✅ 2026-11-01 日本時間20時56分・22時31分 ユーザー）：いまの話の向きを伺う言葉（選択肢 → 言葉。会話の側は「はい、」＋
        「（言葉）ということでしょうか？」で言う）は、事実を伺う質問ではない選ぶ質問の、宣言した選択肢にだけ付く。形の決まりは `doubt_words_ok` の1か所
        （会話の側の meanings.check の doubt と同じ：問いかけの終わり・「ということ」「…こと」・記号・頭のつなぎの言葉を使わない）。"""
        for words in ("ご予約はされない方がいい", "パンフレットはお送りしない方がいい"):
            with self.subTest(ok=words):
                r = raw()
                r["questions"][0]["doubt_words"] = {"しない": words}
                s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
                self.assertEqual((s.questions[0].doubt_words, s.questions[1].doubt_words), ({"しない": words}, {}))
        for words in ("ご予約はされないということ", "ご予約はされないということの方がいい", "ご予約はされないこと", "ご予約はされないでしょうか", "ご予約はされない方がよろしい",
                      "それではご予約はされない方がいい", "ご予約は、されない方がいい", " "):
            with self.subTest(ng=words):
                r = raw()
                r["questions"][0]["doubt_words"] = {"しない": words}
                self.assertTrue(any("いまの話の向きを伺う言葉" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0]["doubt_words"] = {"どちらでも": "どちらでもいい方がいい"}   # 宣言した選択肢だけ
        self.assertTrue(any("いまの話の向きを伺う言葉" in p for p in problems_of(r)), problems_of(r))
        r = raw()
        r["questions"][0].update(asks_fact=True, next=None, final_goal=False, declines=[], doubt_words={"しない": "ご予約はされない方がいい"})
        self.assertTrue(any("事実を伺う質問なので、いまの話の向きを伺う言葉" in p for p in problems_of(r)), problems_of(r))
        self.assertEqual(bs.valid_answer_words({"する": "ご予約をなさるということ", "しない": "ご予約はされない方がいい"}, ("する", "しない"),
                                               replies=False, form="doubt"), {"しない": "ご予約はされない方がいい"})   # 取込の AI の答えは合う言葉だけ

    def test_tell_words_after_the_answer(self):
        """CL-1 の連絡帳 4-72 の (i)（✅ 2026-11-03 日本時間19時台 ユーザー）：その答えのあとに伝える一言（選択肢 → 一言）は、はい／いいえ・決まった
        選択肢の質問の、宣言した選択肢にだけ付く（事実を伺う質問にも付く）。依頼者が書く（無ければ空）。"""
        r = raw()
        r["questions"][0]["tell_words"] = {"する": "○○駐車場をご利用いただけますのでどうぞご利用ください。"}
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.questions[0].tell_words, {"する": "○○駐車場をご利用いただけますのでどうぞご利用ください。"})
        self.assertEqual(s.questions[1].tell_words, {})
        r = raw()
        r["questions"][0].update(asks_fact=True, next=None, final_goal=False, declines=[], tell_words={"する": "当日はお車でお越しください。"})
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)   # 事実を伺う質問にも付く
        for name, change in (("宣言に無い選択肢", lambda r: r["questions"][0].update(tell_words={"どちらでも": "x"})),
                             ("値を聞く質問", lambda r: r["questions"][1].update(tell_words={"答えた": "x"})),
                             ("空の一言", lambda r: r["questions"][0].update(tell_words={"する": " "})),
                             ("表でない", lambda r: r["questions"][0].update(tell_words=["x"]))):
            with self.subTest(name):
                r = raw()
                change(r)
                self.assertTrue(any("その答えのあとに伝える一言" in p for p in problems_of(r)), problems_of(r))

    def test_unclear_none_is_a_declared_choice_of_a_choice_question(self):
        """CL-1 の連絡帳 4-82（✅ 2026-11-04 日本時間20時27分 管理者「(1)…OK」）：ほかに分からない点が無いかを伺う質問の「無い」にあたる答え
        （unclear_none）は、はい／いいえ・決まった選択肢の質問の、宣言した選択肢の1つ（空＝伺う質問ではない）。"""
        r = raw()
        r["questions"][0]["unclear_none"] = "しない"
        self.assertEqual([q.unclear_none for q in bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES,
                                                            ai_reading_needs_person=True).questions], ["しない", ""])
        for qi, bad in ((0, "どちらでも"), (0, "x"), (0, 1), (1, "する"), (1, bs.VALUE_CHOICE)):   # 宣言した選択肢でない・型が違う・値を伺う質問
            with self.subTest(bad=bad):
                r = raw()
                r["questions"][qi]["unclear_none"] = bad
                self.assertTrue(any("unclear_none" in p for p in problems_of(r)), problems_of(r))

    def test_ends_on_answers(self):
        """CL-1 の連絡帳 4-74（✅ 2026-11-04 日本時間2時10分54秒 管理者「OK」）：ここで終える答え（ends_on）は、必須の、はい／いいえ・選択肢の質問の、
        宣言した選択肢と「どちらでも」「どれも要らない」（返りうるとき）だけ。「わからない」「答えたくない」は書けない（先へ進むかが決まらない答え）。"""
        r = raw()
        r["questions"][0].update(ends_on=["しない", bs.NONE_CHOICE])   # Q1 は事実を伺わない選ぶ質問＝「どれも要らない」が返りうる
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].ends_on, ("しない", bs.NONE_CHOICE))
        self.assertEqual(bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[0].ends_on, ("しない",))   # 分岐のシートから
        for name, change, words in (
                ("わからない", lambda r: r["questions"][0].update(ends_on=["わからない"]), "書けません"),
                ("答えたくない", lambda r: r["questions"][0].update(ends_on=["答えたくない"]), "書けません"),
                ("選択肢に無い", lambda r: r["questions"][0].update(ends_on=["たぶん"]), "書けません"),
                ("事実を伺う質問のどちらでも", lambda r: r["questions"][0].update(asks_fact=True, next=None, final_goal=False, declines=[],
                                                                                  ends_on=[bs.ANY_CHOICE]), "書けません"),
                ("必須でない", lambda r: r["questions"][0].update(required=False, ends_on=["しない"]), "必須の"),
                ("値を聞く質問", lambda r: r["questions"][1].update(ends_on=["答えた"]), "必須の、はい／いいえ"),
                ("重なる", lambda r: r["questions"][0].update(ends_on=["しない", "しない"]), "重ならない"),
                ("並びでない", lambda r: r["questions"][0].update(ends_on="しない"), "重ならない")):
            with self.subTest(name):
                r = raw()
                change(r)
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))

    def test_question_ids_must_not_look_like_derived_recording_parts(self):
        """業務の録音の部分の名前は、会話の側が質問の番号やシステムの名前に「_」と何かを続けて作る（日の言い方「_d番号」・つなぎの言葉「_lead_…」・
        その答えのあとに伝える一言「_tell番号」・ほかのご希望を伺う一言「_other」・読みの違い「_v番号」と、その組み合わせ＝CL-1 の 4-71 の (d)・4-72 の
        (i)(k) の (4)）。ほかの質問の番号やシステムの名前に「_」を続けた質問の番号は止める（`_derived_part_problems` の1か所。作り方を写さない）。
        会話の側のシステムの質問 W1・C10 も質問の番号に使えない（4-72 の (k) の (1)）。"""
        def extra(qid):
            r = raw()
            q = copy.deepcopy(r["questions"][1])
            q.update(id=qid, text="ほかにご希望はございますか？", required=False)
            r["questions"].append(q)
            return r
        for qid in ("Q1_d2", "Q1_tell1", "Q1_other", "Q2_tell12", "Q1_lead_deha", "Q1_v1", "Q1_d2_lead_soredeha_v2", "Q1_x",
                    "greeting_v1", "intro_lead_deha"):                # 会話の側が作る形とその組み合わせ・システムの名前の続き（作り方を写さず「_」の続きで決める）
            with self.subTest(qid):
                self.assertTrue(any("録音の部分の名前" in p for p in problems_of(extra(qid))), problems_of(extra(qid)))
        for qid in ("Q3_other", "Q1x", "Q1-2", "Q10"):                         # 無い質問の番号からの形・「_」で続かない番号は取り違えない
            with self.subTest(ok=qid):
                bs.check(extra(qid), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        for qid in ("W1", "C10", "busy"):
            with self.subTest(reserved=qid):
                self.assertTrue(any("会話の側で使う" in p for p in problems_of(extra(qid))), problems_of(extra(qid)))

    def test_a_question_asked_only_after_a_hedged_answer(self):
        """CL-1 の連絡帳 4-71 の (b)（✅ 2026-11-03 ユーザー (3)(4)）：伺う条件の hedged＝前の質問の答えが言い切っていないときだけ伺う。真偽で書き、
        前の質問は、はい／いいえ・決まった選択肢の質問だけ（会話の側が言い直さずに受け取ってこの質問で伺うのは、選択肢の答えだけ）。"""
        r = raw()
        r["questions"].append({"id": "Q3", "text": "日時変更なさいますか？それとも、キャンセルなさいますか？", "ask_about": "日時の変更", "type": "yes_no",
                               "required": False, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None,
                               "asks_fact": False, "do_words": None, "away": None, "intro": None,
                               "ask_if": {"after": "Q1", "answers": ["しない"], "hedged": True}, "approval": False, "final_goal": False,
                               "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False})
        r["questions"][0]["next"] = None
        r["questions"][0]["final_goal"], r["questions"][0]["declines"] = False, []
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.questions[2].ask_if, bs.AskIf("Q1", ("しない",), True))
        for name, change, words in (
                ("真偽でない", lambda r: r["questions"][2]["ask_if"].update(hedged="はい"), "hedged）は true / false"),
                ("無い", lambda r: r["questions"][2]["ask_if"].pop("hedged"), "伺う条件（ask_if）"),
                ("値を伺う前の質問", lambda r: r["questions"][2]["ask_if"].update(after="Q2", answers=["答えた"]),
                 "言い切っていないときだけ伺う（hedged）にできません")):
            with self.subTest(name):
                bad = copy.deepcopy(r)
                change(bad)
                self.assertTrue(any(words in p for p in problems_of(bad)), problems_of(bad))
        ok = copy.deepcopy(r)
        ok["questions"][2]["ask_if"].update(after="Q2", answers=["答えた"], hedged=False)   # 言い切っていないときだけ、でなければ値の質問の後でもよい
        bs.check(ok, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)

    def test_earliest_day_setting(self):
        """CL-1 の連絡帳 4-71 の (c)（✅ 2026-11-03 ユーザー：「業務設定では「3営業日以降」などの設定になるので、それを自動で置き換える仕組みが
        必要です。」）：最短の日（「N営業日以降」）は、質問の文か入る一言に {最短の日} を書いた業務だけが書き、書いた業務は必ず書く。{最短の日} を
        書けるのは質問の文と入る一言だけ（会話の側が置き換えるのはそこだけ）。言い方ごとの録音の名前（質問の番号_d番号）と同じ形の質問の番号は
        使えない。"""
        from datetime import date

        def slotted() -> dict:
            r = raw()
            r["questions"][1]["text"] = f"（{bs.DAY_SLOT}なら対応できますが、）いつ頃がよろしいでしょうか？"
            r["earliest"] = {"business_days": 3, "closed_weekdays": ["sat", "sun"], "closed_on_holidays": True,
                             "closed_dates": ["2026-12-30", "2026-12-29"]}
            return r
        s = bs.check(slotted(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.earliest, bs.Earliest(3, ("sat", "sun"), True, (date(2026, 12, 29), date(2026, 12, 30))))
        self.assertIsNone(bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).earliest)
        r = slotted()
        r["questions"][1]["text"] = "ご希望の日時をお聞かせください。"
        r["questions"][1]["intro"] = f"{bs.DAY_SLOT}からのご案内になります。"   # 入る一言に書いてもよい
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).earliest.business_days, 3)

        def extra_question(qid):
            def change(r):
                q = copy.deepcopy(r["questions"][1])
                q.update(id=qid, text="ほかにご希望はございますか？", required=False)
                r["questions"].append(q)
            return change
        for name, change, words in (
                ("最短の日が無い", lambda r: r.pop("earliest"), "最短の日（earliest：「N営業日以降」）を書きます"),
                ("使わない最短の日", lambda r: r["questions"][1].update(text="ご希望の日時をお聞かせください。"), "使われない"),
                ("聞くことに書いた", lambda r: r["questions"][1].update(ask_about=f"{bs.DAY_SLOT}の日時"), "質問 Q2 の ask_about に {最短の日}"),
                ("用件の一文に書いた", lambda r: r["caller"].update(purpose_line=f"{bs.DAY_SLOT}のご案内です。"), "業務設定の caller"),
                ("想定問答に書いた", lambda r: r.update(answers=[{"name": "最短", "text": f"{bs.DAY_SLOT}です。"}]), "業務設定の answers"),
                ("鍵が足りない", lambda r: r["earliest"].pop("closed_dates"), "最短の日（earliest）は"),
                ("数が負", lambda r: r["earliest"].update(business_days=-1), "営業日の数"),
                ("数が大きすぎる", lambda r: r["earliest"].update(business_days=bs.EARLIEST_MAX_DAYS + 1), "営業日の数"),
                ("数が真偽", lambda r: r["earliest"].update(business_days=True), "営業日の数"),
                ("数が文字", lambda r: r["earliest"].update(business_days="3"), "営業日の数"),
                ("曜日の名前でない", lambda r: r["earliest"].update(closed_weekdays=["土"]), "休みの曜日"),
                ("曜日が重なる", lambda r: r["earliest"].update(closed_weekdays=["sat", "sat"]), "休みの曜日"),
                ("毎日休み", lambda r: r["earliest"].update(closed_weekdays=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
                 "営業日がありません"),
                ("祝日が真偽でない", lambda r: r["earliest"].update(closed_on_holidays="はい"), "祝日を休みにするか"),
                ("休業日の形", lambda r: r["earliest"].update(closed_dates=["2026/12/30"]), "日にち（YYYY-MM-DD）ではありません"),
                ("休業日が無い日", lambda r: r["earliest"].update(closed_dates=["2026-02-30"]), "日にち（YYYY-MM-DD）ではありません"),
                ("休業日が詰めた形", lambda r: r["earliest"].update(closed_dates=["20261230"]), "日にち（YYYY-MM-DD）ではありません"),
                ("休業日が重なる", lambda r: r["earliest"].update(closed_dates=["2026-12-30", "2026-12-30"]), "同じ日が2つ"),
                ("休業日が並びでない", lambda r: r["earliest"].update(closed_dates="2026-12-30"), "休業日（closed_dates）は"),
                ("録音の名前と同じ形の番号", extra_question("Q2_d1"), "録音の部分の名前")):
            with self.subTest(name):
                r = slotted()
                change(r)
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))
        r = slotted()
        extra_question("Q2x")(r)                                           # 「_」で続かなければ取り違えない
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)

    def test_earliest_day_counts_business_days(self):
        """最短の日の数え方（`Earliest.day` の1か所）：電話の日の次の日から数えて N 番目の営業日（0 なら、電話の日から数えて最初の営業日）。
        営業日は、休みの曜日・休業日・（休みにするなら）祝日でない日。1年先までに無ければ ValueError。"""
        from datetime import date, timedelta
        mon, holiday = date(2026, 11, 2), date(2026, 11, 3)                 # 月曜日・その次の日を祝日に
        is_holiday = lambda d: d == holiday                                 # noqa: E731
        e = bs.Earliest(3)
        self.assertEqual(e.day(mon, is_holiday), date(2026, 11, 6))         # 火（祝日）を飛ばして 水・木・金
        self.assertEqual(e.day(mon, lambda d: False), date(2026, 11, 5))
        self.assertEqual(bs.Earliest(3, closed_on_holidays=False).day(mon, is_holiday), date(2026, 11, 5))   # 祝日も営業日
        self.assertEqual(bs.Earliest(1).day(date(2026, 11, 6), is_holiday), date(2026, 11, 9))   # 金曜日の電話 ⇒ 土日を飛ばして月曜日
        self.assertEqual(bs.Earliest(0).day(mon, is_holiday), mon)          # 当日から（電話の日が営業日）
        self.assertEqual(bs.Earliest(0).day(date(2026, 11, 7), is_holiday), date(2026, 11, 9))   # 土曜日の電話 ⇒ 月曜日
        self.assertEqual(bs.Earliest(0).day(holiday, is_holiday), date(2026, 11, 4))   # 祝日の電話 ⇒ 次の営業日
        self.assertEqual(bs.Earliest(1, ("sun",)).day(date(2026, 11, 6), is_holiday), date(2026, 11, 7))   # 土曜日も営業日
        self.assertEqual(bs.Earliest(1, closed_dates=(date(2026, 11, 4),)).day(holiday, is_holiday), date(2026, 11, 5))   # 休業日
        self.assertEqual(bs.Earliest(2, ()).day(date(2026, 11, 6), lambda d: False), date(2026, 11, 8))   # 休みの曜日が無い
        with self.assertRaises(ValueError):                                 # 1年先まで営業日が無い
            bs.Earliest(1, ()).day(mon, lambda d: True)
        self.assertEqual(bs.Earliest(1, ()).day(mon, lambda d: d < mon + timedelta(days=366)), mon + timedelta(days=366))

    def test_many_optional_follow_ups_check_quickly_and_still_find_skips(self):
        """「はい ⇒ 詳しく聞く、いいえ ⇒ 次へ」が続く業務（40問）でも、確かめはすぐ終わる（前は質問の数に対して何倍にも
        増え、36問で15秒かかった＝2026-10-16 の見直し I2）。必須を飛ばす道は、これまでどおり見つける。"""
        import time
        r = raw()
        qs = [r["questions"][0]]
        n = 20
        for i in range(n):
            ask, detail = f"Ask{i}", f"Detail{i}"   # 質問の番号は、システムの質問の番号（D1 など＝RESERVED_IDS）と重ならない名前
            nxt = f"Ask{i + 1}" if i + 1 < n else "Q2"
            qs.append({"id": ask, "text": "ご利用はございますか？", "ask_about": "ご利用", "type": "choice", "required": False,
                       "choices": ["ある", "ない"], "next": {"ある": detail, "*": nxt}, "unit": None, "meanings": None, "replies": None,
                       "asks_fact": True, "do_words": None, "away": None, "intro": None, "ask_if": None, "approval": False, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False})
            qs.append({"id": detail, "text": "詳しくお聞かせください。", "ask_about": "詳しく", "type": "free_text",
                       "required": False, "choices": None, "next": {"*": nxt}, "unit": None, "meanings": None, "replies": None,
                       "asks_fact": True, "do_words": None, "away": None, "intro": None, "ask_if": None, "approval": False, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False})
        qs[0] = dict(qs[0], next={"しない": "END", "*": "Ask0"})
        qs.append(r["questions"][1])
        r["questions"] = qs
        r["rules"]["handling"] = {"*": r["rules"]["handling"]["Q1"]}   # 必須の質問すべてに当てる扱い（置き換えで必須になる質問にも当たる）
        started = time.monotonic()
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertLess(time.monotonic() - started, 2.0)
        by = {q.id: q for q in s.questions}
        self.assertEqual([by[f"Detail{i}"].ask_if for i in range(n)], [bs.AskIf(f"Ask{i}", ("ある",)) for i in range(n)])
        qs[-2] = dict(qs[-2], next={"*": "END"})   # 最後の詳しく聞く質問のあとで終わる＝続きの終わり（Ask19 の「ある」で終わる）
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual({q.id: q.ends_on for q in s.questions if q.ends_on}, {"Q1": ("しない",), f"Ask{n - 1}": ("ある",)})

    def test_malformed_values_are_problems_not_crashes(self):
        """画面・抜き出しの入口から形の違う値が来ても、処理が落ちずに問題として並べる（2026-10-16 の見直し I9）。"""
        r = raw(); r["questions"][1]["choices"] = 5
        self.assertTrue(any("選択肢" in p for p in problems_of(r)))
        r = raw(); r["questions"][1]["type"] = ["x"]
        self.assertTrue(any("型" in p for p in problems_of(r)))

    def test_names_used_by_the_conversation_cannot_be_question_ids(self):
        """会話の側で使う名前（かけ直しの質問 C1・C2、名乗りや締めの録音の名前）は、質問の番号に使えない（同じ名前だと、かけ直しの
        答えや録音を取り違える。見直し9）。"""
        for name in sorted(bs.RESERVED_IDS):
            with self.subTest(name=name):
                r = raw(); r["questions"][1]["id"] = name
                self.assertTrue(any("会話の側で使う" in p for p in problems_of(r)), problems_of(r))
        # 想定問答の答えの録音の部分（answer_ で始まる名前）と同じ依頼者の単位の組に入るので、その頭の名前も使えない（見直し17d）
        r = raw(); r["questions"][1]["id"] = "answer_Q2"
        self.assertTrue(any("会話の側で使う" in p for p in problems_of(r)), problems_of(r))
        # 契約者の単位の録音の名前（締め closing など）は別の組なので、ぶつからない＝使える（見直し17d）
        for name in ("closing", "closing_declined", "talk_osewa"):
            with self.subTest(name=name):
                r = raw(); r["questions"][1]["id"] = name; r["questions"][0]["next"] = {"しない": "END", "*": name}
                self.assertFalse(any("会話の側で使う" in p for p in problems_of(r)), problems_of(r))
        r = raw(); r["questions"][1]["id"] = "Q-2"; r["questions"][0]["next"] = {"しない": "END", "*": "Q-2"}
        self.assertFalse(any("会話の側で使う" in p for p in problems_of(r)))

    def test_each_problem_is_found(self):
        cases = []
        r = raw(); r["questions"][0]["next"] = {"しない": "Q9", "*": "Q2"}; cases.append((r, "分岐先 Q9 がありません"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "する": "Q2"}; cases.append((r, "受け皿がありません"))
        r = raw(); r["questions"][1]["next"] = {"*": "Q1"}; cases.append((r, "前へ戻る分岐は書けません"))
        r = raw(); r["questions"][0]["next"] = {"*": "END"}; cases.append((r, "質問 Q2 へは、どこからも行きません"))
        r = raw(); r["names"][2]["reading_by"] = "ai"; cases.append((r, "AI が決めた読みです。検証モードでは、契約者が確かめてから"))
        r = raw(); r["names"][2]["reading_by"] = None; cases.append((r, "読みを決めたもの"))
        r = raw(); r["names"][2]["reading"] = "テストブツリュウ"; cases.append((r, "ひらがなで"))      # 持つのはひらがな
        r = raw(); r["names"][2].update(reading=None, reading_by=None, candidates=["中島"]); cases.append((r, "ひらがなです"))
        r = raw(); r["names"][2]["candidates"] = ["てすと"]; cases.append((r, "決まった読みには候補を付けません"))
        r = raw(); r["names"].append(name("テスト物流", "てすとものながれ", "script")); cases.append((r, "同じ表記が2つ"))
        r = raw(); r["caller"]["company"]["reading"] = "べつのよみ"; cases.append((r, "名前の一覧の読みと違います"))
        r = raw(); del r["names"][1]; cases.append((r, "名乗りの担当者名「山田」が、名前の一覧（名乗り）にありません"))
        # 担当者名の読みは、入口で必ず入れる（2026-10-16 ユーザー決定＝案C。読みが決まらない・AI の読みは通さない）
        r = raw(); r["names"][1].update(reading=None, reading_by=None, candidates=["やまだ"], likely="やまだ")
        r["caller"]["person"]["reading"] = None; cases.append((r, "担当者名の読みは必ず入れます"))
        r = raw(); r["names"][1]["reading_by"] = "ai"; cases.append((r, "担当者名の読みは必ず入れます"))
        r = raw(); r["questions"][1]["unit"] = "人"; cases.append((r, "単位は、数の型（number）の質問だけ"))
        r = raw(); r["questions"][0]["choices"] = ["する", "わからない"]; cases.append((r, "システムが足す答え"))
        r = raw(); r["questions"][0]["choices"] = ["する", "-"]; cases.append((r, "出力の印"))
        r = raw(); r["questions"][1]["type"] = "color"; cases.append((r, "型の一覧にありません"))
        r = raw(); r["questions"][1]["choices"] = ["はい"]; cases.append((r, "決まっています"))
        r = raw(); r["target_person"] = {"kind": "anyone", "role": "社長"}; cases.append((r, "役職を書きません"))
        # 「ご担当者」だけ（何の担当か無い）の役職は使えない（✅ 2026-11-05 20時29分50秒 ユーザー。business_settings.role_problem の1か所）
        for bare in ("ご担当者", "ご担当者様", "ご担当の方", "担当者", "担当"):
            r = raw(); r["target_person"] = {"kind": "role", "role": bare}; cases.append((r, "何のご担当か分からない"))
        r = raw(); r["target_class"] = "individual"; cases.append((r, "お客様が個人の業務は、まだ選べません"))
        r = raw(); r["target_class"] = None; cases.append((r, "お客様の区分（target_class）は"))
        r = raw(); r["questions"][0]["next"] = {"しない": "END", "*": "Q2", "やめる": "END"}
        cases.append((r, "分岐の答え「やめる」は、その質問の答えにありません"))
        r = raw()
        for q in r["questions"]:
            q["required"] = False
            q["next"] = None   # 分岐のシートの「→ 終わり」がある質問は、置き換えで必須になる
        cases.append((r, "必須の質問が1つもありません"))
        for r, expected in cases:
            with self.subTest(expected):
                self.assertTrue(any(expected in p for p in problems_of(r)), problems_of(r))

    def test_no_false_reason_when_a_question_cannot_be_read(self):
        r = raw(); r["questions"][1]["type"] = "color"                     # Q2 が読めない
        got = problems_of(r)
        self.assertTrue(any("型の一覧にありません" in p for p in got))
        self.assertFalse(any("分岐先" in p or "どこからも" in p for p in got), got)   # 読めない質問の巻き添えの理由を出さない

    def test_all_problems_are_listed_at_once(self):
        r = raw(); r["purpose"] = ""; r["voice"] = ""; r["names"][2]["reading_by"] = "ai"
        self.assertGreaterEqual(len(problems_of(r)), 3)                # 1つずつではなく、まとめて示す

    def test_system_answers_may_skip(self):
        """「わからない」「答えたくない」で次へ進むのは、理由×扱いで決まる（飛ばしでは止めない）。"""
        r = raw()
        r["questions"][0]["next"] = {"しない": "END", "わからない": "END", "*": "Q2"}
        bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)

    def test_ai_reading_and_unit(self):
        """AI が決めた読みは、フルオートでは契約者が確かめずに通る。読みが決まらない名前も、検証モードでも通る（会話の中で確かめる。
        2026-10-16 ユーザー）。自社名の読みは決まらなくてもよい。数の質問は単位を持てる。"""
        r = raw(); r["names"][2]["reading_by"] = "ai"
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=False)
        self.assertEqual(s.names[2].reading_by, "ai")
        r = raw(); r["names"][2].update(reading=None, reading_by=None, candidates=["てすとぶつりゅう", "てすともつりゅう"],
                                        likely="てすとぶつりゅう")
        r["names"][0].update(reading=None, reading_by=None); r["caller"]["company"]["reading"] = None
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.names[2].reading, s.names[2].likely, s.company.reading), (None, "てすとぶつりゅう", None))
        r = raw(); r["questions"][1].update(type="number", unit="名")
        self.assertEqual(bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions[1].unit, "名")


class BranchFlow(unittest.TestCase):
    """分岐のシートの置き換え（`business_settings._branch_flow`。CL-1 の連絡帳 4-78・CL-2 の連絡帳 4-206）：会話は分岐の表を読まないので、
    取込も画面も置いた版も、確かめの1か所で、管理者が決めた形（ここで終える答え・この答えのときだけ伺う・最終目的の断り）に置き換える。
    人に伺わない（表せない形だけを問題にする）。"""

    @staticmethod
    def book(*specs) -> dict:
        """質問の並び（番号・型・選択肢・分岐・目的から遠ざかる向き）から業務設定を作る。理由×扱いは必須の質問すべてに当てる形。"""
        r = raw()
        qs = []
        for qid, kind, choices, nxt, away in specs:
            qs.append({"id": qid, "text": "ご希望はございますでしょうか？", "ask_about": "ご希望", "type": kind, "required": True,
                       "choices": list(choices) if kind == "choice" else None, "next": nxt, "unit": None, "meanings": None,
                       "replies": None, "asks_fact": kind != "choice" or not away, "do_words": None, "away": list(away) or None,
                       "intro": None, "ask_if": None, "approval": False, "final_goal": False, "declines": [], "tell_words": {},
                       "ends_on": [], "unclear_none": "", "doubt_words": {}, "asks_way": False, "unclear_none": "", "doubt_words": {}, "asks_way": False})
        r["questions"] = qs
        r["rules"]["handling"] = {"*": r["rules"]["handling"]["Q1"]}
        return r

    def flow(self, r):
        return {q.id: q for q in bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True).questions}

    def test_end_is_a_stop_answer_or_a_goal_refusal(self):
        """「→ 終わり」：どの答えでも（目的から遠ざかる向きでも）ここで終える答え（断りではない）。最終目的は依頼者が列で決めたものだけで、
        「→ 終わり」から推し量らない（✅ 11-04 日本時間7時台 管理者・CL-1 の連絡帳 4-79）。先へ進むかを決める質問なので必須に。「わからない」
        「答えたくない」の行き先は使わない。"""
        r = self.book(("Q1", "yes_no", (), {"いいえ": "END", "わからない": "END", "*": "Q2"}, ()),
                      ("Q2", "choice", ("来店する", "来店しない"), {"来店しない": "END", "*": "Q3"}, ("来店しない",)),
                      ("Q3", "free_text", (), None, ()))
        r["questions"][0]["required"] = False
        q = self.flow(r)
        self.assertEqual((q["Q1"].ends_on, q["Q1"].final_goal, q["Q1"].required), (("いいえ",), False, True))
        self.assertEqual((q["Q2"].ends_on, q["Q2"].final_goal, q["Q2"].declines), (("来店しない",), False, ()))
        self.assertEqual([q[x].ask_if for x in ("Q1", "Q2", "Q3")], [None, None, None])
        # 「どれも要らない → 終わり」も、ここで終える答え（会話の側は、先にほかのご希望を1回伺う）
        r["questions"][1]["next"] = {"来店しない": "END", bs.NONE_CHOICE: "END", "*": "Q3"}
        self.assertEqual(self.flow(r)["Q2"].ends_on, ("来店しない", bs.NONE_CHOICE))

    def test_skips_become_conditions_without_none(self):
        """飛ばし：あいだの質問の伺う条件に、飛ばさない答え（宣言した選択肢と「どちらでも」）を入れる。「どれも要らない」・システムの答えは入れない。
        飛ばされる質問が続けて2つなら、どちらも同じ元の質問の条件。"""
        r = self.book(("Q1", "choice", ("電話", "メール", "訪問"), {"訪問": "Q4", "*": "Q2"}, ("訪問",)),
                      ("Q2", "free_text", (), None, ()), ("Q3", "free_text", (), None, ()), ("Q4", "free_text", (), None, ()))
        q = self.flow(r)
        self.assertEqual([q[x].ask_if for x in ("Q2", "Q3", "Q4")],
                         [bs.AskIf("Q1", ("電話", "メール", "どちらでも")), bs.AskIf("Q1", ("電話", "メール", "どちらでも")), None])
        self.assertEqual((q["Q1"].ends_on, q["Q1"].final_goal), ((), False))
        # 入れ子の飛ばし：Q1 で飛ばす範囲の中で Q2 がさらに飛ばす＝Q3 の条件は近いほう（Q2）。Q2 を伺わなければ Q3 も伺わない
        r = self.book(("Q1", "yes_no", (), {"いいえ": "Q4", "*": "Q2"}, ()), ("Q2", "yes_no", (), {"いいえ": "Q4", "*": "Q3"}, ()),
                      ("Q3", "free_text", (), None, ()), ("Q4", "free_text", (), None, ()))
        q = self.flow(r)
        self.assertEqual((q["Q2"].ask_if, q["Q3"].ask_if), (bs.AskIf("Q1", ("はい",)), bs.AskIf("Q2", ("はい",))))

    def test_a_follow_up_then_end_ends_on_the_earlier_answer(self):
        """続きの終わり（「お電話 → 時間帯 → 終わり」）：続きの質問は伺う条件で残り、元の質問の答えが、ここで終える答えになる（続きのある答えは、
        遠ざかる向きでも、続き＝業務の言葉を伺ってから終える）。"""
        r = self.book(("Q1", "choice", ("電話", "メール"), {"電話": "Q2", "*": "Q3"}, ("メール",)),
                      ("Q2", "free_text", (), {"*": "END"}, ()), ("Q3", "free_text", (), None, ()))
        q = self.flow(r)
        self.assertEqual((q["Q2"].ask_if, q["Q1"].ends_on, q["Q1"].final_goal, q["Q3"].ask_if),
                         (bs.AskIf("Q1", ("電話",)), ("電話",), False, None))
        # 続きのあとで終わる答えは、続きより後ろの質問の伺う条件に入れない（入れると、終えたあとも会話の側が伺う＝伺う条件をたどる質問は止めない）
        r = self.book(("Q1", "choice", ("電話", "メール", "訪問"), {"電話": "Q2", "メール": "Q3", "*": "Q4"}, ()),
                      ("Q2", "free_text", (), {"*": "END"}, ()), ("Q3", "free_text", (), None, ()), ("Q4", "free_text", (), None, ()))
        q = self.flow(r)
        self.assertEqual((q["Q1"].ends_on, q["Q2"].ask_if, q["Q3"].ask_if), (("電話",), bs.AskIf("Q1", ("電話",)), bs.AskIf("Q1", ("メール",))))

    def test_forms_the_conversation_cannot_express_are_problems(self):
        """表せない形だけを問題にする（人に決めさせるためではなく、会話の形が無いため）：前へ戻る・同じ質問を2つの元の質問が飛ばす（すでに伺う条件が
        ある）・元の質問を通らずに来る道・どの答えでも伺わない・続きでない質問のあとの終わり・どこからも行かない質問。"""
        cases = (
            ("前へ戻る", (("Q1", "yes_no", (), None, ()), ("Q2", "yes_no", (), {"いいえ": "Q1", "*": "Q3"}, ()), ("Q3", "free_text", (), None, ())),
             "前へ戻る分岐は書けません"),
            ("書いた伺う条件と飛ばし", (("Q1", "yes_no", (), {"いいえ": "Q3", "*": "Q2"}, ()), ("Q2", "free_text", (), None, ()), ("Q3", "free_text", (), None, ())),
             "伺う条件（この答えのときだけ伺う）がもうある"),
            ("元の質問を通らずに来る", (("Q1", "yes_no", (), {"いいえ": "Q3", "*": "Q2"}, ()), ("Q2", "yes_no", (), {"いいえ": "Q4", "*": "Q3"}, ()),
                                       ("Q3", "free_text", (), None, ()), ("Q4", "free_text", (), None, ())), "を通らずに来る道がある"),
            ("どの答えでも伺わない", (("Q1", "choice", ("A", "B"), {"A": "END", bs.NONE_CHOICE: "Q2", "*": "Q3"}, ("A",)),
                                      ("Q2", "free_text", (), None, ()), ("Q3", "free_text", (), None, ())), "どの答えでも伺わない"),
            ("いつも伺う質問のあとで終わる", (("Q1", "free_text", (), {"*": "END"}, ()), ("Q2", "free_text", (), None, ())), "どこからも行きません"),
            ("続きでない質問のあとで終わる", (("Q1", "choice", ("A", "B", "C"), {"A": "Q4", "B": "Q2", "*": "Q3"}, ()), ("Q2", "free_text", (), {"*": "Q3"}, ()),
                                            ("Q3", "free_text", (), {"*": "END"}, ()), ("Q4", "free_text", (), None, ())), "あとで終わる分岐を、会話の形で表せません"),
        )
        for name, specs, words in cases:
            with self.subTest(name):
                r = self.book(*specs)
                if name == "書いた伺う条件と飛ばし":
                    r["questions"][1]["ask_if"] = {"after": "Q1", "answers": ["はい"], "hedged": False}
                self.assertTrue(any(words in p for p in problems_of(r)), problems_of(r))

    def test_handling_for_all_required_reaches_questions_made_required(self):
        """理由×扱いの「必須の質問すべて」（取込の「全部」の行）は、置き換えで必須になった質問にも当たる（業務の決まりの確かめが広げる1か所）。
        質問ごとの行は、答え・理由ごとにそれより勝つ。「すべて」も質問ごとの行も無い必須の質問は問題。"""
        r = self.book(("Q1", "yes_no", (), {"いいえ": "END", "*": "Q2"}, ()), ("Q2", "free_text", (), None, ()))
        r["questions"][0]["required"] = False
        r["rules"]["handling"]["Q2"] = {"dont_know": {"undecided": {"kind": "ask_when"}}}
        s = bs.check(r, standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        rules = business_rules.check(UUID(int=0), tuple(q.id for q in s.questions if q.required), r["rules"])
        self.assertEqual((rules.handling[("Q1", "dont_know", "undecided")].kind, rules.handling[("Q2", "dont_know", "undecided")].kind,
                          rules.handling[("Q2", "dont_know", "not_in_charge")].kind), ("retry", "ask_when", "retry"))
        r["rules"]["handling"] = {"Q2": r["rules"]["handling"]["*"]}
        self.assertTrue(any("必須の質問ごとに1つずつ要ります" in p for p in problems_of(r)), problems_of(r))


class Versions(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work, actor=OPERATOR):
        return self.w.a.in_client(actor, self.s["tenant"], self.s["client"], work)

    def test_save_load_restore_and_assign(self):
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual((got.company.text, [q.id for q in got.questions], got.rules.required),
                         ("株式会社テスト物流", ["Q1", "Q2"], ("Q1", "Q2")))
        rows = self.tx(lambda tx: tx.all("SELECT question_id, position, type_name, required FROM salescall.questions"
                                         " WHERE version_id = %s ORDER BY position", (v,)))
        self.assertEqual([(r["question_id"], r["type_name"]) for r in rows], [("Q1", "choice"), ("Q2", "datetime")])
        back = self.tx(lambda tx: bs.restore(tx, v))                    # 前の版へ戻す＝同じ中身の新しい版
        self.assertNotEqual(back, v)
        self.assertEqual(self.tx(lambda tx: bs.load(tx, back)).raw, got.raw)
        outcome = self.tx(lambda tx: bs.assign(tx, self.s["run"], back, reason="試験：新しい版に切り替え"))
        self.assertEqual(outcome, "mock_needed")                       # 依頼者で最初の模擬通話を待つ
        self.assertEqual(self.tx(lambda tx: tx.one("SELECT salescall.run_current_version(%s) AS v",
                                                   (self.s["run"],))["v"]), back)

    def test_a_version_saved_before_asks_fact_is_read_as_fact_questions(self):
        """「事実を伺う質問か」（asks_fact。2026-11-01 に足した）の無い、前に置いた版も読める（版の中身は書き換えられないので、読む側の
        1か所 `_current_shape` で今の形に直す）。無い質問は真（事実を伺う質問＝取込で決まらないときと同じ側）。前の版へ戻した新しい版は、
        今の形で置かれる（⛔ 見直し28：前は、足す前に置いた版が読めず、その版の実施回はかけられなかった）。"""
        from unittest import mock
        old_content = bs._content

        def before_asks_fact(s):   # 足す前の形（あとで足した do_words・intro＝4-65、ask_if・approval＝4-66、away＝4-71 も無い）
            c = old_content(s)
            return dict(c, questions=[{k: v for k, v in q.items()
                                       if k not in ("asks_fact", "do_words", "away", "intro", "ask_if", "approval")}
                                      for q in c["questions"]])

        with mock.patch.object(bs, "_content", before_asks_fact):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        stored = self.tx(lambda tx: tx.one("SELECT content FROM salescall.business_contents WHERE version_id = %s", (v,)))
        self.assertFalse(any("asks_fact" in q for q in stored["content"]["questions"]))   # 前の形のまま置いてある
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual([(q.asks_fact, q.do_words, q.away, q.intro, q.ask_if, q.approval) for q in got.questions],
                         [(True, {}, (), None, None, False), (True, {}, (), None, None, False)])
        self.assertEqual(self.tx(lambda tx: bs.ask_ifs(tx, v)), {})
        back = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
        self.assertEqual([(q["asks_fact"], q["do_words"], q["intro"]) for q in back.raw["questions"]],
                         [(True, {}, None), (True, {}, None)])

    def test_a_version_saved_before_tell_words_and_full_do_words(self):
        """その答えのあとに伝える一言（tell_words。2026-11-03 に足した）の無い前の版は、無い（空）として読む。文の終わりまで持たない前の形の
        こちらがすることの言葉（「ご予約をお取り」＝`do_words_ok` に合わない）は、無いとして読む（会話の側は「それでは（言葉）ね？」で言うので、
        そのままでは文にならない＝意味の言葉の言い方になる）。合う言葉はそのまま。前の版へ戻した新しい版は、今の形で置かれる（読む側の1か所
        `_current_shape`）。"""
        from unittest import mock
        old_content = bs._content
        r = raw()
        r["questions"][0]["do_words"] = {"する": "ご予約をお取りさせていただきます", "しない": "ご予約は控えさせていただきます"}

        def before(s):
            c = old_content(s)
            qs = [{k: v for k, v in q.items() if k != "tell_words"} for q in c["questions"]]
            qs[0]["do_words"] = {"する": "ご予約をお取り", "しない": "ご予約は控えさせていただきます"}
            return dict(c, questions=qs)
        with mock.patch.object(bs, "_content", before):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        stored = self.tx(lambda tx: tx.one("SELECT content FROM salescall.business_contents WHERE version_id = %s", (v,)))
        self.assertEqual((stored["content"]["questions"][0]["do_words"]["する"], "tell_words" in stored["content"]["questions"][0]),
                         ("ご予約をお取り", False))                          # 前の形のまま置いてある
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual([(q.do_words, q.tell_words) for q in got.questions],
                         [({"しない": "ご予約は控えさせていただきます"}, {}), ({}, {})])
        back = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
        self.assertEqual([(q["do_words"], q["tell_words"]) for q in back.raw["questions"]],
                         [({"しない": "ご予約は控えさせていただきます"}, {}), (None, {})])   # 書いていない質問は書いたまま（None）

    def test_a_version_saved_before_ends_on_reads_it_as_empty(self):
        """ここで終える答え（ends_on。2026-11-04 に足した）の無い前の版は、無い（空）として読む（読む側の1か所 `_current_shape`）。"""
        from unittest import mock
        old_content = bs._content

        def before(s):
            c = old_content(s)
            return dict(c, questions=[{k: v for k, v in q.items() if k != "ends_on"} for q in c["questions"]])
        with mock.patch.object(bs, "_content", before):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.assertEqual([q.ends_on for q in self.tx(lambda tx: bs.load(tx, v)).questions], [("しない",), ()])   # 分岐のシートの置き換えだけ

    def test_a_version_saved_before_unclear_none_reads_it_as_empty(self):
        """ほかに分からない点は無いにあたる答え（unclear_none。2026-11-05 に足した）の無い前の版は、無い（空＝伺う）として読む（`_current_shape`）。"""
        from unittest import mock
        old_content = bs._content

        def before(s):
            c = old_content(s)
            return dict(c, questions=[{k: v for k, v in q.items() if k != "unclear_none"} for q in c["questions"]])
        with mock.patch.object(bs, "_content", before):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.assertEqual([q.unclear_none for q in self.tx(lambda tx: bs.load(tx, v)).questions], ["", ""])

    def test_a_version_saved_before_doubt_words_reads_them_as_empty(self):
        """いまの話の向きを伺う言葉（doubt_words。2026-11-05 に足した）の無い前の版は、無い（空＝念のための確かめ）として読む（`_current_shape`）。"""
        from unittest import mock
        old_content = bs._content

        def before(s):
            c = old_content(s)
            return dict(c, questions=[{k: v for k, v in q.items() if k != "doubt_words"} for q in c["questions"]])
        with mock.patch.object(bs, "_content", before):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.assertEqual([q.doubt_words for q in self.tx(lambda tx: bs.load(tx, v)).questions], [{}, {}])

    def test_a_version_saved_before_ways_reads_them_as_not_asking(self):
        """ご案内の方法を伺う質問か（asks_way。2026-11-06 に足した＝CL-1 の連絡帳 4-91）の無い前の版は、偽として読む（`_current_shape`）。"""
        from unittest import mock
        old_content = bs._content

        def before(s):
            c = old_content(s)
            return dict(c, questions=[{k: v for k, v in q.items() if k != "asks_way"} for q in c["questions"]])
        with mock.patch.object(bs, "_content", before):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.assertEqual([q.asks_way for q in self.tx(lambda tx: bs.load(tx, v)).questions], [False, False])

    def test_a_version_saved_before_hedged_conditions_reads_them_as_always_asked(self):
        """伺う条件に「言い切っていないときだけ伺うか」（hedged。2026-11-03 に足した＝CL-1 の連絡帳 4-71 の (b)）が無い前の版の質問は、偽（伺う
        答えならいつも伺う＝足す前と同じ）として読む（読む側の1か所 `_current_shape`）。前の版へ戻した新しい版は、今の形で置かれる。"""
        from unittest import mock
        old_content = bs._content
        r = raw()
        r["questions"][0].update(next=None, final_goal=False, declines=[])
        r["questions"][1].update(required=False, ask_if={"after": "Q1", "answers": ["する"], "hedged": False})
        r["rules"] = rules_raw(required=("Q1",))

        def before_hedged(s):
            c = old_content(s)
            return dict(c, questions=[dict(q, ask_if={k: v for k, v in q["ask_if"].items() if k != "hedged"}) if q["ask_if"] else q
                                      for q in c["questions"]])
        with mock.patch.object(bs, "_content", before_hedged):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        stored = self.tx(lambda tx: tx.one("SELECT content FROM salescall.business_contents WHERE version_id = %s", (v,)))
        self.assertEqual(stored["content"]["questions"][1]["ask_if"], {"after": "Q1", "answers": ["する"]})   # 前の形のまま置いてある
        self.assertEqual(self.tx(lambda tx: bs.load(tx, v)).questions[1].ask_if, bs.AskIf("Q1", ("する",), False))
        self.assertEqual(self.tx(lambda tx: bs.ask_ifs(tx, v)), {"Q2": bs.AskIf("Q1", ("する",), False)})
        back = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
        self.assertEqual(back.raw["questions"][1]["ask_if"], {"after": "Q1", "answers": ["する"], "hedged": False})
        r["questions"][1]["ask_if"]["hedged"] = True                        # 今の形の真は、そのまま読む
        r["questions"][0]["asks_fact"] = False
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        self.assertEqual(self.tx(lambda tx: bs.ask_ifs(tx, v)), {"Q2": bs.AskIf("Q1", ("する",), True)})

    def test_day_of_call_gives_the_earliest_day_from_the_calendar(self):
        """会話の側へ渡す最短の日と電話の日（`day_of_call` の1か所。CL-1 の連絡帳 4-71 の (c)）：電話の日は、かける時刻の日本時間の日付。
        最短の日は、その版の「N営業日以降」を、祝日の暦（core/calendar.py）で数えたもの。最短の日を書いていない業務は None。暦の範囲の外は
        判断できない（CalendarOutOfRange）。"""
        from datetime import date, datetime, timezone
        from salescall.core import calendar
        from tests.test_gate import calendar_csv
        owner_access().in_system(OPERATOR, lambda tx: calendar.load_official_csv(
            tx, calendar_csv([2098, 2099], holidays=[date(2098, 11, 4)])))
        r = raw()
        r["questions"][1]["text"] = f"{bs.DAY_SLOT}であれば承れますが、ご希望の日時をお聞かせください。"
        r["earliest"] = {"business_days": 2, "closed_weekdays": ["sat", "sun"], "closed_on_holidays": True, "closed_dates": ["2098-11-05"]}
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        at = datetime(2098, 11, 2, 15, 30, tzinfo=timezone.utc)            # 日本時間では 11月3日（月曜日）の0時30分
        self.assertEqual(self.tx(lambda tx: bs.day_of_call(tx, v, at)),
                         {"earliest_day": date(2098, 11, 7), "today": date(2098, 11, 3)})   # 4日 祝日・5日 休業日 ⇒ 6日・7日
        plain = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.assertEqual(self.tx(lambda tx: bs.day_of_call(tx, plain, at)), {"earliest_day": None, "today": date(2098, 11, 3)})
        with self.assertRaises(calendar.CalendarOutOfRange):
            self.tx(lambda tx: bs.day_of_call(tx, v, datetime(2099, 12, 30, 3, 0, tzinfo=timezone.utc)))   # 2100年の暦が無い

    def test_a_version_saved_with_decline_rules_reads_them_as_final_goals(self):
        """業務の決まりに「断りの答え」（decline）を持っていた前の版（2026-11-02 に業務設定の質問の final_goal・declines と業務の goal_mode
        へ移した＝CL-1 の連絡帳 4-69 の 4-182 の (e)）も読める（版の中身・決まりの表は書き換えられないので、読む側の1か所 `_current_shape`）。
        断りの答えを書いた質問は最終目的の申し出（断る答えはそのまま）、2つ以上なら果たし方は all（前の判定は、どれか1つが断られたら
        「用件を断られた」とした＝同じ結果）。前の版へ戻した新しい版は、今の形で置かれる（決まりに decline を持たない）。"""
        import json
        from unittest import mock
        from salescall.core import business_rules
        old_content = bs._content
        r = raw()
        r["questions"][1].update(type="yes_no", text="ご案内の資料をお送りしてもよろしいでしょうか？", final_goal=True, declines=["いいえ"])
        r["goal_mode"] = "all"

        def before_goals(s):   # 移す前の中身（質問に final_goal・declines が無く、業務に goal_mode が無い）
            c = old_content(s)
            return dict({k: v for k, v in c.items() if k != "goal_mode"},
                        questions=[{k: v for k, v in q.items() if k not in ("final_goal", "declines")} for q in c["questions"]])

        def before_rules(tx, vid, rules):   # 移す前の決まり（断りの答えを持つ）
            tx.execute("INSERT INTO salescall.business_rules (tenant_id, client_id, version_id, rules, saved_by)"
                       " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())",
                       (vid, json.dumps(dict(rules, decline={"Q1": ["しない"], "Q2": ["いいえ"]}), ensure_ascii=False)))

        with mock.patch.object(bs, "_content", before_goals), mock.patch.object(business_rules, "save", before_rules):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        stored = self.tx(lambda tx: tx.one("SELECT rules FROM salescall.business_rules WHERE version_id = %s", (v,)))
        self.assertIn("decline", stored["rules"])                                   # 前の形のまま置いてある
        got = self.tx(lambda tx: bs.load(tx, v))
        self.assertEqual(([(q.final_goal, q.declines) for q in got.questions], got.goal_mode),
                         ([(True, ("しない",)), (True, ("いいえ",))], "all"))
        self.assertEqual([q.ends_on for q in got.questions], [("しない",), ()])   # 分岐のシートの「しない → 終わり」は読むたびに置き換える
        back = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
        self.assertEqual(([(q.final_goal, q.declines) for q in back.questions], back.goal_mode),
                         ([(True, ("しない",)), (True, ("いいえ",))], "all"))
        restored = self.tx(lambda tx: tx.one("SELECT rules FROM salescall.business_rules WHERE version_id = %s",
                                             (back.rules.version_id,)))
        self.assertNotIn("decline", restored["rules"])                              # 戻した版は今の形
        one = raw()                                                                 # 断りの答えが1つなら、果たし方は無い（どちらでも同じ）
        with mock.patch.object(bs, "_content", before_goals), mock.patch.object(business_rules, "save", lambda tx, vid, rules: tx.execute(
                "INSERT INTO salescall.business_rules (tenant_id, client_id, version_id, rules, saved_by) VALUES (salescall.cur_tenant(),"
                " salescall.cur_client(), %s, %s, salescall.cur_actor())", (vid, json.dumps(dict(rules, decline={"Q1": ["しない"]}))))):
            v1 = self.tx(lambda tx: bs.save_version(tx, self.s["business"], one))
        got = self.tx(lambda tx: bs.load(tx, v1))
        self.assertEqual(([(q.final_goal, q.declines) for q in got.questions], got.goal_mode), ([(True, ("しない",)), (False, ())], None))

    def test_a_version_saved_before_none_reads_none_as_going_on(self):
        """「どれも要らない」（4-66 の (a)）が返りうる答えに増える前に置いた版で、事実を伺わない選択肢の質問の分岐を答えごとに書き、受け皿の
        無いもの：「どれも要らない」の行き先を並びの次の質問として読む（会話の側は、答えとして受け取って続ける）。読めなくならない。"""
        from unittest import mock
        old_content = bs._content
        every = {"する": "Q2", "しない": "END", "どちらでも": "Q2", "わからない": "Q2", "答えたくない": "Q2"}

        def before_none(s):   # 足す前の形（伺う条件・ご承認も無い）と、受け皿の無い分岐
            c = old_content(s)
            qs = [{k: v for k, v in q.items() if k not in ("ask_if", "approval")} for q in c["questions"]]
            qs[0]["next"] = every
            return dict(c, questions=qs)

        with mock.patch.object(bs, "_content", before_none):
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        got = self.tx(lambda tx: bs.load(tx, v))
        q1 = got.questions[0]
        self.assertEqual((q1.ends_on, got.questions[1].ask_if), (("しない",), None))

    def test_questions_asked_after_an_answer_are_read_for_the_judge(self):
        """判定へ渡す、前の答えで伺う質問（`ask_ifs` の1か所。置いた版から、業務設定の並び順で）。"""
        r = raw()
        r["questions"][1]["ask_if"] = {"after": "Q1", "answers": ["する", "どちらでも"], "hedged": False}
        r["questions"][0]["required"] = True
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        self.assertEqual(self.tx(lambda tx: bs.ask_ifs(tx, v)), {"Q2": bs.AskIf("Q1", ("する", "どちらでも"))})

    def test_responses_are_kept_in_the_version(self):
        """相手の申し出への応じ方・伝言の文・業務の情報は版に残り、読み直しても同じ（前の版へ戻しても同じ）。"""
        r = raw()
        r.update(substitute_policy="target_only", message_policy="leave", message="お伝えいただけますでしょうか？",
                 departed_policy="end", info=["営業時間は9時から18時です"], follow_up=True)
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], r))
        got = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
        self.assertEqual((got.substitute_policy, got.message_policy, got.message, got.departed_policy, got.info, got.follow_up),
                         ("target_only", "leave", "お伝えいただけますでしょうか？", "end", ("営業時間は9時から18時です",), True))

    def test_assigning_a_new_version_releases_reservations_of_the_old_one(self):
        """版を替えたら、前の版で取ったまだ発信していない予約を取り消す（門の前提が変わったときの決まり。2026-10-16 の見直し G7）。"""
        claim = self.w.insert_claim(self.s)                            # 前の版での予約
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        self.tx(lambda tx: bs.assign(tx, self.s["run"], v, reason="試験：新しい版に切り替え"))
        state = self.tx(lambda tx: tx.one("SELECT state FROM salescall.attempts WHERE attempt_id = %s", (claim,)))["state"]
        self.assertEqual(state, "released")

    def test_nothing_is_saved_when_invalid(self):
        before = self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.business_versions")["n"])
        bad = raw(); bad["rules"]["retry"] = {}                        # 業務の決まりの抜け（版を作る途中で見つかる）
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bs.save_version(tx, self.s["business"], bad))
        bad2 = copy.deepcopy(raw()); bad2["questions"][0]["next"] = {"*": "Q9"}
        with self.assertRaises(bs.SettingsInvalid):
            self.tx(lambda tx: bs.save_version(tx, self.s["business"], bad2))
        after = self.tx(lambda tx: tx.one("SELECT count(*) AS n FROM salescall.business_versions")["n"])
        self.assertEqual(after, before)                                # 全部か、何も無いか

    def test_contents_are_frozen(self):
        v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
        own = owner_access()
        for sql in ("UPDATE salescall.business_contents SET content = '{}'", "DELETE FROM salescall.business_contents"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                own.in_client(OPERATOR, self.s["tenant"], self.s["client"], lambda tx: tx.execute(sql))
        self.assertIsNotNone(v)


if __name__ == "__main__":
    unittest.main()


STD = (("番号の出どころ", "以前お問い合わせいただいた際のお電話番号にお掛けしております。"),
       ("会社の場所", "東京都港区にございます。"))


class StandardScripts(unittest.TestCase):
    """システムの標準の想定問答と、業務の上書き（設計書 3-8-3 の作る順の (4)・0174。✅ 2026-11-02 管理者 伺い2「システムの想定問答や
    トークスクリプトにデフォルトの想定を…表示しておき、基本はそれを使う。上書きが必要な業務は…上書きし、その分の録音を実施する。」）。"""

    def setUp(self):
        self.w = World()
        self.s = self.w.setup()

    def tx(self, work, actor=OPERATOR):
        return self.w.a.in_client(actor, self.s["tenant"], self.s["client"], work)

    @staticmethod
    def std(*answers):
        return bs.Standard(UUID(int=7), tuple(answers))

    def test_the_standard_answers_come_in_and_the_businesss_own_answer_overrides_by_name(self):
        """業務の版の想定問答＝業務の答え（業務の録音）＋業務が書いていない名前の標準の答え（標準の録音）。同じ名前は業務の答えが勝つ。
        どれが標準から来たか（standard_answers）と、標準の録音から流す部分の名前（standard_parts）は、重ね方の1か所が決める。"""
        r = raw()
        r["answers"] = [{"name": "会社の場所", "text": "大阪府にございます。"}, {"name": "駐車場", "text": "ございます。"}]
        s = bs.check(r, standard=self.std(*STD), field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual(s.answers, (("会社の場所", "大阪府にございます。"), ("駐車場", "ございます。"), STD[0]))
        self.assertEqual((s.standard_id, s.standard_answers), (UUID(int=7), frozenset({"番号の出どころ"})))
        self.assertEqual(bs.standard_parts(s), ("answer_番号の出どころ",))
        self.assertEqual(bs.own_answers(s), (("会社の場所", "大阪府にございます。"), ("駐車場", "ございます。")))
        self.assertEqual([(x["name"], x["source"]) for x in bs.answer_rows(s, self.std(*STD))],
                         [("番号の出どころ", "standard"), ("会社の場所", "overridden"), ("駐車場", "business")])
        plain = bs.check(raw(), standard=bs.NO_STANDARD, field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((plain.answers, plain.standard_id, plain.standard_answers), ((), None, frozenset()))

    def test_a_business_turns_off_a_standard_answer_only_by_its_name(self):
        """業務は、標準の想定問答を名前で「使わない」にできる（answers_off）。標準に無い名前・業務の答えで上書きした名前は「使わない」に
        できない（どちらか1つ）。並びでない・同じ名前が2回も問題。"""
        r = raw()
        r["answers_off"] = ["会社の場所"]
        s = bs.check(r, standard=self.std(*STD), field_types=FIELD_TYPES, ai_reading_needs_person=True)
        self.assertEqual((s.answers, s.standard_answers), ((STD[0],), frozenset({"番号の出どころ"})))
        self.assertEqual([(x["name"], x["source"]) for x in bs.answer_rows(s, self.std(*STD))],
                         [("番号の出どころ", "standard"), ("会社の場所", "off")])
        for off, answers, want in ((["無い問い"], [], "標準の想定問答にありません"),
                                   (["会社の場所"], [{"name": "会社の場所", "text": "大阪府にございます。"}], "どちらか1つ"),
                                   ("会社の場所", [], "問いの名前の並び"),
                                   (["会社の場所", "会社の場所"], [], "問いの名前の並び")):
            r = raw()
            r["answers_off"], r["answers"] = off, answers
            with self.assertRaises(bs.SettingsInvalid) as e:
                bs.check(r, standard=self.std(*STD), field_types=FIELD_TYPES, ai_reading_needs_person=True)
            self.assertTrue(any(want in p for p in e.exception.problems), (off, e.exception.problems))
        r = raw()
        r["answers_off"] = ["会社の場所"]
        self.assertTrue(any("標準の想定問答にありません" in p for p in problems_of(r)))   # 標準を使わない版には、使わない問いも無い

    def test_the_standard_has_the_same_shape_as_the_businesss_answers(self):
        """標準の版の中身の確かめは、業務の想定問答と同じ1か所（名前の重なり・使えない名前・ファイル名に入らない文字）。"""
        self.assertEqual(bs.check_standard({"answers": [{"name": "a", "text": " 答え "}]}).answers, (("a", "答え"),))
        for bad in ({"answers": [{"name": "a", "text": "x"}, {"name": "a", "text": "y"}]},
                    {"answers": [{"name": "a/b", "text": "x"}]},
                    {"answers": [{"name": "a"}]},
                    {"answers": [], "busy_line": "x"},
                    []):
            with self.assertRaises(bs.SettingsInvalid):
                bs.check_standard(bad)

    def test_only_the_admin_makes_a_standard_version_and_it_never_changes(self):
        """標準の版を作れるのは管理者だけ（システムの取引で。データベースが守る）。作った版は変えられない（直すのは新しい版）。
        すべての契約者が読める。"""
        from tests.support import ADMIN
        body = {"answers": [{"name": "会社の場所", "text": "東京都港区にございます。"}]}
        for who in (lambda: self.tx(lambda tx: bs.save_standard(tx, body)),
                    lambda: self.w.a.in_system(OPERATOR, lambda tx: bs.save_standard(tx, body))):
            with self.assertRaises(Rejected) as e:
                who()
            self.assertEqual(e.exception.constraint, "standard_by_admin")
        with self.w.standard(STD[1:]) as sid:
            self.assertEqual(self.tx(lambda tx: bs.current_standard(tx)), bs.Standard(sid, STD[1:]))
            stored = self.w.a.in_system(ADMIN, lambda tx: tx.one(
                "SELECT created_by FROM salescall.standard_versions WHERE standard_id = %s", (sid,)))
            self.assertEqual(stored["created_by"], ADMIN.name)
            for change in ("UPDATE salescall.standard_versions SET content = '{\"answers\": []}' WHERE standard_id = %s",
                           "DELETE FROM salescall.standard_versions WHERE standard_id = %s"):
                with self.assertRaises(Rejected):
                    owner_access().in_system(ADMIN, lambda tx: tx.execute(change, (sid,)))
        with self.assertRaises(bs.SettingsInvalid):   # 確かめに通らない中身は置かない
            self.w.a.in_system(ADMIN, lambda tx: bs.save_standard(tx, {"answers": [{"name": "a", "text": ""}]}))
        trimmed = self.w.a.in_system(ADMIN, lambda tx: tx.one(   # 置くのは確かめた形（標準の録音の文と比べる守りと同じ文）
            "SELECT content FROM salescall.standard_versions WHERE standard_id = %s",
            (bs.save_standard(tx, {"answers": [{"name": "a", "text": " 答え "}]}),)))
        self.w.a.in_system(ADMIN, lambda tx: bs.save_standard(tx, {"answers": []}))   # いまの標準を空に戻す
        self.assertEqual(trimmed["content"], {"answers": [{"name": "a", "text": "答え"}]})

    def test_a_version_keeps_the_standard_it_was_made_with(self):
        """業務の版は、作ったときのいまの標準の版を指し、読むときもその版を重ねる（管理者が標準を新しくしても、その業務の版の文は
        変わらない＝どの通話がどの文で話したかを追える）。前の版へ戻す・複製も、元の版の標準のまま。新しい版は、そのときのいまの標準。"""
        with self.w.standard(STD) as first:
            v = self.tx(lambda tx: bs.save_version(tx, self.s["business"], raw()))
            with self.w.standard(STD[:1]) as second:
                got = self.tx(lambda tx: bs.load(tx, v))
                self.assertEqual((got.standard_id, got.answers), (first, STD))
                back = self.tx(lambda tx: bs.load(tx, bs.restore(tx, v)))
                self.assertEqual((back.standard_id, back.answers), (first, STD))
                new = self.tx(lambda tx: bs.load(tx, bs.save_version(tx, self.s["business"], raw())))
                self.assertEqual((new.standard_id, new.answers), (second, STD[:1]))
                self.assertEqual(self.tx(lambda tx: bs.standard_parts_of(tx, v)), ("answer_会社の場所", "answer_番号の出どころ"))
                none = self.tx(lambda tx: bs.load(tx, bs.save_version(tx, self.s["business"], raw(), standard_id=None)))
                self.assertEqual((none.standard_id, none.answers), (None, ()))
        old = self.s["version"]                                         # 標準の前の版（標準を指さない）
        self.assertEqual(self.tx(lambda tx: bs.standard_parts_of(tx, old)), ())
        with self.assertRaises(Rejected):                               # 版が指す標準の版は変えられない（版と同じ）
            self.tx(lambda tx: tx.execute("UPDATE salescall.business_versions SET standard_id = NULL WHERE version_id = %s", (v,)))


class RoleProblem(unittest.TestCase):
    def test_a_bare_tantou_role_is_refused_but_a_role_with_its_charge_is_used(self):
        """「ご担当者」だけ（何の担当か無い）の役職は、業務設定でも、お客様リストの行の役職でも使えない。何の担当かがあればよい
        （✅ 2026-11-05 ユーザー：「～のご担当者様」「～をされているご担当者様」「～を受け持ちのご担当者様」は書ける）。"""
        for bare in ("ご担当者", "ご担当者様", "ご担当の方", "担当者", "担当", "ご担当者 様", "御担当者"):
            with self.subTest(bare=bare):
                self.assertIsNotNone(bs.role_problem(bare))
                self.assertEqual(bs.TargetNeeds(name=False, role=True).missing(name=None, role=bare)[0], "target_role_missing")
        for ok in ("経理のご担当者様", "採用をされているご担当者様", "店長", "人事部長"):
            with self.subTest(ok=ok):
                self.assertIsNone(bs.role_problem(ok))
                self.assertIsNone(bs.TargetNeeds(name=False, role=True).missing(name=None, role=ok))
