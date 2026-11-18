"""PC ごとの通行証と、PC が動いているかの見張りの試験（DESIGN.md 5-5。core/agents.py・表の作り 0105）。"""

from __future__ import annotations

import unittest
import uuid
from unittest import mock

from salescall.core import agents, settings
from salescall.core.access import Rejected
from tests.support import ADMIN, OPERATOR, SYSTEM, World, app_access, fictional_caller, kindless, owner_access
from salescall.core import registry


class Logins(unittest.TestCase):
    """PC のログインの待ちは、データベースの1か所（表の作り 0241。DESIGN.md 5-5・15-2 の867）。画面のサーバーが2台以上でも、どのサーバー
    からも同じ待ちが見え、払い出しは1回だけ（✅ 2026-11-18 ユーザー「契約者が増えたら、2台以上は確実なんじゃないの？」）。"""

    def setUp(self):
        self.w = World()
        self.a, self.b = app_access(), app_access()   # 画面のサーバー2台
        self.o = owner_access()

    def age(self, start, **secs):
        """待ちの時刻を、決めた秒だけ前へずらす（時間がたったことにする）。"""
        sets = ", ".join(f"{col} = {col} - make_interval(secs => {n})" for col, n in secs.items())
        self.o.in_system(SYSTEM, lambda tx: tx.execute(f"UPDATE salescall.pc_logins SET {sets} WHERE device_sha = %s",
                                                       (agents.fingerprint(start.device_code),)))

    def who(self, token):
        return self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, token))

    def test_a_login_started_on_one_server_is_approved_and_taken_on_another_once(self):
        """始めたサーバー・許可したページのサーバー・受け取りを聞くサーバーが違っても、ログインできる。待ちの印は指紋だけを置く。
        許可した契約者の PC になり、前の PC（その契約者の PC）は止まる。生きている通行証は、いつも1つ（払い出し直したら前のものは止まる）。"""
        t = self.w.tenant()
        old = agents.issue(self.a, SYSTEM, t, label="前の PC")
        s = agents.login_start(self.a, label="  事務所の PC  ", replaces=str(old.pc_id))
        self.assertRegex(s.user_code, r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$")
        self.assertEqual((s.interval, s.expires_in), (agents.LOGIN_EVERY, agents.LOGIN_EXPIRES))
        kept = self.o.in_system(SYSTEM, lambda tx: tx.all("SELECT device_sha FROM salescall.pc_logins"))
        self.assertEqual(kept, [{"device_sha": agents.fingerprint(s.device_code)}])     # PC の待ちの印そのものは置かない
        self.assertEqual(agents.login_take(self.b, s.device_code), (agents.PENDING, None))
        typed = s.user_code.lower().replace("-", " ")                                    # 小文字・区切りの違いは吸う
        seen = self.b.in_tenant(OPERATOR, t, lambda tx: agents.login_waiting(tx, typed))
        self.assertEqual((seen.user_code, seen.pc_label, seen.replaces_label), (s.user_code, "事務所の PC", "前の PC"))
        self.assertEqual(self.b.in_tenant(OPERATOR, t, lambda tx: agents.login_decide(tx, typed, approve=True)), "事務所の PC")
        self.assertIsNone(self.b.in_tenant(OPERATOR, t, lambda tx: agents.login_waiting(tx, s.user_code)))   # 決めたら出さない
        self.assertIsNone(self.b.in_tenant(OPERATOR, t, lambda tx: agents.login_decide(tx, s.user_code, approve=False)))
        state, got = agents.login_take(self.a, s.device_code)
        self.assertEqual((state, got.tenant_id), ("ok", t))
        self.assertEqual(self.who(got.token), agents.Who(t, got.pc_id, "事務所の PC"))
        self.assertIsNone(self.who(old.token))                                           # 前の PC は止まる
        issued = self.a.in_tenant(OPERATOR, t, lambda tx: tx.one("SELECT issued_by FROM salescall.pc_agents WHERE pc_id = %s",
                                                                 (got.pc_id,)))["issued_by"]
        self.assertIn(OPERATOR.name, issued)                                             # 誰が許可したかが残る
        # 払い出した答えが PC に届かず、同じ待ちの印でまた聞かれた ⇒ 前に払い出した PC を止めて払い出し直す（生きている通行証は、いつも1つ。0243）
        state, again = agents.login_take(self.b, s.device_code)
        self.assertEqual(state, "ok")
        self.assertNotEqual(again.pc_id, got.pc_id)
        self.assertIsNone(self.who(got.token))
        self.assertEqual(self.who(again.token), agents.Who(t, again.pc_id, "事務所の PC"))
        live = self.a.in_tenant(OPERATOR, t, lambda tx: [p["pc_id"] for p in agents.pcs(tx) if p["revoked_at"] is None])
        self.assertEqual(live, [again.pc_id])

    def test_asking_too_fast_slows_down_and_the_wait_ends(self):
        """間隔より早く聞けば「遅く」と答え、その待ちの間隔を5秒延ばす（RFC 8628 3.5）。断られたら1回だけ「断られた」。期限が切れたら、
        ページにも出さず許可もできず、受け取りは「時間切れ」。期限の切れた待ちは、次に始めるときに消える。"""
        t = self.w.tenant()
        s = agents.login_start(self.a, label="")
        take = lambda: agents.login_take(self.b, s.device_code)[0]   # noqa: E731
        self.assertEqual(take(), agents.PENDING)
        self.assertEqual(take(), agents.SLOW_DOWN)                                       # 間隔は 5→10 秒
        self.age(s, polled_at=6)
        self.assertEqual(take(), agents.SLOW_DOWN)                                       # 10 秒より早い（間隔は 15 秒）
        self.age(s, polled_at=16)
        self.assertEqual(take(), agents.PENDING)
        self.assertEqual(self.a.in_tenant(OPERATOR, t, lambda tx: agents.login_decide(tx, s.user_code, approve=False)), "名前の無い PC")
        self.assertEqual((take(), take()), (agents.DENIED, agents.EXPIRED))
        late = agents.login_start(self.a, label="遅い PC")
        self.age(late, started_at=agents.LOGIN_EXPIRES + 1, expires_at=agents.LOGIN_EXPIRES + 1)
        self.assertIsNone(self.a.in_tenant(OPERATOR, t, lambda tx: agents.login_waiting(tx, late.user_code)))
        self.assertIsNone(self.a.in_tenant(OPERATOR, t, lambda tx: agents.login_decide(tx, late.user_code, approve=True)))
        self.assertEqual(agents.login_take(self.a, late.device_code), (agents.EXPIRED, None))
        approved = agents.login_start(self.a, label="許可のあと遅い PC")
        self.a.in_tenant(OPERATOR, t, lambda tx: agents.login_decide(tx, approved.user_code, approve=True))
        self.age(approved, started_at=agents.LOGIN_EXPIRES + 1, expires_at=agents.LOGIN_EXPIRES + 1)
        took = self.a.in_tenant(SYSTEM, t, lambda tx: tx.all("SELECT * FROM salescall.pc_login_take(%s)",
                                                              (agents.fingerprint(approved.device_code),)))
        self.assertEqual(took, [])                                                       # 期限の切れた許可は受け取れない
        agents.login_start(self.a, label="次の PC")
        left = self.o.in_system(SYSTEM, lambda tx: tx.all("SELECT pc_label FROM salescall.pc_logins ORDER BY pc_label"))
        self.assertNotIn({"pc_label": "許可のあと遅い PC"}, left)                          # 期限の切れた待ちは消える
        self.assertEqual(agents.login_take(self.a, approved.device_code), (agents.EXPIRED, None))
        self.assertEqual(agents.login_take(self.a, ""), (agents.EXPIRED, None))

    def test_who_may_see_approve_and_take_a_login(self):
        """始める・聞くのはサーバーの全体の取引だけ。ページに出す・許可するのは、ログインした管理者か契約者だけ。受け取るのは許可した
        契約者の取引のシステムの処理だけ。表はどの役も直接は読み書きしない。ほかの契約者の PC は、入れ替える PC として名前を見せず、止めない。"""
        t, other = self.w.tenant(), self.w.tenant()
        theirs = agents.issue(self.a, SYSTEM, other, label="ほかの契約者の PC")
        s = agents.login_start(self.a, label="PC", replaces=theirs.pc_id)
        sha = agents.fingerprint(s.device_code)
        for actor, call in ((SYSTEM, f"SELECT * FROM salescall.pc_login_waiting('{s.user_code}')"),
                            (SYSTEM, f"SELECT salescall.pc_login_decide('{s.user_code}', true)"),
                            (kindless(), f"SELECT salescall.pc_login_decide('{s.user_code}', true)"),
                            (OPERATOR, f"SELECT salescall.pc_login_start(repeat('1', 64), 'BBBB-BBBB', 'x', NULL, 600, 5)"),
                            (OPERATOR, f"SELECT * FROM salescall.pc_login_poll('{sha}')"),
                            (OPERATOR, f"SELECT * FROM salescall.pc_login_take('{sha}')"),
                            (ADMIN, f"SELECT * FROM salescall.pc_login_take('{sha}')"),
                            (OPERATOR, "SELECT * FROM salescall.pc_logins"),
                            (OPERATOR, "DELETE FROM salescall.pc_logins")):
            with self.subTest(actor=actor.kind, call=call), self.assertRaises(Rejected):
                self.a.in_tenant(actor, t, lambda tx, call=call: tx.all(call))
        for call in (f"SELECT * FROM salescall.pc_login_waiting('{s.user_code}')", "SELECT * FROM salescall.pc_logins",
                     f"SELECT * FROM salescall.pc_login_take('{sha}')"):
            with self.subTest(by="system", call=call), self.assertRaises(Rejected):
                self.a.in_system(SYSTEM, lambda tx, call=call: tx.all(call))
        with self.assertRaises(Rejected):
            self.a.in_system(ADMIN, lambda tx: tx.all(f"SELECT * FROM salescall.pc_login_poll('{sha}')"))
        seen = self.a.in_tenant(ADMIN, t, lambda tx: agents.login_waiting(tx, s.user_code))
        self.assertIsNone(seen.replaces_label)                                           # ほかの契約者の PC の名前は見せない
        self.a.in_tenant(ADMIN, t, lambda tx: agents.login_decide(tx, s.user_code, approve=True))
        self.assertEqual(self.a.in_tenant(SYSTEM, other, lambda tx: tx.all(f"SELECT * FROM salescall.pc_login_take('{sha}')")), [])
        state, got = agents.login_take(self.a, s.device_code)                            # 許可した契約者の PC になる
        self.assertEqual((state, got.tenant_id), ("ok", t))
        self.assertIsNotNone(self.who(theirs.token))                                     # ほかの契約者の PC は止めない


class Passes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def test_a_pass_is_given_once_kept_only_as_a_fingerprint_and_identifies_the_pc(self):
        """通行証は1回だけ返し、データベースには指紋だけ（2026-10-21 ユーザー：読み取られないように）。サーバーは通行証から契約者と
        PC を決める。止めた通行証・知らない通行証は通らない。"""
        t = self.w.tenant()
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        self.assertNotIn(p.token, repr(p))
        rows = self.a.in_tenant(OPERATOR, t, lambda tx: tx.all("SELECT token_sha256 FROM salescall.pc_agents"))
        self.assertEqual(rows, [{"token_sha256": agents.fingerprint(p.token)}])
        who = lambda tok: self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, tok))   # noqa: E731
        self.assertEqual(who(p.token), agents.Who(t, p.pc_id, "架空の PC"))
        self.assertIsNone(who("知らない通行証"))
        self.assertTrue(self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id)))   # 契約者が「この PC のログインを外す」
        self.assertIsNone(who(p.token))
        self.assertFalse(self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id)))  # 止めた印は1回だけ

    def test_in_the_interim_form_the_server_gives_this_pc_a_pass_by_itself(self):
        """移る間の形（つなぎ口がこの機械の中だけ＝管理者の PC で中央の見守り役と PC の部品を両方動かす）：見守り役が、この機械の
        PC の部品の通行証を自分で払い出してしまう（管理者は命令を打たない＝2026-10-22 ユーザー）。止められた・消えた通行証は
        入れ直す（前の通行証は止める）。サーバーへ移ったあと（ほかの機械からつなぐ形）は何もしない。PC の部品は、ログインの入口が
        無いあいだ、しまわれるのを待つ。"""
        import tempfile
        from pathlib import Path
        from salescall.agent import pc_login
        from salescall.link import pass_file
        from salescall.supervisor import config as sup_config, local_pc
        t = self.w.tenant()
        place = Path(tempfile.mkdtemp(prefix="salescall-pass-")) / "pc-pass.bin"
        who = lambda tok: self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, tok))   # noqa: E731
        base = {"SALESCALL_DSN": "x", "SALESCALL_ADMIN_TENANT": str(t)}
        server = sup_config.load({**base, "SALESCALL_LISTEN": "0.0.0.0:8765"})
        here = sup_config.load({**base, "SALESCALL_LISTEN": "127.0.0.1:8765"})
        with mock.patch.object(pass_file, "pass_path", return_value=place):
            self.assertFalse(local_pc.ensure(self.a, server))                   # サーバーへ移ったあと：何もしない
            self.assertIsNone(pass_file.load())
            self.assertTrue(local_pc.ensure(self.a, here))
            first = pass_file.load()
            self.assertEqual((who(first.token).tenant_id, who(first.token).pc_id), (t, first.pc_id))   # 管理者自身の契約者の PC
            self.assertFalse(local_pc.ensure(self.a, here))                     # 使える通行証があれば、そのまま
            self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, first.pc_id))
            self.assertTrue(local_pc.ensure(self.a, here))                      # 止められたら入れ直す
            self.assertNotEqual(pass_file.load().pc_id, first.pc_id)
            pass_file.logout()
            self.assertTrue(local_pc.ensure(self.a, here))                      # 消えたら入れ直す
            again = pass_file.load()
            # PC の部品：ログインの入口が無いあいだは、しまわれた通行証を受け取る（ブラウザは開かない）
            opened = []
            got = pc_login.until_logged_in("", open_browser=opened.append, sleep=lambda _: None)
            self.assertEqual((got, opened), (again, []))

    def test_logging_in_again_on_the_same_pc_stops_the_old_pass(self):
        t = self.w.tenant()
        old = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        new = agents.issue(self.a, SYSTEM, t, label="架空の PC", replaces=old.pc_id)
        who = lambda tok: self.a.in_system(SYSTEM, lambda tx: agents.identify(tx, tok))   # noqa: E731
        self.assertIsNone(who(old.token))
        self.assertEqual(who(new.token), agents.Who(t, new.pc_id, "架空の PC"))

    def test_who_may_issue_stop_and_change(self):
        """払い出すのはシステムの処理か管理者だけ。止めるのは管理者・契約者・システムの処理。通行証・合図は書き換えられない。消せない。
        ほかの契約者の PC は見えない・止められない。サーバーの関数は全体の取引のシステムの処理だけ。"""
        t, other = self.w.tenant(), self.w.tenant()
        with self.assertRaises(Rejected):
            agents.issue(self.a, OPERATOR, t, label="契約者が自分で払い出す")
        p = agents.issue(self.a, ADMIN, t, label="架空の PC")
        for sql in ("UPDATE salescall.pc_agents SET token_sha256 = repeat('0', 64)",
                    "UPDATE salescall.pc_agents SET last_seen_at = now()",
                    "DELETE FROM salescall.pc_agents"):
            with self.subTest(sql=sql), self.assertRaises(Rejected):
                self.a.in_tenant(OPERATOR, t, lambda tx, sql=sql: tx.execute(sql))
        with self.assertRaises(LookupError):
            self.a.in_tenant(OPERATOR, other, lambda tx: agents.revoke(tx, p.pc_id))
        self.assertEqual(self.a.in_tenant(OPERATOR, other, agents.pcs), [])
        for call in ("SELECT * FROM salescall.agent_by_token(repeat('0', 64))", "SELECT salescall.all_tenants()",
                     "SELECT salescall.agent_beat(gen_random_uuid(), '{}')"):
            with self.subTest(call=call), self.assertRaises(Rejected):
                self.a.in_tenant(SYSTEM, t, lambda tx, call=call: tx.all(call))
            with self.subTest(call=call, by="admin"), self.assertRaises(Rejected):
                self.a.in_system(ADMIN, lambda tx, call=call: tx.all(call))

    def test_a_transaction_without_a_kind_cannot_issue_or_stop_a_pass(self):
        """誰が、の種類の無い取引は、払い出せない・止められない（0205。⛔ 前は NOT IN で比べたので、種類が無いと断らなかった）。"""
        t = self.w.tenant()
        with self.assertRaises(Rejected):
            agents.issue(self.a, kindless(), t, label="種類の無い取引が払い出す")
        p = agents.issue(self.a, ADMIN, t, label="架空の PC")
        with self.assertRaises(Rejected):
            self.a.in_tenant(kindless(), t, lambda tx: agents.revoke(tx, p.pc_id))
        self.assertTrue(self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id)))

    def test_a_beat_keeps_only_the_tenants_lines_in_use(self):
        """合図で伝えた回線は、その契約者の、使っている回線だけが残る（ほかの契約者の回線を名乗って、その電話をかけさせない）。"""
        t, other = self.w.tenant(), self.w.tenant()
        mine = self.a.in_tenant(OPERATOR, t, lambda tx: registry.create_line(tx, "架空の回線", method="rakuten_link",
                                                                              caller=fictional_caller()))
        theirs = self.a.in_tenant(OPERATOR, other, lambda tx: registry.create_line(tx, "架空の回線", method="rakuten_link",
                                                                                    caller=fictional_caller()))
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        kept = self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, [mine, theirs, uuid.uuid4()]))
        self.assertEqual(kept, [mine])
        self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id))
        self.assertIsNone(self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, [mine])))


