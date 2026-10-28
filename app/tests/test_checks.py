"""機械の見張り（app/checks）の試験：わざと決まりを破ったコードを置くと、本当に落ちる。いまのコードは通る。

⚠️ 破るためのコードの中の決まった文字（試作の場所の名前・データベースへ渡す鍵の名前など）は、この試験のファイル
自体が見張りに引っかからないよう、文字をつないで作っている。
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from checks import rules

APP = Path(__file__).resolve().parents[1]
P0 = "phase" + "0"
SCOPE_KEY = "salescall." + "tenant_id"
KEY_NAME = "GEMINI_" + "API_KEY"
SET_CONFIG = "set_" + "config"


class Planted(unittest.TestCase):
    def _app(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp()) / "app"
        (root.parent / P0).mkdir(parents=True)
        (root.parent / P0 / "flowctl.py").write_text("", encoding="utf-8")
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(body), encoding="utf-8")
        return root

    def test_no_phase0_import_by_module_name(self):
        root = self._app({"src/salescall/core/x.py": "import flowctl\n"})
        self.assertEqual([v.rule for v in rules.no_phase0(root)], ["I-30"])

    def test_no_phase0_path(self):
        root = self._app({"src/salescall/core/x.py": f"import sys\nsys.path.append('../{P0}')\n"})
        self.assertEqual([v.rule for v in rules.no_phase0(root)], ["I-30"])

    def test_db_driver_outside_layer(self):
        root = self._app({"src/salescall/web/x.py": "import psycopg\n",
                          "src/salescall/core/access.py": "import psycopg\n"})
        found = rules.db_driver_only_in_layer(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("I-31", "src/salescall/web/x.py")])

    def test_web_parts_only_in_web(self):
        root = self._app({"src/salescall/core/x.py": "import fastapi\nfrom jinja2 import Template\n",
                          "src/salescall/output/y.py": "import httpx\n",
                          "src/salescall/web/app.py": "import fastapi\nimport uvicorn\n",
                          "tests/test_web.py": "import httpx\n"})
        found = rules.web_parts_only_in_web(root)
        self.assertEqual(sorted((v.path, v.line) for v in found),
                         [("src/salescall/core/x.py", 1), ("src/salescall/core/x.py", 2), ("src/salescall/output/y.py", 1)])

    def test_scope_settings_outside_layer(self):
        root = self._app({"src/salescall/output/x.py": f"SQL = \"SELECT {SET_CONFIG}('{SCOPE_KEY}', '1', true)\"\n"})
        self.assertEqual([v.rule for v in rules.scope_settings_only_in_layer(root)], ["11章"])

    def test_type_name_is_not_a_scope_setting(self):
        root = self._app({"src/salescall/core/x.py": 'SQL = "SELECT %s::salescall.actor_kind, salescall.cur_actor()"\n'})
        self.assertEqual(rules.scope_settings_only_in_layer(root), [])

    def test_current_setting_outside_layer(self):
        key = "salescall." + "client_id"
        root = self._app({"src/salescall/web/x.py": f"SQL = \"SELECT current_setting('{key}')\"\n"})
        self.assertEqual([v.rule for v in rules.scope_settings_only_in_layer(root)], ["11章"])

    def test_key_value_outside_key_selection(self):
        root = self._app({"src/salescall/conversation/session.py": f"import os\nk = os.environ['{KEY_NAME}']\n",
                          "src/salescall/conversation/keys.py": f"import os\nk = os.environ['{KEY_NAME}']\n"})
        found = rules.key_values_only_in_key_selection(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("I-32", "src/salescall/conversation/session.py")])


class SingleWriters(Planted):
    """I-7・I-12：書く場所は1か所。"""

    def test_result_written_outside_results(self):
        root = self._app({"src/salescall/core/judge.py": 'SQL = "INSERT INTO salescall.task_results (x) VALUES (1)"\n',
                          "src/salescall/core/results.py": 'SQL = "INSERT INTO salescall.task_results (x) VALUES (1)"\n'})
        found = rules.single_writers(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("I-12", "src/salescall/core/judge.py")])

    def test_attempt_state_written_outside_attempts(self):
        root = self._app({"src/salescall/core/supervisor_x.py": 'SQL = "UPDATE salescall.attempts SET state = %s"\n'})
        self.assertEqual([v.rule for v in rules.single_writers(root)], ["I-12"])

    def test_schedule_written_outside_next_action(self):
        root = self._app({"src/salescall/core/errors.py": 'SQL = "insert into salescall.task_next_actions (x) values (1)"\n'})
        self.assertEqual([v.rule for v in rules.single_writers(root)], ["I-12"])

    def test_screen_writes_core_table(self):
        root = self._app({"src/salescall/web/x.py": 'SQL = "UPDATE salescall.contacts SET phone_e164 = %s"\n'})
        self.assertEqual([v.rule for v in rules.single_writers(root)], ["I-12"])

    def test_delete_anywhere(self):
        root = self._app({"src/salescall/core/x.py": 'SQL = "DELETE FROM salescall.tasks"\n'})
        self.assertEqual([v.rule for v in rules.single_writers(root)], ["I-12"])

    def test_dynamic_table_name(self):
        root = self._app({"src/salescall/core/results.py": 't = "task_results"\nSQL = f"INSERT INTO salescall.{t} (x) VALUES (1)"\n'})
        self.assertEqual([v.rule for v in rules.single_writers(root)], ["I-12"])

    def test_tasks_and_contacts_are_created_in_one_place(self):
        root = self._app({"src/salescall/core/gate.py": 'SQL = "INSERT INTO salescall.tasks (x) VALUES (1)"\n',
                          "src/salescall/core/conversation_io.py": 'SQL = "INSERT INTO salescall.contacts (x) VALUES (1)"\n',
                          "src/salescall/core/importer.py": 'SQL = "INSERT INTO salescall.tasks (x) VALUES (1)"\n',
                          "src/salescall/core/contacts.py": 'SQL = "INSERT INTO salescall.contacts (x) VALUES (1)"\n'})
        found = rules.single_writers(root)
        self.assertEqual(sorted(v.path for v in found),
                         ["src/salescall/core/conversation_io.py", "src/salescall/core/gate.py"])

    def test_output_may_write_deliveries(self):
        root = self._app({"src/salescall/output/ledger.py": 'SQL = "UPDATE salescall.deliveries SET state = %s"\n'})
        self.assertEqual(rules.single_writers(root), [])

    def test_notification_deliveries_only_from_output(self):
        # 2026-10-16 見直し：表の行の括弧がずれて、知らせの送信の記録が「連携の台帳の中の鍵」になり、見張られていなかった
        root = self._app({"src/salescall/core/x.py": 'SQL = "INSERT INTO salescall.notification_deliveries (a) VALUES (1)"\n',
                          "src/salescall/output/y.py": 'SQL = "INSERT INTO salescall.notification_deliveries (a) VALUES (1)"\n'})
        self.assertEqual([v.path for v in rules.single_writers(root)], ["src/salescall/core/x.py"])

    def test_write_functions_are_called_only_from_their_place(self):
        """データベースの中で書き換える関数も、呼んでよい部品を決める（2026-10-22 の見直し：画面の部品からでも通った）。読むだけの
        関数は、どこから呼んでもよい。"""
        root = self._app({"src/salescall/web/x.py": 'SQL = "SELECT * FROM salescall.recover_lines(%s)"\n',
                          "src/salescall/core/leases.py": 'SQL = "SELECT * FROM salescall.recover_lines(%s)"\n',
                          "src/salescall/web/y.py": 'SQL = "SELECT salescall.jst(now())"\n'})
        self.assertEqual([v.path for v in rules.single_writers(root)], ["src/salescall/web/x.py"])

    def test_quoted_capital_or_inserted_names_are_watched_too(self):
        """引用符でくくった名前・大文字の名前・差し込んだ名前でも、書く表と書き換える関数を見張る（⛔ 2026-10-22 の見直し13：
        salescall."recover_lines"( ・ salescall.{f}( は素通りしていた）。"""
        root = self._app({"src/salescall/web/a.py": 'SQL = \'SELECT * FROM salescall."recover_lines"(%s)\'\n',
                          "src/salescall/web/b.py": 'SQL = "SELECT * FROM salescall.RECOVER_LINES(%s)"\n',
                          "src/salescall/web/c.py": 'f = "recover_lines"\nSQL = f"SELECT * FROM salescall.{f}(%s)"\n',
                          "src/salescall/web/d.py": 'SQL = \'UPDATE salescall."contacts" SET phone_e164 = %s\'\n',
                          "src/salescall/web/e.py": 'SQL = "UPDATE salescall.CONTACTS SET phone_e164 = %s"\n',
                          "src/salescall/web/f.py": 't = "contacts"\nSQL = f\'UPDATE salescall."{t}" SET phone_e164 = %s\'\n',
                          "src/salescall/core/leases.py": 'SQL = \'SELECT * FROM salescall."recover_lines"(%s)\'\n',
                          "src/salescall/web/g.py": 'SQL = \'SELECT salescall."jst"(now()) FROM salescall."tasks" x\'\n'})
        self.assertEqual(sorted(v.path for v in rules.single_writers(root)),
                         [f"src/salescall/web/{c}.py" for c in "abcdef"])

    def test_writers_table_shape(self):
        # 書き手の表の1行は、書き方（INSERT・UPDATE）ごとの場所だけ（ほかの鍵が入っていたら、表の行がずれている）
        for table, verbs in rules.WRITERS.items():
            self.assertTrue(set(verbs) <= {"INSERT", "UPDATE"}, table)
            self.assertTrue(all(isinstance(v, frozenset) for v in verbs.values()), table)


class PhoneFormatting(Planted):
    def test_formatting_outside_q1(self):
        root = self._app({"src/salescall/core/importer.py": 'def f(x):\n    return "+81" + x[1:]\n',
                          "src/salescall/core/phone.py": 'P = "+81"\n'})
        found = rules.phone_formatting_only_in_q1(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("Q1", "src/salescall/core/importer.py")])


    def test_spoken_digits_then_normalize_is_allowed(self):
        """CL-1 の分け方（2026-10-13）：会話の側は「話し言葉 → 数字の並び」だけを行い、そのあと normalize を呼ぶ。
        「+81」の組み立て・種類の判断をしなければ、見張りは落ちない。"""
        body = (
            "from salescall.core.phone import normalize\n"
            "KANJI = str.maketrans('〇一二三四五六七八九', '0123456789')\n"
            "def spoken_to_digits(said):\n"
            "    return said.translate(KANJI).replace('の', '')\n"
            "def phone_value(said):\n"
            "    return normalize(spoken_to_digits(said))\n"
        )
        root = self._app({"src/salescall/core/value_types.py": body, "src/salescall/conversation/flow.py": body})
        self.assertEqual(rules.run_all(root), [])


class DialOnlyThroughExit(Planted):
    """I-1：電話をかけてよいのは、発信の出口だけ（門を通らない発信の道を作らない）。"""

    def test_start_call_outside_exit(self):
        root = self._app({"src/salescall/agent/phones/x.py": "def go(d, p):\n    d.start_call(p)\n",
                          "src/salescall/supervisor/y.py": "def go(d, p):\n    getattr(d, 'start_call')(p)\n",
                          "src/salescall/core/dial_exit.py": "def dial(d, p):\n    d.start_call(p)\n"})
        found = rules.dial_only_through_exit(root)
        self.assertEqual(sorted(v.path for v in found), ["src/salescall/agent/phones/x.py", "src/salescall/supervisor/y.py"])

    def test_the_test_number_is_rung_only_from_its_one_place(self):
        """テスト番号へ鳴るか試す（test_ring）は core/ring_test.py だけ（0134。門を通らない道を、お客様の番号へかける道にしない）。
        発信の出口からも呼ばない（それぞれ自分の1か所だけ）。"""
        root = self._app({"src/salescall/core/ring_test.py": "def run(d, p):\n    d.test_ring(p)\n",
                          "src/salescall/core/dial_exit.py": "def dial(d, p):\n    d.test_ring(p)\n",
                          "src/salescall/supervisor/line.py": "def go(d, p):\n    getattr(d, 'test_ring')(p)\n"})
        found = rules.dial_only_through_exit(root)
        self.assertEqual(sorted(v.path for v in found), ["src/salescall/core/dial_exit.py", "src/salescall/supervisor/line.py"])

    def test_the_relays_may_pass_it_on_only_in_their_own_place(self):
        """中継（5-5）は、決めた関数の中だけ（同じファイルのほかの所では、かけられない）。"""
        root = self._app({"src/salescall/agent/main.py": "class Agent:\n    def _do(self, p):\n        p.start_call('x')\n"
                                                        "    def other(self, p):\n        p.start_call('x')\n",
                          "src/salescall/supervisor/channel.py": "class AgentDriver:\n    def start_call(self, n):\n"
                                                                  "        self.ask(op='start_call')\n"
                                                                  "def side(d):\n    d.start_call('x')\n"})
        found = rules.dial_only_through_exit(root)
        self.assertEqual(sorted((v.path, v.line) for v in found),
                         [("src/salescall/agent/main.py", 5), ("src/salescall/supervisor/channel.py", 5)])


class EntriesCalledFromTheirPlaces(Planted):
    """一本化：決める入口（Python の関数）は、決めた所からだけ呼ぶ（0136。⛔ 見直し19b：データベースの関数を包む Python の入口は、
    どこからでも呼べた）。呼ぶ・関数を渡す・名前で取り込む、のどれも見る。その入口のモジュールの中はよい。"""

    def test_an_entry_outside_its_places_is_caught(self):
        root = self._app({
            "src/salescall/supervisor/line.py": "def go(tx, l):\n    attempts.close_unrung_run(tx, l, 'no_rows')\n",
            "src/salescall/core/attempts.py": "def close_unrung_run(tx, l, r):\n    pass\ndef x(tx, l):\n    close_unrung_run(tx, l, 'r')\n",
            "src/salescall/core/settle.py": "def go(tx, l):\n    attempts.close_unrung_run(tx, l, 'no_rows')\n",
            "src/salescall/core/next_action.py": "from salescall.core.attempts import end_for_trouble\n",
            "src/salescall/supervisor/jobs.py": "def go(a):\n    a.in_tenant(lambda tx: ring_test.run)\n",
            # こちらの不具合を伝える入口（0139）：通話ごとの口（supervisor/line.py）のほかからは伝えない（2つ目の伝え方を作らない）
            "src/salescall/core/settle_more.py": "def go(a):\n    troubles.after_trouble(a)\n",
            # 別名で取り込んでも・部品の名前を全部書いても見つける（⛔ 見直し20a：`import … as 別名` を素通りした）
            "src/salescall/core/alias_a.py": "from salescall.core import attempts as at\ndef go(tx, l):\n    at.close_unrung_run(tx, l, 'x')\n",
            "src/salescall/core/alias_b.py": "import salescall.core.ring_test as rt\ndef go(a):\n    rt.run(a)\n",
            "src/salescall/core/alias_c.py": "import salescall.core.attempts\ndef go(tx, l):\n    salescall.core.attempts.hold_unrung_run(tx, l)\n",
            "src/salescall/core/alias_ok.py": "from salescall.core import attempts as at\ndef go(tx, l):\n    at.blame(tx, l)\n",
        })
        found = rules.entries_called_from_their_places(root)
        self.assertEqual(sorted((v.path, v.rule) for v in found),
                         [("src/salescall/core/alias_a.py", "一本化"), ("src/salescall/core/alias_b.py", "一本化"),
                          ("src/salescall/core/alias_c.py", "一本化"),
                          ("src/salescall/core/next_action.py", "一本化"), ("src/salescall/core/settle.py", "一本化"),
                          ("src/salescall/core/settle_more.py", "一本化"), ("src/salescall/supervisor/jobs.py", "一本化")])


class CallErrorsOnlyThroughTroubles(Planted):
    """一本化：出どころ「通話」のエラーを作るのは core/troubles.py だけ（見直し20b：ほかで作ると、行が休み始めた知らせの出ない
    2つ目の伝え方になる）。位置でも source= でも、trouble= だけでも見つける。読み戻して表を引く所（出どころが変数）はよい。"""

    def test_a_call_error_made_elsewhere_is_caught(self):
        root = self._app({
            "src/salescall/core/troubles.py": "def after_trouble(a):\n    watch.after_error(a, ErrorReport('call', None, 'x', trouble='frozen'))\n",
            "src/salescall/supervisor/line.py": "def go(a):\n    watch.after_error(a, ErrorReport('call', None, 'x', trouble='frozen'))\n",
            "src/salescall/supervisor/jobs.py": "def go(a):\n    x = errors.ErrorReport(source='call', code=None, message='x')\n",
            "src/salescall/core/settle.py": "def go(a, s):\n    x = ErrorReport(s, None, 'x', trouble='poor_audio')\n",
            "src/salescall/core/stops.py": "def go(e):\n    return errors.lookup(errors.ErrorReport(e['source'], e['code'], e['message']))\n",
            "src/salescall/core/attempts.py": "def go(tx):\n    watch.record_error(tx, ErrorReport('recording', None, 'x'))\n",
        })
        found = rules.call_errors_only_through_troubles(root)
        self.assertEqual(sorted((v.path, v.line) for v in found),
                         [("src/salescall/core/settle.py", 2), ("src/salescall/supervisor/jobs.py", 2),
                          ("src/salescall/supervisor/line.py", 2)])

    def test_restoring_a_resting_row_is_for_the_screens_only(self):
        root = self._app({"src/salescall/web/rows.py": "def go(tx, t):\n    troubles.restore(tx, t, reason='直した')\n",
                          "src/salescall/supervisor/jobs.py": "def go(tx, t):\n    troubles.restore(tx, t, reason='x')\n"})
        found = rules.entries_called_from_their_places(root)
        self.assertEqual([(v.path, v.rule) for v in found], [("src/salescall/supervisor/jobs.py", "一本化")])


class PcSideStaysOffTheServer(Planted):
    """5-5：PC の部品（agent・link）は、データベースにも、サーバーの部品にも触れない。PC とのつなぎを使うのは、つなぎ口と PC の部品だけ。"""

    def test_the_pc_side_reaching_the_server(self):
        root = self._app({"src/salescall/agent/a.py": "import psycopg\nfrom salescall.core import gate\nfrom ..core import x\n",
                          "src/salescall/link/b.py": "from salescall.supervisor.links import Attached\n",
                          "src/salescall/core/c.py": "import websockets\n",
                          "src/salescall/supervisor/channel.py": "import websockets\n",
                          "src/salescall/agent/ok.py": "import websockets\nfrom salescall.link import protocol\n"
                                                      "from salescall.agent import backup\n",
                          "src/salescall/link/d.py": "\nfrom salescall.agent.pc_login import PcPass\n",
                          "src/salescall/core/e.py": "\n\nimport salescall.agent.backup\n",
                          # 電話の部品の置き場（2026-10-22 ユーザー承認：PC の部品の側）も同じ見張りの中
                          "src/salescall/agent/phones/rakuten.py": "from salescall.core import dial_exit\n"})
        found = rules.pc_side_stays_off_the_server(root)
        self.assertEqual(sorted((v.path, v.line) for v in found),
                         [("src/salescall/agent/a.py", 1), ("src/salescall/agent/a.py", 2), ("src/salescall/agent/a.py", 3),
                          ("src/salescall/agent/phones/rakuten.py", 1),
                          ("src/salescall/core/c.py", 1), ("src/salescall/core/e.py", 3), ("src/salescall/link/b.py", 1),
                          ("src/salescall/link/d.py", 2)])


class SettingsReadInOnePlace(Planted):
    """決まった設定は1か所からしか読まない：時間帯は gate（NG時間を引く。5-2-2）、契約者の承認のモードは承認の部分（I-13）。"""

    def test_reading_hours_elsewhere(self):
        root = self._app({"src/salescall/core/next_action.py": "def f(tx, r):\n    return r(tx, 'calling.hours')\n",
                          "src/salescall/web/x.py": "K = 'calling.ng_hours'\n",
                          "src/salescall/core/gate.py": "def g(r):\n    return r('calling.hours'), r('calling.ng_hours')\n"})
        found = rules.settings_read_in_one_place(root)
        self.assertEqual(sorted(v.path for v in found), ["src/salescall/core/next_action.py", "src/salescall/web/x.py"])

    def test_reading_the_approval_mode_elsewhere(self):
        root = self._app({"src/salescall/core/judge.py": "MODE = 'approval.human'\n",
                          "src/salescall/core/approval.py": "MODE = 'approval.human'\n"})
        found = rules.settings_read_in_one_place(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("I-13", "src/salescall/core/judge.py")])


class ValueTypesStaysPure(Planted):
    def test_db_access_in_value_types(self):
        for body in ("from salescall.core.access import Tx\n",
                     'SQL = "SELECT readback FROM salescall.field_types"\n'):
            with self.subTest(body=body):
                root = self._app({"src/salescall/core/value_types.py": body})
                self.assertEqual([v.rule for v in rules.value_types_stays_pure(root)], ["持ち場"])

    def test_pure_checks_are_fine(self):
        root = self._app({"src/salescall/core/value_types.py": "import datetime\nfrom salescall.core.phone import normalize\n"})
        self.assertEqual(rules.value_types_stays_pure(root), [])


class CurrentCodePasses(unittest.TestCase):
    def test_all_rules_pass(self):
        self.assertEqual([str(v) for v in rules.run_all(APP)], [])


if __name__ == "__main__":
    unittest.main()
