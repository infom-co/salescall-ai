"""承認待ちの受け箱（docs/DASHBOARD.md 4章）。種類ごとの違いは、この表 `KINDS` の1か所に書き、画面の部品は1つ。

人は「確かめて、承認する」だけ（2026-11-01 管理者「人は甚大な問題以外で介入しない」）。どの種類も、同じ形で見せる：
なにを決めるか・根拠・決めないと何が起きるか・ボタン（承認する／差し戻す）。並び順は緊急 → 期限 → 古い順（`ordered`）。

この表に無い承認を、別の画面に作らない。種類を足すときは、ここに1行足す（docs/DASHBOARD.md 4-2 の表と同じ並び）。
⏳ まだ入れていない種類：お詫びの電話・解決策の確かめ・1か月以上先のかけ直し・前の担当の方との約束・録音の承認・修正依頼・名前の
録音の審査（docs/DASHBOARD.md 4-2。読む部品と決める部品は分かっている）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core import approval, conversation_io, usage
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


@dataclass(frozen=True)
class Kind:
    title: str
    collect: Callable[[Tx], list[Item]]                       # 依頼者の取引で、その種類の承認待ちを集める
    perform: dict[str, tuple[Callable[[Tx, dict[str, str]], Any], str]]   # ボタン → (決める部品, 済んだときの言葉)


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
            at=d["waited_at"],
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


KINDS: dict[str, Kind] = {
    "decision": Kind("取り消せない決定の確かめ", _decisions, {
        "approve": (lambda tx, k: approval.approve_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "承認しました。"),
        "send_back": (lambda tx, k: approval.send_back_decision(tx, UUID(k["attempt_id"]), int(k["seq"])), "差し戻しました。"),
    }),
    "deficit": Kind("赤字の確認", _deficit, {
        "confirm": (lambda tx, k: usage.confirm_deficit(tx, int(k["warning_id"])), "確かめたことを記録しました。"),
    }),
}


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
