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
    # 前の質問から「結構です」の意味が決まる質問（G-49：2つのモデルとも「結構」＝「いいえ」）。--as-question Q3 で、Q1 の答えの声を流す
    "Q3": Question("Q3", "パンフレットをお送りしてもよろしいでしょうか？", "yes_no", conditions=("はい", "いいえ")),
    # 値を聞く質問（「結構です」だけの答えは、答えの値ではない＝聞き返す。その言葉だけで締めない）。--as-question Q4
    "Q4": Question("Q4", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", conditions=("答えた",), topic="ご希望の日時"),
    # Q1 と同じ問いで、選択肢を言葉で書いた形（依頼主の台本は「決まっている・決まっていない」のように書くことが多い。Intent.md 6-3）
    "Q5": Question("Q5", "ご予約はされていますでしょうか？", "yes_no", conditions=("している", "していない")),
}
# --as-question のときの正解の差し替え（その質問への答えとしての正解）：声ごと（id）か、もとの正解ごと（want）。書いていないものは、もとのまま
WANT_AS = {"Q3": {"id": {"a2": "いいえ"}}, "Q5": {"want": {"はい": "している", "いいえ": "していない"}}}


def _decided(name: str, decide) -> dict:
    """取込で決めるもの（意味の言葉＝G-48・曖昧な言葉の意味＝G-49）を、保存した2つのモデルの答えから、取込と同じ決め方で決める（手で書かない）。
    結果が無ければ空（意味の言葉が無ければ選択肢の名前で言い直す・曖昧な言葉は確かめる）。"""
    files = sorted((HERE / "results").glob(f"{name}_*.json"))
    if not files:
        return {}
    d = json.loads(files[-1].read_text(encoding="utf-8"))
    # 質問の文と選択肢の組で引く（同じ文で選択肢の違う質問がある＝Q1 と Q5）
    rows = {k: {(q, tuple(c)): w for (q, c), w in zip(d["questions"], v)} for k, v in d["rows"].items()}
    return {qid: decide(q, [rows[k].get((q.text, tuple(q.conditions)), {}) for k in ("3.8 Flash", "3.7 Flash") if k in rows])
            for qid, q in QUESTIONS.items()}


def meanings() -> dict:
    from meanings import decide
    return _decided("G-48", lambda q, got: decide(q.text, q.conditions, got))


def replies() -> dict:
    from meanings import decide_replies
    return _decided("G-49", lambda q, got: decide_replies(q.conditions, got))


def business(qid: str, with_meanings: bool = True) -> Business:
    q = QUESTIONS[qid]
    if with_meanings:
        q = Question(q.id, q.text, q.type, conditions=q.conditions, topic=q.topic, meanings=MEANINGS.get(qid, {}),
                     reply_meanings=REPLIES.get(qid, {}))
    return Business(company=BASE.company, caller=BASE.caller, purpose="ご予約の確認", purpose_line=BASE.purpose_line, questions=[q])


def first_report(call: dict, qid: str) -> tuple[str, str | None]:
    """AIの最初の報告（操作と条件）。報告が無ければ ("なし", None)。"""
    for e in call["flow_log"]:
        a = e["args"]
        if e["op"] == "request_end":
            return "request_end", a.get("reason")
        if a.get("question_id") != qid:
            continue
        if e["op"] == "record_answer":
            return "record_answer", a.get("condition_id")
        if e["op"] == "report_unclear":
            return "report_unclear", None
    return "なし", None


# 流れの制御が最後にどう受け取ったか（相手は1言だけ答えるので、そのあとの確かめ・聞き返しは、返事を待ったまま終わる）
TAKEN = {"accepted": "受け付けた", "confirmed": "受け付けた", "dont_know": "わからない", "checking": "意味を確かめた", "unclear": "聞き返した",
         "unresolved": "「-」", "refused": "答えたくない", "empty": "まだ"}


