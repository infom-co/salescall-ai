"""2026-10-22 の見直し（0109）で足したデータベースの守りの試験。守りごとに、外したら落ちることを壊し試し（run_tests.py の
MUTATIONS）で確かめる。データは架空だけ。"""

from __future__ import annotations

import json
import uuid

from salescall.core import attempts, next_action, recordings, registry, stops
from salescall.core.access import Rejected
from tests import support
from tests.support import OPERATOR, SYSTEM, said
from tests.test_approval import TAKEN
from tests.test_quality import Base

PART = recordings.Part("closing", "失礼いたします。", "test://Leda/closing", "1" * 64)


class Guards(Base):
    def other_client(self, s):
        """同じ契約者の、別の依頼者（その取引の形）。"""
        c = self.w.client(s["tenant"])
        return dict(s, client=c)

    def attempt_in(self, s, path):
        """試行を1つ作り、path の状態まで進める（データベースの約束だけを試す）。"""
        aid = self.w.insert_claim(s)
        for state in path:
            self.w.move(s, aid, state)
        return aid

    # ── (4) お客様のところで鳴った発信の決まり（salescall.is_placed の1か所）
    def test_a_call_the_line_could_not_place_counts_nowhere(self):
        """回線の側でかけられなかった発信（call_failed）は、回線の1日の数・ほかの依頼者がかけた番号の間隔・ほかの契約者が
        かけた番号・約束の回に「かけたか」のどれにも入らない（⛔ 2026-10-22 の見直し：前は数え、ほかの依頼者がその番号へ3日
        かけられなくなり、回線の1日の上限も減った）。"""
        s = self.w.setup()
        near = self.other_client(s)
        far_tenant = self.w.tenant()
        far = dict(tenant=far_tenant, client=self.w.client(far_tenant))

        def seen():
            dials = self.tx(s, lambda tx: tx.one(
                "SELECT salescall.line_dials_on(%s, salescall.jst(now())::date) AS n", (s["line"],)))["n"]
            placed = self.tx(s, lambda tx: tx.one(
                f"SELECT count(*) AS n FROM salescall.attempts a WHERE a.task_id = %s AND {attempts.placed('a')}",
                (s["task"],)))["n"]
            elsewhere = self.tx(near, lambda tx: [r["relation"] for r in tx.all(
                "SELECT relation FROM salescall.number_dial_wait_elsewhere(%s, interval '1 day', interval '1 day')",
                (s["phone"],))])
            other = self.tx(far, lambda tx: tx.one(
                "SELECT salescall.number_dialed_by_other_tenant(%s) AS d", (s["phone"],)))["d"]
            return dials, placed, elsewhere, other

        self.attempt_in(s, ["dialing", "call_failed"])
        self.assertEqual(seen(), (0, 0, [], False))                              # 鳴っていない発信は、どこにも数えない
        self.attempt_in(s, ["dialing", "no_answer"])
        self.assertEqual(seen(), (1, 1, ["same_tenant"], True))                  # 鳴った発信は数える

    # ── (6) 録音の組の部分・承認は、取引から見える組に、組と同じ依頼者で
    def test_a_recording_set_cannot_be_added_to_or_approved_from_another_client(self):
        """依頼者Aの取引から、依頼者Bの組に部分を足せない。契約者の単位の組に、依頼者Aの名で部分を足したり承認したりできない
        （⛔ 2026-10-22 の見直し：ほかの依頼者の組は「見えない」と読まれ、同じ依頼者と判断されて、部分を足して承認できた）。"""
        s = self.w.setup()
        other = self.other_client(s)
        _, other_version = self.w.business_with_version(other["tenant"], other["client"])
        theirs = self.tx(other, lambda tx: recordings.create_set(
            tx, unit="business", voice_name="Leda", version_id=other_version, parts=[PART]), OPERATOR)
        shared = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: recordings.create_set(
            tx, unit="contractor", voice_name="Leda", parts=[PART]))
        add = ("INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'Q1', 'お世話になっております。',"
               " 'test://Leda/Q1', %s)")
        for sid in (theirs, shared):
            with self.subTest(set=sid), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(add, (sid, "2" * 64)), OPERATOR)
        with self.assertRaises(Rejected):                                         # 依頼者Aの名で、契約者の組を承認できない
            self.tx(s, lambda tx: tx.execute(
                "INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor())", (shared,)), OPERATOR)
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: recordings.approve(tx, shared))   # 契約者の取引からは承認できる
        self.tx(other, lambda tx: recordings.approve(tx, theirs), OPERATOR)

    # ── (7) 「最新が正本」の記録の順番と、承認の時刻は、データベースが決める
    def test_the_order_and_the_time_of_latest_wins_records_are_decided_by_the_database(self):
        """渡した順番の番号（大きな値）と未来の時刻は使わず、番号は最大＋1、時刻はいま（⛔ 2026-10-22 の見直し：最大の番号を
        入れると、あとの「最大＋1」が桁あふれで失敗し続け、未来の承認の時刻で、あとの承認が「いまの組」にならなかった）。"""
        s = self.w.setup()
        huge, future = 2147483647, "now() + interval '1 year'"
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.task_next_actions (tenant_id, client_id, task_id, seq, kind, call_at, cause, decided_at,"
            " promised, contact_id, purpose) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'call', now(),"
            f" '試験', {future}, false, %s, 'normal')", (s["task"], huge, s["contact"])))
        self.tx(s, lambda tx: next_action.schedule_now(tx, s["task"], cause="試験：そのあとの予定"))
        rows = self.tx(s, lambda tx: tx.all("SELECT seq, decided_at <= now() AS now_or_before FROM salescall.task_next_actions"
                                            " WHERE task_id = %s ORDER BY seq", (s["task"],)))
        self.assertEqual([(r["seq"], r["now_or_before"]) for r in rows], [(1, True), (2, True)])

        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, salescall.cur_actor(), '試験')",
            (s["run"], s["business"], huge, s["version"])), OPERATOR)
        self.assertEqual(self.tx(s, lambda tx: [r["seq"] for r in tx.all(
            "SELECT seq FROM salescall.run_versions WHERE run_id = %s ORDER BY seq", (s["run"],))]), [1, 2])

        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.client_states (tenant_id, client_id, seq, active, reason, set_by)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, false, '試験', salescall.cur_actor())", (huge,)),
            OPERATOR)
        self.tx(s, lambda tx: registry.set_client_active(tx, True, reason="試験：戻す"), OPERATOR)   # あとの「最大＋1」も通る
        self.assertEqual(self.tx(s, lambda tx: [r["seq"] for r in tx.all(
            "SELECT seq FROM salescall.client_states WHERE client_id = %s ORDER BY seq", (s["client"],))]), [1, 2])

        shared = self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: recordings.create_set(
            tx, unit="contractor", voice_name="Leda", parts=[PART]))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.execute(
            "INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by, approved_at)"
            f" VALUES (salescall.cur_tenant(), NULL, %s, salescall.cur_actor(), {future})", (shared,)))
        self.assertTrue(self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: tx.one(
            "SELECT approved_at <= now() AS ok FROM salescall.recording_approvals WHERE set_id = %s", (shared,)))["ok"])

    def test_only_readings_chosen_by_listening_get_an_order_number(self):
        """名前の読みは、録音を聞いて選んだ読みだけが番号（最大＋1）を持つ。版の中身に書いた読みは 0 のまま（2026-10-22 の
        見直し：番号をデータベースが決める形にしたとき、版の中身に書いた読みにも番号を振って、業務設定を置けなくなった）。"""
        s = self.w.setup()
        vid = support.full_version(self.w, s, content_too=False)
        raw = support.content()
        raw["names"] += [{"text": "中島", "reading": None, "source": "script", "reading_by": None,
                          "candidates": ["なかじま", "なかしま"], "likely": "なかじま"}]
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
            " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], vid, json.dumps(raw, ensure_ascii=False), OPERATOR.name)))
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, seq, reading)"
            " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, '中島', 999, 'なかしま')", (vid,)), OPERATOR)
        self.tx(s, lambda tx: recordings.choose_reading(tx, vid, "中島", "なかじま"), OPERATOR)
        got = self.tx(s, lambda tx: [(r["name_text"], r["seq"], r["reading_by"]) for r in tx.all(
            "SELECT name_text, seq, reading_by FROM salescall.version_name_readings WHERE version_id = %s"
            " ORDER BY name_text, seq", (vid,))])
        self.assertEqual([g for g in got if g[0] == "中島"], [("中島", 1, "recording"), ("中島", 2, "recording")])
        self.assertTrue(all(seq == 0 for name, seq, by in got if by != "recording"))   # 版の中身に書いた読み
        self.assertTrue(any(by != "recording" for _, _, by in got))

    # ── (8) 番号だけの参照で、ほかの依頼者・契約者の行を指せない
    def test_an_apology_approval_cannot_point_at_another_contractors_order(self):
        """お詫びの承認は、自分の依頼者の案だけ（⛔ 2026-10-22 の見直し：案の番号だけで参照していたので、契約者Aの取引から
        契約者Bの案に承認を入れ、B自身の承認を「重複」で塞げた）。承認の時刻は、渡してもいまになる。"""
        theirs = self.fresh(stage_cap=20)
        self.verified_call(theirs, said(), items=TAKEN)
        order = self.tx(theirs, lambda tx: tx.one(
            "SELECT order_id FROM salescall.apology_orders WHERE task_id = %s", (theirs["task"],)))["order_id"]
        mine = self.fresh(stage_cap=20)
        approve = ("INSERT INTO salescall.apology_approvals (tenant_id, client_id, order_id, approved_by, approved_at)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor(),"
                   " now() + interval '1 year')")
        with self.assertRaises(Rejected):
            self.tx(mine, lambda tx: tx.execute(approve, (order,)), OPERATOR)
        self.tx(theirs, lambda tx: tx.execute(approve, (order,)), OPERATOR)    # 自分の案は承認できる（塞がれていない）
        self.assertTrue(self.tx(theirs, lambda tx: tx.one(
            "SELECT approved_at <= now() AS ok FROM salescall.apology_approvals WHERE order_id = %s", (order,)))["ok"])

    def test_a_stop_or_a_price_of_another_contractor_cannot_be_pointed_at(self):
        """品質の評価の停止・利用量の単価は、その取引から見える行だけを指せる（⛔ 2026-10-22 の見直し：番号だけの
        参照で、ほかの契約者の停止・単価を指せた）。"""
        mine, theirs = self.w.setup(), self.w.setup()
        stop = self.tx(theirs, lambda tx: stops.stop(tx, scope="client", cause="manual", reason="試験：ほかの契約者の停止",
                                                     tenant_id=theirs["tenant"], client_id=theirs["client"]),
                       OPERATOR)["event_id"]
        price = "INSERT INTO salescall.price_versions (tenant_id, source, service, tier, unit, yen_per_unit, valid_from, note, set_by)" \
                " VALUES (salescall.cur_tenant(), 'ai', 'test-model', 'paid', 'input_tokens', 0.001, now(), '試験', salescall.cur_actor())" \
                " RETURNING price_id"
        their_price = self.w.a.in_tenant(OPERATOR, theirs["tenant"], lambda tx: tx.one(price))["price_id"]
        my_price = self.w.a.in_tenant(OPERATOR, mine["tenant"], lambda tx: tx.one(price))["price_id"]
        use = ("INSERT INTO salescall.usage_entries (tenant_id, client_id, source, kind, quantity, unit, price_id, amount_yen,"
               " service, tier) VALUES (salescall.cur_tenant(), salescall.cur_client(), 'ai', 'measured', 10, 'input_tokens',"
               " %s, 0.01, 'test-model', 'paid')")
        cases = {
            "品質の評価の停止": lambda tx: tx.execute(
                "INSERT INTO salescall.quality_evaluations (tenant_id, client_id, version_id, stage_no, cap, used, metrics,"
                " outcome, stop_event_id) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 1, 20, 0, '{}', 'stop', %s)",
                (mine["version"], stop)),
            "利用量の単価": lambda tx: tx.execute(use, (their_price,)),
        }
        for what, work in cases.items():
            with self.subTest(what), self.assertRaises(Rejected):
                self.tx(mine, work)
        self.tx(mine, lambda tx: tx.execute(use, (my_price,)))                  # 自分の契約者の単価は指せる

    # ── 0108 の答えの守り：どのお客様のどの質問の答えかは変えない
    def test_an_answer_cannot_be_moved_to_another_customer(self):
        """答えの行の、お客様の行（タスク）・質問は書き換えられない（⛔ 2026-10-22 の見直し：中身が変わらなければ通す早道で、
        答えを別のお客様の行へ付け替えられた）。"""
        s = self.w.setup()
        other_task = self.w.task(s["tenant"], s["client"], s["run"],
                                 self.w.contact(s["tenant"], s["client"], support.fictional_phone()))
        aid = self.w.insert_claim(s)
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition,"
            " attempt_id) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'Q1', 'confirmed', 'はい', %s)",
            (s["task"], s["version"], aid)))
        with self.assertRaises(Rejected):
            self.tx(s, lambda tx: tx.execute("UPDATE salescall.item_values SET task_id = %s WHERE task_id = %s",
                                             (other_task, s["task"])))
        self.assertEqual(self.tx(s, lambda tx: tx.one(
            "SELECT count(*) AS n FROM salescall.item_values WHERE task_id = %s", (s["task"],)))["n"], 1)

    # ── (9) 仕掛けだけが書く記録には、サーバーの役が直接書けない（仕掛けからは書ける）
    def test_records_written_only_by_the_database_cannot_be_written_directly(self):
        """状態の移り変わり・前の答え・名前の変更の記録は、仕掛け（持ち主の権限で動く）だけが書く（⛔ 2026-10-22 の見直し：
        サーバーの役が直接足せ、予約のままの試行に「つながった」を足すと「お客様が出た」になり、取っていない値を「前の答え」に
        足すと、確かめ直しが「確かめた答え」として戻した）。"""
        s = self.w.setup()
        first = self.attempt_in(s, ["dialing", "no_answer"])
        self.tx(s, lambda tx: tx.execute(
            "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, condition,"
            " attempt_id) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, 'Q1', 'confirmed', 'はい', %s)",
            (s["task"], s["version"], first)))
        second = self.w.insert_claim(s)
        self.tx(s, lambda tx: tx.execute(                                         # 後の通話が上書き ⇒ 前の答えを仕掛けが残す
            "UPDATE salescall.item_values SET attempt_id = %s, condition = 'いいえ' WHERE task_id = %s", (second, s["task"])))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.rename_client(tx, s["client"], "架空の依頼者（新）"))
        self.w.a.in_tenant(OPERATOR, s["tenant"], lambda tx: registry.rename_line(tx, s["line"], "架空の回線（新）"))
        self.assertEqual(self.tx(s, lambda tx: tx.one(
            "SELECT (SELECT count(*) FROM salescall.attempt_state_history WHERE attempt_id = %s) AS moves,"
            "       (SELECT count(*) FROM salescall.item_value_history WHERE task_id = %s) AS answers,"
            "       (SELECT count(*) FROM salescall.name_changes WHERE target_id IN (%s, %s)) AS names",
            (first, s["task"], s["client"], s["line"]))), {"moves": 3, "answers": 1, "names": 2})
        direct = {
            "状態の移り変わり": ("INSERT INTO salescall.attempt_state_history (tenant_id, client_id, attempt_id, from_state, to_state)"
                         " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'claimed', 'answered')", (second,)),
            "前の答え": ("INSERT INTO salescall.item_value_history (tenant_id, client_id, task_id, question_id, attempt_id,"
                     " version_id, state, value) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, 'Q1', %s, %s,"
                     " 'confirmed', '\"2026-11-01T10:00\"')", (s["task"], second, s["version"])),
            "名前の変更": ("INSERT INTO salescall.name_changes (tenant_id, client_id, kind, target_id, old_name, new_name, changed_by)"
                      " VALUES (salescall.cur_tenant(), salescall.cur_client(), 'client', %s, '前', '後', '試験')", (uuid.uuid4(),)),
        }
        for what, (sql, args) in direct.items():
            with self.subTest(what), self.assertRaises(Rejected):
                self.tx(s, lambda tx: tx.execute(sql, args))
