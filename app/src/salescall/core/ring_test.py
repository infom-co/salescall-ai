"""テスト番号で、回線で電話が鳴るかを試す（DESIGN.md 5-4・7-2-1・9-1。表の作り 0134）。持ち主 CL-2。

2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、一度も
鳴らせていなければ回線のせいです。（テスト番号はフェーズ1で知らせます。）」「回線が止まったあとの再開の確かめ：OK」

- テスト番号は契約者の設定（`line.test_number`。契約者が用意した、お客様ではない電話。⏳ フェーズ1でユーザーが入れる）。お客様の番号では
  ないので、門・発信禁止・番号の種類の決まりは通らない（その回線で電話が鳴るかを試すだけ）。
- 試すのは `run` の1か所（回線ごとの処理が、その回線の電話の部品で呼ぶ＝電話の部品を持つのは回線ごとの処理だけ。話している回線では
  試さない）。結果は回線ごとの記録（`line_ring_tests`。書くのはここだけ）に残す。
- **鳴ったテストは、その回線で「鳴った電話」と同じ証拠**（データベースの salescall.line_rings の1か所）：番号のせいか回線のせいかの続きの
  区切り（鳴ったテストのあとなら、まだ分からなかった続きは番号のせい）と、回線が直った証拠（回線の異常の数え直し）。**鳴らなかった
  テスト**は、まだ分からない続きがあれば、続きを閉じて回線のせいにする（salescall.close_unrung_run の test_silent）。分からない（電話の
  部品が答えない）テストは、どちらの証拠にもしない（記録だけ）。
- 使う所は2つ：(1) その回線にかける行が、まだ分からない続きを待つ行のほかに残っていない（supervisor/line.py の `_close_if_nothing_left`）
  (2) 回線の異常で止めた回線を再開してよいかの確かめ（止めた停止の確かめ＝core/watch.record_check。鳴れば成功）。テスト番号が無い
  契約者は、(1) は前の区切りで決め（0131 の no_rows）、(2) は確かめの部品（アプリが応答するか）で行う（⏳ テスト番号が入るまで）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from salescall.core import attempts, settings
from salescall.core.access import Access, Actor, Tx

__all__ = ["PURPOSES", "Result", "RingDriver", "checks_stop", "number", "run"]

PURPOSES = ("no_rows", "resume_check")   # データベースの守り（0134）と同じ


class RingDriver(Protocol):
    """テスト番号に鳴るか試す、回線の電話の部品（supervisor.links.CallDriver が持つ）。"""

    def in_call(self) -> bool: ...

    def test_ring(self, phone_e164: str) -> bool | None:
        """その番号へかけ、呼び出しの音が出たら（お客様の電話でいう「鳴った」）、出る前に切って True。鳴らなければ False。分からなければ
        None。話している回線では試さない（None）。"""
        ...


class Result(Enum):
    RANG = "rang"             # 鳴った（回線は元気）
    SILENT = "silent"         # 鳴らなかった（回線のせい）
    UNKNOWN = "unknown"       # 分からない（電話の部品が答えない）：どちらの証拠にもしない
    NO_NUMBER = "no_number"   # テスト番号が無い：試していない
    BUSY = "busy"             # 回線が話している途中・答えない：試していない


def number(tx: Tx) -> str | None:
    """取引の契約者のテスト番号（無ければ None＝試せない）。"""
    tenant_id = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    if tenant_id is None:
        return None
    return settings.resolve(tx, "line.test_number", tenant_id=tenant_id, client_id=None, business_id=None)


def checks_stop(tx: Tx, stop: dict[str, Any]) -> bool:
    """止めた停止（watch.due_checks の1行）を、テスト番号で確かめるか（1か所）：回線の異常で止めた回線の停止で、その契約者にテスト
    番号があれば、その回線の処理がテスト番号で確かめる（鳴れば成功）。ほかは確かめの部品（supervisor.jobs）。"""
    return stop["cause"] == "line_failure" and stop.get("line_id") is not None and number(tx) is not None


def run(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, driver: RingDriver, purpose: str) -> Result:
    """テスト番号で、その回線で電話が鳴るかを試し、記録する（1か所）。鳴らなかったら、まだ分からない続きを回線のせいで閉じる。"""
    if purpose not in PURPOSES:
        raise ValueError(f"試す目的は {PURPOSES} のどれかです：{purpose!r}")
    to = access.in_tenant(actor, tenant_id, number)
    if to is None:
        return Result.NO_NUMBER
    try:
        busy = driver.in_call()
    except Exception:  # noqa: BLE001 答えられない＝分からない：話しているとみなす（試さない側）
        busy = True
    if busy:
        return Result.BUSY
    try:
        rang = driver.test_ring(to)
    except Exception:  # noqa: BLE001 電話の部品の誤り：分からない
        rang = None

    def work(tx: Tx) -> None:
        tx.execute("INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang)"
                   " VALUES (salescall.cur_tenant(), %s, %s, %s)", (line_id, purpose, rang))
        if rang is False:
            attempts.close_unrung_run(tx, line_id, "test_silent")
    access.in_tenant(actor, tenant_id, work)
    return Result.UNKNOWN if rang is None else Result.RANG if rang else Result.SILENT


def last(tx: Tx, line_id: UUID) -> dict[str, Any] | None:
    """その回線のいちばん新しいテスト（画面・試験が読む）。"""
    return tx.one("SELECT tested_at, purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT 1", (line_id,))
