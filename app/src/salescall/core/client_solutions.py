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
        # いま足した解決策そのものは出さない（確かめに通らなかった流れは solution_held で知らせる。⛔ 2026-11-07 の第三者の見直し (2) の4）。
        # 確かめは実施回ごとなので、その流れを使う実施回（まだ終えていない実施回＝business_settings.in_use_runs の1か所）の並びも渡す
        # （画面が実施回ごとの入口 `not_in_line`・`add_earlier`・`leave_out` を呼べるように。2026-11-07 のつなぎ目の見直し (C)）
        tip = line.version_id or line.base
        earlier = [e for e in not_in_version(tx, tip) if e["solution_id"] != sid]
        if earlier:
            runs = [str(r) for r in business_settings.in_use_runs(tx, row["business_id"])
                    if business_settings.successor_tip(tx, _run_version(tx, r)) == tip]
            notify.emit(tx, "solution_earlier", key=f"solution_earlier:{sid}:{line.base}",
                        summary=f"依頼者の解決策（問い「{name}」）を足した業務の版の流れに、入っていない前の解決策が {len(earlier)} 件あります"
                                "（前の版に戻す前に足したもの など）。入れるものを、実施回ごとにダッシュボードで選んでください（自動では入れません）。",
                        refs={"solution_id": str(sid), "version_id": str(tip), "runs": runs,
                              "earlier": [str(e["solution_id"]) for e in earlier]})
    return Added(sid, name, tuple(lines))


def _run_version(tx: Tx, run_id: UUID) -> UUID:
    """その実施回のいまの版（データベースの salescall.run_current_version の1か所）。"""
    return tx.one("SELECT salescall.run_current_version(%s) AS v", (run_id,))["v"]


