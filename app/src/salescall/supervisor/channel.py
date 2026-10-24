"""つなぎ口（サーバーの側。DESIGN.md 5-5）。PC の部品からのつなぎを受け、`links.Links` の形で中央の見守り役へ見せる。持ち主 CL-2。

- やり取りの形は `salescall.link.protocol` の1か所。PC から外向きの1本（WebSocket）。暗号化して相手を確かめるのはサーバーの入口。
- **つないだとき**：PC が通行証と版を渡す。通行証から契約者と PC を決める（`agents.identify`。止めた・知らない通行証は断る＝PC は
  ログインし直す）。版が合わなければ断り、その契約者と管理者へ「更新してください」（`updates.pc_out_of_date`）。同じ PC の前の
  つなぎは切る（1つの PC は1本だけ）。
- **動いている合図**：データベースへ書き（`agents.beat`。名乗った回線は、その契約者の使っている回線だけが残る）、つないでいる回線・
  控え用のハードディスクと鍵があるか・緊急停止の印を覚える。緊急停止の印がある PC の回線は、中央の見守り役に見せない（かけない）。
- **同じ回線を2つの PC が名乗ったら**、先につないだほうを中央の見守り役に見せ（入れ替わり続けない）、その契約者へ知らせる
  （`pc_line_conflict`。もう一方の PC の設定を直してもらう）。先につないだ PC が緊急停止を押していれば、その回線ではかけない。
- 中央の見守り役に見せる回線（`links.Attached`）は、**つなぎ（1本ごとの番号）**まで持つ。電話の部品（`AgentDriver`）は、作ったときの
  つなぎに決まる（通話の途中で、頼み・声が別の PC・つなぎ直した新しいつなぎへ行かない。替われば、中央の見守り役が回線の流れを止めて
  立ち上げ直し、止まった試行を後始末する）。
- **通話の途中で通行証が止められたら**（画面の「この PC のログインを外す」・契約の終わり）、その PC の回線をすぐ見せなくし（新しい
  電話をかけない）、いまの通話が終わってから断ってつながりを閉じる（PC はログインし直す）。いきなり閉じると、PC が話している
  お客様をおわびの録音で切るため（2026-10-22 ユーザー：「本番でいきなり止めてはいけません」）。通話が `REVOKED_WAIT` を過ぎても
  終わらなければ閉じる（止めた通行証で、いつまでもつなぎ続けない）。
- **かけてから切れるまでの回線の印**（`_Session.open_calls`。控えを送る速さ・通行証が止められたあとに閉じる時を決める）：出し入れは
  `_Session.call_began`・`call_over` の1か所。かけたら付け、切った・おわびして切った・電話の部品が「通話中ではない」と答えたら外す。
  **お客様が自分で切ったとき**のように、サーバーが切っていない通話も、印が付いたまま声が `protocol.CALL_QUIET` 秒途切れたら、合図の
  たびに電話の部品に「通話中か」を聞いて外す（`_refresh_calls`。通話中かを知っているのは電話の部品だけ＝決まりは同じ1か所。⛔ 2026-10-22
  の見直し15：前は外れず、ログインを外した PC を20分つなぎ続け、控えをいつまでもゆっくり送った）。
- 1つのやり取りの誤り（形の違い・受け口の誤り）では、つなぎを切らない（記録して続ける。切ると、その PC のすべての通話が切れる）。
- 切れたことに数秒で気づく（`protocol.PING_EVERY`）。
- **控えの中身は、通話の声を遅らせない速さで送る**（`protocol.window` の1か所。PC が書けた印を待ちながら送り、その PC で通話の声・
  頼みがあるあいだは1つずつ）。PC が「書けない」と先に答えたら、送るのをやめる。
- **頼む**（電話の部品＝`AgentDriver`・控え＝`RemoteWriter`）：中央の見守り役の流れから頼み、答えを待つ（待ち過ぎたら「分からない」＝
  かけない側）。つながりが切れたら、待っている頼みは誤りで返す。
- **PC の上の緊急停止**：PC から届いたら、その契約者の緊急停止を記録し（`core.emergency`）、記録の番号を PC へ返す。画面から解かれて
  いたら、PC へ伝える（PC は印を消す）。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from salescall.core import agents, emergency, notify
from salescall.core.access import Access
from salescall.link import protocol as p
from salescall.link.phone import CallNotPlaced, CallWithheld
from salescall.link.protocol import RemoteError
from salescall.supervisor import ACTOR, CALL_WAIT, config, report, updates
from salescall.supervisor.links import Attached

HELLO_WAIT = 10   # つないでから、最初のやり取り（hello）を待つ長さ（秒）
REVOKED_WAIT = CALL_WAIT   # 通行証が止められたあと、いまの通話が終わるのを待つ長さの上限（秒。通話の最長から決める＝supervisor の1か所）
_SERIAL = itertools.count(1)
AWAY = "控えを書く PC が、サーバーとのつながりを失いました。PC の電源・ネット・PC の部品が動いているかを確かめてください"
LINE_CONFLICT = ("同じ回線（スマホ）を、2台の PC がつないでいます。先につないだ PC でかけています。もう一方の PC の設定"
                 "（SALESCALL_LINES）から、その回線を外してください")

# PC の部品が「かけていない」と答えたときの誤りの名前と、発信の出口へ上げる形（名前と形の対応はこの1か所。だれのせいかは、ここでも
# PC でも決めない＝発信の出口が「発信に失敗」にし、データベースの salescall.unrung_blame が証拠で決める＝0129）
NOT_PLACED: dict[str, tuple[type[Exception], str]] = {
    "withheld": (CallWithheld, "PC の部品がかけようとしませんでした（PC の上で緊急停止が押されている・前の頼みを待つうちに答えの期限を過ぎた）"),
    "not_placed": (CallNotPlaced, "電話の部品がかけようとして、かけられませんでした（だれのせいかは、同じ回線の前後の電話で決めます）"),
}


@dataclass
class _Session:
    ws: ServerConnection
    who: agents.Who
    conn: int = field(default_factory=lambda: next(_SERIAL))   # つなぎの番号（つなぎ直せば変わる）
    lines: list[UUID] = field(default_factory=list)
    drive: bool = False
    key: bool = False
    emergency: dict[str, Any] | None = None
    voice_at: float = 0.0                                      # 通話の声・頼み・電話の出来事が最後に通った時刻（控えを送る速さ）
    pending: dict[UUID, asyncio.Future] = field(default_factory=dict)
    flows: dict[UUID, "_Flow"] = field(default_factory=dict)  # 送っている控え → PC が書けた数
    order: asyncio.Lock = field(default_factory=asyncio.Lock)   # 合図と緊急停止は、届いた順に1つずつ（データベースを待つあいだも声は止めない）
    tasks: set[asyncio.Task] = field(default_factory=set)
    next_beat: dict[str, Any] | None = None                    # まだ扱っていない、いちばん新しい合図（溜めない）
    # かけてから切れるまでの回線 → 付けた印の番号（「かけて」を頼んでから、切る・おわびして切る・「通話中ではない」の答えまで）。
    # 呼び出しのあいだのように声が流れなくても通話中とみなす（⛔ 2026-10-22 の見直し：声の有無だけで決めると、呼び出しが5秒を超えた
    # あいだに控えをまとめて送り、お客様が出た直後の AI の声がその後ろで待った）。出し入れは call_began・call_over だけ
    open_calls: dict[UUID, int] = field(default_factory=dict)
    marks: threading.Lock = field(default_factory=threading.Lock)
    refreshing: bool = False                                   # 印の確かめ（_refresh_calls）の途中
    revoked_at: float | None = None                            # 通行証が止められたと分かった時刻（回線を見せず、通話が終わったら閉じる）

    def calling(self) -> bool:
        """通話中か（控えを送る速さを決める。判断はこの1か所）：かけてから切れるまでの回線があるか、声が少し前まで通っていた。"""
        return bool(self.open_calls) or time.monotonic() - self.voice_at < p.CALL_QUIET

    def voice(self) -> None:
        self.voice_at = time.monotonic()

    def call_began(self, line: UUID) -> int:
        """その回線で、かけ始めた（印を付ける）。付けた印の番号を返す。"""
        with self.marks:
            mark = self.open_calls[line] = next(_SERIAL)
        return mark

    def mark_of(self, line: UUID) -> int | None:
        with self.marks:
            return self.open_calls.get(line)

    def call_over(self, line: UUID, mark: int | None = None) -> None:
        """その回線の通話は終わった（印を外す）。mark＝「通話中か」を聞いたときの印：そのあいだに、かけ直していれば（印が替わって
        いれば）外さない（聞いたのは前の通話のこと）。"""
        with self.marks:
            if mark is None or self.open_calls.get(line) == mark:
                self.open_calls.pop(line, None)


@dataclass
class _Flow:
    """送っている1つの控えの、PC が書けた数（backup_ack）。"""
    acked: int = 0
    at: float = field(default_factory=time.monotonic)          # 最後に書けた数が進んだ時刻
    moved: asyncio.Event = field(default_factory=asyncio.Event)

    def ack(self, n: Any) -> None:
        if isinstance(n, int) and not isinstance(n, bool) and n > self.acked:
            self.acked, self.at = n, time.monotonic()
        self.moved.set()


class ChannelDown(Exception):
    """PC の部品のつなぎ口が、待ち受けを始められない。"""


class Hub:
    """つなぎ口（中央の見守り役と同じ処理の中で、別の流れの中で動く）。"""

    def __init__(self, app: Access, cfg: config.Config) -> None:
        self.app, self.cfg = app, cfg
        self.sessions: dict[UUID, _Session] = {}       # PC → つなぎ
        self.sinks: dict[UUID, tuple[int, Callable[[bytes], None]]] = {}   # 回線 → (つなぎ, 届いた声の受け口＝通話を進める部品)
        self.events: dict[UUID, tuple[int, Callable[[dict[str, Any]], None]]] = {}   # 回線 → (つなぎ, 電話の出来事の受け口)
        self.lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port: int | None = None
        self._ready = threading.Event()
        self.closing = False                           # 止め始めた（取りやめた頼みを、失敗として記録しない）
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None        # つなぎ口の流れが終わった誤り（待ち受けを始められなかった など）

    # ── 動かす・止める ──
    def start(self) -> None:
        """待ち受けを始める（止まったあとに呼べば、動かし直す）。始められなければ ChannelDown（⛔ 2026-10-22 の見直し：前は、待ち受けに
        失敗しても黙って動き続け、どの PC もつながらないのに、契約者に「あなたの PC が止まった」と誤って知らせた）。"""
        self._ready.clear()
        self.loop, self.port, self.error, self.closing = None, None, None, False
        with self.lock:   # 前の流れのつなぎは、もう無い（落ちた流れが片付けられなかった分も）
            self.sessions.clear()
        self._thread = threading.Thread(target=self._run, name="salescall-channel", daemon=True)
        self._thread.start()
        self._ready.wait(30)
        if self.port is None:
            raise ChannelDown(f"PC の部品のつなぎ口が、待ち受けを始められません（設定 SALESCALL_LISTEN を確かめてください。"
                              f"{type(self.error).__name__ if self.error else '30秒で始まらない'}）")

    def running(self) -> bool:
        """つなぎ口の流れが動いているか（中央の見守り役が、止まっていれば動かし直す）。"""
        return self._thread is not None and self._thread.is_alive() and self.error is None

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 待ち受けを始められない・流れが落ちた：記録して、始める側・見守り役に知らせる
            self.error = e
        finally:
            self._ready.set()
            try:   # この流れのデータベースを待つ流れの組を片付ける（動かし直すたびに残さない）
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve(self) -> None:
        host, port = self.cfg.listen or ("127.0.0.1", 0)
        self._stop = asyncio.Event()
        async with serve(self._handle, host, port, max_size=p.MAX_FRAME, compression=None,
                         ping_interval=p.PING_EVERY, ping_timeout=p.PING_EVERY) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.listening_since = time.monotonic()
            self._ready.set()
            await self._stop.wait()
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in rest:   # 閉じるとき：残っている合図の処理を終わらせる（データベースの取引は、それぞれ終わるか戻される）
            t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)

    def close(self) -> None:
        self.closing = True
        if self.loop is not None and self._stop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:   # ちょうど流れが終わって閉じた：もう止まっている
                pass
        if self._thread is not None:
            self._thread.join(10)

    async def _db(self, work: Callable[[], Any]) -> Any:
        """データベースを使う処理は、別の流れで（つなぎ口の流れでデータベースを待つと、すべての PC の声と生存確認が止まる）。"""
        return await asyncio.get_running_loop().run_in_executor(None, work)

    def _report(self, tenant_id: UUID | None, what: str, error: BaseException) -> None:
        """誤りの記録は、別の流れで、待たずに（データベースが止まっているときに、つなぎ口の流れを止めない）。"""
        asyncio.get_running_loop().run_in_executor(None, report, self.app, tenant_id, what, error)

    # ── 1本のつなぎ ──
    async def _handle(self, ws: ServerConnection) -> None:
        session = None
        try:
            hello = p.loads(await asyncio.wait_for(ws.recv(), HELLO_WAIT), frozenset({"hello"}))
            who = await self._db(lambda: self.app.in_system(ACTOR, lambda tx: agents.identify(tx, str(hello.get("token", "")))))
            if who is None:
                await ws.send(p.dumps("refuse", why="この PC のログインが止められているか、分かりません。ログインし直してください",
                                      relogin=True, update=False))
                return
            if hello.get("v") != p.VERSION:
                await self._db(lambda: self.app.in_tenant(ACTOR, who.tenant_id, lambda tx: updates.pc_out_of_date(
                    tx, who.tenant_id, who.pc_id, can_replace=bool(hello.get("updater")))))
                await ws.send(p.dumps("refuse", why="PC のプログラムの更新が要ります", relogin=False, update=True))
                return
            session = _Session(ws, who)
            kept = await self._beat(session, hello)
            if kept is None:
                return
            await ws.send(p.dumps("welcome", tenant=who.tenant_id, pc=who.pc_id, lines=kept))   # 頼みより先に届ける
            with self.lock:
                old = self.sessions.get(who.pc_id)
                self.sessions[who.pc_id] = session
            if old is not None:   # 同じ PC の前のつなぎ（1つの PC は1本だけ。待たずに閉じる）
                asyncio.get_running_loop().create_task(old.ws.close())
            await self._emergency_state(session)
            async for message in ws:
                try:
                    await self._receive(session, message)
                except ConnectionClosed:
                    raise
                except Exception as e:  # noqa: BLE001 1つのやり取りの誤りで、つなぎを切らない（切ると、その PC のすべての通話が切れる）
                    self._report(who.tenant_id, "つなぎ口のやり取り", e)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except Exception as e:  # noqa: BLE001 1本のつなぎの誤りで、つなぎ口を止めない
            self._report(None if session is None else session.who.tenant_id, "つなぎ口", e)
        finally:
            if session is not None:
                with self.lock:
                    if self.sessions.get(session.who.pc_id) is session:
                        del self.sessions[session.who.pc_id]
                for f in session.pending.values():
                    if not f.done():
                        f.set_exception(ConnectionError("PC の部品とのつながりが切れました"))

    async def _beat(self, session: _Session, msg: dict[str, Any]) -> list[UUID] | None:
        """動いている合図（hello も同じ）。止めた通行証なら断って None。"""
        kept = await self._db(lambda: self.app.in_system(
            ACTOR, lambda tx: agents.beat(tx, session.who.pc_id, p.uuids(msg.get("lines")))))
        if kept is None:
            # 止められた通行証：回線はすぐ見せなくし（`lines`）、いまの通話が終わってから閉じる（判断はこの1か所。⛔ 2026-10-22 の
            # 見直し14：前はその場で閉じ、PC が話しているお客様をおわびの録音で切った）
            if session.revoked_at is None:
                session.revoked_at = time.monotonic()
            if session.open_calls and time.monotonic() - session.revoked_at < REVOKED_WAIT:
                return None
            await session.ws.send(p.dumps("refuse", why="この PC のログインが止められました。ログインし直してください",
                                          relogin=True, update=False))
            await session.ws.close()
            return None
        session.lines = list(kept)
        session.drive, session.key = bool(msg.get("drive")), bool(msg.get("key"))
        session.emergency = msg.get("emergency") if isinstance(msg.get("emergency"), dict) else None
        with self.lock:
            # 同じ PC の前のつなぎ（すぐつなぎ直して、サーバーがまだ前のつなぎを持っている）は数えない（2026-10-22 の見直し：
            # 「2台の PC が同じ回線を使っている」と誤って知らせた）
            shared = sorted({line for o in self.sessions.values()
                             if o.who.pc_id != session.who.pc_id and o.who.tenant_id == session.who.tenant_id
                             for line in o.lines if line in session.lines}, key=str)
        if shared:
            await self._db(lambda: self.app.in_tenant(ACTOR, session.who.tenant_id, lambda tx: [notify.emit(
                tx, "pc_line_conflict", key=f"pc_line_conflict:{line}:{tx.today()}",
                summary=LINE_CONFLICT, refs={"line_id": line}) for line in shared]))
        return session.lines

    async def _receive(self, session: _Session, message: str | bytes) -> None:
        if isinstance(message, bytes):
            kind, ident, payload = p.unframe(message)
            if kind == p.AUDIO and (sink := self._sink(self.sinks, session, ident)) is not None:
                session.voice()
                sink(payload)
            return
        msg = p.loads(message, p.PC_TO_SERVER - {"hello"})
        kind = msg["t"]
        if kind == "beat" and session.next_beat is not None:
            # まだ扱っていない合図があれば、いちばん新しいものに置き換える（データベースが遅いあいだ、PC ごとに溜め続けない。
            # 合図は、そのときの状態を丸ごと運ぶので、古いものは要らない＝⛔ 2026-10-22 の見直し）
            session.next_beat = msg
        elif kind in ("beat", "emergency"):   # データベースを待つ：別に動かす（届いた順に1つずつ）
            if kind == "beat":
                session.next_beat = msg
            task = asyncio.get_running_loop().create_task(self._in_order(session, None if kind == "beat" else msg))
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
        elif kind in ("reply", "backup_done"):
            f = session.pending.pop(p.uuid_of(msg.get("id")), None)
            if f is not None and not f.done():
                f.set_result(msg)
        elif kind == "backup_ack":
            flow = session.flows.get(p.uuid_of(msg.get("id")))
            if flow is not None:
                flow.ack(msg.get("n"))
        elif kind == "phone_event":
            sink = self._sink(self.events, session, p.uuid_of(msg.get("line")))
            if sink is not None and isinstance(msg.get("event"), dict):
                session.voice()
                sink(msg["event"])

    @staticmethod
    def _sink(sinks: dict[UUID, tuple[int, Any]], session: _Session, line: UUID | None) -> Any:
        """届いた声・出来事の受け口（その回線の電話の部品を作った、そのつなぎから届いたものだけ）。"""
        got = sinks.get(line) if line is not None and line in session.lines else None
        return got[1] if got is not None and got[0] == session.conn else None

    async def _in_order(self, session: _Session, msg: dict[str, Any] | None) -> None:
        """合図（msg=None＝その順番が来たときの、いちばん新しい合図）と緊急停止を、届いた順に1つずつ。"""
        try:
            async with session.order:
                if msg is None:
                    msg, session.next_beat = session.next_beat, None
                    if msg is None:
                        return
                if msg["t"] == "emergency":
                    await self._emergency(session, msg)
                    return
                if await self._beat(session, msg) is not None:
                    await self._emergency_state(session)
            self._refresh_calls(session)
        except ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001 1つの合図の誤りで、つなぎを止めない（次の合図でまた合わせる）
            self._report(session.who.tenant_id, "つなぎ口の合図", e)

    def _refresh_calls(self, session: _Session) -> None:
        """印が付いたまま声が CALL_QUIET 秒途切れた回線は、電話の部品に「通話中か」を聞き、通話中でなければ印を外す（お客様が自分で
        切った・電話が自分で終わった通話。上の説明）。別の流れで、待たずに（合図・緊急停止の順番を止めない）。重ねて聞かない。"""
        if session.refreshing or not session.open_calls or time.monotonic() - session.voice_at < p.CALL_QUIET:
            return
        session.refreshing = True
        with session.marks:
            lines = list(session.open_calls)

        def work() -> None:
            for line in lines:
                try:
                    self.in_call(session, line, live=False)
                except Exception:  # noqa: BLE001 答えられない：通話中のまま（印を残す＝控えはゆっくり。次の合図でまた聞く）
                    pass
        done = asyncio.get_running_loop().run_in_executor(None, work)
        done.add_done_callback(lambda _: setattr(session, "refreshing", False))

    def in_call(self, session: _Session, line: UUID, *, live: bool = True) -> bool:
        """その回線が通話中かを、電話の部品に聞く（中央の見守り役の流れ・別の流れから）。通話中でなければ印を外す（聞いたあいだに
        かけ直していれば外さない）。答えられなければ誤りを上げる（「分からない＝通話中」と決めるのは発信の出口の1か所）。
        live＝この頼みを通話の動きとして数える（控えをゆっくり送る）。印の確かめ（_refresh_calls）は数えない。"""
        mark = session.mark_of(line)
        got = self.ask(session, "request", live=live, op="in_call", line=line)
        if not got.get("ok"):
            raise RuntimeError("電話の部品が、通話中か答えられませんでした")
        if not got.get("value"):
            session.call_over(line, mark)
        return bool(got.get("value"))

    # ── PC の上の緊急停止 ──
    async def _emergency(self, session: _Session, msg: dict[str, Any]) -> None:
        who, reason = session.who, str(msg.get("reason") or "")
        if msg.get("action") == "release":   # その PC が押した停止だけを解き、PC が知っている番号で伝える（押し直していれば PC は消さない）
            await self._db(lambda: emergency.release(self.app, who.tenant_id, who.pc_id, who.label, reason))
            event = p.event_of(msg.get("event"))
            await session.ws.send(p.dumps("emergency_state", event=event, open=False))
            return
        await self._emergency_state(session)

    async def _emergency_state(self, session: _Session) -> None:
        """PC の印の状態を合わせる：記録がまだなら記録して番号を返す。画面から解かれていれば、PC へ伝える（PC は印を消す）。"""
        flag, who = session.emergency, session.who
        if flag is None:
            return
        event = p.event_of(flag.get("event"))
        if event is not None:
            if not await self._db(lambda: emergency.is_open(self.app, who.tenant_id, event)):
                await session.ws.send(p.dumps("emergency_state", event=event, open=False))   # PC は、番号が同じなら印を消す
            return
        press = str(flag.get("at") or "（押した時刻なし）")
        reason = str(flag.get("reason") or "（理由なし）")
        # 同じ押しは1回だけ記録する（判断は core.emergency.stop の1か所。PC が番号を書くまでの合図・つなぎ直しでも、前の番号が返る。
        # 記録に失敗したら、次の合図でまた試す）
        event = await self._db(lambda: emergency.stop(self.app, who.tenant_id, who.pc_id, who.label, reason, press))
        await session.ws.send(p.dumps("emergency_state", event=event, open=True, at=flag.get("at")))

    # ── 頼む（中央の見守り役の流れから呼ぶ） ──
    def _session_of(self, at: Attached, line_id: UUID) -> _Session:
        """そのつなぎ（その回線をつないでいるときだけ）。つなぎ直していれば、無いものとする（新しいつなぎへ、前の通話の頼みを送らない）。"""
        with self.lock:
            got = self.sessions.get(at.pc_id)
        if got is None or got.conn != at.conn or line_id not in got.lines:
            raise ConnectionError("その回線をつないでいる、そのつなぎがありません")
        return got

    def ask(self, session: _Session, kind: str, *, wait: float = p.REPLY_WAIT, source: Path | None = None, live: bool = True,
            **fields: Any) -> dict[str, Any]:
        """頼んで答えを待つ（中央の見守り役の流れから）。source＝続けて送るファイル（控えの中身。少しずつ読んで送る＝丸ごとメモリに
        持たない。`_stream`）。wait＝答えを待つ長さ（source があれば、送り終えてから）。live＝通話の頼みを通話の動きとして数える
        （控えをゆっくり送る）。つなぎ口の流れ（声・出来事の受け口の中）からは呼べない（自分の流れを止めて、すべての PC のやり取りが
        固まるため）。"""
        if self.loop is None or not self.loop.is_running():   # 止めたつなぎ口に頼むと、答えが来ないまま待ち続ける
            raise ConnectionError("つなぎ口が動いていません")
        if threading.current_thread() is self._thread:
            raise RuntimeError("つなぎ口の流れ（声・出来事の受け口）から、PC の部品への頼みは呼べません（別の流れへ渡してください）")
        ident = p.new_id()
        if kind == "request" and live:   # 通話の頼み：このつなぎで、控えの中身をゆっくり送る
            session.voice()

        async def go() -> dict[str, Any]:
            f = asyncio.get_running_loop().create_future()
            f.add_done_callback(lambda f: f.cancelled() or f.exception())   # 待つ側が先に止まっても、誤りを「受け取られなかった」にしない
            session.pending[ident] = f
            try:
                await session.ws.send(p.dumps(kind, id=ident, **fields))
                if source is not None:
                    await self._stream(session, ident, source, f)
                return await asyncio.wait_for(f, wait)
            finally:
                session.pending.pop(ident, None)
                session.flows.pop(ident, None)
        running = asyncio.run_coroutine_threadsafe(go(), self.loop)
        try:   # 控えの中身を送る長さは、中で決める（書けた印が PROGRESS_WAIT 来なければ止まる）
            return running.result(None if source is not None else wait + 10)
        except BaseException:   # 待ち過ぎ・止める：送っている途中のもの（控えの中身）も止める（やり直しと重ねて送らない）
            running.cancel()
            raise

    @staticmethod
    async def _stream(session: _Session, ident: UUID, source: Path, answered: asyncio.Future) -> None:
        """控えの中身を CHUNK ずつ送る。PC が書けた数を待ちながら、窓（`protocol.window`）を超えて先へ送らない。PC が先に答えたら
        （書けない）やめる。書けた数が PROGRESS_WAIT 進まなければ TimeoutError。"""
        loop = asyncio.get_running_loop()
        flow = session.flows[ident] = _Flow()
        answered.add_done_callback(lambda _: flow.moved.set())
        sent = 0
        with open(source, "rb") as content:
            while not answered.done():
                if sent - flow.acked >= p.window(session.calling()):
                    if time.monotonic() - flow.at > p.PROGRESS_WAIT:
                        raise TimeoutError("PC の部品が、控えの中身を書けた印を返しません")
                    flow.moved.clear()
                    try:   # 書けた印か答えを待つ（1秒ごとに、通話の有無を見直す）
                        await asyncio.wait_for(flow.moved.wait(), 1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                piece = await loop.run_in_executor(None, content.read, p.CHUNK)   # 読むのは別の流れで（声を止めない）
                if not piece:
                    break
                await session.ws.send(p.frame(p.BACKUP, ident, piece))
                sent += 1
        if not answered.done():
            await session.ws.send(p.dumps("backup_end", id=ident))

    # ── links.Links ──
    def lines(self) -> dict[UUID, Attached]:
        """回線ごとに、先につないだつなぎ（入れ替わり続けない）。そのつなぎの PC で緊急停止が押されている・通行証が止められていれば、
        その回線は見せない（中央の見守り役は、いまの通話が終わってから、その回線の流れを止める）。"""
        with self.lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.conn)
        owner: dict[UUID, _Session] = {}
        for s in sessions:
            for line_id in s.lines:
                owner.setdefault(line_id, s)
        return {line_id: Attached(s.who.tenant_id, s.who.pc_id, s.conn) for line_id, s in owner.items()
                if s.emergency is None and s.revoked_at is None}

    def driver(self, line_id: UUID, at: Attached) -> "AgentDriver":
        return AgentDriver(self, line_id, at)

    def writer(self, tenant_id: UUID) -> "RemoteWriter | None":
        """その契約者の控えを書く PC（ハードディスクと鍵がある PC。無ければ、つないでいる PC に頼む＝PC が直す人あての文を返し、
        その契約者に「つないでください」「鍵を入れてください」が届く）。つないでいる PC が無ければ None。"""
        with self.lock:
            got = sorted((s for s in self.sessions.values() if s.who.tenant_id == tenant_id and s.revoked_at is None),
                         key=lambda s: s.conn)
        ready = [s for s in got if s.drive and s.key] or got
        return RemoteWriter(self, ready[0]) if ready else None

    def connected(self) -> tuple[frozenset[UUID], float | None]:
        """いまつないでいる PC と、待ち受けを始めてからの秒数（待ち受けていなければ 0）。"""
        with self.lock:
            pcs = frozenset(self.sessions)
        since = getattr(self, "listening_since", None)
        return pcs, (0.0 if since is None or not self.running() else time.monotonic() - since)

    def whole_writer(self) -> "RemoteWriter | None":
        """データベース全体の控えを書く PC：サーバーの設定で決めた契約者（管理者自身の契約者）の PC だけ（すべての契約者のデータが入る）。"""
        return self.writer(self.cfg.admin_tenant) if self.cfg.admin_tenant else None


@dataclass
class AgentDriver:
    """そのつなぎの、その回線の電話の部品（PC の部品へ頼む形。dial_exit.LineDriver）。通話の声もここを通る。作ったときのつなぎだけに頼む。"""
    hub: Hub
    line_id: UUID
    at: Attached
    voice_type: str | None = None   # 次にかける通話の声の型（prepare_call。PC の電話の部品へ渡す＝おわびの録音もこの声）

    def prepare_call(self, voice_type: str) -> None:
        self.voice_type = voice_type

    def _session(self) -> _Session:
        return self.hub._session_of(self.at, self.line_id)

    def in_call(self) -> bool:
        """答えられない（つながっていない・待ち過ぎ）ときは誤りを上げる（「分からない＝通話中」と決めるのは発信の出口の1か所）。"""
        return self.hub.in_call(self._session(), self.line_id)

    def start_call(self, phone_e164: str) -> None:
        try:
            session = self._session()
        except ConnectionError:   # 頼む前に、つなぎが無いと分かった：かけていないと確かに言える（回線の異常ではない）
            raise CallWithheld("PC の部品がつながっていません") from None
        mark = session.call_began(self.line_id)
        got = self.hub.ask(session, "request", op="start_call", line=self.line_id, phone=phone_e164, voice=self.voice_type)
        if got.get("ok"):
            return
        if got.get("error") in NOT_PLACED:   # かけていない
            session.call_over(self.line_id, mark)
            kind, why = NOT_PLACED[got["error"]]
            raise kind(why)
        raise RuntimeError("電話の部品が、かけたか分からない誤りを返しました")

    def hang_up(self) -> None:
        session = self._session()
        got = self.hub.ask(session, "request", op="hang_up", line=self.line_id)
        session.call_over(self.line_id)
        if not got.get("ok"):
            raise RuntimeError("電話の部品が、切れたか分からない誤りを返しました")

    def apologize_and_hang_up(self) -> None:
        """おわびの録音を流して切る（サーバーの側の誤りで通話を続けられないとき。PC の電話の部品が持っている録音）。"""
        session = self._session()
        got = self.hub.ask(session, "request", op="apologize", line=self.line_id)
        session.call_over(self.line_id)
        if not got.get("ok"):
            raise RuntimeError("電話の部品が、おわびして切れたか分からない誤りを返しました")

    def send_audio(self, audio: bytes) -> None:
        """AI の声・録音を、通話へ流す（PC の部品が電話の部品の play へ渡す。待たずに返す）。"""
        session, loop = self._session(), self.hub.loop
        if loop is None or not loop.is_running():
            raise ConnectionError("つなぎ口が動いていません")
        session.voice()
        asyncio.run_coroutine_threadsafe(session.ws.send(p.frame(p.AUDIO, self.line_id, audio)), loop)

    def on_audio(self, sink: Callable[[bytes], None]) -> None:
        """お客様の声の受け口を決める（通話を進める部品＝CL-1）。つなぎ口の流れから呼ばれるので、待たずに返すこと（中から PC への
        頼みは呼べない＝別の流れへ渡す）。"""
        self.hub.sinks[self.line_id] = (self.at.conn, sink)

    def on_event(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """電話の出来事（呼び出し・応答・切れた など。中身は電話接続の担当が決める）の受け口を決める（待たずに返すこと。上と同じ）。"""
        self.hub.events[self.line_id] = (self.at.conn, sink)


@dataclass
class RemoteWriter:
    """控えを、その PC の部品に書かせる（core.backup.Writer）。つながりが切れたら、直す人あての文つきの誤り（PC の電源・ネット）。"""
    hub: Hub
    session: _Session

    def _ask(self, kind: str, **fields: Any) -> dict[str, Any]:
        try:
            return self.hub.ask(self.session, kind, **fields)
        except BaseException as e:
            if self.hub.closing:   # サーバーを止めるので取りやめた：失敗ではない（次に動いたときにやり直す）
                raise p.Interrupted("サーバーを止めるので、控えを取りやめました") from None
            if isinstance(e, (ConnectionError, TimeoutError, asyncio.TimeoutError, ConnectionClosed)):
                raise RemoteError("控えを書く PC の部品とのつながりが切れました", ask=AWAY) from None
            raise

    def write(self, folder: list[str], file_name: str, source: Path, sha256: str) -> tuple[str, int]:
        # 送り終えてから、PC がディスクに書き終え、読み戻して確かめるのを待つ（遅いハードディスクでも 1 MiB に1秒）
        got = self._ask("backup_begin", wait=60 + source.stat().st_size / (1 << 20), source=source, folder=folder,
                        file_name=file_name, sha256=sha256)
        if not got.get("ok"):
            raise RemoteError(str(got.get("error") or "PC の部品が控えを書けませんでした"), ask=got.get("ask"))
        return str(got["sha256"]), int(got["size"])

    def prune(self, folders: dict[str, Any]) -> int:
        got = self._ask("prune", wait=300, folders=folders)
        if not got.get("ok"):
            raise RemoteError(str(got.get("error") or "PC の部品が古い控えを片付けられませんでした"), ask=got.get("ask"))
        return int(got.get("removed") or 0)

