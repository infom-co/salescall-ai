"""業務設定の取込（引き継ぎの (10)。Intent.md 6-1 の方式B・6-4・6-5・6-8）。持ち主 CL-2。

入口ごとに、業務設定の1つの形（core/business_settings.py の `check` が受ける形）にするだけ。入口は、Excel のファイル
（標準のひな形。シートと列の名前は、依頼主の形に合わせて対応付けられる＝`preview`）と、依頼主の画面から抜き出したもの・画面の
入力（同じ形を作って `review` に渡す。2026-10-16 ユーザー：「依頼主の画面から抽出する場合もある」）。**どの入口も `review`
（確かめ）と `register_reviewed`（登録）の同じ道を通る**。確かめそのものは business_settings の1か所。ファイルを読むのは
core/import_files.py。

流れ（Intent.md 6-5）：`preview`（何も書かない）⇒ 人が問題を直し、名前の読みを確かめる ⇒ `register`（プレビューと同じ中身の
ときだけ、新しい版を作る。全部か、何も無いか）。

- 標準のひな形の、シート・列・言葉の名前の正本は、この部品の `SHEETS`・`LABELS`（画面のひな形の書き出しも、これを読む）。
- 名前の読み（Intent.md 6-8）：名乗りと「名前」のシートの名前は、人がプレビューで読みを確かめたもの（`confirmed`）だけを
  登録できる。読みが無ければ、AI の読みの案（`ReadingSuggester`。作るのは CL-1）を出し、人が確かめる（直してもよい）。質問の文などの「」の中の言葉で、名前の一覧に無いものは「名前かもしれない言葉」として示す（枠だけ。⏳ 候補の
  出し方は CL-1 が比べている。PHASE0 G-23）。
- 「わからない・答えたくない」のシートは、質問ID「全部」の行で、必須の質問すべての扱いを1度に書ける（質問ごとの行があれば、
  そちらが勝つ）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from salescall.core import business_settings
from salescall.core.access import Tx
from salescall.core.import_files import FileRejected, Sheet, read

# ── 標準のひな形（正本はここ）。役目 → (シートの名前, 列の役目 → 見出し, 必ず要るシートか)
SHEETS: dict[str, tuple[str, dict[str, str], bool]] = {
    "basic": ("基本", {"item": "項目", "value": "値"}, True),
    "questions": ("質問", {"id": "質問ID", "text": "質問の文", "ask_about": "聞くこと", "type": "型", "required": "必須",
                          "choices": "選択肢", "decline": "断りの答え"}, True),
    "branches": ("分岐", {"id": "質問ID", "answer": "回答条件", "next": "次の質問ID"}, False),
    "handling": ("わからない・答えたくない", {"id": "質問ID", "state": "答え", "reason": "理由", "kind": "扱い",
                                          "conclusion": "結論の名前"}, True),
    "retry": ("かけ直し", {"key": "結果", "action": "かけ直すか", "max_times": "回数", "interval": "間隔"}, True),
    "forbidden": ("言ってはいけないこと", {"key": "名前", "say_not": "言ってはいけないこと", "correct": "正しい案内"}, False),
    "names": ("名前", {"text": "表記", "reading": "読み"}, False),
}
BASIC_ITEMS = {"業務の目的": "purpose", "自社名": "company", "自社名の読み": "company_reading", "担当者名": "person",
               "担当者名の読み": "person_reading", "用件の一文": "purpose_line", "目的の人": "target_kind",
               "目的の人の役職": "target_role", "忙しいと言われたときの一言": "busy_line", "声の型": "voice"}
LABELS: dict[str, dict[str, str]] = {
    "type": {"日付": "date", "時刻": "time", "日時": "datetime", "数": "number", "金額": "amount", "名前": "name",
             "電話番号": "phone", "メール": "email", "住所": "address", "はい／いいえ": "yes_no", "選択肢": "choice",
             "自由な意見": "free_text"},
    "target_kind": {"役職のみ": "role", "役職と名前": "role_and_name", "名前のみ": "name", "誰でも": "anyone"},
    "state": {"わからない": "dont_know", "答えたくない": "refused"},
    "reason": {"担当ではない": "not_in_charge", "まだ決まっていない": "undecided", "いまは分からない": "unknown_now",
               "理由を言わなかった": "unspecified", "決まりで言えない": "policy", "答えたくない": "unwilling"},
    "kind": {"分かる人を聞く": "ask_who", "分かる時を聞く": "ask_when", "日を改める": "retry", "結論として終える": "conclude"},
    "retry_key": {"会話に至らなかった": "open", "一部だけ取れた": "incomplete", "不在": "not_reached_absent",
                  "取り次いでもらえなかった": "not_reached_not_transferred", "かけ直しの時刻が決まっていない": "callback_unspecified_time"},
    "action": {"かける": "call", "終える": "end"},
    "required": {"はい": True, "○": True, "いいえ": False, "": False},
}
ALL_QUESTIONS = "全部"            # 「わからない・答えたくない」の質問ID：必須の質問すべて
OTHER_ANSWER = "その他"           # 分岐の回答条件：受け皿
END_LABEL = "終わり"              # 分岐の次の質問ID：終わり
_SPLIT = re.compile(r"[／\n]")    # 選択肢・断りの答えの区切り（全角の「／」か、欄の中の改行）
_INTERVAL = re.compile(r"^\s*(\d+)\s*(時間おき|日後)\s*$")
_QUOTED = re.compile(r"「([^「」]{1,40})」")


@dataclass
class Preview:
    raw: dict[str, Any] | None                    # 業務設定の形（読めなかったら None）
    problems: list[str]                           # 直すこと（シート・行つき）。空なら登録できる
    names_to_confirm: list[dict[str, str]]        # 人が読みを確かめる名前（表記・読み・出どころ）
    maybe_names: list[str] = field(default_factory=list)   # 「」の中の、名前の一覧に無い言葉（名前なら「名前」のシートに足す）
    digest: str = ""                              # 登録のときに同じ中身かを確かめる指紋


def _sheets(file_name: str, data: bytes, mapping: dict[str, Any]) -> tuple[dict[str, tuple[Sheet, dict[str, int]]], list[str]]:
    """役目ごとのシートと列の位置。mapping＝{"sheets": {役目: シートの名前}, "columns": {"役目.列の役目": 見出し}}（無い所は標準）。"""
    out: dict[str, tuple[Sheet, dict[str, int]]] = {}
    problems: list[str] = []
    for role, (default_name, columns, required) in SHEETS.items():
        name = mapping.get("sheets", {}).get(role, default_name)
        try:
            sheet = read(file_name, data, sheet_name=name)
        except FileRejected as e:
            if str(e) == "指定のシートがありません" and not required:
                continue                                   # 無くてもよいシート（分岐・言ってはいけないこと・名前）
            problems.append(f"シート「{name}」：{e}")
            continue
        cols: dict[str, int] = {}
        for col, default_title in columns.items():
            title = mapping.get("columns", {}).get(f"{role}.{col}", default_title)
            hits = [i for i, h in enumerate(sheet.header) if h is not None and h.strip() == title]
            if len(hits) != 1:
                problems.append(f"シート「{name}」：見出し「{title}」の列が{'ありません' if not hits else '2つ以上あります'}")
                continue
            cols[col] = hits[0]
        out[role] = (sheet, cols)
    return out, problems


def _rows(sheets: dict[str, tuple[Sheet, dict[str, int]]], role: str, problems: list[str]) -> list[tuple[int, dict[str, str | None]]]:
    """そのシートの、空でない行（行番号と、列の役目 → 文字）。値を信じられない欄（エラーの値など）は、問題に足す。"""
    if role not in sheets:
        return []
    sheet, cols = sheets[role]
    out = []
    for r in sheet.rows:
        if r.is_empty:
            continue
        if not r.width_ok:
            problems.append(f"シート「{sheet.sheet_name}」の {r.row_no} 行目：列の数が見出しと合いません")
            continue
        values = {}
        for col, i in cols.items():
            c = r.cell(i)
            if c.problem:
                problems.append(f"シート「{sheet.sheet_name}」の {r.row_no} 行目「{sheet.header[i]}」：{c.problem}")
            values[col] = c.text.strip() if c.text else None
        out.append((r.row_no, values))
    return out


def _label(kind: str, value: str | None, where: str, problems: list[str]) -> Any:
    table = LABELS[kind]
    v = "" if value is None else value
    if v not in table:
        problems.append(f"{where}：「{value or ''}」は使えません（{[k for k in table if k]} のどれか）")
        return None
    return table[v]


def _split(value: str | None) -> list[str] | None:
    return None if not value else [x.strip() for x in _SPLIT.split(value) if x.strip()]


def _to_raw(sheets: dict[str, tuple[Sheet, dict[str, int]]], problems: list[str]) -> tuple[dict[str, Any], list[str]]:
    basic: dict[str, str | None] = {}
    for row_no, v in _rows(sheets, "basic", problems):
        item = BASIC_ITEMS.get(v.get("item") or "")
        if item is None:
            problems.append(f"シート「基本」の {row_no} 行目：項目「{v.get('item')}」は使えません（{list(BASIC_ITEMS)}）")
        elif item in basic:
            problems.append(f"シート「基本」：項目「{v.get('item')}」が2つあります")
        else:
            basic[item] = v.get("value")
    tkind = _label("target_kind", basic.get("target_kind"), "シート「基本」の目的の人", problems)

    questions, decline = [], {}
    for row_no, v in _rows(sheets, "questions", problems):
        where = f"シート「質問」の {row_no} 行目"
        qtype = _label("type", v.get("type"), f"{where}の型", problems)
        required = _label("required", v.get("required"), f"{where}の必須", problems)
        questions.append({"id": v.get("id"), "text": v.get("text"), "ask_about": v.get("ask_about"), "type": qtype,
                          "required": required, "choices": _split(v.get("choices")) if qtype == "choice" else None,
                          "next": None})
        if v.get("decline"):
            decline[v.get("id")] = _split(v.get("decline"))
    by_id = {q["id"]: q for q in questions}
    for row_no, v in _rows(sheets, "branches", problems):
        q = by_id.get(v.get("id"))
        if q is None:
            problems.append(f"シート「分岐」の {row_no} 行目：質問ID {v.get('id')} は「質問」のシートにありません")
            continue
        answer = business_settings.ANY if v.get("answer") == OTHER_ANSWER else v.get("answer")
        nxt = business_settings.END if v.get("next") == END_LABEL else v.get("next")
        q["next"] = q["next"] or {}
        if answer in q["next"]:
            problems.append(f"シート「分岐」の {row_no} 行目：質問 {v.get('id')} の回答条件「{v.get('answer')}」が2つあります")
        q["next"][answer] = nxt

    required_ids = [q["id"] for q in questions if q["required"]]
    handling: dict[str, dict[str, dict[str, Any]]] = {}
    specific: set[tuple[str, str, str]] = set()
    for row_no, v in _rows(sheets, "handling", problems):
        where = f"シート「わからない・答えたくない」の {row_no} 行目"
        state = _label("state", v.get("state"), f"{where}の答え", problems)
        reason = _label("reason", v.get("reason"), f"{where}の理由", problems)
        kind = _label("kind", v.get("kind"), f"{where}の扱い", problems)
        if None in (state, reason, kind):
            continue
        h = {"kind": kind} if kind != "conclude" else {"kind": kind, "conclusion": v.get("conclusion")}
        targets = required_ids if v.get("id") == ALL_QUESTIONS else [v.get("id")]
        for qid in targets:
            if qid != ALL_QUESTIONS and (v.get("id") == ALL_QUESTIONS and (qid, state, reason) in specific):
                continue                                   # 質問ごとの行が勝つ
            handling.setdefault(qid, {"dont_know": {}, "refused": {}})[state][reason] = h
            if v.get("id") != ALL_QUESTIONS:
                specific.add((qid, state, reason))

    retry: dict[str, Any] = {}
    for row_no, v in _rows(sheets, "retry", problems):
        where = f"シート「かけ直し」の {row_no} 行目"
        key = _label("retry_key", v.get("key"), f"{where}の結果", problems)
        action = _label("action", v.get("action"), f"{where}のかけ直すか", problems)
        if key is None or action is None:
            continue
        if action == "end":
            retry[key] = {"action": "end"}
            continue
        m = _INTERVAL.match(v.get("interval") or "")
        times = v.get("max_times")
        if m is None or not (times or "").isdigit():
            problems.append(f"{where}：かけるときは、回数（数）と間隔（「2時間おき」「1日後」の形）が要ります")
            continue
        retry[key] = {"action": "call", "max_times": int(times),
                      "interval": {"hours" if m.group(2) == "時間おき" else "days": int(m.group(1))}}

    forbidden = {}
    for row_no, v in _rows(sheets, "forbidden", problems):
        forbidden[v.get("key") or f"行{row_no}"] = {"say_not": v.get("say_not"), "correct": v.get("correct")}

    names = []
    pairs = [(basic.get("company"), basic.get("company_reading"), "caller"),
             (basic.get("person"), basic.get("person_reading"), "caller")]
    pairs += [(v.get("text"), v.get("reading"), "script") for _, v in _rows(sheets, "names", problems)]
    for text, reading, source in pairs:
        if text:                                   # 読みは無くてもよい（AI の読みの案を人が確かめる。`review`）
            names.append({"text": text, "reading": reading, "source": source, "confirmed": False})
    raw = {
        "purpose": basic.get("purpose"),
        "caller": {"company": {"text": basic.get("company"), "reading": basic.get("company_reading")},
                   "person": {"text": basic.get("person"), "reading": basic.get("person_reading")},
                   "purpose_line": basic.get("purpose_line")},
        "target_person": {"kind": tkind, "role": basic.get("target_role")},
        "busy_line": basic.get("busy_line"), "voice": basic.get("voice"),
        "questions": questions, "names": names,
        "rules": {"decline": decline, "handling": handling, "retry": retry, "forbidden": forbidden},
    }
    return raw, problems


def _field_types(tx: Tx) -> frozenset[str]:
    return frozenset(r["type_name"] for r in tx.all("SELECT type_name FROM salescall.field_types"))


ReadingSuggester = Callable[[list[str]], dict[str, str]]
"""名前の読みの案を出す処理（表記の並び → 表記ごとの読み）。AI（文字のモデル）で出すのは会話の担当 CL-1（Gemini のキーを
使うのは CL-1 だけ）。案は案：人が確かめる（2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」）。"""


def review(tx: Tx, raw: dict[str, Any], *, confirmed_names: Any = (), suggest_readings: ReadingSuggester | None = None,
           problems: list[str] | None = None) -> Preview:
    """業務設定の形を確かめる（どの入口も、ここを通る：ファイル・依頼主の画面から抜き出したもの・画面の入力）。何も書かない。
    - 名前の読み：人が確かめた読み（confirmed_names＝(表記, 読み) の並び。人が直した読みも、ここで渡す）＞入口が持ってきた読み＞
      AI の読みの案（suggest_readings）の順で使い、**人が確かめた読みと同じものだけ**を「確かめた」にする（確かめていない名前があれば
      登録できない。Intent.md 6-8）。名乗りの自社名・担当者名の読みも、名前の一覧と同じ読みにそろえる。
    - 「」の中の言葉で、名前の一覧に無いものを「名前かもしれない言葉」として示す（枠だけ）。"""
    problems = list(problems or [])
    raw = json.loads(json.dumps(raw, ensure_ascii=False))           # 受けたものを変えない
    confirmed = {t: r for t, r in confirmed_names}
    names = raw.get("names") if isinstance(raw.get("names"), list) else []
    missing = [n.get("text") for n in names if isinstance(n, dict) and n.get("text")
               and not n.get("reading") and n.get("text") not in confirmed]
    suggested = suggest_readings(missing) if suggest_readings and missing else {}
    to_confirm = []
    for n in names:
        if not isinstance(n, dict) or not n.get("text"):
            continue
        text = n["text"]
        given = n.get("reading")
        reading = confirmed.get(text) or given or suggested.get(text)
        n["reading"], n["confirmed"] = reading, reading is not None and confirmed.get(text) == reading
        if reading is None:
            problems.append(f"名前「{text}」の読みがありません（読みを入れるか、AI の読みの案を確かめてください）")
        elif not n["confirmed"]:
            to_confirm.append({"text": text, "reading": reading, "source": n.get("source"),
                               "by": "入口の読み" if given else "AI の読みの案"})
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    for key in ("company", "person"):
        c = caller.get(key)
        if isinstance(c, dict) and c.get("text"):
            same = next((n for n in names if isinstance(n, dict) and n.get("text") == c["text"]), None)
            if same is not None:
                c["reading"] = same["reading"]
    try:
        business_settings.check(raw, field_types=_field_types(tx))   # 確かめは1か所（業務の決まりも、この中）
    except business_settings.SettingsInvalid as e:
        problems += [p for p in e.problems if p not in problems]
    questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    texts = [q.get("text") or "" for q in questions if isinstance(q, dict)]
    texts += [caller.get("purpose_line") or "", raw.get("busy_line") or ""]
    known = {n.get("text") for n in names if isinstance(n, dict)}
    maybe = sorted({m for t in texts if isinstance(t, str) for m in _QUOTED.findall(t) if m not in known})
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return Preview(raw, problems, to_confirm, maybe, digest)


def preview(tx: Tx, file_name: str, data: bytes, *, mapping: dict[str, Any] | None = None,
            confirmed_names: Any = (), suggest_readings: ReadingSuggester | None = None) -> Preview:
    """ファイル（入口の1つ）を業務設定の形にして、確かめた結果を返す（何も書かない）。"""
    sheets, problems = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        return Preview(None, problems, [])
    raw, problems = _to_raw(sheets, problems)
    return review(tx, raw, confirmed_names=confirmed_names, suggest_readings=suggest_readings, problems=problems)


class PreviewChanged(Exception):
    """プレビューのあとで、ファイル・対応付け・名前の確かめが変わった（もう一度プレビューを確かめる）。"""


def register_reviewed(tx: Tx, business_id: UUID, p: Preview, *, expected_digest: str) -> UUID:
    """人が確かめたプレビューと同じ中身なら、新しい版を作る（どの入口も、ここを通る。1つの取引。問題が1つでもあれば何も書かない）。"""
    if p.digest != expected_digest:
        raise PreviewChanged("プレビューのあとで中身が変わりました。もう一度プレビューを確かめてください")
    if p.problems or p.raw is None:
        raise business_settings.SettingsInvalid(p.problems)
    return business_settings.save_version(tx, business_id, p.raw)


def register(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, expected_digest: str,
             mapping: dict[str, Any] | None = None, confirmed_names: Any = (),
             suggest_readings: ReadingSuggester | None = None) -> UUID:
    """ファイルの入口の登録：読み直して、プレビューと同じ中身なら版を作る。"""
    p = preview(tx, file_name, data, mapping=mapping, confirmed_names=confirmed_names, suggest_readings=suggest_readings)
    return register_reviewed(tx, business_id, p, expected_digest=expected_digest)
