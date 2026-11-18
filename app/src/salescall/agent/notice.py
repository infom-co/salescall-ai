"""PC の画面に知らせる（PC の部品の側。1か所）。持ち主 CL-2。

サーバーがすべて止まると、サーバーからの知らせ（契約者・管理者あて）も出せない。そのときだけ、PC の部品が、その PC の画面に
直接知らせる（2026-10-22 ユーザー承認：「それでも10分つながらないときは、契約者の PC の部品が画面に『サーバーにつながりません』と
出します」）。Windows では、ほかの窓の上に出る知らせの窓（押すまで残る。PC の部品の動きは止めない＝別の流れで出す）。Windows で
なければ、画面（標準出力）に書く。
"""

from __future__ import annotations

import sys
import threading

TITLE = "SalesCall AI"


def show(text: str) -> None:
    """知らせを出す（待たない）。"""
    if sys.platform == "win32":
        import ctypes   # Windows だけにある（ほかの PC では読まない）

        def box() -> None:
            ctypes.windll.user32.MessageBoxW(0, text, TITLE, 0x40 | 0x40000)   # 「i」の印・ほかの窓の上に出す
        threading.Thread(target=box, name="salescall-notice", daemon=True).start()
    else:
        print(f"【{TITLE}】{text}", flush=True)
