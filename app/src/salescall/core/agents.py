"""PC ごとの通行証と、PC が動いているかの見張り（DESIGN.md 5-5。表の作り 0105）。持ち主 CL-2。

2026-10-21 ユーザー：「最近はREAなどでプログラムを読み取るのが簡単になっているようなので、読み取られないように対応する必要があります。
その上での最適解を考えてください」⇒ 契約者の PC には電話をつなぐ部分（PC の部品）だけを置き、データベースにつながない。PC は
サーバー（中央の見守り役）にだけつなぎ、サーバーは PC の通行証で、どの契約者のどの PC かを決める。

- **払い出しは `_put` の1か所**（機器のログインが済んだとき＝`login_take`、移る間の形の見守り役・管理者＝`issue`。払い出せるのは
  システムの処理か管理者だけ＝データベースが守る）。
  通行証（でたらめな長い文字）は返すのは1回だけで、どこにも保存しない（データベースには指紋＝`fingerprint` だけ）。同じ PC で
  ログインし直したら、前の通行証を止める（`replaces`）。
- **止めるのは `revoke` の1か所**（画面の「この PC のログインを外す」＝管理者か契約者・契約の終わり。止めた印は1回だけ）。
- **サーバーが通行証から PC を決めるのは `identify` の1か所**（止めた通行証は通らない）。動いている合図は `beat`（つないでいる
  回線は、その契約者の、使わなくなった印の無い回線だけが残る）。
- **PC のログイン（RFC 8628 の機器のログイン）の待ちは、データベースの1か所**（表の作り 0241。画面のサーバーが2台以上でも同じ記録が
  見える＝✅ 2026-11-18 ユーザー「契約者が増えたら、2台以上は確実なんじゃないの？」）：PC が始める `login_start`→ 契約者がページで
  確かめて許可する・断る `login_waiting`・`login_decide`→ PC が受け取る `login_take`（許可済みなら、待ちを消すのと払い出しが同じ取引＝
  1回だけ）。画面のサーバー（CL-3）の入口 `/pc-login/start`・`/pc-login/token`・`/pc-login` は、これらを呼ぶだけ。
- **PC が止まったら知らせる**（`watch_down`。中央の見守り役の契約者ごとの仕事）：合図が設定 `pc.down_after` より途切れた PC を、
  その契約者と管理者へ知らせる（1回の途切れに1回。止めない＝その PC の回線は借用の期限で門が通さない。PC はいつも動いている
  前提＝Intent.md 14-4）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import notify, settings
from salescall.core.access import Access, Actor, Tx
from salescall.link.basics import PcPass

DOWN = "PC（電話をつなぐ部品）からの動いている合図が途切れました。その PC の回線では電話をかけていません。PC の電源・ネット・PC の部品が動いているかを確かめてください"


Pass = PcPass   # 払い出した通行証（形は link.basics.PcPass の1か所＝PC がしまう形と同じ。返すのは1回だけ。保存しない）


def fingerprint(token: str) -> str:
    """通行証の指紋（データベースに残すのは、これだけ）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(app: Access, actor: Actor, tenant_id: UUID, *, label: str, replaces: UUID | None = None) -> Pass:
    """その契約者の PC に、通行証を払い出す（機器のログインが済んだとき）。replaces＝同じ PC が前に持っていた PC の番号（止める）。"""
    token = secrets.token_urlsafe(32)
    return Pass(tenant_id, app.in_tenant(actor, tenant_id, lambda tx: _put(tx, label, token, replaces)), token)


def _put(tx: Tx, label: str, token: str, replaces: UUID | None) -> UUID:
    """取引の契約者の PC に、その通行証の指紋を置き、前の PC の通行証を止める（払い出しの1か所。`issue`・`login_take` が呼ぶ）。"""
    pc = tx.one("INSERT INTO salescall.pc_agents (tenant_id, label, token_sha256) VALUES (salescall.cur_tenant(), %s, %s) RETURNING pc_id",
                (label.strip(), fingerprint(token)))["pc_id"]
    if replaces is not None:
        revoke(tx, replaces, missing_ok=True)
    return pc


