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
- **印を消すのは、サーバーが解いたと言った停止の番号が、いまの印の番号と同じときだけ**（`clear`。判断はここだけ）：解く頼みを
  届けているあいだ・画面から解かれたことが届くまでのあいだに、もう一度押したら、その押しは新しい停止（番号を空に戻す・解いて
  ほしいを外す）なので、前の停止が解けても消さない（2026-10-21 の見直し：前は、押し直しても黙って消え、かけ始めた）。
- 印が読めないときは、効いているとみなす（止める側）。印の置き場は PC 全体で1つ（Windows の利用者が違っても同じ印＝`link.locks.lock_dir`）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from salescall.link.locks import lock_dir


def flag_path() -> Path:
    """印の置き場（環境変数 SALESCALL_EMERGENCY_FLAG。無ければ、PC 全体で1つの場所の EMERGENCY_STOP）。"""
    given = os.environ.get("SALESCALL_EMERGENCY_FLAG")
    return Path(given) if given else lock_dir() / "EMERGENCY_STOP"


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
    """止める（印を残す）。もう止めていれば、新しい押しとして書き直す（番号を空に＝サーバーが新しく記録する・解いてほしいを外す。
    前の停止が解けても、この押しは消えない）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
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


def recorded(event: int, at: str | None) -> bool:
    """サーバーが記録した停止の番号を、印に書く（記録した押しと、いまの印の押しが同じときだけ。押し直していれば書かない＝新しい押しは
    サーバーがまた記録する）。書いたら True。"""
    now = engaged()
    if now is None or now.get("event") is not None or str(now.get("at") or "") != str(at or ""):
        return False
    _write(dict(now, event=event))
    return True


def clear(event: int | None) -> bool:
    """サーバーが「その停止は解けた」と言ったとき、印の番号が同じなら印を消す（番号の無い印は、PC で解いてほしいと書いたもの
    だけ。番号が違う・押し直した印は消さない）。消したら True。"""
    now = engaged()
    if now is None:
        return False
    if now.get("event") != event or (event is None and not now.get("release")):
        return False
    flag_path().unlink(missing_ok=True)
    return True


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
