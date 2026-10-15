"""番号の確かめ（相手が言った番号が「番号違い」だったとき。DESIGN.md 3-5。2026-10-15 ユーザー決定）。持ち主 CL-2。

ユーザー：「最初にかけた相手が番号を伝え忘れたか、こちらの聞き違いの可能性がある。証拠と付け合わせ、確認の上、それでも間違いなら、
最初の相手に、お聞きした番号が番号違いだったことを告げ、再度、かけ直し先の番号を伺う」。

- 要るのは、**相手が言った番号（行の番号の表で callee）へかけて、番号違い（相手の種別 wrong_party）だった通話**だけ（`needed`）。
- 付け合わせるのは、その通話の通話後の検証（Q21。CL-1）：番号を聞いた通話（`source_attempt`）の証拠（録音・別の耳の文字）と、
  かけた番号を比べ、結果をここで記録する（`record`。書くのはここだけ。変更不可）：
    misheard＝こちらの聞き違い（正しい番号つき）／as_said＝言われたとおりにかけていた／cannot_check＝付け合わせられなかった。
- 要る通話は、記録が無いと通話後の検証を済ませられない（core/attempts.py の `record_verification` が守る）。
  ⇒ 締めの処理は、必ず記録を読める。次の動き（next_action.choose_number）が、記録から次にかける番号を決める。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from salescall.core import call_report
from salescall.core.access import Tx
from salescall.core.phone import normalize

Outcome = Literal["misheard", "as_said", "cannot_check"]
OUTCOMES = ("misheard", "as_said", "cannot_check")


class RecheckInvalid(Exception):
    """記録できない番号の確かめ（理由は日本語）。"""


def dialed(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """その通話でかけた番号の、行の番号の表の行（source・source_attempt_id・phone_e164・contact_id）。"""
    return tx.one(
        "SELECT n.contact_id, n.phone_e164, n.source, n.source_attempt_id FROM salescall.attempts a"
        " JOIN salescall.task_numbers n ON n.task_id = a.task_id AND n.contact_id = a.contact_id"
        " WHERE a.attempt_id = %s", (attempt_id,))


def needed(tx: Tx, attempt_id: UUID) -> bool:
    """番号の確かめが要る通話か：相手が言った番号へかけて、番号違いだった。"""
    number = dialed(tx, attempt_id)
    if number is None or number["source"] != "callee":
        return False
    report = call_report.load(tx, attempt_id)
    return report is not None and "wrong_party" in report.parties


def record(tx: Tx, attempt_id: UUID, outcome: Outcome, *, corrected: str | None, evidence: str,
           checked_by: str) -> None:
    """付け合わせた結果を記録する（通話後の検証 Q21 だけが呼ぶ。1回だけ）。corrected：聞き違いのときの正しい番号。"""
    if not needed(tx, attempt_id):
        raise RecheckInvalid("相手が言った番号へかけて番号違いだった通話ではありません")
    if outcome not in OUTCOMES:
        raise RecheckInvalid(f"結果は {OUTCOMES} のどれかです（{outcome!r}）")
    e164 = None
    if outcome == "misheard":
        n = normalize(corrected) if corrected is not None else None
        if n is None or n.e164 is None:
            raise RecheckInvalid("聞き違いのときは、そろえられる正しい番号が要ります")
        if n.e164 == dialed(tx, attempt_id)["phone_e164"]:
            raise RecheckInvalid("正しい番号が、かけた番号と同じです（聞き違いではありません）")
        e164 = n.e164
    elif corrected is not None:
        raise RecheckInvalid("正しい番号を持てるのは、聞き違いのときだけです")
    if not evidence or not evidence.strip():
        raise RecheckInvalid("何と付け合わせたか（証拠）が要ります")
    tx.execute(
        "INSERT INTO salescall.number_rechecks (tenant_id, client_id, attempt_id, outcome, corrected_e164, evidence,"
        " checked_by) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s)",
        (attempt_id, outcome, e164, evidence, checked_by))


def load(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    return tx.one("SELECT outcome, corrected_e164, evidence FROM salescall.number_rechecks WHERE attempt_id = %s",
                  (attempt_id,))


def asked_again_before(tx: Tx, task_id: UUID, *, except_attempt: UUID) -> bool:
    """この行で、前にも「言われたとおりにかけて番号違い」（＝聞き直しをした）があったか。"""
    return tx.one(
        "SELECT 1 FROM salescall.number_rechecks c JOIN salescall.attempts a ON a.attempt_id = c.attempt_id"
        " WHERE a.task_id = %s AND c.attempt_id <> %s AND c.outcome IN ('as_said', 'cannot_check') LIMIT 1",
        (task_id, except_attempt)) is not None
