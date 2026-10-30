"""テスト番号で、回線で電話が鳴るかを試す（DESIGN.md 5-4・9-1・17章。表の作り 0134）。持ち主 CL-2。

2026-10-24 ユーザー決定：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、一度も
鳴らせていなければ回線のせいです。（テスト番号はフェーズ1で知らせます。）」「回線が止まったあとの再開の確かめ：OK」

- テスト番号は設定（`line.test_number`。お客様ではない電話）：管理者がシステムの初めの値として仮の番号を入れ、契約者が自分の番号を
  入れたら、その契約者ではそちらを使う（2026-10-29 管理者。設定の作り＝override のまま）。お客様の番号ではないので、門・発信禁止・
  番号の種類の決まりは通らない（その回線で電話が鳴るかを試すだけ）。
- **いつ・なぜ鳴らすかは、回線の状態の表が決める**（DESIGN.md 17章。4段目＝2026-10-29）：確かめ中の段 (a) の15分（【当(5)】）・待つ理由
  （鳴らしてよい時間の外・15→30→60→120→240分あける・PC の部品が試さなかった・ほかの回線のテスト中・テスト番号が無い）・休みの次の
  テストの時刻は、回線の欄の中身に持つ。回線ごとの処理（supervisor/line.py）が、回線の欄が「確かめ中 (c)」か「休みで答え待ち」のとき
  （表が鳴らす頼みを置いたとき）だけ `run` を呼ぶ。答え（E6 鳴った・E7 鳴らなかった・E8 見分けられない・E9 試さなかった）は、状態の表を
  引く入口（core/health.tested）へ渡す。番号のせいか回線のせいか・回線の休みと再開・知らせ・修正依頼は、表が決めて入口が行う。
  ⛔ 4段目の前：この部品が、鳴らす間（wait_seconds＝契約者のすべてのテストから数えた）・鳴らしてよい時間（allowed）・まだ分からない続きを
  閉じる（close_unrung_run）・知らせと修正依頼（_tell_undecided）を、それぞれその場で導き、回線の停止の確かめの間隔（watch）と重なった
  （17-8 の (3)(4)(6)）。
- 試すのは `run` の1か所（電話の部品を持つのは回線ごとの処理だけ。話している回線では試さない）。結果は回線ごとの記録（`line_ring_tests`。
  書くのはここだけ）に残す。
- **結果は3つ**（電話の部品の約束＝link/phone.py の test_ring。0141）：鳴った＝回線から発信できた（呼び出しの音・話し中の音・電源オフや
  圏外のお知らせの声。2026-10-25 夜 管理者：「テスト番号が話し中・電源オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」）／
  鳴らなかった＝回線から発信できない／見分けられない（電話の部品が誤りを返したときも）。**試さなかった**（PC の部品がつながっていない・
  答えが期限に届かない・PC の上の緊急停止の印＝電話の部品が CallWithheld を上げる・回線が話している途中・ほかの回線か見守り役が鳴らして
  いる途中）は結果ではない：記録しない（表は答えに数えない＝2026-10-25 夜 管理者）。
- 1つの契約者のテストは1本ずつ（データベースの鍵＝Access.exclusive。契約者ごと。見守り役の入れ替えで新旧が少し重なっても同時に鳴らさない）。
- **鳴らしてよい時間**（設定 `line.test_ring_hours`。2026-10-25 ユーザー：「0～24時の選択は残したうえで」）・**あける長さの並び**（設定
  `line.test_ring_gaps`）は、表の決まりの値（core/health_rules.Rules）として読む。時刻の決まりは calling_hours.daily_spans の1か所。
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from salescall.core import attempts, calling_hours, errors, fix_requests, health, notify, settings
from salescall.core.health_rules import RESTS
from salescall.core.access import Access, Actor, Tx
from salescall.link.phone import CallWithheld

__all__ = ["PURPOSES", "Result", "RingDriver", "allowed", "checkable_seconds", "checks_stop", "decide_within", "gap_after", "hours",
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
    n = len(settings.resolve(tx, "line.test_ring_gaps", **_scope(tx)))
    rows = tx.all("SELECT tested_at, rang, now() AS now FROM salescall.line_ring_tests WHERE tenant_id = salescall.cur_tenant()"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT %s", (n,))
    run = next((i for i, r in enumerate(rows) if r["rang"]), len(rows))   # 続けて「鳴った」以外だった数（新しいほうから）
    if run == 0:   # テストが無い・いちばん新しいテストが鳴った（OK で終わり）：あける長さは無い
        return 0.0
    return max(0.0, (rows[0]["tested_at"] + timedelta(seconds=gap_after(tx, run)) - rows[0]["now"]).total_seconds())


def gap_after(tx: Tx, n: int) -> float:
    """続けて n 回（1から）のあとにあける長さ（秒。1か所）：長さの並び（設定 line.test_ring_gaps。初め 15→30→60→120→240分）の n 番目
    （並びの最後で止まる）。テスト番号の鳴らす間（`wait_seconds`）と、声が聞き取れずに止め直した回線の、次に再開を試してよいまで
    （core/watch.checkable。2026-10-29 管理者：「次に試すまで 15→30→60→120→240分あけます」）が読む。"""
    gaps = [float(g) for g in settings.resolve(tx, "line.test_ring_gaps", **_scope(tx))]
    return gaps[min(n - 1, len(gaps) - 1)]


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


def run(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, driver: RingDriver) -> Result:
    """テスト番号で、その回線から発信できるかを試し、記録して、答えを状態の表を引く入口へ渡す（1か所。DESIGN.md 17章の E6〜E9）。
    **いつ鳴らすかは、この部品では決めない**：回線ごとの処理が、回線の欄（表が「確かめ中 (c)」か「休みで答え待ち」に置いた＝鳴らす頼み）を
    読んで呼ぶ（core/health.tick）。鳴らしてよい時間・あける長さ・ほかの回線のテスト中・テスト番号が無い、は表の待つ理由（17-1-1 の (b)）で、
    ここでは見ない（⛔ 4段目の前：ここで wait_seconds・allowed・番号の有る無しを、その場で導き、表と別に決めていた＝17-8 の (6)）。
    - 鳴った＝E6・鳴らなかった＝E7・見分けられない（電話の部品の誤りも）＝E8：記録（line_ring_tests。目的は回線の欄から＝休みなら
      resume_check・確かめ中なら no_rows）してから渡す。
    - 試さなかった（PC の部品がつながっていない・答えが期限に届かない・PC の上の緊急停止）・回線が話している途中・ほかの回線か見守り役が
      鳴らしている途中（1つの契約者のテスト番号は1本ずつ＝データベースの鍵）・テスト番号が無くなっていた＝E9（記録しない。表は答えに
      数えず、並びの1つ目のあとで試し直す＝L-12・L-19）。"""
    to = access.in_tenant(actor, tenant_id, number)
    if to is None:   # 表が鳴らす頼みを置いたあとで、テスト番号が無くなった：試さなかったと同じ
        access.in_tenant(actor, tenant_id, lambda tx: health.tested(tx, line_id, "withheld"))
        return Result.NO_NUMBER
    with access.exclusive(_lock_key(tenant_id)) as mine:   # 契約者ごとに1本ずつ（データベースの鍵）
        if not mine:   # ほかの回線・ほかの見守り役が鳴らしている途中：鳴らさない（記録もしない）
            access.in_tenant(actor, tenant_id, lambda tx: health.tested(tx, line_id, "withheld"))
            return Result.WAIT
        try:
            busy = driver.in_call()
        except Exception:  # noqa: BLE001 答えられない＝分からない：話しているとみなす（試さない側）
            busy = True
        if busy:
            access.in_tenant(actor, tenant_id, lambda tx: health.tested(tx, line_id, "withheld"))
            return Result.BUSY
        try:
            rang = driver.test_ring(to)
        except CallWithheld:   # PC の部品が試さなかった：結果ではない（記録しない）
            access.in_tenant(actor, tenant_id, lambda tx: health.tested(tx, line_id, "withheld"))
            return Result.WITHHELD
        except Exception:  # noqa: BLE001 電話の部品の誤り：見分けられない（回線のせいの証拠にしない）
            rang = None

        def work(tx: Tx) -> None:
            purpose = "resume_check" if health.line_of(tx, line_id).stage in RESTS else "no_rows"
            tx.execute("INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang)"
                       " VALUES (salescall.cur_tenant(), %s, %s, %s)", (line_id, purpose, rang))
            health.tested(tx, line_id, "unknown" if rang is None else "rang" if rang else "silent")
        access.in_tenant(actor, tenant_id, work)
    return Result.UNKNOWN if rang is None else Result.RANG if rang else Result.SILENT


# テスト番号が無いときの知らせ：実際に起きることだけを書く（見直し22 の 4。⛔ 0143：「この回線で次にお客様の電話が鳴れば決まります」と
# 書いたが、その回線にかける行が残っていなければ起きない）。決め方そのもの（待つか・前の区切りで決めるか）は管理者に伺い中
NO_NUMBER_SUMMARY = ("テスト番号が無いので、回線のせいか番号のせいか決められません（その続きの行は、番号の回数にも回線の異常にも数えないまま"
                     "待っています。この回線で、あとでほかの行の電話がお客様のところで鳴るか、この回線を使わなくすると決まります。それまでは"
                     "決まりません）。管理者か契約者がテスト番号を入れてください（管理者が初めの値として入れた番号を、契約者が自分の番号を"
                     "入れるまで使います）。")
UNDECIDED_SUMMARY = {
    ("no_rows", "unknown"): ("テスト番号の電話の答えが見分けられず、回線のせいか番号のせいかが決まりません（その続きの行は、番号の回数にも回線の"
                             "異常にも数えないまま待っています。テスト番号は、あける長さのあとで、また鳴らします）。回線とテスト番号の電話を"
                             "確かめてください。"),
    ("no_rows", "withheld"): ("PC の部品がテスト番号を試さないまま（つながっていない・答えが期限に届かない・緊急停止）、回線のせいか番号のせいかが"
                              "決まりません（その続きの行は、番号の回数にも回線の異常にも数えないまま待っています。テスト番号は、試せるように"
                              "なりしだい鳴らします）。PC の部品と回線を確かめてください。"),
    ("resume_check", "unknown"): ("テスト番号の電話の答えが見分けられず、止めた回線を再開してよいか確かめられません（回線は止めたまま、あける長さの"
                                  "あとで、また鳴らします）。回線とテスト番号の電話を確かめてください。"),
}
UNDECIDED_FIX = {"unknown": "テスト番号の電話の答え（鳴った・鳴らない）を、電話をつなぐ部品が見分けられません",
                 "withheld": "回線のせいか番号のせいかを決めるテスト番号の頼みを、PC の部品が試しません"}


def _tell_undecided(tx: Tx, line_id: UUID, purpose: str, why: str) -> None:
    """テスト番号で決められない・確かめられないことを、契約者と管理者に1回知らせる（1か所。0141・0143）。why＝unknown（テスト番号の答えが
    見分けられなかった）・withheld（決めにいくテストを PC の部品が試さないまま、続きが始まってから＝テストを試していれば、そのあとから、
    あける長さの最初＝`decide_within` を過ぎた。それより前は知らせない。見直し22 の 9）：どちらも同じ知らせ（同じ続きで1回）と、電話を
    つなぐ部品の不具合として直す担当への修正依頼（同じ不具合で1つ）・no_number（テスト番号が無い。決めにいく回だけ）。
    鍵：決めにいくテストは回線と続きの始まり・再開の確かめは止めた停止（同じ続き・同じ停止で何度でも1回。知らせの「同じ出来事は1回だけ」の
    作り＝notify.emit）。"""
    if purpose == "no_rows":
        pending = attempts.line_pending(tx, line_id)
        if pending is None:
            return
        if why == "withheld":
            got = tx.one("SELECT greatest(%s, max(tested_at)) AS since, now() AS now FROM salescall.line_ring_tests WHERE line_id = %s",
                         (pending["since"], line_id))
            if (got["now"] - got["since"]).total_seconds() < decide_within(tx):
                return
        about, refs = f"{line_id}:{pending['since'].isoformat()}", {"line_id": line_id, "since": pending["since"]}
    else:
        stop = tx.one("SELECT event_id FROM salescall.open_stop_events WHERE scope = 'line' AND line_id = %s"
                      " AND cause::text = 'line_failure' ORDER BY event_id DESC LIMIT 1", (line_id,))
        if stop is None:
            return
        about, refs = f"{line_id}:stop:{stop['event_id']}", {"line_id": line_id, "stop_event_id": stop["event_id"]}
    if why == "no_number":
        notify.emit(tx, "line_undecided", key=f"line_undecided:{about}:no_number", admin=True, summary=NO_NUMBER_SUMMARY, refs=refs)
        return
    notify.emit(tx, "line_undecided", key=f"line_undecided:{about}", admin=True, summary=UNDECIDED_SUMMARY[(purpose, why)], refs=refs)
    fix_requests.request(tx, errors.PHONE_FIXER, "test_ring_undecided", key="test_ring_undecided", summary=UNDECIDED_FIX[why],
                         refs={**refs, "purpose": purpose})


def last(tx: Tx, line_id: UUID) -> dict[str, Any] | None:
    """その回線のいちばん新しいテスト（画面・試験が読む）。"""
    return tx.one("SELECT tested_at, purpose, rang FROM salescall.line_ring_tests WHERE line_id = %s"
                  " ORDER BY tested_at DESC, test_id DESC LIMIT 1", (line_id,))
