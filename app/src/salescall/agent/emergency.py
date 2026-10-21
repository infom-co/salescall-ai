"""本番の PC の上で押せる緊急停止（PC の側。DESIGN.md 9-2・5-5。CL-3 の連絡帳 4-1d の2：画面が開けないとき＝ネットの切断 などでも
止められる道）。持ち主 CL-2。

押し方（本番の PC で）：`python -m salescall.agent emergency stop "理由"`／`status`／`release "理由"`
- **stop**：PC の中に「止めた」印を残す（ネットが切れていても必ずできる）。**印があるあいだ、PC の部品は「かけて」と頼まれても断る**
  （発信の出口の物理的な止め。ネットが切れているあいだに押した停止を、ネットが戻っても守る）。サーバーにつながれば、PC の部品が
  届け、サーバーが、その契約者の緊急停止を記録する（門が止める・まだ発信していない予約を取り消す・画面にも出る）。記録できたら、
  印にその停止の番号を書く。
- 画面から解かれたら、サーバーが PC の部品に伝え、印を消す（解いた記録が正本）。
- **release**：印に「解いてほしい」を書く。PC の部品がサーバーに届けて、サーバーが解いたら印を消す。届かないうちは消さない
  （解いたことを記録できないまま、かけ始めない）。
- 印が読めないときは、効いているとみなす（止める側）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def flag_path() -> Path:
    """印の置き場（環境変数 SALESCALL_EMERGENCY_FLAG。無ければ、利用者のホームの .salescall/EMERGENCY_STOP）。"""
    given = os.environ.get("SALESCALL_EMERGENCY_FLAG")
    return Path(given) if given else Path.home() / ".salescall" / "EMERGENCY_STOP"


def engaged() -> dict[str, Any] | None:
    """印が効いていれば、その中身（理由・時刻・停止の番号・解いてほしい理由）。効いていなければ None（判断はここだけ）。"""
    p = flag_path()
    if not p.exists():
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {"reason": "印の中身が読めません（止めたままにします）"}
    except (OSError, ValueError):
        return {"reason": "印の中身が読めません（止めたままにします）"}


def _write(content: dict[str, Any]) -> Path:
    p = flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(content, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(p)                                   # 書きかけの印を残さない
    return p


def stop(reason: str) -> Path:
    """止める（印を残す）。もう止めていれば、前の印のまま（記録の番号を消さない）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
    now = engaged()
    if now is not None:
        return flag_path()
    return _write({"reason": reason, "at": datetime.now(timezone.utc).isoformat(), "event": None, "release": None})


def ask_release(reason: str) -> bool:
    """解いてほしいことを印に書く（PC の部品がサーバーに届け、解けたら印を消す）。止めていなければ False。"""
    if not reason.strip():
        raise ValueError("解く理由を書いてください")
    now = engaged()
    if now is None:
        return False
    _write(dict(now, release=reason))
    return True


def recorded(event: int) -> None:
    """サーバーが記録した停止の番号を、印に書く。"""
    now = engaged()
    if now is not None:
        _write(dict(now, event=event))


def clear() -> None:
    """印を消す（サーバーで解けたとき・画面から解かれたとき）。"""
    flag_path().unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("stop", "status", "release"):
        print('使い方：python -m salescall.agent emergency stop "理由" | status | release "理由"')
        return 2
    reason = " ".join(argv[1:])
    if argv[0] == "status":
        got = engaged()
        print("止めています：" + json.dumps(got, ensure_ascii=False) if got else "止めていません（この PC の印はありません）")
        return 0
    if argv[0] == "stop":
        print("この PC の印を置きました（この PC からは、かけません）：" + str(stop(reason)))
        print("サーバーにつながっていれば、PC の部品が、その契約者の緊急停止として記録します（画面にも出ます）")
        return 0
    if not ask_release(reason):
        print("止めていません（この PC の印はありません）")
        return 1
    print("解いてほしいことを印に書きました。PC の部品がサーバーに届けて、解けたら印を消します（届かないうちは止めたままです）")
    return 0
