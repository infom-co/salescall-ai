"""中央の見守り役（サーバーで常に動き、システムの処理をまとめて動かす役。Intent.md 14-1・DESIGN.md 5-4・5-5）。持ち主 CL-2。

- `main`：本体（起動のときの後始末・回線の借用と延長・回線ごとの流れの立ち上げと見張り・決まった間隔の仕事）。
- `line`：回線ごとの流れ（候補 → 門 → 発信の出口 → 通話 を繰り返す）。
- `jobs`：決まった間隔の仕事の表（仕事の一覧は、この表の1か所。すべての契約者の分）。
- `channel`：PC の部品のつなぎ口（`links` の形で本体へ見せる）。
- `config`：サーバーの設定。`plugins`：ほかの担当の部品の形。`updates`：プログラムの版。
契約者の PC の側は `salescall.agent`（PC の部品）。
"""

from __future__ import annotations

import threading
from collections import Counter

from salescall.core.access import Access, Actor, busy, unreachable
from salescall.core.errors import ErrorReport

ACTOR = Actor("見守り役", "system")
# 通話が「進まない」とみなす長さ（秒。1か所）。通話の長さに上限は持たない（2026-10-24 ユーザー：時間で業務を打ち切らない＝
# 「何を言っているの！？それだと業務を果たせずに終わることになるでしょ！」。CL-1 の連絡帳 4-41 の (1)）。通話を進める部品（CL-1）は、
# 通話の途中でターンごと・待つ間も決まった間ごとに「進んだ」と知らせる（core/conversation_io.progress）。この長さ進まなければ、こちらの
# 仕組みが止まったとみなし、おわびして切る（回線ごとの見張り＝line._Watch）。止めるとき（main.Supervisor.shutdown）・PC のログインを
# 外したとき（channel の通行証の取り消し）は、進んでいる通話がある間は待つ（進んでいなければ、この長さのあとで止める・閉じる）。
# 回線が「通話中」のまま、かけられない状態を異常とみなす長さ（line.STUCK_AFTER）も同じ。⛔ 2026-10-24：前は通話の最長（10分＋1分）を
# 決めて、それを過ぎた通話は待たずに止め・閉じた（話している電話を時間で切りえた）
CALL_STALL = 90.0
# 混雑（共有のつなぎの空き待ちの時間切れ）で記録できなかった誤り：仕事の名前 → 回数。空いたときに、まとめて記録する（flush_busy）
_BUSY: Counter[str] = Counter()
_BUSY_LOCK = threading.Lock()
_BUSY_SEEN = [0]   # 混雑に出会った回数の通し（減らさない。この回に混雑があったかを、本体の繰り返しが見る＝busy_seen）


def report(access: Access, tenant_id: object, what: str, error: BaseException, *, line_id: object = None) -> None:
    """見守り役の処理の誤りを、エラーの入口へ渡す（止めずに CL-2 へ修正依頼。errors の「supervisor」）。**誤りの文は渡さない**
    （お客様の情報・接続先が入りうる。渡すのは、どの仕事か と誤りの種類の名前だけ＝控えと同じ決まり）。記録できなくても止めない。"""
    if unreachable(error):   # データベースに届かない誤りは、データベースに記録できない（記録しに行って、さらに待たない）
        return
    if busy(error):   # 混雑：いまは記録しに行かない（さらに埋める）。数えておき、空いたときにまとめて記録する（消さない）
        with _BUSY_LOCK:
            _BUSY[what] += 1
            _BUSY_SEEN[0] += 1
        return
    try:
        from salescall.core import watch   # 見守り役の部品を読むときに、watch の読み込みを先にしない
        watch.after_error(access, ACTOR, ErrorReport("supervisor", None, f"{what}：{type(error).__name__}"),
                          tenant_id=tenant_id, line_id=line_id)
    except Exception:  # noqa: BLE001 データベースに届かないときも、見守り役は止めない（サーバーの記録には残る）
        pass


def busy_seen() -> int:
    """混雑に出会った回数の通し（前に見た数と比べて、そのあいだに混雑があったかを知る）。"""
    with _BUSY_LOCK:
        return _BUSY_SEEN[0]


def flush_busy(access: Access) -> None:
    """混雑で記録できなかった誤りを、まとめて記録する（本体の繰り返しが呼ぶ。CL-2 へ修正依頼＝つなぎの数が足りない）。"""
    with _BUSY_LOCK:
        waiting = dict(_BUSY)
        _BUSY.clear()
    for what, n in waiting.items():
        try:
            from salescall.core import watch
            watch.after_error(access, ACTOR, ErrorReport("supervisor", None, f"{what}：共有のつなぎの混雑（{n} 回）"),
                              tenant_id=None, line_id=None)
        except Exception as e:  # noqa: BLE001 まだ混んでいる・届かない：数を戻して、次の回にまた試す
            if busy(e) or unreachable(e):
                with _BUSY_LOCK:
                    _BUSY[what] += n
