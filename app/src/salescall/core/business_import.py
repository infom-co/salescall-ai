"""業務設定の取込（引き継ぎの (10)。Intent.md 6-1 の方式B・6-4・6-5・6-8）。持ち主 CL-2。

入口ごとに、業務設定の1つの形（core/business_settings.py の `check` が受ける形）にするだけ。入口は、Excel のファイル
（標準のひな形。シートと列の名前は、依頼者の形に合わせて対応付けられる＝`preview`）と、依頼者の画面から抜き出したもの・画面の
入力（同じ形を作って `review` に渡す。2026-10-16 ユーザー：「依頼主の画面から抽出する場合もある」）。**どの入口も `review`
（確かめ）と `register_reviewed`（登録）の同じ道を通る**。確かめそのものは business_settings の1か所。ファイルを読むのは
core/import_files.py。

流れ（Intent.md 6-5）：`preview`（何も書かない）⇒ 契約者が問題を直す（検証モードでは、AI の読みの案も確かめる）⇒ `register`
（プレビューと同じ中身のときだけ、新しい版を作る。全部か、何も無いか）。

- 標準のひな形の、シート・列・言葉の名前の正本は、この部品の `SHEETS`・`LABELS`（画面のひな形の書き出しも、これを読む）。
- **AI（読みの案・単位を決める処理）に聞くのは、プレビューの1回だけ**（`ask_ai`）。登録は、プレビューの答え（`Preview.ai`）を
  そのまま使う（もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になり、登録できなくなる）。
- 名前の読み（Intent.md 6-8）：決め方は core/readings.py の1か所。入口が読みを持ってくれば、それを使う。無ければ AI の読みの案
  （`ReadingSuggester`。作るのは CL-1）を使う。**契約者が確かめるのは、検証モードの業務の AI の案だけ**（2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」。
  確かめるかを決めるのは business_settings の1か所）。契約者が確かめた（直した）読みは、入口の読みの無い名前の AI の案に代わる（入口の読みは上書きしない）。質問の文などの「」の中の
  言葉で、名前の一覧に無いものは「名前かもしれない言葉」として示す（枠だけ。⏳ 候補の出し方は CL-1 が比べている。PHASE0 G-23）。
- 数の質問の単位（CL-1 の連絡帳 4-14）：入口が単位を持ってこなければ、単位を決める処理（`UnitDecider`。決め方は CL-1 の1か所）
  の答えを入れる。管理者も契約者も確かめない。決まらなければ空（補わない）。
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

from salescall.core import business_settings, readings
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
BASIC_ITEMS = {"業務の目的": "purpose", "お客様の区分": "target_class", "自社名": "company", "自社名の読み": "company_reading", "担当者名": "person",
               "担当者名の読み": "person_reading", "用件の一文": "purpose_line", "目的のお客様": "target_kind",
               "目的のお客様の役職": "target_role", "忙しいと言われたときの一言": "busy_line", "声の型": "voice"}
LABELS: dict[str, dict[str, str]] = {
    "type": {"日付": "date", "時刻": "time", "日時": "datetime", "数": "number", "金額": "amount", "名前": "name",
             "電話番号": "phone", "メール": "email", "住所": "address", "はい／いいえ": "yes_no", "選択肢": "choice",
             "自由な意見": "free_text"},
    "target_class": {"法人": "corporate", "個人": "individual"},
    "target_kind": {"役職のみ": "role", "役職と名前": "role_and_name", "名前のみ": "name", "誰でも": "anyone"},
    "state": {"わからない": "dont_know", "答えたくない": "refused"},
    "reason": {"担当ではない": "not_in_charge", "まだ決まっていない": "undecided", "いまは分からない": "unknown_now",
               "理由を言わなかった": "unspecified", "決まりで言えない": "policy", "答えたくない": "unwilling"},
    "kind": {"分かるお客様を聞く": "ask_who", "分かる時を聞く": "ask_when", "日を改める": "retry", "結論として終える": "conclude"},
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
    names_to_confirm: list[dict[str, str]]        # 契約者が読みを確かめる名前（検証モードの AI の案。表記・読み・出どころ）
    maybe_names: list[str] = field(default_factory=list)   # 「」の中の、名前の一覧に無い言葉（名前なら「名前」のシートに足す）
    digest: str = ""                              # 登録のときに同じ中身かを確かめる指紋
    ai: dict[str, dict[str, str]] = field(default_factory=dict)   # AI の答え（登録にそのまま渡す。聞き直さない）


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
    tkind = _label("target_kind", basic.get("target_kind"), "シート「基本」の目的のお客様", problems)
    tclass = _label("target_class", basic.get("target_class"), "シート「基本」のお客様の区分", problems)

    questions, decline = [], {}
    for row_no, v in _rows(sheets, "questions", problems):
        where = f"シート「質問」の {row_no} 行目"
        qtype = _label("type", v.get("type"), f"{where}の型", problems)
        required = _label("required", v.get("required"), f"{where}の必須", problems)
        questions.append({"id": v.get("id"), "text": v.get("text"), "ask_about": v.get("ask_about"), "type": qtype,
                          "required": required, "choices": _split(v.get("choices")) if qtype == "choice" else None,
                          "next": None, "unit": None})
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
        if text:                                   # 読みは無くてもよい（AI の読みの案を使う。`review`）
            names.append({"text": text, "reading": reading, "source": source, "reading_by": "given" if reading else None})
    raw = {
        "purpose": basic.get("purpose"), "target_class": tclass,
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


ReadingSuggester = readings.Suggester              # 名前の読みの案を出す処理（正本は core/readings.py。作るのは CL-1）

UnitDecider = Callable[[list[str]], dict[str, str]]
"""数の質問の単位を決める処理（質問の文の並び → 文ごとの単位。決まらない文は入れないか空）。決め方は CL-1 の1か所
（CL-1 の連絡帳 4-14。質問の文の数える言葉か、文字のモデル2つが合うときだけ）。管理者も契約者も確かめない。"""


def _names(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in (raw.get("names") if isinstance(raw.get("names"), list) else []) if isinstance(n, dict) and n.get("text")]


def _unit_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and q.get("type") == business_settings.UNIT_TYPE
            and not q.get("unit") and isinstance(q.get("text"), str) and q["text"].strip()]


def ask_ai(raw: dict[str, Any], *, confirmed_names: Any = (), suggest_readings: ReadingSuggester | None = None,
           decide_units: UnitDecider | None = None) -> dict[str, dict[str, str]]:
    """業務設定の形について、AI（と単位を決める処理）に1回だけ聞く。答え＝{"readings": 表記→読み, "units": 質問の文→単位}。
    **プレビューで聞いた答えを、登録でもそのまま使う**（もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になる）。"""
    confirmed = {t for t, _ in confirmed_names}
    texts = [n["text"] for n in _names(raw) if not n.get("reading") and n["text"] not in confirmed]
    units: dict[str, str] = {}
    to_decide = sorted({q["text"] for q in _unit_questions(raw)})
    if to_decide and decide_units is not None:
        units = {t: u.strip() for t, u in decide_units(to_decide).items()
                 if t in to_decide and isinstance(u, str) and u.strip()}
    return {"readings": readings.ask(texts, suggest_readings), "units": units}


def review(tx: Tx, business_id: UUID, raw: dict[str, Any], *, confirmed_names: Any = (),
           ai: dict[str, dict[str, str]] | None = None, problems: list[str] | None = None) -> Preview:
    """業務設定の形を確かめる（どの入口も、ここを通る：ファイル・依頼者の画面から抜き出したもの・画面の入力）。何も書かない。
    AI には聞かない（AI の答えは `ask_ai` で1回だけ聞いて、ai に渡す）。
    - 名前の読み：決め方は core/readings.py の1か所（入口の読み＞契約者が確かめた AI の案＞AI の案。どれで決めたかを
      `reading_by` に残す）。AI の案を契約者が確かめるのは検証モードの業務だけ（確かめていなければ問題に出る）。名乗りの自社名・担当者名の
      読みも、名前の一覧と同じ読みにそろえる。
    - 数の質問の単位：入口が持ってこなければ、AI の答えの単位を入れる（無ければ空）。
    - 「」の中の言葉で、名前の一覧に無いものを「名前かもしれない言葉」として示す（枠だけ）。"""
    problems = list(problems or [])
    ai = ai or {}
    raw = json.loads(json.dumps(raw, ensure_ascii=False))           # 受けたものを変えない
    verification = business_settings.ai_reading_needs_person(tx, business_id)
    confirmed = {t: r for t, r in confirmed_names}
    names = _names(raw)
    to_confirm = []
    for n in names:
        got = readings.resolve(n["text"], n.get("reading"), confirmed=confirmed, ai=ai.get("readings", {}))
        n["reading"], n["reading_by"] = got.reading, got.by
        if got.reading is None:
            problems.append(f"名前「{n['text']}」の読みがありません（読みを入れてください。AI の読みの案も出せませんでした）")
        elif readings.needs_person(got, verification=verification):
            to_confirm.append({"text": n["text"], "reading": got.reading, "source": n.get("source")})
    for q in _unit_questions(raw):
        q["unit"] = ai.get("units", {}).get(q["text"]) or None
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    for key in ("company", "person"):
        c = caller.get(key)
        if isinstance(c, dict) and c.get("text"):
            same = next((n for n in names if n.get("text") == c["text"]), None)
            if same is not None:
                c["reading"] = same["reading"]
    try:
        business_settings.check(raw, field_types=_field_types(tx), ai_reading_needs_person=verification)   # 確かめは1か所
    except business_settings.SettingsInvalid as e:
        problems += [p for p in e.problems if p not in problems]
    questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    texts = [q.get("text") or "" for q in questions if isinstance(q, dict)]
    texts += [caller.get("purpose_line") or "", raw.get("busy_line") or ""]
    known = {n.get("text") for n in names}
    maybe = sorted({m for t in texts if isinstance(t, str) for m in _QUOTED.findall(t) if m not in known})
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return Preview(raw, problems, to_confirm, maybe, digest, ai)


def preview(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, mapping: dict[str, Any] | None = None,
            confirmed_names: Any = (), suggest_readings: ReadingSuggester | None = None,
            decide_units: UnitDecider | None = None) -> Preview:
    """ファイル（入口の1つ）を業務設定の形にして、AI に1回だけ聞き、確かめた結果を返す（何も書かない）。
    返す Preview の ai（AI の答え）を、登録（`register`）にそのまま渡す。"""
    sheets, problems = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        return Preview(None, problems, [])
    raw, problems = _to_raw(sheets, problems)
    ai = ask_ai(raw, confirmed_names=confirmed_names, suggest_readings=suggest_readings, decide_units=decide_units)
    return review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems)


class PreviewChanged(Exception):
    """プレビューのあとで、ファイル・対応付け・名前の確かめが変わった（もう一度プレビューを確かめる）。"""


def register_reviewed(tx: Tx, business_id: UUID, p: Preview, *, expected_digest: str) -> UUID:
    """契約者が確かめたプレビューと同じ中身なら、新しい版を作る（どの入口も、ここを通る。1つの取引。問題が1つでもあれば何も書かない）。"""
    if p.digest != expected_digest:
        raise PreviewChanged("プレビューのあとで中身が変わりました。もう一度プレビューを確かめてください")
    if p.problems or p.raw is None:
        raise business_settings.SettingsInvalid(p.problems)
    return business_settings.save_version(tx, business_id, p.raw)


def register(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, expected_digest: str,
             mapping: dict[str, Any] | None = None, confirmed_names: Any = (),
             ai: dict[str, dict[str, str]] | None = None) -> UUID:
    """ファイルの入口の登録：読み直して、プレビューと同じ中身なら版を作る。AI には聞き直さない（ai＝プレビューの AI の答え）。"""
    sheets, problems = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        raise business_settings.SettingsInvalid(problems)
    raw, problems = _to_raw(sheets, problems)
    p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems)
    return register_reviewed(tx, business_id, p, expected_digest=expected_digest)
