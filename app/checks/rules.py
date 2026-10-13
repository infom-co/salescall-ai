"""決まりの機械の見張り（静的検査。DESIGN.md 14章）。持ち主 CL-2。

どの見張りも「わざと破ると、本当に落ちる」ことを app/tests/test_checks.py で確かめている（見張りが落ちることを
確かめていないものは、見張っていないのと同じ。DESIGN.md 14章）。
見張りを足す・許す場所を変えるときは、この1か所を変える（ほかの担当は、連絡帳で CL-2 に頼む）。
"""

from __future__ import annotations

import ast
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
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
_SCOPE_KEYS = ("salescall.tenant_id", "salescall.client_id", "salescall.actor")


def scope_settings_only_in_layer(app_root: Path) -> list[Violation]:
    out: list[Violation] = []
    for path in _python_files(app_root):
        rel = _rel(app_root, path)
        if rel in SCOPE_SETTING_ALLOWED:
            continue
        for text, line in _string_constants(_parse(path)):
            if any(k in text for k in _SCOPE_KEYS):
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


ALL_RULES: tuple[Callable[[Path], list[Violation]], ...] = (
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
