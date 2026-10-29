"""テスト番号で、回線で電話が鳴るかを試す（DESIGN.md 5-4・7-2-1・9-1。表の作り 0134）。持ち主 CL-2。

2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、一度も
鳴らせていなければ回線のせいです。（テスト番号はフェーズ1で知らせます。）」「回線が止まったあとの再開の確かめ：OK」

- テスト番号は契約者の設定（`line.test_number`。契約者が用意した、お客様ではない電話。⏳ フェーズ1でユーザーが入れる）。お客様の番号では
  ないので、門・発信禁止・番号の種類の決まりは通らない（その回線で電話が鳴るかを試すだけ）。
- 試すのは `run` の1か所（回線ごとの処理が、その回線の電話の部品で呼ぶ＝電話の部品を持つのは回線ごとの処理だけ。話している回線では
  試さない）。結果は回線ごとの記録（`line_ring_tests`。書くのはここだけ）に残す。
- **鳴ったテストは、その回線で「鳴った電話」と同じ証拠**（データベースの salescall.line_rings の1か所）：番号のせいか回線のせいかの続きの
  区切り（鳴ったテストのあとなら、まだ分からなかった続きは番号のせい）と、回線が直った証拠（回線の異常の数え直し）。**鳴らなかった
  テスト**は、まだ分からない続きがあれば、続きを閉じて回線のせいにする（salescall.close_unrung_run の test_silent）。見分けられない
  テストは、どちらの証拠にもしない（記録だけ。あける長さのあとで、また鳴らす）。
- 使う所は2つ：(1) 番号のせいか回線のせいかがまだ分からない続きがあり、その回線にまもなく（`decide_within`）かけられる行が無い
  （supervisor/line.py の `_decide_if_stuck`。違う番号の数にはよらない＝0143） (2) 回線の異常で止めた回線を再開してよいかの確かめ（止めた
  停止の確かめ＝core/watch.record_check。1回鳴れば解く＝2026-10-25 夜 管理者「鳴ったら、テストＯＫなだけ」。0143）。テスト番号が無い
  契約者（`usable` が False）は、(1) は決めずに待ち、契約者と管理者に1回「テスト番号が無いので決められません」と知らせ（0143。⛔ 0131〜0141：
  前の区切りで決めた＝壊れた回線のお客様の行を失いえた）、(2) は確かめの部品（アプリが応答するか）で行う。
- **鳴らしてよい時間**（設定 `line.test_ring_hours`。契約者ごと。1日の中の時刻の範囲の並び＝値の形 daily_ranges。初め 7:00〜22:00
  の1つ。表の作り 0135・0139）：2026-10-24 ユーザー：「私の電話です。軌道に乗れば、新しい電話をもうひとつ契約予定なのでその後に
  考えます。この仕事専用なら夜でもOK」。2026-10-25 ユーザー：「止める回線を2択だけじゃなく、こちらが決められるようにして。昼間に私は
  寝ていることがあるし、夜中でも起きていることがある。0～24時の選択は残したうえで。」＝1日に何か所でも・日をまたいでも・0〜24時も。
  時刻の決まりは calling_hours.daily_spans の1か所（`in_daily`・`daily_seconds`）。範囲の重なり・空の並びは、データベースの値の形の
  確かめ（setting_value_ok の daily_ranges）が断る。前の形（範囲1つ）の値は 0139 が並びに移した（読み口は並びの形だけを読む）。
  時間の外では鳴らさない（`run` の1か所＝(1)(2) のどちらも）。(2) は、時間の外ではアプリが応答するかだけの弱い確かめで代わりもしない
  （`checks_stop` は時間に関係なくテスト番号で確かめる停止と答える＝確かめの部品は触らない）。時間の始まりまで待ち、待った分は確かめに
  数えない（記録しない＝間隔も伸びない）。「止まったまま1時間以上」の修正依頼も、鳴らしてよい時間の長さで数える（`checkable_seconds`。
  core/watch.lasting_stops。夜の待ちで修正依頼を出さない）。
- **鳴らす間（契約者ごと・回線をまたいで1つ。`wait_seconds` の1か所。0139・0141）**：テスト番号はいまユーザー自身の電話なので、
  鳴らなかった・見分けられなかったあとは続けて鳴らさない：続けて1回目のあと設定 `line.test_ring_gaps`（長さの並び。初め 15→30→60→120→
  240分）の1つ目、2回目のあと2つ目…（最後の長さで止まる）。鳴ったら、テストは OK で終わり、あける長さは無い（2026-10-25 夜 管理者の
  決定。⛔ 0139：鳴ったあとも15分あけていた。⛔ 0136 の「初め30分・倍ずつ・最大4時間」は外した）。回線を
  またいで同時に鳴らさないよう、1つの契約者のテストは1本ずつ（データベースの鍵＝Access.exclusive。契約者ごと。見守り役の入れ替えで
  新旧が少し重なっても同時に鳴らさない。⛔ 見直し20a：前はプログラムの中だけの鍵だった）。ほかが鳴らしている途中なら、鳴らさずに
  WAIT（記録しない）。
- **結果は3つ**（電話の部品の約束＝link/phone.py の test_ring。0141）。**試さなかった**（PC の部品がつながっていない・答えが期限に
  届かない・PC の上の緊急停止の印＝電話の部品が CallWithheld を上げる）は結果ではない：記録せず、あける長さも伸ばさない（`Result.WITHHELD`。
  PC がつながらないことは、ふつうの PC の見張り＝agents.watch_down が知らせる。⛔ 0134〜0142：「見分けられない」として記録し、あける長さを
  伸ばし、誤った知らせを出した＝見直し21a の 10）。結果の3つ鳴った＝回線から発信できた（呼び出しの音・話し中の音・電源オフや
  圏外のお知らせの声が返った＝電話網がこの回線の発信を受け付けた＝回線は元気。決まっていない続きは番号のせい）／鳴らなかった＝回線から
  発信できない（アプリが発信できない・呼び出しもお知らせも無いまま終わった＝回線のせい）／見分けられない（どちらの証拠にもしない。
  記録だけ。あける長さのあとで、また鳴らす。電話の部品が誤りを返したときも、これ）。2026-10-25 夜 管理者：「テスト番号が話し中・電源
  オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」。⛔ 0136〜0140：話し中・電源オフ・圏外を「分からない」とし、
  「分からない」が設定 line.test_ring_unknown_limit（3回）続いたらテスト番号で決めるのをやめて前の区切りで決めていた（0141 で外した）。
- **見分けられないと返ったら、すぐ知らせる（`_tell_undecided` の1か所。0141・0143。2026-10-25 夜 管理者「OK」）**：決めにいくテストでも
  再開の確かめでも、見分けられなかったら、契約者と管理者にすぐ1回知らせる（`line_undecided`。鍵は、決めにいくテストは回線と続きの始まり・
  再開の確かめは止めた停止＝同じ続き・同じ停止で何度見分けられなくても1回）。あわせて、電話をつなぐ部品の不具合として直す担当
  （errors.PHONE_FIXER）へ修正依頼を出す（同じ不具合で1つ）。⛔ 0141：決めにいくテストだけ知らせ、修正依頼は出さなかった。テスト番号が
  無いときは、決めにいく回に1回「テスト番号が無いので、回線のせいか番号のせいか決められません」と知らせる（続きごとに1回）。鳴らしてよい
  時間の外は、時間の始まりまで待つ（知らせない）。
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from salescall.core import attempts, calling_hours, errors, fix_requests, notify, settings
from salescall.core.access import Access, Actor, Tx
from salescall.link.phone import CallWithheld

__all__ = ["PURPOSES", "Result", "RingDriver", "allowed", "checkable_seconds", "checks_stop", "decide_within", "hours",
           "number", "run", "usable", "wait_seconds"]

PURPOSES = ("no_rows", "resume_check")   # データベースの守り（0134）と同じ


class RingDriver(Protocol):
    """テスト番号に鳴るか試す、回線の電話の部品（supervisor.links.CallDriver が持つ）。"""

    def in_call(self) -> bool: ...

    def test_ring(self, phone_e164: str) -> bool | None:
        """その番号へかけ、この回線から発信できたか（意味は link/phone.Phone.test_ring の1か所）：True＝発信できた（呼び出しの音・
        話し中の音・電源オフや圏外のお知らせの声）・False＝発信できない（回線のせい）・None＝見分けられない。話している回線では試さない
        （None）。試さなかった（つながっていない・答えが期限に届かない・PC の上の緊急停止）は CallWithheld を上げる（0143）。"""
        ...


class Result(Enum):
    # 試した結果（3つ。記録する）
    RANG = "rang"             # 鳴った＝回線から発信できた（呼び出しの音・話し中の音・電源オフや圏外のお知らせ。回線は元気）
    SILENT = "silent"         # 鳴らなかった＝回線から発信できない（回線のせい）
    UNKNOWN = "unknown"       # 見分けられない：どちらの証拠にもしない（あける長さのあとで、また鳴らす）
    # 試していない（記録しない）
    NO_NUMBER = "no_number"   # テスト番号が無い（決めにいく回は、契約者と管理者に1回知らせる）
    WITHHELD = "withheld"     # PC の部品が試さなかった（つながっていない・答えが期限に届かない・PC の上の緊急停止）
    BUSY = "busy"             # 回線が話している途中・答えない
    OFF_HOURS = "off_hours"   # 鳴らしてよい時間の外（時間の始まりまで待つ）
    WAIT = "wait"             # 鳴らす間がまだ来ていない（`wait_seconds`）・ほかの回線が鳴らしている途中


def _lock_key(tenant_id: UUID) -> str:
    """契約者ごとに、テストは1本ずつ（回線をまたいで・見守り役をまたいで同時に鳴らさない）のデータベースの鍵の名前。"""
    return f"salescall.ring_test:{tenant_id}"


def _scope(tx: Tx) -> dict[str, Any]:
    return {"tenant_id": tx.one("SELECT salescall.cur_tenant() AS t")["t"], "client_id": None, "business_id": None}


def number(tx: Tx) -> str | None:
    """取引の契約者のテスト番号（無ければ None＝試せない）。"""
    scope = _scope(tx)
    if scope["tenant_id"] is None:
        return None
    return settings.resolve(tx, "line.test_number", **scope)


def hours(tx: Tx) -> tuple[tuple[int, int], ...]:
    """取引の契約者の、テスト番号を鳴らしてよい時間（設定 line.test_ring_hours。0時からの秒の組の並び。終わりが始まりより前なら日を
    またぐ。決まりは calling_hours.daily_spans の1か所）。"""
    return tuple((calling_hours.clock_seconds(a), calling_hours.clock_seconds(b))
                 for a, b in settings.resolve(tx, "line.test_ring_hours", **_scope(tx)))


def wait_seconds(tx: Tx) -> float:
    """取引の契約者が、つぎにテスト番号を鳴らしてよいまでの秒（0＝いま鳴らしてよい。1か所。どの回線・どの目的のテストも数える）：
    いちばん新しいテストが鳴ったなら0（テストは OK で終わり、あける長さは無い）。鳴らなかった・見分けられなかったが続けて n 回なら、前の
    テストから、長さの並び（line.test_ring_gaps）の n 番目の長さ（並びの最後で止まる＝初めの値で 15→30→60→120→240分）。2026-10-25 夜
    管理者の決定（⛔ 0139 の読み方：鳴ったあとも15分あけ、鳴らなかった1回目のあとは30分だった）。"""
    gaps = [float(g) for g in settings.resolve(tx, "line.test_ring_gaps", **_scope(tx))]
    rows = tx.all("SELECT tested_at, rang, now() AS now FROM salescall.line_ring_tests WHERE tenant_id = salescall.cur_tenant()"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT %s", (len(gaps),))
    run = next((i for i, r in enumerate(rows) if r["rang"]), len(rows))   # 続けて「鳴った」以外だった数（新しいほうから）
    if run == 0:   # テストが無い・いちばん新しいテストが鳴った（OK で終わり）：あける長さは無い
        return 0.0
    gap = gaps[min(run - 1, len(gaps) - 1)]
    return max(0.0, (rows[0]["tested_at"] + timedelta(seconds=gap) - rows[0]["now"]).total_seconds())


def decide_within(tx: Tx) -> float:
    """取引の契約者の、その回線で次にかける電話の結果で決まるのを待つ長さ（秒。1か所）：テスト番号をあける長さの並び
    （line.test_ring_gaps）の1つ目（初め15分）。まだ分からない続きは、その回線がこの長さのうちにかけられる行があれば、テスト番号を
    鳴らさずに、その電話の結果を待つ（scheduler.soon_left が読む）。2026-10-25 夜 管理者：「その回線で次にかけるのは15分後と決めたんだ
    から、15分になるんじゃないの？新たな設定はいらないでしょ？」（⛔ 0136〜0140：別の設定 line.decide_horizon＝30分。0141 で外した）。"""
    return float(settings.resolve(tx, "line.test_ring_gaps", **_scope(tx))[0])


def usable(tx: Tx) -> bool:
    """取引の契約者が、テスト番号で決められるか（1か所）：テスト番号がある。False なら、まだ分からない続きは決めずに待ち（`run` が
    1回知らせる）、止めた回線の確かめはアプリの確かめで行う（`checks_stop`）。"""
    return number(tx) is not None


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
    """テスト番号で、その回線から発信できるかを試し、記録する（1か所）。鳴らなかったら、まだ分からない続きを回線のせいで閉じる。
    見分けられなかったら、契約者と管理者にすぐ1回知らせ、直す担当へ修正依頼を出す（`_tell_undecided`）。試さなかったら記録しない。
    テスト番号が無ければ、決めにいく回（no_rows）に1回知らせる（決めずに待つ）。"""
    if purpose not in PURPOSES:
        raise ValueError(f"試す目的は {PURPOSES} のどれかです：{purpose!r}")
    to = access.in_tenant(actor, tenant_id, number)
    if to is None:
        if purpose == "no_rows":
            access.in_tenant(actor, tenant_id, lambda tx: _tell_undecided(tx, line_id, purpose, unknown=False))
        return Result.NO_NUMBER
    if not access.in_tenant(actor, tenant_id, allowed):   # 鳴らしてよい時間の外：鳴らさず、記録もしない（時間の始まりまで待つ）
        return Result.OFF_HOURS
    with access.exclusive(_lock_key(tenant_id)) as mine:   # 契約者ごとに1本ずつ（間を見てから記録するまで。データベースの鍵）
        if not mine:   # ほかの回線・ほかの見守り役が鳴らしている途中：鳴らさない（記録もしない）
            return Result.WAIT
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
        except CallWithheld:   # PC の部品が試さなかった（つながっていない・答えが期限に届かない・緊急停止）：結果ではない（記録しない）
            return Result.WITHHELD
        except Exception:  # noqa: BLE001 電話の部品の誤り：見分けられない（回線のせいの証拠にしない）
            rang = None

        def work(tx: Tx) -> None:
            tx.execute("INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang)"
                       " VALUES (salescall.cur_tenant(), %s, %s, %s)", (line_id, purpose, rang))
            if rang is False:
                attempts.close_unrung_run(tx, line_id, "test_silent")
            if rang is None:
                _tell_undecided(tx, line_id, purpose, unknown=True)
        access.in_tenant(actor, tenant_id, work)
    return Result.UNKNOWN if rang is None else Result.RANG if rang else Result.SILENT


NO_NUMBER_SUMMARY = ("テスト番号が無いので、回線のせいか番号のせいか決められません（その続きの行は、番号の回数にも回線の異常にも数えないまま"
                     "待っています。この回線で次にお客様の電話が鳴れば決まります）。テスト番号を設定してください。")
UNKNOWN_SUMMARY = {
    "no_rows": ("テスト番号の電話の答えが見分けられず、回線のせいか番号のせいかが決まりません（その続きの行は、番号の回数にも回線の異常にも"
                "数えないまま待っています。テスト番号は、あける長さのあとで、また鳴らします）。回線とテスト番号の電話を確かめてください。"),
    "resume_check": ("テスト番号の電話の答えが見分けられず、止めた回線を再開してよいか確かめられません（回線は止めたまま、あける長さのあとで、"
                     "また鳴らします）。回線とテスト番号の電話を確かめてください。"),
}


def _tell_undecided(tx: Tx, line_id: UUID, purpose: str, *, unknown: bool) -> None:
    """テスト番号で決められない・確かめられないことを、契約者と管理者に1回知らせる（1か所。0141・0143）。unknown＝テスト番号の答えが見分け
    られなかった（電話をつなぐ部品の不具合として、直す担当へ修正依頼も出す＝同じ不具合で1つ）・False＝テスト番号が無い（決めにいく回だけ）。
    鍵：決めにいくテストは回線と続きの始まり・再開の確かめは止めた停止（同じ続き・同じ停止で何度でも1回。知らせの「同じ出来事は1回だけ」の
    作り＝notify.emit）。"""
    if purpose == "no_rows":
        pending = attempts.line_pending(tx, line_id)
        if pending is None:
            return
        about, refs = f"{line_id}:{pending['since'].isoformat()}", {"line_id": line_id, "since": pending["since"]}
    else:
        stop = tx.one("SELECT event_id FROM salescall.open_stop_events WHERE scope = 'line' AND line_id = %s"
                      " AND cause::text = 'line_failure' ORDER BY event_id DESC LIMIT 1", (line_id,))
        if stop is None:
            return
        about, refs = f"{line_id}:stop:{stop['event_id']}", {"line_id": line_id, "stop_event_id": stop["event_id"]}
    if not unknown:
        notify.emit(tx, "line_undecided", key=f"line_undecided:{about}:no_number", admin=True, summary=NO_NUMBER_SUMMARY, refs=refs)
        return
    notify.emit(tx, "line_undecided", key=f"line_undecided:{about}", admin=True, summary=UNKNOWN_SUMMARY[purpose], refs=refs)
    fix_requests.request(tx, errors.PHONE_FIXER, "test_ring_undecided", key="test_ring_undecided",
                         summary="テスト番号の電話の答え（鳴った・鳴らない）を、電話をつなぐ部品が見分けられません",
                         refs={**refs, "purpose": purpose})


def last(tx: Tx, line_id: UUID) -> dict[str, Any] | None:
    """その回線のいちばん新しいテスト（画面・試験が読む）。"""
    return tx.one("SELECT tested_at, purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT 1", (line_id,))
