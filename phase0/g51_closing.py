"""G-51：締めを、決まった言い方のまま終えられるか（flowctl.Flow._close。DESIGN.md 6-14。2026-10-17）。

きっかけ（2026-10-17 の見直し）：ふつうの締めは、名乗りと質問を録音で流す形のときしか言い方を渡しておらず、AIが話す形では「締めの言葉を
言って」とだけ伝えていた ⇒ AIが自分で文を作り、「お気軽にお問い合わせください」「お気をつけてお越しくださいませ」（命令の形）を足した（G-45）。
断られたときの締めは録音にしていなかった（「お忙しいところ」はAIの声で崩れやすい）。⇒ 締めは2つとも決まった言い方にし、録音で流す形では
録音、AIが話す形・相手の質問に答えてから締めるときは、AIがその言い方のまま言う形にした。
- 場面：(1) ふつうに終える（予約・日時・人数）(2) 「今ちょっと忙しい」(3) 最後の答えと一緒に相手が質問する（「当日はどなたが担当されるんですか？」。
  質問を1つにした業務）。(1)(2) は、AIが話す形と録音で流す形の両方。(3) は録音で流す形だけ（AIが答えてから締める）。
- 見ること：締めのターンでAIが言った言葉（AI自身の文字）が、決まった言い方と同じか（句読点・空白は見ない）。録音で流す形では、AIが黙ったか。
  (3) では、相手の質問に答えたうえで、最後が決まった言い方か。命令の形（「〜ください」）を言わなかったか。
"""
from __future__ import annotations

import asyncio
import json
import re

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import callee_voice_for, callee_wav, run_call
from g22_opening import BUSINESS, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import VOICE_TYPES, Business
from make_callee_samples import verified
from phrases import CLOSE_DECLINED, CLOSING

ONE_Q = Business(company=BUSINESS.company, caller=BUSINESS.caller, purpose=BUSINESS.purpose, purpose_line=BUSINESS.purpose_line,
                 questions=BUSINESS.questions[:1])
FULL = [W("yes_reserved"), W("datetime_pm"), W("affirm"), W("three_people"), W("affirm")]
# 場面：(業務, 相手の台詞, 名乗りと質問を録音で流すか, 締めの言い方, 相手の質問に答えるか)
SCENES = {
    "終わり_AIが話す": (BUSINESS, FULL, False, CLOSING, False),
    "終わり_録音": (BUSINESS, FULL, True, CLOSING, False),
    "忙しい_AIが話す": (BUSINESS, [W("busy")], False, CLOSE_DECLINED, False),
    "忙しい_録音": (BUSINESS, [W("busy")], True, CLOSE_DECLINED, False),
    "最後に質問_録音": (ONE_Q, [W("ask_staff")], True, CLOSING, True),
}
_PUNCT = re.compile(r"[\s、。，．,.!！?？「」]")


def plain(s: str) -> str:
    return _PUNCT.sub("", s or "")


def measures(call: dict, scene: str) -> dict:
    _, _, recorded, phrase, answers = SCENES[scene]
    end = next((e for e in call["flow_log"] if e["reply"]["result"] == "end"), None)
    turn = next((t for t in call["turns"] if any(e["reply"] == "end" for e in t.get("tool_events") or [])), None)
    said = (turn or {}).get("intended") or ""
    played = bool(end and end["reply"].get("play"))
    if recorded and not answers:
        ok = played and plain(said) == ""                      # 録音を流し、AIは黙る
    elif answers:
        ok = not played and plain(said).endswith(plain(phrase)) and len(plain(said)) > len(plain(phrase))  # 答えてから、決まった締め
    else:
        ok = not played and plain(said) == plain(phrase)       # 決まった言い方のまま。言葉を足さない
    return {"checks": {"close_ok": ok, "ended": end is not None, "played": played,
                       "imperative": any(re.search(r"ください|下さい", t.get("intended") or "") for t in call["turns"]),
                       "close_said": said, "close_ear": (turn or {}).get("ear")},
            "said": [(t.get("intended") or "")[:90] for t in call["turns"]]}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for scene, (biz, lines, recorded, _, _) in SCENES.items():
            if args.only and not any(scene.startswith(o) for o in args.only):
                continue
            for vt in args.type or VOICE_TYPES:
                head = {"rep": rep, "scene": scene, "type": vt}
                unverified = [x["wav"] for x in lines if not verified(callee_wav(x["wav"], callee_voice_for(vt)))]
                if unverified:  # 確かめに通っていない相手役の声では動かさない
                    rows.append({**head, "skipped": f"相手役の声が確かめに通っていない：{unverified}"})
                    say(f"[{rep}] {scene} {vt} 動かさない：相手役の声が確かめに通っていない {unverified}")
                    continue
                try:
                    call = await run_call(client, args.model, biz, vt, lines, opening_mode="recorded" if recorded else "ai",
                                          use_ear=True, save_audio="bad", audio_label=f"G51_{scene}_{vt}_{rep}",
                                          followup_wait_s=FOLLOWUP_WAIT_S, play_recordings=recorded, callee_voice="swap", **FAST)
                    row = keep_call(head, call, lambda c: measures(c, scene))
                except Exception as exc:
                    row = {**head, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {scene} {vt}：締め={c.get('close_ok')} 終えた={c.get('ended')} 録音={c.get('played')} 命令の形={c.get('imperative')} "
                    f"締めの言葉={c.get('close_said')!r} 別の耳={c.get('close_ear')!r} {row.get('error', '')}")
                save_partial(f"G-51{args.tag}", {"rows": rows})
                await asyncio.sleep(args.pause)
    return {"model": args.model, "summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if "checks" in r]
    s = {sc: {k: f"{sum(1 for r in done if r['scene'] == sc and r['checks'].get(k))}/{sum(1 for r in done if r['scene'] == sc)}"
              for k in ("close_ok", "ended", "imperative")} for sc in SCENES}
    s["誤り・数えられなかった"] = [f"{r['scene']} {r.get('type')}" for r in rows if "error" in r or "measure_error" in r]
    say("数え：", json.dumps(s, ensure_ascii=False))
    return s


def merge(paths: list[str]) -> dict:
    """別々のキーで同時に流した結果を1つにまとめて数え直す（問い合わせはしない）。"""
    rows = []
    for p in paths:
        for r in json.loads(open(p, encoding="utf-8").read())["rows"]:
            if "turns" in r:
                r.update(measures(r, r["scene"]))
            rows.append(r)
    return {"merged_from": paths, "summary": summarize(rows), "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--only", action="append")
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--tag", default="")
    p.add_argument("--merge", nargs="+", help="保存した結果をまとめて数え直す（問い合わせはしない）")
    args = p.parse_args()
    if args.merge:
        save_result("G-51", merge(args.merge))
        return
    save_result(f"G-51{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-51{args.tag}")


if __name__ == "__main__":
    main()
