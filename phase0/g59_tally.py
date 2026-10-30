"""G-59 の答え（results/ の G-59*.json と、Jev だけに聞いた行の並び）を、いまの正解の表（g59_calling_judge.cases）で数える（0円）。
- misses：組み合わせた読みか、項目ごとの答えの外れを1行ずつ出す
- plan：判定を場面で選び分ける形を数える（✅ 2026-10-29 ユーザー：「Jevの0.8以上と『難しくない場合』Haiku（考える）で、『難しい場合』のみ
  Sonnetを使う形がいいんじゃないですか？」）＝Jev の確からしさが線から上なら Jev、そうでなければ難しくない場面（呼ぶ一言への返事・確かめへの
  返事）は安い判定、難しい場面（取り次ぎのあと・後任の方）は今の組。場面は流れの制御の状態だけで分ける（言葉の中身で分けない）
- agree：会社の違う安い判定2つの読みが、流れの制御の動きでそろえば決め、そろわなければ今の組を出す形を数える（2秒超も、ご本人の回とほかの回に分ける）
- cascade：判断のモデル（Jev・d1 など）を線つきで並べ、どれかが線から上なら決め、どれも違えば今の組を出す形を数える"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import g59_calling_judge as g
from flowctl import HANDOVER, TARGET_IS_NOT, TARGET_IS_THERE, UNDECIDED, facts_reading

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


def flow_flags(f: dict, r: dict) -> tuple:
    """項目ごとの答え f で、行 r の場面の流れの制御がどう動くか（読み・不在・電話を代わった方・後任の方の印）。2つの答えの印が同じなら、流れの制御の
    動きも同じ（flow_ok と、判定どうしのそろいを数える agree の両方が使う1か所）。電話を代わった方の印は、流れの制御が使うときだけ持つ
    （Flow._new_speaker：確かめる一言を聞いた方の返事・取り次ぎの言葉のあとの言葉では使わない＝g.HELD_BEFORE）。"""
    used = not r["asked"] and not set((r["before"] or "").split("／")) & g.HELD_BEFORE
    value, away, new, succ = facts_reading(f, r["successor"])
    if not used and value == HANDOVER:   # 印を使わない所では「代わった人が話し始めた」＝「別の方」と同じ動き（2026-10-30 に会話の試しで確かめた）
        value = TARGET_IS_NOT
    # 「決められない」の読みは、必ず確かめに回り、確かめたあとに、あいさつと名乗りから流す＝電話を代わった方の印は動きを変えない（同じ日に確かめた）
    return value, away, new if used and value != UNDECIDED else None, succ


def flow_ok(got: dict, facts: dict, acc: list, r: dict) -> bool:
    """流れの制御の動きが正解と同じか：組み合わせた読み（flowctl.facts_reading）が受け取ってよい読みに入り、印（不在・電話を代わった方・後任の方）も
    正解の組み合わせのどれかと同じ（flow_flags）。項目だけの外れ（読みも印も変わらない）は、流れの制御の動きを変えない。"""
    from itertools import product
    used = not r["asked"] and not set((r["before"] or "").split("／")) & g.HELD_BEFORE
    keys = list(facts)
    want = {flow_flags(dict(zip(keys, c)), r)[1:] for c in product(*(facts[k] for k in keys))}
    value, *rest = flow_flags(got, r)
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


def agree(a_specs: list[str], b_specs: list[str], jev: list[str], group: str, line: float, sonnet_scenes: set[str]) -> None:
    """判定2つのそろいで決める形（✅ 2026-10-30 ユーザー：「考えない Sonnet の割合を下げながら、100％精度を出す組や仕組みを模索してください。」）：
    Jev の確からしさが線から上なら Jev。そうでなければ、sonnet_scenes の場面は今の組、ほかの場面は、安い判定 a と b（Jev と同時に出す）の流れの
    制御の印（flow_flags）がそろえばその読み、そろわなければ今の組（a・b の両方を待ってから出す）。外れは流れの制御の動きで数え、2秒を超えた回は、
    正しい読みがご本人の回（繋ぐ前に判定を待ち、間に合わなければ確かめに回る）と、ほかの回に分ける（答えの段は遅れて当て直す＝Flow.hold_s）。"""
    table = current()
    def load(specs: list[str], col: str) -> dict:
        rows = collections.defaultdict(list)
        for spec in specs:
            name, c = source(spec, col)
            for r in rows_of(name):
                rows[key(r)].append((r, c))
        return rows
    ar, br, jr_, gr = load(a_specs, "judge2"), load(b_specs, "judge2"), load(jev, "jev"), load([group], "judge2")
    by, moved, only, late = (collections.Counter() for _ in range(4))
    out = []
    for k in sorted(set(ar) & set(br) & set(gr), key=str):
        acc, facts = table[k]
        for i in range(max(len(ar[k]), len(br[k]))):
            jr, jc = jr_[k][i % len(jr_[k])] if jr_[k] else (ar[k][0][0], None)
            sc = scene(jr)
            got, sure, _ = answer(jr, jc) if jc else (None, 0.0, 0.0)
            g_got, _, g_s = answer(*gr[k][i % len(gr[k])])
            if got and sure >= line:
                who, s = "Jev", 0.3
            elif sc in sonnet_scenes:
                who, got, s = "今の組（場面）", g_got, 0.3 + g_s
            else:
                (a_got, _, a_s), (b_got, _, b_s) = answer(*ar[k][i % len(ar[k])]), answer(*br[k][i % len(br[k])])
                both = max(a_s, b_s)
                if a_got and b_got and len(a_got) == len(b_got) == len(facts) and flow_flags(a_got, jr) == flow_flags(b_got, jr):
                    who, got, s = "そろい", a_got, both
                else:
                    who, got, s = "今の組（食い違い）", g_got, both + g_s
            by[who] += 1
            if s > 2.0:
                late["ご本人の回" if TARGET_IS_THERE in acc else "ほかの回"] += 1
            if not got or len(got) != len(facts) or not flow_ok(got, facts, acc, jr):
                moved[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or ""))
            elif not g.facts_ok(got, facts):
                only[f"{who}・{sc}"] += 1
    n = sum(by.values())
    group_n = by["今の組（場面）"] + by["今の組（食い違い）"]
    print("決めた判定：", dict(by), f"｜今の組を出した回 {group_n}/{n}（{group_n / n:.0%}）")
    print("流れの制御の動きが変わる外れ：", dict(moved) or 0, "｜項目だけの外れ：", dict(only) or 0)
    print("2秒超：", dict(late) or 0)
    for x in out:
        print("  ", x)


def cascade(stages: list[str], group: str) -> None:
    """判断のモデルを線つきで並べる形（2026-10-30 ユーザー：「他に、無料や格安で使える優秀なAPIはありますか？」）：どれも同時に出し、確からしさが
    その判定の線から上の読みが届けば、並べた順の先のもので決める。どれも線より下なら今の組（0.3秒で出す＝meaning_judge の組と同じ）。
    stages は「ファイル名:列@線」（列の既定は judge2）。外れは流れの制御の動きで数え、2秒超は plan と同じに数える。"""
    table = current()
    loaded = []
    for spec in stages:
        src, _, line = spec.rpartition("@")
        name, c = source(src, "judge2")
        rows = collections.defaultdict(list)
        for r in rows_of(name):
            rows[key(r)].append((r, c))
        loaded.append((name, float(line), rows))
    gr = collections.defaultdict(list)
    gname, gc = source(group, "judge2")
    for r in rows_of(gname):
        gr[key(r)].append((r, gc))
    by, moved, only, late = (collections.Counter() for _ in range(4))
    out = []
    keys = set(gr).intersection(*(set(rows) for _, _, rows in loaded))
    for k in sorted(keys, key=str):
        acc, facts = table[k]
        for i in range(max(len(rows[k]) for _, _, rows in loaded)):
            who = got = None
            for name, line, rows in loaded:
                r, c = rows[k][i % len(rows[k])]
                g_, sure, s = answer(r, c)
                if g_ and sure >= line:
                    who, got, sc_row = name, g_, r
                    break
            if who is None:
                r, c = gr[k][i % len(gr[k])]
                got, _, s = answer(r, c)
                who, sc_row, s = "今の組", r, 0.3 + s
            by[who] += 1
            if s > 2.0:
                late["ご本人の回" if TARGET_IS_THERE in acc else "ほかの回"] += 1
            sc = scene(sc_row)
            if not got or len(got) != len(facts) or not flow_ok(got, facts, acc, sc_row):
                moved[f"{who}・{sc}"] += 1
                out.append((sc, who, sc_row["text"], sc_row["before"] or ""))
            elif not g.facts_ok(got, facts):
                only[f"{who}・{sc}"] += 1
    n = sum(by.values())
    print("決めた判定：", dict(by), f"｜今の組を出した回 {by['今の組']}/{n}（{by['今の組'] / n:.0%}）")
    print("流れの制御の動きが変わる外れ：", dict(moved) or 0, "｜項目だけの外れ：", dict(only) or 0)
    print("2秒超：", dict(late) or 0)
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
    w = sub.add_parser("agree")
    w.add_argument("--a", required=True, help="安い判定の1つ目（「ファイル名:列」を,で区切る。列の既定は judge2・その場の判定は judge）")
    w.add_argument("--b", required=True, help="安い判定の2つ目（会社の違うもの）")
    w.add_argument("--jev", required=True, help="Jev の答え（plan と同じ）")
    w.add_argument("--group", required=True, help="今の組の答え（plan と同じ）")
    w.add_argument("--line", type=float, default=0.8)
    w.add_argument("--sonnet-scenes", default=",".join(sorted(HARD)), help="そろいを使わず今の組で決める場面（,で区切る。空なら全部の場面でそろいを使う）")
    c = sub.add_parser("cascade")
    c.add_argument("--stage", action="append", required=True, help="判断のモデルの答えと線（「ファイル名:列@線」。並べた順に先のもので決める。いくつでも）")
    c.add_argument("--group", required=True, help="今の組の答え（plan と同じ）")
    a = p.parse_args()
    if a.cmd == "misses":
        misses(a.names)
    elif a.cmd == "plan":
        plan(a.cheap.split(","), a.jev.split(","), a.group, a.line)
    elif a.cmd == "cascade":
        cascade(a.stage, a.group)
    else:
        agree(a.a.split(","), a.b.split(","), a.jev.split(","), a.group, a.line, set(filter(None, a.sonnet_scenes.split(","))))


if __name__ == "__main__":
    main()
