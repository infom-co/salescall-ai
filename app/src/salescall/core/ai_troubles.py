"""止まったAI（AIの不具合での一時停止の材料。2026-10-30 深夜 管理者のご決定。0155）。持ち主 CL-2。DESIGN.md 9-1・CL-1 の連絡帳 4-62。

ご決定：「基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止して
ください。ただし、どうしても使わなければいけないシーンが出たときは、契約者以上の権限でOpenRouter を通して Google が預かる Claudeを
使って再開することができるようにしておいてください。」

- 通話で使うAI（会話AI・別の耳・判定）のどれかが止まったかを決めるのは、会話の側の1か所（CL-1 の Flow.on_ai_trouble。同じAI・同じ道・
  同じ出来事は1つ）。ここは、それを受け取る入口の1か所（`note`）：事実の表 attempt_ai_troubles に書き、「止まった」なら、エラーの入口
  （watch.record_error。出どころ ai_trouble）へ渡す。止めるかは処置の表（errors.ACTIONS）と watch.decide_stop の1か所
  （`watch.after_ai_troubles`）。
- 受け取る道は3つで、どれも `note` を通る（同じものは2回書かない＝止める判断を二重にしない）：
  (1) 通話の途中ですぐ（通話ごとの口 `call.ai_trouble(entry)` → 通話ごとの書き手 → `note` → `watch.after_ai_troubles`）。通話の終わりを
      待つと、そのあいだ同じ契約者のほかの回線が、止まったAIで新しい電話をかけてしまうため。
  (2) 会話の報告の `ai_trouble`（conversation_io.save_report。会話の処理がまだ (1) を呼ばない形でも止まる）。
  (3) 通話後の確かめ（下の箇条）。
  止めるかは、どの通話で書いたかを見ずに、その契約者のエラーで決める（`watch.decide_ai_stops`。見直し25a の A）。回線ごとの処理は通話の
  あとにも、見守り役の仕事 ai_stops は定期にも決め直す（どの道で書いても・決める取引が落ちても、止める判断が漏れない）。
- 「緊急の道で答えた」は書くだけ（エラーにしない。止めるのは、その前の「止まった」）。「おかしな動き」（甚大でない＝電話は続けられた）は、
  止めずに警告だけ（エラーの種類 ai_glitch＝契約者と管理者のダッシュボードに知らせる。2026-10-31 管理者のご決定。0160）。
- **通話後の確かめ（別の耳）が止まったときも、同じ入口**（2026-10-31 管理者のご決定：「通話中は止めません。ただし、次の電話はかけず、
  確かめが通れば自動で再開します。…この変更は、AI の判断が、以前よりも影響が大きくなったからです。」。0158）：止まったかを決めるのは、
  会話の側の通話後の確かめの部品（通話中と同じ決め方）。入口は conversation_io.verification_ai_trouble（`note` に stage="verification"
  を付けて通し、すぐ `watch.after_ai_troubles`）。通話の中の分（stage="call"）は、通話の途中の知らせと報告の両方から届くので1つに
  数える。通話後の確かめの分は、確かめのたびに別の出来事として書く（重ねると、再開したあとに同じ通話の確かめがまた止まっても止め直せない）。
"""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from salescall.core import call_report, errors
from salescall.core.access import Tx

STOPPED, ANSWERED_ON_EMERGENCY, GLITCH = call_report.AI_TROUBLE_WHATS   # 会話の報告の言葉の1か所から
# 出来事ごとのエラーの出どころ（「止まった」＝止める材料・「おかしな動き」＝警告だけ。「緊急の道で答えた」は書くだけ＝エラーにしない）
ERROR_SOURCE = {STOPPED: "ai_trouble", GLITCH: "ai_glitch"}
STAGES = {"call": "通話の中", "verification": "通話後の確かめ"}   # どこで止まったか（データベースの守りと同じ名前）


def note(tx: Tx, attempt_id: UUID, troubles: Iterable[call_report.AiTrouble], *, stage: str = "call") -> list[str]:
    """その通話の止まったAIを書く（依頼者の取引で。1か所）。新しく書いた「止まった」を、エラーの入口へ渡し、その種類を返す（通話の中の分で
    もう書いたものは何もしない）。止めるかは `watch.after_ai_troubles`（契約者の取引）。stage＝どこで止まったか（`STAGES`）。"""
    if stage not in STAGES:
        raise ValueError(f"どこで止まったかは {tuple(STAGES)} のどれかです：{stage!r}")
    from salescall.core import watch   # watch → stops → ここ（停止の説明）と読むので、使うときに読む

    kinds = []
    for t in troubles:
        row = tx.one(
            "INSERT INTO salescall.attempt_ai_troubles (tenant_id, client_id, attempt_id, stage, ai, route, what, error, turn)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (attempt_id, ai, route, what) WHERE stage = 'call' DO NOTHING RETURNING attempt_id",
            (attempt_id, stage, t.ai, t.route, t.what, t.error, t.turn))
        if row is None or t.what not in ERROR_SOURCE:
            continue
        message = f"{STAGES[stage]}で {t.ai}（{t.route}）：{t.what}" + ("" if t.error is None else f"：{t.error}")
        kinds.append(watch.record_error(tx, errors.ErrorReport(ERROR_SOURCE[t.what], None, message, ai=t.ai, route=t.route),
                                        attempt_id=attempt_id))
    return kinds


def for_stop(tx: Tx, stop_event_id: int, limit: int = 20) -> list[dict]:
    """AIの不具合の停止の説明に出す、止まったAI（その契約者で、同じ原因の前の解き以後に書いたもの。新しい順）。画面が、止まったAIの
    名前・時刻を出す（CL-3）。期間は、止める数え方と同じ起点（データベースの salescall.errors_counted_since の1か所）から、その停止を
    解いて数え直すまで（⛔ 見直し25b の 3：上の端が無く、解けた古い停止の説明に、あとの停止の止まったAIまで出た）。契約者の取引で読む。"""
    return tx.all(
        "SELECT t.stage, t.ai, t.route, t.what, t.error, t.noted_at, t.attempt_id FROM salescall.attempt_ai_troubles t"
        " JOIN salescall.stop_events s ON s.event_id = %s AND s.tenant_id = t.tenant_id"
        " LEFT JOIN salescall.stop_events r ON r.resolves_event_id = s.event_id"
        " WHERE t.noted_at > salescall.errors_counted_since('ai_connection', s.at)"
        "   AND t.noted_at <= coalesce(r.counted_from, r.at, 'infinity'::timestamptz)"
        " ORDER BY t.noted_at DESC LIMIT %s", (stop_event_id, limit))
