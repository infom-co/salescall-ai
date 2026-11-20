"""G-59 の答え（results/ の G-59*.json と、Jev だけに聞いた行の並び）を、いまの正解の表（g59_calling_judge.cases）で数える（0円）。
- misses：組み合わせた読みか、項目ごとの答えの外れを1行ずつ出す
- plan：判定を場面で選び分ける形を数える（✅ 2026-10-29 ユーザー：「Jevの0.8以上と『難しくない場合』Haiku（考える）で、『難しい場合』のみ
  Sonnetを使う形がいいんじゃないですか？」）＝Jev の確からしさが線から上なら Jev、そうでなければ難しくない場面（呼ぶ一言への返事・確かめへの
  返事）は安い判定、難しい場面（取り次ぎのあと・後任の方）は今の組。場面は流れの制御の状態だけで分ける（言葉の中身で分けない）
- agree：会社の違う安い判定2つの読みが、流れの制御の動きでそろえば決め、そろわなければ今の組を出す形を数える（2秒超も、ご本人の回とほかの回に分ける）
- cascade：本番の組（meaning_judge.RACE）が、判定ごとの記録（1つの判定で測った結果）の秒で、どの判定で決めるかを本番と同じ1か所で決めて数える
  （顔ぶれ＝members_for・確かな読み＝sure_reading・先に出すもの＝leads_of・決め方＝Race を記録の秒で動かす race_recorded）"""
from __future__ import annotations

import argparse
import collections
import functools
import json
from pathlib import Path

import g59_calling_judge as g
from flowctl import TARGET_IS_THERE, Limits, facts_reading

RESULTS = Path(__file__).parent / "results"
HARD = {"取り次ぎのあと", "後任の方"}


def rows_of(name: str) -> list[dict]:
    d = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    return d["rows"] if isinstance(d, dict) else d


def key(r: dict) -> tuple:
    """測りの行の鍵（唯一の作り）：言葉・前の言葉・確かめたあとか・後任の方か・フルネームのある業務の行の印 full（2026-11-20 rv176/S。印の無い記録は偽）。"""
    return r["text"], r["before"], bool(r["asked"]), r["successor"], bool(r.get("full"))


def scene(r: dict) -> str:
    return "後任の方" if r["successor"] else "確かめへの返事" if r["asked"] else "取り次ぎのあと" if r["before"] else "呼ぶ一言への返事"


def current() -> dict:
    """いまの正解の表：場面の鍵 ⇒ (受け取ってよい読み, 項目ごとの正解)。前の表で作った答えも、この表で数え直す。"""
    return {(t, b, a is not None, s, c.full): (acc, f) for c in g.cases("確かめる一言") for t, acc, b, a, s, f in (c,)}


# 外れの種類（flow_miss の1か所が返す名前）
ALL_REPORTS = "どの報告でも動きが変わる"
SOME_REPORTS = "報告で動きが変わる（(FN) の残り）"   # 会話AIの報告で、外れが動きを変えるかが変わる＝会話AIが判定に加わっている所（本番の穴）
NO_SCENE = "場面を組めない（数えない）"
UNREADABLE = "今の問いでは読めない読み（数えない）"   # 古い文で取った記録の、今の選べるものに無い答え（本番なら読みとして届かない）
INCOMPLETE = "項目がそろわない"


@functools.cache
def _hear_rows() -> frozenset:
    return frozenset((t, a) for t, a, _ in g.lines_hear())


def hear_only(r: dict) -> bool:
    """聞こえ方だけを数える行（試しの言葉 G-73。ほかの項目は正解を付けていない＝流れの制御の動きは数えない＝g59_calling_judge の run の hear_only）。"""
    return (not r["successor"] and (r["text"], bool(r["asked"])) in _hear_rows()
            and (r["before"] or None) == g.hear_before(bool(r["asked"])))   # 前の言葉は試しの言葉の行の1か所（hear_before）


