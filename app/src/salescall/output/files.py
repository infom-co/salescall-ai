"""結果のファイル（docs/DESIGN.md 7-3-1 の (3)。持ち主 CL-3）。いまは既定の型（CSV）だけ。⏳ 依頼者ごとの出力の型（7-3-5）は、型の表ができてから。

- **書き出す直前に、こちらが作った言葉に、依頼者へ出さない言葉が無いかを確かめる**（`output/words.not_for_client` の1か所。2026-11-18 管理者の一覧）。
  当たったら出さずに止める（`ClientWordsLeak`）。お客様・依頼者のデータ（会社名など）は確かめない（依頼者のものなので）。
- CSV は、Excel でそのまま開けるように UTF-8（BOM つき）・改行は CRLF。
- 依頼者へ渡すファイルとして書き出したときだけ、提出用の控えに置く（`submit` の1か所。`backup.record_submission` を呼ぶのはここだけ）。
  渡せる結果（確定した版）が1つも無ければ渡さない（サンプル検証＝模擬通話の段では、依頼者へは何も渡さない＝2026-11-18 管理者。
  模擬通話は結果の版を作らないので、渡せる結果が無い）。同じ中身をもう控えに置いていれば、重ねて置かない（二度押し・同じファイルの渡し直し）。
- **表計算の式として動く書き出しを作らない**：セルの頭が「= + - @」・タブ・改行なら、頭に「'」を付けて文字のままにする（`_cell` の1か所。
  依頼者のファイルに来た会社名などが式の形でも、依頼者の Excel で式として動かない）。
- ファイルの名前（依頼者の名前・実施回の名前・日付）も、依頼者の目に入る。こちらが付けた実施回の名前に依頼者へ出さない言葉があれば止め、
  ファイルの名前に使えない文字は「_」にする（`file_name` の1か所）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from typing import Any

from salescall.core import backup
from salescall.output import result_rows, words


class ClientWordsLeak(ValueError):
    """依頼者へ出さない言葉が、こちらの作った言葉に入っていた（何も書き出さない）。"""


class NothingToDeliver(ValueError):
    """依頼者へ渡せる結果（確定した版）が、まだ1つも無い。"""


def check(rows: list[result_rows.ResultRow], *ours: str) -> None:
    """こちらが作った言葉（列の名前・行の言葉・ours＝ファイルの名前に使う実施回の名前など）に、依頼者へ出さない言葉が無いか。"""
    texts = list(result_rows.COLUMNS) + [w for r in rows for w in r.our_words()] + list(ours)
    hits = sorted({h for t in texts for h in words.not_for_client(t)})
    if hits:
        raise ClientWordsLeak(f"依頼者へ出さない言葉が入っていたので、書き出しませんでした（{'・'.join(hits)}）。担当に直しを頼んでください")


_FORMULA_HEADS = ("=", "+", "-", "@", "\t", "\r", "\n")


def _cell(v: str) -> str:
    return "'" + v if v.startswith(_FORMULA_HEADS) else v


def to_csv(rows: list[result_rows.ResultRow], *ours: str) -> bytes:
    check(rows, *ours)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(result_rows.COLUMNS)
    for r in rows:
        w.writerow([_cell(c) for c in r.cells()])
    return buf.getvalue().encode("utf-8-sig")


_NOT_IN_NAMES = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def file_name(client_name: str, run_name: str, day: date) -> str:
    """結果のファイルの名前（1か所）。実施回の名前は、こちらが付けた言葉なので確かめる（依頼者の名前は依頼者のもの）。"""
    check([], run_name)
    name = _NOT_IN_NAMES.sub("_", f"{client_name}_{run_name}_結果_{day.isoformat()}").strip(" .")
    return name[:116] + ".csv"


def submit(tx: Any, rows: list[result_rows.ResultRow], name: str, data: bytes) -> bool:
    """依頼者へ渡すファイルを、提出用の控えに置く（依頼者の取引で）。置いたら True、同じ中身をもう置いていれば False（重ねない）。"""
    if not any(r.result for r in rows):
        raise NothingToDeliver("依頼者へ渡せる結果（確定した結果）が、まだありません。確かめるための書き出しは使えます")
    digest = hashlib.sha256(data).hexdigest()
    if tx.one("SELECT 1 FROM salescall.submission_files WHERE sha256 = %s", (digest,)) is not None:
        return False
    backup.record_submission(tx, name, data)
    return True
