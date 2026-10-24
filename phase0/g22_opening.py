"""G-22：名乗りを録音で流す形（DESIGN.md 6-13 の案）で、会話が成り立つか。決める前に確かめる（2026-10-13 ユーザー指示）。

(1) 相手が、録音が終わったあとに聞き返したとき（「はい？」「どちら様ですか」「何のご用件ですか」「もう一度お願いします」
    「今ちょっと忙しいんですけど」「担当に代わります」）
(2) 録音を流している最中に、相手が話し始めたとき（録音のどこまで届いたかは playback が決め、
    instruction.barge_in_note でAIへ知らせる）。短い相づち（「はい」）では止めない決まりは、playback の試験で確かめる。
相手の発話は、録った声（results/callee/ の電話の音質の声。Puck）を送る。通話の動かし方は convo の1か所。
確かめること（AIの声は別の耳で文字にしたもので見る）：
- 聞き返し・話の途中に、ふさわしく答えたか（場面ごとの言葉が出たか）
- まだ伝えていないこと（社名・名前・用件・最初の質問）を、必要な場面で伝えたか。届いたことを、要らない場面で繰り返していないか
- 書き出しが崩れていないか。最初の質問へ戻れたか（相手の「はい、予約しています」を Q1 として受け、次の質問へ進む）
"""
from __future__ import annotations

import asyncio
import json
import re

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from common import drop_partial, read_wav_pcm16_mono, save_partial
from convo import AUDIO_DIR, run_call
from flowctl import GOT, Question, tool_declarations
from instruction import VOICE_TYPES, Business, build
from listen import differences, hear_feeds, in_all, in_any
from make_callee_samples import verified
from live_util import build_config
from turn_check import bad_kinds
from topics import decide as decide_topic
from units import decide

Q1_TEXT = "ご予約はされていますでしょうか？"
Q2_TEXT = "ご希望の日時をお聞かせいただけますでしょうか？"
Q3_TEXT = "ご来店の人数をお伺いしてもよろしいでしょうか？"
BUSINESS = Business(
    company="株式会社アオバ物流", caller="佐藤", purpose="ご予約の確認", purpose_line="ご予約の確認でお電話いたしました。",
    # 聞くこと（topic）は取込と同じ決め方で（topics.decide。G-43）。試験の業務ではモデルを呼ばないので、決まりだけで決まる分になる
    # （Q1 は「（聞くこと）を＋聞く言葉」の形ではないので空＝聞くことを言わずに聞き返す。前は手で「ご予約の有無」と書いていた）
    questions=[Question("Q1", Q1_TEXT, "yes_no", conditions=("はい", "いいえ"), topic=decide_topic(Q1_TEXT, [])),
               # ✅ 2026-10-15 ユーザー：お客様に向ける言い方は、命令の形（「〜ください」）や冷たい言い回しを避け、柔らかく同意をいただく
               #    接客の話し方に ⇒ 試験の業務設定（トークスクリプト）の質問も直した（前は「ご希望の日時をお聞かせください。」など。G-36 まで）
               # ✅ 2026-10-15 ユーザー（G-37 を聞いて）：言葉の途中の読点で不自然な間が入る ⇒ 読点を外した
               Question("Q2", Q2_TEXT, "datetime", conditions=("答えた",), topic=decide_topic(Q2_TEXT, [])),
               Question("Q3", Q3_TEXT, "number", conditions=("答えた",), topic=decide_topic(Q3_TEXT, []),
                        unit=decide(Q3_TEXT, []))],  # 単位は取込と同じ決め方で（units.decide。G-41。この文は決まりだけで「人」に決まる）
)
PARTS = BUSINESS.opening_parts()  # [あいさつ, 名乗り, 用件, 最初の質問]
NAME = ["アオバ", "青葉", "あおば", "大場", "オーバ", "オオバ"]  # 社名（「ア」がオ寄りに聞こえるものも、言えたとみなす）


def W(name: str) -> dict:
    return {"wav": f"Puck_{name}.wav"}


