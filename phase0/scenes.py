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
from phrases import CLOSE_DECLINED, CLOSING

# 目的の人のある業務（DESIGN.md 4-3）：営業部部長の岡田様
TARGET_BUSINESS = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長"))
FULL = [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")]


def T(name: str) -> dict:
    """目的の人ご本人の台詞：目的の人かを聞かれたら（Flow._ask_target）、本物の方と同じく「はい、そうです。」と答える（convo.ANSWER_AGAIN）。"""
    return W(name) | {"if_asked_target": W("affirm")["wav"]}
# AIが言ってはいけない約束（かけ直す・連絡する。Intent.md 9-4「勝手に約束しない」・G-22）と、人間だという答え（原則8）
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


def said_to(call: dict, wav_name: str) -> str:
    """その台詞へのAIの声の全部（続きの待ち＝followup の分も）。"""
    def line_of(t):
        cl = t.get("callee")
        cl = cl.get("followup_of", cl) if isinstance(cl, dict) else cl
        return cl.get("wav", "") if isinstance(cl, dict) else ""
    return "".join(said(t) for t in call["turns"] if line_of(t).endswith(f"_{wav_name}.wav"))


def callback_recorded(call: dict) -> bool:
    """かけ直しで足す質問を、録音で流したか（AIに言わせない。⛔ 1回目・2回目：AIが言うと「折り返しのお電話はお約束できかねます」などを足した）。"""
    return any(x["op"] == "request_callback" and (x["reply"].get("play") or [{}])[0].get("kind") == "recorded" for x in ops(call))


def no_promise(call: dict) -> bool:
    return not any(PROMISE.search(said(t)) for t in call["turns"])


def played_parts(reply: dict) -> list[str]:
    return [p.get("part", p.get("kind")) for p in reply.get("play") or []]


def connect_reply(call: dict) -> dict:
    """目的の人に繋がったときの返事（流れの制御が、名乗り・用件・最初の質問を流した返事）。繋ぐのは、AIの「目的の人」の報告か、代わった方への
    名乗りの流し直しの頼み（判定が「ご本人」。Flow._target_reported の1か所）の、どちらか。"""
    return next((x["reply"] for x in ops(call)
                 if ((x["op"] == "report_party" and x["args"].get("party") == "target")
                     or (x["op"] == "replay_opening" and x["args"].get("part") == "handover"))
                 and x["reply"]["result"] in ("next", "confirm_required")), {})


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
    # ── 「AIですか」・範囲外の質問（原則8・Intent.md 9-4）
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
    # ── AIの声が渡した言い方から外れたとき（DESIGN.md 6-7）：外れることはめったに無いので、日時の言い直しのターンを外れたものとして止め、
    #    相手に聞こえていたものに合わせた言い方で、新しいつなぎが言い直すか（✅ 2026-10-19 ユーザー：「違和感のない形にしないと、AIや機械だと
    #    勘繰られますよ」「明らかに違う内容を喋ろうとしているのが事前に分かるのであれば、止めてもOK」＝offscript.SpeechGate・restate）
    **{f"言い間違えの直し_{label}": Scene(BUSINESS, FULL, restated(head), kw={"off_script_at": 1, "off_script_heard": kind})
       for kind, label, head in (("none", "聞こえる前", "ありがとうございます"), ("opener", "受けの言葉だけ", "それでは"),
                                 ("content", "中身まで", "失礼いたしました改めまして"))},
    "範囲外の質問": Scene(BUSINESS, [W("yes_reserved"), W("out_of_scope")] + FULL[1:], lambda c: {
        "答えた（聞き流さない）": said(turn_of(c, "out_of_scope")) != "", "金額を作らない": "円" not in said(turn_of(c, "out_of_scope")),
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
        "代わった方に全部": played_parts(connect_reply(c))[:3] == ["greeting", "intro", "purpose"],
        # ⛔ 1回目は数えていなかった：取り次ぎを待つ間に、AIが相手の言葉を真似た・ありもしない用件を7秒話した（3本中2本）
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "取り次ぐ方には流さない": not any(x["op"] == "report_party" and x["args"].get("party") == "target"
                                       and x["callee_turns"] == 1 and x["reply"]["result"] not in ("hold", "wait") for x in ops(c)),
        "答えが取れた": got(c, "Q1", "はい")}),
    # 受付の方が、名前を確かめてから間を置いて「少々お待ちください。」（⛔ G-57：その間でAIが報告し、判定は「岡田ですね。」だけを見て、
    # 取り次ぎの最中の方に目的の人かを聞いた。間0.6秒の台詞では18本中1本ほどなので、間を1.5秒にして毎回その形を作る）
    "目的の人_取り次ぎの前の間": Scene(TARGET_BUSINESS, [W("hold_target_pause"), T("taken_over_okada")] + FULL, lambda c: {
        "代わった方に全部": played_parts(connect_reply(c))[:3] == ["greeting", "intro", "purpose"],
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
        "代わった方に全部": played_parts(connect_reply(c))[:3] == ["greeting", "intro", "purpose"], "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_用件を聞かれる": Scene(TARGET_BUSINESS, [W("what_for"), W("hold_target"), T("taken_over_okada")] + FULL, lambda c: {
        "用件と呼ぶ一言": any(played_parts(x["reply"]) == ["purpose", "target"] for x in ops(c) if x["op"] == "replay_opening"),
        "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "代わった方に全部": played_parts(connect_reply(c))[:3] == ["greeting", "intro", "purpose"], "答えが取れた": got(c, "Q1", "はい")}),
    "目的の人_不在": Scene(TARGET_BUSINESS, [W("absent_okada"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "戻る時刻": "5時" in (last_fact(c, "callback").get("time") or ""),
        "用件を受付に流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
    # 受付の方が取り次いだあと、また出て「出られない」と伝える：取り次いだあとでも、ご本人とは決めない（不在として、戻る時刻を聞いて締める）
    # ⚠️ 2026-10-19 ユーザー：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともあるので、取次後は必ずしも、
    #    本人とは限らないので注意、見直しが必要」
    "目的の人_取り次いだあとに出られない": Scene(TARGET_BUSINESS, [W("hold_target"), W("busy_after_hold"), W("back_5pm"), W("affirm")], lambda c: {
        "理由": last_fact(c, "callback").get("why") == "absent", "取り次ぎの間は話さない": plain(said_to(c, "hold_target")) == "",
        "用件を受付に流さない": connect_reply(c) == {}, "締め": closing_of(c) == CLOSING, "足す質問は録音": callback_recorded(c)}),
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