def right_readings(got: dict, facts: dict) -> list[dict]:
    """正解の項目ごとの答えの組み合わせの全部。項目を足す前の記録（got に無い、あとから足した項目 g.ADDED）は、その項目を読みに入れない
    （g59_calling_judge.facts_ok と同じ決まり）。⛔ 2026-11-19 日本時間21時台まで、正解をまだ付けていない項目（相手の申し出・電話に出られない理由）は
    判定の読みをそのまま入れていた（外れと数えない）＝正解の表に正解を付けて外した。"""
    from itertools import product
    keys = [k for k in facts if not (k in g.ADDED and k not in got)]
    return [dict(zip(keys, c)) for c in product(*(sorted(facts[k]) for k in keys))]


def off_reports(got: dict | None, facts: dict, r: dict, asks: str | None = None) -> list[str]:
    """判定の読みで、行 r の場面の本番の流れの制御の動きが、正しい読みの動きと違う会話AIの報告の名前の並び（唯一の比べ。同じなら空）。判定の読みは、
    目的のお客様の判定の読み got（項目ごとの答え）か、尋ねているかの問い合わせの読み asks（そのときの got は None）の1つ（測りの数えの2つの問い合わせ＝cascade の
    PARTS）。もう1つの問い合わせは正しい読み（正解の表の読み right_readings・尋ねているかの正しい読み g59_calling_judge.asks_truths）を同じ入口で渡す。
    行の場面を本番の流れの制御に組み（g59_flow_check.row_scene）、読みを本番と同じ入口で渡し、会話AIの報告（g59_flow_check.REPORTS＝ご本人・受付の方・
    代理の方・代わった方・後任の方・お呼びした方で答える・報告なし）ごとに動きの名前（g59_flow_check.act_name）を比べる：その報告で、判定の読みの動きが、
    もう1つの問い合わせの同じ正しい読みでの正しい読みのどれかの動きと同じなら同じ（正解の動きも判定の読みの動きも g59_flow_check.calling_acts の1か所）。
    場面を組めない行は g59_flow_check.NoScene・今の問いで読めない判定の読みは g59_flow_check.Unreadable を上げる（正解の読みが読めなければ正解の表の誤り＝止める）。
    ⛔ 4bc94b4d まで、尋ねているかの読みは比べず（いつも正しい読み）、尋ねているかの外れは値だけで数えていた（まとめ役 CL-1 の頼み rv178/X の4回目 (1)(2)）"""
    import g59_flow_check as fc
    readings = right_readings(facts if got is None else got, facts)   # 尋ねているかの読みを比べるときは、正解の表の項目の全部
    try:
        rights = [fc.calling_acts(r, c) for c in readings]
    except fc.Unreadable as e:
        raise ValueError(f"正解の表の読みが今の問いで読めない（{r['text']}）：{e}") from None
    if asks is None:
        mine = fc.calling_acts(r, got)
        # 正しい尋ねているかの読み（a）ごとに比べ、どれかで違えば、その報告は違う
        return [name for i, (name, _) in enumerate(fc.REPORTS) if any(mine[a][i] not in {x[a][i] for x in rights} for a in range(len(mine)))]
    # 目的のお客様の判定の正しい読み（c）ごとに比べ、どれかで違えば、その報告は違う
    mine = [fc.calling_acts(r, c, asks)[0] for c in readings]
    return [name for i, (name, _) in enumerate(fc.REPORTS) if any(m[i] not in {y[i] for y in x} for m, x in zip(mine, rights))]


