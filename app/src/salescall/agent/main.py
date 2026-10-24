"""PC の部品の本体（DESIGN.md 5-5）。持ち主 CL-2。

1. **1台の PC に1つだけ**（PC の中の印のファイル。同じ PC で2つ動くと、同じスマホで二重にかけうる）。
2. **サーバーへつなぐ**（外向きの1本。通行証と版を渡す）。断られたら：ログインし直す（通行証が止められた）・更新を待つ（版が違う）。
   つながらない・切れたら、間をあけてつなぎ直す（続けるほど延ばす。上限1分）。
3. **動いている合図**（`protocol.BEAT_EVERY` 秒ごと）：つないでいる回線・控え用のハードディスクと鍵があるか・緊急停止の印。
4. **頼まれたことをする**：電話の部品（`salescall.link.phone.Phone`。電話接続の担当）の「通話中か・かける・切る」。**緊急停止の印が
   あれば、かけない**（「かけて」を断る。通話中かと聞かれたら「通話中」と答える）。**1つの回線への頼みは、届いた順に1つずつ**
   （「かけて」の途中に届いた「おわびして切って」を、かける前にしない）。**答えの期限（`protocol.ANSWER_BY`）を過ぎて「かけた」が
   終わったら、自分でおわびして切り、「分からない」と答える**（サーバーはもう待っていない＝AI のいない電話にしない）。
   控えを書く・片付ける（`backup.LocalWriter`）：受け取った中身は、控えごとの順番待ちに置くだけにし、書くのは別に動かす（受け取りの
   繰り返しを、ディスクと暗号化で止めない＝声を止めない）。書けた数を返す（`backup_ack`。サーバーは、それを見て先へ送る）。
5. **通話の声と電話の出来事**：電話の部品が受けたお客様の声と、電話の出来事（呼び出し・応答・切れた など）をサーバーへ送り、
   サーバーから届いた声を電話の部品へ渡す（中身は見ない）。電話の部品は、自分の流れ（音声の機器の流れ など）から送ってよい。
6. **つながりが切れたら**：通話の途中の回線は、電話の部品が、おわびの録音を流して切る（お客様を無言で放置しない）。切れたことには
   数秒で気づく（`protocol.PING_EVERY`。通信の終わりの合図が来ない切れ方＝Wi-Fi が落ちた など、でも）。**切れたあとに「かける」が
   終わったら、すぐおわびして切る**（つなぎの番号で見分ける。サーバーは、その試行を結果不明にしている）。
7. 1つのやり取り・電話の部品の誤りで、つなぎを切らない・PC の部品を終わらせない（記録して続ける）。
8. **サーバーに `UNREACHABLE_NOTICE` 秒続けてつながらなければ、この PC の画面に知らせる**（`notice.show`。サーバーそのものが止まると、
   サーバーからの知らせも出せないため。2026-10-22 ユーザー承認）。1回の途切れに1回だけ。つながり直したら、それも知らせる。
   **「つながった」とみなすのは、つなぎが `STABLE_AFTER` 秒続いたときだけ**（判断は `_watch_reach` の1か所）。それより短いつなぎ
   （立ち上がってはすぐ落ちるサーバー）では、途切れの数えを始め直さない（⛔ 2026-10-22 の見直し15：前は、つながるたびに数え直し、
   30秒ごとに落ちるサーバーでは、いつまでも知らせが出なかった）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from salescall.agent import backup, emergency, notice
from salescall.agent.config import Config
from salescall.link import protocol as p
from salescall.link import sealing
from salescall.link.basics import PcPass, backoff, load
from salescall.link.phone import CallNotPlaced, Phone

RECONNECT_FIRST, RECONNECT_MAX = 1.0, 60.0
# PC の部品が終わるとき、途中の頼み（かけている途中の電話）が済むのを待つ長さ（秒）。かけ終わったあとの見張り（つながりが切れたら
# おわびして切る）まで走らせる。電話の部品の「かける」は答えの期限で終わる（ANSWER_BY）。おわびの録音を流す時間をあける
FINISH_WAIT = p.ANSWER_BY + 30
# つなぎがこれだけ続いたら「つながった」とみなす（秒）：途切れの数えを終え（画面の知らせ）、すぐつなぎ直す（短く終わり続けるほど、
# 間を延ばす）。判断は `_stable` の1か所
STABLE_AFTER = 60.0
UNREACHABLE_NOTICE = 600.0   # サーバーに続けてつながらないと、この PC の画面に知らせるまでの長さ（秒。2026-10-22 ユーザー承認：10分）
UNREACHABLE_TEXT = ("サーバーにつながりません（つながっても、すぐ切れます。10分以上）。そのあいだ、この PC からの電話は止まって"
                    "います。つながれば自動で再開します。インターネットにつながっているかを確かめてください。")
BACK_TEXT = "サーバーにつながりました。電話を再開します。"


class NeedLogin(Exception):
    """サーバーが通行証を断った（止められた・分からない）。ログインし直す。"""


class NeedUpdate(Exception):
    """サーバーと版が違う。更新を待つ。"""


@dataclass
class Agent:
    cfg: Config
    pc_pass: PcPass
    phones: dict[UUID, Phone] = field(default_factory=dict)
    lines: list[UUID] = field(default_factory=list)       # サーバーが確かめ直した回線
    ws: ClientConnection | None = None
    loop: asyncio.AbstractEventLoop | None = None
    generation: int = 0                                    # つなぎの番号（つなぎ直すたびに増える）
    _incoming: dict[UUID, "_Receiving"] = field(default_factory=dict)   # 控えの番号 → 受け取っている途中の控え
    _tasks: set[asyncio.Task] = field(default_factory=set)
    _requests: set[asyncio.Future] = field(default_factory=set)          # 途中の頼み（かける・切る・おわび など）
    _line_order: dict[UUID, asyncio.Lock] = field(default_factory=dict)   # 回線ごとの頼みの順番
    connected_at: float | None = None                      # いまのつなぎがサーバーに迎えられた時刻（つないでいなければ None）
    # 途切れの始まり（動き出した時・安定したつなぎが切れた時）。安定したつなぎ（`_stable`）のあいだは None
    offline_since: float | None = field(default_factory=time.monotonic)
    told: bool = False                                     # この途切れを、画面に知らせたか
    tell: Callable[[str], None] = notice.show             # 画面に知らせる（試験で差し替える）

    def __post_init__(self) -> None:
        if self.cfg.phone and not self.phones:
            self.phones = {line: load(self.cfg.phone, line) for line in self.cfg.lines}   # 電話の部品（回線の ID を渡す）
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

    async def _state_off_loop(self) -> dict[str, Any]:
        """合図の中身は別の流れで作る（眠っているハードディスクが起きるのを待つあいだ、声を止めない）。"""
        return await asyncio.get_running_loop().run_in_executor(None, self._state)

    # ── つなぐ ──
    async def run(self, stop: asyncio.Event) -> None:
        """止める印が立つまで、つなぎ直しながら動く（ログインし直す・更新が要るときは NeedLogin・NeedUpdate を上へ）。
        終わるときは（どの終わり方でも）、途中の頼みが済むまで待つ（`_finish_requests`）。"""
        try:
            await self._run(stop)
        finally:
            await self._finish_requests()

    async def _finish_requests(self) -> None:
        """途中の頼みが済むのを待つ（上限 FINISH_WAIT）。⛔ 2026-10-22 の見直し：通行証が止められて終わるとき、かけている途中の頼みを
        取り消していた ⇒ 電話の部品は別の流れでかけ終え、かけ終わったあとの見張り（切れていたら、おわびして切る）が動かず、AI も
        おわびも無い電話が残った。つながりは切れている（つなぎの番号が進んでいる）ので、かけ終えた頼みは自分でおわびして切る。"""
        waiting = [t for t in self._requests if not t.done()]
        if waiting:
            await asyncio.wait(waiting, timeout=FINISH_WAIT)

    async def _run(self, stop: asyncio.Event) -> None:
        fails = 0   # 続けて短く終わったつなぎの数（すぐ切られるサーバーへ、1秒ごとにつなぎ直し続けない）
        while not stop.is_set():
            stable = False
            try:
                await self.session(stop)
            except (NeedLogin, NeedUpdate):
                raise
            except (OSError, ConnectionClosed, asyncio.TimeoutError, p.BadMessage):
                pass
            finally:
                stable = self._lost()
            fails = 0 if stable else fails + 1
            try:
                await asyncio.wait_for(stop.wait(), backoff(fails, RECONNECT_FIRST, RECONNECT_MAX))
            except asyncio.TimeoutError:
                pass

    def _stable(self, now: float) -> bool:
        """いまのつなぎが「つながった」と言えるか（STABLE_AFTER 秒続いている。判断はこの1か所）。"""
        return self.connected_at is not None and now - self.connected_at >= STABLE_AFTER

    def _watch_reach(self, now: float | None = None) -> None:
        """つながりの見張り（画面の知らせの判断はこの1か所。つないでいるあいだは毎秒、つなぎ直すたびにも呼ぶ）：
        - 安定したつなぎ：途切れは終わり。知らせていたら、つながり直したことを知らせる。
        - そうでない（つないでいない・つないでも STABLE_AFTER 秒続いていない）：途切れの始まりから UNREACHABLE_NOTICE を過ぎたら、
          この途切れについて1回だけ知らせる。"""
        now = time.monotonic() if now is None else now
        if self._stable(now):
            self.offline_since = None
            if self.told:
                self.told = False
                self.tell(BACK_TEXT)
        elif self.offline_since is not None and not self.told and now - self.offline_since >= UNREACHABLE_NOTICE:
            self.told = True
            self.tell(UNREACHABLE_TEXT)

    def _welcomed(self, now: float | None = None) -> None:
        """サーバーに迎えられた：つなぎの始まり（「つながった」とみなすのは、続いてから＝`_stable`）。"""
        self.connected_at = time.monotonic() if now is None else now

    async def session(self, stop: asyncio.Event) -> None:
        async with connect(self.cfg.hub_url, max_size=p.MAX_FRAME, open_timeout=20, compression=None,
                           ping_interval=p.PING_EVERY, ping_timeout=p.PING_EVERY) as ws:
            self.ws, self.loop = ws, asyncio.get_running_loop()
            self.generation += 1
            await ws.send(p.dumps("hello", v=p.VERSION, token=self.pc_pass.token, updater=self.cfg.updater is not None,
                                  **await self._state_off_loop()))
            first = p.loads(await asyncio.wait_for(ws.recv(), 30), frozenset({"welcome", "refuse"}))
            if first["t"] == "refuse":
                if first.get("relogin"):
                    raise NeedLogin(str(first.get("why")))
                if first.get("update"):
                    raise NeedUpdate(str(first.get("why")))
                return
            self._welcomed()
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
            self._watch_reach(now)
            if now - last >= p.BEAT_EVERY or flag != seen:
                await self.beat()
                last, seen = now, flag
        if self.ws is not None:   # 止める印：つながりを閉じて、受け取りの繰り返しを終える
            await self.ws.close()

    async def beat(self) -> None:
        """動いている合図（緊急停止の印も載せる＝サーバーが、まだ記録していなければ記録する）。解いてほしいと書かれていれば届ける。"""
        if self.ws is None:
            return
        state = await self._state_off_loop()
        if self.ws is None:
            return
        await self.ws.send(p.dumps("beat", **state))
        flag = emergency.engaged()
        if flag is not None and flag.get("release"):
            await self.ws.send(p.dumps("emergency", action="release", reason=flag["release"], event=flag.get("event")))

    def _lost(self, now: float | None = None) -> bool:
        """つながりが切れた（つないだが迎えられなかった・つなげなかった、も）。切れる前のつなぎが安定していたかを返す。
        通話の途中の回線は、おわびの録音を流して切る（お客様を無言で放置しない。**ほかの後始末より先に**）。
        回線ごとに別の流れで同時に流す（⛔ 2026-10-22 の見直し：前は流れの上で1本ずつ待ち、3本目のお客様は前の2本のおわびが
        終わるまで無言だった。合図・つなぎ直しも止まっていた）。PC の部品が終わるときも、流し終えるまで待つ（`_finish_requests`）。
        途中の控えは捨てる（書きかけを消す＝控えと取り違えない。サーバーは、書けなかったとして記録し、あとでやり直す）。"""
        now = time.monotonic() if now is None else now
        self.ws = None
        self.generation += 1
        stable = self._stable(now)
        self._watch_reach(now)   # 切れる前に安定していたなら、途切れはいったん終わる（知らせていれば、つながり直したことも）
        self.connected_at = None
        if self.offline_since is None:   # 安定したつなぎが切れた：ここから、途切れを数える（短いつなぎでは数え直さない）
            self.offline_since = now
        self._watch_reach(now)
        loop = asyncio.get_running_loop()
        for phone in self.phones.values():
            done = loop.run_in_executor(None, _apologize_if_busy, phone)
            self._requests.add(done)
            done.add_done_callback(self._requests.discard)
        for receiving in self._incoming.values():
            receiving.drop()
        self._incoming.clear()
        return stable

    def _send(self, line: UUID, message: str | bytes) -> None:
        """電話の部品から（どの流れからでも）サーバーへ送る。つないでいない・サーバーが認めていない回線のものは送らない。"""
        ws, loop = self.ws, self.loop
        if ws is not None and loop is not None and line in self.lines:
            asyncio.run_coroutine_threadsafe(ws.send(message), loop)

    # ── 頼まれたこと ──
    def _spawn(self, work: Any, *, request: bool = False) -> None:
        """頼まれたこと（かける・控えを書く など）は、別に動かす（そのあいだも、ほかの回線の声を止めない）。request＝回線への頼み
        （PC の部品が終わるときも、済むまで待つ）。"""
        task = asyncio.get_running_loop().create_task(work)
        kept = self._requests if request else self._tasks
        kept.add(task)
        task.add_done_callback(kept.discard)

    async def _receive(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            kind, ident, payload = p.unframe(message)
            if kind == p.AUDIO and ident in self.lines and ident in self.phones:
                try:
                    self.phones[ident].play(payload)
                except Exception:  # noqa: BLE001 電話の部品の誤りで、ほかの回線の声を止めない
                    pass
            elif kind == p.BACKUP and (receiving := self._incoming.get(ident)) is not None:
                receiving.put(payload)   # 順番待ちに置くだけ（書くのは別に動かす）
            return
        msg = p.loads(message, p.SERVER_TO_PC - {"welcome"})
        kind = msg["t"]
        if kind == "request":
            self._spawn(self._reply(msg), request=True)
        elif kind == "backup_begin":
            if (ident := p.uuid_of(msg.get("id"))) is not None and ident not in self._incoming:
                receiving = self._incoming[ident] = _Receiving()
                self._spawn(self._take(ident, receiving, msg))
        elif kind == "backup_end":
            if (receiving := self._incoming.get(p.uuid_of(msg.get("id")))) is not None:
                receiving.put(None)
        elif kind == "prune":
            self._spawn(self._prune(msg))
        elif kind == "emergency_state":
            event = p.event_of(msg.get("event"))
            if msg.get("open") is False:
                emergency.clear(event)            # 印の番号が同じときだけ消す（押し直した印は消さない）
            elif event is not None:
                emergency.recorded(event, msg.get("at"))
        elif kind == "refuse":
            if msg.get("relogin"):
                raise NeedLogin(str(msg.get("why")))
            raise NeedUpdate(str(msg.get("why")))

    async def _reply(self, msg: dict[str, Any]) -> None:
        """1つの頼みに答える。同じ回線への頼みは、届いた順に1つずつ。"""
        got = time.monotonic()
        line = p.uuid_of(msg.get("line"))
        phone = self.phones.get(line) if line is not None and line in self.lines else None
        if phone is None or msg.get("op") not in p.OPS:
            await self._send_now(p.dumps("reply", id=msg.get("id"), ok=False, error="failed"))
            return
        async with self._line_order.setdefault(line, asyncio.Lock()):
            out = await self._do(phone, msg, got)
        if out is not None:
            await self._send_now(p.dumps("reply", id=msg.get("id"), **out))

    async def _do(self, phone: Phone, msg: dict[str, Any], got: float) -> dict[str, Any] | None:
        """頼まれたことをする（got＝頼みが届いた時刻）。None＝答えない（かけているあいだにつながりが切れ、自分でおわびして切った）。"""
        op, loop = msg["op"], asyncio.get_running_loop()
        if op == "in_call":   # 緊急停止の印があれば「通話中」（かけない側）
            return {"ok": True, "value": emergency.engaged() is not None or await loop.run_in_executor(None, _busy, phone)}
        if op == "start_call":
            if emergency.engaged() is not None:   # かける直前にもう一度見る（印があれば、かけない）
                return {"ok": False, "error": "withheld"}
            if time.monotonic() - got > p.ANSWER_BY:   # 前の頼みを待つうちに、答えの期限を過ぎた：かけようとしない（番号も回線も
                return {"ok": False, "error": "withheld"}   # 試していない＝どの数にも入れない）
            generation = self.generation
            try:
                voice = msg.get("voice") if isinstance(msg.get("voice"), str) else None   # この通話の声の型（おわびの録音も、この声）
                await loop.run_in_executor(None, lambda: phone.start_call(str(msg.get("phone")), voice_type=voice))
                out: dict[str, Any] = {"ok": True}
            except CallNotPlaced:    # かけようとして、かけられなかった（だれのせいかは、サーバーが同じ回線の前後の電話で決める）
                return {"ok": False, "error": "not_placed"}
            except Exception:  # noqa: BLE001 かけたか分からない
                out = {"ok": False, "error": "failed"}
            if self.generation != generation or time.monotonic() - got > p.ANSWER_BY:
                # かけているあいだに、つながりが切れた・答えの期限を過ぎた：サーバーは結果不明にしている（もう待っていない）。
                # AI のいない電話にしない
                await loop.run_in_executor(None, _apologize, phone)
                return None if self.generation != generation else {"ok": False, "error": "failed"}
            return out
        work = phone.hang_up if op == "hang_up" else phone.play_apology_and_hang_up
        try:
            await loop.run_in_executor(None, work)
            return {"ok": True}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "failed"}

    async def _send_now(self, message: str) -> None:
        """いまのつなぎへ送る（つないでいない・送るあいだに切れたら、送らない＝サーバーは待つのをやめて「分からない」にする）。"""
        if self.ws is not None:
            try:
                await self.ws.send(message)
            except ConnectionClosed:
                pass

    # ── 控え ──
    async def _take(self, ident: UUID, receiving: "_Receiving", msg: dict[str, Any]) -> None:
        """1つの控えを、届いた順に書く（受け取りの繰り返しとは別に動く）。書けた数を返し（backup_ack）、書けなければ、終わりを待たずに
        すぐ答える（サーバーは送るのをやめる）。"""
        incoming = None
        try:
            out = await self._write(lambda: self.writer.begin(list(msg.get("folder") or []), str(msg.get("file_name")),
                                                              str(msg.get("sha256"))))
            incoming = out.pop("value", None)
            written = 0
            while out["ok"]:
                try:   # 続きが届かない：サーバーが送るのをやめた（書けた印を待つ長さは、両方とも PROGRESS_WAIT）。書きかけを開いたまま
                    # 待ち続けない（⛔ 2026-10-22 の見直し：次につなぎ直すまで、書きかけのファイルを開いたままにした）
                    piece = await asyncio.wait_for(receiving.queue.get(), p.PROGRESS_WAIT + p.PING_EVERY)
                except asyncio.TimeoutError:
                    out = {"ok": False, "error": "サーバーから控えの続きが届きません"}
                    break
                if receiving.dropped:
                    return
                if receiving.overflow:
                    out = {"ok": False, "error": "控えの中身が、送る速さの決まりより多く届きました"}
                    break
                if piece is None:   # 終わり：ディスクに書き終え、読み戻して確かめる
                    out = await self._write(lambda: backup.as_remote(incoming.finish))
                    incoming = None   # 書き終えた・書けなかった：どちらも finish が片付けた
                    if out["ok"]:
                        sha256, size = out.pop("value")
                        out.update(sha256=sha256, size=size)
                    break
                out = await self._write(lambda piece=piece: backup.as_remote(lambda: incoming.feed(piece)))
                out.pop("value", None)
                if out["ok"]:
                    written += 1
                    await self._send_now(p.dumps("backup_ack", id=ident, n=written))
            if not receiving.dropped:
                await self._send_now(p.dumps("backup_done", id=ident, **out))
        finally:
            receiving.dropped = True   # 答えたあとに届く中身は捨てる
            if self._incoming.get(ident) is receiving:
                del self._incoming[ident]
            if incoming is not None:   # 書きかけを消す（控えと取り違えない）
                incoming.abort()

    async def _prune(self, msg: dict[str, Any]) -> None:
        out = await self._write(lambda: self.writer.prune(msg.get("folders") or {}))
        if out.get("ok"):
            out = {"ok": True, "removed": out["value"]}
        await self._send_now(p.dumps("backup_done", id=msg.get("id"), **out))

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


class _Receiving:
    """受け取っている途中の1つの控え（届いた中身の順番待ち。None＝終わり）。サーバーは書けた数を待ちながら送るので、順番待ちは窓
    （protocol.WINDOW）より長くならない。超えたら、決まりに合わないとして書くのをやめる（PC のメモリを守る）。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.dropped = self.overflow = False

    def put(self, piece: bytes | None) -> None:
        if self.dropped or self.overflow:
            return
        if self.queue.qsize() > p.WINDOW:
            self.overflow = True
        self.queue.put_nowait(piece)

    def drop(self) -> None:
        """つながりが切れた：書くのをやめる（書く側が、書きかけを消す）。"""
        self.dropped = True
        self.queue.put_nowait(None)


def _busy(phone: Phone) -> bool:
    """回線が通話中か（答えられない＝分からない：通話中とみなす。PC の側の判断はこの1か所＝かけない側・おわびして切る側）。"""
    try:
        return bool(phone.in_call())
    except Exception:  # noqa: BLE001
        return True


def _apologize_if_busy(phone: Phone) -> None:
    if _busy(phone):
        _apologize(phone)


def _apologize(phone: Phone) -> None:
    try:
        phone.play_apology_and_hang_up()
    except Exception:  # noqa: BLE001 電話の部品の誤りで、PC の部品を止めない
        pass
