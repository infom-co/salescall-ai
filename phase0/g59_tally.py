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
import json
from pathlib import Path

import g59_calling_judge as g
from flowctl import END, END_REASON, HANDOVER, TARGET_IS_NOT, TARGET_IS_THERE, UNDECIDED, Limits, facts_reading

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
    """項目ごとの答え f で、行 r の場面の流れの制御がどう動くか（読み・不在・電話を代わった方・後任の方の印・電話を終える言葉か）。2つの答えの印が
    同じなら、流れの制御の動きも同じ（flow_ok と、判定どうしのそろいを数える agree の両方が使う1か所）。電話を代わった方の印は、流れの制御が
    使うときだけ持つ（Flow._new_speaker：確かめる一言を聞いた方の返事・取り次ぎの言葉のあとの言葉では使わない＝g.HELD_BEFORE）。
    電話を終える言葉か（END）：判定が電話を終える言葉（締める・急いでいる方への伺い・お礼の締め・確かめの一言・お呼びし直し）か「決められない」と
    読めば、流れの制御は会話AIのどの報告より先に、その読みで次の動きを決める（Flow._end_due_now。お呼びする段では _topic_first は働かない）
    ＝(END, その読み) だけを返す（その発話の目的のお客様の読みは、その一手に効かない）。「言っていない」・項目を足す前の記録（None）は、目的の
    お客様の読みで決まる。⛔ 2026-11-05 に足した：11-01 に項目を足したとき、答えの段の数え（いまの g53_meaning.flow_miss の中）には入れたが、ここには入れず、
    この項目の外れを「項目だけ」と数えていた。
    こちらの声が届いていない様子か（HEAR）は、電話が切れたときの記録（Flow.cut）だけを変える＝ここでは数えない（g59_calling_judge が別に数える）。
    尋ねているか（別の問い合わせ asks）は、流れの制御がほかのどの読みより先に当てる（Flow._asking_now：お客様の質問に先に答える）が、ここでは
    数えず、g59_calling_judge の「尋ねているか」の数えに任せる＝質問の発話で目的のお客様・電話を終える読みが外れても、ここでは外れと数える
    （本当は動きが変わらない＝多めに数える側。外れを見逃す側ではない）。"""
    end = f.get(END)
    if end in END_REASON or end == UNDECIDED:
        return END, end
    used = not r["asked"] and not set((r["before"] or "").split("／")) & g.HELD_BEFORE
    value, away, new, succ = facts_reading(f, r["successor"])
    if not used and value == HANDOVER:   # 印を使わない所では「代わった人が話し始めた」＝「別の方」と同じ動き（2026-10-30 に会話の試しで確かめた）
        value = TARGET_IS_NOT
    # 「決められない」の読みは、必ず確かめに回り、確かめたあとに、あいさつと名乗りから流す＝電話を代わった方の印は動きを変えない（同じ日に確かめた）
    return value, away, new if used and value != UNDECIDED else None, succ


def flow_ok(got: dict, facts: dict, acc: list, r: dict) -> bool:
    """流れの制御の動きが正解と同じか：組み合わせた読み（flowctl.facts_reading）が受け取ってよい読みに入り、印（不在・電話を代わった方・後任の方）も
    正解の組み合わせのどれかと同じ（flow_flags）。項目だけの外れ（読みも印も変わらない）は、流れの制御の動きを変えない。電話を終える言葉か（END）が
    読みに無い記録（項目を足す前）は、その項目を数えない（g59_calling_judge.facts_ok と同じ決まり）。"""
    from itertools import product
    facts = {k: v for k, v in facts.items() if k != END or END in got}
    used = not r["asked"] and not set((r["before"] or "").split("／")) & g.HELD_BEFORE
    keys = list(facts)
    flags = [flow_flags(dict(zip(keys, c)), r) for c in product(*(facts[k] for k in keys))]
    mine = flow_flags(got, r)
    if mine[0] == END:   # 電話を終える読み（決められないも）が次の動きを決める：正解の組み合わせのどれかと同じ読みか
        return mine in flags
    want = {f[1:] for f in flags if f[0] != END}
    value, *rest = mine
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
                value = facts_reading(got, r["successor"])[0] if g.facts_whole(got, facts) else None
                if bad or value not in acc:
                    moved = not (g.facts_whole(got, facts) and flow_ok(got, facts, acc, r))
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
            if not g.facts_whole(got, facts):
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
                if g.facts_whole(a_got, facts) and g.facts_whole(b_got, facts) and flow_flags(a_got, jr) == flow_flags(b_got, jr):
                    who, got, s = "そろい", a_got, both
                else:
                    who, got, s = "今の組（食い違い）", g_got, both + g_s
            by[who] += 1
            if s > 2.0:
                late["ご本人の回" if TARGET_IS_THERE in acc else "ほかの回"] += 1
            if not g.facts_whole(got, facts) or not flow_ok(got, facts, acc, jr):
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


