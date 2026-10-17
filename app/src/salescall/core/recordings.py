"""録音の版と承認の記録（定型の音声の台帳。DESIGN.md 6-13）と、話してよい声。持ち主 CL-2。表の作り 0047。

- 録音は2つの単位（2026-10-15 ユーザー）：契約者の単位（契約者と声の名前ごと。相づち・締めなど、どの依頼者にも同じもの）と、
  依頼者の単位（業務設定の版と声の名前ごと。トークスクリプト）。声の名前ごとに1組（声の型ごとではない）。
- 組は変えられない（作り直しは新しい組）。部分（part）の一覧は会話の側（CL-1）の1か所が決める。ここは、渡された部分をそのまま残す。
- 承認は組ごとに1回。管理者か契約者が聞いて承認する（事前の録音チェック。データベースが守る）。承認した組には部分を足せない。
- **話してよい声は `ready_voices` の1か所で決める**：使う声（使わない印の無い声）のうち、承認した録音が、契約者の単位と、その版の
  依頼者の単位の両方にあり、初めの声の型を持つもの（2026-10-17 ユーザー：「事前の録音チェックになる方向性」「新しい声が増える
  かもしれません」＝声を足しても処理は変えない）。門（core/gate.py の `choose_voice`）だけが使う。
- 流すときに使う組は `current_sets`（それぞれの単位で、いちばん新しく承認した組）。
- **測り終わるまで消さない録音**（`hold`・`release`。表の作り 0056。2026-10-17 ユーザー 案A。Intent.md 11-4 第2版）：検証モードの通話
  （締めのときに付ける）と、誤判定率を測るために抜き取った通話（抜き取る処理が付ける）は、測り終わって外すまで、古い録音を消す処理
  （core/backup.py の `prune`）が消さない。外すのは測定を記録する処理（16-3）が、使い終えた通話について1回だけ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from salescall.core.access import Tx


@dataclass(frozen=True)
class Part:
    """録音の1つの部分（流す単位）。"""
    part: str            # 流す単位の名前（会話の側の名前。例 greeting・closing・Q2）
    spoken_text: str     # 録音の文
    audio_ref: str       # 音声のファイルの置き場の参照
    audio_sha256: str    # 中身の指紋


HOLD_REASONS = ("verification", "sample")   # 検証モードの通話・誤判定率を測るために抜き取った通話


def hold(tx: Tx, attempt_id: UUID, reason: str) -> None:
    """その通話の録音を、測り終わるまで消さない印を付ける（同じ理由は1つ）。"""
    if reason not in HOLD_REASONS:
        raise ValueError(f"録音を残す理由は {HOLD_REASONS} のどれかです（{reason!r}）")
    tx.execute("INSERT INTO salescall.recording_holds (tenant_id, client_id, attempt_id, reason)"
               " SELECT tenant_id, client_id, attempt_id, %s FROM salescall.attempts WHERE attempt_id = %s"
               " ON CONFLICT (attempt_id, reason) DO NOTHING", (reason, attempt_id))


def release(tx: Tx, attempt_ids: list[UUID], reason: str, *, measurement: str) -> int:
    """測り終わった通話の印を外す（どの測定で使い終えたかを書く。1回だけ）。外した数を返す。ほかの理由の印が残っていれば消さない。"""
    if not measurement.strip():
        raise ValueError("使い終えた測定の名前が要ります")
    return len(tx.all("UPDATE salescall.recording_holds SET released_at = now(), released_for = %s"
                      " WHERE attempt_id = ANY(%s) AND reason = %s AND released_at IS NULL RETURNING attempt_id",
                      (measurement, list(attempt_ids), reason)))


def add_voice(tx: Tx, voice_name: str, *, note: str) -> None:
    """声の一覧に声を足す（会話AIのモデルが進化して声が増えたとき。管理者だけ＝データベースが守る）。話し方（声の型）は CL-1 が足す。"""
    tx.execute("INSERT INTO salescall.voices (voice_name, note) VALUES (%s, %s)", (voice_name, note))


def retire_voice(tx: Tx, voice_name: str, *, reason: str) -> None:
    """声に「使わない」の印を付ける（1回だけ。管理者だけ）。門は、その声で話さない。"""
    row = tx.one("UPDATE salescall.voices SET retired_at = now(), retired_reason = %s"
                 " WHERE voice_name = %s AND retired_at IS NULL RETURNING voice_name", (reason, voice_name))
    if row is None:
        raise LookupError(f"使っている声ではありません：{voice_name}")


def create_set(tx: Tx, *, unit: Literal["contractor", "business"], voice_name: str, parts: list[Part],
               version_id: UUID | None = None) -> UUID:
    """録音の組を作る（承認の前）。契約者の単位は取引の契約者、依頼者の単位は取引の依頼者の、その版。"""
    if not parts:
        raise ValueError("録音の部分がありません")
    if len({p.part for p in parts}) != len(parts):
        raise ValueError("同じ名前の部分が2つあります")
    if (unit == "business") != (version_id is not None):
        raise ValueError("依頼者の単位の組には業務設定の版を、契約者の単位の組には版を渡しません")
    set_id = tx.one(
        "INSERT INTO salescall.recording_sets (tenant_id, unit, voice_name, client_id, version_id)"
        " VALUES (salescall.cur_tenant(), %s, %s, CASE WHEN %s = 'business' THEN salescall.cur_client() END, %s)"
        " RETURNING set_id", (unit, voice_name, unit, version_id))["set_id"]
    for p in parts:
        tx.execute("INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)"
                   " SELECT tenant_id, client_id, set_id, %s, %s, %s, %s FROM salescall.recording_sets WHERE set_id = %s",
                   (p.part, p.spoken_text, p.audio_ref, p.audio_sha256, set_id))
    return set_id


def approve(tx: Tx, set_id: UUID) -> None:
    """録音の組を承認する（管理者か契約者が聞いて。データベースが守る）。"""
    row = tx.one("INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by)"
                 " SELECT tenant_id, client_id, set_id, salescall.cur_actor() FROM salescall.recording_sets WHERE set_id = %s"
                 " RETURNING set_id", (set_id,))
    if row is None:
        raise LookupError(f"録音の組がありません（または、取引の範囲の外）：{set_id}")


_APPROVED = ("EXISTS (SELECT 1 FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)"
             " WHERE s.voice_name = v.voice_name AND s.tenant_id = salescall.cur_tenant() AND {where})")


def ready_voices(tx: Tx, version_id: UUID) -> list[str]:
    """その版で話してよい声の名前（名前の順）。決まりはここだけ（上の説明）。"""
    return [r["voice_name"] for r in tx.all(
        "SELECT v.voice_name FROM salescall.voices v"
        " WHERE v.retired_at IS NULL"
        "   AND EXISTS (SELECT 1 FROM salescall.voice_types t WHERE t.voice_name = v.voice_name AND t.default_for_voice)"
        "  AND " + _APPROVED.format(where="s.unit = 'contractor'") +
        "  AND " + _APPROVED.format(where="s.unit = 'business' AND s.version_id = %s") +
        " ORDER BY v.voice_name", (version_id,))]


def current_sets(tx: Tx, version_id: UUID, voice_name: str) -> dict[str, UUID | None]:
    """流すときに使う組（それぞれの単位で、いちばん新しく承認した組）。無ければ None。"""
    def latest(where: str, args: tuple) -> UUID | None:
        row = tx.one("SELECT s.set_id FROM salescall.recording_sets s JOIN salescall.recording_approvals p USING (set_id)"
                     f" WHERE s.voice_name = %s AND s.tenant_id = salescall.cur_tenant() AND {where}"
                     " ORDER BY p.approved_at DESC, s.created_at DESC LIMIT 1", (voice_name, *args))
        return None if row is None else row["set_id"]
    return {"contractor": latest("s.unit = 'contractor'", ()),
            "business": latest("s.unit = 'business' AND s.version_id = %s", (version_id,))}


def parts(tx: Tx, set_id: UUID) -> dict[str, Part]:
    """組の部分（名前 → 部分）。"""
    return {r["part"]: Part(r["part"], r["spoken_text"], r["audio_ref"], r["audio_sha256"]) for r in tx.all(
        "SELECT part, spoken_text, audio_ref, audio_sha256 FROM salescall.recordings WHERE set_id = %s ORDER BY part",
        (set_id,))}
