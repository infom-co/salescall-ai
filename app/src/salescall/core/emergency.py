"""本番のPCの上で押せる緊急停止（CL-3 の連絡帳 4-1d の2：画面が開けないとき＝Fly.io の不具合・ネットの切断 でも止められる道）。
持ち主 CL-2（運転の状態 Q13。DESIGN.md 9-1）。

押し方（本番のPCで）：`python -m salescall.core.emergency stop "理由"`／`status`／`release "理由"`
- **stop**：(1) まず、このPCの上に「止めた」印を残す（ネットが切れていても必ずできる）。(2) データベースへ届けば、全体の範囲の
  人の緊急停止（manual）を記録する（ふつうの停止と同じ。門が止め、まだ発信していない予約を取り消す。画面にも出る）。
  届かなければ、そのことを表示する（印だけで止まっている）。
- **印があるあいだ、発信の出口（core/dial_exit.py）はかけない**（予約を戻す）。⚠️ ネットが切れているあいだに押し、あとでネットが
  戻ったとき、データベースに停止が無くても発信を始めないため（止めたつもりで、かけ始める穴を作らない）。
  印は「発信の出口の物理的な止め」で、かけてよいかの判断（門）は増やさない。
- **release**：データベースの、この道で記録した緊急停止を解いてから、印を消す。データベースへ届かなければ、印は消さない
  （解いたことを記録できないまま、かけ始めない）。
- 押す人は、そのPCのシステムの管理者（全体の範囲の緊急停止は管理者だけ。0018・0019）。名前は環境変数 SALESCALL_ACTOR_NAME。
  データベースへのつなぎ方は環境変数 SALESCALL_DATABASE_URL（APIキーと同じく、値を画面・記録に出さない）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from salescall.core import stops
from salescall.core.access import Access, Actor, Tx

EVIDENCE = "本番のPCの緊急停止（core/emergency.py）"


def flag_path() -> Path:
    """印の置き場（環境変数 SALESCALL_EMERGENCY_FLAG。無ければ、利用者のホームの .salescall/EMERGENCY_STOP）。"""
    given = os.environ.get("SALESCALL_EMERGENCY_FLAG")
    return Path(given) if given else Path.home() / ".salescall" / "EMERGENCY_STOP"


def engaged() -> dict[str, Any] | None:
    """印があれば、その中身（理由・時刻・誰が）。無ければ None。読めない印も「ある」とみなす（止める側に倒す）。"""
    p = flag_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reason": "印の中身が読めません（止めたままにします）"}


def _write_flag(reason: str, by: str) -> Path:
    p = flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"reason": reason, "by": by, "at": datetime.now(timezone.utc).isoformat()},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)                                   # 書きかけの印を残さない（置き換えは1回で済む）
    return p


def stop_all(access: Access | None, actor: Actor, reason: str) -> dict[str, Any]:
    """止める：印を残し、データベースへ届けば全体の緊急停止を記録する（つなぎ先が無い＝None でも、印は残す）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
    path = _write_flag(reason, actor.name)
    if access is None:
        return {"flag": str(path), "db_event": None, "db_error": "つなぎ先（SALESCALL_DATABASE_URL）がありません"}
    try:
        event = access.in_system(actor, lambda tx: stops.stop(tx, scope="system", cause="manual", reason=reason,
                                                              evidence=EVIDENCE)["event_id"])
        return {"flag": str(path), "db_event": event, "db_error": None}
    except Exception as e:  # noqa: BLE001  データベースへ届かなくても、印で止まっている（そのことを返す）
        return {"flag": str(path), "db_event": None, "db_error": type(e).__name__}


def _open_by_this_switch(tx: Tx) -> list[int]:
    return [r["event_id"] for r in tx.all(
        "SELECT s.event_id FROM salescall.stop_events s WHERE s.action = 'stop' AND s.scope = 'system' AND s.cause = 'manual'"
        " AND s.evidence = %s AND NOT EXISTS (SELECT 1 FROM salescall.stop_events r WHERE r.resolves_event_id = s.event_id)",
        (EVIDENCE,))]


def release_all(access: Access | None, actor: Actor, reason: str) -> dict[str, Any]:
    """解く：この道で記録した緊急停止を、データベースで解いてから、印を消す。届かなければ印は消さない。"""
    if not reason.strip():
        raise ValueError("解く理由を書いてください")
    if access is None:
        return {"released": False, "resumed": [], "db_error": "つなぎ先（SALESCALL_DATABASE_URL）がありません"}
    try:
        resumed = access.in_system(actor, lambda tx: [stops.resume(tx, e, reason=reason, evidence=EVIDENCE)
                                                      for e in _open_by_this_switch(tx)])
    except Exception as e:  # noqa: BLE001
        return {"released": False, "resumed": [], "db_error": type(e).__name__}
    flag_path().unlink(missing_ok=True)
    return {"released": True, "resumed": resumed, "db_error": None}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("stop", "status", "release"):
        print('使い方：python -m salescall.core.emergency stop "理由" | status | release "理由"')
        return 2
    if argv[0] == "status":
        got = engaged()
        print("止めています：" + json.dumps(got, ensure_ascii=False) if got else "止めていません（このPCの印はありません）")
        return 0
    reason = " ".join(argv[1:])
    url = os.environ.get("SALESCALL_DATABASE_URL")
    access = Access(url) if url else None             # つなぎ先が無くても、止める（印を残す）ことはできる
    actor = Actor(os.environ.get("SALESCALL_ACTOR_NAME", "本番のPCの管理者"), "admin")
    if argv[0] == "stop":
        got = stop_all(access, actor, reason)
        print("このPCの印を置きました（発信の出口は、かけません）：" + got["flag"])
        print("データベースにも記録しました" if got["db_event"] else
              f"⚠️ データベースへ届きませんでした（{got['db_error']}）。印だけで止まっています。届くようになったら、もう一度押してください")
        return 0
    got = release_all(access, actor, reason)
    print("解きました" if got["released"] else
          f"⚠️ データベースへ届かないので、解いていません（{got['db_error']}）。印はそのままです")
    return 0 if got["released"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
