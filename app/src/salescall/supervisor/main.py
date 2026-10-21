"""中央の見守り役の本体（サーバー。DESIGN.md 5-4・5-5）。持ち主 CL-2。

2026-10-21 ユーザー承認：契約者の PC には電話をつなぐ部分（PC の部品）だけを置き、それ以外はサーバーで動かす。中央の見守り役は、
すべての契約者の分の仕事をし、PC の部品がつないでいる回線ごとに、電話をかける流れを動かす。

1. **1つの機械に1つだけ**（機械の中の印のファイルを押さえる。`salescall.link.locks`）。
2. **起動したら、前の自分（同じ名前）が借りていた回線の進行中の試行を後始末**する（予約のまま＝取り消し、発信してから＝結果不明。
   お客様が出ていれば、締めがお詫びしてすぐかけ直す）。
3. **PC の部品がつないでいる回線を借り、決まった間隔で延ばす**（`links.Links.lines`）。ほかの見守り役が生きて借りている回線は借りない。
   PC がつながりを切った回線は延ばさず、その回線の流れを止め（いまの通話は、PC の側で切れる）、止まったら後始末して返す。
   同じ回線を別の PC がつなぎ直したら、前の流れが止まってから、新しい PC で始める（同じスマホで二重にかけない）。
4. **回線ごとの流れ**（`line.loop`）：別の流れ（スレッド）で動かす。外へ出た誤りでは止まらない（その通話だけを結果不明にする）。
   止まってしまったら（思いがけない終わり）、後始末して「見守り役の誤り」として記録し（CL-2 へ修正依頼）、間をあけて立ち上げ直す
   （続けて止まるほど間を延ばす。上限5分）。
5. **決まった間隔の仕事**（`jobs.JOBS`）：すべての契約者に共通の仕事は1つ、契約者ごとの仕事はすべての契約者について1つずつ、
   それぞれ別の流れで、同じ仕事が重ならないように回す。
6. **版**：プログラムとデータベースの版がずれていれば、回線ごとの流れを止め（いまの通話は最後まで）、立ち上げず、決まった間隔の仕事も
   しない（`supervisor.updates`。合えば、またかける）。ずれは CL-2 へ（変わったときだけ）。
7. **どの段の誤りでも、本体は止まらない**（データベースに届かない など）：記録して、次の回にまた試す。
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from salescall.core import leases
from salescall.core.access import Access
from salescall.link.locks import AlreadyRunning, SingleInstance, lock_dir, machine_id
from salescall.supervisor import ACTOR, config, jobs, line, plugins, report, updates
from salescall.supervisor.links import Attached, Links, NoLinks

RESTART_FIRST, RESTART_MAX = 5.0, 300.0   # 回線ごとの流れを立ち上げ直すまでの間（秒。続けて止まるほど倍に）
STABLE_AFTER = 600.0                       # 回線ごとの流れが、これだけ続けて動いたら、止まった回数を数え直す（秒）
RENEW_FALLBACK = 10.0                      # 延ばす間隔をまだ一度も読めていないとき（データベースに届かない）の、次に試すまでの秒数
JOB_THREADS = 8                            # 決まった間隔の仕事を同時に回す流れの数（多い分は順番を待つ）
__all__ = ["AlreadyRunning", "Supervisor", "serve", "holder_name"]


def holder_name() -> str:
    """この見守り役の名前（機械で決まる。起動し直しても同じ＝前に落ちた自分の回線を後始末できる）。"""
    return f"{socket.gethostname()}#{machine_id()}#hub"


@dataclass
class _Worker:
    line_id: UUID
    tenant_id: UUID
    pc_id: UUID
    thread: Any                  # 回線ごとの流れ（is_alive・join）。立ち上げ直しを待っているあいだは None
    stop: Any                    # 止める印（set）
    fails: int = 0
    restart_at: float = 0.0
    started_at: float = 0.0
    stopping: bool = False       # こちらから止めた（つながりが切れた・版がずれた・PC が替わった）


def _start_thread(cfg: config.Config, access: Access, at: Attached, line_id: UUID, holder: str, links: Links) -> tuple[Any, Any]:
    driver = links.driver(line_id, at.pc_id)   # この流れは、立ち上げたときの PC だけに頼む（PC が替われば、流れごと止めて立ち上げ直す）
    runner = plugins.load(cfg.call_runner, line_id, driver)
    stop = threading.Event()
    thread = threading.Thread(target=line.loop, args=(access, at.tenant_id, line_id, holder, driver, runner, stop),
                              name=f"salescall-line-{line_id}", daemon=True)
    thread.start()
    return thread, stop


def _backoff(fails: int) -> float:
    """続けて失敗するほど、次に試すまでの間を延ばす（上限 RESTART_MAX）。"""
    return min(RESTART_FIRST * 2 ** fails, RESTART_MAX)


@dataclass
class Supervisor:
    cfg: config.Config
    links: Links = field(default_factory=NoLinks)
    start_worker: Callable[..., tuple[Any, Any]] = _start_thread
    holder: str = ""
    access: Access = field(init=False)
    workers: dict[UUID, _Worker] = field(default_factory=dict)
    leased: dict[UUID, UUID] = field(default_factory=dict)        # 借りている回線 → 契約者
    tenants: list[UUID] = field(default_factory=list)
    _next_job: dict[str, float] = field(default_factory=dict)
    _running: dict[str, Future] = field(default_factory=dict)
    _next_renew: float = 0.0
    _renew_every: float | None = None
    version_gap: str = updates.UNKNOWN   # プログラムとデータベースの版のずれ（確かめるまでは「分からない」）

    def __post_init__(self) -> None:
        self.holder = self.holder or holder_name()
        self.access = Access(self.cfg.dsn)
        self.pool = ThreadPoolExecutor(max_workers=JOB_THREADS, thread_name_prefix="salescall-job")
        self.context = jobs.Context(self.access, self.cfg, self.holder, self.links)

    # ── 起動 ──
    def start(self) -> None:
        self.check_version()
        self._load_tenants()
        for t in self.tenants:   # 前の自分（同じ名前）が借りていた回線の進行中の試行を後始末する
            try:
                self.access.in_tenant(ACTOR, t, lambda tx: leases.recover(tx, self.holder))
            except Exception as e:  # noqa: BLE001
                report(self.access, t, "起動のときの後始末", e)
        self.renew(time.monotonic())

    def _load_tenants(self) -> None:
        self.tenants = self.access.in_system(ACTOR, lambda tx: [r["t"] for r in tx.all("SELECT salescall.all_tenants() AS t")])

    # ── プログラムとデータベースの版（supervisor.updates） ──
    def check_version(self) -> None:
        """版を比べる。比べられなければ「分からない」＝合わないとみなす（かけない・仕事をしない側）。ずれは、変わったときだけ CL-2 へ。"""
        before = self.version_gap
        try:
            self.version_gap = self.access.in_system(ACTOR, updates.current)
        except Exception as e:  # noqa: BLE001
            self.version_gap = updates.UNKNOWN
            report(self.access, None, "版の確かめ", e)
            return
        if self.version_gap != updates.SAME and before != self.version_gap:
            report(self.access, None, "版のずれ", RuntimeError(updates.describe(self.version_gap)))

    # ── 回線の借用と、回線ごとの流れ ──
    def renew(self, now: float) -> None:
        """PC の部品がつないでいる回線を借りる・延ばす。借りられた回線は流れを動かし、つながりが切れた回線は流れを止めて、止まったら
        後始末して返す。版がずれていれば、流れを止め（いまの通話は最後まで）、立ち上げない。"""
        self._load_tenants()
        attached = self.links.lines() if self.cfg.calls else {}
        for line_id, at in attached.items():
            try:
                until = self.access.in_tenant(ACTOR, at.tenant_id, lambda tx, l=line_id: leases.take_line(tx, l, self.holder))
            except Exception as e:  # noqa: BLE001 データベースに届かない：延ばせない回線は期限で門が通さなくなる
                report(self.access, at.tenant_id, "回線の借用", e, line_id=line_id)
                until = None
            if until is None:   # 借りられない（ほかの見守り役が生きて借りている）・届かない：流れを止める
                self.leased.pop(line_id, None)
                self._stop_worker(line_id)
                continue
            self.leased[line_id] = at.tenant_id
            w = self.workers.get(line_id)
            if self.version_gap != updates.SAME:
                self._stop_worker(line_id)
                if w is None or w.thread is None or not w.thread.is_alive():
                    self._recover(line_id, at.tenant_id)
            elif w is not None and w.pc_id != at.pc_id:   # 同じ回線を別の PC がつなぎ直した：前の流れが止まってから始める
                self._stop_worker(line_id)
            elif w is None:
                self._launch(line_id, at, fails=0, now=now)
        for line_id in [l for l in self.leased if l not in attached]:   # つながりが切れた回線
            self._stop_worker(line_id)
            w = self.workers.get(line_id)
            if w is None or w.thread is None or not w.thread.is_alive():
                self.workers.pop(line_id, None)
                self._finish(line_id)
        try:
            self._renew_every = float(self.access.in_system(ACTOR, leases.renew_every))
        except Exception as e:  # noqa: BLE001 読めなければ、前に読めた値（無ければ短い間）で次に試す
            report(self.access, None, "延ばす間隔の読み出し", e)
        self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def _launch(self, line_id: UUID, at: Attached, *, fails: int, now: float) -> None:
        """回線ごとの流れを立ち上げる。その前に、その回線の後始末をする（いま進行中の試行があれば、前の流れが残したもの）。
        後始末も立ち上げもできなければ、間をあけてまた試す（続けるほど延ばす）。"""
        try:
            self._recover(line_id, at.tenant_id, raise_errors=True)
            thread, stop = self.start_worker(self.cfg, self.access, at, line_id, self.holder, self.links)
        except Exception as e:  # noqa: BLE001 立ち上げない（後始末が済まないうちにかけない）
            report(self.access, at.tenant_id, "回線ごとの流れの立ち上げ", e, line_id=line_id)
            self.workers[line_id] = _Worker(line_id, at.tenant_id, at.pc_id, None, None, fails + 1, now + _backoff(fails))
            return
        self.workers[line_id] = _Worker(line_id, at.tenant_id, at.pc_id, thread, stop, fails, started_at=now)

    def _recover(self, line_id: UUID, tenant_id: UUID, *, raise_errors: bool = False) -> None:
        try:
            self.access.in_tenant(ACTOR, tenant_id, lambda tx: leases.recover(tx, self.holder, line_id))
        except Exception as e:  # noqa: BLE001
            if raise_errors:
                raise
            report(self.access, tenant_id, "回線の後始末", e, line_id=line_id)

    def _finish(self, line_id: UUID) -> None:
        """回線を返す（つながりが切れて、流れが止まったあと）：進行中の試行を後始末してから返す。"""
        tenant_id = self.leased.pop(line_id, None)
        if tenant_id is None:
            return
        def back(tx: Any) -> None:
            leases.recover(tx, self.holder, line_id)
            leases.return_line(tx, line_id, self.holder)
        try:
            self.access.in_tenant(ACTOR, tenant_id, back)
        except Exception as e:  # noqa: BLE001 返せなくても、期限で次が後始末する
            report(self.access, tenant_id, "回線を返す", e, line_id=line_id)

    def _stop_worker(self, line_id: UUID) -> None:
        w = self.workers.get(line_id)
        if w is None:
            return
        if w.thread is None:   # 立ち上げ直しを待っていた：やめる
            self.workers.pop(line_id, None)
            return
        w.stopping = True
        w.stop.set()

    def watch_workers(self, now: float) -> None:
        """止まった回線ごとの流れを後始末する。こちらから止めたものは片付け、思いがけず止まったものは、間をあけて立ち上げ直す。"""
        attached = self.links.lines() if self.cfg.calls else {}
        for line_id, w in list(self.workers.items()):
            if w.thread is None:   # 立ち上げ直す時刻を待っている
                at = attached.get(line_id)
                if at is not None and now >= w.restart_at and line_id in self.leased and self.version_gap == updates.SAME:
                    self._launch(line_id, at, fails=w.fails, now=now)
                continue
            if w.thread.is_alive():
                if w.fails and now - w.started_at >= STABLE_AFTER:   # しばらく続けて動いた：止まった回数を数え直す
                    w.fails = 0
                continue
            self._recover(line_id, w.tenant_id)   # 止まった流れの通話を、すぐ後始末する（お客様が出ていれば、締めがお詫びしてかけ直す）
            if w.stopping:
                self.workers.pop(line_id, None)
                if line_id not in attached:
                    self._finish(line_id)
                continue
            report(self.access, w.tenant_id, "回線ごとの流れが止まった", RuntimeError("思いがけない終わり"), line_id=line_id)
            self.workers[line_id] = _Worker(line_id, w.tenant_id, w.pc_id, None, None, w.fails + 1, now + _backoff(w.fails))

    # ── 決まった間隔の仕事 ──
    def run_jobs(self, now: float) -> None:
        """決まった間隔の仕事を回す（共通の仕事は1つ、契約者ごとの仕事はすべての契約者について1つずつ）。版がずれているあいだは、
        どの仕事もしない（古いプログラムが新しいデータベースの決まりを知らずに書くと、守りが外れうる）。"""
        if self.version_gap != updates.SAME:
            return
        for job in jobs.JOBS:
            if not job.runs_here(self.cfg):
                continue
            for tenant_id in ([None] if job.scope == "system" else self.tenants):
                key = job.key(tenant_id)
                if now < self._next_job.get(key, 0.0):
                    continue
                running = self._running.get(key)
                if running is not None and not running.done():   # 前の回がまだ終わっていない：重ねない
                    continue
                ctx = self.context if tenant_id is None else self.context.for_tenant(tenant_id)
                self._running[key] = self.pool.submit(jobs.run_job, ctx, job)
                self._next_job[key] = now + job.every

    # ── 1回分（本体の繰り返し） ──
    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        steps: list[tuple[str, Callable[[], None]]] = (
            [("版の確かめ", self.check_version), ("回線の借用", lambda: self.renew(now))] if now >= self._next_renew else [])
        steps += [("回線ごとの流れの見張り", lambda: self.watch_workers(now)), ("決まった間隔の仕事", lambda: self.run_jobs(now))]
        for what, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 どの段の誤りでも本体は止めない（次の回にまた試す）
                report(self.access, None, what, e)
                if what == "回線の借用":
                    self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def shutdown(self, wait_seconds: float = 600.0) -> None:
        """回線ごとの流れを止め（いまの通話は最後まで）、終わったら、その回線の後始末をして返す。"""
        for line_id in list(self.workers):
            self._stop_worker(line_id)
        deadline = time.monotonic() + wait_seconds
        for w in self.workers.values():
            if w.thread is not None:
                w.thread.join(max(0.0, deadline - time.monotonic()))
        for line_id in list(self.leased):
            w = self.workers.get(line_id)
            if w is not None and w.thread is not None and w.thread.is_alive():   # まだ通話の途中：返さない（期限で次が後始末する）
                continue
            self._finish(line_id)
        self.pool.shutdown(wait=True)


def serve(cfg: config.Config, stop: threading.Event | None = None) -> None:
    """中央の見守り役を動かす（止める印が立つか Ctrl+C まで）。PC の部品のつなぎ口（channel）も、同じ処理の中で開く。"""
    from salescall.supervisor import channel
    stop = stop or threading.Event()
    with SingleInstance(lock_dir() / "salescall-supervisor.lock"):
        hub = channel.Hub(Access(cfg.dsn), cfg) if cfg.listen else None
        links: Links = hub if hub is not None else NoLinks()
        if hub is not None:
            hub.start()
        sup = Supervisor(cfg, links)
        try:
            sup.start()
        except Exception as e:  # noqa: BLE001 起動のときに届かなくても止めない（tick がまた試す）
            report(sup.access, None, "起動", e)
        try:
            while not stop.is_set():
                sup.tick()
                stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            sup.shutdown()
            if hub is not None:
                hub.close()
