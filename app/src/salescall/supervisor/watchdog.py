"""見守り役の本体が固まったことに、自分で気づいて終わる見張り（1か所）。持ち主 CL-2。DESIGN.md 5-4。

見守り役の本体（決まった間隔の確かめの繰り返し）が、落ちずに固まると、動かす所の仕組み（Windows のタスク スケジューラ・Fly.io の
立ち上げ直しの設定）は気づけない（処理が生きているため）。そのあいだ、電話も締めも控えも止まり、止まったことの知らせも出ない。
⇒ 本体は1回まわるごとに `beat` し、別の流れが、`STALL_LIMIT` 秒まわっていなければ、記録を試みてから処理を終える（終わり方 1）。
終われば、動かす所の仕組みが立ち上げ直す（2026-10-22 ユーザー承認：見守り役そのものが止まったことに気づく仕組み）。
通話の途中の回線は、つながりが切れたことに気づいた PC の部品が、おわびの録音を流して切る（お客様を無言で放置しない）。
止めるとき（いまの通話の終わりを待つあいだ）は見張らない（`stop`）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

STALL_LIMIT = 600.0   # 本体がまわらないまま、これだけたったら終える（秒。10分）
CHECK_EVERY = 10.0


class Watchdog:
    def __init__(self, on_stall: Callable[[float], None], limit: float = STALL_LIMIT) -> None:
        self.on_stall, self.limit = on_stall, limit
        self._last = time.monotonic()
        self._off = threading.Event()

    def beat(self) -> None:
        self._last = time.monotonic()

    def stop(self) -> None:
        self._off.set()

    def stalled(self, now: float | None = None) -> float | None:
        """まわっていない長さ（限度を過ぎていれば。見張っていない・過ぎていなければ None）。"""
        if self._off.is_set():
            return None
        quiet = (time.monotonic() if now is None else now) - self._last
        return quiet if quiet >= self.limit else None

    def start(self) -> None:
        def watch() -> None:
            while not self._off.wait(CHECK_EVERY):
                quiet = self.stalled()
                if quiet is not None:
                    self.on_stall(quiet)
                    return
        threading.Thread(target=watch, name="salescall-watchdog", daemon=True).start()


def end_process(report: Callable[[], None]) -> Callable[[float], None]:
    """固まったときの始末：記録を試み（固まっているのがデータベースなら待たない＝5秒まで）、処理を終える。"""
    def on_stall(quiet: float) -> None:
        print(f"見守り役の本体が {int(quiet)} 秒まわっていません。処理を終えます（自動で立ち上げ直されます）", flush=True)
        t = threading.Thread(target=report, name="salescall-watchdog-report", daemon=True)
        t.start()
        t.join(5.0)
        sys.stdout.flush()
        os._exit(1)
    return on_stall
