"""本番のPCの上で押せる緊急停止（CL-3 の連絡帳 4-1d の2：画面が開けないとき＝Fly.io の不具合・ネットの切断 でも止められる道）。
持ち主 CL-2（運転の状態 Q13。DESIGN.md 9-1）。

押し方（本番のPCで）：`python -m salescall.core.emergency stop "理由"`／`status`／`release "理由"`
- **stop**：(1) まず、このPCの上に「止めた」印を残す（ネットが切れていても必ずできる）。(2) データベースへ届けば、押した管理者か
  契約者の範囲の緊急停止（manual）を記録する（ふつうの停止と同じ。門が止め、まだ発信していない予約を取り消す。画面にも出る）。
  範囲：管理者＝全体／契約者＝その契約者（全体の範囲の停止は管理者だけ。0018・0019）。記録できたら、印にその停止の番号を書く。
  届かなければ、そのことを表示する（印だけで止まっている）。
- **印があるあいだ、発信の出口（core/dial_exit.py）はかけない**（予約を戻す）。⚠️ ネットが切れているあいだに押し、あとでネットが
  戻ったとき、データベースに停止が無くても発信を始めないため（止めたつもりで、かけ始める穴を作らない）。
  印は「発信の出口の物理的な止め」で、かけてよいかの判断（門）は増やさない。
- **印が効いているかの判断は `engaged` の1か所**：印に書いた停止が、データベースで解かれていたら（管理者か契約者が画面から
  解いた）、印を消す（2026-10-16 見直し：前は、画面から解いても印が残り、画面は「止まっていない」のに、このPCは黙ってかけ
  なかった）。印に停止の番号が無い（届かないまま押した）・停止が見えない・印が読めないときは、止めたままにする。
- **release**：データベースの、この道で記録した緊急停止（押した範囲のもの）を解いてから、印を消す。データベースへ届かなければ、
  印は消さない（解いたことを記録できないまま、かけ始めない）。印が別の範囲で押されたものなら、解かない（その範囲の管理者か
  契約者が解く）。
- 押すのは、そのPCを使う管理者か契約者。**データベースへのつなぎ方と、押したのが誰かは、見守り役と同じ1か所**
  （`supervisor.config.load`）：管理者の PC＝SALESCALL_DSN と SALESCALL_TENANT_ID（管理者として全体を止める）／契約者の PC＝
  ログインでしまった PC の鍵（その契約者として、その契約者を止める）。⚠️ 前は SALESCALL_DATABASE_URL・SALESCALL_ACTOR_NAME・
  SALESCALL_ACTOR_KIND という、PC の準備（docs/PC_SETUP.md）に無い名前を読んでいたので、契約者の PC では印だけで止まり、
  解くこともできなかった（2026-10-19 の見直し）。つなげなくても・誰か分からなくても、印は残す（止めることは、いつでもできる）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from salescall.core import stops
from salescall.core.access import Access, Actor, Tx

EVIDENCE = "本番のPCの緊急停止（core/emergency.py）"


def flag_path() -> Path:
    """印の置き場（環境変数 SALESCALL_EMERGENCY_FLAG。無ければ、利用者のホームの .salescall/EMERGENCY_STOP）。"""
    given = os.environ.get("SALESCALL_EMERGENCY_FLAG")
    return Path(given) if given else Path.home() / ".salescall" / "EMERGENCY_STOP"


def _read_flag() -> dict[str, Any] | None:
    p = flag_path()
    if not p.exists():
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {"reason": "印の中身が読めません（止めたままにします）"}
    except (OSError, ValueError):
        return {"reason": "印の中身が読めません（止めたままにします）"}


def engaged(tx: Tx | None = None) -> dict[str, Any] | None:
    """印が効いていれば、その中身（理由・誰が・範囲・時刻・停止の番号）。効いていなければ None（判断はここだけ）。
    tx を渡すと、印に書いた停止がデータベースで解かれていれば、印を消して None を返す（解いた記録が正本）。
    停止が取引から見えない・番号が無い・印が読めないときは、効いているとみなす（止める側に倒す）。"""
    flag = _read_flag()
    if flag is None or tx is None or not isinstance(flag.get("db_event"), int):
        return flag
    row = tx.one("SELECT (SELECT 1 FROM salescall.open_stop_events o WHERE o.event_id = s.event_id) IS NULL AS resolved"
                 " FROM salescall.stop_events s WHERE s.event_id = %s AND s.action = 'stop'", (flag["db_event"],))
    if row is None or not row["resolved"]:
        return flag
    flag_path().unlink(missing_ok=True)
    return None


def _write_flag(content: dict[str, Any]) -> Path:
    p = flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(content, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(p)                                   # 書きかけの印を残さない（置き換えは1回で済む）
    return p


def _in_scope(access: Access, actor: Actor, tenant_id: UUID | None, work):
    """押した管理者か契約者の範囲の取引（管理者＝全体／契約者＝その契約者）。データベースが PC の鍵を断ったとき（LoginRefused。
    見守り役がログインし直すための BaseException）も、ここで、ふつうの誤り（PermissionError）にして返す（1か所。止める・解く・
    状態の確かめは、どれも「届かなかった」として印のとおりに扱う。⚠️ 前は受け止めず、印を置いたあと誤りの表示で落ちた＝見直し5）。"""
    from salescall.core.access import LoginRefused
    try:
        if actor.kind == "admin":
            return access.in_system(actor, work)
        if actor.kind == "operator" and tenant_id is not None:
            return access.in_tenant(actor, tenant_id, work)
    except LoginRefused as e:
        raise PermissionError(f"データベースが PC の鍵を断りました（{e}）。PC でログインし直してください（docs/PC_SETUP.md）") from None
    raise PermissionError("押せるのは管理者（admin）か、契約者（operator。契約者の番号つき）です")


def stop_all(access: Access | None, actor: Actor | None, reason: str, *, tenant_id: UUID | None = None,
             no_actor: str = "押した管理者か契約者が分かりません") -> dict[str, Any]:
    """止める：印を残し、データベースへ届けば、押した範囲の緊急停止を記録して、印に停止の番号を書く
    （つなぎ先が無い＝None・押した管理者か契約者が分からない＝None でも、印は残す）。"""
    if not reason.strip():
        raise ValueError("止める理由を書いてください")
    flag = {"reason": reason, "by": None if actor is None else actor.name, "kind": None if actor is None else actor.kind,
            "tenant_id": tenant_id if actor is not None and actor.kind == "operator" else None,
            "at": datetime.now(timezone.utc).isoformat(), "db_event": None}
    path = _write_flag(flag)
    if access is None or actor is None:
        return {"flag": str(path), "db_event": None,
                "db_error": no_actor if actor is None else "データベースへのつなぎ先がありません（PC の準備：docs/PC_SETUP.md）"}
    scope = "system" if actor.kind == "admin" else "tenant"
    try:
        event = _in_scope(access, actor, tenant_id, lambda tx: stops.stop(
            tx, scope=scope, cause="manual", reason=reason, evidence=EVIDENCE,
            tenant_id=None if scope == "system" else tenant_id)["event_id"])
    except Exception as e:  # noqa: BLE001  データベースへ届かなくても、印で止まっている（そのことを返す）
        return {"flag": str(path), "db_event": None, "db_error": f"{type(e).__name__}：{e}" if isinstance(e, PermissionError)
                else type(e).__name__}
    _write_flag(dict(flag, db_event=event))
    return {"flag": str(path), "db_event": event, "db_error": None}


def _open_by_this_switch(tx: Tx) -> list[int]:
    """この道で記録した、まだ解かれていない緊急停止のうち、取引の範囲のもの（全体の取引＝全体／契約者の取引＝その契約者）。"""
    return [r["event_id"] for r in tx.all(
        "SELECT s.event_id FROM salescall.open_stop_events s WHERE s.cause = 'manual' AND s.evidence = %s"
        " AND CASE WHEN salescall.cur_tenant() IS NULL THEN s.scope = 'system'"
        "          ELSE s.scope = 'tenant' AND s.tenant_id = salescall.cur_tenant() END", (EVIDENCE,))]


def release_all(access: Access | None, actor: Actor, reason: str, *, tenant_id: UUID | None = None) -> dict[str, Any]:
    """解く：この道で記録した緊急停止（押した範囲のもの）を、データベースで解いてから、印を消す。届かなければ印は消さない。
    印が別の範囲で押されたものなら、解かない。"""
    if not reason.strip():
        raise ValueError("解く理由を書いてください")
    if access is None:
        return {"released": False, "resumed": [], "db_error": "データベースへのつなぎ先がありません（PC の準備：docs/PC_SETUP.md）"}
    flag = _read_flag()
    if flag is not None and flag.get("db_event") is not None and (flag.get("kind"), str(flag.get("tenant_id"))) != (
            actor.kind, str(tenant_id if actor.kind == "operator" else None)):
        return {"released": False, "resumed": [], "db_error": "印は別の範囲で押されたものです（その範囲の管理者か契約者が解きます）"}
    try:
        resumed = _in_scope(access, actor, tenant_id, lambda tx: [stops.resume(tx, e, reason=reason, evidence=EVIDENCE)
                                                                  for e in _open_by_this_switch(tx)])
    except Exception as e:  # noqa: BLE001
        return {"released": False, "resumed": [], "db_error": type(e).__name__}
    flag_path().unlink(missing_ok=True)
    return {"released": True, "resumed": resumed, "db_error": None}


def _who_and_where() -> tuple[Access | None, Actor | None, UUID | None, str | None]:
    """つなぎ先と、押した管理者か契約者（見守り役と同じ `supervisor.config.load` の1か所）。分からなければ (None, None, None, 理由)。
    どんな誤り（設定の抜け・PC の鍵のファイルが壊れている など）でも、止める印を残せるように、ここで受ける（止めることは、いつでも
    できる。⚠️ 前は設定の抜けだけを受け、鍵のファイルが壊れていると印を書く前に落ちた＝見直し4）。ここはファイルを読むだけで、
    データベースにはつながない（鍵が止められたことが分かるのは、つないだとき＝`_in_scope`）。"""
    import socket
    from salescall.supervisor import config
    try:
        cfg = config.load()
    except config.ConfigMissing as e:
        return None, None, None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, None, None, f"つなぎ先を読めません（{type(e).__name__}）。PC でログインし直してください（docs/PC_SETUP.md）"
    admin = bool(cfg.environ.get("SALESCALL_DSN", "").strip())   # 管理者の PC（システムの接続）／契約者の PC（PC の鍵）
    who = Actor(f"{'管理者' if admin else '契約者'}の PC の緊急停止（{socket.gethostname()}）", "admin" if admin else "operator")
    return Access(cfg.dsn), who, None if admin else cfg.tenant_id, None


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("stop", "status", "release"):
        print('使い方：python -m salescall.core.emergency stop "理由" | status | release "理由"')
        return 2
    access, actor, tenant_id, missing = _who_and_where()   # つなぎ先が無くても、止める（印を残す）ことはできる
    reason = " ".join(argv[1:])
    if argv[0] == "status":
        got = engaged()
        if got is not None and access is not None and actor is not None:
            try:                                       # 画面から解かれていれば、印を消す（判断は engaged）
                got = _in_scope(access, actor, tenant_id, engaged)
            except Exception:  # noqa: BLE001  届かなければ、印のとおり
                pass
        print("止めています：" + json.dumps(got, ensure_ascii=False) if got else "止めていません（このPCの印はありません）")
        return 0
    if argv[0] == "stop":
        got = stop_all(access, actor, reason, tenant_id=tenant_id, no_actor=missing or "")
        print("このPCの印を置きました（発信の出口は、かけません）：" + got["flag"])
        print("データベースにも記録しました" if got["db_event"] else
              f"⚠️ データベースへ記録できませんでした（{got['db_error']}）。印だけで止まっています。直したら、もう一度押してください")
        return 0
    if actor is None:
        print(f"⚠️ 解いていません（{missing}）。印はそのままです")
        return 1
    got = release_all(access, actor, reason, tenant_id=tenant_id)
    print("解きました" if got["released"] else f"⚠️ 解いていません（{got['db_error']}）。印はそのままです")
    return 0 if got["released"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
