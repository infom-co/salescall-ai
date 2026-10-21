"""つなぎ口と PC の部品の試験（DESIGN.md 5-5。supervisor/channel.py・agent/・link/）。本物のつなぎ口（127.0.0.1 の空いている番号）に、
本物の PC の部品をつなぎ、電話の部品だけを代わりにする。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from unittest import mock

from websockets.sync.client import connect

from salescall.agent import backup as pc_backup
from salescall.agent import config as agent_config
from salescall.agent import emergency as pc_emergency
from salescall.agent import main as agent_main
from salescall.agent.pc_login import PcPass
from salescall.core import agents, stops
from salescall.link import protocol, sealing
from salescall.link.phone import CallNotPlaced, CallWithheld
from salescall.link.protocol import RemoteError
from salescall.supervisor import channel, config
from salescall.supervisor.links import Attached
from salescall.supervisor.main import Supervisor
from tests.support import OPERATOR, SYSTEM
from tests.test_backup import drive_at
from tests.test_gate import HOLDER, Base

KEY = "架空の控えの鍵の元-0123456789-abcdefghij"


class FakePhone:
    """電話の部品の代わり（電話接続の担当の部品の形＝link.phone.Phone）。"""

    def __init__(self, busy=False, fail=None):
        self.calls, self.busy, self.fail, self.hung, self.apologies, self.played = [], busy, fail, 0, 0, []
        self.send_audio = self.send_event = None

    def in_call(self):
        return self.busy

    def start_call(self, phone):
        if self.fail:
            raise self.fail
        self.calls.append(phone)

    def hang_up(self):
        self.hung += 1

    def play_apology_and_hang_up(self):
        self.apologies += 1
        self.busy = False

    def on_audio(self, send):
        self.send_audio = send

    def on_event(self, send):
        self.send_event = send

    def play(self, audio):
        self.played.append(audio)


class Running:
    """PC の部品を、別の流れで動かす（止めるまで。断られたら error に残る）。"""

    def __init__(self, agent):
        self.agent, self.error, self.loop, self.stop = agent, None, None, None
        ready = threading.Event()

        async def go():
            self.loop, self.stop = asyncio.get_running_loop(), asyncio.Event()
            ready.set()
            await agent.run(self.stop)

        def main():
            try:
                asyncio.run(go())
            except BaseException as e:  # noqa: BLE001
                self.error = e
        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        ready.wait(10)

    def beat(self):
        """すぐ合図を送らせる（ふつうは BEAT_EVERY 秒ごと）。"""
        asyncio.run_coroutine_threadsafe(self.agent.beat(), self.loop).result(10)

    def close(self):
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.stop.set)
            except RuntimeError:
                pass
        self.thread.join(15)


def until(test, what, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = test()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"待っても、そうなりませんでした：{what}")


class LinkBase(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="salescall-link-"))
        patcher = mock.patch.dict(os.environ, {"SALESCALL_EMERGENCY_FLAG": str(self.dir / "EMERGENCY_STOP")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hub = self.start_hub()

    def start_hub(self, **more):
        values = {"SALESCALL_DSN": self.w.a._conninfo, "SALESCALL_LISTEN": "127.0.0.1:0",
                  "SALESCALL_CALL_RUNNER": "tests.test_supervisor:make_runner", **more}
        hub = channel.Hub(self.w.a, config.load(values))
        hub.start()
        self.addCleanup(hub.close)
        return hub

    def pc(self, s, label="架空の PC"):
        return agents.issue(self.w.a, SYSTEM, s["tenant"], label=label)

    def agent(self, got, lines, phones=None, drive=None, hub=None, **env):
        hub = hub or self.hub
        values = {"SALESCALL_HUB_URL": f"ws://127.0.0.1:{hub.port}/link", "SALESCALL_LINES": ",".join(map(str, lines)), **env}
        if drive is not None:
            values.update(SALESCALL_BACKUP_DRIVE=str(drive), SALESCALL_BACKUP_KEY=KEY)
        a = agent_main.Agent(agent_config.load(values), PcPass(got.tenant_id, got.pc_id, got.token),
                             phones if phones is not None else {line: FakePhone() for line in lines})
        r = Running(a)
        self.addCleanup(r.close)
        return r

    def attached(self, s, got, hub=None):
        """その PC のつなぎで、回線がつながるまで待ち、つなぎ（Attached）を返す。"""
        hub = hub or self.hub
        return until(lambda: (at := hub.lines().get(s["line"])) is not None and (at.tenant_id, at.pc_id) == (s["tenant"], got.pc_id)
                     and at, "回線がつながる")

    def open_manual(self, s):
        return self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: [
            r["event_id"] for r in stops.open_stops(tx, tenant_id=s["tenant"], client_id=None, business_id=None)
            if r["cause"] == "manual"])


class Connect(LinkBase):
    def test_a_call_goes_through_the_hub_to_the_phone_and_is_settled_and_the_line_returned(self):
        """サーバーの中央の見守り役が、PC の部品がつないだ回線で1本かけ（電話の部品の start_call）、締め、止めると回線を返す。"""
        s = self.ready()
        got = self.pc(s)
        phone = FakePhone()
        self.agent(got, [s["line"]], {s["line"]: phone})
        self.attached(s, got)
        sup = Supervisor(self.hub.cfg, self.hub, holder=HOLDER)
        sup._load_tenants = lambda: setattr(sup, "tenants", [s["tenant"]])
        sup.start()
        done = None
        deadline = time.monotonic() + 60
        while done is None and time.monotonic() < deadline:
            sup.tick()
            time.sleep(0.3)
            done = self.tx(s, lambda tx: tx.one("SELECT business_state FROM salescall.task_results WHERE task_id = %s",
                                                (s["task"],)))
        sup.shutdown(30)
        self.assertEqual((done and done["business_state"], phone.calls), ("completed", [s["phone"]]))
        held = self.w.a.in_tenant(SYSTEM, s["tenant"], lambda tx: tx.one(
            "SELECT lease_holder FROM salescall.lines WHERE line_id = %s", (s["line"],)))["lease_holder"]
        self.assertIsNone(held)
        seen = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.one(
            "SELECT last_seen_at, lines FROM salescall.pc_agents WHERE pc_id = %s", (got.pc_id,)))
        self.assertEqual((seen["last_seen_at"] is not None, seen["lines"]), (True, [s["line"]]))   # 合図はデータベースへ

    def test_voice_and_phone_events_pass_both_ways_from_any_thread(self):
        """お客様の声・電話の出来事は、電話の部品のどの流れからでもサーバーへ届き、サーバーの声は電話の部品へ届く。"""
        s = self.ready()
        got = self.pc(s)
        phone = FakePhone()
        self.agent(got, [s["line"]], {s["line"]: phone})
        driver = self.hub.driver(s["line"], self.attached(s, got))
        heard, events = [], []
        driver.on_audio(heard.append)
        driver.on_event(events.append)
        sender = threading.Thread(target=lambda: (phone.send_audio(b"\x01\x02"), phone.send_event({"kind": "answered"})))
        sender.start()
        sender.join()
        until(lambda: heard == [b"\x01\x02"] and events == [{"kind": "answered"}], "お客様の声と出来事がサーバーへ届く")
        driver.send_audio(b"\x03")
        until(lambda: phone.played == [b"\x03"], "サーバーの声が電話の部品へ届く")

    def test_the_phone_answers_and_failures_map_to_the_dial_exit(self):
        """通話中か・かける・切るは、PC の部品を通して電話の部品へ。かけられなかった＝CallNotPlaced（回線の異常として数える）、
        かけたか分からない＝ほかの誤り（結果不明）。つながっていない回線は「通話中」（かけない側）。"""
        s = self.ready()
        got = self.pc(s)
        phone = FakePhone(busy=True)
        self.agent(got, [s["line"]], {s["line"]: phone})
        at = self.attached(s, got)
        driver = self.hub.driver(s["line"], at)
        self.assertTrue(driver.in_call())
        phone.busy = False
        self.assertFalse(driver.in_call())
        driver.start_call("+81300000000")
        driver.hang_up()
        self.assertEqual((phone.calls, phone.hung), (["+81300000000"], 1))
        phone.fail = CallNotPlaced("試験：アプリが受け付けない")
        with self.assertRaises(CallNotPlaced) as e:
            driver.start_call("+81300000001")
        self.assertNotIsInstance(e.exception, CallWithheld)
        phone.fail = RuntimeError("試験：分からない")
        with self.assertRaises(RuntimeError):
            driver.start_call("+81300000002")
        driver.apologize_and_hang_up()                                       # サーバーの側の誤り：おわびして切る
        self.assertEqual(phone.apologies, 1)
        self.assertTrue(self.hub.driver(uuid.uuid4(), at).in_call())         # つないでいない回線
        away = Attached(s["tenant"], uuid.uuid4(), at.conn)
        self.assertTrue(self.hub.driver(s["line"], away).in_call())          # その回線をつないでいない PC
        with self.assertRaises(CallWithheld):                                # つないでいない：かけていないと確かに言える（回線の異常ではない）
            self.hub.driver(s["line"], away).start_call("+81300000003")

    def test_lines_of_another_tenant_are_dropped_and_two_pcs_on_one_line_keep_the_first(self):
        """名乗った回線は、その契約者の使っている回線だけが残る（ほかの契約者の回線の電話をかけさせない）。同じ回線を2つの PC が
        名乗ったら、先につないだほう（入れ替わり続けない）で、その契約者に知らせる。通話の途中の頼み・声は、作ったときのつなぎだけ。"""
        s = self.ready()
        other = self.ready()
        got = self.pc(s)
        first = self.agent(got, [s["line"], other["line"]])
        at = self.attached(s, got)
        self.assertNotIn(other["line"], self.hub.lines())
        self.assertEqual(first.agent.lines, [s["line"]])                    # PC の部品も、確かめ直した回線だけを使う
        driver = self.hub.driver(s["line"], at)                              # 1台目の PC で動いている回線の流れ
        heard = []
        driver.on_audio(heard.append)
        second = self.pc(s, "架空の2台目の PC")
        phone2 = FakePhone()
        self.agent(second, [s["line"]], {s["line"]: phone2})
        until(lambda: second.pc_id in self.hub.sessions, "2台目の PC")
        notes = lambda: self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(   # noqa: E731
            "SELECT 1 FROM salescall.notification_events WHERE kind = 'pc_line_conflict'"))
        until(notes, "同じ回線を2台の PC がつないでいると知らせる")
        driver.start_call("+81300000000")                                   # 通話の途中の頼み・声は、前の PC のまま
        driver.send_audio(b"\x09")
        until(lambda: first.agent.phones[s["line"]].played == [b"\x09"], "前の PC へ届く")
        until(lambda: phone2.send_audio is not None, "新しい PC の電話の部品の送る口")
        phone2.send_audio(b"\x07")                                           # 新しい PC の声は、前の PC の通話へ混ぜない
        time.sleep(0.5)
        self.assertEqual((first.agent.phones[s["line"]].calls, phone2.calls, phone2.played, heard),
                         (["+81300000000"], [], [], []))
        first.beat()
        time.sleep(0.5)
        self.assertEqual(self.hub.lines()[s["line"]], at)                   # 先につないだほうのまま（合図で入れ替わらない）
        self.assertEqual(len(notes()), 1)                                    # 知らせは1日1回


class Refusals(LinkBase):
    def test_an_unknown_or_revoked_pass_makes_the_pc_log_in_again(self):
        s = self.ready()
        bad = agents.Pass(s["tenant"], uuid.uuid4(), "知らない通行証")
        r = self.agent(bad, [s["line"]])
        until(lambda: isinstance(r.error, agent_main.NeedLogin), "知らない通行証は断る")
        got = self.pc(s)
        r = self.agent(got, [s["line"]])
        self.attached(s, got)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: agents.revoke(tx, got.pc_id))   # 画面の「この PC のログインを外す」
        r.beat()
        until(lambda: isinstance(r.error, agent_main.NeedLogin), "止めた通行証は、次の合図で断る")
        until(lambda: s["line"] not in self.hub.lines(), "その PC の回線ではかけない")

    def test_an_old_pc_program_is_refused_and_told_to_update(self):
        """PC の部品のやり取りの版がサーバーと違えば断る（その PC からはかけない）。その契約者と管理者へ知らせる。"""
        s = self.ready()
        got = self.pc(s)
        newer = types.SimpleNamespace(**{k: getattr(protocol, k) for k in dir(protocol) if not k.startswith("__")})
        newer.VERSION = protocol.VERSION + 1
        with mock.patch.object(channel, "p", newer):
            r = self.agent(got, [s["line"]])
            until(lambda: isinstance(r.error, agent_main.NeedUpdate), "版が違えば断る")
        self.assertEqual(self.hub.lines(), {})
        notes = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.all(
            "SELECT summary, refs FROM salescall.notification_events WHERE kind = 'program_update'"))
        self.assertEqual(len(notes), 1)
        self.assertIn("プログラムを更新してください", notes[0]["summary"])

    def test_bad_messages_are_refused_without_stopping_the_hub(self):
        s = self.ready()
        with connect(f"ws://127.0.0.1:{self.hub.port}/link") as ws:
            ws.send(json.dumps({"t": "beat"}))                                  # 最初は hello だけ
            with self.assertRaises(Exception):
                ws.recv(timeout=5)
        got = self.pc(s)
        self.agent(got, [s["line"]])
        self.attached(s, got)                                                    # つなぎ口は動き続ける


class LostConnection(LinkBase):
    def test_a_call_in_progress_is_apologised_for_and_hung_up_when_the_server_goes_away(self):
        """つながりが通話の途中で切れたら、電話の部品が、おわびの録音を流して切る（お客様を無言で放置しない）。サーバーが待っていた
        答えは「分からない」（通話中＝かけない側）。"""
        s = self.ready()
        got = self.pc(s)
        phone = FakePhone()
        self.agent(got, [s["line"]], {s["line"]: phone})
        self.attached(s, got)
        phone.busy = True
        self.hub.close()
        until(lambda: phone.apologies >= 1, "おわびを流して切る")
        self.assertTrue(self.hub.driver(s["line"], Attached(s["tenant"], got.pc_id, 1)).in_call())

    def test_a_call_that_finishes_after_the_line_drops_is_apologised_for(self):
        """「かける」の途中でつながりが切れ、そのあとで電話の部品がかけ終わったら、すぐおわびして切る（AI のいない電話にしない）。"""
        s = self.ready()
        got = self.pc(s)
        release = threading.Event()

        class SlowPhone(FakePhone):
            def start_call(self, phone):
                release.wait(10)
                super().start_call(phone)
        phone = SlowPhone()
        self.agent(got, [s["line"]], {s["line"]: phone})
        driver = self.hub.driver(s["line"], self.attached(s, got))
        caller = threading.Thread(target=lambda: self.assertRaises(Exception, driver.start_call, "+81300000000"))
        caller.start()
        until(lambda: self.hub.sessions[got.pc_id].pending, "かけてと頼んだ")
        self.hub.close()                                                     # かけている途中で切れた
        time.sleep(0.5)
        release.set()
        until(lambda: phone.calls and phone.apologies >= 1, "かけ終わったら、すぐおわびして切る")
        caller.join(30)

    def test_a_quick_reconnect_of_the_same_pc_is_a_new_connection(self):
        """同じ PC がすぐつなぎ直しても、新しいつなぎ（番号が違う）として見せる（中央の見守り役が回線の流れを立ち上げ直し、前の通話の
        頼み・声を新しいつなぎへ送らない）。"""
        s = self.ready()
        got = self.pc(s)
        r = self.agent(got, [s["line"]])
        before = self.attached(s, got)
        asyncio.run_coroutine_threadsafe(r.agent.ws.close(), r.loop)          # 一度切れて、すぐつなぎ直す
        after = until(lambda: (at := self.hub.lines().get(s["line"])) is not None and at.conn != before.conn and at, "つなぎ直した")
        self.assertEqual((after.tenant_id, after.pc_id), (before.tenant_id, before.pc_id))
        self.assertTrue(self.hub.driver(s["line"], before).in_call())        # 前のつなぎには頼めない（かけない側）


class Emergency(LinkBase):
    def test_a_stop_pressed_on_the_pc_is_recorded_and_the_pc_withholds_calls(self):
        """PC の上の緊急停止（ネットが切れていても押せる）：つないだら、その契約者の緊急停止として記録し、番号を PC の印に書く。
        印があるあいだ、その PC の回線は中央の見守り役に見せず、頼まれても PC がかけない。"""
        s = self.ready()
        got = self.pc(s)
        pc_emergency.stop("試験：ネットが切れているあいだに押した")              # つなぐ前に押した
        phone = FakePhone()
        self.agent(got, [s["line"]], {s["line"]: phone})
        flag = until(lambda: (pc_emergency.engaged() or {}).get("event"), "記録の番号が印に書かれる")
        self.assertEqual(self.open_manual(s), [flag])
        self.assertEqual(self.hub.lines(), {})                                  # かけない
        session = self.hub.sessions[got.pc_id]
        driver = self.hub.driver(s["line"], Attached(s["tenant"], got.pc_id, session.conn))
        self.assertTrue(driver.in_call())
        with self.assertRaises(CallWithheld):
            driver.start_call("+81300000000")
        self.assertEqual(phone.calls, [])
        claim = self.claim(s)                                                    # 門も止める（記録した停止）
        self.assertEqual(getattr(claim, "reason", None), "stopped")

    def test_a_release_on_the_screen_clears_the_flag_and_a_release_on_the_pc_is_recorded(self):
        s = self.ready()
        got = self.pc(s)
        r = self.agent(got, [s["line"]])
        self.attached(s, got)
        pc_emergency.stop("試験：押した")
        event = until(lambda: (pc_emergency.engaged() or {}).get("event"), "記録の番号")
        until(lambda: self.hub.lines() == {}, "押したら、その PC の回線ではかけない")
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: stops.resume(tx, event, reason="試験：画面から解いた"))
        r.beat()
        until(lambda: pc_emergency.engaged() is None, "画面から解かれたら印を消す")
        self.attached(s, got)                                                    # またかける
        pc_emergency.stop("試験：また押した")
        until(lambda: (pc_emergency.engaged() or {}).get("event"), "記録の番号")
        self.assertTrue(pc_emergency.ask_release("試験：直った"))
        until(lambda: pc_emergency.engaged() is None, "PC で解いたら、サーバーが解いてから印を消す")
        self.assertEqual(self.open_manual(s), [])
        self.attached(s, got)

    def test_a_stop_that_could_not_be_recorded_is_recorded_on_the_next_beat(self):
        """記録に一度失敗しても（データベースに届かない など）、次の合図でまた記録する（前は、そのつなぎのあいだ二度と記録しなかった）。"""
        s = self.ready()
        got = self.pc(s)
        r = self.agent(got, [s["line"]])
        self.attached(s, got)
        real, calls = channel.emergency.stop, []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("試験：データベースに届かない")
            return real(*a, **k)
        with mock.patch.object(channel.emergency, "stop", flaky), mock.patch.object(channel, "report"):
            pc_emergency.stop("試験：押した")
            until(lambda: calls, "1回目の記録（失敗）")
            r.beat()
            event = until(lambda: (pc_emergency.engaged() or {}).get("event"), "次の合図で記録")
        self.assertEqual(self.open_manual(s), [event])

    def test_a_release_asked_while_the_server_is_away_keeps_the_flag(self):
        """解いてほしいと書いても、サーバーに届かないうちは印を消さない（解いたことを記録できないまま、かけ始めない）。"""
        pc_emergency.stop("試験：押した")
        self.assertTrue(pc_emergency.ask_release("試験：直った"))
        self.assertEqual(pc_emergency.engaged()["release"], "試験：直った")
        self.assertFalse(pc_emergency.ask_release("試験：2回目") and pc_emergency.engaged() is None)
        self.assertFalse(pc_emergency.clear(5))                                  # 番号の違う停止が解けても消さない
        self.assertTrue(pc_emergency.clear(None))                                # 記録の前に、PC で解いてほしいと書いた押し
        pc_emergency.stop("試験：押した")
        self.assertFalse(pc_emergency.clear(None))                               # 解いてほしいと書いていない押しは消さない
        self.assertTrue(pc_emergency.recorded(7, pc_emergency.engaged()["at"]))
        self.assertTrue(pc_emergency.ask_release("試験：直った"))
        pc_emergency.stop("試験：やっぱり止める")                                # 届く前に押し直した：新しい押し
        self.assertEqual((pc_emergency.engaged()["event"], pc_emergency.engaged()["release"]), (None, None))
        self.assertFalse(pc_emergency.clear(7))                                  # 前の停止が解けても、押し直した印は消えない
        self.assertFalse(pc_emergency.recorded(8, "前の押しの時刻"))            # 前の押しの番号は、新しい押しに書かない
        (self.dir / "EMERGENCY_STOP").write_text("壊れた中身", encoding="utf-8")
        self.assertIsNotNone(pc_emergency.engaged())                             # 読めない印は、効いているとみなす
        self.assertFalse(pc_emergency.clear(None))
        (self.dir / "EMERGENCY_STOP").unlink()
        self.assertIsNone(pc_emergency.engaged())
        self.assertFalse(pc_emergency.ask_release("試験：止めていない"))
        with self.assertRaises(ValueError):
            pc_emergency.stop(" ")


class Backups(LinkBase):
    def folder(self):
        return ["架空の依頼者", "AI"]

    def test_the_server_sends_a_backup_and_the_pc_seals_writes_and_checks_it(self):
        """控えの中身はサーバーが書き出し、PC の部品が暗号化して控え用のハードディスクに書き、読み戻して確かめる（大きな中身は
        分けて送る）。中身が欠けたら書かない。"""
        s = self.ready()
        got = self.pc(s)
        drive = drive_at(self.dir / "drive")
        self.agent(got, [s["line"]], drive=drive)
        writer = until(lambda: (w := self.hub.writer(s["tenant"])) and w.session.drive and w.session.key and w, "書ける PC")
        data = os.urandom(protocol.CHUNK * 2 + 123)
        name = "salescall-20991231-120000-test.enc"
        sha, size = writer.write(self.folder(), name, data, hashlib.sha256(data).hexdigest())
        path = drive / pc_backup.BASE / "架空の依頼者" / "AI" / name
        self.assertEqual((path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()), (size, sha))
        self.assertEqual(sealing.decrypt(path.read_bytes(), sealing.load_key({sealing.KEY_ENV: KEY}), name), data)
        with self.assertRaises(RemoteError):
            writer.write(self.folder(), "salescall-20261021-120001-x.enc", data, "0" * 64)
        with self.assertRaises(RemoteError):
            writer.write(["..", "AI"], "salescall-20261021-120002-x.enc", b"x", hashlib.sha256(b"x").hexdigest())
        old = "salescall-20200101-000000-old.enc"
        writer.write(self.folder(), old, b"old", hashlib.sha256(b"old").hexdigest())
        self.assertEqual(writer.prune({"架空の依頼者": {"days": {"AI": 30}, "verified": [], "held": []}}), 1)
        self.assertEqual(sorted(f.name for f in path.parent.iterdir()), [name])   # 残す日数を過ぎたものだけ消す

    def test_a_pc_without_the_drive_says_what_to_do_and_the_whole_backup_goes_only_to_the_set_pc(self):
        s = self.ready()
        got = self.pc(s)
        self.agent(got, [s["line"]])
        self.attached(s, got)
        writer = self.hub.writer(s["tenant"])
        with self.assertRaises(RemoteError) as e:
            writer.write(self.folder(), "salescall-20261021-120000-x.enc", b"x", hashlib.sha256(b"x").hexdigest())
        self.assertIn("SALESCALL_BACKUP_DRIVE", e.exception.ask or "")
        self.assertIsNone(self.hub.whole_writer())                             # 全体の控えを書く PC を決めていない
        self.assertIsNone(self.hub.writer(self.w.tenant()))                    # つないでいない契約者
        t = self.ready()
        stranger = self.pc(t, "架空のほかの契約者の PC")
        admin = self.start_hub(SALESCALL_WHOLE_BACKUP_TENANT=str(s["tenant"]))
        self.agent(stranger, [], hub=admin)
        until(lambda: stranger.pc_id in admin.sessions, "ほかの契約者の PC")
        self.assertIsNone(admin.whole_writer())                                # 決めた契約者の PC がつないでいない：ほかの契約者には送らない
        self.agent(got, [s["line"]], hub=admin)
        until(lambda: admin.whole_writer() is not None, "決めた契約者の PC にだけ全体の控えを頼む")
        self.assertEqual(admin.whole_writer().session.who.tenant_id, s["tenant"])


class Settings(LinkBase):
    def test_the_hub_url_must_be_encrypted_except_on_the_same_machine(self):
        with self.assertRaises(agent_config.ConfigMissing):
            agent_config.load({"SALESCALL_HUB_URL": "ws://salescall.example.invalid/link"})
        with self.assertRaises(agent_config.ConfigMissing):
            agent_config.load({"SALESCALL_SERVER_URL": "http://salescall.example.invalid"})
        cfg = agent_config.load({"SALESCALL_SERVER_URL": "https://salescall.example.invalid/"})
        self.assertEqual(cfg.hub_url, "wss://salescall.example.invalid/link")
        with self.assertRaises(agent_config.ConfigMissing):
            agent_config.load({"SALESCALL_SERVER_URL": "https://x.invalid", "SALESCALL_LINES": "回線ではない"})
        lines = agent_config.status({"SALESCALL_BACKUP_KEY": KEY, "GEMINI_KEY_1": "架空"})
        self.assertNotIn(KEY, "\n".join(lines))
        self.assertNotIn("GEMINI_KEY_1", agent_config.gather({"GEMINI_KEY_1": "架空"}))

