"""Jev（判断だけの専用モデル）を、公式の型どおりの使い方で測る（G-53。2026-10-18）。
✅ 2026-10-18 ユーザー：「Jevの使い方は理解できていますか？」⇒ 1回目は、チャットのAI向けの長い決まりの文をそのまま問いに渡し、選択肢の定義を空にし、
   答えの確からしさを捨てて数えていた（Jev を公平に測っていなかった）。公式の型（OpenRouter の Jev の手引き・使い方の例）：
   - 問いは短く1つ・選択肢の意味は定義（criteria）に書く
   - 答えの確からしさ（確率）で、どこから「確かめに回す」かの線を決める。線は正解の付いた答えで決める（決め打ちの丸い数にしない）
   - ほかのAIの答えを、根拠と照らして確かめる使い方（確かめる形）が型の1つ
2つの形を、同じ答え（G-53 の約2,200個・付け直した正解）で測る：
   (A) 選ぶ形：どの答えか（確からしさ付き）＋言い切っているか（meaning_judge.jev_request）
   (B) 確かめる形：会話AIが選んだ答え（候補）を、お客様の言葉が裏付けるか。正しい答えと、受け取ると誤りになる答えの両方を候補として聞く
線は、答えを半分に分けて半分（決める側）で選び、残りの半分（確かめる側）で数える（同じ答えで線を決めて数えると、数字が高めに出るため）。
確からしさはすべて結果に残す（線を決め直すときに、問い合わせし直さなくてよい）。
(C) 振り分けの形：選ぶ形に、はっきり度（score）と、難しい答えを見分ける問い（noul）を足し、線を満たさない答えは Claude Sonnet 5.5（考える）へ回す
使い方：python jev_try.py            （問い合わせて数える。結果は results/G-53-jev2_*.json）
        python jev_try.py --gates    （(C) を問い合わせて数える。結果は results/G-53-jev3_*.json）
        python jev_try.py --reuse 結果  （問い合わせずに数え直す）"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from common import HERE, base_parser, load_secret, require_python, save_result, say
from flowctl import ANY, HANDOVER, HOLD, UNDECIDED
from g53_meaning import accept, dangerous, g48_questions, hedged, value_questions
from meaning_judge import FIRM, JEV_FIRM, OTHER_JUDGES, options, other_client, read, jev_request, scene

NAME = "Jev 1.14"
SOURCE = "results/G-53-firm2_20261018_162826.json"  # 同じ答え（いまの判定と Claude の2つも、この答えで測った）
VERIFY = {"true": "お客様の言葉の意味は、会話のAIが選んだ答えだと、はっきり言える（言い直しへの返事なら、言い直した意味を認めた、"
                  "または言い直しを打ち消して、その答えの側を言った）。言い切っていなくても、その答えに寄っていれば当てはまる",
          "false": "お客様の言葉は、別の答え・逆の意味である。または、どの答えとも言えない（迷っている・どちらとも言っていない・"
                   "答えになっていない・別の人が話し始めた・待つように言った）"}
NOT_VALUES = (UNDECIDED, "unknown", "refused", HOLD, HANDOVER)


def verify_request(model: str, q, text: str, asked: str | None, cand: str) -> dict:
    """(B) 確かめる形：会話AIが選んだ答え（cand＝判定に見せる名前）を、お客様の言葉が裏付けるか（noul）＋言い切っているか。"""
    return {"model": model, "state": scene(q, text, asked) | {"会話のAIが選んだ答え": cand},
            "questions": {"supported": {"type": "noul", "instructions": "お客様の言葉は、会話のAIが選んだ答えを裏付けていますか？", "criteria": VERIFY},
                          FIRM: {"type": "noul", "instructions": "お客様は、その答えを言い切っていますか？", "criteria": JEV_FIRM}}}


# (C) 振り分けの形（✅ 2026-10-18 ユーザー：「scoreと組み合わせることで、このスコア以上ならJevで間違いなく正確というラインを決めて、日常利用を
#     Jev、難しい判断はsonnetなどにするなどして、精度アップができませんか？」）：選ぶ形と同じ場面に、はっきり度（score）と、Jev が自信を持って
#     誤った答えの種類（断りの「結構です」・電話を代わった人のあいさつ・条件付きの話）を見分ける問い（noul）を足す。どれかに当たれば上のモデルへ回す
CLARITY = ["どの答えとも言えない", "どれかの答えに寄っているが、含みを残している", "答えをはっきり言い切っている"]
GATES = {"decline": ("お客様は、こちらの申し出や確かめを断っていますか？",
                     {"true": "断っている（「結構です」「いりません」「けっこうです」など、受け取らない・要らないと言っている）",
                      "false": "断っていない"}),
         "handover": ("この言葉は、電話を代わった別の人の名乗りやあいさつですか？",
                      {"true": "電話を代わった人が、名乗りやあいさつをしている（「お電話代わりました」「〇〇でございます」など）",
                       "false": "電話に出ているお客様が、質問に返事をしている"}),
         "condition": ("お客様は、答えに条件や但し書きを付けていますか？",
                       {"true": "ある条件のときだけの話をしている（「〜だと」「〜なら」「〜全体では」など）",
                        "false": "条件を付けずに答えている"})}


def gate_request(model: str, q, text: str, asked: str | None) -> dict:
    body = jev_request(model, q, text, asked)
    body["questions"] |= {"clarity": {"type": "score", "instructions": "お客様の答えは、どのくらいはっきりしていますか？", "criteria": CLARITY}}
    body["questions"] |= {k: {"type": "noul", "instructions": ins, "criteria": cr} for k, (ins, cr) in GATES.items()}
    return body


def candidates(it: dict, q) -> dict:
    """確かめる形で聞く候補（中の名前）：正しい答え（値のとき）と、受け取ると誤りになる答え。"""
    ok = accept(it)
    shown = [c for _, _, c in options(q, it.get("asked"))]
    pos = next((c for c in ok if c not in NOT_VALUES and c in shown), None)
    if it["set"] == "D":
        wrong = it.get("asked_choice") or "affirmed"
    else:
        wrong = next((c for c in q.conditions if c not in ok), None)
    neg = wrong if wrong in shown and wrong not in ok else None
    return {k: v for k, v in (("pos", pos), ("neg", neg)) if v}


def half(it: dict) -> str:
    h = hashlib.sha256(f"{it['qid']}|{it['text']}|{it.get('asked')}".encode()).digest()[0]
    return "決める側" if h % 2 == 0 else "確かめる側"


def ask_all(items: list[dict], qmap: dict, env, gates: bool = False) -> None:
    j = OTHER_JUDGES[NAME]
    key = load_secret(j["key"], env)

    def one(it: dict) -> None:
        q, client = qmap[it["qid"]], other_client(NAME, key)
        if gates:
            jobs = [("C", gate_request(j["model"], q, it["text"], it.get("asked")))]
        else:
            jobs = [("A", jev_request(j["model"], q, it["text"], it.get("asked")))]
            names = {c: n for n, _, c in options(q, it.get("asked"))}
            for side, c in candidates(it, q).items():
                jobs.append((f"B_{side}", verify_request(j["model"], q, it["text"], it.get("asked"), names[c]), c))
        for job in jobs:
            for attempt in range(5):
                t = time.monotonic()
                try:
                    r = client.post("/api/v1/systemone", json=job[1])
                except httpx.HTTPError as exc:  # ⛔ 2026-10-18：1回の時間切れで測定全体が止まり、問い合わせた分が残らなかった ⇒ やり直す
                    it[f"jev_{job[0]}"] = {"error": f"{type(exc).__name__}"}
                    time.sleep(2 * 2 ** attempt)
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(float(r.headers.get("retry-after") or 0) or 2 * 2 ** attempt)
                    continue
                d = r.json() if r.status_code == 200 else {"error": r.text[:200]}
                it[f"jev_{job[0]}"] = {"answers": d.get("answers"), "error": d.get("error"), "秒": round(time.monotonic() - t, 3),
                                       "cost": (d.get("usage") or {}).get("cost"), **({"候補": job[2]} if len(job) > 2 else {})}
                break

    with ThreadPoolExecutor(12) as ex:
        for n, _ in enumerate(ex.map(one, items), 1):
            if n % 200 == 0:
                say("問い合わせた答え", n, "/", len(items))


def reading_A(it: dict, q, conf_min: float, firm_min: float) -> tuple[str | None, bool | None]:
    """(A) の答えを、線で読む：確からしさ（confidence）が線より下なら「決められない」・言い切っている確からしさが線より下なら言い切っていない。"""
    a = (it.get("jev_A") or {}).get("answers") or {}
    ch, firm = a.get("choice") or {}, (a.get(FIRM) or {}).get("noul")
    got = read(q, json.dumps({"choice": ch.get("choice")}, ensure_ascii=False), it.get("asked"))
    if got is None:
        return None, None
    if (ch.get("confidence") or 0) < conf_min:
        return UNDECIDED, None
    return str(got), (firm is None or firm >= firm_min)


def score_A(items, qmap, conf_min, firm_min) -> Counter:
    c = Counter()
    for it in items:
        q = qmap[it["qid"]]
        got, firm = reading_A(it, q, conf_min, firm_min)
        if got is None:
            c["読めない"] += 1
            continue
        t = {**it, "j": got, "j言い切り": firm}
        c["判定した"] += 1
        if got in accept(it):
            c["正しい"] += 1
            if got not in NOT_VALUES:
                c["曖昧の印・" + ("付いた" if hedged(it) and firm is False else "落ちた" if hedged(it) else
                               "要らない" if firm is False else "なし")] += 1
        elif dangerous(t, q, "j"):
            c["⛔危ない"] += 1
        else:
            c["確かめに回る"] += 1
    return c


def score_B(items, qmap, sup_min) -> Counter:
    c = Counter()
    for it in items:
        for side in ("pos", "neg"):
            b = it.get(f"jev_B_{side}")
            if not b or not b.get("answers"):
                continue
            p = (b["answers"].get("supported") or {}).get("noul")
            yes = p is not None and p >= sup_min
            c[f"{side}・聞いた"] += 1
            c[f"{side}・裏付けた"] += yes
    return c


BIG = "Claude Sonnet 5.5（考える）"   # 上へ回す先（同じ答えで測った読み＝SOURCE）


def gated(it: dict, q, rule: dict) -> tuple[str | None, bool | None, bool]:
    """(C) の振り分け：Jev のままでよいか（rule の線をすべて満たす）。満たさなければ上のモデルの読み。返り値＝(読み, 言い切っているか, 上へ回したか)。"""
    a = (it.get("jev_C") or {}).get("answers") or {}
    ch = a.get("choice") or {}
    got = read(q, json.dumps({"choice": ch.get("choice")}, ensure_ascii=False), it.get("asked"))
    clarity = (a.get("clarity") or {}).get("score")
    ok = (got is not None and (ch.get("confidence") or 0) >= rule["conf"]
          and (clarity is None or clarity >= rule["clarity"])
          and all(((a.get(k) or {}).get("noul") or 0) < rule[k] for k in GATES))
    if ok:
        firm = (a.get(FIRM) or {}).get("noul")
        return str(got), (firm is None or firm >= 0.2), False
    return it.get(BIG), it.get(f"{BIG}言い切り"), True


def score_C(items, qmap, rule) -> Counter:
    c = Counter()
    for it in items:
        q = qmap[it["qid"]]
        got, firm, up = gated(it, q, rule)
        c["上へ回した"] += up
        if got is None:
            c["読めない"] += 1
            continue
        t = {**it, "j": got, "j言い切り": firm}
        c["判定した"] += 1
        c["正しい" if got in accept(it) else "⛔危ない" if dangerous(t, q, "j") else "確かめに回る"] += 1
        secs = ((it.get("jev_C") or {}).get("秒") or 0) + ((it.get(f"{BIG}秒") or 0) if up else 0)
        c["2秒以内"] += secs <= 2
    return c


RULES_C = {"確からしさだけ": {"conf": 0.8, "clarity": 0, "decline": 2, "handover": 2, "condition": 2},
           "確からしさ＋はっきり度": {"conf": 0.8, "clarity": 1.5, "decline": 2, "handover": 2, "condition": 2},
           "確からしさ＋見分ける問い": {"conf": 0.8, "clarity": 0, "decline": 0.5, "handover": 0.5, "condition": 0.5},
           "全部": {"conf": 0.8, "clarity": 1.5, "decline": 0.5, "handover": 0.5, "condition": 0.5},
           "全部（確からしさ0.9）": {"conf": 0.9, "clarity": 1.5, "decline": 0.5, "handover": 0.5, "condition": 0.5},
           "全部（見分ける問いを厳しく）": {"conf": 0.8, "clarity": 1.5, "decline": 0.2, "handover": 0.2, "condition": 0.2},
           "Sonnet だけ": {"conf": 2, "clarity": 0, "decline": 2, "handover": 2, "condition": 2}}


def report_C(items, qmap) -> dict:
    return {side: {name: dict(score_C([it for it in items if half(it) == side], qmap, rule)) for name, rule in RULES_C.items()}
            for side in ("決める側", "確かめる側")}


def report(items, qmap) -> dict:
    out = {}
    for side in ("決める側", "確かめる側"):
        part = [it for it in items if half(it) == side]
        out[side] = {"(A) 選ぶ形": {f"確からしさ{cm}以上・言い切り{fm}以上": dict(score_A(part, qmap, cm, fm))
                                   for cm in (0.0, 0.6, 0.8, 0.9, 0.95, 0.99) for fm in (0.5, 0.2)},
                     "(B) 確かめる形": {f"裏付け{sm}以上": dict(score_B(part, qmap, sm)) for sm in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99)}}
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--reuse", help="前の結果（問い合わせずに数え直す）")
    p.add_argument("--gates", action="store_true", help="(C) 振り分けの形だけを聞いて数える（結果は results/G-53-jev3_*.json）")
    args = p.parse_args()
    qmap = {q.id: q for q in g48_questions() + value_questions()}
    from g14_listen import business
    qmap |= {qid: business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")}
    src = args.reuse or str(HERE / SOURCE)
    items = [it for it in json.loads(open(src, encoding="utf-8").read())["items"] if it["set"] in "ABDE" and it["qid"] in qmap]
    gates = args.gates or any("jev_C" in it for it in items)
    if not args.reuse:
        items = [{k: v for k, v in it.items() if not k.startswith("jev_")} for it in items]
        ask_all(items, qmap, args.env, gates)
    keys = ("jev_C",) if gates else ("jev_A", "jev_B_pos", "jev_B_neg")
    cost = sum((it.get(k) or {}).get("cost") or 0 for it in items for k in keys)
    errors = sum(1 for it in items for k in keys if (it.get(k) or {}).get("error"))
    summary = {"答え": len(items), "料金（ドル）": round(cost, 4), "失敗": errors,
               "数え": report_C(items, qmap) if gates else report(items, qmap)}
    say(json.dumps(summary, ensure_ascii=False, indent=1))
    if not args.reuse:
        save_result("G-53-jev3" if gates else "G-53-jev2", {"items": items, "summary": summary})


if __name__ == "__main__":
    main()
