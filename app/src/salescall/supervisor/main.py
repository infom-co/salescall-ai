"""中央の見守り役の本体（サーバー。DESIGN.md 5-4・5-5）。持ち主 CL-2。

2026-10-21 ユーザー承認：契約者の PC には電話をつなぐ部分（PC の部品）だけを置き、それ以外はサーバーで動かす。中央の見守り役は、
すべての契約者の分の仕事をし、PC の部品がつないでいる回線ごとに、電話をかける流れを動かす。

1. **1つの機械に1つだけ**（機械の中の印のファイルを押さえる。`salescall.link.locks`）。
2. **起動したら、前の自分（同じ名前）が借りていた回線の進行中の試行を後始末**する（予約のまま＝取り消し、発信してから＝結果不明。
   お客様が出ていれば、締めがお詫びしてすぐかけ直す）。
3. **PC の部品がつないでいる回線を借り、決まった間隔で延ばす**（`links.Links.lines`）。ほかの見守り役が生きて借りている回線は借りない。
   PC がつながりを切った回線は、その回線の流れを止め（いまの通話は、PC の側で切れる）、止まったら後始末して返す（止まるまでは
   借用を延ばし続ける＝通話の途中の回線を、ほかの見守り役に渡さない）。
   同じ回線を別の PC がつなぎ直したら、前の流れが止まってから、新しい PC で始める（同じスマホで二重にかけない）。
4. **回線ごとの流れ**（`line.loop`）：別の流れ（スレッド）で動かす。外へ出た誤りでは止まらない（その通話だけを結果不明にする）。
   止まってしまったら（思いがけない終わり）、後始末して「見守り役の誤り」として記録し（CL-2 へ修正依頼）、間をあけて立ち上げ直す
   （続けて止まるほど間を延ばす。上限5分）。
5. **決まった間隔の仕事**（`jobs.JOBS`）：すべての契約者に共通の仕事は1つ、契約者ごとの仕事はすべての契約者について1つずつ、
   それぞれ別の流れで、同じ仕事が重ならないように回す。
6. **版**：プログラムとデータベースの版がずれていれば、回線ごとの流れを止め（いまの通話は最後まで）、立ち上げず、決まった間隔の仕事も
   しない（`supervisor.updates`。合えば、またかける）。ずれは CL-2 へ（変わったときだけ）。
7. **どの段の誤りでも、本体は止まらない**（データベースに届かない など）：記録して、次の回にまた試す。
8. **止め方は1つ**（Ctrl+C・終わりの合図 SIGTERM・入れ替えの前の命令 `drain`）：新しい電話をかけず、話している通話がある間は
   待ち（supervisor.call_live の1か所。通話の長さでは打ち切らない＝2026-10-24 ユーザー。固まった通話は回線ごとの処理が3秒でお詫びして
   終える）、話していない回線の流れは `SHUTDOWN_WAIT` まで待って、回線を返して止まる。`drain`（`ask_drain`）は、機械の中の印のファイル（`DRAIN_FLAG`）で、動いている見守り役に
   頼み、止まるまで待って知らせる（入れ替えで通話をいきなり切らない。動かす所の仕組みの待つ長さの上限に頼らない＝⛔ 2026-10-22 の
   見直し15）。動き出す前に置かれた印は、前の回の残りとして消す（次に動かしたとたんに止まらない）。止まったことは、見守り役が
   止まり終えたときに書く「止まった」の印（`DRAIN_DONE`。頼んだ印と同じ番号）で知る（⛔ 見直し16：前は、止まったかを印のファイルを
   一瞬押さえて確かめていたので、その一瞬に動き出した新しい見守り役が「もう動いている」と思って終わりえた）。
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from salescall.core import dial_exit, leases
from salescall.core.access import Access, busy, unreachable
from salescall.link.basics import backoff
from salescall.link.locks import AlreadyRunning, SingleInstance, lock_dir, machine_id
from salescall.supervisor import (ACTOR, busy_seen, call_live, config, flush_busy, jobs, line, local_pc, plugins, report,
                                 updates, watchdog)
from salescall.supervisor.links import Attached, Links, NoLinks

RESTART_FIRST, RESTART_MAX = 5.0, 300.0   # 回線ごとの流れを立ち上げ直すまでの間（秒。続けて止まるほど倍に）
STABLE_AFTER = 600.0                       # 回線ごとの流れが、これだけ続けて動いたら、止まった回数を数え直す（秒）
RENEW_FALLBACK = 10.0                      # 延ばす間隔をまだ一度も読めていないとき（データベースに届かない）の、次に試すまでの秒数
JOB_THREADS = 8                            # 決まった間隔の仕事を同時に回す流れの数（多い分は順番を待つ）
HEAVY_THREADS = 4                          # 長い仕事（控えの書き込み＝遅い PC を待つ）の流れの数。締め・後始末の順番を塞がないよう分ける
__all__ = ["AlreadyRunning", "Supervisor", "serve", "holder_name"]


# 止めるとき、いまの通話を待つ決まり（1か所＝`Supervisor.shutdown`）：話している通話（supervisor.call_live）がある回線の流れは、
# 通話が終わるまで待つ（通話の長さに上限は無い＝2026-10-24 ユーザー：時間で業務を打ち切らない。固まった通話は、回線ごとの処理が
# call.frozen_after＝3秒でお詫びして終える）。話していない回線の流れ（門・発信の出口の取引の途中など）は、この長さまで待つ。
# ⛔ 0130：前はデータベースの「進んだ時刻」で 90 秒見ていた。入れ替えのときは、先に `drain` で止めておく（動かす所の仕組みの待つ長さ＝
# Fly.io の kill_timeout には上限があるので頼らない）
SHUTDOWN_WAIT = 30.0
DRAIN_FLAG = "salescall-supervisor.drain"   # 「新しい電話をかけずに、いまの通話が終わったら終わって」の印（機械の中。lock_dir）
DRAIN_DONE = "salescall-supervisor.drained"  # 頼まれて止まり終えた印（中身は頼んだ印と同じ番号）


LOCK_FILE = "salescall-supervisor.lock"   # 1つの機械に1つだけ（機械の中の印）


def ask_drain(wait_seconds: float | None = None, every: float = 1.0) -> bool | None:
    """動いている見守り役に「新しい電話をかけずに、いまの通話が終わったら終わって」と頼み、止まるまで待つ（命令 `drain`。入れ替えの
    前に使う）。止まったら True、待っても止まらなければ False、動いていなければ None（印は置かない）。wait_seconds＝待つ長さの上限
    （None＝上限なし：見守り役が止まり終えるか、止まり終えた印を書かずに終わるまで待つ。通話の長さに上限は無いため）。"""
    lock, flag, done = lock_dir() / LOCK_FILE, lock_dir() / DRAIN_FLAG, lock_dir() / DRAIN_DONE
    try:
        with SingleInstance(lock):   # 押さえられた：動いていない
            return None
    except AlreadyRunning:
        pass
    token = repr(time.time())
    try:
        done.unlink()                # 前の回の「止まった」の印
    except OSError:
        pass
    flag.write_text(token)
    deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
    while deadline is None or time.monotonic() < deadline:   # 待つあいだは印のファイルを押さえない（動き出す見守り役の邪魔をしない）
        time.sleep(every)
        try:
            if done.read_text() == token:   # 見守り役が止まり終えた
                return True
        except OSError:
            pass
        if deadline is None:   # 上限なし：止まり終えた印を書かずに終わった（落ちた）なら、待ち続けない
            try:
                with SingleInstance(lock):
                    return done.exists() and done.read_text() == token
            except AlreadyRunning:
                continue
    return False


def drained(token: str) -> None:
    """頼まれて止まり終えた印を書く（見守り役が、止まり終えたときに1回。drain の命令が、これを待つ）。"""
    try:
        (lock_dir() / DRAIN_DONE).write_text(token)
    except OSError:
        pass


def drain_asked(since: float) -> str | None:
    """drain の印があれば、その番号（見たら消す。止まり終えたら `drained` に渡す）。無ければ None。since＝この見守り役が動き出した
    時刻（time.time）：それより前に置かれた印は、前の回の残りとして消すだけ（判断はこの1か所）。"""
    flag = lock_dir() / DRAIN_FLAG
    try:
        text = flag.read_text()
    except OSError:
        return None
    try:
        at = float(text)
    except ValueError:   # 読めない印：置かれた時刻で見る
        try:
            at = flag.stat().st_mtime
        except OSError:
            return None
    try:
        flag.unlink()
    except OSError:
        pass
    return text if at >= since else None


def holder_name() -> str:
    """この見守り役の名前（機械で決まる。起動し直しても同じ＝前に落ちた自分の回線を後始末できる）。"""
    return f"{socket.gethostname()}#{machine_id()}#hub"


@dataclass
class _Worker:
    line_id: UUID
    at: Attached                 # 立ち上げたときのつなぎ（契約者・PC・つなぎの番号）
    thread: Any                  # 回線ごとの流れ（is_alive・join）。立ち上げ直しを待っているあいだは None
    stop: Any                    # 止める印（set）
    fails: int = 0
    restart_at: float = 0.0
    started_at: float = 0.0
    stopping: bool = False       # こちらから止めた（つながりが切れた・版がずれた・PC が替わった）


def _start_thread(cfg: config.Config, access: Access, at: Attached, line_id: UUID, holder: str, links: Links) -> tuple[Any, Any]:
    driver = links.driver(line_id, at)   # この流れは、立ち上げたときのつなぎだけに頼む（替われば、流れごと止めて立ち上げ直す）
    runner = plugins.load(cfg.call_runner, line_id, driver)
    stop = threading.Event()
    thread = threading.Thread(target=line.loop, args=(access, at.tenant_id, line_id, holder, driver, runner, stop),
                              name=f"salescall-line-{line_id}", daemon=True)
    thread.start()
    return thread, stop


def _backoff(fails: int) -> float:
    """続けて失敗するほど、次に試すまでの間を延ばす（上限 RESTART_MAX。決まりは link.basics.backoff の1か所）。"""
    return backoff(fails, RESTART_FIRST, RESTART_MAX)


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
    _started: dict[str, float] = field(default_factory=dict)      # 仕事 → 始めた時刻（固まった仕事に気づくため）
    _stuck: set[str] = field(default_factory=set)                  # 固まったと知らせた仕事（終わるまで1回だけ）
    _next_renew: float = 0.0
    _renew_every: float | None = None
    version_gap: str = updates.UNKNOWN   # プログラムとデータベースの版のずれ（確かめるまでは「分からない」）
    leftover_roles: int = 0              # 前の PC の鍵の役が残っている数（残り始めたときだけ知らせる）

    def __post_init__(self) -> None:
        self.holder = self.holder or holder_name()
        self.access = Access(self.cfg.dsn)
        self.pool = ThreadPoolExecutor(max_workers=JOB_THREADS, thread_name_prefix="salescall-job")
        self.heavy_pool = ThreadPoolExecutor(max_workers=HEAVY_THREADS, thread_name_prefix="salescall-backup")
        self.context = jobs.Context(self.access, self.cfg, self.holder, self.links)
        # 動いているプログラムの版（読み込んだときに1回。こちらの不具合で休んだ行・止めた契約者を、直した版に替わったら戻すため）
        self.program = updates.program_version(self.cfg.call_runner, self.cfg.verifier, self.cfg.prober)

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
        if self.version_gap == updates.SAME:   # 版が合っている：動いているプログラムの版を書く（替わったときだけ書かれる）
            try:
                self.access.in_system(ACTOR, lambda tx: updates.note(tx, self.program))
            except Exception as e:  # noqa: BLE001
                report(self.access, None, "プログラムの版の記録", e)
        try:   # 前の PC の鍵の役が残っていないか（残っていれば、CL-2 へ修正依頼。残り始めたときだけ）
            left, self.leftover_roles = self.leftover_roles, self.access.in_system(ACTOR, updates.leftover_pc_roles)
            if self.leftover_roles and not left:
                report(self.access, None, "前の PC の鍵の役", RuntimeError(updates.LEFTOVER))
        except Exception as e:  # noqa: BLE001
            report(self.access, None, "前の PC の鍵の役の確かめ", e)

    # ── 回線の借用と、回線ごとの流れ ──
    def renew(self, now: float) -> None:
        """PC の部品がつないでいる回線を借りる・延ばす。借りられた回線は流れを動かし、つながりが切れた回線は流れを止めて、止まったら
        後始末して返す。版がずれていれば、流れを止め（いまの通話は最後まで）、立ち上げない。"""
        self._load_tenants()
        attached = self.links.lines() if self.cfg.calls else {}
        down = False   # データベースに届かないと分かったら、ほかの回線で待ち直さない（回線の数だけ待つと、この回りが止まる）
        crowded = False   # 混雑（共有のつなぎが全部使われている）と分かったら、この回はもう借りに行かない（さらに混ませない。
        #                   ⛔ 2026-10-22 の見直し13：前は回線の数だけ空きを待ち、この回りが止まった）。流れはそのまま、次の回に延ばす
        for line_id, at in attached.items():
            if crowded:
                continue
            until = None
            if not down:
                try:
                    until = self.access.in_tenant(ACTOR, at.tenant_id, lambda tx, l=line_id: leases.take_line(tx, l, self.holder))
                except Exception as e:  # noqa: BLE001 データベースに届かない：延ばせない回線は期限で門が通さなくなる
                    down = unreachable(e)
                    report(self.access, at.tenant_id, "回線の借用", e, line_id=line_id)
                    if busy(e):   # 混雑：いまの流れはそのまま（借用の期限はまだ残っている）。次の回にまた延ばす
                        crowded = True
                        continue
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
            elif w is not None and w.at != at:   # 別の PC・同じ PC の新しいつなぎに替わった：前の流れが止まってから始める
                self._stop_worker(line_id)
            elif w is None:
                self._launch(line_id, at, fails=0, now=now)
        for line_id in [l for l in self.leased if l not in attached]:   # つながりが切れた回線
            self._stop_worker(line_id)
            if crowded:   # 延ばす・後始末して返すのは、混雑が解けた次の回に（借りたままの印は残す）
                continue
            w = self.workers.get(line_id)
            if w is not None and w.thread is not None and w.thread.is_alive():
                self._extend(line_id)   # 流れがまだ止まっていない（通話の途中）：借用を延ばし続ける
            else:
                self.workers.pop(line_id, None)
                self._finish(line_id)
        try:
            if not crowded:
                self._renew_every = float(self.access.in_system(ACTOR, leases.renew_every))
        except Exception as e:  # noqa: BLE001 読めなければ、前に読めた値（無ければ短い間）で次に試す
            report(self.access, None, "延ばす間隔の読み出し", e)
        self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def _extend(self, line_id: UUID) -> None:
        """流れがまだ動いている回線の借用を延ばす（つながりが切れた・止める途中でも。期限が切れると、ほかの見守り役の後始末が、まだ
        話している通話を結果不明にし、締めがお詫びのかけ直しを決めて、同じお客様へ重ねてかけうる）。"""
        tenant_id = self.leased.get(line_id)
        if tenant_id is None:
            return
        try:
            self.access.in_tenant(ACTOR, tenant_id, lambda tx: leases.take_line(tx, line_id, self.holder))
        except Exception as e:  # noqa: BLE001 延ばせなければ、期限で門が通さなくなる（記録だけ）
            report(self.access, tenant_id, "回線の借用", e, line_id=line_id)

    def _launch(self, line_id: UUID, at: Attached, *, fails: int, now: float) -> None:
        """回線ごとの流れを立ち上げる。その前に、その回線の後始末をする（いま進行中の試行があれば、前の流れが残したもの）。
        後始末も立ち上げもできなければ、間をあけてまた試す（続けるほど延ばす）。"""
        try:
            self._recover(line_id, at.tenant_id, raise_errors=True)
            thread, stop = self.start_worker(self.cfg, self.access, at, line_id, self.holder, self.links)
        except Exception as e:  # noqa: BLE001 立ち上げない（後始末が済まないうちにかけない）
            report(self.access, at.tenant_id, "回線ごとの流れの立ち上げ", e, line_id=line_id)
            self.workers[line_id] = _Worker(line_id, at, None, None, fails + 1, now + _backoff(fails))
            return
        self.workers[line_id] = _Worker(line_id, at, thread, stop, fails, started_at=now)

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
            if not w.stopping:   # 思いがけず止まった：通話の途中なら、おわびして切ってもらう（データベースの後始末だけでは、
                self._hang_up_left(line_id, w.at)   # 電話がつながったまま無言になる＝⛔ 2026-10-22 の見直し）
            self._recover(line_id, w.at.tenant_id)   # 止まった流れの通話を、すぐ後始末する（お客様が出ていれば、締めがお詫びしてかけ直す）
            if w.stopping:
                self.workers.pop(line_id, None)
                if line_id not in attached:
                    self._finish(line_id)
                continue
            report(self.access, w.at.tenant_id, "回線ごとの流れが止まった", RuntimeError("思いがけない終わり"), line_id=line_id)
            self.workers[line_id] = _Worker(line_id, w.at, None, None, w.fails + 1, now + _backoff(w.fails))

    def _hang_up_left(self, line_id: UUID, at: Attached) -> None:
        """回線ごとの流れが思いがけず止まった：その回線が通話中（か分からない）なら、おわびして切ってもらう（判断は発信の出口の
        dial_exit.abandon の1か所。つないでいなければ、PC の部品の側でおわびして切る）。"""
        try:
            driver = self.links.driver(line_id, at)
            busy_line = True
            try:
                busy_line = bool(driver.in_call())
            except Exception:  # noqa: BLE001 答えられない＝通話中とみなす
                pass
            if busy_line:
                dial_exit.abandon(driver)
        except Exception as e:  # noqa: BLE001 頼めなくても止めない
            report(self.access, at.tenant_id, "止まった流れの通話を切る", e, line_id=line_id)

    # ── 決まった間隔の仕事 ──
    def run_jobs(self, now: float) -> None:
        """決まった間隔の仕事を回す（共通の仕事は1つ、契約者ごとの仕事はすべての契約者について1つずつ）。版がずれているあいだは、
        どの仕事もしない（古いプログラムが新しいデータベースの決まりを知らずに書くと、守りが外れうる）。"""
        if self.version_gap != updates.SAME:
            return
        for job in jobs.JOBS:
            if not job.wanted(self.cfg):
                continue
            for tenant_id in ([None] if job.scope == "system" else self.tenants):
                key = job.key(tenant_id)
                if now < self._next_job.get(key, 0.0):
                    continue
                running = self._running.get(key)
                if running is not None and not running.done():   # 前の回がまだ終わっていない：重ねない
                    if now - self._started.get(key, now) > job.lease and key not in self._stuck:
                        # 借りる期限を過ぎても終わらない（固まった）：止めずに CL-2 へ修正依頼（1回だけ。⛔ 2026-10-22 の見直し：
                        # だれも気づかず、二度と出し直されなかった）
                        self._stuck.add(key)
                        report(self.access, tenant_id, job.name, TimeoutError(f"{job.lease} 秒たっても終わりません（固まった）"))
                    continue
                self._stuck.discard(key)
                self._started[key] = now
                ctx = self.context if tenant_id is None else self.context.for_tenant(tenant_id)
                self._running[key] = (self.heavy_pool if job.heavy else self.pool).submit(jobs.run_job, ctx, job)
                self._next_job[key] = now + job.every

    # ── 1回分（本体の繰り返し） ──
    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        seen = busy_seen()
        steps: list[tuple[str, Callable[[], None]]] = (
            [("版の確かめ", self.check_version), ("回線の借用", lambda: self.renew(now))] if now >= self._next_renew else [])
        steps += [("回線ごとの流れの見張り", lambda: self.watch_workers(now)), ("決まった間隔の仕事", lambda: self.run_jobs(now)),
                  # 混雑の記録は、この回に混雑が無かったときだけ（混んでいる最中に記録しに行くと、さらに混ませる。⛔ 見直し13）
                  ("混雑の記録", lambda: flush_busy(self.access) if busy_seen() == seen else None)]
        for what, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 どの段の誤りでも本体は止めない（次の回にまた試す）
                report(self.access, None, what, e)
                if what == "回線の借用":
                    self._next_renew = now + (self._renew_every or RENEW_FALLBACK)

    def shutdown(self, wait_seconds: float = SHUTDOWN_WAIT) -> None:
        """回線ごとの流れを止め（いまの通話は最後まで）、終わったら、その回線の後始末をして返す。話している通話がある回線
        （supervisor.call_live の1か所）は終わるまで待ち（通話の長さでは打ち切らない）、話していない回線は wait_seconds まで待つ。"""
        for line_id in list(self.workers):
            self._stop_worker(line_id)
        deadline, next_extend = time.monotonic() + wait_seconds, 0.0
        while True:   # 通話が終わるのを待つあいだも、その回線の借用は延ばし続ける
            alive = [l for l, w in self.workers.items() if w.thread is not None and w.thread.is_alive()]
            left = deadline - time.monotonic()
            if not alive or (left <= 0 and not any(call_live(l) for l in alive)):
                break
            if time.monotonic() >= next_extend:
                for line_id in alive:
                    self._extend(line_id)
                next_extend = time.monotonic() + (self._renew_every or RENEW_FALLBACK)
            self.workers[alive[0]].thread.join(1.0 if left <= 0 else min(1.0, left))
        for line_id in list(self.leased):
            w = self.workers.get(line_id)
            if w is not None and w.thread is not None and w.thread.is_alive():   # まだ通話の途中：返さない（期限で次が後始末する）
                continue
            self._finish(line_id)
        # 決まった間隔の仕事は、残りの待ち時間まで待つ（固まった仕事を待ち続けて終われない、にしない。借用の期限で次が引き継ぐ）
        rest = [f for f in self._running.values() if not f.done()]
        if rest:
            futures_wait(rest, timeout=max(1.0, deadline - time.monotonic()))
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.heavy_pool.shutdown(wait=False, cancel_futures=True)   # 控えの書き込みは待たない（借用の期限で、次が引き継ぐ）


def serve(cfg: config.Config, stop: threading.Event | None = None) -> None:
    """中央の見守り役を動かす（止める印が立つか Ctrl+C まで）。PC の部品のつなぎ口（channel）も、同じ処理の中で開く。"""
    from salescall.supervisor import channel
    stop = stop or threading.Event()
    if cfg.listen is None:   # 無いと、PC がつながらず、電話もかけず、控えも書かないまま黙って動く
        raise config.ConfigMissing("設定 SALESCALL_LISTEN が入っていません（PC の部品からのつなぎを待つ場所。手順：app/README.md）")
    if cfg.call_runner and not cfg.verifier:   # 検証の部品が無いと、かけた電話を締められず、行もお客様も待ち続ける（2026-10-22 の見直し）
        raise config.ConfigMissing("設定 SALESCALL_VERIFIER が入っていません（電話をかけるときは、通話後の検証の部品も要ります。"
                                   "手順：app/README.md）")
    if cfg.call_runner and not cfg.prober:   # 停止を解く確かめの部品が無いと、回線・会話AIの停止が自動では解けず、管理者か契約者が
        #   解くまで、約束のお客様も途中で切れたお客様も待ち続ける（管理者も契約者も確かめと承認だけ＝2026-10-17 ユーザー。⛔ 見直し14）
        raise config.ConfigMissing("設定 SALESCALL_PROBER が入っていません（電話をかけるときは、停止を解く確かめの部品も要ります。"
                                   "手順：app/README.md）")
    if cfg.local_only and cfg.admin_tenant is None:   # 移る間の形は、この機械の PC の部品の通行証を自分で払い出す（local_pc）。
        #   どの契約者の分か無いと、通行証が無いまま黙って動き、電話もかけず控えも書かない（⛔ 2026-10-22 の見直し13）
        raise config.ConfigMissing("設定 SALESCALL_ADMIN_TENANT が入っていません（つなぎ口がこの機械の中だけのときは、この機械の"
                                   "PC の部品の契約者が要ります。手順：app/README.md）")
    with SingleInstance(lock_dir() / LOCK_FILE):
        started = time.time()   # これより前に置かれた drain の印は、前の回の残り
        hub = channel.Hub(Access(cfg.dsn), cfg)
        sup = Supervisor(cfg, hub)
        hub_fails, next_hub = 0, 0.0
        try:
            hub.start()
        except channel.ChannelDown as e:   # 待ち受けを始められない（前の自分がまだ場所を放していない など）：止めずに、間をあけて試し直す
            report(sup.access, None, "PC の部品のつなぎ口", e)
            hub_fails, next_hub = 1, time.monotonic() + _backoff(0)
        try:
            sup.start()
        except Exception as e:  # noqa: BLE001 起動のときに届かなくても止めない（tick がまた試す）
            report(sup.access, None, "起動", e)
        next_pass = 0.0   # 移る間の形：この機械の PC の部品の通行証を確かめる時刻（local_pc の1か所）
        dog = watchdog.Watchdog(watchdog.end_process(lambda: report(
            sup.access, None, "見守り役の本体", RuntimeError(f"本体が {int(watchdog.STALL_LIMIT)} 秒まわらなかったので終えた"))))
        dog.start()
        asked: str | None = None   # drain で頼まれた（止まり終えたら、その番号で「止まった」の印を書く）
        try:
            while not stop.is_set():
                dog.beat()
                asked = drain_asked(started)
                if asked:   # 入れ替えの前の drain：ほかの止め方と同じに止める（新しい電話をかけず、いまの通話の終わりを待つ）
                    print("入れ替えの準備（drain）：新しい電話をかけずに、いまの通話の終わりを待ってから止まります", flush=True)
                    stop.set()
                    break
                if time.monotonic() >= next_pass:
                    try:
                        local_pc.ensure(sup.access, cfg)
                    except Exception as e:  # noqa: BLE001 データベースに届かないときも止めない（次の確かめでまた入れる）
                        report(sup.access, None, "この機械の PC の部品の通行証", e)
                    next_pass = time.monotonic() + local_pc.CHECK_EVERY
                if not hub.running() and not stop.is_set() and time.monotonic() >= next_hub:
                    # つなぎ口の流れが落ちた・始められなかった：記録して、動かし直す（どの PC ともやり取りできないまま、黙って
                    # 動き続けない）。動かし直せなくても見守り役は終わらない（⛔ 2026-10-22 の見直し：前は ChannelDown で処理が
                    # まるごと終わり、だれも起こさず、PC の途切れの知らせも出なかった）。続けて失敗するほど間を延ばす（上限5分）
                    if hub.error is not None:
                        report(sup.access, None, "PC の部品のつなぎ口", hub.error)
                    try:
                        hub.start()
                        hub_fails = 0
                    except channel.ChannelDown as e:
                        report(sup.access, None, "PC の部品のつなぎ口", e)
                        next_hub = time.monotonic() + _backoff(hub_fails)
                        hub_fails += 1
                sup.tick()
                stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            dog.stop()   # 止めるとき（いまの通話の終わりを待つあいだ）は見張らない
            sup.shutdown()
            hub.close()
            if asked:
                drained(asked)
