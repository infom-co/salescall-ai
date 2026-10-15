"""会話から返る報告（試行ごとに1つ）。持ち主 CL-2（形は CL-1 の連絡帳 2-2「会話から返すもの」の仮の形・版 0.2）。

- 形と確かめは、この1か所（`from_json`）。書く（`save`。書き込む入口 core/conversation_io.py から呼ぶ）ときも、
  読む（`load`。締めの処理 core/settle.py が読む）ときも、同じ確かめを通す。合わない報告は受け取らない（ReportInvalid）。
- 「わからない」を既定値で埋めない（原則10）：分からないものは None のまま。判定が「決められない」として扱う。
- 名前（相手の種別など）は DESIGN.md 4-3・6-2・7-1c のもの。⚠️ 仮の形：CL-1 の会話の処理を app/ へ移すときに合わせて版を上げる。
- 版 2（2026-10-15）：別の番号のかけ直しに、相手が言ったままの番号（number_said）を必ず持つ。そろえた番号（number_e164）は
  そろえられたときだけ（内線つき・桁が足りないなど、そろえられない番号も、言われたことは記録して依頼主へ渡す。DESIGN.md 3-5）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core.access import Tx
from salescall.core.phone import normalize

VERSION = 2
PARTY_KINDS = ("unknown", "reception", "target", "voicemail", "ivr", "fax", "wrong_party")   # 4-3（区間ごと）
CALLBACK_REASONS = ("absent", "other_number", "later", "ask_who", "ask_when", "our_promise")
# 不在・別の番号・あとで（相手に頼まれた。6-2）／分かる人・分かる時を聞いた（15-2-3 の a1・a2）／こちらが約束した
NOT_REACHED_REASONS = ("absent", "not_transferred")      # 目的の人に繋がらなかった理由（7-1）
SPEAKERS = ("callee", "ai", "none")                      # 切れた瞬間に話していた人（7-1c）


class ReportInvalid(Exception):
    """報告の形が違う（理由は日本語）。"""


@dataclass(frozen=True)
class Callback:
    reason: str
    at: datetime | None          # かけ直す時刻（取れたときだけ。時差つき）
    number_said: str | None      # 別の番号：相手が言ったまま（reason = other_number のときは必ず要る。それ以外は持たない）
    number_e164: str | None      # 別の番号：番号の正規化（Q1）でそろえた番号（そろえられたときだけ）
    person: str | None           # 分かる人（a1）


@dataclass(frozen=True)
class Cut:
    speaker: str | None          # 切れた瞬間に話していた人（分からなければ None）
    in_closing: bool             # 締めの言葉に入っていた・締めのあとだった
    callee_refused_last: bool    # 相手の最後の発話が、断りの言葉だった


@dataclass(frozen=True)
class CallReport:
    parties: tuple[str, ...]     # 区間ごとの相手の種別（並び順）
    do_not_call: bool            # 発信の拒否（営業拒否）の申告
    out_of_scope: bool           # 範囲外の求めで終えた
    complaint: bool              # 苦情・怒り
    hurried: bool                # 相手が急いでいて終えた
    not_reached: str | None      # 目的の人に繋がらなかった理由
    callback: Callback | None
    cut: Cut | None              # 切れ方の材料（相手が出なかった通話は None）

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["parties"] = list(self.parties)
        if self.callback is not None and self.callback.at is not None:
            d["callback"]["at"] = self.callback.at.isoformat()
        d["version"] = VERSION
        return d


def _bool(d: dict, key: str) -> bool:
    v = d.get(key)
    if not isinstance(v, bool):
        raise ReportInvalid(f"{key} は true / false です")
    return v


def _choice(v: Any, allowed: tuple[str, ...], key: str, *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if v not in allowed:
        raise ReportInvalid(f"{key} は {allowed} のどれかです（{v!r}）")
    return v


def _keys(d: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        raise ReportInvalid(f"{where} の形が違います")
    extra = set(d) - allowed
    if extra:
        raise ReportInvalid(f"{where} に知らない項目があります：{sorted(extra)}")
    return d


def from_json(raw: Any) -> CallReport:
    """報告を確かめて読む（ただ1つの確かめ）。"""
    d = _keys(raw, {"version", "parties", "do_not_call", "out_of_scope", "complaint", "hurried", "not_reached",
                    "callback", "cut"}, "報告")
    if d.get("version") != VERSION:
        raise ReportInvalid(f"報告の版が違います（{d.get('version')!r}。この部品は {VERSION}）")
    parties = d.get("parties")
    if not isinstance(parties, list):
        raise ReportInvalid("parties は並びです")
    parties_t = tuple(_choice(p, PARTY_KINDS, "parties") for p in parties)
    not_reached = _choice(d.get("not_reached"), NOT_REACHED_REASONS, "not_reached", optional=True)
    if not_reached is not None and "target" in parties_t:
        raise ReportInvalid("目的の人に繋がったのに、繋がらなかった理由があります")
    cb = None
    if d.get("callback") is not None:
        c = _keys(d["callback"], {"reason", "at", "number_said", "number_e164", "person"}, "callback")
        at = c.get("at")
        if at is not None:
            try:
                at = datetime.fromisoformat(at)
            except (TypeError, ValueError):
                raise ReportInvalid("callback.at は時刻（ISO 8601）です") from None
            if at.tzinfo is None:
                raise ReportInvalid("callback.at は時差つきの時刻です")
        said, number = c.get("number_said"), c.get("number_e164")
        if said is not None and (not isinstance(said, str) or not said.strip()):
            raise ReportInvalid("callback.number_said は、相手が言ったままの番号（文字）です")
        if number is not None and (not isinstance(number, str) or normalize(number).e164 != number):
            raise ReportInvalid("callback.number_e164 は、番号の正規化（Q1）を通した番号です")
        reason = _choice(c.get("reason"), CALLBACK_REASONS, "callback.reason")
        if (reason == "other_number") != (said is not None):
            raise ReportInvalid("別の番号のかけ直しには、相手が言ったままの番号が要り、それ以外には番号を持ちません")
        if number is not None and said is None:
            raise ReportInvalid("そろえた番号だけがあって、相手が言ったままの番号がありません")
        person = c.get("person")
        if person is not None and (not isinstance(person, str) or not person.strip()):
            raise ReportInvalid("callback.person は名前です")
        cb = Callback(reason, at, said, number, person)
    cut = None
    if d.get("cut") is not None:
        c = _keys(d["cut"], {"speaker", "in_closing", "callee_refused_last"}, "cut")
        cut = Cut(_choice(c.get("speaker"), SPEAKERS, "cut.speaker", optional=True), _bool(c, "in_closing"),
                  _bool(c, "callee_refused_last"))
    return CallReport(parties_t, _bool(d, "do_not_call"), _bool(d, "out_of_scope"), _bool(d, "complaint"),
                      _bool(d, "hurried"), not_reached, cb, cut)


def save(tx: Tx, attempt_id: UUID, report: CallReport) -> None:
    """試行の報告を置く（1回だけ。変更不可）。形はもう一度確かめる（書く前の確かめも、読むときと同じ1つ）。"""
    data = from_json(report.to_json()).to_json()
    tx.execute("INSERT INTO salescall.call_reports (tenant_id, client_id, attempt_id, report)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)",
               (attempt_id, json.dumps(data, ensure_ascii=False)))


def load(tx: Tx, attempt_id: UUID) -> CallReport | None:
    row = tx.one("SELECT report FROM salescall.call_reports WHERE attempt_id = %s", (attempt_id,))
    return None if row is None else from_json(row["report"])
