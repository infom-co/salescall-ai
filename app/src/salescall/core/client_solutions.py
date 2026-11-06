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
        # （✅ 2026-11-06 ユーザー＝計画書 6-1 (3)：「契約者にそれをダッシュボードで確認します。」。一覧は `not_in_line`・足す入口は `add_earlier`）
        earlier = not_in_line(tx, line.version_id or line.base)
        if earlier:
            notify.emit(tx, "solution_earlier", key=f"solution_earlier:{sid}:{line.base}",
                        summary=f"依頼者の解決策（問い「{name}」）を足した業務の版の流れに、入っていない前の解決策が {len(earlier)} 件あります"
                                "（前の版に戻す前に足したもの など）。入れるものをダッシュボードで選んでください（自動では入れません）。",
                        refs={"solution_id": str(sid), "version_id": str(line.version_id or line.base),
                              "earlier": [str(e["solution_id"]) for e in earlier]})
    return Added(sid, name, tuple(lines))


def _add_line(tx: Tx, sid: UUID, attempt_id: UUID, business_id: UUID, tip: UUID, name: str, text: str, note: str, *,
              find_names: NameFinder | None, confirmed_names: Any, chosen: bool = False) -> Line:
    """業務の版の流れ1つ（tip＝その流れのいちばん後の版）に、解決策の問答を足す（1か所。`add` と、契約者が前の解決策を選んで足す
    `add_earlier` の両方が通る）：取込と同じ確かめ（business_import.ask_ai＋review。重ねる標準は、その版の標準のまま）を通し、通れば
    業務設定の版を足す（business_settings.derive。きっかけ solution）。中身が変わらなければ版を作らない。流れの結果を記録し（chosen＝契約者が
    選んで足した＝選んだ方を残す。0194）、通らなければ契約者に知らせる。"""
    now = business_settings.load(tx, tip)
    replaced = name in {a for a, _ in now.answers}
    raw = business_settings.with_answer(now.raw, name, text)
    version, problems = tip, ()
    if raw != now.raw:
        p = business_import.review(tx, business_id, raw, confirmed_names=confirmed_names,
                                   ai=business_import.ask_ai(raw, find_names=find_names), standard_id=now.standard_id)
        problems = tuple(p.problems)
        version = None if problems or p.raw is None else business_settings.derive(
            tx, tip, lambda _old, r=p.raw: r, cause="solution", note=note, standard_id=p.standard_id)
        if version is None:
            problems = problems or ("確かめに通りませんでした",)
    tx.execute("INSERT INTO salescall.client_solution_lines (tenant_id, client_id, solution_id, base_version, version_id, replaced,"
               "                                             problems, chosen_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, CASE WHEN %s THEN salescall.cur_actor() END)",
               (sid, tip, version, replaced, list(problems), chosen))
    if version is None:
        notify.emit(tx, "solution_held", key=f"solution_held:{sid}:{tip}",
                    summary=f"依頼者の解決策（問い「{name}」）を、確かめに通らなかったので想定問答に足していません："
                            + "／".join(problems), refs={"solution_id": str(sid), "attempt_id": str(attempt_id),
                                                        "version_id": str(tip)})
    return Line(tip, version, replaced, problems)


