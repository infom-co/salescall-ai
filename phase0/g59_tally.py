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
from flowctl import facts_reading

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
                value = facts_reading(got)[0] if len(got) == len(facts) else None
                if bad or value not in acc:
                    print(name, r.get("rep", ""), col, scene(r), r["text"], "｜前：", r["before"] or "", "｜外れ：", bad,
                          "｜読み：", value, "○" if value in acc else "×")


def plan(cheap: list[str], jev: list[str], group: str, line: float) -> None:
    table = current()
    cheap_rows, jev_rows = collections.defaultdict(list), collections.defaultdict(list)
    for name in cheap:
        for r in rows_of(name):
            cheap_rows[key(r)].append(r)
    for name in jev:
        for r in rows_of(name):
            jev_rows[key(r)].append(r)
    groups = {key(r): r for r in rows_of(group)}
    by, slow, count, out = collections.Counter(), collections.Counter(), collections.Counter(), []
    for k, js in jev_rows.items():
        acc, facts = table[k]
        for i, jr in enumerate(js):
            j = jr.get("jev") or {}
            sure = min(v[1] for v in j.values()) if judged(jr, "jev") else 0.0
            if sure >= line:
                who, got, s = "Jev", judged(jr, "jev"), 0.3
            elif scene(jr) in HARD:
                gr = groups[k]
                who, got, s = "今の組", gr.get("judge2_facts"), gr.get("judge2_s") or 99
            else:
                cr = cheap_rows[k][i % len(cheap_rows[k])]
                who, got, s = "安い判定", cr.get("judge2_facts"), cr.get("judge2_s") or 99
            by[who] += 1
            value = facts_reading(got)[0] if got else None
            ok, fok = value in acc, g.facts_ok(got, facts)
            count["回"] += 1
            count["読み○"] += ok
            count["項目○"] += fok
            if s > 2.0:
                slow[f"{who}・{scene(jr)}"] += 1
            if not (ok and fok):
                out.append((scene(jr), who, jr["text"], jr["before"] or "", "読み○" if ok else "読み×",
                            {x: (got.get(x) if got else None, sorted(v)) for x, v in facts.items() if not got or got.get(x) not in v}))
    print("決めた判定：", dict(by), "｜", dict(count))
    print("2秒超：", dict(slow))
    for x in out:
        print("  外れ：", x)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("misses")
    m.add_argument("names", nargs="+")
    q = sub.add_parser("plan")
    q.add_argument("--cheap", required=True, help="安い判定の G-59 の答え（,で区切る。回りの順に Jev と組む）")
    q.add_argument("--jev", required=True, help="Jev だけに聞いた行の並び（,で区切る）")
    q.add_argument("--group", required=True, help="今の組の G-59 の答え")
    q.add_argument("--line", type=float, default=0.8)
    a = p.parse_args()
    if a.cmd == "misses":
        misses(a.names)
    else:
        plan(a.cheap.split(","), a.jev.split(","), a.group, a.line)


if __name__ == "__main__":
    main()
