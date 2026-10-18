"""G-55：残る誤り（DESIGN.md 1章の 1-3）＝会話のAIと意味の判定が、そろって同じ向きに意味を取り違えて、確かめずに受け取る回を数える（2026-10-18）。

✅ 2026-10-18 ユーザー：「残る誤りは、2つの仕組みがそろって同じ向きに誤ったときだけ ⇒ この対応も必要だよね。」
- 材料：G-53 の答え（正解が決まっているもの）。(B) 打ち消しを混ぜた答え（G-52）と、(C) わからない・どちらでも。
- 形：本物の会話のAI（gemini-3.8-live）に、相手の言葉を文字で送る（聞き違いは入らない＝意味の取り違えだけを測る）。意味の判定（3.5 Flash Lite）も
  通話の中と同じく走らせる（convo.check_handler）。流れの制御が受け取ったものを、正解と比べる。
- 数えること：(1) ⛔受け取った・誤り（AIと判定がそろって誤った＝残る誤り） (2) 確かめた（AIか判定が誤った・どちらかが決められなかった）
  (3) 2つ目の判定の候補（G-53 で 3.6〜3.8 Flash に聞いた答え）が、(1) を拾えたか
- 1つの答えに1本の通話（質問1つ）。相手は答えを1回言うだけ（確かめの返事はしない＝確かめに回った答えは、相手が直せる側）。
"""
from __future__ import annotations

import asyncio
import glob
import json
import random
from collections import Counter

from common import HERE, base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import ANY, TAKEN
from g22_opening import BUSINESS as BASE
from g34_ack import FAST
from g53_meaning import JUDGES, g48_questions, truth
from instruction import Business
from meaning_judge import JUDGE2, make_judge


def business(q) -> Business:
    return Business(company=BASE.company, caller=BASE.caller, purpose="ご利用状況の確認", purpose_line="ご利用状況の確認でお電話いたしました。",
                    questions=[q])


def outcome(call: dict, qid: str, want: str) -> str:
    it = call["items"][qid]
    if it["state"] in TAKEN:
        got = "unknown" if it["state"] == "dont_know" else "refused" if it["state"] == "refused" else it["condition"]
        return "受け取った・正しい" if got == want else "⛔受け取った・誤り"
    return {"checking": "確かめた", "unclear": "聞き返した", "unresolved": "「-」", "empty": "受け取っていない"}.get(it["state"], it["state"])


async def lane(k: int, items: list[dict], qmap: dict, args, rows: list[dict]) -> None:
    a = base_parser("").parse_args(["--key-index", str(k)] + (["--env", args.env] if args.env else []))
    client = make_client(pick_key(a))
    judge = make_judge(a, "3.5 Flash Lite")
    judge2 = make_judge(a, JUDGE2) if args.judge2 else None  # 2つ目の判定（✅ 2026-10-18 ユーザー：当面は Sonnet だけ）
    for it in items:
        q = qmap[it["qid"]]
        want = truth(it)
        head = {"set": it["set"], "qid": it["qid"], "text": it["text"], "truth": want, "key": k,
                **{j: it.get(j) for j in JUDGES if j in it}}
        try:
            call = await run_call(client, args.model, business(q), "S4", [it["text"]], opening_mode="recorded", use_ear=False,
                                  save_audio="none", play_recordings=True, judge=judge, judge2=judge2, **FAST)
            ops = [(e["op"], e["args"].get("condition_id") or e["args"].get("result") or e["args"].get("reason"), e["reply"]["result"])
                   for e in call["flow_log"]]
            judged = next((t.get("ear_check", {}).get("judge") for t in call["turns"] if t.get("ear_check")), None)
            row = {**head, "outcome": outcome(call, q.id, want), "ops": ops, "judge_in_call": judged,
                   "history": call["items"][q.id]["history"], "unchecked": call.get("unchecked")}
        except Exception as exc:
            row = {**head, "error": describe_error(exc)}
        rows.append(row)
        if row.get("outcome", "").startswith("⛔") or "error" in row:
            say(f"{row['qid']}「{row['text']}」正解={want} {row.get('outcome')} 操作={row.get('ops')} 判定={row.get('judge_in_call')} "
                f"{row.get('error', '')}")
        if len(rows) % 25 == 0:
            say("数え", dict(Counter(r.get("outcome", "誤り") for r in rows)), f"{len(rows)}本")
            save_partial(f"G-55{args.tag}", {"rows": rows})
        await asyncio.sleep(args.pause)


