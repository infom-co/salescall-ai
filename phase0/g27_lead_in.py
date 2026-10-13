"""G-27：プログラムの返事のあとに話す文の頭に、受けの言葉を置く形を、次の質問にも広げて、順番に回しても効くか（DESIGN.md 6-11 の案）。

G-25：言い直しの頭に「ありがとうございます。」を置くと、言い直しの頭の崩れが 42%→0%。ただし次の質問（「ご来店の人数を…」）は
言い方を変えていないので、10〜20% のまま（多くは「ご」が落ちる）。同じ受けの言葉の連発は機械っぽい（ユーザー：言い直しの言い方の連発と同じ）。
比べる形（同じ台本・同じ回数。形の順は1本ずつ回す）：
- W0_言い直しだけ：G-25 の形1（言い直しの頭に「ありがとうございます。」。次の質問には付けない）
- W1_両方に回して：言い直しと次の質問の両方に、受けの言葉を順番に回して付ける（phrases.LEAD_IN。流れの制御の lead_ins）
見ること（別の耳は2通りの聞かせ方。G-24）：
- 言い直しの値が聞こえたか（どちらか一方で聞こえれば聞こえた。両方で聞こえなかった回を数える）
- 次の質問の文が聞こえたか（質問の文の読みが、別の耳の文字に入っているか。同じ数え方）
- 受けの言葉そのものが崩れても、値・質問が守られていればよい
"""
from __future__ import annotations

import asyncio
import json

from common import (base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python,
                    save_partial, save_result, say)
from convo import AUDIO_DIR, run_call
from flowctl import normalize_value
from g25_after_tool import BUSINESS, CALLEE
from instruction import VOICE_TYPES
from jtext import compare_reading
from listen import hear_feeds, in_any
from phrases import confirm

VARIANTS = {
    "W0_言い直しだけ": {"confirm": lambda v, s="ふつう", n=0: "ありがとうございます。" + confirm(v, s, n)},
    "W1_両方に回して": {"lead_ins": True},
}
QUESTION_TEXTS = {q.id: q.text.rstrip("。") for q in BUSINESS.questions[1:]}  # Q1 は録音の名乗りの中で聞くので除く


def heard_in(text: str):
    return lambda v: compare_reading(text) in compare_reading(v)


async def check_turns(client, call: dict) -> list[dict]:
    out = []
    for t in call["turns"]:
        if not (t.get("tool_events") and t.get("audio_file") and t.get("intended")):
            continue
        pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        heard = await hear_feeds(client, pcm, rate)
        row = {"index": t["index"], "intended": t["intended"], "audio_file": t["audio_file"], "heard": heard}
        asked = [e for e in t["tool_events"] if e.get("reply") == "confirm_required"]
        if asked:
            q = next(x for x in BUSINESS.questions if x.id == asked[-1]["args"].get("question_id"))
            value = normalize_value(q.type, asked[-1]["args"].get("value") or asked[-1]["args"].get("correction"))
            if value:
                row["value"] = value
                row["value_heard_any"] = in_any(heard, heard_in(value))
        for qid, text in QUESTION_TEXTS.items():
            if compare_reading(text) in compare_reading(t["intended"]):
                row["question"] = qid
                row["question_heard_any"] = in_any(heard, heard_in(text))
        out.append(row)
    return out


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "turns_checked" in r and "check_error" not in r]
        ts = [t for r in rs for t in r["turns_checked"]]
        rb = [t for t in ts if "value" in t]
        qs = [t for t in ts if "question" in t]
        s[name] = {"calls": len(rs), "readbacks": len(rb), "value_missed_in_all": sum(1 for t in rb if not t["value_heard_any"]),
                   "questions": len(qs), "question_missed_in_all": sum(1 for t in qs if not t["question_heard_any"]),
                   "passed_calls": sum(1 for r in rs if r["checks"]["passed"])}
    return s


async def run(args) -> dict:
    if args.dry_run:
        return {"dry_run": True, "variants": list(VARIANTS), "questions": QUESTION_TEXTS}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for name, kw in VARIANTS.items():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, CALLEE, opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G27_{name}_{rep}", gap_s=args.gap, **kw)
                except Exception as exc:
                    call, row = None, {"rep": rep, "type": vt, "variant": name, "error": describe_error(exc)}
                if call is not None:
                    items = call["items"]
                    checks = {"passed": items["Q2"]["value"] == "15日の午後2時" and items["Q3"]["value"] == "3人"
                              and items["Q2"]["state"] == "confirmed" and items["Q3"]["state"] == "confirmed"}
                    row = {"rep": rep, "type": vt, "variant": name, "checks": checks, **call}
                    try:  # 聞き分けで誤りが出ても、通話の記録は残す
                        row["turns_checked"] = await check_turns(client, call)
                    except Exception as exc:
                        row["turns_checked"], row["check_error"] = [], describe_error(exc)
                rows.append(row)
                tc = row.get("turns_checked", [])
                say(f"[{rep}] {vt} {name} 項目={row.get('checks', {}).get('passed')} "
                    f"値={[t['value_heard_any'] for t in tc if 'value' in t]} 質問={[t['question_heard_any'] for t in tc if 'question' in t]} "
                    f"{row.get('error', '')}")
                save_partial("G-27", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": list(VARIANTS), "gap_s": args.gap, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--gap", type=float, default=0.0, help="相手が話し始めるまでの間（G-26 の結果で決める）")
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-27", asyncio.run(run(args)))
    drop_partial("G-27")


if __name__ == "__main__":
    main()