def flow_miss(got: dict | None, facts: dict, r: dict, asks: str | None = None) -> str | None:
    """判定の読み（目的のお客様の判定の got か、尋ねているかの問い合わせの asks＝off_reports）で、行 r の場面の本番の流れの制御の動きが正しい読みの動きと違えば、その種類（唯一の数え。違わなければ None）：全部の報告で違う
    （ALL_REPORTS）か、一部の報告で違う（SOME_REPORTS＋報告の名前＝会話AIの報告で外れが動きを変えるかが変わる＝会話AIが判定に加わっている所）。
    報告ごとの比べは off_reports の1か所。場面を組めない行は NO_SCENE・今の問いで読めない読みは UNREADABLE。
    ⛔ 2026-11-18（見直し (13) の持ち場 D の D1・D2）：それまでは流れの制御の決まりを手で写した印（flow_flags：読み・不在・電話を代わった方・後任の方・
    電話を終える言葉か）で比べ、(D1) 前の言葉の無い「お電話代わりました。山本です。」を代理の方と読んだ外れ（本番は代理の方・後任の方の報告で動きが
    変わる）を「項目だけ」と数え、(D2) 電話を終える言葉の読みを必ず「締める」と数えていた（本番は取り次ぎを待つ読みがあれば待つ）。"""
    import g59_flow_check as fc
    try:
        off = off_reports(got, facts, r, asks)
    except fc.NoScene:
        return NO_SCENE
    except fc.Unreadable:
        return UNREADABLE
    if not off:
        return None
    return ALL_REPORTS if len(off) == len(fc.REPORTS) else f"{SOME_REPORTS}：{'・'.join(off)}"


def flow_kind(got: dict | None, facts: dict, r: dict, asks: str | None = None) -> str | None:
    """1行の判定の読み got の、流れの制御の動きの外れの種類（同じなら None）：項目がそろわない（INCOMPLETE）・flow_miss の種類。聞こえ方だけを数える行は、
    動きを数えない（None）。plan・agree・cascade・misses の1か所。asks＝尋ねているかの問い合わせの判定の読み（渡すと、その読みの外れの種類＝got は None。
    目的のお客様の判定は正解の表の読み＝off_reports。cascade の尋ねているかの問い合わせ）。"""
    if asks is None and not g.facts_whole(got, facts):
        return INCOMPLETE
    if hear_only(r):
        return None
    return flow_miss(got, facts, r, asks)


UNCOUNTED = (NO_SCENE, UNREADABLE)   # 流れの制御の動きを比べられない行（別に数えて出す＝黙って数えない形にしない）


def moves(kind: str | None) -> bool:
    """外れの種類が、流れの制御の動きを変える外れか（場面を組めない行・今の問いで読めない読みは、動きを数えない＝別に数える）。"""
    return kind is not None and kind not in UNCOUNTED


def same_moves(a: dict, b: dict, r: dict) -> bool:
    """2つの判定の読みで、行 r の場面の流れの制御の動きが、どの報告でも同じか（判定どうしのそろいを数える agree）。場面を組めない行はそろわない。"""
    import g59_flow_check as fc
    try:
        return fc.calling_acts(r, a) == fc.calling_acts(r, b)
    except (fc.NoScene, fc.Unreadable):
        return False


def judged(r: dict, col: str) -> dict | None:
    """1行の、判定 col の項目ごとの答え（G-59 の行は「<col>_facts」・Jev だけに聞いた行は「jev」＝{項目: (答え, 確からしさ)}）。"""
    if col == "jev":
        j = r.get("jev") or {}
        return {k: v[0] for k, v in j.items()} if j and all(v[0] is not None for v in j.values()) else None
    return r.get(f"{col}_facts")


