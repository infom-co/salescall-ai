"""依頼者からの解決策を、その業務の想定問答に足す（(h)。1か所。持ち主 CL-2。DESIGN.md 3-8-3。データベースは 0184）。

✅ 2026-11-02 5時56分 ユーザー：「依頼者から、解決策の連絡がフィードバックされたら、こちらのトークスクリプトに追加する。」・21時37分の流れ：
(1) 入口は2つ（契約者が画面に書く・依頼者が書き込んで返した報告のファイルを取り込む）で、どちらも `add` の1か所を通る。
(2) 問いの名前は AI が決める（今ある想定問答と比べ、同じ問いならその答えを置き換え、新しい問いなら足す。人は確かめない）。
(3) 取込と同じ確かめ（core/business_import.review＝名前と読み・形）を通して、業務設定の新しい版を作る（business_settings.derive。
    きっかけ solution）。録音がそろったら、版を自動で替える1か所（business_settings.adopt_ready）が実施回に割り当てる（電話は止めない）。
(4) どの点にどの解決策を足したかを残す（表 client_solutions。画面＝CL-3 が読む）。
点＝通話の報告の「ご納得いただけなかった点」（not_agreed）のうち、ご説明できなかったもの（explained が偽＝担当の者に申し伝えた点。
CL-1 の連絡帳 4-66 のあとの (h)）。確かめに通らない（検証モードで、AI が決めた名前の読みを契約者がまだ確かめていない など）ときは、版を
作らずに理由を残して契約者に知らせる（契約者が確かめてから、confirmed_names を付けて `add` をもう一度呼ぶ）。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import business_import, business_settings, call_report, notify
from salescall.core.access import Tx
from salescall.core.readings import NameFinder

ORIGINS = ("dashboard", "report_file")   # 0184 の CHECK と同じ
# 問いの名前を決める AI（CL-1 の決め方。ご納得いただけなかった点・解決策の文・今ある想定問答の (問いの名前, 答えの文) の並び →
# 問いの名前。今ある問いと同じ問いなら、その名前をそのまま返す＝置き換え。新しい問いなら新しい名前）
AnswerNamer = Callable[[str, str, list[tuple[str, str]]], str]


class SolutionInvalid(ValueError):
    """その解決策は受けられない（点が無い・ご説明できた点・文が空 など。理由は日本語）。何も書かない。"""


@dataclass(frozen=True)
class Line:
    """解決策を足した、業務の版の流れ1つ（0189）。base＝足す元にした版（流れのいちばん後の版）・version_id＝足した版（中身が同じなら
    base のまま＝新しい版は作らない。無い＝確かめに通らなかった）・replaced＝同じ問いの答えを置き換えた・problems＝通らなかった理由。"""
    base: UUID
    version_id: UUID | None
    replaced: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Added:
    solution_id: UUID
    answer_name: str
    lines: tuple[Line, ...]          # 先頭は、解決策を求めた通話の実施回の流れ

    @property
    def version_id(self) -> UUID | None:
        return self.lines[0].version_id

    @property
    def replaced(self) -> bool:
        return self.lines[0].replaced

    @property
    def problems(self) -> tuple[str, ...]:
        return self.lines[0].problems


def _point_text(n: call_report.NotAgreed) -> str:
    return (n.point or n.said or "").strip()


def points(tx: Tx, attempt_id: UUID) -> list[dict[str, Any]]:
    """その通話の、解決策を求める点（ご説明できなかった「ご納得いただけなかった点」）：[{index, point}]（index＝報告の not_agreed の
    何番目か）。読むのはここだけ（画面が点を並べる・`add` が点を確かめる）。同じ行の前の通話で、同じ質問の同じ点をもう求めていれば
    入れない（⛔ 2026-11-06 の見直し (C) の2：申し伝える一言まで言って切れた電話の続きで申し伝え直した点＝CL-1 の連絡帳 4-99 の
    outcome unresolved・told が、前の通話と2回、解決策を求める点になった。点は文字の形をそろえて比べる＝readings の NFKC と同じ）。"""
    report = call_report.load(tx, attempt_id)
    if report is None:
        return []
    asked = set()
    for e in tx.all("SELECT e.attempt_id FROM salescall.attempts a JOIN salescall.attempts e ON e.task_id = a.task_id"
                    " WHERE a.attempt_id = %s AND (e.claimed_at, e.attempt_id) < (a.claimed_at, a.attempt_id)", (attempt_id,)):
        before = call_report.load(tx, e["attempt_id"])
        asked |= {(n.question, unicodedata.normalize("NFKC", _point_text(n))) for n in (before.not_agreed if before else ())
                  if n.explained is False and _point_text(n)}
    return [{"index": i, "point": _point_text(n)} for i, n in enumerate(report.not_agreed)
            if n.explained is False and _point_text(n)
            and (n.question, unicodedata.normalize("NFKC", _point_text(n))) not in asked]


def _lines(tx: Tx, business_id: UUID, first: UUID) -> list[UUID]:
    """解決策を足す、その業務の版の流れ（それぞれのつながりのいちばん後の版。同じ版は1つ）：まだ終えていない行のある実施回の今の版
    すべてと、解決策を求めた通話の実施回の今の版（先頭）。✅ 2026-11-06 ユーザー「業務のすべての実施回」（見直し(B) の3）。"""
    tips = [business_settings.successor_tip(tx, first)]
    for v in business_settings.in_use_versions(tx, business_id):   # 使っている版を決めるのは業務設定の1か所
        tip = business_settings.successor_tip(tx, v)
        if tip not in tips:
            tips.append(tip)
    return tips


def add(tx: Tx, attempt_id: UUID, point_index: int, solution: str, *, origin: str, name_answer: AnswerNamer,
        find_names: NameFinder | None = None, confirmed_names: Any = ()) -> Added:
    """依頼者からの解決策を、その業務の想定問答に足す（1か所。上の説明）。足すのは、その業務のまだ終えていない実施回の版の流れすべて
    （`_lines`。それぞれのつながりの先＝business_settings.derive が決める）。問いの名前は、解決策を求めた通話の実施回の流れの今の答えと
    比べて AI が1回決め、どの流れにも同じ名前で置く。流れごとに取込と同じ確かめを通し（重ねる標準は、その版の標準のまま＝標準を替える
    のは derive_for_standard の1か所。見直し(B) の2）、中身が変わらなければ新しい版を作らない（見直し(B) の8）。足したかどうか
    （流れごとの版・確かめに通らなかった理由）を記録して返す。"""
    if origin not in ORIGINS:
        raise ValueError(f"入口は {list(ORIGINS)} のどれかです（{origin!r}）")
    text = solution.strip() if isinstance(solution, str) else ""
    if not text:
        raise SolutionInvalid("解決策（お客様へのご説明の文）が空です")
    point = next((p for p in points(tx, attempt_id) if p["index"] == point_index), None)
    if point is None:
        raise SolutionInvalid("その通話に、解決策を求める点（ご説明できなかった、ご納得いただけなかった点）がありません")
    row = tx.one("SELECT a.task_id, t.run_id, r.business_id, salescall.run_current_version(t.run_id) AS version_id"
                 " FROM salescall.attempts a JOIN salescall.tasks t ON t.task_id = a.task_id"
                 " JOIN salescall.runs r ON r.run_id = t.run_id WHERE a.attempt_id = %s", (attempt_id,))
    if row is None or row["version_id"] is None:
        raise SolutionInvalid("その通話の業務の版がありません")
    tips = _lines(tx, row["business_id"], row["version_id"])
    name = (name_answer(point["point"], text, list(business_settings.load(tx, tips[0]).answers)) or "").strip()
    if not name:
        raise SolutionInvalid("問いの名前を決められませんでした")
    sid = tx.one(
        "INSERT INTO salescall.client_solutions (tenant_id, client_id, attempt_id, task_id, point_index, point_text, solution_text,"
        "                                        answer_name, origin, recorded_by)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
        " RETURNING solution_id",
        (attempt_id, row["task_id"], point_index, point["point"], text, name, origin))["solution_id"]
    note = f"ご納得いただけなかった点「{point['point']}」への解決策（問い「{name}」）"
    lines = [_add_line(tx, sid, attempt_id, row["business_id"], tip, name, text, note, find_names=find_names,
                       confirmed_names=confirmed_names) for tip in tips]
    for line in lines:
        # 戻す前に足した解決策が入っていない流れ（手で前の版に戻した流れ など）：自動では入れず、契約者にダッシュボードで確かめていただく
        # （✅ 2026-11-06 ユーザー＝計画書 6-1 (3)：「契約者にそれをダッシュボードで確認します。」。一覧は `not_in_line`・足す入口は `add_earlier`）。
        # いま足した解決策そのものは出さない（確かめに通らなかった流れは solution_held で知らせる。⛔ 2026-11-07 の第三者の見直し (2) の4）
        earlier = [e for e in not_in_line(tx, line.version_id or line.base) if e["solution_id"] != sid]
        if earlier:
            notify.emit(tx, "solution_earlier", key=f"solution_earlier:{sid}:{line.base}",
                        summary=f"依頼者の解決策（問い「{name}」）を足した業務の版の流れに、入っていない前の解決策が {len(earlier)} 件あります"
                                "（前の版に戻す前に足したもの など）。入れるものをダッシュボードで選んでください（自動では入れません）。",
                        refs={"solution_id": str(sid), "version_id": str(line.version_id or line.base),
                              "earlier": [str(e["solution_id"]) for e in earlier]})
    return Added(sid, name, tuple(lines))


def _add_line(tx: Tx, sid: UUID, attempt_id: UUID, business_id: UUID, version: UUID, name: str, text: str, note: str, *,
              find_names: NameFinder | None, confirmed_names: Any, chosen: bool = False) -> Line:
    """業務の版の流れ1つ（version＝その流れの版。足す元は、そのつながりのいちばん後の版＝`business_settings.successor_tip`＝データベースの
    salescall.version_tip の1か所。derive も同じ1か所で決める）に、解決策の問答を足す（1か所。`add` と、契約者が前の解決策を選んで足す
    `add_earlier` の両方が通る）：取込と同じ確かめ（business_import.review。重ねる標準は、その版の標準のまま）を通し、通れば業務設定の版を
    足す（business_settings.derive。きっかけ solution）。中身が変わらなければ版を作らない。流れの結果を記録し（chosen＝契約者が選んで足した＝
    選んだ方を残す。0194）、通らなければ契約者に知らせる。
    名前（✅ 2026-11-07 の第三者の見直し (2) の1）：AI に名前を探させるのは、足す問答の文だけ。元の版にある名前と読みは、版のまま変えない
    （review の placed_names。⛔ 前は台本全体の名前を探し直したので、版の名前の一覧が増え、前からある名前の AI の読みも変わりえて、録音を
    引き継げなかった）。新しい版の中身は、derive が渡す元の版の中身から作る（⛔ 前は先に作った中身を渡し、元の版の中身を捨てていた）。"""
    tip = business_settings.successor_tip(tx, version)
    now = business_settings.load(tx, tip)
    replaced = name in {a for a, _ in now.answers}
    made, problems = tip, ()
    if business_settings.with_answer(now.raw, name, text) != now.raw:
        ai = business_import.ask_ai({"answers": [{"name": name, "text": text}]}, find_names=find_names)   # 足す文の名前だけ（1回）

        def change(old: dict[str, Any]) -> dict[str, Any]:
            raw = business_settings.with_answer(old, name, text)
            p = business_import.review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, standard_id=now.standard_id,
                                       placed_names=old.get("names"))
            if p.problems or p.raw is None:
                raise business_settings.SettingsInvalid(list(p.problems) or ["確かめに通りませんでした"])
            # 変えるのは想定問答と、足す文の名前だけ（確かめ review が形を整える項目＝質問の AI の答えの欄 など は、元の版のまま）
            return dict(raw, names=p.raw["names"])
        try:
            with tx.savepoint():
                made = business_settings.derive(tx, tip, change, cause="solution", note=note)
        except business_settings.SettingsInvalid as e:
            made, problems = None, tuple(e.problems) or ("確かめに通りませんでした",)
    tx.execute("INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced,"
               "                                             problems, chosen_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, CASE WHEN %s THEN salescall.cur_actor() END)",
               (sid, tip, made, replaced, list(problems), chosen))
    if made is None:
        notify.emit(tx, "solution_held", key=f"solution_held:{sid}:{tip}",
                    summary=f"依頼者の解決策（問い「{name}」）を、確かめに通らなかったので想定問答に足していません："
                            + "／".join(problems), refs={"solution_id": str(sid), "attempt_id": str(attempt_id),
                                                        "version_id": str(tip)})
    return Line(tip, made, replaced, problems)


def _latest(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """その業務に、依頼者の解決策で足せた問答（問いの名前ごとに、いちばん新しく足せたもの。1か所＝取り込み直したときに残す `kept_answers` と、
    流れに入っていない前の解決策の一覧 `not_in_line` が読む）。いちばん新しいものが、取り込み直した中身で置き換えられていれば（`replaced`）、
    その問いの名前は入れない（前の解決策も、よみがえらせない。⛔ 2026-11-07 の第三者の見直し (2) の4）。
    removed＝契約者が手で外した解決策か（`removed_by_hand` が残し、そのあと契約者が選んで足していない＝流れの結果の時刻で比べる）。手で外した
    解決策は、取り込み直しで入れず（`keep_in`）、流れに入っていない前の解決策の一覧には出す（`not_in_line`。✅ 2026-11-07 ユーザー
    「契約者が確かめる」＝計画書 6-1 の (3) が (2) より先）。
    unconfirmed＝手で外した解決策を、契約者がまだ確かめていないか（1か所。✅ 2026-11-07 ユーザー「契約者が承認をしないと自動で入らず、
    新規の電話もかけられません」）：外した印があり（removed）、いちばん新しい外した印のあとに「入れない」（`leave_out`）も無い。「入れる」
    （`add_earlier`）なら removed が消えるので、確かめたことになる。門（gate.unconfirmed_solutions）と一覧（`not_in_line`）が読む。"""
    return tx.all(
        # まだ確かめていない＝外した印があり、いちばん新しい外した印のあとに「入れない」（leave_out）も無い（「入れる」＝選んで足すと removed が消える）
        "SELECT o.*, o.removed AND NOT EXISTS (SELECT 1 FROM salescall.client_solution_leave_outs k WHERE k.solution_id = o.solution_id"
        "   AND k.recorded_at >= (SELECT max(d.recorded_at) FROM salescall.client_solution_removals d"
        "                         WHERE d.solution_id = o.solution_id)) AS unconfirmed FROM ("
        "SELECT l.*, EXISTS (SELECT 1 FROM salescall.client_solution_removals d WHERE d.solution_id = l.solution_id"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.client_solution_lines x WHERE x.solution_id = d.solution_id"
        "                   AND x.chosen_by IS NOT NULL AND x.version_id IS NOT NULL AND x.recorded_at >= d.recorded_at)) AS removed"
        " FROM ("
        " SELECT DISTINCT ON (c.answer_name) c.solution_id, c.attempt_id, c.answer_name, c.solution_text, c.point_text, c.recorded_at"
        " FROM salescall.client_solutions c"
        " JOIN salescall.tasks t ON t.task_id = c.task_id JOIN salescall.runs r ON r.run_id = t.run_id"
        " WHERE r.business_id = %s AND EXISTS (SELECT 1 FROM salescall.client_solution_lines l"
        "                                      WHERE l.solution_id = c.solution_id AND l.version_id IS NOT NULL)"
        " ORDER BY c.answer_name, c.seq DESC) l"   # 足した順（データベースが振る＝0192。時刻は同じ取引で並ぶ）
        " WHERE NOT EXISTS (SELECT 1 FROM salescall.client_solution_replacements x WHERE x.solution_id = l.solution_id)"
        ") o ORDER BY o.answer_name", (business_id,))


def not_in_line(tx: Tx, version_id: UUID) -> list[dict[str, Any]]:
    """その版の流れに入っていない、その業務の前の解決策（問いの名前ごとにいちばん新しく足せたもの＝`_latest`）の一覧（1か所。画面が
    契約者に見せて選んでいただく＝計画書 6-1 (3)。足すのは `add_earlier`）：[{solution_id, attempt_id, answer_name, solution_text,
    point_text, recorded_at, removed, unconfirmed}]（removed＝契約者が手で外した解決策・unconfirmed＝そのうち、契約者がまだ「入れる」
    も「入れない」も確かめていないもの＝ダッシュボードの承認ボタンの元。どちらも `_latest`）。入っていない＝その流れのいちばん後の版
    （`successor_tip`）の、その問いの答えが、その解決策の文と違う（無い）。
    取り込み直しで残した問答・同じ答えを書いた問答・選んで足した問答は、入っているとみなす。
    （attempt_id＝解決策を求めた通話も入る。）
    ✅ 2026-11-06 ユーザー「契約者にそれをダッシュボードで確認します」＝自動では入れない。"""
    return _outside(tx, version_id, lambda r: True)


def unconfirmed(tx: Tx, version_id: UUID) -> list[dict[str, Any]]:
    """その版の流れに入っていない、手で外した解決策のうち、契約者がまだ確かめていないもの（`not_in_line` の unconfirmed。門が読む＝
    gate.unconfirmed_solutions。「入れない」と確かめる入口 `leave_out` が受けるのも、この一覧だけ）。"""
    return _outside(tx, version_id, lambda r: r["unconfirmed"])


def _outside(tx: Tx, version_id: UUID, pick: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """その版の流れに入っていない、その業務の解決策（`_latest`）のうち pick に当たるもの（入っているかを決めるのは、ここだけ）。当たる
    解決策が無ければ、版を読まない（門が行ごとに読む）。"""
    tip = business_settings.successor_tip(tx, version_id)
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (tip,))["business_id"]
    rows = [r for r in _latest(tx, business) if pick(r)]
    if not rows:
        return []
    now = dict(business_settings.load(tx, tip).answers)
    return [dict(r) for r in rows if now.get(r["answer_name"]) != r["solution_text"]]


def add_earlier(tx: Tx, version_id: UUID, solution_ids: list[UUID], *, find_names: NameFinder | None = None,
                confirmed_names: Any = ()) -> list[Line]:
    """契約者が `not_in_line` の一覧から選んだ前の解決策を、その版の流れに足す（1か所の入口。呼ぶのは画面だけ＝CL-3。管理者か契約者
    だけ＝データベースの守り client_solution_line_chosen_by_person）。足し方は新しい解決策と同じ `_add_line`（取込と同じ確かめ・版を足す道）。
    一覧に無い解決策は受けない（何も書かない）。足した流れの結果（版・通らなかった理由）を、選んだ順に返す。"""
    offered = {r["solution_id"]: r for r in not_in_line(tx, version_id)}
    _picked(solution_ids, offered, "足す解決策が選ばれていません", "その流れに入っていない前の解決策の一覧に無いものが選ばれています")
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))["business_id"]
    lines = []
    for sid in solution_ids:
        r = offered[sid]
        lines.append(_add_line(tx, sid, r["attempt_id"], business, version_id, r["answer_name"], r["solution_text"],
                               f"契約者が選んで足した前の解決策（点「{r['point_text']}」・問い「{r['answer_name']}」）",
                               find_names=find_names, confirmed_names=confirmed_names, chosen=True))
    return lines


def _picked(solution_ids: list[UUID], offered: Any, empty: str, missing: str) -> None:
    """契約者が選んだ解決策が、1つ以上・重ならず・すべて一覧（offered）にあるか（1か所。`add_earlier`・`leave_out`）。無ければ何も書かない。"""
    if not solution_ids:
        raise SolutionInvalid(empty)
    if len(set(solution_ids)) != len(solution_ids) or any(sid not in offered for sid in solution_ids):
        raise SolutionInvalid(missing)


def leave_out(tx: Tx, version_id: UUID, solution_ids: list[UUID]) -> None:
    """契約者が、手で外した解決策を「入れない」と確かめる（承認。1か所の入口。呼ぶのは画面だけ＝CL-3。管理者か契約者だけ＝データベースの
    守り client_solution_leave_out_by_person。0200）。「入れる」は `add_earlier`。受けるのは、その版の流れの、まだ確かめていない手で外した
    解決策（`unconfirmed`）だけ（ほかは何も書かない）。確かめたら、門はすぐ通す（gate.unconfirmed_solutions）。取り込み直しでも入れないまま
    （removed のまま＝`keep_in`）。✅ 2026-11-07 ユーザー「契約者が承認をしないと自動で入らず、新規の電話もかけられません」。"""
    offered = {r["solution_id"] for r in unconfirmed(tx, version_id)}
    _picked(solution_ids, offered, "入れない解決策が選ばれていません",
            "その流れの、まだ確かめていない手で外した解決策の一覧に無いものが選ばれています")
    for sid in solution_ids:
        tx.execute("INSERT INTO salescall.client_solution_leave_outs (tenant_id, client_id, solution_id, version_id, recorded_by)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (sid, version_id))


def kept_answers(tx: Tx, business_id: UUID) -> list[tuple[str, str]]:
    """その業務に、依頼者の解決策で足した問答（問いの名前ごとに、いちばん新しく足せたもの）。取り込み直した版にも残す（1か所。
    ✅ 2026-11-06 ユーザー「残す」＝見直し(B) の3。残すのは `keep_in`＝取込の入口の共通の1か所 business_import._kept で、同じ `_latest` を読む）。"""
    return [(r["answer_name"], r["solution_text"]) for r in _latest(tx, business_id) if not r["removed"]]


def keep_in(tx: Tx, business_id: UUID, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[UUID]]:
    """業務設定の中身に、解決策で足した問答を残す（取り込み直したとき。1か所）：中身に同じ問いの名前の答えがあれば中身を優先し、
    中身が「使わない」にした問いの名前も、そのまま（契約者が決めた）。無ければ足した問答を入れる。（中身, 残した問いの名前の知らせ,
    取り込んだ中身が先になった解決策＝違う答えを書いた・「使わない」にした）を返す（中身が先になった解決策は、登録したときに置き換えられたと
    残す＝`replaced`。呼ぶのは business_import._kept だけ）。"""
    own = {a.get("name"): a.get("text") for a in (raw.get("answers") or []) if isinstance(a, dict)}
    off = set(raw.get("answers_off") or []) if isinstance(raw.get("answers_off"), list) else set()
    notes, overridden = [], []
    for r in _latest(tx, business_id):
        name, text = r["answer_name"], r["solution_text"]
        if name in off or (name in own and own[name] != text):
            overridden.append(r["solution_id"])
        if name in own or name in off:
            continue
        if r["removed"]:   # 契約者が手で外した解決策は入れない（ダッシュボードで選んでいただく＝`not_in_line`・`add_earlier`）
            notes.append(f"依頼者の解決策で足した想定問答「{name}」は、手で前の版にして外したものなので入れていません"
                         "（入れるときは、ダッシュボードの「入っていない前の解決策」から選んでください）")
            continue
        raw = business_settings.with_answer(raw, name, text)
        notes.append(f"依頼者の解決策で足した想定問答「{name}」を残しました（取り込んだ中身に、この問いがありません）")
    return raw, notes, overridden


def removed_by_hand(tx: Tx, run_id: UUID, before: UUID | None, chosen: UUID, after: UUID, *, auto: bool) -> None:
    """実施回に版を割り当てたとき（business_settings.assign の1か所が、どの割り当てでも呼ぶ）、契約者が「手で外した」解決策を残す（当たるかを
    決めるのはここだけ。0197 の client_solution_removals）。before＝割り当てる前の実施回の版（実施回を作るときは無い）・chosen＝選んだ版・
    after＝割り当てた版（選んだ版がつながりの途中なら、その写し）。当たるのは、管理者か契約者が選んだとき（auto でない）の次の2つ：
    (1) 手で版を替えた：前の版の流れ（before のつながりの先）に入っていて、割り当てた版に入っていない解決策。
    (2) 実施回を作るときに、その業務のつながりの先でない版（古い版）を選んだ：選んだ版の流れ（chosen のつながりの先）に入っていて、割り当てた
        版に入っていない解決策（✅ 2026-11-07 ユーザー：伺い「古い版で実施回を作ったとき、入っていない解決策を手で外したとみなすか」に
        「みなす」）。つながりの先の版を選んだときは、割り当てた版が選んだ版のまま（写しが無い）なので当たらない。
    ✅ 2026-11-07 ユーザー「契約者が確かめる」：手で外した解決策は、取り込み直しでも自動では入れない（`keep_in`）。契約者が「入れる」
    （`add_earlier`）か「入れない」（`leave_out`）かを確かめるまで、その実施回では、まだ一度もかけていないお客様の行へ新しい電話をかけない
    （gate.unconfirmed_solutions）。外したら契約者に知らせる（ダッシュボードの承認ボタンの元。solution_unconfirmed）。"""
    if auto:
        return
    base = before if before is not None else chosen
    if base == after:
        return
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (after,))["business_id"]
    rows = _latest(tx, business)
    if not rows:   # 解決策の無い業務は、版を読まない
        return
    was = dict(business_settings.load(tx, business_settings.successor_tip(tx, base)).answers)
    now = dict(business_settings.load(tx, after).answers)
    out = []
    for r in rows:
        if was.get(r["answer_name"]) == r["solution_text"] and now.get(r["answer_name"]) != r["solution_text"]:
            out.append((r, tx.one("INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, recorded_by)"
                                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor()) RETURNING seq",
                                  (r["solution_id"], after))["seq"]))
    if out:
        names = "・".join(f"「{r['answer_name']}」" for r, _ in out)
        notify.emit(tx, "solution_unconfirmed", key=f"solution_unconfirmed:{out[0][1]}",
                    summary=f"実施回の業務設定を、依頼者の解決策の入る前の版にしたので、解決策 {len(out)} 件（問い{names}）が入っていません。"
                            "ダッシュボードで、解決策ごとに「入れる」か「入れない」かを選んで承認してください。承認するまで、この実施回では、"
                            "まだ一度もかけていないお客様の行へ新しい電話をかけません（約束したかけ直し・続きの電話・お詫びの電話はかけます）。",
                    refs={"run_id": str(run_id), "version_id": str(after), "solutions": [str(r["solution_id"]) for r, _ in out]})


def replaced(tx: Tx, solution_ids: list[UUID], version_id: UUID) -> None:
    """取り込み直して登録した版（version_id）の中身が先になった解決策を、置き換えられたと残す（1か所。0197 の client_solution_replacements。
    書くのは取込の登録の共通の1か所 business_import.register_shaped だけ）。置き換えられた解決策は `_latest` に入らない（取り込み直しでも
    残さない・流れに入っていない前の解決策の一覧に出さない・知らせ solution_earlier に数えない）。"""
    for sid in solution_ids:
        tx.execute("INSERT INTO salescall.client_solution_replacements (tenant_id, client_id, solution_id, version_id, recorded_by)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (sid, version_id))
