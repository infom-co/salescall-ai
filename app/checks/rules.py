"""決まりの機械の見張り（静的検査。DESIGN.md 14章）。持ち主 CL-2。

どの見張りも「わざと破ると、本当に落ちる」ことを app/tests/test_checks.py で確かめている（見張りが落ちることを
確かめていないものは、見張っていないのと同じ。DESIGN.md 14章）。
見張りを足す・許す場所を変えるときは、この1か所を変える（ほかの担当は、連絡帳で CL-2 に頼む）。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}:{self.line} {self.message}"


def _python_files(app_root: Path) -> Iterator[Path]:
    for sub in ("src", "tests"):
        base = app_root / sub
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for p in sorted(app_root.glob("*.py")):
        yield p


def _rel(app_root: Path, path: Path) -> str:
    return path.relative_to(app_root).as_posix()


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno


def _string_constants(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """文字の定数。f文字列は、差し込む部分を「{}」にして1つの文字にする（表の名前を差し込んで見張りを逃れるのを見つける）。"""
    joined_parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in node.values)
            joined_parts.update(id(v) for v in node.values)
            yield text, node.lineno
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in joined_parts:
            yield node.value, node.lineno


# ── I-30：app/ のコードは phase0/ を読まない（試作が裏口から本番に入らないように。CL-1 の条件 (d)）
def no_phase0(app_root: Path) -> list[Violation]:
    phase0 = app_root.parent / "phase0"
    names = {p.stem for p in phase0.glob("*.py")} if phase0.is_dir() else set()
    names.add("phase0")
    out: list[Violation] = []
    for path in _python_files(app_root):
        tree = _parse(path)
        for module, line in _imported_modules(tree):
            if module.split(".")[0] in names:
                out.append(Violation("I-30", _rel(app_root, path), line, f"試作（phase0/）の部品 {module} を読んでいます"))
        for text, line in _string_constants(tree):
            if "phase0" in text:
                out.append(Violation("I-30", _rel(app_root, path), line, "試作（phase0/）の場所を指す文字があります"))
    return out


# ── I-31：データベースへつなぐのは、読み書きの層だけ（と、表の作りを流す処理・試験の土台）
DB_DRIVER_ALLOWED = {"src/salescall/core/access.py", "src/salescall/core/migrate.py", "run_tests.py"}


def db_driver_only_in_layer(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel in DB_DRIVER_ALLOWED:
            continue
        for module, line in _imported_modules(_parse(path)):
            if module.split(".")[0] in ("psycopg", "psycopg2", "psycopg_pool", "asyncpg", "sqlalchemy"):
                out.append(Violation("I-31", rel, line, f"データベースの部品 {module} は、読み書きの層（core/access.py）だけが使えます"))
    return out


# ── 11章：「いま誰が・どの契約者・どの依頼主か」をデータベースへ渡すのは、読み書きの層の1か所だけ
SCOPE_SETTING_ALLOWED = {"src/salescall/core/access.py"}
# 設定の名前は、引用符でくくった形（'salescall.tenant_id' など）か、set_config で渡す。
# ⚠️ 型の名前（salescall.actor_kind）を取り違えないよう、引用符でくくった形だけを見る（2026-10-13 に取り違えた）。
_SCOPE_SETTING = re.compile(r"""['"]salescall\.(tenant_id|client_id|actor|actor_kind)['"]|set_config\s*\(""")


