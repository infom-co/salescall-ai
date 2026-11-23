"""読み手の別の読み（帳面 1 の (6) の (i) の L6・担当 AK の棚卸しの P10 と束6。分身 AV）：答えを伏せた読み手が挙げた、正解の外の読み（1人だけの読み・決まりで決まる側を
正解にした行のもう一方の読み・読みは正解とそろったが説明の文が引っ張りうると書いた読み）を、正解の表の行に持たせる唯一の欄と読み口。
- 欄の形は1つ（どの表の行も同じ名前・同じ形）：{"表": G-53・G-59・G-68・G-63 のどれか, "鍵": 行の鍵（key_of の1か所で作る）, "束": 読んだ束, "読み手": A・B など,
  "項目": 読んだ項目（答えは answer・言い切りは firm・ほかは項目の中の名前＝flowctl の SIDE_ITEMS・お呼びする段の項目と asks・中身の判定は choice）, "値": 読んだ値,
  "種類": 読んだ（READ）か、説明の文が引っ張りうると書いた（PULLED）か, "動き": その読みのときの流れの制御の動き（作ったときに流れの制御に通して書いた）, "出どころ": 並べた表など}。
  置き場は reader_alts.json の1つ（表ごとに欄を写さない＝G-59・G-68・G-63 の表は行が組の形で、行の中に欄を足すと置き場が4つになる）。行は「表」と「鍵」で引く。
- 読み口は of・same の1か所（どの数えの道具もここを呼ぶ）：測りの数え（答えの段 g53_meaning.tally・お呼びする段 g59_tally.cascade・戻ったか g68_returned.run・
  中身の判定 g63_grounding.overall）は、判定の読みが正解の外で、読み手の別の読みと同じ値だったら、それを分けて出す（正解には数えない＝数えは今と同じ・割れ方の欄が増えるだけ）。
- ⛔ 欄は答えを決めるのに使わない（判定に送る文・流れの制御・正解の数えは読まない＝試験 AVReaderAltsTest）。
- これからの束の読み手の別の読みも、この欄に足す（足し方は分身 AV の書き置き rv200/AV/notes.md の 5章）。"""
from __future__ import annotations

import functools
import json
from pathlib import Path

HERE = Path(__file__).parent
PATH = HERE / "reader_alts.json"
TABLES = ("G-53", "G-59", "G-68", "G-63")
READ, PULLED = "読んだ", "説明の文が引っ張りうる"
FIELDS = ("表", "鍵", "束", "読み手", "項目", "値", "種類", "動き", "出どころ")


def key_of(table: str, row) -> tuple:
    """行の鍵（唯一の作り）：答えの段（G-53）＝(組, 質問, 言葉, 言い直し, 場面)・お呼びする段（G-59）＝(言葉, その前の言葉, 確かめたあとか, 後任の方の場面か, 場面の業務の印)・
    戻ったか（G-68）＝(待たせた方の言葉, 話した方の言葉)・中身の判定（G-63）＝(お客様の言葉, こちらが返す文)。row は表の行（dict か、G-68・G-63 は組の形）。"""
    if table == "G-53":
        return (row["set"], row["qid"], row["text"], row.get("asked") or "", row.get("scene") or "")
    if table == "G-59":
        from g59_calling_judge import row_marks
        return (row["text"], row.get("before") or "", bool(row.get("asked")), bool(row.get("successor")), *row_marks(row))
    if table in ("G-68", "G-63"):
        return tuple(row[:2])
    raise KeyError(table)


@functools.cache
def _all() -> dict:
    """{(表, 鍵): [欄の並び]}（reader_alts.json を1回だけ読む）。"""
    got = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {"alts": []}
    out: dict = {}
    for e in got["alts"]:
        assert tuple(e) == FIELDS and e["表"] in TABLES and e["種類"] in (READ, PULLED), e
        out.setdefault((e["表"], tuple(e["鍵"])), []).append(e)
    return out


def of(table: str, row) -> list[dict]:
    """行の読み手の別の読みの並び（無ければ []）。"""
    return list(_all().get((table, key_of(table, row)), []))


def same(table: str, row, readings: dict) -> list[dict]:
    """判定の読み readings（{項目: 値}）のうち、行の読み手の別の読みと同じ値のもの（読み手が読んだ・説明の文が引っ張りうると書いた、のどちらも）。
    正解の外かは、呼ぶ数えの側が決める（ここは値を比べるだけ）。"""
    return [e for e in of(table, row) if e["項目"] in readings and readings[e["項目"]] == e["値"]]


def label(e: dict) -> str:
    """数えの欄の名前（束・読み手・項目・値・種類）。"""
    return f"束 {e['束']} の {e['読み手']}・{e['項目']}＝{e['値']}（{e['種類']}）"
