"""使っている AI に新しい版が出ていないかの見張り（本番。DESIGN.md 9-3）。持ち主 CL-2（いつ確かめるか・何を知らせるか）。

✅ 2026-11-15 日本時間12時43分22秒 ユーザー：「AIは全て、最新版が大きく進化するため、テストをする場合は必ず、最新版が出ていないかを確認して
ください。本番に入っても10日に一回は最新版が出ていないかを確認して、最新版が出れば置き換えてください。」（CL-1 の連絡帳 4-163）

- **確かめるのは CL-1 の部品**（`supervisor.plugins.VersionChecker`。CL-1 の latest_ai と同じ1か所：使っている名前は、版まで決めて持つ所から
  読み、作り手の公開のモデルの一覧だけを読む＝問い合わせではないので料金0円）。こちらはモデルの名前も、新しい版かの決め方も持たない。
  答えの行は CL-1 の `latest_ai.check` の行のまま（役目・名前・使っている版・一覧にある・新しい版・別の系統で後から出たもの・見送った版、
  または一覧を読めなかった理由＝結果）。見送った版（CL-1 が新しい版で測って替えないと決めた版＝CL-1 の latest_ai.DECLINED の1か所）は、
  CL-1 の部品が「新しい版」にも「別の系統」にも並べない（CL-1 の連絡帳 4-164）＝同じ版の知らせ・修正依頼をくり返さない。こちらは新しい版の
  知らせの記録に添えるだけ（管理者が、見送ったことを知って読む）。古い形の行（見送った版の無い行）も読む。
- **いつ**：中央の見守り役の毎日1回の仕事（`supervisor.jobs.ai_versions`）。「10日に一回は」を毎日で満たす（料金0円なので回数を減らす
  得が無い・前の確かめの時刻を持たない＝見守り役が止まっても、1回落ちても、次の回にまた確かめる）。
- **何を知らせるか**は、行ごとに `outcome` の1か所：
  (1) 新しい版がある ⇒ 管理者あての知らせ（`ai_newer_version`）と、CL-1 への修正依頼（同じ AI の同じ版は、知らせは1回・修正依頼は CL-1 が
      直したと報告するまで1つ）。置き換えは CL-1 が新しい版で線（確かな読みとする確からしさ）を決め直す測りをしてから、版を持つ1か所を
      直す（名前だけ替えると、線が古い版の測りの値のまま残る＝4-163 の (b)）。⚠️ 測って見送った版を、CL-1 の部品が新しい版として返し
      続けると、直したと報告したあと、次の日にまた修正依頼が出る（見送った版を覚えるのは CL-1 の1か所）。
  (2) 使っている版が作り手の一覧に無い（もう無い・名前が変わった＝近く使えなくなりうる）⇒ 同じく管理者あての知らせと CL-1 への修正依頼
      （`ai_version_missing`）。
  (3) 作り手の一覧を読めなかった・一覧の口が無い（AmiVoice は作り手の頁で確かめる）⇒ 管理者あての知らせ（`ai_version_unchecked`）で、
      手で確かめていただく。同じ AI について10日ごとに1つ（`UNCHECKED_EVERY_DAYS`。毎日の知らせにしない＝一覧の口が無い作り手は
      ずっとこの行になる）。
- 知らせ・修正依頼に入るのは、役目・名前・版の名前だけ（お客様の情報は入らない）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from salescall.core import fix_requests, notify
from salescall.core.access import Tx

UNCHECKED_EVERY_DAYS = 10   # 手で確かめていただく知らせの間隔（✅ 2026-11-15 ユーザー「10日に一回は」）
NEWER, MISSING, UNCHECKED = "newer", "missing", "unchecked"
KIND = {NEWER: "ai_newer_version", MISSING: "ai_version_missing", UNCHECKED: "ai_version_unchecked"}


class BadRows(ValueError):
    """確かめの部品の答えの形が違う（CL-1 の latest_ai.check の行でない）。"""


def _text(row: dict[str, Any], key: str) -> str:
    v = row.get(key)
    if not isinstance(v, str) or not v.strip():
        raise BadRows(f"確かめの部品の行の {key} は、空でない文字です（{v!r}）")
    return v.strip()


def _names(row: dict[str, Any], key: str) -> list[str]:
    v = row.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise BadRows(f"確かめの部品の行の {key} は、版の名前の並びです（{v!r}）")
    return [x.strip() for x in v]


def outcome(row: Any) -> tuple[str, str] | None:
    """1つの行から、何を知らせるか（1か所）：(種類, 同じ知らせかを決める版の名前) か None（知らせることが無い）。種類は NEWER（いちばん新しい
    版）・MISSING（使っている版）・UNCHECKED（読めなかった理由）。形の違う行は BadRows。"""
    if not isinstance(row, dict):
        raise BadRows(f"確かめの部品の行は、名前と値の組です（{row!r}）")
    _text(row, "役目"), _text(row, "名前")
    if "結果" in row:   # 一覧を読めなかった・一覧の口が無い（CL-1 の latest_ai.check）
        return UNCHECKED, _text(row, "結果")
    current = _text(row, "使っている版")
    newer = _names(row, "新しい版")
    _names(row, "別の系統で後から出たもの"), _names(row, "見送った版")
    if not isinstance(row.get("一覧にある"), bool):
        raise BadRows(f"確かめの部品の行の 一覧にある は、真か偽です（{row.get('一覧にある')!r}）")
    if newer:
        return NEWER, newer[-1]   # 並びは古い順（CL-1 の compare＝版の比べ方の順）
    if not row["一覧にある"]:
        return MISSING, current
    return None


def record(tx: Tx, rows: Any, *, now: datetime | None = None) -> dict[str, int]:
    """確かめの部品の答え（行の並び）を受けて、知らせと修正依頼を出す（全体の取引で。中央の見守り役の仕事だけが呼ぶ）。答え＝種類ごとの、
    知らせることのあった行の数。どれかの行の形が違えば、どれも出さずに BadRows（半分だけ知らせない）。"""
    if not isinstance(rows, list):
        raise BadRows(f"確かめの部品の答えは、行の並びです（{type(rows).__name__}）")
    found = [(r, got) for r in rows if (got := outcome(r)) is not None]
    period = int((now or datetime.now(timezone.utc)).timestamp() // 86400) // UNCHECKED_EVERY_DAYS
    # 同じ知らせかの鍵（1か所）：新しい版・一覧に無いは、使っている版（と新しい版）ごと＝同じ版を2つの役目・名前で使っていても（「Claude Sonnet 5.5」と
    # 「Claude Sonnet 5.5（考える）」など）、知らせと修正依頼は1つ（置き換えは版を持つ1か所で1回）。読めなかったは AI の名前ごと・10日ごと（版が分からない
    # こともある）。⛔ 2026-11-15 の見直しまで：名前ごとの鍵で、同じ版の知らせと修正依頼が2つずつ出た
    groups: dict[str, list[tuple[dict[str, Any], str, str]]] = {}
    for row, (kind, version) in found:
        current = row.get("使っている版") or row.get("id") or row.get("display")
        key = f"ai_version:{kind}:" + (f"{current}:{version}" if kind == NEWER else str(current) if kind == MISSING
                                       else f"{row['名前'].strip()}:{period}")
        groups.setdefault(key, []).append((row, kind, version))
    counts = dict.fromkeys(KIND, 0)
    for key, members in groups.items():
        row, kind, version = members[0]
        who = "・".join(dict.fromkeys(f"{r['役目'].strip()}の {r['名前'].strip()}" for r, _, _ in members))
        refs = {"役目": [r["役目"].strip() for r, _, _ in members], "名前": [r["名前"].strip() for r, _, _ in members],
                "使っている版": row.get("使っている版") or row.get("id") or row.get("display")}
        if kind == NEWER:
            summary = f"使っている AI に新しい版が出ています：{who}（{refs['使っている版']} → {version}）。CL-1 が新しい版で測ってから置き換えます"
            refs |= {k: row.get(k, []) for k in ("新しい版", "別の系統で後から出たもの", "見送った版")}
        elif kind == MISSING:
            summary = f"使っている AI の版が作り手の一覧にありません（もう無い・名前が変わった）：{who}（{version}）。CL-1 が確かめます"
        else:
            summary = f"使っている AI の新しい版を、作り手の一覧で確かめられませんでした：{who}（{version}）。作り手の頁で確かめてください"
        notify.emit(tx, KIND[kind], key=key, summary=summary, refs=refs, admin=True, contractor=False)
        if kind != UNCHECKED:
            fix_requests.request(tx, "CL-1", KIND[kind], key=key, summary=summary, refs=refs)
        counts[kind] += len(members)
    return counts