def misses(names: list[str]) -> None:
    table = current()
    for name in names:
        rows = rows_of(name)
        if gone := [r for r in rows if key(r) not in table]:   # いまの正解の表に無い場面の行（前の言葉を直す前の記録＝D6 など）は数えない（cascade と同じ）
            print(name, "｜いまの正解の表に無い行（数えない）：", len(gone))
        for r in (r for r in rows if key(r) in table):
            acc, facts = table[key(r)]
            for col in ["jev"] if "jev" in r else [k[:-6] for k in r if k.endswith("_facts") and not k.endswith("_asks_facts")]:
                got = judged(r, col) or {}
                bad = {k: (got.get(k), sorted(v)) for k, v in facts.items() if got.get(k) not in v}
                value = facts_reading(got, r["successor"])[0] if g.facts_whole(got, facts) else None
                if bad or value not in acc:
                    kind = flow_kind(got, facts, r)
                    print(name, r.get("rep", ""), col, scene(r), r["text"], "｜前：", r["before"] or "", "｜外れ：", bad,
                          "｜読み：", value, "○" if value in acc else "×", "｜流れの制御の動き：", kind or "同じ")


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
    場面（取り次ぎのあと・後任の方）は今の組。外れは、流れの制御の動きが変わるもの（flow_kind）と、項目だけのものを分けて、場面ごとに数える。
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
            if not g.facts_whole(got, facts):
                none[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or "", "答え無し"))
                continue
            kind = flow_kind(got, facts, jr)
            if moves(kind):
                moved[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or "", kind,
                            {x: (got.get(x), sorted(v)) for x, v in facts.items() if got.get(x) not in v}))
            elif kind in UNCOUNTED:
                out.append((sc, who, jr["text"], jr["before"] or "", kind))
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
    制御の動き（same_moves＝どの報告でも同じ動き）がそろえばその読み、そろわなければ今の組（a・b の両方を待ってから出す）。外れは流れの制御の動きで数え、2秒を超えた回は、
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
                if g.facts_whole(a_got, facts) and g.facts_whole(b_got, facts) and same_moves(a_got, b_got, jr):
                    who, got, s = "そろい", a_got, both
                else:
                    who, got, s = "今の組（食い違い）", g_got, both + g_s
            by[who] += 1
            if s > 2.0:
                late["ご本人の回" if TARGET_IS_THERE in acc else "ほかの回"] += 1
            kind = flow_kind(got, facts, jr)
            if moves(kind):
                moved[f"{who}・{sc}"] += 1
                out.append((sc, who, jr["text"], jr["before"] or "", kind))
            elif kind in UNCOUNTED:
                out.append((sc, who, jr["text"], jr["before"] or "", kind))
            elif not g.facts_ok(got, facts):
                only[f"{who}・{sc}"] += 1
    n = sum(by.values())
    group_n = by["今の組（場面）"] + by["今の組（食い違い）"]
    print("決めた判定：", dict(by), f"｜今の組を出した回 {group_n}/{n}（{group_n / n:.0%}）")
    print("流れの制御の動きが変わる外れ：", dict(moved) or 0, "｜項目だけの外れ：", dict(only) or 0)
    print("2秒超：", dict(late) or 0)
    for x in out:
        print("  ", x)


PARTS = (("目的のお客様の判定", ""), ("尋ねているか", "_asks"))   # 判定の問い合わせ（行の欄の後ろに付く名前）
EPS = 1e-6


def new_person(r: dict) -> bool:
    """行の場面の電話口の方が、まだあいさつを聞いていないかもしれない新しい方か（本番は、その方の報告が繋ぐ報告なら、判定を待つ間にあいさつを流す＝
    Flow.hold_play）：取り次ぎの言葉など前の言葉のあとの方で、確かめる一言（その方に伺う一言）への返事ではない。名乗りの頭であいさつを聞いた方
    （呼ぶ一言への返事）・確かめる一言への返事は、もうあいさつを聞いた方。"""
    return bool(r["before"]) and not r["asked"]


def waits(new: bool) -> tuple[float, ...]:
    """本番の会話の側が判定を待つ長さのありうる値（待つ長さの1か所 Limits.judge_wait_s）：待つ間に録音を流さない回と、新しい方（new＝new_person）なら
    あいさつ（Flow.hold_play の greeting）を流す回（声の型ごとの録音の長さ＝本番の電話接続側と同じ convo.recording_seconds）。あいさつを流すかは、その
    方への会話AIの報告（繋ぐ報告か）でも決まり、測りは持たない＝記録の無い判定の届き方と同じく、どの待つ長さでも同じ判定で決まる行だけを決めたと数える。
    ⛔ 2026-11-06 日本時間13時台の見直し（判定の組の持ち場の A4）：Limits.hold_s だけで数えていた"""
    lim = Limits()
    if not new:
        return (lim.judge_wait_s(),)
    from convo import recording_seconds
    from instruction import VOICE_TYPES
    return tuple(sorted({lim.judge_wait_s()} | {lim.judge_wait_s(recording_seconds(v, "greeting")) for v in VOICE_TYPES}))