# ═════════ PC のログインの待ち（表の作り 0241） ═════════
LOGIN = Actor("PC のログインの入口", "system")   # 始める・聞くのは、サーバーの全体の取引のシステムの処理
LOGIN_EXPIRES = 600      # 確かめのコードの期限（秒）
LOGIN_EVERY = 5          # PC が受け取りを聞いてよい間隔（秒）。これより早く聞けば slow_down（その待ちの間隔を5秒延ばす）
LABEL_MAX = 60
_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"   # 読み違えにくい字だけ（母音・0/O・1/I を除く。表の作りの CHECK と同じ）
PENDING, SLOW_DOWN, DENIED, EXPIRED = "authorization_pending", "slow_down", "access_denied", "expired_token"


@dataclass(frozen=True)
class LoginStart:
    """始めた待ち（PC へ返す。device_code は PC だけが持つ＝データベースには指紋だけ）。"""
    device_code: str
    user_code: str
    interval: int
    expires_in: int


@dataclass(frozen=True)
class LoginWaiting:
    """ページに出す待ち（契約者が、PC の画面のコードと同じかを確かめる）。replaces_label＝入れ替える前の PC の名前（その契約者の PC のときだけ）。"""
    user_code: str
    pc_label: str
    replaces_label: str | None
    expires_at: datetime


def login_code(code: str) -> str:
    """契約者が打った確かめのコードを、待ちの形（ABCD-EFGH）にそろえる（小文字・区切りの違いを吸う）。"""
    c = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 else (code or "").strip().upper()


def login_start(app: Access, *, label: str | None, replaces: str | UUID | None = None) -> LoginStart:
    """PC がログインを始める（ログイン不要）。label＝PC の名前（空なら「名前の無い PC」）。replaces＝この PC が前に持っていた PC の番号
    （ログインし直したとき。許可した契約者の PC なら、受け取ったときに止める。形が違えば無いもの）。"""
    name = (label or "").strip()[:LABEL_MAX] or "名前の無い PC"
    try:
        before = UUID(str(replaces)) if replaces else None
    except ValueError:
        before = None
    device = secrets.token_urlsafe(32)
    for _ in range(20):   # 確かめのコードが、いまの待ちと重なったら別のコードで
        code = "".join(secrets.choice(_LETTERS) for _ in range(8))
        code = f"{code[:4]}-{code[4:]}"
        if app.in_system(LOGIN, lambda tx: tx.one("SELECT salescall.pc_login_start(%s, %s, %s, %s, %s, %s) AS ok",
                                                   (fingerprint(device), code, name, before, LOGIN_EXPIRES, LOGIN_EVERY))["ok"]):
            return LoginStart(device, code, LOGIN_EVERY, LOGIN_EXPIRES)
    raise RuntimeError("確かめのコードを作れませんでした（いまの待ちと重なり続けた）")


def login_waiting(tx: Tx, code: str) -> LoginWaiting | None:
    """ページに出す待ち（取引の契約者の、ログインした管理者か契約者だけ＝データベースが守る）。まだ決めていない・期限の内だけ。"""
    c = login_code(code)
    row = tx.one("SELECT pc_label, replaces_label, expires_at FROM salescall.pc_login_waiting(%s)", (c,))
    return None if row is None else LoginWaiting(c, row["pc_label"], row["replaces_label"], row["expires_at"])


def login_decide(tx: Tx, code: str, *, approve: bool) -> str | None:
    """契約者が許可する・断る（取引の契約者の、ログインした管理者か契約者だけ。許可した契約者の PC になる）。決めた PC の名前を返す。
    まだ決めていない・期限の内の待ちが無ければ None。"""
    return tx.one("SELECT salescall.pc_login_decide(%s, %s) AS label", (login_code(code), approve))["label"]


def login_take(app: Access, device_code: str) -> tuple[str, Pass | None]:
    """PC が受け取りを聞く。許可済みなら通行証を払い出して ("ok", 通行証)（待ちを消すのと払い出しが同じ取引＝1回だけ。前の PC の通行証は
    止める）。ほかは (PENDING／SLOW_DOWN／DENIED／EXPIRED, None)。誰が許可したかは、払い出した処理の名前に残す。"""
    sha = fingerprint(device_code or "")
    got = app.in_system(LOGIN, lambda tx: tx.one("SELECT state, tenant_id, decided_by FROM salescall.pc_login_poll(%s)", (sha,)))
    if got["state"] != "approved":
        return got["state"], None
    token = secrets.token_urlsafe(32)

    def work(tx: Tx) -> UUID | None:
        row = tx.one("SELECT pc_label, replaces FROM salescall.pc_login_take(%s)", (sha,))
        return None if row is None else _put(tx, row["pc_label"], token, row["replaces"])
    pc = app.in_tenant(Actor(f"{LOGIN.name}（許可：{got['decided_by']}）", "system"), got["tenant_id"], work)
    return (EXPIRED, None) if pc is None else ("ok", Pass(got["tenant_id"], pc, token))


