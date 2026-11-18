"""G-46：名前の読みの確かめ（flowctl.reading_question）を、まだ試していない2つの形で確かめる（2026-10-16 ユーザー：「そのまま試験を進めて」）。

(A) 録音で流す形：次の質問と締めを録音で流す形（play_recordings。G-37）の中に、読みの確かめを挟む。読みの確かめは名前が入るので、
    AIが話す（録音にできない）。見ること：読みの確かめが場面どおりに済むか／確かめのあとの質問が録音で流れ、つなぎの言葉が
    「それでは」のままか（⛔ 見直しで直したずれ）／録音を流したターンでAIが話していないか。名前は「中島」（G-45 と同じ）。
(B) 読みの候補が3つある名前：「角田」＝つのだ（まず言ってみる読み）・かくた・かどた。相手が「かくた」「かどた」に直したとき、
    次に確かめる読みが、相手の言った読みになるか。
    ⚠️ 相手役は台本どおりにしか答えない（違う読みで聞き直されても「はい、そうです。」と答える）。⇒ 場面どおりかは、
    相手が認めたかではなく、**直されたあとにプログラムが確かめた読みが、相手の言った読みか**で決める。
相手の声：make_callee_samples の Leda（correct_nakashima・name_only_nakashima・correct_kakuta・correct_kadota・name_only_kadota）。
⚠️ 相手役の名前の声の読み（カクタ・カドタ）は、別の耳では「角田」としか書かれないので、3.8 Flash のひらがなの書き起こしで別に確かめる。
"""
from __future__ import annotations

import asyncio
import json

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import GOT, Question, normalize_value, reading_question
from g22_opening import BUSINESS as BASE, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import Business


def business(surface: str, likely: str, readings: list[str]) -> Business:
    return Business(company=BASE.company, caller=BASE.caller, purpose=BASE.purpose, purpose_line=BASE.purpose_line,
                    questions=[BASE.questions[0], reading_question("N1", surface, likely, readings),
                               Question("Q2", BASE.questions[1].text, "datetime", conditions=("答えた",), topic=BASE.questions[1].topic)])


NAKAJIMA = business("中島", "なかじま", ["なかじま", "なかしま"])
TSUNODA = business("角田", "つのだ", ["つのだ", "かくた", "かどた"])
TAIL = [W("datetime_pm"), W("affirm")]


def reply(want: str, deny: str) -> dict:
    """こちらの言った読みで答えが変わる相手：合っていれば「はい、そうです。」、違えば、もう一度直す（convo の if_value）。
    ⛔ G-46 の1〜3回目は、相手役が違う読みで聞かれても「はい、そうです。」と答える台本だった（3つ目の候補まで確かめる形を試せなかった）"""
    return {"if_value": ("N1", want), "yes": W("affirm")["wav"], "no": W(deny)["wav"]}


def corrected(first: str, want: str, deny: str) -> list:
    return [W("yes_reserved"), W(first), reply(want, deny), reply(want, deny), *TAIL]


# 場面：(業務, 相手の台詞, 相手の言った読み)
SCENES = {
    "A そのまま": (NAKAJIMA, [W("yes_reserved"), W("affirm"), *TAIL], "なかじま"),
    "A 読みを直す": (NAKAJIMA, corrected("correct_nakashima", "なかしま", "correct_nakashima"), "なかしま"),
    "A 名前だけ言い直す": (NAKAJIMA, corrected("name_only_nakashima", "なかしま", "correct_nakashima"), "なかしま"),
    "B そのまま": (TSUNODA, [W("yes_reserved"), W("affirm"), *TAIL], "つのだ"),
    "B かくたに直す": (TSUNODA, corrected("correct_kakuta", "かくた", "correct_kakuta"), "かくた"),
    "B かどたに直す": (TSUNODA, corrected("correct_kadota", "かどた", "correct_kadota"), "かどた"),
    "B 名前だけかどた": (TSUNODA, corrected("name_only_kadota", "かどた", "correct_kadota"), "かどた"),
}


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
    return {"checks": {"q1_ok": it["Q1"]["condition"] == "はい" and it["Q1"]["state"] in GOT,
                       "n1_final": (it["N1"]["state"], it["N1"]["value"]),
                       # 場面どおり：相手の言った読みで確かめが済んだ（相手役は、違う読みで聞かれたら直す＝if_value）。
                       # 認める場面は1回で確定していること
                       "scene_ok": it["N1"]["state"] == "confirmed" and it["N1"]["value"] == want and (corrected_scene or len(asked) == 1),
                       "tries": len(asked),
                       "asked": asked,
                       "ai_heard_right": (want in ai_corrections) if corrected_scene else None,
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