# 記録の無い判定（測っていない・まだ流していない）の届き方：すぐに使える答え・すぐに使えない答え・届かない・待つ長さ（その行の waits のどれも）の前後の
# 使える答え。どの届き方でも同じ判定で決まる行だけを「決めた」と数える（記録の無い判定の読みを推し量らない）
def missing_tries(ws: tuple[float, ...]) -> tuple:
    return ((EPS, True), (EPS, False), (float("inf"), False), *((w + d, True) for w in ws for d in (-EPS, EPS)))


def judge_of(rows: list[dict], col: str) -> str:
    """1つの判定で測った記録（g59_calling_judge の --judge2 に1つの判定）の、判定の名前（答えた名前 _by・確かでない読み・失敗の _tried の
    model）。2つ以上の名前が出る記録（組で測った記録）は、判定ごとの秒が残っていないので使えない。"""
    names = set()
    for r in rows:
        for _, sfx in PARTS:
            if r.get(f"{col}{sfx}_by"):
                names.add(r[f"{col}{sfx}_by"])
            names |= {t["model"] for t in r.get(f"{col}{sfx}_tried") or [] if t.get("model")}
    if len(names) != 1:
        raise SystemExit(f"1つの判定だけで測った記録を渡してください（この記録の判定：{sorted(names)}）")
    return names.pop()


def record(r: dict, col: str, sfx: str, name: str, q):
    """1行の、1つの判定の記録：(答え・失敗が届くまでの秒, 使える答えか, 答え)。使える答え＝その判定が答え、確かな読み（本番と同じ
    meaning_judge.sure_reading＝線は今の値。記録したときの線より下げた線では、記録に答えが残っていないので数えられない）。秒の無い行（その問い合わせを
    測っていない・まとめて送る測り）・答えの形の無い古い記録は None（記録が無い）。"""
    from types import SimpleNamespace

    from meaning_judge import sure_reading
    s = r.get(f"{col}{sfx}_s")
    if s is None:
        return None
    if r.get(f"{col}{sfx}_by") != name:
        return s, False, None
    got = r.get(f"{col}_facts") if sfx == "" else r.get(f"{col}_asks")
    if got is None:
        return None
    return s, sure_reading(name, q, SimpleNamespace(sure=r.get(f"{col}{sfx}_sure"), firm_p=None)), got


def outcomes(runs: list, leads, new: bool = False) -> set:
    """記録の秒で、本番の組が決める (番号, 秒, 待つ長さ) の全部：会話の側の待つ長さ（waits(new) のどれも。new＝行の電話口の方が新しい方か＝new_person）と、
    記録の無い判定 None の届き方（missing_tries のどれも）のすべての組み合わせで動かす（待つ長さが切れる時に組へ合図＝本番の release と同じく、組が
    後ろの順の確かな読みを持つ長さ prefer_s）。"""
    from itertools import product

    from meaning_judge import race_recorded
    missing = [i for i, x in enumerate(runs) if x is None]
    outs = set()
    ws = waits(new)
    for wait in ws:
        for tries in product(missing_tries(ws), repeat=len(missing)):
            rs = list(runs)
            for i, x in zip(missing, tries):
                rs[i] = x
            won, at, _ = race_recorded([x[:2] for x in rs], leads, prefer_s=wait)
            outs.add((won, at, wait))
    return outs