# need_any：AIの最初の返事に、どれか1つは出てほしい言葉の組（組ごとに判定）
# no_reintro：最初の返事で、名乗り直す必要が無い場面（名乗り直したら記録する。会話は成り立つので不合格にはしない）
SCENES = {
    "K1_どちら様": {"callee": [W("whois"), W("yes_reserved")], "barge": None,
                  "need_any": [NAME, ["佐藤"], ["予約"]], "then_q2": True},
    "K2_ご用件": {"callee": [W("what_for"), W("yes_reserved")], "barge": None,
                "need_any": [["予約"], ["確認"]], "then_q2": True, "no_reintro": True},
    "K3_もう一度": {"callee": [W("again"), W("yes_reserved")], "barge": None,
                 "need_any": [NAME, ["佐藤"], ["予約"]], "then_q2": True},
    "K4_はい？": {"callee": [W("hai_q"), W("yes_reserved")], "barge": None,
                "need_any": [["予約"]], "then_q2": True},
    # 設計（DESIGN.md 6-11・4-2）：相手が急いでいるときは、念のための確認をせずに終える。かけ直すかは next_action が決める
    # （⛔AIが「改めてお電話します」と約束しない）。⛔ 直す前の合格の条件は「お時間」「改めて」などを求めていた＝設計に無い条件
    "K5_忙しい": {"callee": [W("busy")], "barge": None, "hurried": True,
                "need_any": [["申し訳", "失礼", "恐れ入", "すみません", "ありがとう"]]},
    "K6_代わる": {"callee": [W("transfer"), W("taken_over"), W("yes_reserved")], "barge": None,
                "need_any": [["お願い", "お待ち", "かしこまり", "承知"]], "no_question_first": True,
                "second_need_any": [NAME, ["佐藤"], ["予約"]], "then_q2_at": 2},
    "B0_最初から": {"callee": [W("barge_who"), W("yes_reserved")], "barge": ([], PARTS),
                  "need_any": [NAME, ["佐藤"], ["予約"]], "then_q2": True},
    "B1_あいさつのあと": {"callee": [W("barge_who"), W("yes_reserved")], "barge": (PARTS[:1], PARTS[1:]),
                     "need_any": [NAME, ["佐藤"], ["予約"]], "then_q2": True},
    "B2_名乗りのあと": {"callee": [W("barge_what"), W("yes_reserved")], "barge": (PARTS[:2], PARTS[2:]),
                    "need_any": [["予約"], ["確認"]], "then_q2": True, "no_reintro": True},
    "B3_用件のあと": {"callee": [W("barge_yes2"), W("datetime_pm"), W("affirm")], "barge": (PARTS[:3], PARTS[3:]),
                   "need_any": [["日時", "日にち", "お日にち"]], "q1_answered_first": True, "no_reintro": True},
}


def judge(scene: dict, call: dict) -> dict:
    turns = call["turns"]
    first = turns[0].get("ear", "") if turns else ""
    # 相手に何が聞こえたかは、2通りの聞かせ方の両方で見る（first_heard。1回だけだと、別の耳は頭を約5%聞き違える。G-24）
    heard = call.get("first_heard") or {"1回": first}
    c = {"first_said": first, "first_heard": heard}
    # 誤りに数えるのは、2通りの両方でそろったときだけ（言うべき言葉は、どちらか一方で聞こえれば、言えたとする）
    c["need_ok"] = in_any(heard, lambda v: all(any(w in v for w in group) for group in scene["need_any"]))
    # 名乗り直し：「申します」だけでなく、名前を言ったか（⛔ 2026-10-13：「佐藤です」と名乗り直した回を、見逃していた）。
    # AIが言ったことなので、言うつもりの言葉（AI自身の文字）と、聞こえた言葉の、どちらかにあれば名乗り直したとする
    said = (turns[0].get("intended") or "") if turns else ""
    c["reintroduced"] = any("申します" in s or BUSINESS.caller in s for s in (said, first))
    if scene.get("no_reintro"):
        c["reintro_ok"] = not c["reintroduced"]
    if scene.get("hurried"):
        c["hurried_ok"] = call["hurried"]  # 「急いでいる」として終わりを報告した（request_end の callee_hurry）
        # ⛔ 2026-10-13 G-22 の2回目：6本中5本で「また改めてご連絡いたします」と約束していたのに、採点が見ていなかった
        c["no_promise"] = not any(re.search(r"改めて|またお電話|ご連絡いたします|かけ直", s or "") for s in
                                  [t.get("intended") for t in turns] + [t.get("ear") for t in turns])
    if scene.get("no_question_first"):
        c["no_question_ok"] = not any(w in first for w in ("予約", "日時", "人数"))
    if "second_need_any" in scene and len(turns) > 1:
        second = turns[1].get("ear", "")
        c["second_ok"] = all(any(w in second for w in g) for g in scene["second_need_any"])
    # 書き出しの崩れ：最初の返事の声（別の耳）と、言うつもりの言葉（AI自身の文字起こし）の、最初の10音の食い違い
    if turns and turns[0]["intended"]:
        c["head_broken"] = in_all(heard, lambda v: any(d["at"] < 10 and d["heard"] != "お" and "あおば" not in d["script"]
                                                       for d in differences(turns[0]["intended"], v)))
    # 最初の質問へ戻れたか：Q1 が「はい」で受け付けられ、次の質問（日時）を聞いた
    q1 = call["items"]["Q1"]
    c["q1_yes"] = q1["condition"] == "はい" and q1["state"] in GOT
    idx = scene.get("then_q2_at", 1)
    if scene.get("then_q2") or "then_q2_at" in scene:
        c["asked_q2"] = len(turns) > idx and any(w in turns[idx].get("ear", "") for w in ("日時", "日にち", "お日にち"))
    if scene.get("q1_answered_first"):
        c["asked_q2"] = c["need_ok"]
    c["bad_turns"] = bad_kinds(turns)
    keys = [k for k in ("need_ok", "no_question_ok", "second_ok", "asked_q2", "hurried_ok", "no_promise") if k in c]
    if scene.get("then_q2") or "then_q2_at" in scene or scene.get("q1_answered_first"):
        keys.append("q1_yes")
    c["passed"] = all(c[k] for k in keys) and not c.get("head_broken") and not c["bad_turns"]
    return c


