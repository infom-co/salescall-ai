"""G-25：AIが操作（答えの報告）の返事を受けてから話すと、声の頭の1〜3音が崩れる（約2割。G-16 の4回目・G-24）。原因を切り分け、直し方を測る。

崩れるのは言い直しの値（「15日」⇒「5日」「日付」、「3人で」⇒「SUNNY の」）。相手には別の値に聞こえうるので、確かめの意味がなくなる。
比べる形（同じ台本・同じ回数。形の順は1本ずつ回す）：
- V0_いまの形：「（値）で、よろしいでしょうか。」（phrases.confirm）
- V1_頭にお礼：「ありがとうございます。（値）で、よろしいでしょうか。」＝崩れが頭の1〜3音だけなら、値は守られるはず
- V2_考える量0：V0 の言い方のまま、考える量を0にする（考える処理が、話し始めの崩れに関わっているか）
見ること（AIの声は全部残す。別の耳は2通りの聞かせ方＝声の前の無音なし・あり。G-24 で、1回だけだと約5%で頭を聞き違えると分かった）：
- 値が聞こえたか：言い直しのターンで、値の読みが、2通りの両方の文字に入っているか（両方に入っていれば「聞こえた」）
- 頭の崩れ：操作の返事のあとのターンで、2通りの両方で、読みの最初の4音が言うつもりの言葉と食い違ったか
"""
from __future__ import annotations

import asyncio
import json

from common import (base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python,
                    save_partial, save_result, say)
from convo import AUDIO_DIR, run_call
from flowctl import Question, normalize_value
from instruction import VOICE_TYPES, Business
from jtext import compare_reading
from listen import differences, hear_feeds, in_all, in_any
from phrases import confirm

BUSINESS = Business(
    company="アオバ物流", caller="佐藤", purpose="ご予約の確認", purpose_line="ご予約の確認でお電話いたしました。",
    questions=[Question("Q1", "ご予約はされていますでしょうか。", "yes_no", conditions=("yes", "no")),
               Question("Q2", "ご希望の日時をお聞かせください。", "datetime", conditions=("answered",)),
               Question("Q3", "ご来店の人数をお聞かせください。", "number", conditions=("answered",))],
)
# 名乗りと Q1 は録音で流した形（AIは相手の答えから続ける）。AIの発話は、ほとんどが操作の返事のあと
CALLEE = ["はい、予約しています。", "15日の午後2時です。", "はい、そうです。", "3人です。", "はい、そうです。"]
VARIANTS = {
    "V0_いまの形": {},
    "V1_頭にお礼": {"confirm": lambda v: "ありがとうございます。" + confirm(v)},
    "V2_考える量0": {"thinking_budget": 0},
}
HEAD = 4


def head_bad(intended: str, heard: str) -> bool:
    return any(d["at"] < HEAD for d in differences(intended, heard))


async def check_turns(client, call: dict) -> list[dict]:
    """操作の返事のあとのターンを、2通りで聞く。言い直しのターンは、値が聞こえたかも見る。"""
    out = []
    for t in call["turns"]:
        if not (t.get("tool_events") and t.get("audio_file") and t.get("intended")):
            continue
        pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        heard = await hear_feeds(client, pcm, rate)
        row = {"index": t["index"], "intended": t["intended"], "audio_file": t["audio_file"], "heard": heard,
               "head_bad_both": in_all(heard, lambda v: head_bad(t["intended"], v))}
        asked = [e for e in t["tool_events"] if e.get("reply") == "confirm_required"]
        if asked:
            q = BUSINESS.questions[[x.id for x in BUSINESS.questions].index(asked[-1]["args"].get("question_id"))]
            value = normalize_value(q.type, asked[-1]["args"].get("value") or asked[-1]["args"].get("correction"))
            if value is None:  # 値の無い報告（流れの制御が差し戻す形に直した。直す前の記録の読み直しのため）
                row["value_missing"] = True
                out.append(row)
                continue
            row["value"] = value
            # 値が聞こえなかったと数えるのは、2通りの両方で聞こえなかったときだけ（value_heard_any）。両方で聞こえたか（value_heard_both）も残す
            row["value_heard_both"] = in_all(heard, lambda v: compare_reading(value) in compare_reading(v))
            row["value_heard_any"] = in_any(heard, lambda v: compare_reading(value) in compare_reading(v))
            row["value_in_intended"] = compare_reading(value) in compare_reading(t["intended"])
        out.append(row)
    return out


def summarize(rows: list[dict]) -> dict:
    s = {}
    for name in VARIANTS:
        rs = [r for r in rows if r.get("variant") == name and "turns_checked" in r and "check_error" not in r]
        turns = [t for r in rs for t in r["turns_checked"]]
        rb = [t for t in turns if "value" in t]
        s[name] = {"calls": len(rs), "after_tool_turns": len(turns), "head_bad_both": sum(t["head_bad_both"] for t in turns),
                   "readbacks": len(rb), "value_heard_both": sum(t["value_heard_both"] for t in rb),
                   "value_missed_in_all": sum(1 for t in rb if not t.get("value_heard_any", t["value_heard_both"])),
                   "value_said": sum(t["value_in_intended"] for t in rb),
                   "passed_calls": sum(1 for r in rs if r["checks"]["passed"])}
    return s


async def run(args) -> dict:
    if args.dry_run:
        from flowctl import Flow
        f = Flow(BUSINESS.questions, confirm=VARIANTS["V1_頭にお礼"]["confirm"])
        f.handle(type("F", (), {"name": "record_answer", "args": {"question_id": "Q1", "condition_id": "yes"}})())
        r = f.handle(type("F", (), {"name": "record_answer", "args": {"question_id": "Q2", "condition_id": "answered", "value": "15日の午後2時です。"}})())
        return {"dry_run": True, "v1_message": r["message"]}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for name, kw in VARIANTS.items():
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, CALLEE, opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G25_{name}_{rep}", **kw)
                except Exception as exc:
                    call = None
                    row = {"rep": rep, "type": vt, "variant": name, "error": describe_error(exc)}
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
                say(f"[{rep}] {vt} {name} 項目={row.get('checks', {}).get('passed')} 値の無い報告={sum(1 for t in tc if t.get('value_missing'))} "
                    f"値が聞こえた={[t['value_heard_both'] for t in tc if 'value' in t]} 頭の崩れ={sum(t['head_bad_both'] for t in tc)}/{len(tc)} "
                    f"{row.get('error', '')}")
                await asyncio.sleep(args.pause)
                save_partial("G-25", {"model": args.model, "summary": summarize(rows), "rows": rows})  # 1本ごとに途中の結果を残す
        say("途中のまとめ：", json.dumps(summarize(rows), ensure_ascii=False))
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variants": list(VARIANTS), "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=4)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-25", asyncio.run(run(args)))
    drop_partial("G-25")


if __name__ == "__main__":
    main()