def cascade(stages: list[str]) -> dict:
    """本番の組（meaning_judge.RACE）で、どの判定で決めるかと外れを数える（0円）：stages＝判定ごとの記録（「ファイル名:列」。列の既定は judge2。
    1つの判定で測った記録＝判定の名前は記録から読む）。お呼びする段の2つの問い合わせ（目的のお客様の判定・尋ねているか）ごとに、組の顔ぶれ
    （members_for）と先に出すもの（leads_of）を本番の1か所から作り、判定ごとの記録の秒と確かな読みを、本番の決め方（Race）に記録の秒で通す
    （race_recorded）。記録の無い判定で決まりうる行は、決めたとは数えない：どの届き方でも記録の無い判定（測っていない Sonnet など）か「どれも答えない」
    になる行は「記録の無い判定に回る」、記録のある判定とない判定のどちらでも決まりうる行は「記録が足りない」（流す判定の名前つき＝次の測りの
    --only の言葉）。決めた行の外れは、流れの制御の動きが変わるもの（2つの問い合わせとも flow_kind の1か所＝目的のお客様の判定の読み・尋ねているかの読みを
    本番と同じ入口で渡し、会話AIの報告ごとに正しい読みの動きと比べる。種類＝外れの種類）と、項目だけのもの（目的のお客様の判定だけ）に分ける。
    ⛔ 4bc94b4d まで、尋ねているかの外れは値だけで数えていた（「尋ねている」かが正解と違う＝まとめ役 CL-1 の頼み rv178/X の4回目 (2)）。2秒超＝決めた秒が会話の側の待つ長さ（その回の待つ長さ＝waits）を超える（記録の無い判定の届き方によっては超える行も数える）。
    待つ長さ（あいさつの録音を流す回か＝waits）で決める判定が変わる行は「待つ長さで変わる」（どちらの判定の読みも外れを数える＝外れを隠さない）。
    ⛔ 2026-11-06 日本時間8時台（EN-C の指摘8）：線を手で渡し、並べた順で線から上かだけを見ていた（届いた秒を見ない＝本番の組と違う判定で決めたと
    数えうる）・どれも線より下なら、組で測った記録（今の組）の答えを使っていた"""
    from meaning_judge import RACE, leads_of, members_for, question_kind
    table = current()
    have: dict[str, dict] = {}                         # 判定の名前 ⇒ {(場面の鍵, 回): (行, 列)}
    for spec in stages:
        name, c = source(spec, "judge2")
        rows = [r for r in rows_of(name) if key(r) in table]
        who = judge_of(rows, c)
        if who not in RACE:
            raise SystemExit(f"組（meaning_judge.RACE）に入っていない判定の記録です：{who}")
        have.setdefault(who, {}).update({(key(r), r.get("rep", 0)): (r, c) for r in rows})
    by, moved, only, late, rest, kinds, unbuilt = (collections.Counter() for _ in range(7))
    short, out, split = [], [], []
    for k, rep in sorted({kr for h in have.values() for kr in h}, key=str):
        acc, facts = table[k]
        sc_row = next(h[(k, rep)][0] for h in have.values() if (k, rep) in h)
        shown = g.judge_questions(sc_row)   # 判定に見せる質問は、行の場面の本番の流れの制御の1か所（Flow._calling_asked）
        sc = scene(sc_row)
        for part, sfx in PARTS:
            q = shown[0] if sfx == "" else shown[1]
            names = members_for(RACE, question_kind(q))
            runs = []
            for n in names:
                rc = have.get(n, {}).get((k, rep))
                runs.append(record(*rc, sfx, n, q) if rc else None)
            if all(x is None for x in runs):
                continue                               # この問い合わせは、どの判定でも測っていない
            outs = outcomes(runs, leads_of(names), new_person(sc_row))
            winners = {w for w, _, _ in outs}
            known = {i for i, x in enumerate(runs) if x is not None}
            label = lambda ws: "・".join("どれも答えない" if w is None else names[w] for w in sorted(ws, key=lambda w: (w is None, w)))

            def miss(w) -> None:
                """記録のある判定 w で決めたときの外れ（流れの制御の動きが変わるもの・項目だけのもの）を数える。"""
                who, got = names[w], runs[w][2]
                kind = flow_kind(got, facts, sc_row) if sfx == "" else flow_kind(None, facts, sc_row, asks=got)
                if moves(kind):
                    moved[(part, who, sc)] += 1
                    kinds[(part, kind.split("：")[0])] += 1
                    out.append((part, sc, who, sc_row["text"], sc_row["before"] or "", got, kind))
                elif kind in UNCOUNTED:
                    unbuilt[(part, who, sc, kind)] += 1
                    if sfx == "" and not g.facts_ok(got, facts):
                        out.append((part, sc, who, sc_row["text"], sc_row["before"] or "", got, f"{kind}・項目の外れ"))
                elif sfx == "" and not g.facts_ok(got, facts):
                    only[(part, who, sc)] += 1
            if winners <= known:
                if any(at > wait for _, at, wait in outs):
                    late[(part, "ご本人の回" if TARGET_IS_THERE in acc else "ほかの回")] += 1
                if len(winners) == 1:
                    by[(part, names[next(iter(winners))])] += 1
                else:
                    split.append((part, sc, sc_row["text"], sc_row["before"] or "", label(winners)))
                for w in sorted(winners):
                    miss(w)
            elif not winners & known:
                rest[(part, label(winners), sc)] += 1
            else:
                need = [names[w] for w in winners if w is not None and w not in known]
                short.append((part, sc, sc_row["text"], sc_row["before"] or "", rep, need))
    return {"決めた判定": by, "動きが変わる外れ": moved, "項目だけの外れ": only, "2秒超": late, "記録の無い判定に回る": rest,
            "記録が足りない": short, "待つ長さで変わる": split, "外れの行": out, "外れの種類": kinds, "比べられない": unbuilt}


