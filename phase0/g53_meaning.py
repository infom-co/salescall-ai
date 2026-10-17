"""G-53：答えの意味の2つ目の判定（meaning_judge。DESIGN.md 6-10 の (6)）を、たくさんの言い方で測る。

✅ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
材料（どれも正解が決まっているもの）：
 (A) G-14 の記録の通話の言葉（会話のAIの聞き取りの文字と、Soniox の文字。G-15）＝正解は通話の記録の want
 (B) G-52 の答え 756個（G-48 の31問 × 選択肢ごとに6つ × 文字のモデル2つ。打ち消しを混ぜた言い方）＝正解はその選択肢
 (C) この試験で作る答え：わからない・答えたくない・どちらでも（選択肢から選ぶ質問だけ）・決められない（はい／いいえの質問だけ）を、
     文字のモデル2つ（判定に使わない 3.7 Flash・3.6 Flash）に、質問ごとに3つずつ作らせる
判定は1つの答えに1回ずつ問い合わせる（本番と同じ形）。かかった時間も残す（通話の中で、会話のAIの報告を待つ間に間に合うか）。
"""
from __future__ import annotations

import glob
import json
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from common import HERE, KeyRing, base_parser, describe_error, require_python, save_partial, save_result, say
from flowctl import ANY, Question
from g23_names import find_models
from g48_meanings import QUESTIONS as G48
from meaning_judge import UNDECIDED, judge

JUDGES = ("3.5 Flash Lite", "3.8 Flash")
MAKERS = ("3.7 Flash", "3.6 Flash")
KINDS = {"わからない": "unknown", "答えたくない": "refused", "どちらでも": ANY, "決められない": UNDECIDED}

MAKE_PROMPT = (
    "電話でお客様に、次の質問をしました。質問ごとに、お客様が次の答え方をするときの、自然な一言の答えを、それぞれ3つずつ書いてください。\n"
    "答え方：\n"
    "- わからない：知らない・覚えていない・確かめていない・まだ決めていない、と答える\n"
    "- 答えたくない：答えを断る\n"
    "- どちらでも：どれでもよいと、こちらに任せる（選択肢から選ぶ質問だけ。はい／いいえの質問には書かない）\n"
    "- 決められない：受け入れにも断りにも取れて、その言葉だけでは、どの選択肢か決められない一言（はい／いいえの質問だけ）\n"
    "決まり：選択肢の言葉そのもの（「カード」「はい」「いいえ」など）は入れません。電話で実際に言いそうな、短い言い方にします。\n"
    "質問（番号：質問の文／選択肢／質問の種類）：\n"
)


# G-52 の答えの正解の向きを、意味の言葉（G-48）にそろえる質問（打ち消しの問いの「はい」「いいえ」）：(質問, 作ったモデル。None＝どちらも)。
# ⛔ 2026-10-17 に見つけた：G-52 で答えを作ったモデルには意味の言葉を渡しておらず、「ご不明な点はございませんでしょうか？」の「はい」を
#    「分からない点がある」の向きで書いていた（意味の言葉は「はい」＝ご不明な点はないということ）。向きはモデルどうしでもそろわなかった
#    （「当日のキャンセルはございませんか？」は 3.8 Flash だけが逆）。⇒ 1つずつ読んで決めた（中身の向きで数える）
FLIP = {("G26", None), ("G27", None), ("G28", "3.8 Flash")}


def truth(it: dict) -> str:
    if it["set"] == "B" and ((it["qid"], None) in FLIP or (it["qid"], it["maker"]) in FLIP):
        return {"はい": "いいえ", "いいえ": "はい"}[it["truth"]]
    return it["truth"]


def g48_question(n: int, text: str, conditions: tuple, meanings: dict) -> Question:
    """G-48 の質問を、流れの制御の質問の形に（選択肢から選ぶ質問＝「どちら」「どれ」を聞く文。ほかは、はい／いいえの型）。"""
    kind = "choice" if ("どちら" in text or "どれ" in text) else "yes_no"
    return Question(f"G{n}", text, kind, conditions=conditions, meanings={c: w for c, w in meanings.items() if c in conditions})


def g48_questions() -> list[Question]:
    d = json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-52_*.json")))[-1], encoding="utf-8").read())
    return [g48_question(n, q, tuple(c), m) for n, ((q, c), m) in enumerate(zip(d["questions"], d["meanings"]), 1)]


def set_b(qs: list[Question]) -> list[dict]:
    d = json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-52_*.json")))[-1], encoding="utf-8").read())
    out = []
    for maker, per_q in d["answers"].items():
        for q, got in zip(qs, per_q):
            for cond, texts in got.items():
                out += [{"set": "B", "maker": maker, "qid": q.id, "text": t, "truth": cond} for t in texts]
    return out


