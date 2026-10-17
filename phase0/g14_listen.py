"""G-14：その場で確かめない答え（はい／いいえ・選択肢）を、会話のAIがどれだけ正しく受け取るか（DESIGN.md 6-10・PHASE0 G-14）。

✅ 2026-10-12 ユーザー：「確かめない種類の項目（はい／いいえ等）は、聞き取れていれば渡します。チャットGPTでの実験では、聞き取り精度が
   悪かったので、聞き取り精度の良い仕組みを入れてください」
- 材料：make_g14_set の試験集（6つの声・電話の音質・はっきりした答えには雑音ありも）。
- 形：1本の通話＝名乗りと質問を録音で流した形（いまの形）→ 相手の答え（試験集の1本）→ AIの最初の報告を見る。
- 正しい受け取り：
  はい／いいえ／カード／振込 ⇒ その条件で record_answer
  unknown（「ちょっとわからないです」など）⇒ 「わからない」で record_answer
  ambiguous（「大丈夫です」「結構です」など。受け入れにも断りにも取れる）⇒ 聞き返す（report_unclear）か、プログラムが曖昧として聞き返す
    （⏳ 6-10 の (3)。いまの形にはまだ無い）
  ⛔ 一番悪いのは「違う条件を取る」（はいを いいえ に、など）＝そのまま結果に入る。次に悪いのは「曖昧を、はい／いいえに決めてしまう」
- 数え方：正しい／⛔違う条件／曖昧を決めてしまった／聞き返した（はっきりした答えなのに）／報告が無い。種類・声・雑音ごとにも。
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

from common import HERE, base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import run_call
from flowctl import Question
from g22_opening import BUSINESS as BASE
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import Business

QUESTIONS = {
    "Q1": Question("Q1", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ")),
    "Q2": Question("Q2", "お支払いはカードと振込のどちらがよろしいでしょうか？", "choice", conditions=("カード", "振込")),
}


def business(qid: str) -> Business:
    return Business(company=BASE.company, caller=BASE.caller, purpose="ご予約の確認", purpose_line=BASE.purpose_line,
                    questions=[QUESTIONS[qid]])


def first_report(call: dict, qid: str) -> tuple[str, str | None]:
    """AIの最初の報告（操作と条件）。報告が無ければ ("なし", None)。"""
    for e in call["flow_log"]:
        a = e["args"]
        if a.get("question_id") != qid:
            continue
        if e["op"] == "record_answer":
            return "record_answer", a.get("condition_id")
        if e["op"] == "report_unclear":
            return "report_unclear", None
    return "なし", None


def verdict(want: str, op: str, cond: str | None) -> str:
    if op == "なし":
        return "報告が無い"
    if want == "ambiguous":
        if op == "report_unclear" or cond == "unknown":
            return "正しい"
        return "曖昧を決めてしまった"
    if op == "report_unclear":
        return "聞き返した"
    return "正しい" if cond == want else "⛔違う条件"


def measures(call: dict, item: dict) -> dict:
    op, cond = first_report(call, item["question"])
    return {"op": op, "condition": cond, "verdict": verdict(item["want"], op, cond),
            "callee_heard": [t.get("heard") for t in call["turns"]][:1]}


def tally(rows: list[dict]) -> dict:
    out = {"全体": Counter(r["verdict"] for r in rows if "verdict" in r)}
    for key in ("kind", "voice", "noise_tag", "want"):
        for r in rows:
            if "verdict" in r:
                out.setdefault(f"{key}={r.get(key)}", Counter())[r["verdict"]] += 1
    return {k: dict(v) for k, v in out.items()}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    items = [m for m in json.loads((HERE / "results" / "callee" / "g14" / "index.json").read_text(encoding="utf-8")) if m.get("ok")]
    if args.only:
        items = [m for m in items if any(m["id"].startswith(o) for o in args.only)]
    if args.voice:
        items = [m for m in items if m["voice"] in args.voice]
    rows = []
    for n, item in enumerate(items[: args.limit or None]):
        head = {**{k: item[k] for k in ("id", "voice", "question", "want", "kind", "text")}, "noise_tag": "雑音" if item.get("noise") else "無し"}
        try:
            call = await run_call(client, args.model, business(item["question"]), "S4", [{"wav": item["wav"]}], opening_mode="recorded",
                                  use_ear=False, save_audio="bad", audio_label=f"G14_{item['voice']}_{item['id']}",
                                  followup_wait_s=FOLLOWUP_WAIT_S, play_recordings=True, **FAST)
            row = keep_call(head, call, lambda c: measures(c, item))
        except Exception as exc:
            row = {**head, "error": describe_error(exc)}
        rows.append(row)
        say(f"[{n + 1}/{len(items)}] {item['voice']} {item['id']}「{item['text']}」{head['noise_tag']}：{row.get('verdict')} "
            f"（{row.get('op')} {row.get('condition')}） {row.get('error', '')}")
        save_partial(f"G-14{args.tag}", {"rows": rows})
        await asyncio.sleep(args.pause)
    summary = tally(rows)
    say("数え：", json.dumps(summary["全体"], ensure_ascii=False))
    return {"model": args.model, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", action="append", help="試験集の中の名前の頭（「y」「a1」など）で絞る")
    p.add_argument("--voice", action="append", help="試験集の声で絞る（声ごとに別のキーで同時に流すため）")
    p.add_argument("--tag", default="", help="結果のファイル名に足す印（同時に流す回を分けるため。例 -base-Kore）")
    p.add_argument("--tally", help="結果のファイル名の頭（例 G-14-base-）で、同時に流した回を合わせて数える（問い合わせはしない）")
    args = p.parse_args()
    if args.tally:
        rows = [r for f in sorted((HERE / "results").glob(f"{args.tally}*.json")) if "_partial" not in f.name
                for r in json.loads(f.read_text(encoding="utf-8"))["rows"]]
        summary = tally(rows)
        for k, v in summary.items():
            say(k, v)
        save_result(f"{args.tally.rstrip('-')}-TALLY", {"summary": summary, "rows": rows})
        return
    save_result(f"G-14{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-14{args.tag}")


if __name__ == "__main__":
    main()
