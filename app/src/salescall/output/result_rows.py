"""依頼者へ渡す1件の結果の行（docs/DESIGN.md 7-3-1 の (1)・7-3-3・7-3-4。持ち主 CL-3）。

- 出力の処理は、判定し直さない・項目の状態を変えない・設定を自分で合成しない。読むのは答える場所の答えだけ（行の進み具合
  `customer_rows.progress`・次の予定の最新・確定の印 `result_releases`・試行）。1つの依頼者の取引の中で、1つの実施回の行だけを並べる。
- **出す結果は確定した版だけ**（確定の印のある版のうち、段が `DELIVERABLE_STAGES` のもの）。✅ 2026-11-18 管理者：サンプル検証（＝模擬通話）は
  渡さない・実検証（＝検証モード）は渡せる・そのあとフルオート（＝本番）。⇒ verification と production。模擬通話は結果の版を作らない。
  ⏳ 依頼者のシステムへの書き戻し（`deliveries`）は、データベースの守りが本番の版だけのまま（CL-2 へのお願い 17）。ファイルはこの決まりで出す。
- **判定待ち（次の予定が hold）の行は出さない**（判定し直してから）。
- ⏳ 質問ごとの2列（答え／お客様の言葉）と判定の中身の列（ご質問・ほかのご希望 など）は、書き方の変更（2026-11-18 管理者「変更 CL-1かCL-2に
  連絡済み」）が届いてから、この部品の `QUESTION_COLUMNS` の1か所に足す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import calling_hours, customer_rows, phone
from salescall.core.access import Tx
from salescall.output import words

DELIVERABLE_STAGES: tuple[str, ...] = ("verification", "production")

COLUMNS: tuple[str, ...] = ("管理番号", "会社名", "お呼びした方", "電話番号", "処理", "結果", "通話の終わり方", "かけた回数", "最後にかけた日時")


@dataclass(frozen=True)
class ResultRow:
    client_ref: str
    company: str
    person: str
    phone: str
    handling: str          # 処理（済み／対応中）
    result: str            # 結果（確定した版の業務の状態の、依頼者へ出す言葉。まだ無ければ空）
    call_end: str          # 通話の終わり方（7-3-4）
    dialed: int            # かけた回数（発信まで進んだ電話）
    last_at: str           # 最後にかけた日時（日本時間）

    def cells(self) -> list[str]:
        return [self.client_ref, self.company, self.person, self.phone, self.handling, self.result, self.call_end,
                str(self.dialed), self.last_at]

    def our_words(self) -> list[str]:
        """こちらが作った言葉（依頼者へ出さない言葉の確かめに通すもの。お客様・依頼者のデータ＝会社名などは入れない）。"""
        return [self.handling, self.result, self.call_end]


def call_end(last: dict[str, Any] | None, next_kind: str | None) -> str:
    """通話の終わり方（最後の電話の事実だけから。7-3-4 の上から順に、最初に当たったもの）。言葉は output/words の1か所。"""
    if last is None:
        return words.CALL_END_CLIENT["no_call_ended" if next_kind == "end" else "not_yet"]
    state, notice = last["state"], last.get("notice")
    if notice and f"notice:{notice}" in words.CALL_END_CLIENT:
        return words.CALL_END_CLIENT[f"notice:{notice}"]
    if state == "dropped":
        return words.CALL_END_CLIENT.get(f"side:{last.get('hangup_side')}", words.CALL_END_CLIENT["side:unknown"])
    w = words.ATTEMPT_STATES.get(state)
    return w.client if w is not None and w.client else words.CALL_END_CLIENT["unknown"]


def rows(tx: Tx, run_id: UUID) -> list[ResultRow]:
    """その実施回の、依頼者へ渡す結果の行（一覧の行番号の順。判定待ちの行は出さない）。"""
    released = {r["task_id"]: r["business_state"] for r in tx.all(
        "SELECT DISTINCT ON (x.task_id) x.task_id, t.business_state::text AS business_state FROM salescall.result_releases x"
        " JOIN salescall.task_results t ON t.client_id = x.client_id AND t.task_id = x.task_id AND t.version_no = x.version_no"
        " JOIN salescall.tasks k ON k.task_id = x.task_id WHERE k.run_id = %s AND x.stage = ANY(%s)"
        " ORDER BY x.task_id, x.version_no DESC", (run_id, list(DELIVERABLE_STAGES)))}
    calls = {r["task_id"]: r for r in tx.all(
        "SELECT DISTINCT ON (a.task_id) a.task_id, a.state::text AS state, a.notice, a.hangup_side::text AS hangup_side, a.claimed_at,"
        "       count(*) OVER (PARTITION BY a.task_id) AS dialed"
        " FROM salescall.attempts a JOIN salescall.tasks k ON k.task_id = a.task_id"
        " WHERE k.run_id = %s AND salescall.is_dialed(a.state) ORDER BY a.task_id, a.claimed_at DESC", (run_id,))}
    out = []
    for r in customer_rows.progress(tx, run_id):
        kind = r["next_kind"]
        if kind == "hold":
            continue
        state = released.get(r["task_id"])
        result = (words.BUSINESS_STATES[state].client or "") if state in words.BUSINESS_STATES else ""
        last = calls.get(r["task_id"])
        person = (r["target_name"] or "") + (f"（{r['target_role']}）" if r["target_role"] else "")
        out.append(ResultRow(
            client_ref=r["client_ref"] or "", company=r["source_name"] or "", person=person,
            phone=(phone.normalize(r["dial_phone"]).national or r["dial_phone"]) if r["dial_phone"] else "",
            handling=words.NEXT_KINDS["end"].client if kind == "end" else words.NEXT_KINDS["call"].client,
            result=result, call_end=call_end(last, kind), dialed=int(last["dialed"]) if last else 0,
            last_at=calling_hours.label(last["claimed_at"]) if last else ""))
    return out
