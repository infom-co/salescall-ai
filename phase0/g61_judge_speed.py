"""G-61：意味の判定（その場の判定＝3.5 Flash Lite）の遅れを、問い合わせ方で比べる（DESIGN.md 1-2 の待つ長さ・6-10 の (6)）。

✅ 2026-10-19 ユーザー：「同じ判定を2本同時に出して、早く返ってきたほうを使う形…遅れがどこまで縮むかを測らせていただけますでしょうか。OK」
⛔ G-57 の9回目：その場の判定 328回のうち58回が2秒超（遅いほうから1割 6.0秒・最長 22.9秒）。報告を待たせた185回中21回で間に合わなかった。
   上限の答え（429）は、ほとんど無かった＝遅い返事そのもの。同じ朝の G-53 の4回目（1本ずつ、キーごとに4秒あけて 576回）は最長 1.59秒
   ＝混んでいるときだけ遅い。⇒ 通話の試験（混み具合）と同じ時間に流して比べる。
比べる形（同じ答えを、3つの形で、入れ替えながら流す＝同じ混み具合で比べる）：
 (a) 1本・同じキー：いまの形（meaning_judge.make_judge の KeyRing は、1つの試験の判定を、空いている最初のキーに集める）。流れごとに1つのキー
 (b) 1本・キーを順に：問い合わせごとに、使えるキーを順に回す
 (c) 2本・早いほう：2つの違うキーに同時に出し、早く返ったほうを使う。遅いほうも最後まで待って記録する（読みが同じか・早いほうの正しさが
     落ちないかを見る。本番では遅いほうは使わない）
材料：G-53 の4回目の答え（G-53-firmlite2。正解は1つずつ読んで決めたもの＝g53_meaning.accept）から無作為に。
使い方：python g61_judge_speed.py --items 120 --streams 4 --pace 3 [--a-keys 3,4,5]
通話の記録で数える（問い合わせはしない）：python g61_judge_speed.py --calls results/G-57-…json …
 ＝報告を待たせた回のうち、決めるときに判定が1つも届いていなかった回・判定の秒（話し終わりから・早く届いたほう）・どちらが先か・
   止まった判定の代わり（meaning_judge.STANDIN）が入った回・答えにならなかった回・通話後の検証へ回った項目
   （✅ 2026-10-19 ユーザー：「同じ会社のモデルだけだと、通信の問題が影響するでしょ」「どれかがダウンしていたら、Jevも使ったらいいんじゃない」）
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

from common import HERE, KeyRing, base_parser, describe_error, is_quota, require_python, save_partial, save_result, say
from g53_meaning import accept, g48_questions, value_questions
from meaning_judge import ajudge

SOURCE = HERE / "results" / "G-53-firmlite2_20261019_072528.json"
MODES = ("a", "b", "c")
MODE_NAME = {"a": "1本・同じキー", "b": "1本・キーを順に", "c": "2本・早いほう"}
LATE_S = 2.0  # 報告を待たせる長さ（flowctl.Limits.hold_s）


def questions() -> dict:
    from g14_listen import business
    qs = g48_questions() + value_questions()
    return {q.id: q for q in qs} | {qid: business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")}


async def timed(client, model: str, q, it: dict) -> tuple[float, object, str | None]:
    """1回の問い合わせ：(かかった秒, 読み, 誤り)。上限の答えは誤りとして返す（測る形の混み具合として数える）。"""
    t = time.monotonic()
    try:
        got = await ajudge(client, model, q, it["text"], it.get("asked"))
        return round(time.monotonic() - t, 3), got, None
    except Exception as exc:
        return round(time.monotonic() - t, 3), None, ("上限" if is_quota(exc) else describe_error(exc).get("message", "")[:120])


async def run(args) -> dict:
    ring = KeyRing(args)
    model = ring.model_ids()["3.5 Flash Lite"]
    qmap = questions()
    items = [it for it in json.loads(SOURCE.read_text(encoding="utf-8"))["items"] if it.get("3.5 Flash Lite") is not None]
    # 使えるキー：1本ずつ1回問い合わせて確かめる（止められている・1日の上限のキーを外す。記録の状態は当てにしない＝CL-1.md 0-2）
    probe_it = items[0]
    probe = await asyncio.gather(*(timed(ring.client(i), model, qmap[probe_it["qid"]], probe_it) for i in range(len(ring.keys))))
    usable = [i for i, (_, got, err) in enumerate(probe) if err is None and got is not None]
    say("使えるキーの番号", usable, "／外したキー", {ring.keys[i].label.split("#")[0]: err for i, (_, _, err) in enumerate(probe) if i not in usable})
    a_keys = [int(x) for x in args.a_keys.split(",")] if args.a_keys else usable[:args.streams]
    assert all(k in usable for k in a_keys), f"(a) のキーに使えないものがある：{a_keys}"
    say("(a) のキー", a_keys)
    items = random.Random(args.seed).sample(items[1:], min(args.items, len(items) - 1))
    ops = []
    for n, it in enumerate(items):
        order = list(MODES)
        random.Random(args.seed + n).shuffle(order)  # 同じ答えの3つの形を、続けて・順番を変えて流す（混み具合をそろえる）
        ops += [(m, it) for m in order]
    queue: asyncio.Queue = asyncio.Queue()
    for op in ops:
        queue.put_nowait(op)
    turn = {"next": 0}

    def next_keys(k: int) -> list[int]:
        got = [usable[(turn["next"] + j) % len(usable)] for j in range(k)]
        turn["next"] += k
        return got
    rows = []
    t0 = time.monotonic()

    async def stream(s: int) -> None:
        while not queue.empty():
            mode, it = queue.get_nowait()
            q = qmap[it["qid"]]
            keys = [a_keys[s % len(a_keys)]] if mode == "a" else next_keys(1 if mode == "b" else 2)
            started = round(time.monotonic() - t0, 2)
            if mode == "c":
                tasks = [asyncio.create_task(timed(ring.client(k), model, q, it)) for k in keys]
                first, first_s = None, None
                for done in asyncio.as_completed(tasks):
                    took, got, err = await done
                    if err is None and first is None:
                        first, first_s = got, took  # 早く返った読みを使う（遅いほうは記録だけ）
                results = [t.result() for t in tasks]
                if first is None:
                    first_s = max(r[0] for r in results)
            else:
                results = [await timed(ring.client(keys[0]), model, q, it)]
                first, first_s = results[0][1], results[0][0]
            ok = accept(it)
            rows.append({"mode": mode, "stream": s, "at_s": started, "keys": keys, "set": it["set"], "qid": it["qid"], "text": it["text"],
                         "result_s": first_s, "got": first, "correct": None if first is None else first in ok,
                         "calls": [{"s": r[0], "got": r[1], "error": r[2], "correct": None if r[1] is None else r[1] in ok} for r in results]})
            if len(rows) % 30 == 0:
                say("判定", len(rows), "／", len(ops), dict(Counter(r["mode"] for r in rows)))
                save_partial(f"G-61{args.tag}", {"rows": rows})
            await asyncio.sleep(args.pace)

    await asyncio.gather(*(stream(s) for s in range(args.streams)))
    labels = {i: ring.keys[i].label.split("#")[0] for i in usable}
    return {"model": model, "usable": usable, "a_keys": a_keys, "streams": args.streams, "pace_s": args.pace,
            "summary": summarize(rows) | {"キーごと": by_key(rows, labels)}, "rows": rows}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for m in MODES:
        rs = [r for r in rows if r["mode"] == m]
        got = sorted(r["result_s"] for r in rs if r["got"] is not None)
        q = (lambda p: got[int(p * (len(got) - 1))]) if got else (lambda p: None)
        errs = Counter(c["error"] for r in rs for c in r["calls"] if c["error"])
        judged = [r for r in rs if r["correct"] is not None]
        s = {"形": MODE_NAME[m], "数": len(rs), "読めなかった": len(rs) - len(got), "誤り": dict(errs),
             "中央": q(.5), "遅いほうから1割": q(.9), "遅いほうから5分": q(.95), "最長": got[-1] if got else None,
             f"{LATE_S}秒超": sum(x > LATE_S for x in got), "正しい": f"{sum(r['correct'] for r in judged)}/{len(judged)}"}
        if m == "c":
            both = [r for r in rs if all(c["got"] is not None for c in r["calls"])]
            s["2本の読みが同じ"] = f"{sum(r['calls'][0]['got'] == r['calls'][1]['got'] for r in both)}/{len(both)}"
            slow = [max(r["calls"], key=lambda c: c["s"]) for r in both]
            s["遅いほうの正しさ"] = f"{sum(c['correct'] for c in slow)}/{len(slow)}"
            calls = sorted(c["s"] for r in rs for c in r["calls"] if c["got"] is not None)
            s["1本ずつの中央"] = calls[len(calls) // 2] if calls else None
            s[f"1本ずつの {LATE_S}秒超"] = f"{sum(x > LATE_S for x in calls)}/{len(calls)}"
        out[m] = s
    return out


def by_key(rows: list[dict], labels: dict) -> dict:
    """キーごとの1本ずつの秒（遅いのがキーについて回るか＝キーを回す・2本出す、のどちらが効くかを分ける）。"""
    per: dict[int, list[float]] = {}
    for r in rows:
        for k, c in zip(r["keys"], r["calls"]):
            if c["got"] is not None:
                per.setdefault(k, []).append(c["s"])
    out = {}
    for k, xs in sorted(per.items()):
        xs.sort()
        out[labels.get(k, str(k))] = {"数": len(xs), "中央": xs[len(xs) // 2], "最長": xs[-1], f"{LATE_S}秒超": sum(x > LATE_S for x in xs)}
    return out


def _q(xs: list[float], p: float):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else None


def calls_tally(paths: list[str]) -> dict:
    """通話の試験（scenes.py ほか）の記録から、意味の判定の届き方を数える。古い記録（その場の判定だけを待っていた形）は、決めるときに
    その場の判定が届いていなかった回（late に judge）を「間に合わなかった」と数える（新しい形は、届いていた判定＝judged が空の回）。"""
    from meaning_judge import STANDIN
    out = {}
    for path in paths:
        d = json.loads(open(path, encoding="utf-8").read())
        held = missed = 0
        first, by, decided = [], Counter(), Counter()
        each = {"judge": [], "judge2": []}
        unsure = none = 0
        unchecked = 0
        for r in d.get("rows", []):
            unchecked += sum(len(v) for v in (r.get("unchecked") or {}).values())
            for t in r.get("turns", []):
                ec = t.get("ear_check") or {}
                got = {k: ec[f"{k}_s"] for k in each if ec.get(f"{k}_s") is not None}
                for k, v in got.items():
                    each[k].append(v)
                    by[ec.get(f"{k}_by") or "（記録なし）"] += 1
                if got:
                    first.append(min(got.values()))
                for k in each:
                    tried = ec.get(f"{k}_tried") or []
                    unsure += any("unsure" in x for x in tried)
                    none += f"{k}_error" in ec
                for w in ec.get("waits", []):
                    if w["for"] != "report":
                        continue
                    held += 1
                    j = w.get("judged")
                    missed += ("judge" in w["late"]) if j is None else (not j)
                    if j is not None:  # 古い記録には無い
                        decided["+".join(j) or "なし"] += 1
        lim = LATE_S
        out[Path(path).name] = {
            "判定": d.get("judges"), "報告を待たせた": held, "決めるときに判定が届いていなかった": missed,
            "決めるときに届いていた判定": dict(decided),
            "早く届いたほうの秒（話し終わりから）": {"数": len(first), "中央": _q(first, .5), "遅いほうから1割": _q(first, .9), "最長": _q(first, 1),
                                         f"{lim}秒超": sum(x > lim for x in first)},
            **{f"{k}の秒": {"数": len(v), "中央": _q(v, .5), "遅いほうから1割": _q(v, .9), "最長": _q(v, 1), f"{lim}秒超": sum(x > lim for x in v)}
               for k, v in each.items()},
            "答えた判定": dict(by), f"{STANDIN}の確からしさが線より下": unsure, "答えが無かった（受け持ちごと）": none,
            "通話後の検証へ回った項目": unchecked}
    return out


def main() -> None:
    require_python()
    if "--calls" in sys.argv:
        paths = sys.argv[sys.argv.index("--calls") + 1:]
        data = calls_tally(paths)
        for name, s in data.items():
            say(name, json.dumps(s, ensure_ascii=False))
        save_result("G-61-calls", {"from": paths, "tally": data})
        return
    p = base_parser(__doc__)
    p.add_argument("--items", type=int, default=120)
    p.add_argument("--streams", type=int, default=4, help="同時に流す流れの数（通話の数のつもり）")
    p.add_argument("--pace", type=float, default=3.0, help="流れごとに、1回の判定のあと休む秒")
    p.add_argument("--a-keys", help="(a) の流れが使うキーの番号（例 3,4,5＝通話の試験と同じキー）")
    p.add_argument("--seed", type=int, default=61)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    data = asyncio.run(run(args))
    for m, s in data["summary"].items():
        say(m, json.dumps(s, ensure_ascii=False))
    save_result(f"G-61{args.tag}", data)


if __name__ == "__main__":
    main()