def summarize(rows: list[dict]) -> dict:
    out = {"全体": dict(Counter(r.get("outcome", "通話が落ちた") for r in rows))}
    for s in ("B", "C"):
        out[s] = dict(Counter(r.get("outcome", "通話が落ちた") for r in rows if r["set"] == s))
    bad = [r for r in rows if r.get("outcome", "").startswith("⛔")]
    # 2つ目の判定の候補が、残った誤りを拾えたか（G-53 でその答えに聞いた判定。正解でなければ、通話の判定と食い違って確かめに回る見込み）
    out["残った誤りを2つ目の判定の候補が拾えたか"] = {
        j: f"{sum(1 for r in bad if r.get(j) is not None and r.get(j) != r['truth'])}/{sum(1 for r in bad if r.get(j) is not None)}"
        for j in JUDGES if j != "3.5 Flash Lite"}
    out["⛔の中身"] = [f"{r['qid']}「{r['text']}」正解={r['truth']} 操作={r.get('ops')} 判定={r.get('judge_in_call')}" for r in bad]
    return out


async def run(args) -> dict:
    src = json.loads(open(args.items or sorted(glob.glob(str(HERE / "results" / "G-53_2*.json")))[-1], encoding="utf-8").read())["items"]
    qmap = {q.id: q for q in g48_questions()}
    pool = [it for it in src if it["set"] in ("B", "C") and it["qid"] in qmap]
    rnd = random.Random(55)
    b = [it for it in pool if it["set"] == "B"]
    if args.b_sample:  # 3.5 Flash Lite の1日の上限（1キー500回）のため、通話の試験と分け合うときに絞る
        b = rnd.sample(b, min(args.b_sample, len(b)))
    # (C) は「わからない」「どちらでも」だけ（「答えたくない」「決められない」として作らせた答えは、正解の付け方が合わないものが多い＝G-53）
    cs = [it for it in pool if it["set"] == "C" and truth(it) in ("unknown", ANY)]
    c = rnd.sample(cs, min(args.c_sample, len(cs)))
    items = b + c
    rnd.shuffle(items)
    if args.limit:
        items = items[:args.limit]
    keys = [int(k) for k in args.keys.split(",")]
    say("答えの数", len(items), "キー", keys)
    rows: list[dict] = []
    await asyncio.gather(*(lane(k, items[i::len(keys)], qmap, args, rows) for i, k in enumerate(keys)))
    summary = summarize(rows)
    say("まとめ：", json.dumps({k: v for k, v in summary.items() if k != "⛔の中身"}, ensure_ascii=False))
    return {"model": args.model, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--items", help="G-53 の結果のファイル（既定は最新）")
    p.add_argument("--keys", required=True, help="使うキーの番号（カンマ区切り。キーごとに1本の流れで同時に流す）")
    p.add_argument("--c-sample", type=int, default=300, help="(C) から無作為に選ぶ数")
    p.add_argument("--b-sample", type=int, default=0, help="(B) から無作為に選ぶ数（0＝全部）")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--tag", default="")
    p.add_argument("--no-judge2", dest="judge2", action="store_false", help="2つ目の判定（meaning_judge.JUDGE2）を使わない（いまの判定だけで残る誤りを数える）")
    args = p.parse_args()
    save_result(f"G-55{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-55{args.tag}")


if __name__ == "__main__":
    main()