def scope_settings_only_in_layer(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel in SCOPE_SETTING_ALLOWED:
            continue
        for text, line in _string_constants(_parse(path)):
            if _SCOPE_SETTING.search(text):
                out.append(Violation("11章", rel, line, "契約者・依頼主・誰が、をデータベースへ渡すのは、読み書きの層だけです"))
    return out


# ── I-32：APIキーの値を読むのは、キーの選択の処理（Q15。持ち主 CL-1）だけ（CL-1 の条件 (g)）
KEY_READ_ALLOWED = {"src/salescall/conversation/keys.py"}
_KEY_MARKERS = ("API_KEY", ".env")


def key_values_only_in_key_selection(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel in KEY_READ_ALLOWED or rel.startswith("tests/"):
            continue
        for text, line in _string_constants(_parse(path)):
            if any(m in text for m in _KEY_MARKERS):
                out.append(Violation("I-32", rel, line, "APIキーの値を読むのは、キーの選択の処理（conversation/keys.py）だけです"))
    return out


# ── I-7・I-12：書く場所は1か所（DESIGN.md 2章の「唯一の答える場所」）。表ごとに、書いてよい部品を決める。
#    ここに無い salescall の表は、業務の土台（src/salescall/core/）の中だけが書ける。消す（DELETE）は、どこからもしない。
#    末尾が「/」のものは、その場所の中ならどこでもよい。
_CORE = "src/salescall/core/"
WRITERS: dict[str, dict[str, frozenset[str]]] = {
    "attempts": {"INSERT": frozenset({_CORE + "gate.py"}),            # 予約を作るのは門だけ（Q2）
                 "UPDATE": frozenset({_CORE + "attempts.py"})},       # 状態を移すのは、試行の状態の部品だけ（4-2）
    "number_ledger": {"INSERT": frozenset({_CORE + "gate.py"}), "UPDATE": frozenset({_CORE + "gate.py"})},
    "task_results": {"INSERT": frozenset({_CORE + "results.py"})},           # 業務の状態（Q5 の記録）
    "result_releases": {"INSERT": frozenset({_CORE + "approval.py"})},       # 送ってよい印は承認の部分だけ（10章）
    "task_next_actions": {"INSERT": frozenset({_CORE + "next_action.py"})},  # 次の予定（Q4）
    "dnc_entries": {"INSERT": frozenset({_CORE + "dnc.py"}), "UPDATE": frozenset({_CORE + "dnc.py"})},  # Q3
    "system_dnc_entries": {"INSERT": frozenset({_CORE + "dnc.py"})},
    "stop_events": {"INSERT": frozenset({_CORE + "stops.py"})},              # 運転の状態（Q13）
    "item_values": {"INSERT": frozenset({_CORE + "conversation_io.py"}),     # 会話の処理が書き込む入口（CL-1 のお願い (a)）
                    "UPDATE": frozenset({_CORE + "conversation_io.py"})},
    "task_append_locks": {"INSERT": frozenset({_CORE + "tasks.py"}), "UPDATE": frozenset({_CORE + "tasks.py"})},
    "setting_values": {"INSERT": frozenset({_CORE + "settings.py"})},        # 設定の値（Q10）
    "contacts": {"INSERT": frozenset({_CORE + "contacts.py"})},              # 連絡先（番号は Q1 を通したものだけ）
    "tasks": {"INSERT": frozenset({_CORE + "importer.py"})},                 # タスクを作るのは取込だけ（3-4）
    "imports": {"INSERT": frozenset({_CORE + "importer.py"})},
    "import_rows": {"INSERT": frozenset({_CORE + "importer.py"})},
    "deliveries": {"INSERT": frozenset({"src/salescall/output/"}),           # 連携の台帳（Q17。持ち主 CL-3）
                   "UPDATE": frozenset({"src/salescall/output/"})},
}
_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+salescall\.(\w+|\{\})", re.IGNORECASE)


def _allowed(rel: str, places: frozenset[str]) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in places)


def single_writers(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel.startswith("tests/") or rel == "run_tests.py":
            continue  # 試験と試験の土台は、約束を試すため・守りをわざと外すために、データを直接書く
        for text, line in _string_constants(_parse(path)):
            for m in _WRITE.finditer(text):
                verb = m.group(1).split()[0].upper()
                table = m.group(2)
                if table == "{}":
                    out.append(Violation("I-12", rel, line, "書く表の名前を差し込みで作っています（書く場所を見張れない）"))
                    continue
                if verb == "DELETE":
                    out.append(Violation("I-12", rel, line, f"salescall.{table} を消しています（消すことはしない）"))
                    continue
                places = WRITERS.get(table, {}).get(verb, frozenset({_CORE}))
                if not _allowed(rel, places):
                    where = "、".join(sorted(places))
                    out.append(Violation("I-12", rel, line, f"salescall.{table} を {verb} してよいのは {where} だけです"))
    return out


# ── Q1：番号を整形してよいのは、番号の正規化（core/phone.py）だけ（取込・門・照合が、それぞれ番号を整形しない）
PHONE_FORMAT_ALLOWED = {"src/salescall/core/phone.py"}


def phone_formatting_only_in_q1(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel in PHONE_FORMAT_ALLOWED or rel.startswith("tests/") or rel == "run_tests.py":
            continue
        for text, line in _string_constants(_parse(path)):
            if "+81" in text:
                out.append(Violation("Q1", rel, line, "番号を整形してよいのは、番号の正規化（core/phone.py）だけです"))
    return out


# ── CL-2 の場所（core）に置く CL-1 の持ち物（2026-10-14 ユーザー承認）は、答えの形の確かめだけをする。
#    データベースの読み書き（読み書きの層を読む・表の名前を書く）はしない（持ち場の境目を、機械が守る）。
PURE_CHECK_FILES = {"src/salescall/core/value_types.py"}


def value_types_stays_pure(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for rel in sorted(PURE_CHECK_FILES):
        path = app_root / rel
        if not path.exists():
            continue
        tree = _parse(path)
        for module, line in _imported_modules(tree):
            if module.startswith("salescall.core.access") or module.split(".")[0] in ("psycopg", "psycopg2"):
                out.append(Violation("持ち場", rel, line, "答えの形の確かめの部品は、データベースの読み書きをしません"))
        for text, line in _string_constants(tree):
            if "salescall." in text and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE):
                out.append(Violation("持ち場", rel, line, "答えの形の確かめの部品は、データベースの読み書きをしません"))
    return out


ALL_RULES: tuple[Callable[[Path], list[Violation]], ...] = (
    value_types_stays_pure,
    phone_formatting_only_in_q1,
    single_writers,
    no_phase0,
    db_driver_only_in_layer,
    scope_settings_only_in_layer,
    key_values_only_in_key_selection,
)


def run_all(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for rule in ALL_RULES:
        out.extend(rule(app_root))
    return out