def make_c(client, model: str, qs: list[Question]) -> list[dict]:
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "kind": types.Schema(type=types.Type.STRING, enum=list(KINDS)),
        "answers": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["no", "kind", "answers"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    body = "\n".join(f"{n}：{q.text}／{'・'.join(q.conditions)}／{'選択肢から選ぶ質問' if q.type == 'choice' else 'はい／いいえの質問'}"
                     for n, q in enumerate(qs, 1))
    r = client.models.generate_content(model=model, contents=MAKE_PROMPT + body, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.7))
    out = []
    for x in json.loads(r.text).get("items", []):
        n, kind = x.get("no"), x.get("kind")
        if not (isinstance(n, int) and 1 <= n <= len(qs)) or kind not in KINDS:
            continue
        q = qs[n - 1]
        if (kind == "どちらでも") != (q.type == "choice") and kind in ("どちらでも", "決められない"):
            continue  # 種類に合わない答え方（はい／いいえの質問の「どちらでも」・選ぶ質問の「決められない」）は使わない
        out += [{"set": "C", "maker": model, "qid": q.id, "text": t.strip(), "truth": KINDS[kind]} for t in x.get("answers", [])[:3] if t.strip()]
    return out


def set_a() -> list[dict]:
    """G-14 の記録の通話の言葉（質問ごとに重ねない）。正解は、その言葉が来た通話の want（曖昧な答えは「決められない」）。"""
    from g15_ear import live_calls

    soniox = {r["wav"]: r.get("ear") for r in json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-15-soniox_*.json")))[-1],
                                                             encoding="utf-8").read())["rows"]}
    seen, out = set(), []
    for c in live_calls():
        qid, want = c.get("question") or "Q1", c.get("want")
        if want in (None, "follow") or qid == "Q4":
            continue
        for src, text in (("会話AI", c["heard"]), ("Soniox", soniox.get(c["wav"]))):
            if text and (qid, text) not in seen:
                seen.add((qid, text))
                out.append({"set": "A", "maker": src, "qid": qid, "text": text, "truth": UNDECIDED if want == "ambiguous" else want})
    return out


def pick_sample(items: list[dict], n: int) -> list[dict]:
    """3.8 Flash の1日の上限が少ないので、3.5 Flash Lite が誤った・決められなかった答えと、ほかから無作為に n 個（記録の通話の言葉は全部）。"""
    import random

    hard = [it for it in items if it["set"] == "A" or it.get("3.5 Flash Lite") != truth(it)]
    rest = [it for it in items if it not in hard]
    return hard + random.Random(53).sample(rest, min(n, len(rest)))


def run_judges(args, items: list[dict], qmap: dict, ids: dict) -> None:
    """1つの答えに1回ずつ、判定のモデルごとに問い合わせる（使えるキーごとに1本の流れで、同時に使えるキーの数だけ）。結果は items に書き込む。
    ⛔ 2026-10-17 の1回目：キーの数（17）だけ同時に流し、止められている6本の分が、ほかの流れのキーに重なって1分の上限に当たり続けた"""
    sample = pick_sample(items, args.sample) if args.sample else []
    usable = [i for i in range(len(KeyRing(args).keys)) if i not in KeyRing(args).denied]
    n = len(usable)
    lock = threading.Lock()
    done = Counter()

    def worker(w: int, mine: list[tuple[str, dict]]):
        a = base_parser("").parse_args(["--key-index", str(usable[w])] + (["--env", args.env] if args.env else []))
        ring = KeyRing(a, min_interval_s=args.interval)
        for key, it in mine:
            q = qmap[it["qid"]]
            took = {}

            def call(client):
                t = time.monotonic()
                got = judge(client, ids[key], q, it["text"])
                took["s"] = round(time.monotonic() - t, 3)
                return got
            try:
                got, _ = ring.call(ids[key], call)
                it[key] = got
                it[f"{key}秒"] = took.get("s")
            except Exception as exc:
                it[f"{key}誤り"] = describe_error(exc).get("message", "")[:200]
            with lock:
                done[key] += 1
                if sum(done.values()) % 50 == 0:
                    say("判定", dict(done))
                    save_partial("G-53", {"items": items})

    jobs = [(key, it) for key in args.judge for it in items if key not in it and (key != "3.8 Flash" or not args.sample or it in sample)]
    lanes = [jobs[w::n] for w in range(n)]
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda w: worker(w, lanes[w]), range(n)))


