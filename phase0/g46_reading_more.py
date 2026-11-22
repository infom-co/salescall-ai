"""G-46：名前の読みの確かめ（flowctl.reading_question）を、まだ試していない2つの形で確かめる（2026-10-16 ユーザー：「そのまま試験を進めて」）。

(A) 録音で流す形：次の質問と締めを録音で流す形（play_recordings。G-37）の中に、読みの確かめを挟む。読みの確かめは名前が入るので、
    AIが話す（録音にできない）。見ること：読みの確かめが場面どおりに済むか／確かめのあとの質問が録音で流れ、つなぎの言葉が
    「それでは」のままか（⛔ 見直しで直したずれ）／録音を流したターンでAIが話していないか。名前は「中島」（G-45 と同じ）。
(B) 読みの候補が3つある名前：「角田」＝つのだ（まず言ってみる読み）・かくた・かどた。相手が「かくた」「かどた」に直したとき、
    次に確かめる読みが、相手の言った読みになるか。
    ⚠️ 相手役は台本どおりにしか答えない（違う読みで聞き直されても「はい、そうです。」と答える）。⇒ 場面どおりかは、
    相手が認めたかではなく、**直されたあとにプログラムが確かめた読みが、相手の言った読みか**で決める。
(C) 読みの中に「さ」「ん」のある読みへの直し（2026-11-22 分身 AN＝担当 AK の棚卸しの P13・分身 AJ の書き置き rv191/AJ/notes.md の (続6-1)）：「斎藤」＝さいとう（まず言って
    みる読み）・さいどう、「菅野」＝すがの（まず言ってみる読み）・かんの。直していただいた読みの「さ」「ん」まで消していた形（65d28d02 で直した＝「さいどう」を「いどう」と確かめた）が、
    (A)(B) の名前（なかしま・かくた・かどた）には出なかった。見ること：(B) と同じ（直されたあとにプログラムが確かめた読みが、相手の言った読みか）。
(D) 業務の名前の質問（名前の型）の答えの言い直し「（お名前）様で…」を聞いたあとに直されたとき（同じ AJ の (6-4)）：「カドタです。」→「はい、ありがとうございます。かどた様で
    よろしいでしょうか？」→「いや、カクタです。」。見ること：会話AIが直した値をどう報告するか（「様」を付けるか＝ai_corrections・ai_sama）と、記録の値に「様」が残らず
    （record_sama）、直したあとの言い直しの「様」が1回だけか（restated_sama）。(C)(D) の「様」の数えは、どの場面にも当てる（読みの確かめの文も「様」付き＝NAME_READING_ASK）。
相手の声：make_callee_samples の Leda（correct_nakashima・name_only_nakashima・correct_kakuta・correct_kadota・name_only_kadota・correct_saidou・correct_kanno・name_only_kanno）。
⚠️ 相手役の名前の声の読み（カクタ・カドタ・サイドウ・カンノ）は、別の耳では漢字（「角田」「斎藤」「菅野」）としか書かれないので、3.8 Flash のひらがなの書き起こしで別に確かめる。
相手役の声が確かめに通っていない場面は動かさない（make_callee_samples.missing_voices の1か所＝G-57 と同じ）。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import callee_voice_for, run_call
from flowctl import GOT, Question, normalize_value, reading_question
from g22_opening import BUSINESS as BASE, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import Business
from make_callee_samples import missing_voices
from names import hira


def business(surface: str, likely: str, readings: list[str]) -> Business:
    return Business(company=BASE.company, caller=BASE.caller, purpose=BASE.purpose, purpose_line=BASE.purpose_line,
                    questions=[BASE.questions[0], reading_question("N1", surface, likely, readings),
                               Question("Q2", BASE.questions[1].text, "datetime", conditions=("答えた",), topic=BASE.questions[1].topic)])


NAKAJIMA = business("中島", "なかじま", ["なかじま", "なかしま"])
TSUNODA = business("角田", "つのだ", ["つのだ", "かくた", "かどた"])
SAITO = business("斎藤", "さいとう", ["さいとう", "さいどう"])
SUGANO = business("菅野", "すがの", ["すがの", "かんの"])
# 業務の名前の質問（名前の型。分身 AJ の試験 AJNameSamaTest の質問と同じ文）：答えは「（お名前）様」で言い直す（flowctl.spoken_value の1か所）
NAME_ASK = Business(company=BASE.company, caller=BASE.caller, purpose=BASE.purpose, purpose_line=BASE.purpose_line,
                    questions=[BASE.questions[0], Question("N1", "ご担当者様のお名前をお伺いしてもよろしいでしょうか？", "name", conditions=("答えた",),
                                                          topic="ご担当者様のお名前", required=True),
                               Question("Q2", BASE.questions[1].text, "datetime", conditions=("答えた",), topic=BASE.questions[1].topic)])
TAIL = [W("datetime_pm"), W("affirm")]
# 電話に出た方の第一声（こちらは第一声を聞いてから名乗る＝flowctl.Flow.on_connect・convo.run_call の録音で流す形は台詞の1つ目を第一声として聞く。✅ 2026-10-24 ユーザー。
# CL-1.md 4-45）。目的の人の無い業務＝お客様ご本人の第一声（G-57 の場面と同じ＝scenes.Scene.call）。⛔ 2026-11-22 分身 AN が見つけた：10-24 から、どの場面も台詞の1つ目
# 「はい、予約しています。」を第一声として聞き、そのあとの台詞が1つずつずれていた（読みの確かめへの直しが、予約の質問への答えになる）
FIRST = W("hello")


def reply(want: str, deny: str) -> dict:
    """こちらの言った読みで答えが変わる相手：合っていれば「はい、そうです。」、違えば、もう一度直す（convo の if_value）。
    ⛔ G-46 の1〜3回目は、相手役が違う読みで聞かれても「はい、そうです。」と答える台本だった（3つ目の候補まで確かめる形を試せなかった）"""
    return {"if_value": ("N1", want), "yes": W("affirm")["wav"], "no": W(deny)["wav"]}


def corrected(first: str, want: str, deny: str) -> list:
    return [FIRST, W("yes_reserved"), W(first), reply(want, deny), reply(want, deny), *TAIL]


# 場面：(業務, 相手の台詞, 相手の言った読み)
SCENES = {
    "A そのまま": (NAKAJIMA, [FIRST, W("yes_reserved"), W("affirm"), *TAIL], "なかじま"),
    "A 読みを直す": (NAKAJIMA, corrected("correct_nakashima", "なかしま", "correct_nakashima"), "なかしま"),
    "A 名前だけ言い直す": (NAKAJIMA, corrected("name_only_nakashima", "なかしま", "correct_nakashima"), "なかしま"),
    "B そのまま": (TSUNODA, [FIRST, W("yes_reserved"), W("affirm"), *TAIL], "つのだ"),
    "B かくたに直す": (TSUNODA, corrected("correct_kakuta", "かくた", "correct_kakuta"), "かくた"),
    "B かどたに直す": (TSUNODA, corrected("correct_kadota", "かどた", "correct_kadota"), "かどた"),
    "B 名前だけかどた": (TSUNODA, corrected("name_only_kadota", "かどた", "correct_kadota"), "かどた"),
    "C さいどうに直す": (SAITO, corrected("correct_saidou", "さいどう", "correct_saidou"), "さいどう"),
    "C かんのに直す": (SUGANO, corrected("correct_kanno", "かんの", "correct_kanno"), "かんの"),
    "C 名前だけかんの": (SUGANO, corrected("name_only_kanno", "かんの", "correct_kanno"), "かんの"),
    # 相手役は、こちらの言った値で答えを変えない（名前の型の値は会話AIの書き方＝かな・カタカナで変わる）＝直したあとの言い直しには「はい、そうです。」
    "D 様の言い直しのあとに直す": (NAME_ASK, [FIRST, W("yes_reserved"), W("name_only_kadota"), W("correct_kakuta"), W("affirm"), *TAIL], "かくた"),
}
SAMA_TAILS = ("様", "さま")   # 名前の終わりの敬称（こちらが言い直しで足す言葉＝phrases.NAME_HONORIFIC と、値を受け取る1か所 flowctl.normalize_value が外す「さま」）


def _plain(v) -> str:
    return (v or "").strip("「」")


def measures(call: dict, want: str) -> dict:
    it = call["items"]
    log = call["flow_log"]
    n1 = [e for e in log if e["args"].get("question_id") == "N1" or e["reply"].get("question_id") == "N1"]
    # プログラムが確かめた読み（まず言ってみる読み＝読みの確かめを始めた返事、そのあとの言い直し）
    asked = [_plain(e["reply"]["value"]) for e in log if e["reply"].get("result") == "confirm_required"
             and (e["reply"].get("question_id") == "N1" or e["args"].get("question_id") == "N1")]
    ai_corrections = [e["args"].get("correction") for e in n1 if e["op"] == "confirm_field" and e["args"].get("correction")]
    plays = [x for e in log for x in (e["reply"].get("play") or [])]
    q2_play = next((x for x in plays if x.get("kind") == "question" and x.get("id") == "Q2"), None)
    spoke_over_play = 0
    for t in call["turns"]:
        if any(e.get("play") for e in t.get("tool_events") or []) and (t.get("audio_seconds") or 0) > 0.5:
            spoke_over_play += 1
    first = asked[0] if asked else None
    corrected_scene = want != first
    # 直したあとの言い直し（直された値の返事＝confirm_field の corrected のあとにプログラムが言った文）。「様」はこちらが足す言葉で、1回だけ（(D)・分身 AJ の (6-4)）
    restated = [e["reply"].get("say") or "" for e in n1 if e["op"] == "confirm_field" and e["args"].get("result") == "corrected"
                and e["reply"].get("result") == "confirm_required"]
    value = it["N1"]["value"] or ""
    return {"checks": {"q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] in GOT,
                       "n1_final": (it["N1"]["state"], it["N1"]["value"]),
                       # 場面どおり：相手の言った読みで確かめが済んだ（相手役は、違う読みで聞かれたら直す＝if_value）。
                       # 認める場面は1回で確定していること。名前の型の値は、会話AIの書き方（かな・カタカナ）によらず読みで比べる（names.hira）
                       "scene_ok": it["N1"]["state"] == "confirmed" and hira(value) == want and (corrected_scene or len(asked) == 1),
                       "ai_sama": [c for c in ai_corrections if str(c).endswith(SAMA_TAILS)],
                       "record_sama": value.endswith(SAMA_TAILS),
                       "restated_sama": [x.count("様") for x in restated],
                       "tries": len(asked),
                       "asked": asked,
                       # 会話AIが相手の言った読みを報告したか（終わりの「様」「さま」を付けて報告しても、読みは聞けている＝ai_sama で別に数える）
                       "ai_heard_right": (want in {hira(str(c).removesuffix("様").removesuffix("さま")) for c in ai_corrections}) if corrected_scene else None,
                       "q2_ok": normalize_value("datetime", it["Q2"]["value"]) == "15日の午後2時" and it["Q2"]["state"] == "confirmed",
                       "q2_lead": q2_play.get("lead") if q2_play else None,
                       "spoke_over_play": spoke_over_play,
                       "rejected": sum(1 for e in log if e["reply"]["result"] == "rejected")},
            "ai_corrections": ai_corrections,
            "n1_reports": [(e["op"], e["args"], e["reply"]["result"]) for e in n1],
            "said": [(t.get("intended") or "").strip() for t in call["turns"]],
            "callee_said": [t.get("heard") for t in call["turns"]]}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for scene, (biz, lines, want) in SCENES.items():
            if args.only and not any(scene.startswith(o) for o in args.only):
                continue
            if missing := missing_voices(lines, callee_voice_for("S4")):   # 確かめに通っていない相手役の声では動かさない（G-57 と同じ1か所）
                rows.append({"rep": rep, "scene": scene, "want": want, "skipped": f"相手役の声が確かめに通っていない：{missing}"})
                say(f"[{rep}] {scene}：動かさない（相手役の声が確かめに通っていない {missing}）")
                continue
            try:
                call = await run_call(client, args.model, biz, "S4", lines, opening_mode="recorded", use_ear=False,
                                      save_audio="all", audio_label=f"G46_{scene.replace(' ', '_')}_{rep}",
                                      followup_wait_s=FOLLOWUP_WAIT_S, callee_voice="swap", play_recordings=True, **FAST)
                row = keep_call({"rep": rep, "scene": scene, "want": want}, call, lambda c: measures(c, want))
            except Exception as exc:
                row = {"rep": rep, "scene": scene, "want": want, "error": describe_error(exc)}
            rows.append(row)
            c = row.get("checks", {})
            say(f"[{rep}] {scene}：場面どおり={c.get('scene_ok')} 確かめた読み={c.get('asked')} 最後={c.get('n1_final')} "
                f"AIの訂正={row.get('ai_corrections')} Q2={c.get('q2_ok')} つなぎ={c.get('q2_lead')} 録音中の声={c.get('spoke_over_play')} "
                f"{row.get('error', '')}")
            save_partial("G-46", {"rows": rows})
            await asyncio.sleep(args.pause)
    ok = {s: f"{sum(1 for r in rows if r['scene'] == s and r.get('checks', {}).get('scene_ok'))}/"
             f"{sum(1 for r in rows if r['scene'] == s and 'checks' in r)}" for s in SCENES}
    say("場面どおりに確かめが済んだ（通話が最後まで流れた本数のうち）：", json.dumps(ok, ensure_ascii=False))
    return {"model": args.model, "summary": ok, "rows": rows}


def rescore(path: str) -> dict:
    """保存した結果に、いまの「次に確かめる読み」の決め方（flowctl.next_reading）を当て直す（問い合わせはしない）。
    AIの最初の訂正の報告から、いまの決め方なら何で確かめたかを出す。候補が2つ以上残り、AIの報告が使えない回は「AIに選ばせる」（数は通話で測る）。"""
    from flowctl import next_reading

    d = json.loads(open(path, encoding="utf-8").read())
    out = {}
    for r in d["rows"]:
        if "checks" not in r:
            continue
        biz = SCENES[r["scene"]][0]
        q = next(x for x in biz.questions if x.reading_of)
        first = (r["checks"]["asked"] or [q.prefill])[0]
        if r["want"] == first:
            continue
        heard = (r.get("ai_corrections") or [None])[0]
        heard = heard if heard and heard != first else None
        untried = [x for x in q.readings if x != first]
        now = next_reading(heard, q.readings, [first]) if heard or len(untried) < 2 else "（AIに選ばせる）"
        out.setdefault(r["scene"], []).append({"ai": heard, "then": r["checks"]["asked"][1:2], "now": now, "ok_now": now == r["want"]})
    for k, v in out.items():
        say(k, f"いまの決め方で正しい読み {sum(1 for x in v if x['ok_now'])}/{len(v)}", [(x["ai"], x["now"]) for x in v])
    return {"rescored_from": path, "rows": out}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--rescore", help="保存した結果に、いまの次に確かめる読みの決め方を当て直す")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--only", action="append", help="場面の頭（「A」「B」や場面の名前）で絞る")
    args = p.parse_args()
    if args.rescore:
        save_result("G-46", rescore(args.rescore))
        return
    save_result("G-46", asyncio.run(run(args)))
    drop_partial("G-46")


if __name__ == "__main__":
    main()
