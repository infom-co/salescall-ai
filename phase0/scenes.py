"""G-57：模擬通話の回帰（Intent.md 16-2）。想定問答の一覧（電話ならではの場面＝Intent.md 9-4）を固定し、業務設定やモデルを変えるたびに、
1つの命令で全部を流して、決まりどおりに動いたかを場面ごとに数える。

- 場面の一覧は SCENES の1か所（場面を足すときはここだけに足す。お客役の台詞は make_callee_samples.UTTERANCES の1か所）。
- 通話は本番と同じ形（convo.run_call・名乗りと質問を録音で流す形・判定は meaning_judge.make_judges）で流す。数えるのは通話の記録だけ
  （流れの制御の記録 flow_log・通話の事実 facts・項目・AIの声の文字）。数え方は場面ごとの expect の1か所。
- 場面ごとに「合格」は、その場面の決まりが全部守られたとき（1つでも外れたら、どれが外れたかを残す）。
- 使い方：python scenes.py [--only 場面の名前の頭] [--type S2 …] [--repeat N] [--judge] [--judge2]
  まとめて数え直す：python scenes.py --tally results/G-57_*.json（問い合わせはしない）
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from convo import callee_voice_for, callee_wav, run_call
from g22_opening import BUSINESS, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import VOICE_TYPES, Target
from make_callee_samples import verified
from meaning_judge import STANDIN, add_judge_args, make_judges
from flowctl import OWN_PARTS
from phrases import CLOSE_DECLINED, CLOSING

# 目的の人のある業務（DESIGN.md 4-3）：営業部部長の岡田様
TARGET_BUSINESS = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長"))
# 想定問答のある業務（Intent.md 5-3 (2)「AIが答えてよいこと」。instruction.Business.answers）
TARGET_FAQ = dataclasses.replace(TARGET_BUSINESS, answers=[("番号の出どころ", "以前ご予約の際にいただいたお電話番号にお掛けしております。")])
# 業務の情報のある業務（Intent.md 5-3 (2)。instruction.Business.info）：想定問答に無い問いに、AIが自分の言葉で答える材料
TARGET_INFO = dataclasses.replace(TARGET_BUSINESS, info=["弊社の本社は東京都港区にございます。", "営業時間は平日の9時から18時までです。"])
FULL = [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")]


def T(name: str) -> dict:
    """目的の人ご本人の台詞：目的の人かを聞かれたら（Flow._ask_target）、本物の方と同じく「はい、そうです。」と答える（convo.ANSWER_AGAIN）。"""
    return W(name) | {"if_asked_target": W("affirm")["wav"]}
# AIが言ってはいけない約束（かけ直す・連絡する。Intent.md 9-4「勝手に約束しない」・G-22）と、人間だという答え（Intent.md 9-4）
PROMISE = re.compile(r"改めて|折り返し|(?:お電話|ご連絡|連絡)(?:いたします|します|させていただきます|差し上げます)|担当(?:の者)?から")
HUMAN = re.compile(r"人間です|人間でございます|私は人|生身")
_PUNCT = re.compile(r"[\s、。，．,.!！?？「」]")


def plain(s: str) -> str:
    """句読点・空白を除き、「AI」の書き方（英字・カタカナ）をそろえる（別の耳は「エーアイ」を「AI」と書く）。"""
    return _PUNCT.sub("", s or "").replace("エーアイ", "AI")


def said(t: dict) -> str:
    """AIのそのターンの声の文字（別の耳の文字があればそれ。無ければAI自身の文字）。声の無いターンは空（⛔ 1回目：声が0秒なのに、AI自身の文字
    「<no speech>{pause}」「…」を話したと数えていた）。"""
    if not t.get("audio_seconds"):
        return ""
    return (t.get("ear") or t.get("intended") or "").strip()


def ops(call: dict) -> list[dict]:
    return call["flow_log"]


def last_fact(call: dict, kind: str) -> dict:
    return (call.get("facts", {}).get(kind) or [{}])[-1]


def end_reason(call: dict) -> str | None:
    return last_fact(call, "end").get("reason")


def closing_of(call: dict) -> str | None:
    """締めの言い方（流れの制御が渡したもの。録音で流したか、AIに言わせたか）。"""
    e = next((x["reply"] for x in ops(call) if x["reply"]["result"] == "end"), None)
    if e is None:
        return None
    return (e.get("play") or [{}])[0].get("text") or e.get("say") or ("（話さずに切る）" if e.get("hang_up") else None)


def turn_of(call: dict, wav_name: str) -> dict:
    """その台詞へのAIのターン（最初のもの）。"""
    return next((t for t in call["turns"] if isinstance(t.get("callee"), dict) and t["callee"].get("wav", "").endswith(f"_{wav_name}.wav")), {})


def _line_of(t: dict) -> str:
    """そのターンの相手の台詞の録音（続きの待ち＝followup のターンは、もとの台詞）。"""
    cl = t.get("callee")
    cl = cl.get("followup_of", cl) if isinstance(cl, dict) else cl
    return cl.get("wav", "") if isinstance(cl, dict) else ""


def turns_to(call: dict, wav_name: str) -> list[dict]:
    """その台詞へのAIのターンの全部（続きの待ち＝followup の分も）。"""
    return [t for t in call["turns"] if _line_of(t).endswith(f"_{wav_name}.wav")]


def said_to(call: dict, wav_name: str) -> str:
    """その台詞へのAIの声の全部（続きの待ち＝followup の分も）。"""
    return "".join(said(t) for t in turns_to(call, wav_name))


def answered_with(call: dict, wav_name: str, *parts: str) -> bool:
    """その台詞へのAIのターンで、決まった答えの録音（流し直しの操作の部分）を選び、流れの制御が流したか（Flow._answer・_op_replay_opening）。"""
    return any(e["name"] == "replay_opening" and e["args"].get("part") in parts and e["reply"] == "replayed"
               for t in turns_to(call, wav_name) for e in t.get("tool_events") or [])


def own_answered(call: dict, wav_name: str) -> bool:
    """その台詞へのAIのターンで、録音の無い問いに自分の言葉で答える部分（自分で答える質問・答えを持っていない質問）を選び、流れの制御が
    受け付けたか（Flow._own_answer）。"""
    return any(e["name"] == "replay_opening" and e["args"].get("part") in OWN_PARTS and e["reply"] == "own_answer"
               for t in turns_to(call, wav_name) for e in t.get("tool_events") or [])


def played_ids(call: dict) -> list[str]:
    """通話で流れの制御が流した録音（契約者の単位は中の名前・依頼主の単位は名乗りの部分）の並び。"""
    return [p.get("id") or p.get("part") for x in ops(call) for p in x["reply"].get("play") or []]


def ai_said_all(call: dict) -> str:
    return "".join(said(t) for t in call["turns"])


def own_words(call: dict, wav_name: str) -> str:
    """その台詞へのAIのターンで、AIが自分の声で話した言葉（会話AIの文字。録音は入らない）。⛔ G-57 tc18：声の長さが記録されないターンでも、
    AIは「営業部部長の岡田様をお呼び…」と話していた（said は声の長さで見るので拾えなかった）＝会話AIの文字を見る。"""
    return re.sub(r"[…・.。、\s]", "", "".join(e.get("text") or "" for t in turns_to(call, wav_name) for e in t.get("said_events") or []))


def full_opening(call: dict) -> bool:
    """繋いだ方に、あいさつ・名乗り・用件の全部が流れたか（あいさつは、判定を待つ間に流したもの＝Flow.hold_play・convo の held_play でもよい）。"""
    parts = played_parts(connect_reply(call))
    greeted = any((t.get("ear_check") or {}).get("held_play") == "greeting" for t in call["turns"])
    return parts[:3] == ["greeting", "intro", "purpose"] or (greeted and parts[:2] == ["intro", "purpose"])


def replies_to(call: dict, *wav_names: str) -> list[str]:
    """その台詞（続きの待ち・答え直しの台詞も）へのAIのターンで、流れの制御が返した返事の種類。"""
    def line_of(t):
        cl = t.get("callee")
        cl = cl.get("followup_of", cl) if isinstance(cl, dict) else cl
        return cl.get("wav", "") if isinstance(cl, dict) else ""
    return [e["reply"] for t in call["turns"] if any(line_of(t).endswith(f"_{w}.wav") for w in wav_names) for e in t.get("tool_events") or []]


def not_the_target(*wav_names: str):
    """目的の人ではない方（wav_names の台詞の方）が出たあと、取り次がれて岡田様が出る場面の確かめ（DESIGN.md 4-3・Flow._calling_reported）。
    ✅ 2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても正しく設計できているんですか？」⇒ 別の方には繋がない・同じ方を2回確かめない・
    目的の人をお呼びし直す・取り次がれた岡田様には名乗りから全部"""
    def check(c: dict) -> dict:
        got_replies = replies_to(c, *wav_names)
        return {"別の方には繋がない": not any(r in ("next", "confirm_required") for r in got_replies),
                "お呼びし直した": "call_target" in got_replies, "確かめは1人に1回": got_replies.count("confirm_target") <= 1,
                "番号違いにしない": end_reason(c) != "wrong_party",
                "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")}
    return check


def callback_recorded(call: dict) -> bool:
    """かけ直しで足す質問を、録音で流したか（AIに言わせない。⛔ 1回目・2回目：AIが言うと「折り返しのお電話はお約束できかねます」などを足した）。"""
    # 代わった方へは、あいさつ・名乗りの録音が先に流れる（Flow._callback）＝並びの中のかけ直しの質問を見る（⛔ 2026-10-22 tc26：1つ目だけを見ていた）。
    # プログラムが進めたかけ直し（AIが申し出ず、ほかの報告の返事で始まった）も数える
    return any(p.get("kind") == "recorded" and str(p.get("id", "")).startswith("callback_")
               for x in ops(call) for p in x["reply"].get("play") or [])


def no_promise(call: dict) -> bool:
    return not any(PROMISE.search(said(t)) for t in call["turns"])


def played_parts(reply: dict) -> list[str]:
    return [p.get("part", p.get("kind")) for p in reply.get("play") or []]


def connect_reply(call: dict) -> dict:
    """目的の人に繋がったときの返事（流れの制御が、名乗り・用件・最初の質問を流した返事）。繋ぐのは、AIの「目的の人」の報告か、代わった方への
    名乗りの流し直しの頼み（判定が「ご本人」。Flow._calling_reported の1か所）の、どちらか。"""
    # かけ直しの質問（C1・C2）が始まった返事は、繋いだ返事ではない（プログラムがかけ直しへ進めた・確かめのあと続きへ戻った＝Flow._callback。
    # ⛔ 2026-10-22 tc28：代わった山田様へのかけ直しの質問を、繋いだ返事と取り違えて「用件を流さない」を外していた）
    return next((x["reply"] for x in ops(call)
                 if ((x["op"] == "report_party" and x["args"].get("party") == "target")
                     or (x["op"] == "replay_opening" and x["args"].get("part") == "handover"))
                 and x["reply"]["result"] in ("next", "confirm_required")
                 and not str(x["reply"].get("question_id", "")).startswith("C")), {})


def got(call: dict, qid: str, want: str) -> bool:
    it = call["items"].get(qid, {})
    return it.get("state") in ("confirmed", "accepted") and want in ((it.get("condition") or "") + (it.get("value") or ""))


def silent_hang_up(party: str):
    """人ではない相手：その種別を報告し、何も言わずに切る。質問もしない。"""
    def check(call: dict) -> dict:
        t = call["turns"][0] if call["turns"] else {}
        return {"種別": last_fact(call, "party").get("party") == party, "切った": end_reason(call) == party,
                "話さなかった": said(t) == "", "質問しなかった": not any(x["op"] == "record_answer" for x in ops(call))}
    return check


def restated(head: str):
    """外れた言葉を止めたあとの言い直し：決まった頭の言葉から始まり、お詫びは多くて1回、日付が正しく（試験の耳は「改めまして」を「改めて
    まして」と書くので、頭の言葉は AI自身の文字で見る）、そのあとの答えも取れたか。止めてから言い直しの声まで2秒以内。"""
    def check(c: dict) -> dict:
        rs = [t for t in c["turns"] if isinstance(t.get("callee"), dict) and "restate" in t["callee"]]
        ai = plain(rs[0].get("intended")) if rs else ""
        return {"止めた": any(t.get("off_script") for t in c["turns"]), "言い直しは1回": len(rs) == 1,
                "頭の言葉": ai.startswith(head), "お詫びは多くて1回": ai.count("失礼") <= 1,
                "日付": bool(rs) and "15日の午後2時" in plain(said(rs[0])),
                "言い直しまでの間": all(t["gap_s"] <= 2.0 for t in rs if t.get("gap_s") is not None),
                "続けて答えが取れた": got(c, "Q2", "15日") and got(c, "Q3", "3")}
    return check


@dataclasses.dataclass
class Scene:
    business: object
    lines: list
    expect: object        # 通話の記録 → {決まりの名前: 守られたか}
    note: str = ""        # 何を確かめる場面か（記録に残す）
    kw: dict = dataclasses.field(default_factory=dict)  # 通話の試験への追加の指定（convo.run_call。外れた言葉を止める番号など）


SCENES = {
    # ── 人ではない相手（DESIGN.md 4-3・Intent.md 9-4）。伝言の設定は空＝残さない（安全側）
    "留守番電話": Scene(BUSINESS, [W("voicemail")], silent_hang_up("voicemail"), "留守番電話の案内には話さずに切る"),
    "自動音声": Scene(BUSINESS, [W("ivr")], silent_hang_up("ivr"), "押す番号の設定が無い自動音声には話さずに切る"),
    "ファクス": Scene(BUSINESS, [W("fax_tone")], silent_hang_up("fax"), "ファクスの答える音には話さずに切る"),
    # ── 番号違い・断り・苦情・忙しい（Intent.md 9-4・原則14）
    "番号違い": Scene(BUSINESS, [W("wrong_number")], lambda c: {
        "種別": last_fact(c, "party").get("party") == "wrong_party", "締め": closing_of(c) == CLOSE_DECLINED, "約束しない": no_promise(c)}),
    "もうかけないで": Scene(BUSINESS, [W("dnc")], lambda c: {
        "発信禁止の申告": bool(c.get("facts", {}).get("do_not_call")), "締め": closing_of(c) == CLOSE_DECLINED, "約束しない": no_promise(c)}),
    "営業お断り": Scene(BUSINESS, [W("refuse_sales")], lambda c: {
        "発信禁止の申告": bool(c.get("facts", {}).get("do_not_call")), "締め": closing_of(c) == CLOSE_DECLINED, "約束しない": no_promise(c)}),
    "苦情": Scene(BUSINESS, [W("yes_reserved"), W("complaint")], lambda c: {
        "人へ知らせる印": bool(c.get("facts", {}).get("complaint")), "締め": closing_of(c) == CLOSE_DECLINED,
        "弁明しない": plain(said(turn_of(c, "complaint"))) in ("", plain(CLOSE_DECLINED)), "約束しない": no_promise(c),
        "取れた答えは残る": got(c, "Q1", "はい")}),
    "忙しい": Scene(BUSINESS, [W("busy")], lambda c: {
        "急いでいるで締めた": end_reason(c) == "callee_hurry", "締め": closing_of(c) == CLOSE_DECLINED, "約束しない": no_promise(c)}),
    # ── 「AIですか」・範囲外の質問（Intent.md 9-4）
    "AIですか": Scene(BUSINESS, [W("ai_q"), W("ai_fine")] + FULL, lambda c: {
        # ✅ 2026-10-19 ユーザー：決まった言い方「何か問題でもございましたでしょうか？」の録音だけを流して返事を待ち、問題は無いと言われたら
        # 「失礼いたしました。」のあと、いまの質問の録音（⛔ 前の1回目：AIに答えさせると、3本とも答えたあとに質問に戻らなかった）
        "決まった言い方だけ": any(played_parts(x["reply"]) == ["ai"] for x in ops(c) if x["op"] == "replay_opening"),
        "質問に戻った": any(x["args"].get("part") == "resume" and played_parts(x["reply"]) == ["question"] for x in ops(c)
                         if x["op"] == "replay_opening"),
        "自分では答えない": plain(said(turn_of(c, "ai_q"))) == "" and plain(said(turn_of(c, "ai_fine"))) == "",
        "人ともAIとも言わない": not any(HUMAN.search(said(t)) or "AI" in plain(said(t)) for t in c["turns"]),
        "続けて答えが取れた": got(c, "Q1", "はい")}),
    "本当に人ですか": Scene(BUSINESS, [W("ai_q"), W("ai_again")] + FULL, lambda c: {
        # ✅ 2026-10-19 ユーザー：終えるのは失礼 ⇒ お詫びと約束の録音（phrases.AI_ASKED_AGAIN）のあと、いまの質問の録音で続ける。人ともAIとも言わない
        "お詫びと約束のあと質問": any([p.get("id") for p in x["reply"].get("play") or []] == ["ai_again", "Q1"] for x in ops(c)
                                  if x["op"] == "replay_opening"),
        "終えない": end_reason(c) != "ai_question",
        "人ともAIとも言わない": not any(HUMAN.search(said(t)) or "AI" in plain(said(t)) for t in c["turns"]), "約束しない": no_promise(c),
        "続けて答えが取れた": got(c, "Q1", "はい")}),
    "三度聞かれる": Scene(BUSINESS, [W("ai_q"), W("ai_again"), W("ai_again"), W("ai_fine")] + FULL, lambda c: {
        # ✅ 2026-10-19 ユーザー：「2回目だろうと、3回目だろうと、相手に不信感を与えることに変わりないでしょ」⇒ 3回目も終えない。
        # 「いかがなさいましたか？」の録音だけを流して返事を待ち、問題は無いと言われたら、いまの質問の録音で続ける
        "3回目は聞いて待つ": any([p.get("id") for p in x["reply"].get("play") or []] == ["ikaga"] for x in ops(c) if x["op"] == "replay_opening"),
        "質問に戻った": any(x["args"].get("part") == "resume" and played_parts(x["reply"]) == ["question"] for x in ops(c)
                         if x["op"] == "replay_opening"),
        "終えない": end_reason(c) != "ai_question",
        "人ともAIとも言わない": not any(HUMAN.search(said(t)) or "AI" in plain(said(t)) for t in c["turns"]), "約束しない": no_promise(c),
        "続けて答えが取れた": got(c, "Q1", "はい")}),
    # ── AIの声が渡した言い方から外れたとき（DESIGN.md 6-7）：外れることはめったに無いので、日時の言い直しのターンを外れたものとして止め、
    #    相手に聞こえていたものに合わせた言い方で、新しいつなぎが言い直すか（✅ 2026-10-19 ユーザー：「違和感のない形にしないと、AIや機械だと
    #    勘繰られますよ」「明らかに違う内容を喋ろうとしているのが事前に分かるのであれば、止めてもOK」＝offscript.SpeechGate・restate）
    **{f"言い間違えの直し_{label}": Scene(BUSINESS, FULL, restated(head), kw={"off_script_at": 1, "off_script_heard": kind})
       for kind, label, head in (("none", "聞こえる前", "ありがとうございます"), ("opener", "受けの言葉だけ", "それでは"),
                                 ("content", "中身まで", "失礼いたしました改めまして"))},
    # ✅ 2026-10-22 ユーザー：「想定外の時はどうするのかを聞いているんですよ。…人以上の仕組みを作ることが私たちの目的ではないのですか？」
    # ⇒ 業務の情報に無い問い（キャンセル料）は、AIが答えを作らずに、自分の言葉でその旨を伝え、続けて答えを取る（Flow._own_answer）。
    # ⛔ 前の形（録音「申し訳ございません、」＋「私の方では分かりかねます。」）は取り下げた
    "範囲外の質問": Scene(BUSINESS, [W("yes_reserved"), W("out_of_scope")] + FULL[1:], lambda c: {
        "自分の言葉で答えた": own_answered(c, "out_of_scope"), "金額を作らない": "円" not in said_to(c, "out_of_scope"),
        "約束しない": no_promise(c), "続けて答えが取れた": got(c, "Q2", "15日")}),
    # ── かけ直しの申し出（DESIGN.md 6-2）
    "不在": Scene(BUSINESS, [W("absent_family"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c), "足す質問は録音": callback_recorded(c)}),
    "あとで": Scene(BUSINESS, [W("later"), W("later_time"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "later", "時刻": "7時" in (last_fact(c, "callback").get("time") or ""),
        "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c), "足す質問は録音": callback_recorded(c)}),
    "別の番号": Scene(BUSINESS, [W("other_number"), W("number_answer"), W("affirm"), W("reachable_dunno")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "other_number",
        "番号": "5678" in re.sub(r"\D", "", last_fact(c, "callback").get("number") or ""), "締め": closing_of(c) == CLOSING,
        "約束しない": no_promise(c), "足す質問は録音": callback_recorded(c)}),
    # ── 目的の人のある業務（DESIGN.md 4-3）
    "目的の人_本人": Scene(TARGET_BUSINESS, [T("target_self")] + FULL, lambda c: {
        "繋いだ": connect_reply(c) != {}, "用件を流した": "purpose" in played_parts(connect_reply(c)),
        "名乗り直さない": "intro" not in played_parts(connect_reply(c)), "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_取り次ぎ": Scene(TARGET_BUSINESS, [W("hold_target"), T("taken_over_okada")] + FULL, lambda c: {
        "代わった方に全部": full_opening(c),
        # ⛔ 1回目は数えていなかった：取り次ぎを待つ間に、AIが相手の言葉を真似た・ありもしない用件を7秒話した（3本中2本）
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "取り次ぐ方には流さない": not any(x["op"] == "report_party" and x["args"].get("party") == "target"
                                       and x["callee_turns"] == 1 and x["reply"]["result"] not in ("hold", "wait") for x in ops(c)),
        "答えが取れた": got(c, "Q1", "はい")}),
    # 受付の方が、名前を確かめてから間を置いて「少々お待ちください。」（⛔ G-57：その間でAIが報告し、判定は「岡田ですね。」だけを見て、
    # 取り次ぎの最中の方に目的の人かを聞いた。間0.6秒の台詞では18本中1本ほどなので、間を1.5秒にして毎回その形を作る）
    "目的の人_取り次ぎの前の間": Scene(TARGET_BUSINESS, [W("hold_target_pause"), T("taken_over_okada")] + FULL, lambda c: {
        "代わった方に全部": full_opening(c),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target_pause")) == "",
        "取り次ぐ方には流さない": not any(x["op"] == "report_party" and x["args"].get("party") == "target"
                                       and x["callee_turns"] == 1 and x["reply"]["result"] not in ("hold", "wait") for x in ops(c)),
        "答えが取れた": got(c, "Q1", "はい")}),
    # 受付の方が岡田様に取り次いだあと、代わった方が名乗らずに「はい、お電話代わりました。」：ご本人として繋ぐ（確かめない）。
    # ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』に
    #    なりますよね」⛔ 直す前は「確かめた」を正しい形にしていた（判定に、その前の受付の方の言葉を渡していなかった）。
    # ⛔ 2026-10-19 の見直し：前は代わった方の台詞のあとに「はい、そうです。」を台本で置いていた＝確かめなかった回は、それが最初の質問への答えになっていた
    "目的の人_名乗らない方": Scene(TARGET_BUSINESS, [W("hold_target"), T("taken_over_unnamed")] + FULL, lambda c: {
        "確かめない": not any(x["reply"]["result"] == "confirm_target" for x in ops(c)),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_用件を聞かれる": Scene(TARGET_BUSINESS, [W("what_for"), W("hold_target"), T("taken_over_okada")] + FULL, lambda c: {
        "用件と呼ぶ一言": any(played_parts(x["reply"]) == ["purpose", "target"] for x in ops(c) if x["op"] == "replay_opening"),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_不在": Scene(TARGET_BUSINESS, [W("absent_okada"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "用件を受付に流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
    # 受付の方が取り次いだあと、また出て「出られない」と伝える：取り次いだあとでも、ご本人とは決めない（不在として、戻る時刻を聞いて締める）
    # ⚠️ 2026-10-19 ユーザー：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともあるので、取次後は必ずしも、
    #    本人とは限らないので注意、見直しが必要」
    "目的の人_取り次いだあとに出られない": Scene(TARGET_BUSINESS, [W("hold_target"), W("busy_after_hold"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "用件を受付に流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
    # 受付の方が「担当の者に代わります」と取り次ぎ、代わった方が名乗らずに出る：✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）
    # ⇒ 繋いで代わった方に全部を流し、答えを取る（2つの判定の読みが分かれたときは、目的の人かを1回確かめてから＝Flow._calling_reported）
    "目的の人_担当の者に取り次がれる": Scene(TARGET_BUSINESS, [W("transfer"), T("taken_over_unnamed")] + FULL, lambda c: {
        "取り次ぎの間は話さない": plain(said_to(c, "transfer")) == "",
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")}),
    # 「担当の者に代わります」のあと、違うお名前の方が出る（お呼びした岡田様ではない）：確かめずに、名乗ってから岡田様をお呼びし直す（確かめた
    # ときは、その方の「いいえ」のあとに）。その方が取り次いで、岡田様が出たら全部を流す
    "目的の人_担当の別の方が出る": Scene(TARGET_BUSINESS, [W("transfer"), W("taken_over") | {"if_asked_target": W("not_okada")["wav"]},
                                                     W("hold_target"), T("taken_over_okada")] + FULL,
                                     lambda c: not_the_target("taken_over", "not_okada")(c) | {
                                         "取り次ぎの間は話さない": plain(said_to(c, "transfer")) == ""}),
    # 名乗らずに出た方が、目的の人かを聞かれて「いえ、私は山田と申します。」：同じ方にもう一度確かめず、お詫びのあと岡田様をお呼びし直す
    # （判定が「代わった人」と読めば、確かめずにお呼びし直す）
    "目的の人_確かめにいいえ": Scene(TARGET_BUSINESS, [W("taken_over_unnamed") | {"if_asked_target": W("not_okada")["wav"]},
                                                W("hold_target"), T("taken_over_okada")] + FULL,
                                  not_the_target("taken_over_unnamed", "not_okada")),
    # 名前を確かめた方が、間を置いて名乗りを聞く（✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）：言葉の途中で
    # 決めず、言い終わってから、名乗りと呼ぶ一言を流す（用件は聞かれたときだけ）。目的の人かは聞かない
    "目的の人_名前を確かめてから名乗りを聞かれる": Scene(TARGET_BUSINESS, [W("okada_whois_pause"), W("hold_target"), T("taken_over_okada")] + FULL,
                                                lambda c: {
        "名乗りと呼ぶ一言": any(played_parts(x["reply"]) == ["intro", "target"] for x in ops(c) if x["op"] == "replay_opening"),
        "聞いた方には確かめも用件も流さない": not any(r in ("confirm_target", "call_target", "next") for r in replies_to(c, "okada_whois_pause")),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "", "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")}),
    # 違うお名前の方が出て、お呼びした方の不在を伝える（✅ 2026-10-22 ユーザー：「違う名乗りの方が、岡田はいないと言っても、また、岡田様を
    # 呼ぶんですか？」）：お呼びし直さず・確かめず、戻る時刻を聞いて締める
    "目的の人_担当の別の方が不在を伝える": Scene(TARGET_BUSINESS, [W("transfer"), W("taken_over_absent"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "お呼びし直さない": not any(r in ("call_target", "confirm_target") for r in replies_to(c, "taken_over_absent")),
        "用件を流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
    # 受付の方が不在を伝えてから、別の方に取り次ぐ：取り次ぎの間は話さず、代わった方には岡田様をお呼びし直さずに、かけ直しの申し出
    # （⛔ 2026-10-22 の見直し：不在の読みを電話口の方ごとに数えていたので、代わった方に岡田様をお呼びし直していた）
    "目的の人_不在で別の方に代わる": Scene(TARGET_BUSINESS, [W("away_to_other"), W("taken_over_yamada"), W("back_5pm"), W("affirm")], lambda c: {
        "取り次ぎの間は話さない": plain(said_to(c, "away_to_other")) == "",
        "理由": last_fact(c, "callback").get("why") == "absent", "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "お呼びし直さない": not any(r in ("call_target", "confirm_target") for r in replies_to(c, "taken_over_yamada")),
        "用件を流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
    # かけ直しの質問の途中で、お呼びした方が戻って代わる（2026-10-22 の見直しで見つけた形。⛔ G-57 tc24：代わって出た岡田様に「恐れ入りますが
    # 何時ごろお戻りになられますでしょうか？」を流し、業務の質問を聞かずに締めた）：1回確かめて繋ぎ、業務の質問を聞いて、ふつうに締める
    "目的の人_かけ直しの途中で戻る": Scene(TARGET_BUSINESS, [W("absent_okada"), W("just_back"), T("taken_over_okada")] + FULL, lambda c: {
        "かけ直しへ": last_fact(c, "callback").get("why") == "absent", "取り次ぎの間は話さない": plain(said_to(c, "just_back")) == "",
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい"),
        "かけ直しは要らない": last_fact(c, "callback").get("connected") is True, "締め": closing_of(c) == CLOSING}),
    # 受付の方が、名乗り・用件のほかのことを聞く（✅ 2026-10-22 ユーザー：「想定の質問ならそれでいいけど、録音や想定にない質問だった場合は？」）：
    # 聞き流さず、目的の人かを確かめずに答え、作らず約束せず、取り次がれた岡田様には全部を流す
    # 決まった答えのある問い（名乗り・用件・想定問答）は録音（✅ 2026-10-22 ユーザー：「必ずどんな営業トークにも出てくるパターンはシステム側に録音して
    # おいた方がよくない？」。Flow._answer）。⛔ tc18（直す前）：「答えた」をAIの声の有無で見ていたので、聞き流した2本と、AIが自分の言葉で目的の人の
    # 名前を言った1本を合格にしていた
    **{f"目的の人_{label}": Scene(business, [W(line), W("hold_target"), T("taken_over_okada")] + FULL, lambda c, line=line, parts=parts: {
        "決まった答えで答えた": answered_with(c, line, *parts), "自分の言葉で話さない": own_words(c, line) == "",
        "確かめない": not any(r in ("confirm_target", "call_target") for r in replies_to(c, line)),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "", "約束しない": no_promise(c),
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")})
       for label, business, line, parts in (
           ("関係を聞かれる", TARGET_BUSINESS, "relation_ask", ("purpose", "intro", "all")),
           ("番号の出どころを聞かれる_想定問答あり", TARGET_FAQ, "number_from", ("番号の出どころ", "answer_番号の出どころ")))},
    # 録音の無い問い（✅ 2026-10-22 ユーザー：「想定外の時はどうするのかを聞いているんですよ」）：AIが業務の情報の範囲で自分の言葉で答える。
    # 情報に無いこと（番号の出どころ）は作らない。どちらも、名前を自分の声で言わず・約束せず・目的の人かを確かめず、返事のあともう一度だけお呼びする
    **{f"目的の人_{label}": Scene(business, [W(line)] + rest, lambda c, line=line, want=want: {
        "自分の言葉で答えた": own_answered(c, line), "情報で答えた": want in plain(said_to(c, line)) if want else True,
        "名前を言わない": not re.search(r"岡田|おかだ|アオバ|佐藤", own_words(c, line)),
        "確かめない": not any(r in ("confirm_target", "call_target") for r in replies_to(c, line)),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "", "約束しない": no_promise(c),
        "代わった方に全部": full_opening(c), "答えが取れた": got(c, "Q1", "はい")})
       for label, business, line, rest, want in (
           ("番号の出どころを聞かれる", TARGET_BUSINESS, "number_from", [W("hold_target"), T("taken_over_okada")] + FULL, ""),
           ("会社の所在地を聞かれる", TARGET_INFO, "where_company", [W("i_see"), W("hold_target"), T("taken_over_okada")] + FULL, "港区"))},
    # ── 相手の申し出・お呼びした方の事情（✅ 2026-10-22 ユーザー：「代わりの方・伝言・折り返し・在籍していない・ほかの手段・長期の不在が
    #    あるのが分かっているなら、それを想定の録音にすべきじゃないですか？」）。応じる言葉はどれも決まった録音（Flow._not_available）
    "目的の人_折り返しの申し出": Scene(TARGET_BUSINESS, [W("callback_offer"), W("back_5pm"), W("affirm")], lambda c: {
        "申し出を受けた": last_fact(c, "callback").get("offer") == "callback", "お礼の録音": "offer_thanks" in played_ids(c),
        "分かりかねますと言わない": "分かりかね" not in ai_said_all(c), "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c), "足す質問は録音": callback_recorded(c)}),
    "目的の人_伝言の申し出": Scene(TARGET_BUSINESS, [W("message_offer"), W("back_5pm"), W("affirm")], lambda c: {
        "申し出を受けた": last_fact(c, "callback").get("offer") == "message", "お礼の録音": "offer_thanks" in played_ids(c),
        "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""), "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c)}),
    "目的の人_代わりの方（判断できる）": Scene(TARGET_BUSINESS, [W("hold_target"), W("substitute_offer"), W("authority_yes")] + FULL, lambda c: {
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "", "お立場を伺った": "callback_authority" in played_ids(c),
        "戻る時刻を聞かない": "callback_back_time" not in played_ids(c),
        "代わりの方と話した": last_fact(c, "connect").get("who") == "substitute", "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_代わりの方（判断できない）": Scene(TARGET_BUSINESS, [W("hold_target"), W("substitute_offer"), W("authority_no"), W("back_5pm"),
                                                         W("affirm")], lambda c: {
        "お立場を伺った": "callback_authority" in played_ids(c), "お礼の録音": "offer_thanks" in played_ids(c),
        "用件より先へ進まない": not c.get("facts", {}).get("connect"), "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c)}),
    "目的の人_在籍していない": Scene(TARGET_BUSINESS, [W("departed"), W("successor_hold"), T("taken_over_tanaka")] + FULL, lambda c: {
        "在籍していないと受けた": bool(c.get("facts", {}).get("departed")),
        "お詫びと後任の方のお呼び": played_ids(c)[:2] == ["departed", "successor_call"],
        "取り次ぎの間は話さない": plain(said_to(c, "successor_hold")) == "",
        "後任の方と話した": last_fact(c, "connect").get("who") == "successor", "代わった方に全部": full_opening(c),
        "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_後任の方が電話口": Scene(TARGET_BUSINESS, [W("successor_self") | {"if_asked_target": W("affirm")["wav"]}] + FULL, lambda c: {
        "在籍していないと受けた": bool(c.get("facts", {}).get("departed")),
        "お詫びと後任の方の確かめ": played_ids(c)[:2] == ["departed", "successor_confirm"],
        "後任の方と話した": last_fact(c, "connect").get("who") == "successor", "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_メールで": Scene(TARGET_BUSINESS, [W("email_request"), W("email_answer"), W("affirm")], lambda c: {
        "ほかの手段と受けた": (last_fact(c, "callback").get("why"), last_fact(c, "callback").get("offer")) == ("other_means", "email"),
        "アドレス": "example" in (last_fact(c, "callback").get("email") or ""), "聞く一言は録音": "callback_email" in played_ids(c),
        "お礼の締め": closing_of(c) == CLOSING and end_reason(c) == "other_means", "約束しない": no_promise(c)}),
    "目的の人_長期の不在": Scene(TARGET_BUSINESS, [W("long_absence"), W("back_date"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "long_absent", "戻る日": "月曜" in (last_fact(c, "callback").get("time") or ""),
        "聞く一言は録音": "callback_back_date" in played_ids(c), "締め": closing_of(c) == CLOSING, "約束しない": no_promise(c)}),
    "目的の人_受付で断られる": Scene(TARGET_BUSINESS, [W("reception_refuse")], lambda c: {
        "発信禁止の申告": bool(c.get("facts", {}).get("do_not_call")), "締め": closing_of(c) == CLOSE_DECLINED}),
}


def measures(call: dict, scene: str) -> dict:
    checks = SCENES[scene].expect(call)
    return {"checks": checks, "pass": all(checks.values()), "failed": [k for k, v in checks.items() if not v],
            "said": [said(t)[:80] for t in call["turns"]],
            "restate_gap_s": [t["gap_s"] for t in call["turns"] if t.get("gap_s") is not None],  # 止めてから言い直しの声まで（6-7）
            "ops": [f"{x['op']}({','.join(f'{k}={v}' for k, v in x['args'].items())})→{x['reply']['result']}" for x in ops(call)]}


async def run(args) -> dict:
    ring = KeyRing(args)  # 上限に当たったら次のキーで、その通話を初めからやり直す（common.KeyRing.acall）
    judges = make_judges(args)
    rows = []
    for rep in range(args.repeat):
        for name, sc in SCENES.items():
            if args.only and not any(name.startswith(o) for o in args.only):
                continue
            for vt in args.type or VOICE_TYPES:
                head = {"rep": rep, "scene": name, "type": vt, "note": sc.note}
                missing = [x["wav"] for x in sc.lines if not verified(callee_wav(x["wav"], callee_voice_for(vt)))]
                if missing:  # 確かめに通っていない相手役の声では動かさない
                    rows.append({**head, "skipped": f"相手役の声が確かめに通っていない：{missing}"})
                    say(f"[{rep}] {name} {vt}：動かさない（相手役の声が確かめに通っていない {missing}）")
                    continue
                try:
                    call, _ = await ring.acall(args.model, lambda client: run_call(
                        client, args.model, sc.business, vt, sc.lines, opening_mode="recorded", use_ear=True, save_audio="bad",
                        audio_label=f"G57_{name}_{vt}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, play_recordings=True,
                        callee_voice="swap", **judges, **FAST, **sc.kw))
                    row = keep_call(head, call, lambda c: measures(c, name))
                except Exception as exc:
                    row = {**head, "error": describe_error(exc)}
                rows.append(row)
                say(f"[{rep}] {name} {vt}：{'合格' if row.get('pass') else '不合格'} {row.get('failed', '')} {row.get('error', '')}")
                save_partial(f"G-57{args.tag}", {"rows": rows})
                await asyncio.sleep(args.pause)
    judged = {k: getattr(args, k) for k in ("judge", "judge2")} | {"standin": None if args.no_standin else STANDIN, "down": args.judge_down}
    return {"model": args.model, "judges": judged, "summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if "checks" in r]
    s = {}
    for name in SCENES:
        mine = [r for r in done if r["scene"] == name]
        if not mine:
            continue
        s[name] = {"合格": f"{sum(1 for r in mine if r['pass'])}/{len(mine)}",
                   "外れた決まり": {k: sum(1 for r in mine if k in r["failed"]) for k in mine[0]["checks"] if any(k in r["failed"] for r in mine)}}
    s["全体"] = f"{sum(1 for r in done if r['pass'])}/{len(done)}"
    s["誤り・動かさなかった"] = [f"{r['scene']} {r.get('type')}：{r.get('skipped') or r.get('error', {}).get('message', '')[:60]}"
                           for r in rows if "error" in r or "skipped" in r]
    say("数え：", json.dumps(s, ensure_ascii=False))
    return s


def tally(paths: list[str]) -> dict:
    """保存した結果を、いまの数え方で数え直す（問い合わせはしない）。"""
    rows = []
    for p in paths:
        for r in json.loads(open(p, encoding="utf-8").read())["rows"]:
            if "turns" in r and r["scene"] in SCENES:
                r.update(measures(r, r["scene"]))
            rows.append(r)
    return {"merged_from": paths, "summary": summarize(rows), "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--only", action="append", help="場面の名前の頭で絞る")
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--tag", default="")
    p.add_argument("--tally", nargs="+", help="保存した結果をまとめて数え直す（問い合わせはしない）")
    add_judge_args(p)
    args = p.parse_args()
    if args.tally:
        save_result("G-57-TALLY", tally(args.tally))
        return
    save_result(f"G-57{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-57{args.tag}")


if __name__ == "__main__":
    main()
