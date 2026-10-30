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
  （17-8 の (3)(4)(6)）。5段目（0147）で、それらの関数と、止めた回線の停止をテスト番号で確かめる道（checks_stop・checkable_seconds）を外した。
- 試すのは `run` の1か所（電話の部品を持つのは回線ごとの処理だけ。話している回線では試さない）。結果は回線ごとの記録（`line_ring_tests`。
  書くのはここだけ）に残す。
- **結果は3つ**（電話の部品の約束＝link/phone.py の test_ring。0141）：鳴った＝回線から発信できた（呼び出しの音・話し中の音・電源オフや
  圏外のお知らせの声。2026-10-25 夜 管理者：「テスト番号が話し中・電源オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」）／
  鳴らなかった＝回線から発信できない／見分けられない（電話の部品が誤りを返したときも）。**試さなかった**（PC の部品がつながっていない・
  答えが期限に届かない・PC の上の緊急停止の印＝電話の部品が CallWithheld を上げる・回線が話している途中・ほかの回線か見守り役が鳴らして
  いる途中）は結果ではない：記録しない（表は答えに数えない＝2026-10-25 夜 管理者）。
- 1つの契約者のテストは1本ずつ（データベースの鍵＝Access.exclusive。契約者ごと。見守り役の入れ替えで新旧が少し重なっても同時に鳴らさない）。
- **鳴らしてよい時間**（設定 `line.test_ring_hours`。2026-10-25 ユーザー：「0～24時の選択は残したうえで」）・**あける長さの並び**（設定
  `line.test_ring_gaps`）は、表の決まりの値（core/health_rules.Rules）として表だけが読む（この部品は読まない）。時刻の決まりは
  calling_hours.daily_spans の1か所。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from salescall.core import health, settings
from salescall.core.health_rules import RESTS
from salescall.core.access import Access, Actor, Tx
from salescall.link.phone import CallWithheld

__all__ = ["PURPOSES", "Result", "RingDriver", "number", "run", "usable"]

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
    NO_NUMBER = "no_number"   # 表が鳴らす頼みを置いたあとで、テスト番号が無くなっていた（試さなかったと同じ＝E9）
    WITHHELD = "withheld"     # PC の部品が試さなかった（つながっていない・答えが期限に届かない・PC の上の緊急停止）
    BUSY = "busy"             # 回線が話している途中・答えない
    WAIT = "wait"             # ほかの回線・ほかの見守り役が鳴らしている途中（1つの契約者のテスト番号は1本ずつ）


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


def usable(tx: Tx) -> bool:
    """取引の契約者が、テスト番号で決められるか（1か所）：テスト番号がある。状態の表を引く入口が、次に動く時刻が来た回線へ渡す
    （core/health.tick の E10。無ければ、表が知らせて待つ＝L-15・17-3-6）。"""
    return number(tx) is not None


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
