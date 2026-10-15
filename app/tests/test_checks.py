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
        root = self._app({"src/salescall/telephony/x.py": "def go(d, p):\n    d.start_call(p)\n",
                          "src/salescall/supervisor/y.py": "def go(d, p):\n    getattr(d, 'start_call')(p)\n",
                          "src/salescall/core/dial_exit.py": "def dial(d, p):\n    d.start_call(p)\n"})
        found = rules.dial_only_through_exit(root)
        self.assertEqual(sorted(v.path for v in found), ["src/salescall/supervisor/y.py", "src/salescall/telephony/x.py"])


class SettingsReadInOnePlace(Planted):
    """決まった設定は1か所からしか読まない：時間帯は gate（NG時間を引く。5-2-2）、人の承認のモードは承認の部分（I-13）。"""

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
