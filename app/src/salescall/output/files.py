"""結果のファイル（docs/DESIGN.md 7-3-1 の (3)。持ち主 CL-3）。いまは既定の型（CSV）だけ。⏳ 依頼者ごとの出力の型（7-3-5）は、型の表ができてから。

- **書き出す直前に、こちらが作った言葉に、依頼者へ出さない言葉が無いかを確かめる**（`output/words.not_for_client` の1か所。2026-11-18 管理者の一覧）。
  当たったら出さずに止める（`ClientWordsLeak`）。お客様・依頼者のデータ（会社名など）は確かめない（依頼者のものなので）。
- CSV は、Excel でそのまま開けるように UTF-8（BOM つき）・改行は CRLF。
- 依頼者へ渡すファイルとして書き出したときだけ、提出用の控えに置く（`backup.record_submission`。呼ぶのは画面の1か所）。
"""

from __future__ import annotations

import csv
import io

from salescall.output import result_rows, words


class ClientWordsLeak(ValueError):
    """依頼者へ出さない言葉が、こちらの作った言葉に入っていた（何も書き出さない）。"""


def check(rows: list[result_rows.ResultRow]) -> None:
    hits = sorted({h for t in list(result_rows.COLUMNS) + [w for r in rows for w in r.our_words()] for h in words.not_for_client(t)})
    if hits:
        raise ClientWordsLeak(f"依頼者へ出さない言葉が入っていたので、書き出しませんでした（{'・'.join(hits)}）。担当に直しを頼んでください")


def to_csv(rows: list[result_rows.ResultRow]) -> bytes:
    check(rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(result_rows.COLUMNS)
    for r in rows:
        w.writerow(r.cells())
    return buf.getvalue().encode("utf-8-sig")