def _latest(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """その業務に、依頼者の解決策で足せた問答（問いの名前ごとに、いちばん新しく足せたもの。1か所＝取り込み直したときに残す `kept_answers` と、
    流れに入っていない前の解決策の一覧 `not_in_line` が読む）。"""
    return tx.all(
        "SELECT DISTINCT ON (c.answer_name) c.solution_id, c.attempt_id, c.answer_name, c.solution_text, c.point_text, c.recorded_at"
        " FROM salescall.client_solutions c"
        " JOIN salescall.tasks t ON t.task_id = c.task_id JOIN salescall.runs r ON r.run_id = t.run_id"
        " WHERE r.business_id = %s AND EXISTS (SELECT 1 FROM salescall.client_solution_lines l"
        "                                      WHERE l.solution_id = c.solution_id AND l.version_id IS NOT NULL)"
        " ORDER BY c.answer_name, c.seq DESC", (business_id,))   # 足した順（データベースが振る＝0192。時刻は同じ取引で並ぶ）


def not_in_line(tx: Tx, version_id: UUID) -> list[dict[str, Any]]:
    """その版の流れに入っていない、その業務の前の解決策（問いの名前ごとにいちばん新しく足せたもの＝`_latest`）の一覧（1か所。画面が
    契約者に見せて選んでいただく＝計画書 6-1 (3)。足すのは `add_earlier`）：[{solution_id, attempt_id, answer_name, solution_text,
    point_text, recorded_at}]。入っていない＝その流れのいちばん後の版（`successor_tip`）の、その問いの答えが、その解決策の文と違う（無い）。
    取り込み直しで残した問答・同じ答えを書いた問答・選んで足した問答は、入っているとみなす。
    （attempt_id＝解決策を求めた通話も入る。）
    ✅ 2026-11-06 ユーザー「契約者にそれをダッシュボードで確認します」＝自動では入れない。"""
    tip = business_settings.successor_tip(tx, version_id)
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (tip,))["business_id"]
    now = dict(business_settings.load(tx, tip).answers)
    return [dict(r) for r in _latest(tx, business) if now.get(r["answer_name"]) != r["solution_text"]]


def add_earlier(tx: Tx, version_id: UUID, solution_ids: list[UUID], *, find_names: NameFinder | None = None,
                confirmed_names: Any = ()) -> list[Line]:
    """契約者が `not_in_line` の一覧から選んだ前の解決策を、その版の流れに足す（1か所の入口。呼ぶのは画面だけ＝CL-3。管理者か契約者
    だけ＝データベースの守り client_solution_line_chosen_by_person）。足し方は新しい解決策と同じ `_add_line`（取込と同じ確かめ・版を足す道）。
    一覧に無い解決策は受けない（何も書かない）。足した流れの結果（版・通らなかった理由）を、選んだ順に返す。"""
    if not solution_ids:
        raise SolutionInvalid("足す解決策が選ばれていません")
    offered = {r["solution_id"]: r for r in not_in_line(tx, version_id)}
    if len(set(solution_ids)) != len(solution_ids) or any(sid not in offered for sid in solution_ids):
        raise SolutionInvalid("その流れに入っていない前の解決策の一覧に無いものが選ばれています")
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))["business_id"]
    lines = []
    for sid in solution_ids:
        r = offered[sid]
        lines.append(_add_line(tx, sid, r["attempt_id"], business, business_settings.successor_tip(tx, version_id), r["answer_name"],
                               r["solution_text"],
                               f"契約者が選んで足した前の解決策（点「{r['point_text']}」・問い「{r['answer_name']}」）",
                               find_names=find_names, confirmed_names=confirmed_names, chosen=True))
    return lines


def kept_answers(tx: Tx, business_id: UUID) -> list[tuple[str, str]]:
    """その業務に、依頼者の解決策で足した問答（問いの名前ごとに、いちばん新しく足せたもの）。取り込み直した版にも残す（1か所。
    ✅ 2026-11-06 ユーザー「残す」＝見直し(B) の3。読むのは business_import._from_file＝ファイルの入口だけ）。"""
    return [(r["answer_name"], r["solution_text"]) for r in _latest(tx, business_id)]


def keep_in(tx: Tx, business_id: UUID, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """業務設定の中身に、解決策で足した問答を残す（取り込み直したとき。1か所）：中身に同じ問いの名前の答えがあれば中身を優先し、
    中身が「使わない」にした問いの名前も、そのまま（契約者が決めた）。無ければ足した問答を入れる。（中身, 残した問いの名前の知らせ）を返す。"""
    own = {a.get("name") for a in (raw.get("answers") or []) if isinstance(a, dict)}
    off = set(raw.get("answers_off") or []) if isinstance(raw.get("answers_off"), list) else set()
    notes = []
    for name, text in kept_answers(tx, business_id):
        if name in own or name in off:
            continue
        raw = business_settings.with_answer(raw, name, text)
        notes.append(f"依頼者の解決策で足した想定問答「{name}」を残しました（取り込んだ中身に、この問いがありません）")
    return raw, notes