def verdict(want: str, state: str, cond: str | None, op: str, reason: str | None, ended: bool = True) -> str:
    """数え方は、AIの報告ではなく、流れの制御が最後にどう受け取ったか（依頼主へ渡るもの）で決める。
    ⛔ 1回目（いまの形）はAIの最初の報告で数えた。プログラムが曖昧な答えを確かめる形（6-10 の (3)）を足したので、同じ数え方で数え直す（--tally）"""
    taken = TAKEN.get(state, state)
    if taken == "まだ":
        if op == "request_end" and reason == "callee_refused_call":  # 締めたか（流れの制御が「結構です」だけの断りを差し戻したら、締めていない）
            return "断りとして締めた" if ended else "報告が無い（断りを差し戻したあと）"
        return "報告が無い"
    if want == "ambiguous":  # 決めずに確かめる・聞き返す・「わからない」「-」にする、のどれか（答えたくない、は相手が言っていない）
        return "曖昧を決めてしまった" if taken in ("受け付けた", "答えたくない") else "正しい"
    if want == "unknown":
        return "正しい" if taken == "わからない" else ("⛔違う条件" if taken in ("受け付けた", "答えたくない") else f"{taken}（わからないの答えに）")
    if taken == "答えたくない":
        return "⛔違う条件"
    if taken == "受け付けた":
        return "正しい" if cond == want else "⛔違う条件"
    return f"{taken}（はっきりした答えに）"


def measures(call: dict, item: dict) -> dict:
    op, cond = first_report(call, item["question"])
    it = call["items"][item["question"]]
    said = [e["reply"].get("say") for e in call["flow_log"] if e["reply"].get("say")]
    return {"op": op, "condition": cond, "state": it["state"], "taken_condition": it["condition"],
            "verdict": verdict(item["want"], it["state"], it["condition"], op, cond, ended=bool(call.get("hurried"))), "program_said": said[:1],
            "callee_heard": [t.get("heard") for t in call["turns"]][:1]}


def tally(rows: list[dict]) -> dict:
    out = {"全体": Counter(r["verdict"] for r in rows if "verdict" in r)}
    for key in ("kind", "voice", "noise_tag", "want"):
        for r in rows:
            if "verdict" in r:
                out.setdefault(f"{key}={r.get(key)}", Counter())[r["verdict"]] += 1
    return {k: dict(v) for k, v in out.items()}


MEANINGS = meanings()
REPLIES = replies()


# 確かめの返事まで流す場面（--followup）：(最初の答え, 相手が思っていた答え)。相手は、こちらの言い直した選択肢が思っていた答えなら
# 「はい、そうです。」、違えば打ち消して言い直す（convo の if_value）。⇒ 見ること：確かめのあと、思っていた答えで受け付けられたか
FOLLOWUP = {"大丈夫です→はい": ("a1", "はい"), "大丈夫です→いいえ": ("a1", "いいえ"), "いいです→はい": ("a4", "はい"),
            "結構です→いいえ": ("a2", "いいえ"), "かもしれない→わからない": ("u2", "unknown")}


def followup_lines(clip: dict, voice: str, truth: str) -> list:
    if truth == "unknown":
        return [{"wav": clip[(voice, "u2")]["wav"]}, {"wav": clip[(voice, "u1")]["wav"]}]
    deny = clip[(voice, "f2" if truth == "はい" else "n1")]["wav"]  # 「いいえ、予約はしています。」／「いいえ、まだです。」
    reply = {"if_value": ("Q1", truth), "yes": clip[(voice, "f1")]["wav"], "no": deny}
    return [reply, reply]


def followup_verdict(call: dict, truth: str) -> str:
    it = call["items"]["Q1"]
    if truth == "unknown":
        return "正しい" if it["state"] == "dont_know" else f"{TAKEN.get(it['state'], it['state'])}・{it['condition']}"
    if it["state"] in GOT_STATES:
        return "正しい" if it["condition"] == truth else "⛔違う条件"
    return TAKEN.get(it["state"], it["state"]) + (f"・{it['condition']}" if it["condition"] else "")


GOT_STATES = ("confirmed", "accepted")


