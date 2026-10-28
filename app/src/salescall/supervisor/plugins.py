"""中央の見守り役（サーバー）が呼ぶ、ほかの担当の部品の形（DESIGN.md 5-4・5-5）。持ち主 CL-2（形だけ。中身は各担当）。

どの部品も、設定に書いた `モジュール:名前`（部品を返す関数）から作る（`load` の1か所）。通話を進める部品は、回線ごとに、回線の ID と
その回線の電話の部品（PC の部品へ頼む形＝`links.Links.driver`。通話の声もここを通る）を受け取って作る。電話の部品そのもの（スマホの
アプリ・音声の機器）は PC の部品の側（`salescall.link.phone`）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from salescall.core.access import Access, Actor, Tx
from salescall.core.gate import Claim
from salescall.link.basics import load   # 部品の作り方は link.basics の1か所（PC の部品と同じ）

__all__ = ["CallControl", "CallRunner", "Prober", "Verifier", "WriteFailed", "load"]


class WriteFailed(Exception):
    """通話ごとの書き手が書けなかった書き込み（`CallControl.flush` が返す。0141）。failed＝(頼んだ書き込み, 誤り) の並び（頼んだ順）。
    受け口の決まりに合わない（core/conversation_io.REFUSED）なら直して頼み直し、データベースの誤りなら同じ書き込みをもう一度頼める。
    返した分は、書き手はもう持たない。"""

    def __init__(self, failed: tuple[tuple[Callable[[Tx], Any], BaseException], ...]) -> None:
        super().__init__(f"書けなかった書き込みが {len(failed)} 件あります（" + "、".join(type(e).__name__ for _, e in failed) + "）")
        self.failed = failed


class CallControl(Protocol):
    """通話ごとの口（回線ごとの処理が通話ごとに作って、通話を進める部品へ渡す。supervisor/line.py の `_Call`）。データベースを通さない。"""

    def beat(self) -> None:
        """「動いています」の知らせ。通話のあいだ、**1秒より短い間隔で**呼び続ける（話している間も、お客様の返事を待つ間も、保留で黙って
        待つ間も止めない）。設定 call.frozen_after（初め3秒）途切れたら、こちらのプログラムが固まったとみなし、回線ごとの処理がお詫びの
        録音で終える（end_for_trouble の frozen と同じ道。2026-10-24 ユーザー：「20秒でも長すぎるでしょ」→ 3秒）。**通話を進める部品の、
        通話を進める流れの中から呼ぶ**（別の流れの時計で呼ぶと、進める流れが固まっても知らせが続き、見張れない）。見張るのは試行が
        進行中のあいだだけ（お客様が切ったあとの片付けでは、途切れても終えない）。"""
        ...

    def end_for_trouble(self, reason: str) -> None:
        """その電話そのものに甚大な問題があるとき、その電話をお詫びして終える（2026-10-24 ユーザー決定：「プログラムやAIがこちらの意図しない
        相手がおかしいと思うような状況に陥った場合全般や、回線が悪く、声がお互いや片方がほとんど聞き取れない場合など規定通りの会話が
        進められない状態です。」）。reason＝core.attempts.TROUBLES のどれか（ai_malfunction・poor_audio・cannot_proceed。frozen は回線
        ごとの処理だけが使う）。通話を途中で切るときの2つの一言（line_trouble_ask→お客様の返事→line_trouble_bye）を流して切り、試行を
        結果不明にして理由を残す（1回だけ。2回目からは何もしない）。呼んだら、通話を進める部品は、その通話の処理を終えて戻る。
        どの理由も、こちらのせい（番号にも回線にも数えない）。お客様へは、復旧次第すぐ、かけた予定のままかけ直す（お客様が出たあとなら
        お詫びとともに。復旧＝同じ契約者で、こちらの不具合なしにお客様と話し終えた電話がある・直したプログラムの版に替わった。それまで
        その行は待つ＝データベースの salescall.awaiting_recovery の1か所。0141。2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さ
        ないといけないでしょ！」）。呼んだあとは戻ること（戻らなければ、設定 call.stuck_after で回線の処理を立て直し、CL-1 へ修正依頼）。"""
        ...

    def write(self, work: Callable[[Tx], Any]) -> None:
        """通話の途中のデータベースへの書き込み（work(tx)。その依頼者の取引。答えは `conversation_io.item_work(…)`・報告は
        `conversation_io.report_work(…)`＝受け口の決まりのうちデータベースを読まずに分かる確かめを、頼むその場で行い、合わなければ
        その場で誤りを返す。状態の移りは `lambda tx: attempts.move(tx, …)`）を、通話ごとの書き手に頼む（**待たずに戻る**。頼んだ順に書く）。
        通話を進める流れが書き込みを待たないので、データベースが遅くても「動いています」の知らせが途切れない（0136）。結果を読む
        必要のある読み書き（通話の前の材料を読む など）は、今まで通り access で行い、通話の途中では避ける。
        **書けなかった分は、書き手が持って `flush` で返す**（WriteFailed。0141。⛔ 前は記録して捨てたので、通話の部品は知らずに通話を
        終えた）。通話を終えて戻る前に `flush` を呼んで受け取ること（受け取らずに戻った分は、回線ごとの処理が記録する）。データベースの
        誤りは、受け取らなくても見守り役の誤りとして記録する。通話を進める部品が戻ると、回線ごとの処理が書き終えるまで待ってから締める。
        通話を途中で終えると決めるとき（知らせの途切れ・`end_for_trouble`）も、頼まれた書き込みを書き切ってから（設定 call.end_write_wait
        まで）、通話がまだ進行中かを見る（話し終えた電話におわびを流さない）。"""
        ...

    def flush(self) -> None:
        """頼んだ書き込みを、すべて書き終えるまで待つ（書いた結果を続けて読むとき・通話を終えて戻る前）。書けなかった分があれば
        WriteFailed で返す（0141）。"""
        ...


class CallRunner(Protocol):
    """通話を最後まで進める部品（会話＝CL-1。サーバーで動く。作るときに受け取った回線の電話の部品で、通話の声を受け渡す）。
    発信の出口（dial_exit.dial）がかけたあとに呼ぶ。
    試行の状態を終わりまで移し（attempts.move）、答え・報告・「もうかけないで」の申告は、書き込む入口（core/conversation_io.py）
    だけを通して書く：答えは決まるたびに `write_item`、報告は `save_report`（版3＝通話の事実の形）、申告はその場で
    `declare_do_not_call`（原則14）。エラーは自分でエラーの入口
    （watch.after_error）へ渡す。外へ出た誤りは、見守り役が「見守り役の誤り」として記録し、その試行を結果不明にする。
    通話ごとの口 `call`（`CallControl`）：通話のあいだ `call.beat()` を1秒より短い間隔で呼び続け、甚大な問題があれば
    `call.end_for_trouble(理由)` を呼んで戻る。**通話の長さでは切らない**（2026-10-24 ユーザー：時間で業務を打ち切らない）。"""

    def run(self, access: Access, actor: Actor, claim: Claim, *, tenant_id: UUID, client_id: UUID, call: CallControl) -> None: ...


class Verifier(Protocol):
    """通話後の検証の部品（CL-1。Q21）。待ちの通話を確かめ、attempts.record_verification・finish_without_recording を呼ぶ。"""

    def run_pending(self, access: Access, actor: Actor, *, tenant_id: UUID) -> None: ...


class Prober(Protocol):
    """停止を解く確かめの部品（会話AI＝CL-1・回線＝電話接続）。確かめた結果（うまくいったか・根拠の短い文）を返す。"""

    def probe(self, stop: dict[str, Any]) -> tuple[bool, str]: ...