PARTS = (("目的のお客様の判定", ""), ("尋ねているか", "_asks"))   # 判定の問い合わせ（行の欄の後ろに付く名前）
EPS = 1e-6
# 記録の無い判定（測っていない・まだ流していない）の届き方：すぐに使える答え・すぐに使えない答え・届かない・待つ長さの前後の使える答え。
# どの届き方でも同じ判定で決まる行だけを「決めた」と数える（記録の無い判定の読みを推し量らない）
MISSING_TRIES = ((EPS, True), (EPS, False), (float("inf"), False), (Limits.hold_s - EPS, True), (Limits.hold_s + EPS, True))


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


def outcomes(runs: list, leads) -> set:
    """記録の秒で、本番の組が決める (番号, 秒) の全部（記録の無い判定 None は、MISSING_TRIES のどの届き方でも動かす）。"""
    from itertools import product

    from meaning_judge import race_recorded
    missing = [i for i, x in enumerate(runs) if x is None]
    outs = set()
    for tries in product(MISSING_TRIES, repeat=len(missing)):
        rs = list(runs)
        for i, x in zip(missing, tries):
            rs[i] = x
        won, at, _ = race_recorded([x[:2] for x in rs], leads)
        outs.add((won, at))
    return outs


def cascade(stages: list[str]) -> dict:
    """本番の組（meaning_judge.RACE）で、どの判定で決めるかと外れを数える（0円）：stages＝判定ごとの記録（「ファイル名:列」。列の既定は judge2。
    1つの判定で測った記録＝判定の名前は記録から読む）。お呼びする段の2つの問い合わせ（目的のお客様の判定・尋ねているか）ごとに、組の顔ぶれ
    （members_for）と先に出すもの（leads_of）を本番の1か所から作り、判定ごとの記録の秒と確かな読みを、本番の決め方（Race）に記録の秒で通す
    （race_recorded）。記録の無い判定で決まりうる行は、決めたとは数えない：どの届き方でも記録の無い判定（測っていない Sonnet など）か「どれも答えない」
    になる行は「記録の無い判定に回る」、記録のある判定とない判定のどちらでも決まりうる行は「記録が足りない」（流す判定の名前つき＝次の測りの
    --only の言葉）。決めた行の外れは、流れの制御の動きが変わるもの（目的のお客様の判定＝flow_ok・尋ねているか＝「尋ねている」かが正解と違う）と、
    項目だけのものに分ける。2秒超＝決めた秒が会話の側の待つ長さ（Limits.hold_s）を超える（記録の無い判定の届き方によっては超える行も数える）。
    ⛔ 2026-11-06 日本時間8時台（EN-C の指摘8）：線を手で渡し、並べた順で線から上かだけを見ていた（届いた秒を見ない＝本番の組と違う判定で決めたと
    数えうる）・どれも線より下なら、組で測った記録（今の組）の答えを使っていた"""
    from flowctl import ASKED
    from meaning_judge import RACE, leads_of, members_for, question_kind
    table = current()
    _, f, fs = g.flows()
    have: dict[str, dict] = {}                         # 判定の名前 ⇒ {(場面の鍵, 回): (行, 列)}
    for spec in stages:
        name, c = source(spec, "judge2")
        rows = [r for r in rows_of(name) if key(r) in table]
        who = judge_of(rows, c)
        if who not in RACE:
            raise SystemExit(f"組（meaning_judge.RACE）に入っていない判定の記録です：{who}")
        have.setdefault(who, {}).update({(key(r), r.get("rep", 0)): (r, c) for r in rows})
    by, moved, only, late, rest = (collections.Counter() for _ in range(5))
    short, out = [], []
    for k, rep in sorted({kr for h in have.values() for kr in h}, key=str):
        acc, facts = table[k]
        sc_row = next(h[(k, rep)][0] for h in have.values() if (k, rep) in h)
        fl = fs if sc_row["successor"] else f
        target_q = g.question_of(fl, sc_row["asked"])
        sc = scene(sc_row)
        for part, sfx in PARTS:
            q = target_q if sfx == "" else fl.for_judge(fl.asks_questions[target_q.id])
            names = members_for(RACE, question_kind(q))
            runs = []
            for n in names:
                rc = have.get(n, {}).get((k, rep))
                runs.append(record(*rc, sfx, n, q) if rc else None)
            if all(x is None for x in runs):
                continue                               # この問い合わせは、どの判定でも測っていない
            outs = outcomes(runs, leads_of(names))
            winners = {w for w, _ in outs}
            known = {i for i, x in enumerate(runs) if x is not None}
            label = lambda ws: "・".join("どれも答えない" if w is None else names[w] for w in sorted(ws, key=lambda w: (w is None, w)))
            if len(winners) == 1 and (w := next(iter(winners))) in known:
                who, got = names[w], runs[w][2]
                by[(part, who)] += 1
                if max(at for _, at in outs) > Limits.hold_s:
                    late[(part, "ご本人の回" if TARGET_IS_THERE in acc else "ほかの回")] += 1
                if sfx == "":
                    if not g.facts_whole(got, facts) or not flow_ok(got, facts, acc, sc_row):
                        moved[(part, who, sc)] += 1
                        out.append((part, sc, who, sc_row["text"], sc_row["before"] or "", got))
                    elif not g.facts_ok(got, facts):
                        only[(part, who, sc)] += 1
                elif sc_row.get("asks_accept") is not None and (got in ASKED) != sc_row["asks_accept"]:
                    moved[(part, who, sc)] += 1
                    out.append((part, sc, who, sc_row["text"], sc_row["before"] or "", got))
            elif not winners & known:
                rest[(part, label(winners), sc)] += 1
            else:
                need = [names[w] for w in winners if w is not None and w not in known]
                short.append((part, sc, sc_row["text"], sc_row["before"] or "", rep, need))
    return {"決めた判定": by, "動きが変わる外れ": moved, "項目だけの外れ": only, "2秒超": late, "記録の無い判定に回る": rest,
            "記録が足りない": short, "外れの行": out}


def show_cascade(got: dict) -> None:
    n = sum(got["決めた判定"].values())
    print("決めた判定：", dict(got["決めた判定"]), f"｜決めた行 {n}")
    print("流れの制御の動きが変わる外れ：", dict(got["動きが変わる外れ"]) or 0, "｜項目だけの外れ：", dict(got["項目だけの外れ"]) or 0)
    print("2秒超：", dict(got["2秒超"]) or 0)
    print("記録の無い判定に回る（どれで決まるか）：", dict(got["記録の無い判定に回る"]) or 0)
    print("記録が足りない（決まらない・流す判定）：", len(got["記録が足りない"]))
    for x in got["記録が足りない"]:
        print("  ", x)
    texts = sorted({x[2] for x in got["記録が足りない"]})
    if texts:
        print("記録が足りない言葉（次の測りの --only）：", json.dumps(texts, ensure_ascii=False))
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