def tally(items: list[dict], judges) -> dict:
    out = {}
    for key in judges:
        t = defaultdict(Counter)
        for it in items:
            if key not in it:
                continue
            got, want = it[key], truth(it)
            res = "正しい" if got == want else ("決められない（確かめる）" if got == UNDECIDED else "⛔別の答え")
            t[f"{it['set']}・{want if it['set'] != 'B' else '選択肢'}"][res] += 1
            t["全体"][res] += 1
        secs = sorted(it[f"{key}秒"] for it in items if it.get(f"{key}秒") is not None)
        out[key] = {"数え": {k: dict(v) for k, v in t.items()},
                    "かかった秒（中央・遅いほうから1割・最大）": [secs[len(secs) // 2], secs[int(len(secs) * 0.9)], secs[-1]] if secs else None,
                    "⛔別の答えの例": [f"{it['qid']}「{it['text']}」正解={truth(it)} 判定={it[key]}" for it in items
                                   if key in it and it[key] not in (truth(it), UNDECIDED)][:80]}
    return out


def predict_calls(items: list[dict], key: str) -> dict:
    """判定の結果を、G-14 の記録の通話（g15_ear.live_calls）に当てる：会話のAIの最初の報告・Soniox の文字（G-15。ear_wait_s までに届いたもの）・
    意味の判定（会話のAIの聞き取りの文字の判定＝通話の中と同じ。convo.ear_handler）を、流れの制御そのもの（flowctl.Flow）に渡して、
    依頼主へ渡るものを数える。判定が無い形（別の耳だけ）と比べる。⚠️ 判定の結果は、この試験で1回ずつ聞いたもの（通話の中の時間の遅れは入らない）。"""
    from flowctl import Flow, Limits
    from g14_listen import QUESTIONS, business
    from g15_ear import live_calls

    qs = {qid: business(qid).questions[0] for qid in QUESTIONS}
    rows = json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-15-soniox_*.json")))[-1], encoding="utf-8").read())["rows"]
    ears = {r["wav"]: (r["ear"] if r.get("first_s") is not None and r["first_s"] <= Limits.ear_wait_s else None) for r in rows}
    judged = {(it["qid"], it["text"]): it.get(key) for it in items if it["set"] == "A"}

    def outcome(q, heard, cond, ear, meaning):
        f = Flow([q])
        f.on_callee()
        f.on_heard(heard)
        if f.wants_ear("record_answer"):
            f.on_ear(ear)
            f.on_meaning(meaning)
        f.apply("record_answer", {"question_id": q.id, "condition_id": cond, "value": heard})
        return f.items[q.id].state, f.items[q.id].condition

    out = {}
    for name, use in (("別の耳だけ", False), ("別の耳＋意味の判定", True)):
        c, bad = Counter(), Counter()
        for call in live_calls():
            qid, want = call.get("question") or "Q1", call.get("want")
            q = qs.get(qid)
            if q is None or want in (None, "follow") or call.get("op") != "record_answer" or q.type not in ("yes_no", "choice"):
                continue
            ear = ears.get(call["wav"])
            meaning = judged.get((qid, call["heard"])) if use else None
            state, cond = outcome(q, call["heard"], call["condition"], ear, meaning)
            right = call["condition"] == want
            if state in ("accepted", "confirmed", "dont_know", "refused"):
                ok = (cond == want) if want != "ambiguous" else state == "dont_know"
                if state == "dont_know" and want == "unknown":
                    ok = True
                res = "受け取った・正しい" if ok else "⛔受け取った・誤り"
            else:
                res = "確かめる（AIは正しかった）" if right else "確かめる（AIが誤っていた）"
            c["通話"] += 1
            c[res] += 1
            if res.startswith("⛔"):
                bad[f"{call['wav'].rsplit('/', 1)[-1]}「{call['heard']}」AI={call['condition']} 正解={want} 判定={meaning}"] += 1
        out[name] = {**dict(c), "⛔の中身": dict(bad.most_common(20))}
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--judge", action="append", choices=JUDGES, help="判定のモデル（既定は2つとも）")
    p.add_argument("--reuse", help="前の結果のファイル（作った答え・判定済みの分を使い回す）")
    p.add_argument("--sample", type=int, default=0, help="3.8 Flash は、3.5 Flash Lite が誤った答えと、ほかから無作為にこの数だけ（1日の上限が少ない）")
    p.add_argument("--interval", type=float, default=4.0, help="同じキー・同じモデルに続けて聞く間（秒）。1分の上限に当たったら、言われた秒だけ待つ")
    args = p.parse_args()
    args.judge = args.judge or list(JUDGES)
    ring = KeyRing(args)
    ids, _ = find_models(ring.client())
    from g14_listen import business
    qs = g48_questions()
    qmap = {q.id: q for q in qs} | {qid: business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")}
    if args.reuse:
        items = json.loads(open(args.reuse, encoding="utf-8").read())["items"]
    else:
        items = set_a() + set_b(qs)
        for m in MAKERS:
            got, _ = ring.call(ids[m], lambda c: make_c(c, ids[m], qs))
            say(m, "が作った答え", len(got))
            items += got
    say("答えの数", dict(Counter(it["set"] for it in items)))
    run_judges(args, items, qmap, ids)
    summary = tally(items, args.judge)
    summary["記録の通話に当てた見込み"] = {k: predict_calls(items, k) for k in args.judge}
    for k, v in summary.items():
        say(k, json.dumps({kk: vv for kk, vv in v.items() if kk != "⛔別の答えの例"}, ensure_ascii=False))
    save_result("G-53", {"model_ids": ids, "summary": summary, "items": items})


if __name__ == "__main__":
    main()
