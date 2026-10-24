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
- 使う所は2つ：(1) 番号のせいか回線のせいかがまだ分からない続きがあり、その回線にまもなくかけられる行が無い（supervisor/line.py の
  `_decide_if_stuck`） (2) 回線の異常で止めた回線を再開してよいかの確かめ（止めた停止の確かめ＝core/watch.record_check。鳴れば成功）。
  テスト番号で決められない契約者（`usable` が False＝テスト番号が無い・続けて「分からない」）は、(1) は前の区切りで決め（0131 の
  no_rows）、(2) は確かめの部品（アプリが応答するか）で行う。
- **鳴らしてよい時間**（設定 `line.test_ring_hours`。契約者ごと。初め 7:00〜22:00。表の作り 0135）：2026-10-24 ユーザー：「私の電話
  です。軌道に乗れば、新しい電話をもうひとつ契約予定なのでその後に考えます。この仕事専用なら夜でもOK」。時間の外では鳴らさない
  （`run` の1か所＝(1)(2) のどちらも）。(2) は、時間の外ではアプリが応答するかだけの弱い確かめで代わりもしない（`checks_stop` は
  時間に関係なくテスト番号で確かめる停止と答える＝確かめの部品は触らない）。時間の始まりまで待ち、待った分は確かめに数えない
  （記録しない＝間隔も伸びない）。「止まったまま1時間以上」の修正依頼も、鳴らしてよい時間の長さで数える（`checkable_seconds`。
  core/watch.lasting_stops。夜の待ちで修正依頼を出さない）。
- **鳴らす間（契約者ごと・回線をまたいで1つ。`wait_seconds` の1か所。0136）**：テスト番号はいまユーザー自身の電話なので、続けて
  鳴らさない。前のテストから設定 `line.test_ring_gap`（初め30分）あけ、続けて「鳴った」以外（鳴らなかった・分からない）だった
  テストの数だけ倍にして、`line.test_ring_gap_max`（初め4時間）で止める。鳴れば初めの間に戻る。回線をまたいで同時に鳴らさないよう、
  1つの契約者のテストは1本ずつ（`_LOCKS`。中央の見守り役の中の1か所）。
  ⏳ 30分・4時間は仮の値（ユーザーに伺う）。
- **結果は3つ**（電話の部品の約束＝link/phone.py の test_ring）：鳴った（回線は元気）／鳴らなかった（回線のせい＝呼び出しの音が
  出なかった）／分からない（テスト番号の電話が話し中・電源が切れている・圏外・呼び出しの音が出たか見分けられない）。分からないは
  どちらの証拠にもしない（記録だけ）。**分からないが設定 `line.test_ring_unknown_limit`（初め3回）続いたら**（契約者ごと・回線をまたいで
  数える＝`unknown_run`）、テスト番号で決めるのをやめ（`usable`＝False。(1) は前の区切りで決め、(2) はアプリの確かめに戻す）、契約者
  と管理者に1回知らせる（`test_ring_unknown`。続きごとに1回）。そのあいだも、鳴らす間が来れば試し、鳴った・鳴らなかったが出れば
  元に戻る。⏳ 3回は仮の値（ユーザーに伺う）。