async def run(args) -> dict:
    if args.dry_run:
        for t in VOICE_TYPES:
            v, text = build(BUSINESS, t, opening_mode="recorded")
            build_config(voice=v, system_instruction=text, tools=tool_declarations(BUSINESS.questions))
        return {"dry_run": True, "instruction": build(BUSINESS, "L4", opening_mode="recorded")[1], "parts": PARTS}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for name, scene in SCENES.items():
            if args.scene and name not in args.scene:
                continue
            unverified = [c["wav"] for c in scene["callee"] if not verified(c["wav"])]
            if unverified:  # 確かめに通っていない相手役の声では動かさない（⛔「はい？」が「はい。」に聞こえていた）
                rows.append({"rep": rep, "scene": name, "skipped": f"相手役の声が確かめに通っていない：{unverified}"})
                say(f"[{rep}] {name} 動かさない：相手役の声が確かめに通っていない {unverified}")
                continue
            for vt in args.type or VOICE_TYPES:
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, scene["callee"], opening_mode="recorded",
                                          barge_in=scene["barge"], save_audio="all",
                                          audio_label=f"G22_{name}_{rep}")  # ユーザーが聞いて決める試験なので、声を全部残す
                    if call["turns"] and call["turns"][0].get("audio_file"):
                        pcm, prate = read_wav_pcm16_mono(AUDIO_DIR / call["turns"][0]["audio_file"])
                        call["first_heard"] = await hear_feeds(client, pcm, prate)
                    row = {"rep": rep, "scene": name, "type": vt, "checks": judge(scene, call), **call}
                except Exception as exc:
                    row = {"rep": rep, "scene": name, "type": vt, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {name} {vt} 合格={c.get('passed')} 名乗り直し={c.get('reintroduced')} 崩れ={c.get('head_broken')} "
                    f"Q1={c.get('q1_yes')} 次の質問={c.get('asked_q2')} 最初の返事={c.get('first_said', '')[:70]} {row.get('error', '')}")
                await asyncio.sleep(args.pause)
                save_partial("G-22", {"model": args.model, "parts": PARTS, "rows": rows})  # 1本ごとに途中の結果を残す
    summary = {"runs": sum(1 for r in rows if "skipped" not in r), "passed": sum(1 for r in rows if r.get("checks", {}).get("passed")),
               "by_scene": {n: [r.get("checks", {}).get("passed") for r in rows if r["scene"] == n and "skipped" not in r] for n in SCENES},
               "skipped": sorted({r["scene"] for r in rows if "skipped" in r}),
               "reintroduced_where_not_needed": sum(1 for r in rows if r.get("checks", {}).get("reintro_ok") is False)}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "parts": PARTS, "summary": summary, "rows": rows}


def rejudge(path: str) -> dict:
    """保存した結果に、いまの判定（judge）を当て直す（判定の誤りを直したとき。キーを使わない）。"""
    d = json.load(open(path, encoding="utf-8"))
    for r in d["rows"]:
        if "turns" in r:
            r["checks"] = judge(SCENES[r["scene"]], r)
    return d


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=4.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--scene", choices=sorted(SCENES), action="append")
    args = p.parse_args()
    save_result("G-22", asyncio.run(run(args)))
    drop_partial("G-22")


if __name__ == "__main__":
    main()
