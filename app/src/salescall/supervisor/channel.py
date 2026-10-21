"""つなぎ口（サーバーの側。DESIGN.md 5-5）。PC の部品からのつなぎを受け、`links.Links` の形で中央の見守り役へ見せる。持ち主 CL-2。

- やり取りの形は `salescall.link.protocol` の1か所。PC から外向きの1本（WebSocket）。暗号化して相手を確かめるのはサーバーの入口。
- **つないだとき**：PC が通行証と版を渡す。通行証から契約者と PC を決める（`agents.identify`。止めた・知らない通行証は断る＝PC は
  ログインし直す）。版が合わなければ断り、その契約者と管理者へ「更新してください」（`updates.pc_out_of_date`）。同じ PC の前の
  つなぎは切る（1つの PC は1本だけ）。
- **動いている合図**：データベースへ書き（`agents.beat`。名乗った回線は、その契約者の使っている回線だけが残る）、つないでいる回線・
  控え用のハードディスクと鍵があるか・緊急停止の印を覚える。緊急停止の印がある PC の回線は、中央の見守り役に見せない（かけない）。
- **同じ回線を2つの PC が名乗ったら**、合図の新しいほうを中央の見守り役に見せる。電話の部品（`AgentDriver`）は、作ったときの PC に
  決まる（通話の途中で、頼み・声が別の PC へ行かない。PC が替われば、中央の見守り役が回線の流れを止めて立ち上げ直す）。
- **頼む**（電話の部品＝`AgentDriver`・控え＝`RemoteWriter`）：中央の見守り役の流れから頼み、答えを待つ（待ち過ぎたら「分からない」＝
  かけない側）。つながりが切れたら、待っている頼みは誤りで返す。
- **PC の上の緊急停止**：PC から届いたら、その契約者の緊急停止を記録し（`core.emergency`）、記録の番号を PC へ返す。画面から解かれて
  いたら、PC へ伝える（PC は印を消す）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from salescall.core import agents, emergency
from salescall.core.access import Access
from salescall.link import protocol as p
from salescall.link.phone import CallNotPlaced, CallWithheld
from salescall.link.protocol import RemoteError
from salescall.supervisor import ACTOR, config, report, updates
from salescall.supervisor.links import Attached

HELLO_WAIT = 10   # つないでから、最初のやり取り（hello）を待つ長さ（秒）


@dataclass
class _Session:
    ws: ServerConnection
    who: agents.Who
    lines: list[UUID] = field(default_factory=list)
    drive: bool = False
    key: bool = False
    emergency: dict[str, Any] | None = None
    seen: float = 0.0
    pending: dict[UUID, asyncio.Future] = field(default_factory=dict)
    recording_emergency: bool = False
    order: asyncio.Lock = field(default_factory=asyncio.Lock)   # 合図と緊急停止は、届いた順に1つずつ（データベースを待つあいだも声は止めない）
    tasks: set[asyncio.Task] = field(default_factory=set)


class Hub:
    """つなぎ口（中央の見守り役と同じ処理の中で、別の流れの中で動く）。"""

    def __init__(self, app: Access, cfg: config.Config) -> None:
        self.app, self.cfg = app, cfg
        self.sessions: dict[UUID, _Session] = {}       # PC → つなぎ
        self.sinks: dict[UUID, tuple[UUID, Callable[[bytes], None]]] = {}   # 回線 → (PC, 届いた声の受け口＝通話を進める部品)
        self.events: dict[UUID, tuple[UUID, Callable[[dict[str, Any]], None]]] = {}   # 回線 → (PC, 電話の出来事の受け口)
        self.lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port: int | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    # ── 動かす・止める ──
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="salescall-channel", daemon=True)
        self._thread.start()
        self._ready.wait(30)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        host, port = self.cfg.listen or ("127.0.0.1", 0)
        self._stop = asyncio.Event()
        async with serve(self._handle, host, port, max_size=p.MAX_FRAME, compression=None) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in rest:   # 閉じるとき：残っている合図の処理を終わらせる（データベースの取引は、それぞれ終わるか戻される）
            t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)

    def close(self) -> None:
        if self.loop is not None and self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(10)

    async def _db(self, work: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, work)

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
            with self.lock:
                old = self.sessions.get(who.pc_id)
                self.sessions[who.pc_id] = session
            if old is not None:   # 同じ PC の前のつなぎ（1つの PC は1本だけ）
                await old.ws.close()
            await ws.send(p.dumps("welcome", tenant=who.tenant_id, pc=who.pc_id, lines=kept))
            await self._emergency_state(session)
            async for message in ws:
                await self._receive(session, message)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except p.BadMessage as e:
            report(self.app, None if session is None else session.who.tenant_id, "つなぎ口のやり取り", e)
        except Exception as e:  # noqa: BLE001 1本のつなぎの誤りで、つなぎ口を止めない
            report(self.app, None if session is None else session.who.tenant_id, "つなぎ口", e)
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
            await session.ws.send(p.dumps("refuse", why="この PC のログインが止められました。ログインし直してください",
                                          relogin=True, update=False))
            await session.ws.close()
            return None
        session.lines, session.seen = list(kept), time.monotonic()
        session.drive, session.key = bool(msg.get("drive")), bool(msg.get("key"))
        session.emergency = msg.get("emergency") if isinstance(msg.get("emergency"), dict) else None
        return session.lines

    async def _receive(self, session: _Session, message: str | bytes) -> None:
        if isinstance(message, bytes):
            kind, ident, payload = p.unframe(message)
            if kind == p.AUDIO and (sink := self._sink(self.sinks, session, ident)) is not None:
                sink(payload)
            return
        msg = p.loads(message, p.PC_TO_SERVER - {"hello"})
        kind = msg["t"]
        if kind in ("beat", "emergency"):   # データベースを待つ：別に動かす（届いた順に1つずつ）
            task = asyncio.get_running_loop().create_task(self._in_order(session, msg))
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
        elif kind in ("reply", "backup_done"):
            f = session.pending.pop(_uuid(msg.get("id")), None)
            if f is not None and not f.done():
                f.set_result(msg)
        elif kind == "phone_event":
            sink = self._sink(self.events, session, _uuid(msg.get("line")))
            if sink is not None and isinstance(msg.get("event"), dict):
                sink(msg["event"])

    @staticmethod
    def _sink(sinks: dict[UUID, tuple[UUID, Any]], session: _Session, line: UUID | None) -> Any:
        """届いた声・出来事の受け口（その回線の電話の部品を作った PC から届いたものだけ）。"""
        got = sinks.get(line) if line is not None and line in session.lines else None
        return got[1] if got is not None and got[0] == session.who.pc_id else None

    async def _in_order(self, session: _Session, msg: dict[str, Any]) -> None:
        try:
            async with session.order:
                if msg["t"] == "emergency":
                    await self._emergency(session, msg)
                elif await self._beat(session, msg) is not None:
                    await self._emergency_state(session)
        except ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001 1つの合図の誤りで、つなぎを止めない（次の合図でまた合わせる）
            report(self.app, session.who.tenant_id, "つなぎ口の合図", e)

    # ── PC の上の緊急停止 ──
    async def _emergency(self, session: _Session, msg: dict[str, Any]) -> None:
        who, reason = session.who, str(msg.get("reason") or "")
        if msg.get("action") == "release":
            await self._db(lambda: emergency.release(self.app, who.tenant_id, who.label, reason))
            session.emergency, session.recording_emergency = None, False
            await session.ws.send(p.dumps("emergency_state", event=None, open=False))
            return
        if session.emergency is None:
            session.emergency = {"reason": reason, "event": None}
        await self._emergency_state(session)

    async def _emergency_state(self, session: _Session) -> None:
        """PC の印の状態を合わせる：記録がまだなら記録して番号を返す。画面から解かれていれば、PC へ伝える（PC は印を消す）。"""
        flag, who = session.emergency, session.who
        if flag is None:
            return
        event = flag.get("event")
        if isinstance(event, int):
            if not await self._db(lambda: emergency.is_open(self.app, who.tenant_id, event)):
                session.emergency, session.recording_emergency = None, False
                await session.ws.send(p.dumps("emergency_state", event=event, open=False))
            return
        if session.recording_emergency:   # 同じつなぎで二重に記録しない（PC が番号を書くまでの合図）
            return
        session.recording_emergency = True
        reason = str(flag.get("reason") or "（理由なし）")
        event = await self._db(lambda: emergency.stop(self.app, who.tenant_id, who.label, reason))
        flag["event"] = event
        await session.ws.send(p.dumps("emergency_state", event=event, open=True))

    # ── 頼む（中央の見守り役の流れから呼ぶ） ──
    def _session_of(self, pc_id: UUID, line_id: UUID) -> _Session:
        """その PC の、いまのつなぎ（その回線をつないでいるときだけ）。"""
        with self.lock:
            got = self.sessions.get(pc_id)
        if got is None or line_id not in got.lines:
            raise ConnectionError("その回線をつないでいる、その PC の部品がありません")
        return got

    def ask(self, session: _Session, kind: str, *, wait: float = p.REPLY_WAIT, chunks: list[bytes] | None = None,
            **fields: Any) -> dict[str, Any]:
        """頼んで答えを待つ（中央の見守り役の流れから）。chunks＝続けて送るバイトのひと続き（控えの中身）。"""
        if self.loop is None:
            raise ConnectionError("つなぎ口が動いていません")
        ident = p.new_id()

        async def go() -> dict[str, Any]:
            f = asyncio.get_running_loop().create_future()
            session.pending[ident] = f
            try:
                await session.ws.send(p.dumps(kind, id=ident, **fields))
                for c in chunks or []:
                    await session.ws.send(p.frame(p.BACKUP, ident, c))
                if chunks is not None:
                    await session.ws.send(p.dumps("backup_end", id=ident))
                return await asyncio.wait_for(f, wait)
            finally:
                session.pending.pop(ident, None)
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(wait + 10)


    # ── links.Links ──
    def lines(self) -> dict[UUID, Attached]:
        with self.lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.seen)
        out: dict[UUID, Attached] = {}
        for s in sessions:   # 合図の新しいほうが残る
            if s.emergency is not None:   # PC の上で緊急停止が押されている：その PC の回線ではかけない
                continue
            for line_id in s.lines:
                out[line_id] = Attached(s.who.tenant_id, s.who.pc_id)
        return out

    def driver(self, line_id: UUID, pc_id: UUID) -> "AgentDriver":
        return AgentDriver(self, line_id, pc_id)

    def writer(self, tenant_id: UUID) -> "RemoteWriter | None":
        with self.lock:
            got = sorted((s for s in self.sessions.values() if s.who.tenant_id == tenant_id), key=lambda s: str(s.who.pc_id))
        ready = [s for s in got if s.drive and s.key] or got   # 書ける PC が無ければ、つないでいる PC に頼む（PC が「つないで」を返す）
        return RemoteWriter(self, ready[0]) if ready else None

    def whole_writer(self) -> "RemoteWriter | None":
        with self.lock:
            s = self.sessions.get(self.cfg.whole_backup_pc) if self.cfg.whole_backup_pc else None
        return RemoteWriter(self, s) if s is not None else None


@dataclass
class AgentDriver:
    """その PC の、その回線の電話の部品（PC の部品へ頼む形。dial_exit.LineDriver）。通話の声もここを通る。作ったときの PC だけに頼む。"""
    hub: Hub
    line_id: UUID
    pc_id: UUID

    def _session(self) -> _Session:
        return self.hub._session_of(self.pc_id, self.line_id)

    def in_call(self) -> bool:
        try:
            got = self.hub.ask(self._session(), "request", op="in_call", line=self.line_id)
        except Exception:  # noqa: BLE001 答えられない＝分からない：通話中とみなす（かけない側）
            return True
        return not got.get("ok") or bool(got.get("value"))

    def start_call(self, phone_e164: str) -> None:
        got = self.hub.ask(self._session(), "request", op="start_call", line=self.line_id, phone=phone_e164)
        if got.get("ok"):
            return
        if got.get("error") == "withheld":
            raise CallWithheld("PC の上で緊急停止が押されています")
        if got.get("error") == "not_placed":
            raise CallNotPlaced("電話の部品がかけられませんでした")
        raise RuntimeError("電話の部品が、かけたか分からない誤りを返しました")

    def hang_up(self) -> None:
        got = self.hub.ask(self._session(), "request", op="hang_up", line=self.line_id)
        if not got.get("ok"):
            raise RuntimeError("電話の部品が、切れたか分からない誤りを返しました")

    def send_audio(self, audio: bytes) -> None:
        """AI の声・録音を、通話へ流す（PC の部品が電話の部品の play へ渡す。待たずに返す）。"""
        session = self._session()
        if self.hub.loop is not None:
            asyncio.run_coroutine_threadsafe(session.ws.send(p.frame(p.AUDIO, self.line_id, audio)), self.hub.loop)

    def on_audio(self, sink: Callable[[bytes], None]) -> None:
        """お客様の声の受け口を決める（通話を進める部品＝CL-1。つなぎ口の流れから呼ばれるので、待たずに返すこと）。"""
        self.hub.sinks[self.line_id] = (self.pc_id, sink)

    def on_event(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """電話の出来事（呼び出し・応答・切れた など。中身は電話接続の担当が決める）の受け口を決める。"""
        self.hub.events[self.line_id] = (self.pc_id, sink)


@dataclass
class RemoteWriter:
    """控えを、その PC の部品に書かせる（core.backup.Writer）。"""
    hub: Hub
    session: _Session

    def write(self, folder: list[str], file_name: str, data: bytes, sha256: str) -> tuple[str, int]:
        chunks = [data[i:i + p.CHUNK] for i in range(0, len(data), p.CHUNK)] or [b""]
        got = self.hub.ask(self.session, "backup_begin", wait=60 + 5 * len(chunks), chunks=chunks, folder=folder,
                           file_name=file_name, size=len(data), sha256=sha256)
        if not got.get("ok"):
            raise RemoteError(str(got.get("error") or "PC の部品が控えを書けませんでした"), ask=got.get("ask"))
        return str(got["sha256"]), int(got["size"])

    def prune(self, folders: dict[str, Any]) -> int:
        got = self.hub.ask(self.session, "prune", wait=300, folders=folders)
        if not got.get("ok"):
            raise RemoteError(str(got.get("error") or "PC の部品が古い控えを片付けられませんでした"), ask=got.get("ask"))
        return int(got.get("removed") or 0)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None
