"""承認待ちの受け箱（docs/DASHBOARD.md 4章）。種類ごとの違いは、この表 `KINDS` の1か所に書き、画面の部品は1つ。

人は「確かめて、承認する」だけ（2026-11-01 管理者「人は甚大な問題以外で介入しない」）。どの種類も、同じ形で見せる：
なにを決めるか・根拠・決めないと何が起きるか・ボタン（承認する／差し戻す）。並び順は緊急 → 期限 → 古い順（`ordered`）。

この表に無い承認を、別の画面に作らない。種類を足すときは、ここに1行足す（docs/DASHBOARD.md 4-2 の表と同じ並び）。
⏳ まだ入れていない種類：解決策の確かめ・1か月以上先のかけ直し・前の担当の方との約束・録音の承認・修正依頼・名前の
録音の審査（docs/DASHBOARD.md 4-2。読む部品と決める部品は分かっている）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core import approval, apology, conversation_io, tasks, usage
from salescall.core.access import Tx
from salescall.output import words


@dataclass(frozen=True)
class Button:
    action: str        # 送る名前（`KINDS[種類].perform` の鍵）
    label: str
    main: bool = False


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


@dataclass(frozen=True)
class Kind:
    title: str
    collect: Callable[[Tx], list[Item]]                       # 依頼者の取引で、その種類の承認待ちを集める
    perform: dict[str, tuple[Callable[[Tx, dict[str, str]], Any], str]]   # ボタン → (決める部品, 済んだときの言葉)


def _business_of(tx: Tx, task_id: Any) -> dict[str, Any]:
    """行の業務（Item の business_id・business_name）。どの業務かは core の1か所（`tasks.scope`）を読む（行 → 実施回 → 業務を、
    ここで書き直さない）。名前は、その業務の名前をそのまま。"""
    bid = tasks.scope(tx, task_id)["business_id"]
    name = tx.one("SELECT name FROM salescall.businesses WHERE business_id = %s", (bid,))
    return {"business_id": bid, "business_name": name["name"] if name else ""}


def _decisions(tx: Tx) -> list[Item]:
    items = []
    for d in approval.pending_decisions(tx):
        parts = "・".join(words.DECISION_PARTS.get(p, words.NO_WORD) for p in (d["parts"] or []))
        said = [u["said"] for u in conversation_io.utterances(tx, d["attempt_id"]) if u.get("speaker") == "callee"][-3:]
        ev = [("待っている中身", parts), ("種類", words.DECISION_KINDS.get(d["kind"], words.NO_WORD))]
        if said:
            ev.append(("お客様の言葉（最後の3つ）", " ／ ".join(said)))
        items.append(Item(
            kind="decision", key={"attempt_id": str(d["attempt_id"]), "seq": str(d["seq"])},
            title="取り消せない決定の確かめ", decide=f"「{parts}」として確定してよいか",
            evidence=ev, if_not="決まるまで、このお客様にはかけません。",
            at=d["waited_at"], **_business_of(tx, d["task_id"]),
            buttons=(Button("send_back", "差し戻す"), Button("approve", "承認する", main=True)) if d["ask"] else (),
            waiting_why="" if d["ask"] else (approval.ASK_WHY.get(d["ask_why"] or "", "") or "AI が確かめています")))
    return items


def _deficit(tx: Tx) -> list[Item]:
    n = usage.deficit_notice(tx)
    if n is None or n["confirmed"]:
        return []
    return [Item(kind="deficit", key={"warning_id": str(n["warning_id"])}, title="赤字の確認",
                 decide="報酬より費用が多くなっていることを確かめたか", evidence=[("数", str(n["figures"]))],
                 if_not="発信は止めていません。確かめるまで、決めた時刻に知らせが届きます。",
                 at=n["warned_at"], deadline=n["deadline"], buttons=(Button("confirm", "確かめた", main=True),))]


# 録音を聞く仕組み（録音の置き場の参照 audio_ref から音を読み出す部品）は、まだ無い（CL-2・CL-1 へのお願い）。お詫びの電話は「承認のときに
# 録音を一緒に聞いていただく」と決まっている（2026-10-24）ので、聞けるようになるまで承認のボタンを出さない（聞かずに承認できる形を作らない）。
CAN_PLAY_RECORDINGS = False


def _apologies(tx: Tx) -> list[Item]:
    items = []
    for o in tx.all("SELECT o.order_id, o.task_id, o.pairs, o.improper, o.text_same_day, o.prepared_at,"
                    "       (SELECT s.set_id FROM salescall.recording_sets s WHERE s.unit = 'apology' AND s.order_id = o.order_id"
                    "         ORDER BY s.created_at DESC LIMIT 1) AS set_id"
                    " FROM salescall.apology_orders o"
                    " WHERE NOT EXISTS (SELECT 1 FROM salescall.apology_approvals a WHERE a.order_id = o.order_id)"
                    " ORDER BY o.prepared_at, o.order_id"):
        ev = [("お詫びで伝える文", o["text_same_day"])]
        ev += [("誤って伝えたこと → 正しい案内", f"{p['wrong']} → {p['correct']}") for p in (o["pairs"] or [])]
        ev += [("失礼な言葉", w) for w in (o["improper"] or [])]
        ready = o["set_id"] is not None and CAN_PLAY_RECORDINGS
        why = ("お詫びの録音を作っています" if o["set_id"] is None
               else "録音を聞く仕組みを準備しています（聞いてから承認する決まりのため）")
        items.append(Item(
            kind="apology", key={"order_id": str(o["order_id"]), "set_id": str(o["set_id"] or "")},
            title="お詫びの電話の承認", decide="この文と録音で、お客様にお詫びの電話をかけてよいか",
            evidence=ev, if_not="承認するまで、このお客様へは、お約束の電話もかけません。", at=o["prepared_at"],
            **_business_of(tx, o["task_id"]),
            buttons=(Button("approve", "承認する", main=True),) if ready else (), waiting_why="" if ready else why))
    return items


KINDS: dict[str, Kind] = {
    "apology": Kind("お詫びの電話の承認", _apologies, {
        "approve": (lambda tx, k: apology.approve(tx, int(k["order_id"]), UUID(k["set_id"])), "承認しました。お詫びの電話をかけます。"),
    }),
    "decision": Kind("取り消せない決定の確かめ", _decisions, {
        "approve": (lambda tx, k: approval.approve_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "承認しました。"),
        "send_back": (lambda tx, k: approval.send_back_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "差し戻しました。"),
    }),
    "deficit": Kind("赤字の確認", _deficit, {
        "confirm": (lambda tx, k: usage.confirm_deficit(tx, int(k["warning_id"])), "確かめたことを記録しました。"),
    }),
}


KEY_FIELDS = ("attempt_id", "seq", "warning_id", "order_id", "set_id")   # 受け箱の操作が送り返してよい鍵の名前（ほかの欄は読まない）


def collect(tx: Tx) -> list[Item]:
    """依頼者の取引で、すべての種類の承認待ち。"""
    return [item for kind in KINDS.values() for item in kind.collect(tx)]


def ordered(items: list[Item]) -> list[Item]:
    """並べ方の1か所：緊急 → 期限が近い → 古い。"""
    far = datetime.max.replace(tzinfo=None)
    def key(i: Item) -> tuple[bool, Any, Any]:
        d = i.deadline.replace(tzinfo=None) if i.deadline else far
        a = i.at.replace(tzinfo=None) if i.at else far
        return (not i.urgent, d, a)
    return sorted(items, key=key)
