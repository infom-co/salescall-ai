"""依頼者へ渡す1件の結果の行（docs/DESIGN.md 7-3-1 の (1)・7-3-3・7-3-4。持ち主 CL-3）。

- 出力の処理は、判定し直さない・項目の状態を変えない・設定を自分で合成しない。読むのは答える場所の答えだけ（行の進み具合
  `customer_rows.progress`・次の予定の最新・確定の印 `result_releases`・試行）。1つの依頼者の取引の中で、1つの実施回の行だけを並べる。
- **出す結果は確定した版だけ**（確定の印のある版のうち、段が `DELIVERABLE_STAGES` のもの）。✅ 2026-11-18 管理者：サンプル検証（＝模擬通話）は
  渡さない・実検証（＝検証モード）は渡せる・そのあとフルオート（＝本番）。⇒ verification と production。模擬通話は結果の版を作らない。
  ⏳ 依頼者のシステムへの書き戻し（`deliveries`）は、データベースの守りが本番の版だけのまま（CL-2 へのお願い 17）。ファイルはこの決まりで出す。
- **判定待ち（次の予定が hold）の行と、最新の結果の版がまだ確定していない行は出さない**（古い確定の版を、今の答えとして渡さない）。
- ⏳ 質問ごとの2列（答え／お客様の言葉）と判定の中身の列（ほかのご希望 など）は、書き方の変更（2026-11-18 管理者「変更 CL-1かCL-2に
  連絡済み」）が届いてから、この部品の `QUESTION_COLUMNS` の1か所に足す。
- ⏳ 「お客様からのご質問」の列（計画書 11-3「お答えできなかったご質問は必ず載せる」）は、書き方の変更とは別：判定の中身にまだご質問が
  入っていない（CL-2 へのお願い 51）。通話の報告を直に読まない（目的のお客様だと確かめていない方の言葉を渡さない決まりを、判定の1か所で守る）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import business_rules, business_settings, calling_hours, customer_rows, phone
from salescall.core.access import Tx
from salescall.output import words

DELIVERABLE_STAGES: tuple[str, ...] = ("verification", "production")

COLUMNS: tuple[str, ...] = ("管理番号", "会社名", "お呼びする方", "お呼びする方のフルネーム", "お呼びする方の部署", "電話番号", "処理", "結果",
                            "通話の終わり方", "かけた回数", "最後にかけた日時")

# 依頼者に埋めていただく欄（「お名前だけではどの方か分からなかった」行。どの行のどの欄かは core の business_settings.who_unclear_blanks の
# 1か所）。✅ 2026-11-18 管理者「(ア)でOK」：結果のファイルに「フルネーム」「部署」の欄を足し、色の付かない形（CSV）では空欄に
# 「（ご記入ください）」と書き、色の付く形（Excel・画面の見本）では、その欄だけを赤い背景にする（✅ 2026-11-02 管理者「一覧の入れるべき
# 空欄部分が、その行だけ赤背景になると分かりやすいですね」）。欄の名前 → 列の名前の組はここ1か所。
TO_FILL_COLUMNS: dict[str, str] = {"full_name": "お呼びする方のフルネーム", "department": "お呼びする方の部署"}
PLEASE_FILL = "（ご記入ください）"
# 赤い背景の色（1か所。Excel の出力と画面の見本で同じ色＝画面の色の値 --stop-soft・--stop と同じかを試験が見る）。✅ 2026-11-18 管理者「文字が
# 見えにくい奇抜な赤にならないように」⇒ 淡い赤の地に濃い赤の文字。文字が読める差（コントラスト比）4.5 以上を試験が確かめる。
TO_FILL_BACKGROUND = "#FDECEC"
TO_FILL_TEXT = "#C42B2B"


@dataclass(frozen=True)
class ResultRow:
    client_ref: str
    company: str
    person: str
    full_name: str
    department: str
    phone: str
    handling: str          # 処理（済み／対応中）
    result: str            # 結果（確定した版の業務の状態の、依頼者へ出す言葉。まだ無ければ空）
    call_end: str          # 通話の終わり方（7-3-4）
    dialed: int            # かけた回数（発信まで進んだ電話）
    last_at: str           # 最後にかけた日時（日本時間）
    to_fill: tuple[str, ...] = ()   # 依頼者に埋めていただく欄の列の名前（TO_FILL_COLUMNS の値）

    def values(self) -> list[str]:
        """列の順の値（そのまま。埋めていただく欄は空）。"""
        return [self.client_ref, self.company, self.person, self.full_name, self.department, self.phone, self.handling, self.result,
                self.call_end, str(self.dialed), self.last_at]

    def cells(self) -> list[str]:
        """色の付かない形（CSV）の値：埋めていただく欄には PLEASE_FILL を書く。"""
        return [PLEASE_FILL if c in self.to_fill else v for c, v in zip(COLUMNS, self.values())]

    def our_words(self) -> list[str]:
        """こちらが作った言葉（依頼者へ出さない言葉の確かめに通すもの。お客様・依頼者のデータ＝会社名などは入れない）。"""
        return [self.handling, self.result, self.call_end] + ([PLEASE_FILL] if self.to_fill else [])


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
    # 行ごとの最新の版が、渡せる段で確定しているか。最新の版がまだ確定していない行（確かめ待ち・判定できない・直した版の確かめ待ち）は、
    # 古い確定の版を出さずに、行ごと出さない（判定待ちと同じ。古い答えを今の答えとして渡さない）。
    latest = {r["task_id"]: r for r in tx.all(
        "SELECT l.task_id, l.version_no, l.business_state::text AS business_state,"
        "       EXISTS (SELECT 1 FROM salescall.result_releases x WHERE x.task_id = l.task_id AND x.version_no = l.version_no"
        "               AND x.stage = ANY(%s)) AS released"
        " FROM salescall.latest_task_results l JOIN salescall.tasks k ON k.task_id = l.task_id WHERE k.run_id = %s",
        (list(DELIVERABLE_STAGES), run_id))}
    calls = {r["task_id"]: r for r in tx.all(
        "SELECT DISTINCT ON (a.task_id) a.task_id, a.state::text AS state, a.notice, a.hangup_side::text AS hangup_side, a.claimed_at,"
        "       count(*) OVER (PARTITION BY a.task_id) AS dialed"
        " FROM salescall.attempts a JOIN salescall.tasks k ON k.task_id = a.task_id"
        " WHERE k.run_id = %s AND salescall.is_dialed(a.state) ORDER BY a.task_id, a.claimed_at DESC", (run_id,))}
    full_names = {r["task_id"]: r["target_full_name"] for r in tx.all(
        "SELECT task_id, target_full_name FROM salescall.tasks_now WHERE run_id = %s", (run_id,))}
    out = []
    for r in customer_rows.progress(tx, run_id):
        kind = r["next_kind"]
        v = latest.get(r["task_id"])
        if kind == "hold" or (v is not None and not v["released"]):
            continue
        state = v["business_state"] if v is not None else None
        result = (words.BUSINESS_STATES[state].client or "") if state in words.BUSINESS_STATES else ""
        last = calls.get(r["task_id"])
        person = (r["target_name"] or "") + (f"（{r['target_role']}）" if r["target_role"] else "")
        out.append(ResultRow(
            client_ref=r["client_ref"] or "", company=r["source_name"] or "", person=person,
            full_name=full_names.get(r["task_id"]) or "", department=r["target_department"] or "",
            to_fill=tuple(TO_FILL_COLUMNS[f] for f in business_settings.who_unclear_blanks(tx, r["task_id"]))
            if kind == "end" and r["next_end_reason"] == business_rules.WHO_UNCLEAR else (),
            phone=(phone.normalize(r["dial_phone"]).national or r["dial_phone"]) if r["dial_phone"] else "",
            handling=words.NEXT_KINDS["end"].client if kind == "end" else words.NEXT_KINDS["call"].client,
            result=result, call_end=call_end(last, kind), dialed=int(last["dialed"]) if last else 0,
            last_at=calling_hours.label(last["claimed_at"]) if last else ""))
    return out
