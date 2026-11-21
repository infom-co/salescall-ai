"""承認待ちの受け箱（docs/DASHBOARD.md 4章）。種類ごとの違いは、この表 `KINDS` の1か所に書き、画面の部品は1つ。

管理者・契約者は「確かめて、承認する」だけ（2026-11-01 管理者「人は甚大な問題以外で介入しない」）。どの種類も、同じ形で見せる：
なにを決めるか・根拠・決めないと何が起きるか・ボタン（承認する／差し戻す）。並び順は緊急 → 期限 → 古い順（`ordered`）。

この表に無い承認を、別の画面に作らない。種類を足すときは、ここに1行足す（docs/DASHBOARD.md 4-2 の表と同じ並び）。
⏳ まだ入れていない種類：録音の承認・名前の録音の審査（録音を聞く仕組みはできた＝CL-2 の 4-460。お詫びの電話の承認と同じ部品で作る）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from salescall.core import (approval, apology, calling_hours, client_solutions, conversation_io, fix_requests, next_action, row_fixes,
                            tasks, usage)
from salescall.core.access import Tx
from salescall.output import result_rows, words
from salescall.web import counts, links


@dataclass(frozen=True)
class Button:
    action: str        # 送る名前（`KINDS[種類].perform` の鍵）
    label: str
    main: bool = False
    go: str = ""       # 押すと決めずに、その画面へ移るボタン（例：「違う（直す）」→ 行の画面の直す所）。送らないので perform の鍵は要らない


@dataclass(frozen=True)
class Item:
    kind: str
    key: dict[str, str]           # 操作に送り返す鍵（文字に直した値）
    title: str                    # 種類の名前
    decide: str                   # なにを決めるか（1行）
    evidence: list[tuple[str, str]] = field(default_factory=list)   # 根拠（見出し, 中身）
    if_not: str = ""              # 決めないと何が起きるか
    at: datetime | None = None    # いつからか
    deadline: datetime | None = None
    urgent: bool = False
    buttons: tuple[Button, ...] = ()
    waiting_why: str = ""         # ボタンを出さない理由（まだ AI が確かめている など）
    business_id: UUID | None = None   # どの業務のものか（業務のタブで絞る。依頼者全体のもの＝赤字の確認などは None）
    business_name: str = ""          # 業務の名前（取り消せない操作は、どの業務のものかを名前つきで見せる＝Intent.md 5-1）
    link: str = ""                   # 決める前に見る画面（例：お客様リスト・お客様の行）
    audio: tuple[tuple[str, str], ...] = ()   # 聞いて決める録音（見出し, "録音の組/部分"。音は /c/{依頼者}/recordings/… の1つの受け口から）


@dataclass(frozen=True)
class Kind:
    title: str
    collect: Callable[[Tx, Any, Any], list[Item]]             # その範囲の取引で、その種類の承認待ちを集める（行・実施回で絞る。None なら全部）
    perform: dict[str, tuple[Callable[[Tx, dict[str, str]], Any], str]]   # ボタン → (決める部品, 済んだときの言葉)
    where: str = "client"   # 集めて決める取引：client＝依頼者ごと（依頼者の取引）／system＝全体（管理者だけ。システムの取引。データベースも守る）


def _business_of(tx: Tx, task_id: Any) -> dict[str, Any]:
    """行の業務（Item の business_id・business_name）。どの業務かは core の1か所（`tasks.scope`）を読む（行 → 実施回 → 業務を、
    ここで書き直さない）。名前は、その業務の名前をそのまま。"""
    bid = tasks.scope(tx, task_id)["business_id"]
    name = tx.one("SELECT name FROM salescall.businesses WHERE business_id = %s", (bid,))
    return {"business_id": bid, "business_name": name["name"] if name else ""}


def _decisions(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    items = []
    for d in approval.pending_decisions(tx):
        if task is not None and str(d["task_id"]) != str(task):
            continue
        parts = "・".join(words.DECISION_PARTS.get(p, words.NO_WORD) for p in (d["parts"] or []))
        said = [u["said"] for u in conversation_io.utterances(tx, d["attempt_id"]) if u.get("speaker") == "callee"][-3:]
        ev = [("待っている中身", parts), ("種類", words.DECISION_KINDS.get(d["kind"], words.NO_WORD))]
        if said:
            ev.append(("お客様の言葉（最後の3つ）", " ／ ".join(said)))
        seen = viewable_evidence(tx, d["attempt_id"])
        items.append(Item(
            kind="decision", key={"task_id": str(d["task_id"]), "attempt_id": str(d["attempt_id"]), "seq": str(d["seq"])},
            title="取り消せない決定の確かめ", decide=f"「{parts}」として確定してよいか",
            evidence=ev, if_not="決まるまで、このお客様にはかけません。",
            at=d["waited_at"], **_business_of(tx, d["task_id"]), link=links.call(tx, d["task_id"], d["attempt_id"]),
            # 確定（承認する）は、録音かその場のやり取りを画面で確かめられるときだけ（計画書 18-3 の 2026-10-30 (6)・結果の確かめと同じ1か所）
            buttons=((Button("send_back", "差し戻す"),) + ((Button("approve", "承認する", main=True),) if seen else ())) if d["ask"] else (),
            waiting_why=("" if seen else NO_EVIDENCE_DECISION) if d["ask"] else (approval.ASK_WHY.get(d["ask_why"] or "", "") or "AI が確かめています")))
    return items


def _deficit(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    n = usage.deficit_notice(tx)
    if task is not None or n is None or n["confirmed"]:
        return []
    return [Item(kind="deficit", key={"warning_id": str(n["warning_id"])}, title="赤字の確認",
                 decide="報酬より費用が多くなっていることを確かめたか", evidence=[("今月の費用と報酬", str(n["figures"]))],
                 if_not="発信は止めていません。確かめるまで、決めた時刻に知らせが届きます。",
                 at=n["warned_at"], deadline=n["deadline"], buttons=(Button("confirm", words.DEFICIT_CONFIRM, main=True),), link="/t/costs")]


# 録音を聞いて決める承認（お詫びの電話＝2026-10-24「承認のときに録音を一緒に聞いていただく」）。音は core の recording_audio の1か所（CL-2 の 4-460）。
# 承認のボタンは、録音が置き場にそろっているときだけ出し、押したときは、押した方がすべての部分を聞いた記録（recording_listens）があるかを確かめる
# （ボタンを隠すだけでは、古い画面などから押せてしまう。聞かずに承認できる形を作らない）。
def playable_parts(tx: Tx, set_id: Any) -> list[str] | None:
    """その録音の組の部分（順に）。どれかの音が置き場に無ければ None（聞けない＝承認のボタンを出さない）。"""
    rows = tx.all("SELECT r.part, EXISTS (SELECT 1 FROM salescall.recording_audio x WHERE x.audio_sha256 = r.audio_sha256) AS stored"
                  " FROM salescall.recordings r WHERE r.set_id = %s ORDER BY r.part", (set_id,))
    if not rows or not all(r["stored"] for r in rows):
        return None
    return [r["part"] for r in rows]


def unheard_parts(tx: Tx, set_id: Any) -> list[str]:
    """その録音の組のうち、いま操作している方（取引の方）がまだ聞いていない部分（聞いた記録は core の recording_audio.listen が残す）。"""
    return [r["part"] for r in tx.all(
        "SELECT r.part FROM salescall.recordings r WHERE r.set_id = %s AND NOT EXISTS (SELECT 1 FROM salescall.recording_listens l"
        " WHERE l.set_id = r.set_id AND l.part = r.part AND l.listened_by = salescall.cur_actor()) ORDER BY r.part", (set_id,))]


def _audio(set_id: Any, parts: list[str]) -> tuple[tuple[str, str], ...]:
    return tuple((f"録音 {n}", f"{set_id}/{p}") for n, p in enumerate(parts, start=1))


def _approve_apology(tx: Tx, k: dict[str, str]) -> Any:
    from salescall.web.actions import Refused
    left = unheard_parts(tx, UUID(k["set_id"]))
    if left:
        raise Refused(f"お詫びの録音をすべて聞いてから承認してください（まだ聞いていない録音が {len(left)} つあります）")
    return apology.approve(tx, int(k["order_id"]), UUID(k["set_id"]))


def _apologies(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    items = []
    for o in tx.all("SELECT o.order_id, o.task_id, o.pairs, o.improper, o.text_same_day, o.prepared_at,"
                    "       (SELECT s.set_id FROM salescall.recording_sets s WHERE s.unit = 'apology' AND s.order_id = o.order_id"
                    "         ORDER BY s.created_at DESC LIMIT 1) AS set_id"
                    " FROM salescall.apology_orders o"
                    " WHERE NOT EXISTS (SELECT 1 FROM salescall.apology_approvals a WHERE a.order_id = o.order_id)"
                    "   AND (%s::uuid IS NULL OR o.task_id = %s::uuid)"
                    " ORDER BY o.prepared_at, o.order_id", (task, task)):
        ev = [("お詫びで伝える文", o["text_same_day"])]
        ev += [("誤って伝えたこと → 正しい案内", f"{p['wrong']} → {p['correct']}") for p in (o["pairs"] or [])]
        ev += [("失礼な言葉", w) for w in (o["improper"] or [])]
        parts = playable_parts(tx, o["set_id"]) if o["set_id"] is not None else None
        ready = parts is not None
        why = ("お詫びの録音を作っています" if o["set_id"] is None
               else "お詫びの録音の音がまだそろっていません（聞いてから承認する決まりのため）")
        items.append(Item(
            kind="apology", key={"task_id": str(o["task_id"]), "order_id": str(o["order_id"]), "set_id": str(o["set_id"] or "")},
            title="お詫びの電話の承認", decide="この文と録音で、お客様にお詫びの電話をかけてよいか",
            evidence=ev, if_not="承認するまで、このお客様へは、お約束の電話もかけません。", at=o["prepared_at"],
            **_business_of(tx, o["task_id"]),
            buttons=(Button("approve", "録音を聞いたので承認する", main=True),) if ready else (), waiting_why="" if ready else why,
            audio=_audio(o["set_id"], parts) if ready else ()))
    return items


def _row(tx: Tx, task_id: Any) -> list[tuple[str, str]]:
    """どのお客様の行か（根拠の頭に出す。取り消せない操作は、どのお客様のものかを名前つきで見せる＝Intent.md 5-1）。"""
    t = tx.one("SELECT source_name, target_name, target_role, client_ref FROM salescall.tasks WHERE task_id = %s", (task_id,))
    if t is None:
        return []
    who = (t["target_name"] or "") + (f"（{t['target_role']}）" if t["target_role"] else "")
    return [("お客様", " ／ ".join(x for x in (t["source_name"], who, t["client_ref"] and f"管理番号 {t['client_ref']}") if x))]


def _said(tx: Tx, attempt_id: Any) -> list[tuple[str, str]]:
    said = [u["said"] for u in conversation_io.utterances(tx, attempt_id) if u.get("speaker") == "callee"][-3:]
    return [("お客様の言葉（最後の3つ）", " ／ ".join(said))] if said else []


def _month_ahead(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """1か月以上先のかけ直しの予定で、契約者の答えを待っているもの（答えられるか＝データベースの1か所 task_month_ahead_answerable）。"""
    items = []
    for r in tx.all("SELECT task_id, salescall.task_month_ahead_answerable(task_id) AS call_at FROM salescall.tasks"
                    " WHERE (%s::uuid IS NULL OR task_id = %s::uuid) AND salescall.task_month_ahead_answerable(task_id) IS NOT NULL ORDER BY 2, task_id",
                    (task, task)):
        n = next_action.current(tx, r["task_id"]) or {}
        items.append(Item(
            kind="month_ahead", key={"task_id": str(r["task_id"]), "call_at": r["call_at"].isoformat()}, title="1か月以上先のかけ直し",
            decide="1か月以上先のこの日時に、お電話してよいか",
            evidence=_row(tx, r["task_id"]) + [("お電話する日時", calling_hours.label(r["call_at"])), ("予定の理由", n.get("cause") or "")],
            if_not="お答えが無いまま約束の時刻になったら、お電話します（その前に緊急の知らせが届きます）。",
            at=n.get("decided_at"), deadline=r["call_at"],
            buttons=(Button("refuse", "認めない（別の日にかける）"), Button("approve", "この日時にかける", main=True)),
            **_business_of(tx, r["task_id"])))
    return items


def _old_promises(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """前の担当の方が新しいかけ直しの約束をして、契約者の選びを待っている行（待っているか＝データベースの1か所 task_old_promise_waiting）。"""
    items = []
    for r in tx.all("SELECT task_id, salescall.task_old_promise_waiting(task_id) AS attempt_id FROM salescall.tasks"
                    " WHERE (%s::uuid IS NULL OR task_id = %s::uuid) AND salescall.task_old_promise_waiting(task_id) IS NOT NULL ORDER BY task_id",
                    (task, task)):
        keep = row_fixes.old_promise_keepable(tx, r["task_id"])
        buttons = (Button("new_person", "新しい担当の方へかける", main=not keep),)
        if keep:   # 「約束も守る」を出すかは core の1か所（old_promise_keepable）
            buttons = (Button("new_person", "新しい担当の方へかける"), Button("keep", "前の担当の方との約束を守る", main=True))
        items.append(Item(
            kind="old_promise", key={"task_id": str(r["task_id"]), "attempt_id": str(r["attempt_id"])}, title="前の担当の方との約束",
            decide="前の担当の方とのお約束を守るか、新しい担当の方へかけるか",
            evidence=_row(tx, r["task_id"]) + _said(tx, r["attempt_id"]),
            if_not="選ぶまで、このお客様にはかけません。", buttons=buttons, **_business_of(tx, r["task_id"])))
    return items


def _client(tx: Tx) -> Any:
    """取引の依頼者（決める前に見る画面の住所に使う）。"""
    return tx.one("SELECT salescall.cur_client() AS c")["c"]


def _refused(do: Callable[[Tx, dict[str, str]], Any], *errors: type[Exception]) -> Callable[[Tx, dict[str, str]], Any]:
    """core が理由の文つきで断る部品（既定は ValueError。承認の部分の「確定できない版」など）を、画面の断りにして返す。断りの文は core の
    文をそのまま見せる（「見つかりません」の一言に言い換えない）。"""
    def run(tx: Tx, k: dict[str, str]) -> Any:
        try:
            return do(tx, k)
        except errors or (ValueError,) as e:
            from salescall.web.actions import Refused
            raise Refused(str(e)) from None
    return run


def _task_approvals(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """検証モードの業務で、発信の前の契約者の承認を待っている行（実施回ごとに1件にまとめる。待っているか＝core の approval.uncleared の1か所
    が awaiting_approval と答える行）。行が多いので、行を見てから、その実施回の待っている行をまとめて承認する。"""
    items = []
    if task is not None:   # 実施回でまとめる種類（1行ごとには出さない）
        return items
    for r in tx.all("SELECT r.run_id, r.name, r.business_id, b.name AS business FROM salescall.runs r"
                    " JOIN salescall.businesses b ON b.business_id = r.business_id WHERE (%s::uuid IS NULL OR r.run_id = %s::uuid)"
                    " ORDER BY r.created_at, r.run_id", (run, run)):
        waiting = _awaiting_rows(tx, r["run_id"], r["business_id"])
        if waiting:
            items.append(Item(
                kind="task_approval", key={"run_id": str(r["run_id"]), "digest": _digest(waiting)}, title="発信前の行の承認（検証モード）",
                decide=f"お客様リスト「{r['name']}」の {len(waiting)} 行に、お電話してよいか",
                evidence=[("待っている行", f"{len(waiting)} 行"), ("行を見る", "お客様リストで、会社名・お呼びする方・番号を確かめてください")],
                if_not="承認するまで、この行にはおかけしません（検証モードの業務）。", business_id=r["business_id"],
                link=f"/c/{_client(tx)}/b/{r['business_id']}/runs/{r['run_id']}",
                business_name=r["business"], buttons=(Button("approve_all", f"{len(waiting)} 行すべて承認する", main=True),)))
    return items


def _awaiting_rows(tx: Tx, run_id: Any, business_id: Any) -> list[Any]:
    """その実施回の、発信前の承認を待っている行（取り込んだ順）。決めるのは core の approval.uncleared_in_run の1か所（門が1行ずつ聞く
    approval.uncleared と同じ問い合わせを、実施回まとめて1回で＝CL-2 4-429。⛔ 前は行ごとに uncleared を呼び、行が多い実施回で重かった）。
    business_id は呼ぶ側の形をそろえるために受ける（決め方には使わない）。"""
    return [t for t, why in approval.uncleared_in_run(tx, run_id).items() if why == "awaiting_approval"]


def _digest(task_ids: list[Any]) -> str:
    """待っている行の顔ぶれの印（見せた行と、承認する行が同じかを確かめる。行が増えた・減ったら印が変わり、`perform` が断る）。"""
    return hashlib.sha256(",".join(sorted(str(t) for t in task_ids)).encode()).hexdigest()[:32]


def _approve_waiting_rows(tx: Tx, run_id: UUID) -> int:
    """その実施回の、承認を待っている行をすべて承認する（core の approval.approve_task だけ。待っているかは同じ1か所の答え）。
    見せた行と同じ顔ぶれかは、呼ぶ前に `perform` が印（digest）で確かめている（同じ取引の中なので、ここで読む行は、その顔ぶれのまま）。"""
    run = tx.one("SELECT business_id FROM salescall.runs WHERE run_id = %s", (run_id,))
    if run is None:
        raise LookupError("そのお客様リストは無い")
    waiting = _awaiting_rows(tx, run_id, run["business_id"])
    for task_id in waiting:
        approval.approve_task(tx, task_id)
    return len(waiting)


def viewable_evidence(tx: Tx, attempt_id: Any) -> bool:
    """契約者が、その電話の根拠を画面で確かめられるか（1か所。結果の確かめの「この結果でよい」を出すか、が読む）。
    いまは、その場のやり取りの文字（core の `conversation_io.utterances`）があるとき。✅ 2026-11-20 管理者「ア」：根拠を見ずに
    確定させない（検証モードの承認は誤判定率の正解にもなる＝計画書 16-3。録音の無い通話は別の耳の文字で確かめる＝18-3 の 11-01）。
    ⏳ 録音を画面で聞く仕組みができたら、聞ける録音があるときも真にする（CL-2 へのお願い 13）。"""
    return attempt_id is not None and bool(conversation_io.utterances(tx, attempt_id))


NO_EVIDENCE_DECISION = "「承認する」は、その場のやり取りの文字が届いてから押せます（録音かやり取りを確かめてから確定する決まり）。"
NO_EVIDENCE = ("「この結果でよい」は、その場のやり取りの文字が届いてから押せます（根拠を見ずに確定させないため。押した結果は"
               "誤判定率の正解にもなります）。違うときは「違う（直す）」で直せます。")


def _result_checks(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """検証モードでかけた通話の結果で、契約者の確かめを待っているもの（システムが判定した最新の版・確定の印が無い・判定できないでない・
    確定できる版。確定できるかは core の approval._blocked と同じデータベースの1か所 salescall.release_blocked）。"""
    items = []
    for r in approval.results_to_check(tx, task_id=task, run_id=run):   # 確かめ待ちは core の1か所（CL-2 4-439 の 29）
        biz = _business_of(tx, r["task_id"])
        seen = viewable_evidence(tx, tx.one("SELECT attempt_id FROM salescall.latest_task_results WHERE task_id = %s",
                                            (r["task_id"],))["attempt_id"])
        fix = Button("fix", "違う（直す）", go=links.row(tx, r["task_id"]) + "#result-fix")
        items.append(Item(
            kind="result_check", link=links.row(tx, r["task_id"]), key={"task_id": str(r["task_id"]), "seq": str(r["version_no"])}, title="結果の確かめ（検証モード）",
            decide="この結果でよいか（違えば、お客様の行の画面で直す）",
            # 何が依頼者へ渡るかを、渡す書き方のまま見せる（答えを見ずに「よい」を押させない。押した結果は誤判定率の正解にもなる＝計画書 16-3）
            evidence=_row(tx, r["task_id"]) + [("システムの読み", words.word(words.BUSINESS_STATES, r["business_state"]).screen)]
            + [(f"答え：{q}", a + (f"（お客様の言葉：{said}）" if said else "")) for q, a, said in result_rows.answers_for_check(tx, r["task_id"])],
            if_not="確かめるまで、この結果は依頼者へ渡しません。", at=r["decided_at"],
            # 「よい」と「違う」は同じ重さで並べる（よいの方だけを目立たせて、見ずに押させない）。違えば、行の画面の「結果を直す」を開く
            # 根拠が画面に無いあいだは「よい」を出さない（受け口も、出ていないボタンは断る＝perform）。「違う（直す）」はいつでも
            buttons=(Button("approve", "この結果でよい"), fix) if seen else (fix,), waiting_why="" if seen else NO_EVIDENCE, **biz))
    return items


def _corrections(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """契約者の直しで電話をかけることになり、「かけてよい」の確かめを待っている行（確かめ待ちか＝データベースの1か所
    task_correction_unconfirmed。✅ 2026-11-18 管理者：伺い2「本当に折り返してよいのか確認と承認をもらう。承認なく勝手にかけたらクレームになる
    可能性があるため」。CL-2 4-422）。"""
    items = []
    # 確かめられるのは直した方ご本人だけ（データベースが守る＝0242 の correction_call_confirm_by_corrector。CL-2 4-425）。ボタンは、
    # 確かめ待ちの直しの asked_by が、いまの取引の方（salescall.cur_actor）のときだけ出す。ほかの方には、だれの確かめを待っているかを見せる。
    for r in tx.all("SELECT t.task_id, c.asked_by, c.asked_by = salescall.cur_actor() AS mine FROM salescall.tasks t"
                    " JOIN salescall.correction_calls c ON c.task_id = t.task_id"
                    "  AND c.version_no = salescall.task_correction_unconfirmed(t.task_id)"
                    " WHERE (%s::uuid IS NULL OR t.task_id = %s::uuid) ORDER BY t.task_id", (task, task)):
        n = next_action.current(tx, r["task_id"]) or {}
        ev = _row(tx, r["task_id"]) + [("直したことでかける電話", n.get("cause") or "")]
        if n.get("call_at"):
            ev.append(("かける時刻", calling_hours.label(n["call_at"])))
        items.append(Item(
            kind="correction", key={"task_id": str(r["task_id"]), "call_at": n["call_at"].isoformat() if n.get("call_at") else ""}, title="直しでかける電話の確かめ",
            decide="直した内容で、このお客様にお電話してよいか",
            evidence=ev, if_not="確かめるまで、この電話はかけません（かけないなら、お客様の行の画面で「かけない」にしてください）。",
            at=n.get("decided_at"), buttons=(Button("confirm", "かけてよい", main=True),) if r["mine"] else (),
            waiting_why="" if r["mine"] else f"直した方（{r['asked_by']}）ご本人の確かめを待っています",
            **_business_of(tx, r["task_id"])))
    return items


def _more_calls(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """お客様と約束した電話・お詫びの電話を、決めた日数・回数かけても話せずに終えた行の「さらにかける」の承認（行ごと。承認できるかは core の
    next_action.more_calls_offered の1か所。押すと core の approval.more_calls）。✅ 2026-11-19 管理者：「契約者が承認ボタンを押したら、さらに
    3回かける。それでも繋がらなければ、再度承認ボタンを出す。繋がるまで承認ボタンは出し続ける。。承認ボタンが押されなければかけない」・
    「ダッシュボードの取り込んだ行に承認ボタンを出します」（CL-2 4-427）。お客様リストの行のボタンも、この承認待ちをそのまま出す（同じ鍵・同じ受け口）。
    鍵に終えた予定の番号（end_seq）を入れる：押したあとにまた話せずに終えた行は、新しい番号の別の承認待ちになる（前の画面のボタンでは押せない）。"""
    items = []
    for r in tx.all("SELECT n.task_id, n.end_reason, n.decided_at FROM salescall.current_next_actions n JOIN salescall.tasks t ON t.task_id = n.task_id"
                    " WHERE n.kind = 'end' AND n.end_reason = ANY(%s) AND (%s::uuid IS NULL OR n.task_id = %s::uuid)"
                    " AND (%s::uuid IS NULL OR t.run_id = %s::uuid) ORDER BY n.decided_at, n.task_id",
                    (sorted(next_action.MORE_CALL_ENDS), task, task, run, run)):
        seq = next_action.more_calls_offered(tx, r["task_id"])
        if seq is None:
            continue
        calls = tx.one("SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND state <> 'released'", (r["task_id"],))["n"]
        items.append(Item(
            kind="more_calls", key={"task_id": str(r["task_id"]), "end_seq": str(seq)}, title=MORE_CALLS_TITLE,
            decide="お約束・お詫びの電話で話せずに終えたこのお客様に、さらにおかけするか",
            evidence=_row(tx, r["task_id"]) + [("終えた理由", words.end_reason(r["end_reason"])), ("かけた回数", f"{calls} 回")],
            if_not="押さなければ、このお客様にはおかけしません（終えたままです）。押すと、この行だけ、もう一度かけ始めます"
                   "（お約束の電話は約束の時刻・幅のまま、お詫びの電話は時間帯を変えて決めた回数）。また話せなければ、もう一度ここに出ます。",
            at=r["decided_at"], link=links.row(tx, r["task_id"]),
            buttons=(Button("approve", MORE_CALLS_BUTTON, main=True),), **_business_of(tx, r["task_id"])))
    return items


MORE_CALLS_TITLE = "さらにかける承認"
MORE_CALLS_BUTTON = "さらにかける"


def _solutions(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """その実施回のいまの版の流れに入っていない、その業務の前の依頼者の解決策（実施回ごと。core の client_solutions.not_in_line の1か所）。
    - 手で外して、まだ確かめていないもの（unconfirmed）：確かめるまで、その実施回の新しい電話はかけない（2026-11-07 管理者「契約者が承認を
      しないと自動で入らず、新規の電話もかけられません、ダッシュボードに承認ボタンを分かりやすく表示」）＝「入れる」（add_earlier）と
      「入れない」（leave_out）の両方を出す。
    - ほかの入っていないもの：電話は止まらない。「入れる」を出す（2026-11-06 管理者「契約者にそれをダッシュボードで確認します」＝自動では
      入れない。計画書 6-1 (3)(5)）。
    - 手で外して「入れない」と確かめたもの：もう決まっているので出さない。
    「入れる」に名前を洗い出す処理は要らない（足す文の名前と読みは、core が依頼者で文ごとに1回だけ聞いて残したものを読む＝CL-2 4-428）。"""
    items = []
    if task is not None:   # 実施回ごとの種類
        return items
    for run_row in tx.all("SELECT r.run_id, r.name, r.business_id, b.name AS business FROM salescall.runs r"
                          " JOIN salescall.businesses b ON b.business_id = r.business_id WHERE (%s::uuid IS NULL OR r.run_id = %s::uuid)"
                          " ORDER BY r.created_at, r.run_id", (run, run)):
        for s in client_solutions.not_in_line(tx, run_row["run_id"]):
            if s["removed"] and not s["unconfirmed"]:
                continue
            blocking = bool(s["unconfirmed"])
            items.append(Item(
                kind="solution", key={"run_id": str(run_row["run_id"]), "solution_id": str(s["solution_id"])},
                title="依頼者の解決策の確かめ", decide=f"お客様リスト「{run_row['name']}」の台本に、この解決策を入れるか",
                evidence=[("ご納得いただけなかった点", s["point_text"] or ""), ("問い", s["answer_name"] or ""),
                          ("解決策の文", s["solution_text"] or "")],
                if_not=("確かめるまで、このお客様リストでは新しい電話をかけません（お約束の電話・お詫びの電話は除く）。" if blocking else
                        "入れなくても、このお客様リストの電話は止まりません（入れるまで、この解決策は台本に入りません）。"),
                at=s["recorded_at"], urgent=False, business_id=run_row["business_id"], business_name=run_row["business"],
                buttons=((Button("leave_out", "入れない"), Button("add", "入れる", main=True)) if blocking
                         else (Button("add", "入れる", main=True),))))
    return items


def _fixes(tx: Tx, task: Any = None, run: Any = None) -> list[Item]:
    """担当が「直した」と報告し、管理者がまだ承認していない修正依頼（システムの取引。管理者だけ）。"""
    items: list[Item] = []
    if task is not None:
        return items
    for r in tx.all("SELECT q.request_id, q.assignee, q.summary, q.requested_at, p.note, p.reported_at FROM salescall.fix_requests q"
                    " JOIN salescall.fix_reports p ON p.request_id = q.request_id"
                    " WHERE NOT EXISTS (SELECT 1 FROM salescall.fix_approvals a WHERE a.request_id = q.request_id)"
                    " ORDER BY p.reported_at, q.request_id"):
        items.append(Item(
            kind="fix", key={"request_id": str(r["request_id"])}, title="修正依頼の承認",
            decide="直した中身を確かめて、承認してよいか",
            evidence=[("不具合", words.plain(r["summary"])), ("直した担当", words.plain(r["assignee"])), ("直した中身", words.plain(r["note"]))],
            if_not="承認するまで、この直しは確かめ待ちのまま残ります。", at=r["reported_at"],
            buttons=(Button("approve", "確かめて承認する", main=True),)))
    return items


KINDS: dict[str, Kind] = {
    "apology": Kind("お詫びの電話の承認", _apologies, {
        "approve": (_approve_apology, "承認しました。お詫びの電話をかけます。"),
    }),
    "decision": Kind("取り消せない決定の確かめ", _decisions, {
        "approve": (lambda tx, k: approval.approve_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "承認しました。"),
        "send_back": (lambda tx, k: approval.send_back_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "差し戻しました。"),
    }),
    "month_ahead": Kind("1か月以上先のかけ直し", _month_ahead, {
        "approve": (lambda tx, k: row_fixes.answer_month_ahead(tx, UUID(k["task_id"]), approve=True), "認めました。その日時にお電話します。"),
        "refuse": (lambda tx, k: row_fixes.answer_month_ahead(tx, UUID(k["task_id"]), approve=False),
                   "認めませんでした。別の日に、時刻の決まっていない約束の決まりでお電話します。"),
    }),
    "old_promise": Kind("前の担当の方との約束", _old_promises, {
        "keep": (lambda tx, k: row_fixes.choose_old_promise(tx, UUID(k["task_id"]), keep_old_promise=True), "前の担当の方とのお約束を守ります。"),
        "new_person": (lambda tx, k: row_fixes.choose_old_promise(tx, UUID(k["task_id"]), keep_old_promise=False), "新しい担当の方へおかけします。"),
    }),
    "task_approval": Kind("発信前の行の承認（検証モード）", _task_approvals, {
        "approve_all": (lambda tx, k: _approve_waiting_rows(tx, UUID(k["run_id"])), "承認しました。このお客様リストのお客様におかけします。"),
    }),
    "result_check": Kind("結果の確かめ（検証モード）", _result_checks, {
        "approve": (_refused(lambda tx, k: approval.approve_result(tx, UUID(k["task_id"]), int(k["seq"]))), "この結果で確定しました。"),
    }),
    "correction": Kind("直しでかける電話の確かめ", _corrections, {
        "confirm": (lambda tx, k: approval.confirm_correction_call(tx, UUID(k["task_id"])), "確かめました。直した内容でお電話します。"),
    }),
    "more_calls": Kind(MORE_CALLS_TITLE, _more_calls, {
        # 戻せない行（お詫びの案が無い・業務を終えた）は core が理由の文つきの LookupError で断る＝その文をそのまま見せる
        "approve": (_refused(lambda tx, k: approval.more_calls(tx, UUID(k["task_id"])), LookupError),
                    "承認しました。この行だけ、もう一度おかけします。"),
    }),
    "solution": Kind("依頼者の解決策の確かめ", _solutions, {
        "add": (lambda tx, k: client_solutions.add_earlier(tx, UUID(k["run_id"]), [UUID(k["solution_id"])]),
                "入れました。このお客様リストの台本に、この解決策が入ります。"),
        "leave_out": (lambda tx, k: client_solutions.leave_out(tx, UUID(k["run_id"]), [UUID(k["solution_id"])]),
                      "入れないと確かめました。このお客様リストの電話を続けます。"),
    }),
    "deficit": Kind("赤字の確認", _deficit, {
        "confirm": (lambda tx, k: usage.confirm_deficit(tx, int(k["warning_id"])), "確かめたことを記録しました。"),
    }),
    "fix": Kind("修正依頼の承認", _fixes, {
        "approve": (lambda tx, k: fix_requests.approve(tx, int(k["request_id"])), "承認しました。"),
    }, where="system"),
}


KEY_FIELDS = ("attempt_id", "seq", "warning_id", "order_id", "set_id", "task_id", "run_id", "solution_id", "request_id", "call_at", "digest", "end_seq")   # 受け箱の操作が送り返してよい鍵の名前（ほかの欄は読まない）


def collect(tx: Tx, where: str = "client", task: Any = None) -> list[Item]:
    """その範囲（client＝依頼者の取引／system＝システムの取引）の、すべての種類の承認待ち。task を渡せば、その行のものだけ
    （お客様の行の画面に、受け箱と同じ形で出す）。"""
    return [item for kind in KINDS.values() if kind.where == where for item in kind.collect(tx, task, None)]


GONE = "この承認待ちは、もう済んだか、中身が変わりました。画面を開き直して、いまの中身を確かめてから決めてください。"


def perform(tx: Tx, kind_name: str, action: str, sent: dict[str, str], where: str) -> Any:
    """受け箱のボタンを受ける1か所。同じ取引の中で、その種類の承認待ちを集め直し、送られた鍵とまったく同じ鍵の承認待ちが今もあって、
    そのボタンが今も出ているときだけ決める（見せた中身と決める中身を同じにする：例 1か月先のかけ直しの日時が変わった・実施回に行が
    増えた・お詫びの録音が作り直された・録音を聞けないのでボタンが無い＝どれも断る）。決める部品へは、送られた値ではなく、集め直した
    承認待ちの鍵を渡す。"""
    from salescall.web.actions import Refused
    kind = KINDS.get(kind_name)
    if kind is None or kind.where != where or action not in kind.perform:
        raise LookupError("その承認待ちの種類は無い")
    try:   # 送られた鍵の行・実施回で、集め直す範囲を絞る（その実施回だけを数え直す。絞っても、鍵が同じものは必ず入る）
        task = UUID(sent["task_id"]) if sent.get("task_id") else None
        run = UUID(sent["run_id"]) if sent.get("run_id") else None
    except ValueError:
        raise Refused(GONE) from None
    for item in kind.collect(tx, task, run):
        if all(sent.get(f, "") == v for f, v in item.key.items()):
            if action not in {b.action for b in item.buttons}:
                raise Refused(item.waiting_why or GONE)
            return kind.perform[action][0](tx, item.key)
    raise Refused(GONE)


def ordered(items: list[Item]) -> list[Item]:
    """並べ方の1か所：緊急 → 期限が近い → 古い。"""
    far = datetime.max.replace(tzinfo=timezone.utc)
    def utc(t: datetime | None) -> datetime:   # 時差のある時刻どうしも同じ物差しで比べる（時差を捨てない）
        if t is None:
            return far
        return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda i: (not i.urgent, utc(i.deadline), utc(i.at)))
