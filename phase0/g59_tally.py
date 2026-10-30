"""G-59 の答え（results/ の G-59*.json と、Jev だけに聞いた行の並び）を、いまの正解の表（g59_calling_judge.cases）で数える（0円）。
- misses：組み合わせた読みか、項目ごとの答えの外れを1行ずつ出す
- plan：判定を場面で選び分ける形を数える（✅ 2026-10-29 ユーザー：「Jevの0.8以上と『難しくない場合』Haiku（考える）で、『難しい場合』のみ
  Sonnetを使う形がいいんじゃないですか？」）＝Jev の確からしさが線から上なら Jev、そうでなければ難しくない場面（呼ぶ一言への返事・確かめへの
  返事）は安い判定、難しい場面（取り次ぎのあと・後任の方）は今の組。場面は流れの制御の状態だけで分ける（言葉の中身で分けない）"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import g59_calling_judge as g
from flowctl import HANDOVER, TARGET_IS_NOT, UNDECIDED, facts_reading

RESULTS = Path(__file__).parent / "results"
HARD = {"取り次ぎのあと", "後任の方"}


def rows_of(name: str) -> list[dict]:
    d = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    return d["rows"] if isinstance(d, dict) else d


def key(r: dict) -> tuple:
    return r["text"], r["before"], bool(r["asked"]), r["successor"]


def scene(r: dict) -> str:
    return "後任の方" if r["successor"] else "確かめへの返事" if r["asked"] else "取り次ぎのあと" if r["before"] else "呼ぶ一言への返事"


def current() -> dict:
    """いまの正解の表：場面の鍵 ⇒ (受け取ってよい読み, 項目ごとの正解)。前の表で作った答えも、この表で数え直す。"""
    return {(t, b, a is not None, s): (acc, f) for t, acc, b, a, s, f in g.cases("確かめる一言")}


def flow_ok(got: dict, facts: dict, acc: list, r: dict) -> bool:
    """流れの制御の動きが正解と同じか：組み合わせた読み（flowctl.facts_reading）が受け取ってよい読みに入り、印（不在・電話を代わった方・後任の方）も
    正解の組み合わせのどれかと同じ。項目だけの外れ（読みも印も変わらない）は、流れの制御の動きを変えない。電話を代わった方の印は、流れの制御が
    使うときだけ比べる（Flow._new_speaker：確かめる一言を聞いた方の返事・取り次ぎの言葉のあとの言葉では使わない＝g.HELD_BEFORE）。"""
    from itertools import product
    seeks = r["successor"]
    used = not r["asked"] and not set((r["before"] or "").split("／")) & g.HELD_BEFORE
    keys = list(facts)
    def flags(f):
        value, away, new, succ = facts_reading(f, seeks)
        if not used and value == HANDOVER:   # 印を使わない所では「代わった人が話し始めた」＝「別の方」と同じ動き（2026-10-30 に会話の試しで確かめた）
            value = TARGET_IS_NOT
        # 「決められない」の読みは、必ず確かめに回り、確かめたあとに、あいさつと名乗りから流す＝電話を代わった方の印は動きを変えない（同じ日に確かめた）
        return value, away, new if used and value != UNDECIDED else None, succ
    want = {flags(dict(zip(keys, c)))[1:] for c in product(*(facts[k] for k in keys))}
    value, *rest = flags(got)
    accepted = {TARGET_IS_NOT if not used and a == HANDOVER else a for a in acc}
    return value in accepted and tuple(rest) in want


def judged(r: dict, col: str) -> dict | None:
    """1行の、判定 col の項目ごとの答え（G-59 の行は「<col>_facts」・Jev だけに聞いた行は「jev」＝{項目: (答え, 確からしさ)}）。"""
    if col == "jev":
        j = r.get("jev") or {}
        return {k: v[0] for k, v in j.items()} if j and all(v[0] is not None for v in j.values()) else None
    return r.get(f"{col}_facts")


def misses(names: list[str]) -> None:
    table = current()
    for name in names:
        for r in rows_of(name):
            acc, facts = table[key(r)]
            for col in ["jev"] if "jev" in r else [k[:-6] for k in r if k.endswith("_facts") and not k.endswith("_asks_facts")]:
                got = judged(r, col) or {}
                bad = {k: (got.get(k), sorted(v)) for k, v in facts.items() if got.get(k) not in v}
                value = facts_reading(got, r["successor"])[0] if len(got) == len(facts) else None
                if bad or value not in acc:
                    moved = not (len(got) == len(facts) and flow_ok(got, facts, acc, r))
                    print(name, r.get("rep", ""), col, scene(r), r["text"], "｜前：", r["before"] or "", "｜外れ：", bad,
                          "｜読み：", value, "○" if value in acc else "×", "｜流れの制御の動き：", "変わる" if moved else "同じ")


def source(spec: str, col: str) -> tuple[str, str]:
    """「ファイル名:列」（列を書かなければ col）⇒ (ファイル名, 列)。列は G-59 の行の判定の列（judge＝その場の判定・judge2＝2つ目の判定）か、
    Jev だけに聞いた行の並びの jev。"""
    name, _, c = spec.partition(":")
    return name, c or col


def answer(r: dict, col: str) -> tuple[dict | None, float, float]:
    """1行の、判定 col の (項目ごとの答え, 確からしさ, 答えまでの秒)。G-59 の形の Jev は、確からしさが線（meaning_judge.sure_line）から上のときだけ
    答えを残す（下なら答え無し＝確からしさ 0）。確からしさを返さない判定は 1。答えの無い判定（止まった・上限）は (None, 0, 99)。"""
    if col == "jev":
        j = r.get("jev") or {}
        return (judged(r, "jev"), min(v[1] for v in j.values()), 0.3) if judged(r, "jev") else (None, 0.0, 0.3)
    got = r.get(f"{col}_facts")
    sure = r.get(f"{col}_sure")
    return got, (1.0 if sure is None else sure) if got else 0.0, r.get(f"{col}_s") or 99.0


def plan(cheap: list[str], jev: list[str], group: str, line: float) -> None:
    """場面で選び分ける形：Jev の確からしさが線から上なら Jev、そうでなければ難しくない場面（呼ぶ一言への返事・確かめへの返事）は安い判定、難しい
    場面（取り次ぎのあと・後任の方）は今の組。外れは、流れの制御の動きが変わるもの（flow_ok）と、項目だけのものを分けて、場面ごとに数える。
    回りの数が違う答えは、言葉ごとに、多いほうの回りに合わせて少ないほうをくり返して組む（⛔ 2026-10-30：前は Jev だけに聞いた行の並びと、
    2つ目の判定の列しか読めず、外れも項目で数えていた）。"""
    table = current()
    def load(specs: list[str], col: str) -> dict:
        rows = collections.defaultdict(list)
        for spec in specs:
            name, c = source(spec, col)
            for r in rows_of(name):
                rows[key(r)].append((r, c))
        return rows
    cheap_rows, jev_rows, group_rows = load(cheap, "judge2"), load(jev, "jev"), load([group], "judge2")
    by, slow, moved, only, none, count = (collections.Counter() for _ in range(6))
    out = []
    for k in sorted(set(cheap_rows) | set(jev_rows) | set(group_rows), key=str):
        acc, facts = table[k]
        js, cs, gs = jev_rows[k], cheap_rows[k], group_rows[k]
        for i in range(max(len(js), len(cs), len(gs))):
            jr, jc = js[i % len(js)]
            got, sure, s = answer(jr, jc)
            sc = scene(jr)
            if got and sure >= line:
                who = "Jev"
            else:
                r, c = (gs if sc in HARD else cs)[i % len(gs if sc in HARD else cs)]
                who = "今の組" if sc in HARD else "安い判定"
                got, _, s2 = answer(r, c)
                s = 0.3 + s2 if js else s2   # Jev を先に出して確かでなければ出す（0.3秒で出す＝meaning_judge の組と同じ）
            by[who] += 1
            count[sc] += 1
            if s > 2.0:
                slow[f"{who}・{sc}"] += 1
            if not got or len(got) != len(facts):
                none[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or "", "答え無し"))
                continue
            if not flow_ok(got, facts, acc, jr):
                moved[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or "", "動きが変わる",
                            {x: (got.get(x), sorted(v)) for x, v in facts.items() if got.get(x) not in v}))
            elif not g.facts_ok(got, facts):
                only[f"{who}・{sc}"] += 1
    print("決めた判定：", dict(by), "｜場面ごとの回：", dict(count))
    print("流れの制御の動きが変わる外れ：", dict(moved) or 0)
    print("項目だけの外れ：", dict(only) or 0, "｜答え無し：", dict(none) or 0)
    print("2秒超：", dict(slow) or 0)
    for x in out:
        print("  ", x)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("misses")
    m.add_argument("names", nargs="+")
    q = sub.add_parser("plan")
    q.add_argument("--cheap", required=True, help="安い判定の答え（「ファイル名:列」を,で区切る。列の既定は judge2・その場の判定は judge）")
    q.add_argument("--jev", required=True, help="Jev の答え（「ファイル名:列」を,で区切る。列の既定は jev＝Jev だけに聞いた行の並び・G-59 の形は judge2）")
    q.add_argument("--group", required=True, help="今の組の答え（「ファイル名:列」。列の既定は judge2）")
    q.add_argument("--line", type=float, default=0.8)
    a = p.parse_args()
    if a.cmd == "misses":
        misses(a.names)
    else:
        plan(a.cheap.split(","), a.jev.split(","), a.group, a.line)


if __name__ == "__main__":
    main()
