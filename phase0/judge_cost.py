"""有料の判定（Anthropic）の料金を、新しく送らずに記録から数える（PHASE0 G-59 の 2026-10-29）。

(1) 送った回数：results/ の記録の、判定ごとの1件の読み（「判定の名前」と「判定の名前＋秒」の対）を、同じ読み（同じ言葉・同じ判定・同じ秒）は
    1回に数える（--reuse で前の回の読みを写した記録を、2回に数えないため）。会話の試験は判定1回ずつの記録が無いので数えられない
    （⇒ これからは meaning_judge.SPEND が、どの結果にも料金を入れる）
(2) 送り方の比べ：3つの判定の読み・確からしさ・秒がそろう記録（意味の判定＝G-53 race・中身の判定＝G-63G67each）で、送り始めの時刻だけを
    変えたときの正しさ・答えまでの秒・Sonnet の数。読みと秒は記録のまま使う
⛔ 2026-10-28 ユーザー：「ここまでで3万円以上使用しているということですよ。無駄遣いや、必要ないのに使っているシーンはありませんか？」
"""
from __future__ import annotations

import collections
import glob
import json
import os
import statistics as st

from common import HERE
from meaning_judge import OTHER_JUDGES

J, T, N = "Jev 1.14", "Claude Sonnet 5.5（考える）", "Claude Sonnet 5.5"
PAID = (T, N, "Claude Haiku 5.5（考える）", "Claude Haiku 5.5")
MEANING = HERE / "results" / "G-53race_b4e8fa3_20261025_122959.json"
GROUND = HERE / "results" / "G-63G67each_20261024_213207.json"
WAITS = (0.0, 0.3, 0.5)   # Jev を先に出して、確かな読みを待つ秒（0＝今の、3つ同時）


def paid_calls() -> dict:
    """日ごと・判定ごとの、本当に送った回数（同じ読みは1回）。"""
    seen: dict = {}

    def walk(x, day):
        if isinstance(x, dict):
            for n in PAID:
                if n + "秒" in x:
                    seen.setdefault((n, x.get("qid"), x.get("text"), x.get("asked"), x.get(n + "秒"), str(x.get(n))), day)
            for v in x.values():
                walk(v, day)
        elif isinstance(x, list):
            for v in x:
                walk(v, day)
    for f in sorted(glob.glob(str(HERE / "results" / "**" / "*.json"), recursive=True)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        walk(d, (d.get("saved_at") or "")[:10] if isinstance(d, dict) else "")
    out = collections.defaultdict(collections.Counter)
    for k, day in seen.items():
        out[day][k[0]] += 1
    return {day: dict(c) for day, c in sorted(out.items())}


def first(readings: dict, wait: float, sonnets: tuple, usable) -> tuple:
    """Jev を先に出し、wait 秒たっても確かな読みが無ければ sonnets を出す（Jev の確かでない読み・失敗が先に届けば、その時に）。wait=0＝同時。
    返す：(使う読み, 届いた秒, 出した Sonnet の数)。readings＝{判定の名前: (読み, 秒, 確からしさ)}。"""
    j = readings.get(J)
    js = j[1] if j else None
    sure = j is not None and js is not None and usable(J, j)
    if sure and js <= wait:
        return j, js, 0
    start = wait if (sure or js is None) else min(wait, js)
    cand = [(js, j)] if sure else []
    cand += [(start + readings[k][1], readings[k]) for k in sonnets if readings.get(k) and readings[k][1] is not None and usable(k, readings[k])]
    if not cand:
        return None, None, len(sonnets)
    s, r = min(cand, key=lambda c: c[0])
    return r, s, len(sonnets)


def compare_meaning() -> list[dict]:
    import g53_meaning as g
    from g14_listen import business

    qmap = {q.id: q for q in g.g48_questions() + g.value_questions()} | {qid: business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")}
    items = json.load(open(MEANING, encoding="utf-8"))["items"]
    line = OTHER_JUDGES[J]["sure_min"]

    def usable(name, r):
        return r[0] is not None and (name != J or (r[2] or 0) >= line)
    rows = []
    for sonnets in ((T, N), (N,)):
        for wait in WAITS if len(sonnets) == 2 else (0.0,):
            calls, secs = 0, []
            for it in items:
                rd = {k: (it.get(k), it.get(k + "秒"), it.get(k + "確からしさ"), k) for k in (J, T, N)}
                r, s, n = first(rd, wait, sonnets, usable)
                calls += n
                it["組"], it["組言い切り"] = (it.get(r[3]), it.get(r[3] + "言い切り")) if r else (None, None)
                if s is not None:
                    secs.append(s)
            t = g.tally(items, ["組"])["組"]["数え"]["全体"]
            secs.sort()
            rows.append({"送り方": f"Jev＋{'・'.join(sonnets)}・待つ{wait}秒", "Sonnet の数": calls, "正しい": t.get("正しい", 0),
                         "危ない向き": sum(1 for it in items if it["qid"] in qmap and it.get("組") is not None and g.dangerous(it, qmap[it["qid"]], "組")),
                         "認めたと誤読": t.get("⛔⛔言い直しを、認めていないのに認めたと読んだ", 0),
                         "秒（中央・遅いほうから1割・2秒超）": [round(st.median(secs), 2), round(secs[int(len(secs) * 0.9)], 2), sum(x > 2 for x in secs)]})
    return rows


def compare_grounding() -> list[dict]:
    raw = json.load(open(GROUND, encoding="utf-8"))["rows"]
    line = OTHER_JUDGES[J]["ground_min"]
    groups = collections.defaultdict(dict)
    for r in raw:
        groups[(r["set"], r["said"], r["draft"], r["rep"])][r["judge"]] = (r["got"], r["seconds"], r.get("sure"), r["want"])

    def usable(name, r):
        return r[0] is not None and (name != J or r[2] is None or r[2] >= line)
    rows = []
    for sonnets in ((T, N), (N,)):
        for wait in WAITS if len(sonnets) == 2 else (0.0,):
            calls, ok, bad, secs = 0, 0, 0, []
            for m in groups.values():
                want = next(iter(m.values()))[3]
                r, s, n = first(m, wait, sonnets, usable)
                calls += n
                if r is not None:
                    ok += r[0] == want
                    bad += want == "止める" and r[0] == "通す"
                    secs.append(s)
            secs.sort()
            rows.append({"送り方": f"Jev＋{'・'.join(sonnets)}・待つ{wait}秒", "Sonnet の数": calls, "正しい": f"{ok}/{len(groups)}",
                         "危ない文を通した": bad,
                         "秒（中央・遅いほうから1割・2秒超）": [round(st.median(secs), 2), round(secs[int(len(secs) * 0.9)], 2), sum(x > 2 for x in secs)]})
    return rows


def main() -> None:
    out = {"送った回数（同じ読みは1回）": paid_calls(), "意味の判定の送り方": compare_meaning(), "中身の判定の送り方": compare_grounding()}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    os.chdir(HERE)
    main()