class Down(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.a = app_access()

    def notices(self, t):
        return self.a.in_tenant(OPERATOR, t, lambda tx: tx.all(
            "SELECT event_key FROM salescall.notification_events WHERE kind = 'pc_down'"))

    def test_a_pc_that_goes_quiet_is_noticed_once_per_outage(self):
        """合図が途切れた PC は、その契約者と管理者へ知らせる（1回の途切れに1回。2026-10-21 ユーザー：「その PC が止まっていると、
        控えが取れていないことに誰も気づきません」への答え）。合図が戻って、また途切れたら、また知らせる。止めた PC は知らせない。"""
        t = self.w.tenant()
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        watch = lambda: self.a.in_tenant(SYSTEM, t, agents.watch_down)   # noqa: E731
        self.assertEqual(watch(), [])                                     # 払い出したばかり
        with mock.patch.object(settings, "system_value", side_effect=lambda tx, key: 0 if key == "pc.down_after" else None):
            self.assertEqual(watch(), [p.pc_id])
            self.assertEqual(watch(), [])                                 # 同じ途切れは1回だけ
            self.a.in_system(SYSTEM, lambda tx: agents.beat(tx, p.pc_id, []))
            self.assertEqual(watch(), [p.pc_id])                          # 戻って、また途切れた
            self.assertEqual(len(self.notices(t)), 2)
            admin = self.a.in_system(ADMIN, lambda tx: tx.all(
                "SELECT origin_tenant FROM salescall.notification_events WHERE kind = 'pc_down' AND origin_tenant = %s", (t,)))
            self.assertEqual([r["origin_tenant"] for r in admin], [t, t])  # 管理者へも
            self.a.in_tenant(OPERATOR, t, lambda tx: agents.revoke(tx, p.pc_id))
            self.a.in_system(SYSTEM, lambda tx: tx.execute("SELECT pg_sleep(0.01)"))
            self.assertEqual(watch(), [])                                 # 止めた PC は知らせない


    def test_a_connected_pc_and_the_time_right_after_a_restart_are_not_reported(self):
        """いまつなぎ口につないでいる PC は、合図の記録が古くても（データベースが止まっていて書けなかった）途切れとしない。つなぎ口が
        待ち受けを始めてから途切れの長さがたつまでは、だれも途切れとしない（⛔ 2026-10-22 の見直し：前は、見守り役を立ち上げ直した
        直後に、元気な PC まで「止まった」とすべての契約者と管理者へ知らせた）。"""
        t = self.w.tenant()
        p = agents.issue(self.a, SYSTEM, t, label="架空の PC")
        watch = lambda **kw: self.a.in_tenant(SYSTEM, t, lambda tx: agents.watch_down(tx, **kw))   # noqa: E731
        with mock.patch.object(settings, "system_value", side_effect=lambda tx, key: 60 if key == "pc.down_after" else None):
            owner_access().in_system(SYSTEM, lambda tx: tx.execute(   # 払い出したのは1時間前（合図は一度も無い）
                "UPDATE salescall.pc_agents SET issued_at = now() - interval '1 hour' WHERE pc_id = %s", (p.pc_id,)))
            self.assertEqual(watch(alive=frozenset({p.pc_id})), [])          # つないでいる
            self.assertEqual(watch(listening_for=10.0), [])                  # 立ち上げ直した直後
            self.assertEqual(watch(listening_for=120.0), [p.pc_id])          # 途切れの長さがたっても、つながってこない
        self.assertEqual(len(self.notices(t)), 1)


if __name__ == "__main__":
    unittest.main()
