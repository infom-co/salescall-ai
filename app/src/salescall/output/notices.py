"""契約者・管理者への知らせの送信（2章 Q23 の送る側。持ち主 CL-3。docs/DASHBOARD.md 5-4）。

出来事を台帳に入れるのは core の `notify.emit`（CL-2）。ここは、台帳の出来事を送り、送り方ごとの記録（notification_deliveries）を書くだけ。
ダッシュボードの「知らせ」の画面は、台帳と記録をそのまま読む（画面に出すこと自体は送信ではない）。

決まり（1か所ずつ）：
- **送る順**：`ORDER`。✅ 2026-11-18 管理者「Rakuten Link が最優先、それが使えなければ、チャットワーク」。
- **1つの出来事は1回届けば終わり**（どれかの送り方で sent になったら、もう送らない）。決めるのは `next_step` の1か所。
- **取引の外で送る**：読み書きの層は、ぶつかると取引を最初からやり直すので、取引の中で送ると同じ知らせが2通届きうる。⇒ (1) 取引で次の
  一手を決める (2) 取引の外で1回だけ送る (3) 別の取引で結果を記録する。送る処理は同時に1つだけ（`Access.exclusive`）。
- **文は短く**：日本語のショートメールは1通70文字まで（超えると何通分にも分かれ、その分の料金がかかる）。`text_for` の1か所で作る。
  お客様の電話番号・名前・発話の中身は入れない（台帳の要約は、もともと入れない作り＝CL-2 4-46 の5）。
- **送れなくても止めない**：決めた回数（呼ぶ側が設定から渡す）を使い切ったら gave_up を記録し、画面の「知らせ」に「送れていません」と出す。
  システムの動き（止める・解く・発信）は変わらない。

⏳ まだ無いもの（CL-2・電話接続の担当へのお願い。docs/team/CL-3.md 4章）：送り方「chatwork」の記録の欄（いまの表は sms・dashboard
だけ）／送り先（契約者の番号・チャットワークの部屋）の設定／回数の設定／Rakuten Link でショートメールを送る、PC の部品への頼み方／
見守り役の仕事への組み込み。送り方の部品（`Transport`）は、それぞれができたときに作る。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from salescall.core.access import Access, Actor, Tx
from salescall.output import words

ORDER: tuple[str, ...] = ("sms", "chatwork")   # 送る順（sms＝Rakuten Link のショートメール）
SMS_LIMIT = 70                                   # 日本語のショートメール1通の文字数
HEAD = "【SalesCall】"
TAIL = "…詳しくは画面で"


class SendFailed(Exception):
    """送れなかった（理由は記録に残す。キーの値・お客様の情報を入れない）。"""


class Transport(Protocol):
    """送り方の部品：文を1回送る。送れなければ SendFailed。"""
    def send(self, text: str) -> None: ...


@dataclass(frozen=True)
class Step:
    channel: str
    action: str          # "send"＝送る／"give_up"＝回数を使い切ったと記録する
    attempt_no: int


def text_for(kind: str, summary: str) -> str:
    """知らせの文（1か所）。70文字に収まらなければ、切って「…詳しくは画面で」を付ける。"""
    body = f"{HEAD}{words.notice_kind(kind)}：{' '.join(summary.split())}"
    if len(body) <= SMS_LIMIT:
        return body
    return body[:SMS_LIMIT - len(TAIL)] + TAIL


def next_step(history: Sequence[tuple[str, str]], ready: Sequence[str], max_tries: int) -> Step | None:
    """次の一手（1か所）。history＝これまでの記録 [(送り方, 状態)]、ready＝いま使える送り方、max_tries＝送り方ごとの回数。

    - どれかで sent なら、もう何もしない（1回届けば終わり）。
    - まだ使い切っていない送り方のうち、失敗のいちばん少ないもの（同じなら ORDER の順）で送る。⇒ Rakuten Link が先。失敗したら、
      次はチャットワーク。両方失敗したら、また Rakuten Link から（回数の内で）。
    - 使える送り方がすべて回数を使い切ったら、まだ gave_up を書いていない送り方に gave_up を書く。全部書いたら、何もしない。"""
    if any(status == "sent" for _, status in history):
        return None
    if max_tries < 1:
        raise ValueError("回数は1以上")
    failed = {ch: sum(1 for c, st in history if c == ch and st == "failed") for ch in ORDER}
    gave_up = {ch for ch, st in history if st == "gave_up"}
    usable = [ch for ch in ORDER if ch in ready and ch not in gave_up]
    trying = [ch for ch in usable if failed[ch] < max_tries]
    if trying:
        ch = min(trying, key=lambda c: (failed[c], ORDER.index(c)))
        return Step(ch, "send", _attempt_no(history, ch))
    for ch in usable:
        return Step(ch, "give_up", _attempt_no(history, ch))
    return None


STATE_WORDS = {"sent": "送りました", "trying": "送り直しています", "gave_up": "送れていません", "none": "まだ"}


def delivery_state(statuses: Sequence[str]) -> str:
    """その知らせが届いたか（画面の「届いたか」の1か所）：どれかで sent＝送りました／まだ届かず、使い切った（gave_up）＝送れていません／
    失敗があり、まだ送り直す＝送り直しています／記録が無い＝まだ。⛔ 2026-11-18：画面で「失敗が1つでもあれば送れていません」としていた
    （Rakuten Link で失敗してチャットワークで届いた知らせまで「送れていません」になる）。"""
    if "sent" in statuses:
        return "sent"
    if "gave_up" in statuses:
        return "gave_up"
    return "trying" if "failed" in statuses else "none"


def _attempt_no(history: Sequence[tuple[str, str]], channel: str) -> int:
    return sum(1 for c, _ in history if c == channel) + 1


def _history(tx: Tx, event_id: int) -> list[tuple[str, str]]:
    return [(r["channel"], r["status"]) for r in tx.all(
        "SELECT channel, status FROM salescall.notification_deliveries WHERE event_id = %s ORDER BY delivery_id", (event_id,))]


def _record(tx: Tx, event_id: int, step: Step, status: str, error: str | None) -> None:
    tx.execute("INSERT INTO salescall.notification_deliveries (event_id, channel, status, attempt_no, error)"
               " VALUES (%s, %s, %s, %s, %s)", (event_id, step.channel, status, step.attempt_no, error))


def deliver_tenant_event(access: Access, actor: Actor, tenant_id: Any, event_id: int,
                         transports: Mapping[str, Transport], max_tries: int) -> str:
    """契約者の出来事を1つ送る（1回の一手だけ。呼ぶ側が、送り終わるまで繰り返す）。答え：sent／failed／gave_up／done（もう済み）。
    送る処理は同時に1つだけ（`Access.exclusive`。取れなければ busy＝ほかの処理が送っている）。"""
    with access.exclusive(f"notice:{event_id}") as mine:
        if not mine:
            return "busy"

        def plan(tx: Tx) -> tuple[Step | None, dict[str, Any] | None]:
            e = tx.one("SELECT kind, summary FROM salescall.notification_events WHERE event_id = %s", (event_id,))
            if e is None:
                raise LookupError(f"その知らせは無い（または範囲の外）：{event_id}")
            return next_step(_history(tx, event_id), list(transports), max_tries), e

        step, event = access.in_tenant(actor, tenant_id, plan)
        if step is None:
            return "done"
        if step.action == "give_up":
            access.in_tenant(actor, tenant_id, lambda tx: _record(
                tx, event_id, step, "gave_up", f"{max_tries}回送っても届きませんでした"))
            return "gave_up"
        try:
            transports[step.channel].send(text_for(event["kind"], event["summary"]))   # 取引の外で1回だけ
        except SendFailed as e:
            reason = str(e).strip() or "理由が返っていません"
            access.in_tenant(actor, tenant_id, lambda tx: _record(tx, event_id, step, "failed", reason))
            return "failed"
        access.in_tenant(actor, tenant_id, lambda tx: _record(tx, event_id, step, "sent", None))
        return "sent"