def revoke(tx: Tx, pc_id: UUID, *, missing_ok: bool = False) -> bool:
    """その PC の通行証を止める（取引の契約者の PC だけ。もう止めてあれば何もしない）。止めたかを返す。無ければ LookupError。"""
    row = tx.one("UPDATE salescall.pc_agents SET revoked_at = now(), revoked_by = salescall.cur_actor()"
                 " WHERE pc_id = %s AND revoked_at IS NULL RETURNING pc_id", (pc_id,))
    if row is None and not missing_ok and tx.one("SELECT 1 AS x FROM salescall.pc_agents WHERE pc_id = %s", (pc_id,)) is None:
        raise LookupError(f"その PC はありません：{pc_id}")
    return row is not None


def pcs(tx: Tx) -> list[dict[str, Any]]:
    """取引の契約者の PC の一覧（画面の「この PC のログインを外す」。通行証の指紋は返さない）。"""
    return tx.all("SELECT pc_id, label, issued_at, revoked_at, last_seen_at, lines FROM salescall.pc_agents"
                  " ORDER BY issued_at, pc_id")


@dataclass(frozen=True)
class Who:
    tenant_id: UUID
    pc_id: UUID
    label: str


def identify(tx: Tx, token: str) -> Who | None:
    """通行証から、どの契約者のどの PC かを決める（サーバーの全体の取引だけ）。止めた・知らない通行証なら None。"""
    row = tx.one("SELECT pc_id, tenant_id, label FROM salescall.agent_by_token(%s)", (fingerprint(token),))
    return None if row is None else Who(row["tenant_id"], row["pc_id"], row["label"])


def beat(tx: Tx, pc_id: UUID, lines: list[UUID]) -> list[UUID] | None:
    """動いている合図（サーバーの全体の取引だけ）。残った回線（その契約者の、使っている回線だけ）を返す。止めた通行証なら None。"""
    return tx.one("SELECT salescall.agent_beat(%s, %s::uuid[]) AS kept", (pc_id, list(lines)))["kept"]


def watch_down(tx: Tx, *, alive: frozenset[UUID] = frozenset(), listening_for: float | None = None) -> list[UUID]:
    """取引の契約者の PC のうち、合図が途切れたものを、その契約者と管理者へ知らせる（1回の途切れに1回。合図が一度も無い PC は、
    払い出してから同じ長さで数える）。知らせた PC を返す。判断はこの1か所：
    - alive＝いまつなぎ口につないでいる PC（動いている。データベースが止まっていて合図を書けなかった間も、途切れとしない）
    - listening_for＝つなぎ口が待ち受けを始めてからの秒数（その長さが途切れの長さに足りないあいだは、だれも途切れとしない＝見守り役
      が立ち上がった直後は、PC がつなぎ直す前に「途切れた」と誤って知らせない）。None＝つなぎ口が無い（合図の記録だけで見る）
    ⛔ 2026-10-22 の見直し：前は、見守り役やデータベースが数分止まったあと、元気な PC まで「途切れた」とすべての契約者と管理者へ
    知らせた。"""
    after = timedelta(seconds=int(settings.system_value(tx, "pc.down_after")))
    if listening_for is not None and listening_for < after.total_seconds():
        return []
    down = []
    for pc in tx.all("SELECT pc_id, label, coalesce(last_seen_at, issued_at) AS since FROM salescall.pc_agents"
                     " WHERE revoked_at IS NULL AND coalesce(last_seen_at, issued_at) < now() - %s AND NOT pc_id = ANY(%s::uuid[])"
                     " ORDER BY pc_id", (after, list(alive))):
        if notify.emit(tx, "pc_down", key=f"pc_down:{pc['pc_id']}:{pc['since'].isoformat()}", summary=DOWN,
                       refs={"pc_id": pc["pc_id"], "pc": pc["label"], "since": pc["since"]}, admin=True, contractor=True):
            down.append(pc["pc_id"])
    return down