"""

from __future__ import annotations

import threading
from datetime import timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from salescall.core import attempts, calling_hours, notify, settings
from salescall.core.access import Access, Actor, Tx

__all__ = ["PURPOSES", "Result", "RingDriver", "allowed", "checkable_seconds", "checks_stop", "hours", "number", "run",
           "unknown_run", "usable", "wait_seconds"]

PURPOSES = ("no_rows", "resume_check")   # データベースの守り（0134）と同じ


class RingDriver(Protocol):
    """テスト番号に鳴るか試す、回線の電話の部品（supervisor.links.CallDriver が持つ）。"""

    def in_call(self) -> bool: ...

    def test_ring(self, phone_e164: str) -> bool | None:
        """その番号へかけ、呼び出しの音が出たら（お客様の電話でいう「鳴った」）、出る前に切って True。呼び出しの音が出ずに終わった
        （回線のせい）なら False。テスト番号の電話が話し中・電源が切れている・圏外、呼び出しの音が出たか見分けられないときは None
        （分からない。False にしない＝回線のせいの証拠にしない）。話している回線では試さない（None）。"""
        ...


class Result(Enum):
    # 試した結果（3つ。記録する）
    RANG = "rang"             # 鳴った（回線は元気）
    SILENT = "silent"         # 鳴らなかった（回線のせい）
    UNKNOWN = "unknown"       # 分からない（テスト番号の電話が話し中・電源が切れている・圏外・見分けられない）：どちらの証拠にもしない
    # 試していない（記録しない）
    NO_NUMBER = "no_number"   # テスト番号が無い
    BUSY = "busy"             # 回線が話している途中・答えない
    OFF_HOURS = "off_hours"   # 鳴らしてよい時間の外（時間の始まりまで待つ）
    WAIT = "wait"             # 鳴らす間がまだ来ていない（`wait_seconds`）


_LOCKS: dict[UUID, threading.Lock] = {}   # 契約者ごとに、テストは1本ずつ（回線をまたいで同時に鳴らさない）
_LOCKS_GUARD = threading.Lock()


def _lock(tenant_id: UUID) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(tenant_id, threading.Lock())


def _scope(tx: Tx) -> dict[str, Any]:
    return {"tenant_id": tx.one("SELECT salescall.cur_tenant() AS t")["t"], "client_id": None, "business_id": None}


def number(tx: Tx) -> str | None:
    """取引の契約者のテスト番号（無ければ None＝試せない）。"""
    scope = _scope(tx)
    if scope["tenant_id"] is None:
        return None
    return settings.resolve(tx, "line.test_number", **scope)


def hours(tx: Tx) -> tuple[int, int]:
    """取引の契約者の、テスト番号を鳴らしてよい時間（設定 line.test_ring_hours。0時からの秒の組。終わりが始まりより前なら日をまたぐ）。"""
    a, b = settings.resolve(tx, "line.test_ring_hours", **_scope(tx))
    return calling_hours.clock_seconds(a), calling_hours.clock_seconds(b)


def wait_seconds(tx: Tx) -> float:
    """取引の契約者が、つぎにテスト番号を鳴らしてよいまでの秒（0＝いま鳴らしてよい。1か所。どの回線・どの目的のテストも数える）：
    前のテストから、初めの間（line.test_ring_gap）に、続けて「鳴った」以外だったテストの数だけ倍をかけた間（line.test_ring_gap_max まで）。"""
    scope = _scope(tx)
    base = float(settings.resolve(tx, "line.test_ring_gap", **scope))
    top = float(settings.resolve(tx, "line.test_ring_gap_max", **scope))
    rows = tx.all("SELECT tested_at, rang, now() AS now FROM salescall.line_ring_tests WHERE tenant_id = salescall.cur_tenant()"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT 16")
    if not rows:
        return 0.0
    run = next((i for i, r in enumerate(rows) if r["rang"]), len(rows))   # 続けて「鳴った」以外だった数（新しいほうから）
    gap = min(base * 2 ** run, top)
    return max(0.0, (rows[0]["tested_at"] + timedelta(seconds=gap) - rows[0]["now"]).total_seconds())


def unknown_run(tx: Tx) -> list[dict[str, Any]]:
    """取引の契約者の、いちばん新しいテストから続いた「分からない」のすべて（test_id・tested_at。新しい順。回線をまたいで数える）。"""
    return tx.all("SELECT test_id, tested_at FROM salescall.line_ring_tests t WHERE t.tenant_id = salescall.cur_tenant()"
                  "   AND t.rang IS NULL AND NOT EXISTS (SELECT 1 FROM salescall.line_ring_tests k"
                  "                                      WHERE k.tenant_id = t.tenant_id AND k.rang IS NOT NULL"
                  "                                        AND (k.tested_at, k.test_id) > (t.tested_at, t.test_id))"
                  " ORDER BY tested_at DESC, test_id DESC")


def usable(tx: Tx) -> bool:
    """取引の契約者が、テスト番号で決められるか（1か所）：テスト番号があり、「分からない」が決めた回数（line.test_ring_unknown_limit）
    続いていない。False なら、まだ分からない続きは前の区切りで決め（supervisor/line.py）、止めた回線の確かめはアプリの確かめに戻す
    （`checks_stop`）。"""
    if number(tx) is None:
        return False
    return len(unknown_run(tx)) < int(settings.resolve(tx, "line.test_ring_unknown_limit", **_scope(tx)))


def allowed(tx: Tx) -> bool:
    """いま、テスト番号を鳴らしてよい時間か（1か所。時刻はデータベースの時計）。"""
    return calling_hours.in_daily(calling_hours.local(tx.one("SELECT now() AS t")["t"]), hours(tx))


def checkable_seconds(tx: Tx, since: Any) -> float:
    """since からいままでのうち、テスト番号を鳴らしてよい時間の秒（＝テスト番号で確かめられた長さ。「止まったまま1時間以上」を数える
    core/watch.lasting_stops が読む。夜の待ちは数えない）。"""
    now = tx.one("SELECT now() AS t")["t"]
    return calling_hours.daily_seconds(calling_hours.local(since), calling_hours.local(now), hours(tx))


def checks_stop(tx: Tx, stop: dict[str, Any]) -> bool:
    """止めた停止（watch.due_checks の1行）を、テスト番号で確かめるか（1か所）：回線の異常で止めた回線の停止で、その契約者が
    テスト番号で決められれば（`usable`）、その回線の処理がテスト番号で確かめる（鳴れば成功）。ほかは確かめの部品（supervisor.jobs）。"""
    return stop["cause"] == "line_failure" and stop.get("line_id") is not None and usable(tx)


def run(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, driver: RingDriver, purpose: str) -> Result:
    """テスト番号で、その回線で電話が鳴るかを試し、記録する（1か所）。鳴らなかったら、まだ分からない続きを回線のせいで閉じる。
    分からないが決めた回数続いたら、契約者と管理者に1回知らせる。"""
    if purpose not in PURPOSES:
        raise ValueError(f"試す目的は {PURPOSES} のどれかです：{purpose!r}")
    to = access.in_tenant(actor, tenant_id, number)
    if to is None:
        return Result.NO_NUMBER
    if not access.in_tenant(actor, tenant_id, allowed):   # 鳴らしてよい時間の外：鳴らさず、記録もしない（時間の始まりまで待つ）
        return Result.OFF_HOURS
    with _lock(tenant_id):   # 契約者ごとに1本ずつ（間を見てから記録するまで）
        if access.in_tenant(actor, tenant_id, wait_seconds) > 0:
            return Result.WAIT
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
            if rang is None and not usable(tx):   # 分からないが続いた：続きごとに1回（鍵は続きの最初のテスト）
                first = unknown_run(tx)[-1]
                notify.emit(tx, "test_ring_unknown", key=f"test_ring_unknown:{first['test_id']}", admin=True,
                            summary="テスト番号の電話が、続けて「分からない」（話し中・電源が切れている・圏外など）でした。"
                                    "テスト番号の電話を確かめてください。そのあいだ、回線のせいか番号のせいかは前の電話で決め、"
                                    "止めた回線はアプリの確かめで再開します。",
                            refs={"line_id": line_id, "since": first["tested_at"]})
        access.in_tenant(actor, tenant_id, work)
    return Result.UNKNOWN if rang is None else Result.RANG if rang else Result.SILENT


def last(tx: Tx, line_id: UUID) -> dict[str, Any] | None:
    """その回線のいちばん新しいテスト（画面・試験が読む）。"""
    return tx.one("SELECT tested_at, purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT 1", (line_id,))
