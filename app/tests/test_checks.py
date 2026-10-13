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
        root = self._app({"src/salescall/output/x.py": f"SQL = \"SELECT set_config('{SCOPE_KEY}', '1', true)\"\n"})
        self.assertEqual([v.rule for v in rules.scope_settings_only_in_layer(root)], ["11章"])

    def test_key_value_outside_key_selection(self):
        root = self._app({"src/salescall/conversation/session.py": f"import os\nk = os.environ['{KEY_NAME}']\n",
                          "src/salescall/conversation/keys.py": f"import os\nk = os.environ['{KEY_NAME}']\n"})
        found = rules.key_values_only_in_key_selection(root)
        self.assertEqual([(v.rule, v.path) for v in found], [("I-32", "src/salescall/conversation/session.py")])


class CurrentCodePasses(unittest.TestCase):
    def test_all_rules_pass(self):
        self.assertEqual([str(v) for v in rules.run_all(APP)], [])


if __name__ == "__main__":
    unittest.main()
