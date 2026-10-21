"""PC の部品の本体（DESIGN.md 5-5）。持ち主 CL-2。

1. **1台の PC に1つだけ**（PC の中の印のファイル。同じ PC で2つ動くと、同じスマホで二重にかけうる）。
2. **サーバーへつなぐ**（外向きの1本。通行証と版を渡す）。断られたら：ログインし直す（通行証が止められた）・更新を待つ（版が違う）。
   つながらない・切れたら、間をあけてつなぎ直す（続けるほど延ばす。上限1分）。
3. **動いている合図**（`protocol.BEAT_EVERY` 秒ごと）：つないでいる回線・控え用のハードディスクと鍵があるか・緊急停止の印。
4. **頼まれたことをする**：電話の部品（`salescall.link.phone.Phone`。電話接続の担当）の「通話中か・かける・切る」。**緊急停止の印が
   あれば、かけない**（「かけて」を断る。通話中かと聞かれたら「通話中」と答える）。控えを書く・片付ける（`backup.LocalWriter`）。
5. **通話の声と電話の出来事**：電話の部品が受けたお客様の声と、電話の出来事（呼び出し・応答・切れた など）をサーバーへ送り、
   サーバーから届いた声を電話の部品へ渡す（中身は見ない）。電話の部品は、自分の流れ（音声の機器の流れ など）から送ってよい。
6. **つながりが切れたら**：通話の途中の回線は、電話の部品が、おわびの録音を流して切る（お客様を無言で放置しない）。切れたことには
   数秒で気づく（`PING_EVERY`。通信の終わりの合図が来ない切れ方＝Wi-Fi が落ちた など、でも）。**切れたあとに「かける」が
   終わったら、すぐおわびして切る**（つなぎの番号で見分ける。サーバーは、その試行を結果不明にしている）。
7. 1つのやり取り・電話の部品の誤りで、つなぎを切らない・PC の部品を終わらせない（記録して続ける）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from salescall.agent import backup, emergency
from salescall.agent.config import Config
from salescall.agent.pc_login import PcPass
from salescall.link import protocol as p
from salescall.link import sealing
from salescall.link.phone import CallNotPlaced, Phone

RECONNECT_FIRST, RECONNECT_MAX = 1.0, 60.0
PING_EVERY = 3.0   # つながりを確かめる間隔と、答えを待つ長さ（秒）。通話の途中で切れたら、これだけでおわびして切る


class NeedLogin(Exception):
    """サーバーが通行証を断った（止められた・分からない）。ログインし直す。"""


class NeedUpdate(Exception):
    """サーバーと版が違う。更新を待つ。"""


def load_phone(spec: str, line_id: UUID) -> Phone:
    """設定の `モジュール:名前` から電話の部品を作る（回線の ID を渡す）。"""
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"部品は「モジュール:名前」で書きます（{spec!r}）")
    return getattr(importlib.import_module(module), name)(line_id)


@dataclass
class Agent:
    cfg: Config
    pc_pass: PcPass
    phones: dict[UUID, Phone] = field(default_factory=dict)
    lines: list[UUID] = field(default_factory=list)       # サーバーが確かめ直した回線
    ws: ClientConnection | None = None
    loop: asyncio.AbstractEventLoop | None = None
    generation: int = 0                                    # つなぎの番号（つなぎ直すたびに増える）
    _incoming: dict[UUID, Any] = field(default_factory=dict)   # 控えの番号 → 書いている途中（backup.Incoming）か、断った答え
    _tasks: set[asyncio.Task] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.cfg.phone and not self.phones:
            self.phones = {line: load_phone(self.cfg.phone, line) for line in self.cfg.lines}
        self.writer = backup.LocalWriter(backup.Drive(self.cfg.backup_drive), self.cfg.environ) if self.cfg.backup_drive else None
        try:   # 鍵があるか（設定は動いているあいだ変わらないので、1回だけ確かめる。鍵の計算は重く、合図のたびにすると声が止まる）
            self.key_ready = bool(sealing.load_key(self.cfg.environ))
        except sealing.KeyMissing:
            self.key_ready = False

    # ── 合図の中身 ──
    def _state(self) -> dict[str, Any]:
        drive = self.cfg.backup_drive is not None and backup.Drive(self.cfg.backup_drive).attached()
        flag = emergency.engaged()
        return {"lines": [str(x) for x in self.phones], "drive": drive, "key": self.key_ready,
                "emergency": None if flag is None else {"reason": flag.get("reason"), "at": flag.get("at"),
                                                        "event": flag.get("event")}}

    # ── つなぐ ──
    async def run(self, stop: asyncio.Event) -> None:
        """止める印が立つまで、つなぎ直しながら動く（ログインし直す・更新が要るときは NeedLogin・NeedUpdate を上へ）。"""
        wait = RECONNECT_FIRST
        while not stop.is_set():
            try:
                await self.session(stop)
                wait = RECONNECT_FIRST
            except (NeedLogin, NeedUpdate):
                raise
            except (OSError, ConnectionClosed, asyncio.TimeoutError, p.BadMessage):
                pass
            finally:
                self._lost()
            try:
                await asyncio.wait_for(stop.wait(), wait)
            except asyncio.TimeoutError:
                pass
            wait = min(wait * 2, RECONNECT_MAX)

    async def session(self, stop: asyncio.Event) -> None:
        async with connect(self.cfg.hub_url, max_size=p.MAX_FRAME, open_timeout=20, compression=None,
                           ping_interval=PING_EVERY, ping_timeout=PING_EVERY) as ws:
            self.ws, self.loop = ws, asyncio.get_running_loop()
            self.generation += 1
            await ws.send(p.dumps("hello", v=p.VERSION, token=self.pc_pass.token, updater=self.cfg.updater is not None,
                                  **self._state()))
            first = p.loads(await asyncio.wait_for(ws.recv(), 30), frozenset({"welcome", "refuse"}))
            if first["t"] == "refuse":
                if first.get("relogin"):
                    raise NeedLogin(str(first.get("why")))
                if first.get("update"):
                    raise NeedUpdate(str(first.get("why")))
                return
            self.lines = p.uuids(first.get("lines"))
            for line, phone in self.phones.items():
                phone.on_audio(lambda audio, line=line: self._send(line, p.frame(p.AUDIO, line, audio)))
                phone.on_event(lambda event, line=line: self._send(line, p.dumps("phone_event", line=line, event=event)))
            beats = asyncio.create_task(self._beats(stop))
            try:
                async for message in ws:
                    try:
                        await self._receive(message)
                    except (NeedLogin, NeedUpdate, ConnectionClosed):
                        raise
                    except Exception:  # noqa: BLE001 1つのやり取り・電話の部品の誤りで、つなぎを切らない（その頼みには答えない＝サーバーは「分からない」）
                        pass
                    if stop.is_set():
                        break
            finally:
                beats.cancel()

    async def _beats(self, stop: asyncio.Event) -> None:
        """合図は BEAT_EVERY 秒ごと。緊急停止の印が変わったら、待たずにすぐ送る（押したら、すぐサーバーに記録させる）。"""
        last, seen = time.monotonic(), json.dumps(emergency.engaged(), sort_keys=True, default=str)
        while not stop.is_set():
            await asyncio.sleep(1)
            now, flag = time.monotonic(), json.dumps(emergency.engaged(), sort_keys=True, default=str)
            if now - last >= p.BEAT_EVERY or flag != seen:
                await self.beat()
                last, seen = now, flag
        if self.ws is not None:   # 止める印：つながりを閉じて、受け取りの繰り返しを終える
            await self.ws.close()

    async def beat(self) -> None:
        """動いている合図（緊急停止の印も載せる＝サーバーが、まだ記録していなければ記録する）。解いてほしいと書かれていれば届ける。"""
        if self.ws is None:
            return
        await self.ws.send(p.dumps("beat", **self._state()))
        flag = emergency.engaged()
        if flag is not None and flag.get("release"):
            await self.ws.send(p.dumps("emergency", action="release", reason=flag["release"], event=flag.get("event")))

    def _lost(self) -> None:
        """つながりが切れた：通話の途中の回線は、おわびの録音を流して切る（お客様を無言で放置しない）。途中の控えの中身は捨てる
        （サーバーは、書けなかったとして記録し、あとでやり直す）。"""
        self.ws = None
        self.generation += 1
        for entry in self._incoming.values():   # 書きかけの控えを消す（控えと取り違えない）
            if isinstance(entry, backup.Incoming):
                entry.abort()
        self._incoming.clear()
        for phone in self.phones.values():
            try:
                busy = phone.in_call()
            except Exception:  # noqa: BLE001 答えられない＝分からない：通話中とみなす（おわびして切る側）
                busy = True
            if busy:
                _apologize(phone)

    def _send(self, line: UUID, message: str | bytes) -> None:
        """電話の部品から（どの流れからでも）サーバーへ送る。つないでいない・サーバーが認めていない回線のものは送らない。"""
        ws, loop = self.ws, self.loop
        if ws is not None and loop is not None and line in self.lines:
            asyncio.run_coroutine_threadsafe(ws.send(message), loop)

    # ── 頼まれたこと ──
    def _spawn(self, work: Any) -> None:
        """頼まれたこと（かける・控えを書く など）は、別に動かす（そのあいだも、ほかの回線の声を止めない）。"""
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _receive(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            kind, ident, payload = p.unframe(message)
            if kind == p.AUDIO and ident in self.lines and ident in self.phones:
                try:
                    self.phones[ident].play(payload)
                except Exception:  # noqa: BLE001 電話の部品の誤りで、ほかの回線の声を止めない
                    pass
            elif kind == p.BACKUP and isinstance(entry := self._incoming.get(ident), backup.Incoming):
                out = await self._write(lambda: backup.as_remote(lambda: entry.feed(payload)))   # 届いた順に、少しずつ暗号化して書く
                if not out.get("ok"):
                    entry.abort()
                    self._incoming[ident] = out
            return
        msg = p.loads(message, p.SERVER_TO_PC - {"welcome"})
        kind = msg["t"]
        if kind == "request":
            self._spawn(self._reply(msg))
        elif kind == "backup_begin":
            ident = UUID(str(msg["id"]))
            out = await self._write(lambda: self.writer.begin(list(msg.get("folder") or []), str(msg.get("file_name")),
                                                              str(msg.get("sha256"))))
            self._incoming[ident] = out["value"] if out.get("ok") else out
        elif kind == "backup_end":
            self._spawn(self._backup_end(UUID(str(msg["id"]))))
        elif kind == "prune":
            self._spawn(self._prune(msg))
        elif kind == "emergency_state":
            event = msg.get("event") if isinstance(msg.get("event"), int) else None
            if msg.get("open") is False:
                emergency.clear(event)            # 印の番号が同じときだけ消す（押し直した印は消さない）
            elif event is not None:
                emergency.recorded(event, msg.get("at"))
        elif kind == "refuse":
            if msg.get("relogin"):
                raise NeedLogin(str(msg.get("why")))
            raise NeedUpdate(str(msg.get("why")))

    async def _reply(self, msg: dict[str, Any]) -> None:
        ident, op = msg.get("id"), msg.get("op")
        line = UUID(str(msg.get("line")))
        phone = self.phones.get(line) if line in self.lines else None
        loop = asyncio.get_running_loop()
        out: dict[str, Any]
        if phone is None or op not in p.OPS:
            out = {"ok": False, "error": "failed"}
        elif op == "in_call":
            try:   # 緊急停止の印があれば「通話中」（かけない側）
                busy = emergency.engaged() is not None or await loop.run_in_executor(None, phone.in_call)
                out = {"ok": True, "value": bool(busy)}
            except Exception:  # noqa: BLE001 答えられない＝分からない：通話中（かけない側）
                out = {"ok": True, "value": True}
        elif op == "start_call":
            if emergency.engaged() is not None:   # かける直前にもう一度見る（印があれば、かけない）
                out = {"ok": False, "error": "withheld"}
            else:
                generation = self.generation
                try:
                    await loop.run_in_executor(None, phone.start_call, str(msg.get("phone")))
                    out = {"ok": True}
                except CallNotPlaced:
                    out = {"ok": False, "error": "not_placed"}
                except Exception:  # noqa: BLE001 かけたか分からない
                    out = {"ok": False, "error": "failed"}
                if self.generation != generation and out.get("error") != "not_placed":
                    # かけているあいだにつながりが切れた：サーバーは結果不明にしている。AI のいない電話にしない
                    await loop.run_in_executor(None, _apologize, phone)
                    return
        else:
            work = phone.hang_up if op == "hang_up" else phone.play_apology_and_hang_up
            try:
                await loop.run_in_executor(None, work)
                out = {"ok": True}
            except Exception:  # noqa: BLE001
                out = {"ok": False, "error": "failed"}
        if self.ws is not None:
            await self.ws.send(p.dumps("reply", id=ident, **out))

    async def _backup_end(self, ident: UUID) -> None:
        entry = self._incoming.pop(ident, None)
        if entry is None:
            return
        if isinstance(entry, backup.Incoming):
            out = await self._write(lambda: backup.as_remote(entry.finish))
            if not out.get("ok"):
                entry.abort()
        else:
            out = entry   # 始めるとき・途中で断った答え
        if self.ws is not None:
            if out.get("ok"):
                out = {"ok": True, "sha256": out["value"][0], "size": out["value"][1]}
            await self.ws.send(p.dumps("backup_done", id=ident, **out))

    async def _prune(self, msg: dict[str, Any]) -> None:
        out = await self._write(lambda: self.writer.prune(msg.get("folders") or {}))
        if self.ws is not None:
            if out.get("ok"):
                out = {"ok": True, "removed": out["value"]}
            await self.ws.send(p.dumps("backup_done", id=msg.get("id"), **out))

    async def _write(self, work: Any) -> dict[str, Any]:
        if self.writer is None:
            return {"ok": False, "error": "控え用のハードディスクの設定がありません",
                    "ask": "この PC に、控え用のハードディスクの設定（SALESCALL_BACKUP_DRIVE）がありません（手順：docs/PC_SETUP.md）"}
        try:
            return {"ok": True, "value": await asyncio.get_running_loop().run_in_executor(None, work)}
        except p.RemoteError as e:
            return {"ok": False, "error": str(e), "ask": e.ask}
        except Exception as e:  # noqa: BLE001 中身は書かない（種類の名前だけ）
            return {"ok": False, "error": f"思いがけない誤り（{type(e).__name__}）"}


def _apologize(phone: Phone) -> None:
    try:
        phone.play_apology_and_hang_up()
    except Exception:  # noqa: BLE001 電話の部品の誤りで、PC の部品を止めない
        pass