def show_cascade(got: dict) -> None:
    n = sum(got["決めた判定"].values())
    print("決めた判定：", dict(got["決めた判定"]), f"｜決めた行 {n}")
    print("流れの制御の動きが変わる外れ：", dict(got["動きが変わる外れ"]) or 0, "｜項目だけの外れ：", dict(got["項目だけの外れ"]) or 0)
    print("動きが変わる外れの種類：", dict(got["外れの種類"]) or 0, "｜動きを比べられない行（数えない）：", dict(got["比べられない"]) or 0)
    print("2秒超：", dict(got["2秒超"]) or 0)
    print("記録の無い判定に回る（どれで決まるか）：", dict(got["記録の無い判定に回る"]) or 0)
    print("記録が足りない（決まらない・流す判定）：", len(got["記録が足りない"]))
    for x in got["記録が足りない"]:
        print("  ", x)
    texts = sorted({x[2] for x in got["記録が足りない"]})
    if texts:
        print("記録が足りない言葉（次の測りの --only）：", json.dumps(texts, ensure_ascii=False))
    print("待つ長さ（あいさつの録音を流す回か）で決める判定が変わる行：", len(got["待つ長さで変わる"]))
    for x in got["待つ長さで変わる"]:
        print("  ", x)
    for x in got["外れの行"]:
        print("  外れ：", x)


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
    c.add_argument("--stage", action="append", required=True,
                   help="判定ごとの記録（「ファイル名:列」。1つの判定で測ったもの。列の既定は judge2。判定の名前・線・組の顔ぶれは本番の1か所から。いくつでも）")
    a = p.parse_args()
    if a.cmd == "misses":
        misses(a.names)
    elif a.cmd == "plan":
        plan(a.cheap.split(","), a.jev.split(","), a.group, a.line)
    elif a.cmd == "cascade":
        show_cascade(cascade(a.stage))
    else:
        agree(a.a.split(","), a.b.split(","), a.jev.split(","), a.group, a.line, set(filter(None, a.sonnet_scenes.split(","))))


if __name__ == "__main__":
    main()