async def run_followup(args) -> dict:
    client = make_client(pick_key(args))
    clip = {(m["voice"], m["id"]): m for m in json.loads((HERE / "results" / "callee" / "g14" / "index.json").read_text(encoding="utf-8"))
            if m.get("ok") and not m.get("noise")}
    voices = sorted({v for v, _ in clip} & set(args.voice or {v for v, _ in clip}))
    rows = []
    for scene, (first, truth) in FOLLOWUP.items():
        for voice in voices:
            if (voice, first) not in clip:
                continue
            lines = [{"wav": clip[(voice, first)]["wav"]}] + (followup_lines(clip, voice, truth)[1:] if truth == "unknown"
                                                              else followup_lines(clip, voice, truth))
            head = {"scene": scene, "voice": voice, "truth": truth}
            try:
                call = await run_call(client, args.model, business("Q1"), "S4", lines, opening_mode="recorded", use_ear=False,
                                      save_audio="bad", audio_label=f"G14f_{voice}_{first}", followup_wait_s=FOLLOWUP_WAIT_S,
                                      play_recordings=True, **FAST)
                row = keep_call(head, call, lambda c: {
                    "verdict": followup_verdict(c, truth), "ops": [(e["op"], e["args"], e["reply"]["result"]) for e in c["flow_log"]],
                    "program_said": [e["reply"].get("say") for e in c["flow_log"] if e["reply"].get("say")],
                    "callee_heard": [t.get("heard") for t in c["turns"]]})
            except Exception as exc:
                row = {**head, "error": describe_error(exc)}
            rows.append(row)
            say(f"{scene} {voice}：{row.get('verdict')} 言い直し={row.get('program_said')} 操作={[(o, a.get('condition_id') or a.get('result') or a.get('reason'), a.get('correction')) for o, a, _ in row.get('ops', [])]} {row.get('error', '')}")
            save_partial(f"G-14F{args.tag}", {"rows": rows})
            await asyncio.sleep(args.pause)
    out = {s: dict(Counter(r.get("verdict") for r in rows if r["scene"] == s)) for s in FOLLOWUP}
    say("数え：", json.dumps(out, ensure_ascii=False))
    return {"model": args.model, "summary": out, "rows": rows}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    items = [m for m in json.loads((HERE / "results" / "callee" / "g14" / "index.json").read_text(encoding="utf-8"))
             if m.get("ok") and m["want"] != "follow"]
    if args.only:
        items = [m for m in items if any(m["id"].startswith(o) for o in args.only)]
    if args.voice:
        items = [m for m in items if m["voice"] in args.voice]
    if args.as_question:  # 同じ声を、別の質問への答えとして流す（正解は WANT_AS で差し替える）
        w = WANT_AS.get(args.as_question, {})
        items = [{**m, "question": args.as_question,
                  "want": w.get("id", {}).get(m["id"], w.get("want", {}).get(m["want"], m["want"]))} for m in items]
    rows = []
    for n, item in enumerate(items[: args.limit or None]):
        head = {**{k: item[k] for k in ("id", "voice", "question", "want", "kind", "text")}, "noise_tag": "雑音" if item.get("noise") else "無し"}
        try:
            call = await run_call(client, args.model, business(item["question"], not args.no_meanings), "S4", [{"wav": item["wav"]}], opening_mode="recorded",
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
    p.add_argument("--as-question", choices=list(QUESTIONS), help="試験集の声を、この質問への答えとして流す（例：Q3）")
    p.add_argument("--followup", action="store_true", help="確かめの返事まで流す場面（FOLLOWUP）を流す")
    p.add_argument("--no-meanings", action="store_true", help="意味の言葉を渡さない（選択肢の名前で言い直す形と比べるため）")
    p.add_argument("--tally", help="結果のファイル名の頭（例 G-14-base-）で、同時に流した回を合わせて数える（問い合わせはしない）")
    args = p.parse_args()
    if args.tally:
        rows = [r for f in sorted((HERE / "results").glob(f"{args.tally}*.json")) if "_partial" not in f.name and "TALLY" not in f.name
                for r in json.loads(f.read_text(encoding="utf-8"))["rows"]]
        for r in rows:  # いまの数え方で数え直す（保存した通話の記録から）
            if "items" in r:
                r.update(measures(r, r))
        summary = tally(rows)
        for k, v in summary.items():
            say(k, v)
        save_result(f"{args.tally.rstrip('-')}-TALLY", {"summary": summary, "rows": rows})
        return
    if args.followup:
        save_result(f"G-14F{args.tag}", asyncio.run(run_followup(args)))
        drop_partial(f"G-14F{args.tag}")
        return
    save_result(f"G-14{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-14{args.tag}")


if __name__ == "__main__":
    main()