def _add_line(tx: Tx, sid: UUID, attempt_id: UUID, business_id: UUID, version: UUID, name: str, text: str, note: str, *,
              find_names: NameFinder | None, confirmed_names: Any, chosen: UUID | None = None) -> Line:
    """業務の版の流れ1つ（version＝その流れの版。足す元は、そのつながりのいちばん後の版＝`business_settings.successor_tip`＝データベースの
    salescall.version_tip の1か所。derive も同じ1か所で決める）に、解決策の問答を足す（1か所。`add` と、契約者が前の解決策を選んで足す
    `add_earlier` の両方が通る）：取込と同じ確かめ（business_import.review。重ねる標準は、その版の標準のまま）を通し、通れば業務設定の版を
    足す（business_settings.derive。きっかけ solution）。中身が変わらなければ版を作らない。流れの結果を記録し（chosen＝契約者が、その実施回の承認で選んで
    足した＝選んだ方と実施回を残す。0194・0200）、通らなければ契約者に知らせる。
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
               "                                             problems, chosen_by, chosen_run)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, CASE WHEN %s THEN salescall.cur_actor() END, %s)",
               (sid, tip, made, replaced, list(problems), chosen is not None, chosen))
    if made is None:
        notify.emit(tx, "solution_held", key=f"solution_held:{sid}:{tip}",
                    summary=f"依頼者の解決策（問い「{name}」）を、確かめに通らなかったので想定問答に足していません："
                            + "／".join(problems), refs={"solution_id": str(sid), "attempt_id": str(attempt_id),
                                                        "version_id": str(tip)})
    return Line(tip, made, replaced, problems)


def _latest(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """その業務に、依頼者の解決策で足せた問答（問いの名前ごとに、いちばん新しく足せたもの。1か所＝取り込み直したときに残す `kept_answers` と、
    流れに入っていない前の解決策の一覧 `not_in_version`・`not_in_line` が読む）。いちばん新しいものが、取り込み直した中身で置き換えられていれば
    （`replaced`）、その問いの名前は入れない（前の解決策も、よみがえらせない。⛔ 2026-11-07 の第三者の見直し (2) の4）。
    手で外したか・確かめたかは実施回ごと（`_marks`）なので、ここでは持たない。"""
    return tx.all(
        "SELECT l.* FROM ("
        " SELECT DISTINCT ON (c.answer_name) c.solution_id, c.attempt_id, c.answer_name, c.solution_text, c.point_text, c.recorded_at"
        " FROM salescall.client_solutions c"
        " JOIN salescall.tasks t ON t.task_id = c.task_id JOIN salescall.runs r ON r.run_id = t.run_id"
        " WHERE r.business_id = %s AND EXISTS (SELECT 1 FROM salescall.client_solution_lines l"
        "                                      WHERE l.solution_id = c.solution_id AND l.version_id IS NOT NULL)"
        " ORDER BY c.answer_name, c.seq DESC) l"   # 足した順（データベースが振る＝0192。時刻は同じ取引で並ぶ）
        " WHERE NOT EXISTS (SELECT 1 FROM salescall.client_solution_replacements x WHERE x.solution_id = l.solution_id)"
        " ORDER BY l.answer_name", (business_id,))


def _marks(tx: Tx, run_id: UUID, solution_ids: list[UUID]) -> dict[UUID, tuple[bool, bool]]:
    """その実施回で、解決策ごとの (removed, unconfirmed)（1か所。✅ 2026-11-07 本体の決定「確かめは実施回ごと」：外すのも確かめるのも、
    契約者が実施回ごとにする操作なので、ほかの実施回の外す・入れる・入れないは、この実施回に効かない）。
    removed＝その実施回で契約者が手で外した（`removed_by_hand` が残した外した印がある）。
    unconfirmed＝そのうえ、その実施回のいちばん新しい外した印のあとに、その実施回で「入れない」（`leave_out`）とも確かめていない（時刻で比べる。
    ✅ 2026-11-07 ユーザー「契約者が承認をしないと自動で入らず、新規の電話もかけられません」）。
    「入れる」（`add_earlier`）を選べば、その解決策はその実施回の流れに入るので、一覧（`_outside`＝入っているかの1か所）に出なくなる＝
    確かめたことになる（ここでは見ない。選んだ実施回は流れの結果の chosen_run に残る＝0200。外した印は消えない）。その実施回の流れは、
    ほかの実施回と分かれている（手で外した印の付く割り当ては、ほかのまだ終えていない実施回の版を使わず写しを作る＝
    business_settings.assign の `_shared_apart`）ので、ほかの実施回の「入れる」で、この実施回の流れに入ることは無い。その実施回で、
    またその解決策を外せば、新しい外した印から、また確かめが要る。"""
    if not solution_ids:
        return {}
    rows = tx.all(
        "SELECT s.sid, d.at IS NOT NULL AS removed,"
        "       d.at IS NOT NULL AND NOT coalesce(k.at >= d.at, false) AS unconfirmed"
        " FROM unnest(%s::uuid[]) AS s(sid)"
        " LEFT JOIN LATERAL (SELECT max(recorded_at) AS at FROM salescall.client_solution_removals"
        "                    WHERE solution_id = s.sid AND run_id = %s) d ON true"
        " LEFT JOIN LATERAL (SELECT max(recorded_at) AS at FROM salescall.client_solution_leave_outs"
        "                    WHERE solution_id = s.sid AND run_id = %s) k ON true",
        (list(solution_ids), run_id, run_id))
    return {r["sid"]: (r["removed"], r["unconfirmed"]) for r in rows}


def not_in_version(tx: Tx, version_id: UUID) -> list[dict[str, Any]]:
    """その版の流れに入っていない、その業務の前の解決策（問いの名前ごとにいちばん新しく足せたもの＝`_latest`）：[{solution_id, attempt_id,
    answer_name, solution_text, point_text, recorded_at}]（実施回の承認の印は無い。解決策を足したときの知らせ solution_earlier が読む）。"""
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))["business_id"]
    return _outside(tx, version_id, [dict(r) for r in _latest(tx, business)])


def not_in_line(tx: Tx, run_id: UUID) -> list[dict[str, Any]]:
    """その実施回のいまの版の流れに入っていない、その業務の前の解決策の一覧（1か所。画面が実施回ごとに契約者に見せて、承認ボタンを出す＝
    計画書 6-1 (3)。「入れる」は `add_earlier`・「入れない」は `leave_out`。どちらも同じ実施回を渡す）：[{solution_id, attempt_id, answer_name,
    solution_text, point_text, recorded_at, removed, unconfirmed}]（removed＝その実施回で契約者が手で外した・unconfirmed＝そのうち、その
    実施回でまだ「入れる」も「入れない」も確かめていない＝承認ボタンの元。どちらも `_marks`）。入っていない＝その流れのいちばん後の版
    （`successor_tip`）の、その問いの答えが、その解決策の文と違う（無い）。取り込み直しで残した問答・同じ答えを書いた問答・選んで足した
    問答は、入っているとみなす。✅ 2026-11-06 ユーザー「契約者にそれをダッシュボードで確認します」＝自動では入れない。"""
    return _for_run(tx, run_id, lambda r: True)


def unconfirmed(tx: Tx, run_id: UUID) -> list[dict[str, Any]]:
    """その実施回の流れに入っていない、その実施回で手で外した解決策のうち、契約者がまだ確かめていないもの（`not_in_line` の unconfirmed。
    門が読む＝gate.unconfirmed_solutions。「入れない」と確かめる入口 `leave_out` が受けるのも、この一覧だけ）。まだ確かめていない解決策が
    無ければ、版を読まない（候補を出す処理が読む）。"""
    return _for_run(tx, run_id, lambda r: r["unconfirmed"])


def _for_run(tx: Tx, run_id: UUID, pick: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """その実施回の承認の印（`_marks`）を付けた解決策のうち pick に当たり、その実施回のいまの版の流れに入っていないもの。"""
    run = tx.one("SELECT business_id, salescall.run_current_version(run_id) AS v FROM salescall.runs WHERE run_id = %s", (run_id,))
    if run is None or run["v"] is None:
        return []
    rows = _latest(tx, run["business_id"])
    marks = _marks(tx, run_id, [r["solution_id"] for r in rows])
    rows = [dict(r, removed=marks[r["solution_id"]][0], unconfirmed=marks[r["solution_id"]][1]) for r in rows]
    return _outside(tx, run["v"], [r for r in rows if pick(r)])


def _outside(tx: Tx, version_id: UUID, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """rows のうち、その版の流れに入っていないもの（入っているかを決めるのは、ここだけ）。rows が無ければ、版を読まない。"""
    if not rows:
        return []
    now = dict(business_settings.load(tx, business_settings.successor_tip(tx, version_id)).answers)
    return [r for r in rows if now.get(r["answer_name"]) != r["solution_text"]]


def add_earlier(tx: Tx, run_id: UUID, solution_ids: list[UUID], *, find_names: NameFinder | None = None,
                confirmed_names: Any = ()) -> list[Line]:
    """契約者が、その実施回の `not_in_line` の一覧から選んだ前の解決策を、その実施回のいまの版の流れに足す（「入れる」の承認。1か所の入口。
    呼ぶのは画面だけ＝CL-3。管理者か契約者だけ＝データベースの守り client_solution_line_chosen_by_person）。足し方は新しい解決策と同じ
    `_add_line`（取込と同じ確かめ・版を足す道）。流れの結果に、選んだ方と実施回を残す（足せば、その実施回の流れに入るので一覧に出なくなる
    ＝その実施回で確かめたことになる。ほかの実施回の流れには入らない）。
    一覧に無い解決策は受けない（何も書かない）。足した流れの結果（版・通らなかった理由）を、選んだ順に返す。"""
    offered = {r["solution_id"]: r for r in not_in_line(tx, run_id)}
    _picked(solution_ids, offered, "足す解決策が選ばれていません", "その流れに入っていない前の解決策の一覧に無いものが選ばれています")
    run = tx.one("SELECT business_id, salescall.run_current_version(run_id) AS v FROM salescall.runs WHERE run_id = %s", (run_id,))
    lines = []
    for sid in solution_ids:
        r = offered[sid]
        lines.append(_add_line(tx, sid, r["attempt_id"], run["business_id"], run["v"], r["answer_name"], r["solution_text"],
                               f"契約者が選んで足した前の解決策（点「{r['point_text']}」・問い「{r['answer_name']}」）",
                               find_names=find_names, confirmed_names=confirmed_names, chosen=run_id))
    return lines


def _picked(solution_ids: list[UUID], offered: Any, empty: str, missing: str) -> None:
    """契約者が選んだ解決策が、1つ以上・重ならず・すべて一覧（offered）にあるか（1か所。`add_earlier`・`leave_out`）。無ければ何も書かない。"""
    if not solution_ids:
        raise SolutionInvalid(empty)
    if len(set(solution_ids)) != len(solution_ids) or any(sid not in offered for sid in solution_ids):
        raise SolutionInvalid(missing)


def leave_out(tx: Tx, run_id: UUID, solution_ids: list[UUID]) -> None:
    """契約者が、その実施回で手で外した解決策を「入れない」と確かめる（承認。1か所の入口。呼ぶのは画面だけ＝CL-3。管理者か契約者だけ＝
    データベースの守り client_solution_leave_out_by_person。0200）。「入れる」は `add_earlier`。受けるのは、その実施回の、まだ確かめていない
    手で外した解決策（`unconfirmed`）だけ（ほかは何も書かない）。確かめたら、その実施回の門はすぐ通す（gate.unconfirmed_solutions）。
    取り込み直しでも入れないまま（`held_back`）。✅ 2026-11-07 ユーザー「契約者が承認をしないと自動で入らず、新規の電話もかけられません」。"""
    offered = {r["solution_id"] for r in unconfirmed(tx, run_id)}
    _picked(solution_ids, offered, "入れない解決策が選ばれていません",
            "その実施回の、まだ確かめていない手で外した解決策の一覧に無いものが選ばれています")
    for sid in solution_ids:
        tx.execute("INSERT INTO salescall.client_solution_leave_outs (tenant_id, client_id, solution_id, run_id, recorded_by)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (sid, run_id))


def held_back(tx: Tx, business_id: UUID) -> set[UUID]:
    """取り込み直しで自動では入れない解決策（1か所。`kept_answers`・`keep_in` が読む）：その業務のまだ終えていない実施回
    （business_settings.in_use_runs＝取り込み直した版を使いうる実施回。行が0の実施回も入る。終えた実施回＝行がすべて終わった実施回だけ
    除く。2026-11-07 のつなぎ目の見直し (D)）のどれかで、契約者が手で外して、その実施回の流れに入っていないもの
    （`not_in_line` の removed。まだ確かめていない・「入れない」と確かめた、の両方）。✅ 2026-11-07 ユーザー「契約者が確かめる」：契約者が
    外した判断・まだしていない判断を、取り込み直しが代わりにしない（その実施回で「入れる」を選べば外れる）。"""
    out: set[UUID] = set()
    for run in business_settings.in_use_runs(tx, business_id):
        out |= {r["solution_id"] for r in not_in_line(tx, run) if r["removed"]}
    return out


def kept_answers(tx: Tx, business_id: UUID) -> list[tuple[str, str]]:
    """その業務に、依頼者の解決策で足した問答（問いの名前ごとに、いちばん新しく足せたもの）。取り込み直した版にも残す（1か所。
    ✅ 2026-11-06 ユーザー「残す」＝見直し(B) の3。残すのは `keep_in`＝取込の入口の共通の1か所 business_import._kept で、同じ `_latest` を読む）。"""
    held = held_back(tx, business_id)
    return [(r["answer_name"], r["solution_text"]) for r in _latest(tx, business_id) if r["solution_id"] not in held]


def keep_in(tx: Tx, business_id: UUID, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[UUID]]:
    """業務設定の中身に、解決策で足した問答を残す（取り込み直したとき。1か所）：中身に同じ問いの名前の答えがあれば中身を優先し、
    中身が「使わない」にした問いの名前も、そのまま（契約者が決めた）。無ければ足した問答を入れる（実施回で手で外した解決策＝`held_back` は
    入れない）。（中身, 残した問いの名前の知らせ, 取り込んだ中身が先になった解決策＝違う答えを書いた・「使わない」にした）を返す（中身が先に
    なった解決策は、登録したときに置き換えられたと残す＝`replaced`。呼ぶのは business_import._kept だけ）。"""
    own = {a.get("name"): a.get("text") for a in (raw.get("answers") or []) if isinstance(a, dict)}
    off = set(raw.get("answers_off") or []) if isinstance(raw.get("answers_off"), list) else set()
    held = held_back(tx, business_id)
    notes, overridden = [], []
    for r in _latest(tx, business_id):
        name, text = r["answer_name"], r["solution_text"]
        if name in off or (name in own and own[name] != text):
            overridden.append(r["solution_id"])
        if name in own or name in off:
            continue
        if r["solution_id"] in held:   # 契約者が実施回で手で外した解決策は入れない（その実施回の承認で選んでいただく＝`not_in_line`）
            notes.append(f"依頼者の解決策で足した想定問答「{name}」は、実施回で手で前の版にして外したものなので入れていません"
                         "（入れるときは、その実施回のダッシュボードの「入っていない前の解決策」から選んでください）")
            continue
        raw = business_settings.with_answer(raw, name, text)
        notes.append(f"依頼者の解決策で足した想定問答「{name}」を残しました（取り込んだ中身に、この問いがありません）")
    return raw, notes, overridden


def removed_by_hand(tx: Tx, run_id: UUID, before: UUID | None, after: UUID, *, auto: bool) -> None:
    """実施回に版を割り当てたとき（business_settings.assign の1か所が、どの割り当てでも呼ぶ）、契約者が「手で外した」解決策を残す（当たるかを
    決めるのはここだけ。0197 の client_solution_removals）。before＝割り当てる前の実施回の版（実施回を作るときは無い）・after＝割り当てた版
    （選んだ版がつながりの途中・ほかの実施回と流れを分けるときは、その写し）。当たるのは、管理者か契約者が選んだとき（auto でない）：
    その業務のいまの解決策（`_latest`）のうち、割り当てた版に入っていないもの（入っているかは `_outside` の1か所）を、すべて「手で外した」と
    する（✅ 2026-11-07 ユーザー＝計画書 6-1 (6)：伺い「古い版で実施回を作ったとき、入っていない解決策を手で外したとみなすか」に「みなす」。
    手で前の版に戻したときも同じ）。ただし手で版を替えたときは、前の版の流れ（before）にも入っていなかったもの（もう外して確かめた・
    まだ確かめている途中のもの）は、新しく外したことにしない。⛔ 2026-11-07 の見直し：前は選んだ版の「自分のつながりの先」とだけ比べたので、
    手で前の版に戻した写し（それ自身がつながりの先）で実施回を作ると、何も印を付けなかった（写しの写し・写しの先の版も同じ）。
    ✅ 2026-11-07 ユーザー「契約者が確かめる」：手で外した解決策は、取り込み直しでも自動では入れない（`keep_in`）。契約者が「入れる」
    （`add_earlier`）か「入れない」（`leave_out`）かを確かめるまで、その実施回では、お客様と約束したかけ直し・続きの電話・お詫びの電話の
    ほかは電話をかけない（gate.unconfirmed_solutions）。外したら契約者に知らせる（ダッシュボードの承認ボタンの元。solution_unconfirmed）。"""
    if auto or before == after:
        return
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (after,))["business_id"]
    rows = [dict(r) for r in _latest(tx, business)]
    gone = _outside(tx, after, rows)
    if before is not None:
        was_out = {r["solution_id"] for r in _outside(tx, before, rows)}
        gone = [r for r in gone if r["solution_id"] not in was_out]
    out = [(r, tx.one("INSERT INTO salescall.client_solution_removals (tenant_id, client_id, solution_id, version_id, run_id, recorded_by)"
                      " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor()) RETURNING seq",
                      (r["solution_id"], after, run_id))["seq"]) for r in gone]
    if out:
        names = "・".join(f"「{r['answer_name']}」" for r, _ in out)
        notify.emit(tx, "solution_unconfirmed", key=f"solution_unconfirmed:{out[0][1]}",
                    summary=f"実施回の業務設定を、依頼者の解決策の入っていない版にしたので、解決策 {len(out)} 件（問い{names}）が入っていません。"
                            "ダッシュボードで、解決策ごとに「入れる」か「入れない」かを選んで承認してください。承認するまで、この実施回では、"
                            "お客様と約束したかけ直し・続きの電話・お詫びの電話のほかは、電話をかけません。",
                    refs={"run_id": str(run_id), "version_id": str(after), "solutions": [str(r["solution_id"]) for r, _ in out]})


def replaced(tx: Tx, solution_ids: list[UUID], version_id: UUID) -> None:
    """取り込み直して登録した版（version_id）の中身が先になった解決策を、置き換えられたと残す（1か所。0197 の client_solution_replacements。
    書くのは取込の登録の共通の1か所 business_import.register_shaped だけ）。置き換えられた解決策は `_latest` に入らない（取り込み直しでも
    残さない・流れに入っていない前の解決策の一覧に出さない・知らせ solution_earlier に数えない）。"""
    for sid in solution_ids:
        tx.execute("INSERT INTO salescall.client_solution_replacements (tenant_id, client_id, solution_id, version_id, recorded_by)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (sid, version_id))
