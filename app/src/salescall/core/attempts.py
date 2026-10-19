"""試行（1回の発信）の状態を移す処理（DESIGN.md 4-2）。持ち主 CL-2。

試行の行を書き換えるのは、この部品だけ（I-12・I-27。app/checks が見張る）。呼ぶ側と、使ってよい処理：
- 回線の部品（呼び出し中・応答・会話中・締め・終わり）と復旧（結果不明）  ⇒ `move`
- 発信の出口（予約 → 発信）                                              ⇒ `start_dialing`
- 門（期限切れの予約を戻す・門の前提が変わった予約を取り消す）           ⇒ `release`
- 通話後の検証（Q21。待ち → 済み）                                        ⇒ `record_verification`
- 締めの処理（core/settle.py。判定が決めた切れ方を1回だけ）                ⇒ `record_disconnect`
移ってよい組・期限・時刻・検証の状態の決まりは、データベースが持つ（ここで2つ目の決まりを作らない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from salescall.core import business_rules, number_recheck, settings, watch
from salescall.core.access import Tx
from salescall.core.errors import ErrorReport

def dialed(table: str = "") -> str:
    """発信まで進んだ試行の条件（SQL。予約のまま・かけずに取り消した予約は入れない）。決まりはデータベースの salescall.is_dialed の
    1か所（データベースの関数も同じものを使う）。table＝表の別名（無ければ state）。回数・間隔・「その回にかけたか」・続けて何回
    かけたか などは、どれもこれを使う。"""
    return f"salescall.is_dialed({table + '.' if table else ''}state)"


DIALED = dialed()


def placed(table: str = "") -> str:
    """発信まで進み、回線の側でかけられなかった（call_failed）ものを除いた試行の条件（SQL。1か所）。お客様のところで電話が
    鳴った（鳴りえた）電話：約束の回に「かけたか」・その回にかけた数・時刻の決まっていない約束の電話の数は、どれもこれを使う。"""
    return f"{dialed(table)} AND {table + '.' if table else ''}state <> 'call_failed'"

# 回線の部品と復旧が移してよい先（予約 → 発信・予約の取り消しは、それぞれ専用の処理だけ）
LineState = Literal["ringing", "answered", "in_conversation", "ending", "ended",
                    "busy", "no_answer", "call_failed", "dropped", "technical_error"]
HangupSide = Literal["callee", "line", "unknown"]


class AttemptNotFound(Exception):
    """その試行は無い（または、取引の依頼者の範囲の外）。"""


def move(tx: Tx, attempt_id: UUID, to_state: LineState, *, hangup_side: HangupSide | None = None) -> dict[str, Any]:
    """回線の部品・復旧が、試行の状態を移す。移れない組なら、データベースが拒む（Rejected）。"""
    if to_state in ("claimed", "dialing", "released"):
        raise ValueError(f"{to_state} へは、この処理では移せません（予約は門、発信は出口、取り消しは門の処理）")
    row = tx.one(
        "UPDATE salescall.attempts SET state = %s, hangup_side = %s WHERE attempt_id = %s"
        " RETURNING attempt_id, state, verification, hangup_side, ended_at",
        (to_state, hangup_side, attempt_id),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    return row


def start_dialing(tx: Tx, attempt_id: UUID) -> bool:
    """発信の出口だけが呼ぶ。予約が「まだ予約のまま・期限内」なら発信中にし True。それ以外は False（発信しない）。

    確かめと書き換えを1つの書き換えで行う（間に、ほかの処理が割り込めない。DESIGN.md 5-3）。
    """
    row = tx.one(
        "UPDATE salescall.attempts SET state = 'dialing'"
        " WHERE attempt_id = %s AND state = 'claimed' AND claim_expires_at > now() RETURNING attempt_id",
        (attempt_id,),
    )
    return row is not None


def release(tx: Tx, attempt_ids: list[UUID]) -> int:
    """まだ発信していない予約を取り消す（期限切れ・門の前提が変わった＝停止・発信禁止・行を終えた・版を替えた・緊急停止の印。
    DESIGN.md 5-2-1 の2・3）。"""
    if not attempt_ids:
        return 0
    return tx.execute(
        "UPDATE salescall.attempts SET state = 'released' WHERE attempt_id = ANY(%s) AND state = 'claimed'",
        (list(attempt_ids),),
    ).rowcount


class RecheckMissing(Exception):
    """番号の確かめ（DESIGN.md 3-5）が要る通話なのに、記録が無い。"""


@dataclass(frozen=True)
class ForbiddenFinding:
    """言ってしまった、言ってはいけないこと（2つの耳で確かめたもの）。"""
    item: str                 # 業務の決まりの、言ってはいけないことの名前（core/business_rules.py の forbidden）
    said: str                 # AI が実際に言ったこと（AI の発話だけ。お客様の発話は入れない）


@dataclass(frozen=True)
class VerificationSummary:
    """通話後の検証の結果の数（品質の段の指標になる。DESIGN.md 10-1）。"""
    items_checked: int        # 2つの耳で確かめた項目の数
    items_disagreed: int      # そのうち、2つの耳が食い違った項目の数
    forbidden_speech: int     # 言ってはいけないこと（2つの耳で確かめたもの）の数
    findings: tuple[ForbiddenFinding, ...] = ()   # その中身（数と同じだけ。お詫びの電話の文面を作るのに使う）
    recording_missing: bool = False  # 録音が届かないまま待つ時間を過ぎ、確かめずに済ませた（`finish_without_recording` だけが付ける）

    def __post_init__(self) -> None:
        if len(self.findings) != self.forbidden_speech:
            raise ValueError("言ってはいけないことの数と、その中身（findings）の数が合いません")
        if self.recording_missing and (self.items_checked or self.items_disagreed or self.forbidden_speech):
            raise ValueError("録音なしで済ませた確かめは、確かめた数を持ちません")


def record_verification(tx: Tx, attempt_id: UUID, *, summary: VerificationSummary) -> None:
    """通話後の検証（Q21）だけが呼ぶ。「待ち」から「済み」へ。検証の結果の数（summary）を必ず一緒に残す（データベースも守る）。
    - 確かめができないときは、ここを呼ばない（「待ち」のまま）。できない原因は仕組みの不具合（別の耳につながらない・誤り・録音が
      無い）なので、エラーの入口（core/watch.py の `after_error`）に渡し、直ってからやり直す（2026-10-17 ユーザー：「そんな状態自体が
      問題では？」。前は「確かめられなかった」で、その行を終えていた）。
    - お客様が言った番号へかけて番号違いだった通話は、番号の確かめ（core/number_recheck.py）を記録してからでないと済ませられない
      （付け合わせられなかったときも cannot_check として記録する）。"""
    if summary is None:
        raise ValueError("検証の結果の数を、必ず渡します")
    if number_recheck.needed(tx, attempt_id) and number_recheck.load(tx, attempt_id) is None:
        raise RecheckMissing("番号違いだった番号の確かめ（証拠との付け合わせ）を、先に記録してください")
    tx.execute(
        "INSERT INTO salescall.verification_summaries (tenant_id, client_id, attempt_id, items_checked, items_disagreed,"
        " forbidden_speech, recording_missing) SELECT tenant_id, client_id, attempt_id, %s, %s, %s, %s FROM salescall.attempts"
        " WHERE attempt_id = %s",
        (summary.items_checked, summary.items_disagreed, summary.forbidden_speech, summary.recording_missing, attempt_id))
    if summary.findings:
        version = tx.one("SELECT version_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["version_id"]
        known = business_rules.load(tx, version).forbidden
        for n, f in enumerate(summary.findings, 1):
            if f.item not in known:
                raise ValueError(f"言ってはいけないことの名前 {f.item!r} は、この版の業務の決まりにありません")
            if not f.said.strip():
                raise ValueError("言ってしまったこと（said）が空です")
            tx.execute("INSERT INTO salescall.forbidden_findings (tenant_id, client_id, attempt_id, seq, item, said)"
                       " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s)",
                       (attempt_id, n, f.item, f.said.strip()))
    row = tx.one(
        "UPDATE salescall.attempts SET verification = 'done' WHERE attempt_id = %s RETURNING attempt_id",
        (attempt_id,),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))


class RecordingWaitNotOver(Exception):
    """まだ録音を待つ時間の中（録音なしでは済ませない）。"""


def finish_without_recording(tx: Tx, attempt_id: UUID) -> None:
    """録音が届かないまま、通話の終わりから待つ時間（設定 verification.recording_wait＝10分）を過ぎた通話の確かめを、「録音なし」で
    済ませる（通話後の検証が呼ぶ。2026-10-18 ユーザー決定）。そのあと締めの処理が、通話中の報告だけで結果を決める。
    - 待つ時間を過ぎたかを決めるのは、ここの1か所。まだなら RecordingWaitNotOver（待ちのまま）。
    - 番号違いの確かめが要る通話は、付け合わせられないので cannot_check として記録する。
    - 録音が残らないこと自体は仕組みの不具合なので、エラーの入口に渡す（修正依頼。その通話は待たない）。"""
    wait = int(settings.system_value(tx, "verification.recording_wait"))
    row = tx.one("SELECT verification, ended_at IS NOT NULL AND ended_at + make_interval(secs => %s) <= now() AS over"
                 " FROM salescall.attempts WHERE attempt_id = %s", (wait, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    if row["verification"] != "pending":
        raise ValueError(f"確かめが待ちの通話ではありません（{row['verification']}）")
    if not row["over"]:
        raise RecordingWaitNotOver(f"通話の終わりから {wait} 秒たっていません（録音を待ちます）")
    if number_recheck.needed(tx, attempt_id) and number_recheck.load(tx, attempt_id) is None:
        number_recheck.record(tx, attempt_id, "cannot_check", corrected=None, evidence="録音が届かなかった（付け合わせられない）",
                              checked_by="通話後の検証（録音なし）")
    watch.record_error(tx, ErrorReport("recording", None, "録音が届かないまま、待つ時間を過ぎた"), attempt_id=attempt_id)
    record_verification(tx, attempt_id, summary=VerificationSummary(0, 0, 0, recording_missing=True))


def forbidden_speech(tx: Tx, attempt_id: UUID) -> bool:
    """その通話で、言ってはいけないことを言ったか（通話後の検証の数。判断はデータベースの1か所 salescall.said_forbidden）。"""
    return tx.one("SELECT salescall.said_forbidden(%s) AS f", (attempt_id,))["f"]


def record_disconnect(tx: Tx, attempt_id: UUID, disconnect: Literal["normal", "refused", "interrupted"]) -> None:
    """締めの処理（core/settle.py）だけが呼ぶ。判定が決めた切れ方（DESIGN.md 7-1c）を、終わった試行に1回だけ記録する。"""
    row = tx.one("UPDATE salescall.attempts SET disconnect = %s WHERE attempt_id = %s RETURNING attempt_id",
                 (disconnect, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
