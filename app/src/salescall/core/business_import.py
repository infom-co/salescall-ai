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
- 名前と読み（Intent.md 6-8。CL-1 の連絡帳 4-15・4-16）：台本（業務設定の文）から名前を洗い出して読みを決めるのは CL-1 の決め方
  （`readings.NameFinder`＝`names.decide`。管理者も契約者も確かめない。2026-10-16 ユーザー）。入口が名前と読みを持ってくれば、
  それを使う。決め方の順は core/readings.py の1か所。**読みが決まらない名前でも止めない**（考えられる読みとまず言ってみる読みを
  持って登録し、会話の中で確かめる）。**契約者が確かめるのは、検証モードの業務の、AI が決めた読みだけ**（「検証中はAIが認識した
  ものを私が確認します」。確かめるかを決めるのは business_settings の1か所）。**名乗りの担当者名の読みは必ず入口で入れる**
  （案C。AI では決めない）。
- 数の質問の単位（CL-1 の連絡帳 4-14）：入口が単位を持ってこなければ、単位を決める処理（`UnitDecider`。決め方は CL-1 の1か所）
  の答えを入れる。管理者も契約者も確かめない。決まらなければ空（補わない）。
- 質問の「聞くこと」（CL-1 の連絡帳 4-15・G-43）：入口が持ってこなければ、聞くことを決める処理（`TopicDecider`。CL-1 の
  `topics.decide`）の答えを入れる。管理者も契約者も確かめない。決まらなければ空（聞くことを言わずに聞き返す）。
- 「わからない・答えたくない」のシートは、質問ID「全部」の行で、必須の質問すべての扱いを1度に書ける（質問ごとの行があれば、
  そちらが勝つ）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from salescall.core import business_rules, business_settings, readings
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
    "answers": ("想定問答", {"name": "問いの名前", "text": "答え"}, False),   # お客様に聞かれたら答えてよいこと（CL-1 の 4-26）
}
BASIC_ITEMS = {"業務の目的": "purpose", "お客様の区分": "target_class", "自社名": "company", "自社名の読み": "company_reading", "担当者名": "person",
               "担当者名の読み": "person_reading", "用件の一文": "purpose_line", "目的のお客様": "target_kind",
               "目的のお客様の役職": "target_role", "忙しいと言われたときの一言": "busy_line",
               "留守番電話に残す伝言": "voicemail_message", "「AIですか」への答え": "ai_answer", "声の型": "voice",
               # 相手の申し出への応じ方と業務の情報（CL-1 の連絡帳 4-28。書かなければ既定＝ユーザー決定）
               "代わりの方への応じ方": "substitute_policy", "伝言への応じ方": "message_policy", "伝言の文": "message",
               "在籍していないときの応じ方": "departed_policy", "業務の情報": "info",
               "担当から改めて連絡すると申し出てよいか": "follow_up"}
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
                  "かけ直しの時刻が決まっていない": "callback_unspecified_time"},
    "action": {"かける": "call", "終える": "end"},
    # 相手の申し出への応じ方（business_settings.POLICIES の値。空なら書かない＝既定）
    "substitute_policy": {"判断いただけるお立場の方なら話す": "decision_maker", "目的のお客様だけと話す": "target_only",
                          "どなたとでも話す": "anyone"},
    "message_policy": {"お礼を言って断り戻る時刻を聞く": "decline", "伝言を残す": "leave"},
    "departed_policy": {"後任の方をお呼びする": "successor", "お礼を言って終える": "end"},
    "follow_up": {"はい": True, "いいえ": False},
    "required": {"はい": True, "○": True, "いいえ": False, "": False},
}
# かけ直しのシートで、システムが決める結果（業務では変えない＝business_rules.SYSTEM_POLICIES）。前のひな形にはこの行があったので、
# 問題にせず読み飛ばし、知らせに出す（⛔ 2026-10-22 の見直し13：前は「使えない結果」として登録を止めていた）
SYSTEM_RETRY_ROWS = {"取り次いでもらえなかった": "日を改めて1回"}
ALL_QUESTIONS = "全部"            # 「わからない・答えたくない」の質問ID：必須の質問すべて
OTHER_ANSWER = "その他"           # 分岐の回答条件：受け皿（その質問に「その他」という選択肢があれば、その選択肢＝下の ANY_LABEL で受け皿）
ANY_LABEL = "＊"                  # 分岐の回答条件：いつでも受け皿
END_LABEL = "終わり"              # 分岐の次の質問ID：終わり
_SPLIT = re.compile(r"[／\n]")    # 選択肢・断りの答えの区切り（全角の「／」か、欄の中の改行）
_INTERVAL = re.compile(r"^\s*(\d+)\s*(時間おき|日後)\s*$")


@dataclass
class Preview:
    raw: dict[str, Any] | None                    # 業務設定の形（読めなかったら None）
    problems: list[str]                           # 直すこと（シート・行つき）。空なら登録できる
    names_to_confirm: list[dict[str, str]]        # 契約者が読みを確かめる名前（検証モードの、AI が決めた読み。表記・読み・出どころ）
    digest: str = ""                              # 登録のときに同じ中身かを確かめる指紋
    ai: dict[str, Any] = field(default_factory=dict)   # AI の答え（登録にそのまま渡す。聞き直さない）
    notes: list[str] = field(default_factory=list)     # 知らせ（登録は止めない。例：無くてもよいシートが無かった）


def _sheets(file_name: str, data: bytes, mapping: dict[str, Any]
            ) -> tuple[dict[str, tuple[Sheet, dict[str, int]]], list[str], list[str]]:
    """役目ごとのシートと列の位置。mapping＝{"sheets": {役目: シートの名前}, "columns": {"役目.列の役目": 見出し}}（無い所は標準）。"""
    out: dict[str, tuple[Sheet, dict[str, int]]] = {}
    problems: list[str] = []
    notes: list[str] = []
    for role, (default_name, columns, required) in SHEETS.items():
        mapped = role in mapping.get("sheets", {})
        name = mapping.get("sheets", {}).get(role, default_name)
        try:
            sheet = read(file_name, data, sheet_name=name)
        except FileRejected as e:
            if str(e) == "指定のシートがありません" and not required and not mapped:
                # 無くてもよいシート（分岐・言ってはいけないこと・名前）：無いものとして読み、プレビューに知らせる（黙って
                # 読み飛ばさない。シートの名前の打ち違いに気づけるように。2026-10-16 の見直し I5）。対応付けで名前を
                # 指定したシートが無ければ、問題（下）
                notes.append(f"シート「{name}」がありません（無いものとして読みました。シートの名前を確かめてください）")
                continue
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
    return out, problems, notes


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


def _responses(basic: dict[str, str | None], problems: list[str]) -> dict[str, Any]:
    """シート「基本」の、相手の申し出への応じ方・伝言の文・業務の情報（1行に1つ）・改めて連絡すると申し出てよいか。書いていない
    項目は入れない（業務設定の確かめが既定にする＝ユーザー決定の既定）。"""
    out: dict[str, Any] = {}
    for key in ("substitute_policy", "message_policy", "departed_policy", "follow_up"):
        if basic.get(key):
            v = _label(key, basic.get(key), f"シート「基本」の{_basic_name(key)}", problems)
            if v is not None:
                out[key] = v
    if basic.get("message"):
        out["message"] = basic["message"]
    if basic.get("info"):
        out["info"] = [x.strip() for x in basic["info"].split("\n") if x.strip()]
    return out


def _basic_name(key: str) -> str:
    return next(k for k, v in BASIC_ITEMS.items() if v == key)


def _policy_words(p: business_rules.Policy) -> str:
    """方針を、ひな形の言葉で書く（知らせの文）。"""
    if p.action == "end":
        return "終える"
    unit, n = p.interval
    return f"かける・{p.max_times}回・{n}{'時間おき' if unit == 'hours' else '日後'}"


def _label(kind: str, value: str | None, where: str, problems: list[str]) -> Any:
    table = LABELS[kind]
    v = "" if value is None else value
    if v not in table:
        problems.append(f"{where}：「{value or ''}」は使えません（{[k for k in table if k]} のどれか）")
        return None
    return table[v]


def _split(value: str | None) -> list[str] | None:
    return None if not value else [x.strip() for x in _SPLIT.split(value) if x.strip()]


def _to_raw(sheets: dict[str, tuple[Sheet, dict[str, int]]], problems: list[str], notes: list[str] | None = None
            ) -> tuple[dict[str, Any], list[str], dict[str, list[str]]]:
    """シートを業務設定の形にする。3つ目は、形の並びの何番目が、どのシートの何行目か（問題の位置の書き換えに使う＝`_placed`）。
    notes には、登録は止めない知らせを足す（システムが決めるので読み飛ばした行など）。"""
    notes = [] if notes is None else notes
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
    where_q: list[str] = []
    for row_no, v in _rows(sheets, "questions", problems):
        where = f"シート「質問」の {row_no} 行目"
        where_q.append(where)
        qtype = _label("type", v.get("type"), f"{where}の型", problems)
        required = _label("required", v.get("required"), f"{where}の必須", problems)
        questions.append({"id": v.get("id"), "text": v.get("text"), "ask_about": v.get("ask_about"), "type": qtype,
                          "required": required, "choices": _split(v.get("choices")) if qtype == "choice" else None,
                          "next": None, "unit": None, "meanings": None, "replies": None})
        if v.get("decline"):
            decline[v.get("id")] = _split(v.get("decline"))
    by_id = {q["id"]: q for q in questions}
    for row_no, v in _rows(sheets, "branches", problems):
        q = by_id.get(v.get("id"))
        if q is None:
            problems.append(f"シート「分岐」の {row_no} 行目：質問ID {v.get('id')} は「質問」のシートにありません")
            continue
        # 「その他」は受け皿。ただし、その質問に「その他」という選択肢があれば、その選択肢（受け皿は「＊」で書く）。
        # 前は、いつも受け皿にしていたので、選択肢の「その他」へ分けられず、「わからない」「答えたくない」まで「その他」の
        # 行き先へ行った（2026-10-16 の見直し I6）
        literal = OTHER_ANSWER in (q.get("choices") or [])
        answer = business_settings.ANY if v.get("answer") == ANY_LABEL or (v.get("answer") == OTHER_ANSWER and not literal) \
            else v.get("answer")
        nxt = business_settings.END if v.get("next") == END_LABEL else v.get("next")
        q["next"] = q["next"] or {}
        if answer in q["next"]:
            problems.append(f"シート「分岐」の {row_no} 行目：質問 {v.get('id')} の回答条件「{v.get('answer')}」が2つあります")
        q["next"][answer] = nxt

    required_ids = [q["id"] for q in questions if q["required"]]
    handling: dict[str, dict[str, dict[str, Any]]] = {}
    specific: set[tuple[str, str, str]] = set()
    seen_handling: dict[tuple[Any, str, str], int] = {}
    for row_no, v in _rows(sheets, "handling", problems):
        where = f"シート「わからない・答えたくない」の {row_no} 行目"
        state = _label("state", v.get("state"), f"{where}の答え", problems)
        reason = _label("reason", v.get("reason"), f"{where}の理由", problems)
        kind = _label("kind", v.get("kind"), f"{where}の扱い", problems)
        if None in (state, reason, kind):
            continue
        h = {"kind": kind} if kind != "conclude" else {"kind": kind, "conclusion": v.get("conclusion")}
        row_key = (v.get("id"), state, reason)
        if row_key in seen_handling:   # 同じ質問・答え・理由の行が2つ：黙って後ろの行にしない（2026-10-16 の見直し I7）
            problems.append(f"{where}：質問 {v.get('id')} の「{v.get('state')}」「{v.get('reason')}」の行が2つあります"
                            f"（{seen_handling[row_key]} 行目と）")
            continue
        seen_handling[row_key] = row_no
        targets = required_ids if v.get("id") == ALL_QUESTIONS else [v.get("id")]
        for qid in targets:
            if qid != ALL_QUESTIONS and (v.get("id") == ALL_QUESTIONS and (qid, state, reason) in specific):
                continue                                   # 質問ごとの行が勝つ
            handling.setdefault(qid, {"dont_know": {}, "refused": {}})[state][reason] = h
            if v.get("id") != ALL_QUESTIONS:
                specific.add((qid, state, reason))

    retry: dict[str, Any] = {}
    seen_retry: dict[str, int] = {}
    named_retry: set[str] = set()                  # 行のある結果（形の誤りがあっても）
    for row_no, v in _rows(sheets, "retry", problems):
        where = f"シート「かけ直し」の {row_no} 行目"
        if v.get("key") in SYSTEM_RETRY_ROWS:
            notes.append(f"{where}：結果「{v.get('key')}」は、システムが決めるので読み飛ばしました"
                         f"（{SYSTEM_RETRY_ROWS[v.get('key')]}）")
            continue
        key = _label("retry_key", v.get("key"), f"{where}の結果", problems)
        action = _label("action", v.get("action"), f"{where}のかけ直すか", problems)
        if key is not None:
            named_retry.add(key)
        if key is None or action is None:
            continue
        if key in seen_retry:
            problems.append(f"{where}：結果「{v.get('key')}」の行が2つあります（{seen_retry[key]} 行目と）")
            continue
        seen_retry[key] = row_no
        if action == "end":
            retry[key] = {"action": "end"}
            continue
        m = _INTERVAL.match(v.get("interval") or "")
        times = v.get("max_times")
        if m is None or not (times or "").isdecimal() or int(times) < 1 or int(m.group(1)) < 1:
            problems.append(f"{where}：かけるときは、回数（1以上の数）と間隔（「2時間おき」「1日後」の形。1以上）が要ります")
            continue
        retry[key] = {"action": "call", "max_times": int(times),
                      "interval": {"hours" if m.group(2) == "時間おき" else "days": int(m.group(1))}}
    for key, p in business_rules.DEFAULT_POLICIES.items():
        if key not in named_retry:         # 行が無いときだけ（行があって形が違えば、上で問題に出している）
            retry[key] = business_rules.policy_raw(p)
            name = next(k for k, v in LABELS["retry_key"].items() if v == key)
            notes.append(f"シート「かけ直し」：結果「{name}」の行が無いので、初めの値（{_policy_words(p)}）で入れました")

    forbidden: dict[str, Any] = {}
    seen_forbidden: dict[str, int] = {}
    for row_no, v in _rows(sheets, "forbidden", problems):
        key = v.get("key") or f"行{row_no}"
        if key in seen_forbidden:
            problems.append(f"シート「言ってはいけないこと」の {row_no} 行目：名前「{key}」が2つあります（{seen_forbidden[key]} 行目と）")
            continue
        seen_forbidden[key] = row_no
        forbidden[key] = {"say_not": v.get("say_not"), "correct": v.get("correct")}

    answers, where_a = [], []
    for row_no, v in _rows(sheets, "answers", problems):   # 形と重なりの確かめは業務設定の1か所（business_settings._answers）
        answers.append({"name": v.get("name"), "text": v.get("text")})
        where_a.append(f"シート「想定問答」の {row_no} 行目")

    names, where_n = [], []
    pairs = [(basic.get("company"), basic.get("company_reading"), "caller", "シート「基本」の自社名"),
             (basic.get("person"), basic.get("person_reading"), "caller", "シート「基本」の担当者名")]
    pairs += [(v.get("text"), v.get("reading"), "script", f"シート「名前」の {row_no} 行目")
              for row_no, v in _rows(sheets, "names", problems)]
    for text, reading, source, where in pairs:
        if text:                                   # 読みは無くてもよい（AI の読みの案を使う。`review`）
            names.append({"text": text, "reading": reading, "source": source, "reading_by": "given" if reading else None,
                          "candidates": [], "likely": None})
            where_n.append(where)
    raw = {
        "purpose": basic.get("purpose"), "target_class": tclass,
        "caller": {"company": {"text": basic.get("company"), "reading": basic.get("company_reading")},
                   "person": {"text": basic.get("person"), "reading": basic.get("person_reading")},
                   "purpose_line": basic.get("purpose_line")},
        "target_person": {"kind": tkind, "role": basic.get("target_role")},
        "busy_line": basic.get("busy_line"), "voicemail_message": basic.get("voicemail_message"),
        "ai_answer": basic.get("ai_answer"), "answers": answers, "voice": basic.get("voice"),
        **_responses(basic, problems),
        "questions": questions, "names": names,
        "rules": {"decline": decline, "handling": handling, "retry": retry, "forbidden": forbidden},
    }
    return raw, problems, {"名前": where_n, "質問": where_q, "想定問答": where_a}


_POSITION = re.compile(r"(名前|質問|想定問答) (\d+) 番目")


def _placed(problems: list[str], where: dict[str, list[str]]) -> list[str]:
    """業務設定の確かめ（1か所）の問題の「名前 n 番目」「質問 n 番目」を、ファイルのシートと行に書き換える
    （前は、名前の一覧の頭に自社名・担当者名が入るので「名前」シートの1行目が「名前 3 番目」と出た。2026-10-16 の見直し I10）。"""
    def place(m: re.Match) -> str:
        rows = where.get(m.group(1), [])
        i = int(m.group(2)) - 1
        return rows[i] if 0 <= i < len(rows) else m.group(0)   # AI が足した名前など、ファイルに無いものはそのまま
    return [_POSITION.sub(place, p) for p in problems]


def _field_types(tx: Tx) -> frozenset[str]:
    return frozenset(r["type_name"] for r in tx.all("SELECT type_name FROM salescall.field_types"))


NameFinder = readings.NameFinder                    # 台本から名前と読みを出す処理（正本は core/readings.py。作るのは CL-1）

UnitDecider = Callable[[list[str]], dict[str, str]]
"""数の質問の単位を決める処理（質問の文の並び → 文ごとの単位。決まらない文は入れないか空）。決め方は CL-1 の1か所
（CL-1 の連絡帳 4-14。質問の文の数える言葉か、文字のモデル2つが合うときだけ）。管理者も契約者も確かめない。"""


def _is_name(n: Any) -> bool:
    """名前の一覧の1つとして読める形か（表記が文字）。読めない形は、そのまま残して、確かめ（check）が問題に出す。"""
    return isinstance(n, dict) and isinstance(n.get("text"), str) and bool(n["text"].strip())


def _names(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in (raw.get("names") if isinstance(raw.get("names"), list) else []) if _is_name(n)]


TopicDecider = Callable[[list[str]], dict[str, str]]
"""質問の「聞くこと」（聞き返しの言い方に使う）を決める処理（質問の文の並び → 文ごとの聞くこと。決まらない文は入れないか空）。
決め方は CL-1 の1か所（`topics.decide`：質問の文の形の決まりか、文字のモデル2つが合うときだけ。G-43。CL-1 の連絡帳 4-15）。
管理者も契約者も確かめない。"""


def _topic_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and not q.get("ask_about") and isinstance(q.get("text"), str)
            and q["text"].strip()]


MeaningDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[dict[str, str]]]
"""はい／いいえ・選択肢の質問の「意味の言葉」を決める処理（(質問の文, 選択肢の並び) の並び → 同じ並びで {選択肢: 意味の言葉}。
決まらない選択肢は入れない）。決め方は CL-1 の1か所（`meanings.by_model` を2つのモデルで1回ずつ → `meanings.decide`。CL-1 の
連絡帳 4-17）。管理者も契約者も確かめない。"""

ReplyDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[dict[str, str]]]
"""はい／いいえ・選択肢の質問の「曖昧な言葉の意味」を決める処理（(質問の文, 選択肢の並び) の並び → 同じ並びで {言葉: 選択肢}。
決まらない言葉は入れない）。決め方は CL-1 の1か所（`meanings.by_model_replies` を2つのモデルで1回ずつ → `meanings.decide_replies`。
CL-1 の連絡帳 4-17）。管理者も契約者も確かめない。"""


def _choice_questions(raw: dict[str, Any], key: str) -> list[tuple[dict[str, Any], tuple[str, ...]]]:
    """意味の言葉（key＝meanings）・曖昧な言葉の意味（replies）が、まだ入口から来ていない選択肢の質問と、その選択肢。"""
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    out = []
    for q in qs:
        if isinstance(q, dict) and q.get("type") in business_settings.CHOICE_TYPES and not q.get(key) \
                and isinstance(q.get("text"), str) and q["text"].strip():
            choices = business_settings.declared_choices(q["type"], q.get("choices"))
            if choices:
                out.append((q, choices))
    return out


def _pair_key(text: str, choices: tuple[str, ...]) -> str:
    """AI の答えの鍵（質問の文と選択肢の組。同じ文でも選択肢が違えば別）。"""
    return json.dumps([text, list(choices)], ensure_ascii=False)


def _decided_pairs(raw: dict[str, Any], key: str, decide: MeaningDecider | ReplyDecider | None) -> dict[str, dict[str, str]]:
    """選択肢の質問ごとに決める処理に、1回だけ聞く（使える組だけ返す＝`business_settings.valid_answer_words`。答えが無い・形が
    違うときは空：会話は止まらない＝空なら選択肢の名前で言い直し、曖昧な言葉は通話の中で確かめる）。"""
    pairs = list(dict.fromkeys((q["text"], c) for q, c in _choice_questions(raw, key)))
    if not pairs or decide is None:
        return {}
    got = decide(pairs)
    if not isinstance(got, list) or len(got) != len(pairs):
        return {}
    out = {}
    for (text, choices), d in zip(pairs, got):
        good = business_settings.valid_answer_words(d, choices, replies=key == "replies")
        if good:
            out[_pair_key(text, choices)] = good
    return out


def _decided(texts: list[str], decide: Callable[[list[str]], dict[str, str]] | None) -> dict[str, str]:
    """文ごとに決める処理に、1回だけ聞く（決まった答えだけ返す）。"""
    if not texts or decide is None:
        return {}
    return {t: u.strip() for t, u in decide(texts).items() if t in texts and isinstance(u, str) and u.strip()}


def _unit_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and q.get("type") == business_settings.UNIT_TYPE
            and not q.get("unit") and isinstance(q.get("text"), str) and q["text"].strip()]


def _script(raw: dict[str, Any]) -> str:
    """名前を洗い出す台本（業務設定の文をまとめたもの。名乗り・用件・目的・忙しいときの一言・留守番電話の伝言・「AIですか」への答え・
    想定問答の答え（録音で話すので、名前の読みが要る）・質問の文・入口の名前）。"""
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    parts = [((caller.get("company") or {}).get("text") if isinstance(caller.get("company"), dict) else None),
             caller.get("purpose_line"), raw.get("purpose"), raw.get("busy_line"), raw.get("voicemail_message"),
             raw.get("ai_answer"), raw.get("message")]   # 伝言の文も録音で話す（CL-1 の連絡帳 4-28）
    parts += [q.get("text") for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else [])
              if isinstance(q, dict)]
    parts += [a.get("text") for a in (raw.get("answers") if isinstance(raw.get("answers"), list) else []) if isinstance(a, dict)]
    parts += [n["text"] for n in _names(raw) if n.get("source") == "script"]
    return "\n".join(p for p in parts if isinstance(p, str) and p.strip())


def ask_ai(raw: dict[str, Any], *, find_names: NameFinder | None = None, decide_units: UnitDecider | None = None,
           decide_topics: TopicDecider | None = None, decide_meanings: MeaningDecider | None = None,
           decide_replies: ReplyDecider | None = None) -> dict[str, Any]:
    """業務設定の形について、AI（名前と読み・単位・聞くこと・選択肢の意味の言葉・曖昧な言葉の意味を決める処理。どれも CL-1 の
    決め方）に1回だけ聞く。答え＝{"names": 表記 → 読み（readings.to_json の形）, "units": 質問の文 → 単位, "topics": 質問の文 →
    聞くこと, "meanings": 質問の文と選択肢 → {選択肢: 意味の言葉}, "replies": 質問の文と選択肢 → {言葉: 選択肢}}。
    **プレビューで聞いた答えを、登録でもそのまま使う**（もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になる）。"""
    return {"names": readings.to_json(readings.find(_script(raw), find_names)),
            "units": _decided(sorted({q["text"] for q in _unit_questions(raw)}), decide_units),
            "topics": _decided(sorted({q["text"] for q in _topic_questions(raw)}), decide_topics),
            "meanings": _decided_pairs(raw, "meanings", decide_meanings),
            "replies": _decided_pairs(raw, "replies", decide_replies)}


def review(tx: Tx, business_id: UUID, raw: dict[str, Any], *, confirmed_names: Any = (),
           ai: dict[str, Any] | None = None, problems: list[str] | None = None,
           where: dict[str, list[str]] | None = None) -> Preview:
    """業務設定の形を確かめる（どの入口も、ここを通る：ファイル・依頼者の画面から抜き出したもの・画面の入力）。何も書かない。
    AI には聞かない（AI の答えは `ask_ai` で1回だけ聞いて、ai に渡す）。
    - 名前と読み：入口の名前に、AI が洗い出した名前を足す（同じ表記は1つ）。読みの決め方は core/readings.py の1か所（入口の読み
      ＞契約者が確かめた読み＞AI が決めた読み＞決まらない＝候補つき）。検証モードの業務では、AI が決めた読みを契約者が確かめる
      （確かめていなければ問題に出る）。名乗りの自社名・担当者名の読みは、名前の一覧の読みの写し。
    - 数の質問の単位：入口が持ってこなければ、AI の答えの単位を入れる（無ければ空）。"""
    problems = list(problems or [])
    ai = ai or {}
    raw = json.loads(json.dumps(raw, ensure_ascii=False))           # 受けたものを変えない
    verification = business_settings.ai_reading_needs_person(tx, business_id)
    confirmed = {t: r for t, r in confirmed_names}
    found = readings.from_json(ai.get("names"))
    # 名前の一覧は、読めない形の行も順番どおり残す（落とすと、確かめが問題に出せず、問題の位置もずれる。2026-10-17 の見直し）。
    # 一覧そのものが並びでなければ、AI の名前を足さず、そのまま確かめに渡す
    listed = isinstance(raw.get("names"), list) or raw.get("names") is None
    names = list(raw.get("names") or []) if listed else []
    have = {unicodedata.normalize("NFKC", n["text"]) for n in names if _is_name(n)}
    for text in sorted(found) if listed else ():                     # AI が洗い出した名前（入口に無いもの）
        if unicodedata.normalize("NFKC", text) not in have:
            names.append({"text": text, "reading": None, "source": "script"})
            have.add(unicodedata.normalize("NFKC", text))
    by_key = {unicodedata.normalize("NFKC", t): r for t, r in found.items()}
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    # 名乗りの読みは、確かめのたびに名前の一覧の読みを写す（下）。なので、名前の一覧で AI・契約者が決めた読みと同じなら、それは
    # 写し（入口の読みではない）。写しを入口の読みとして読むと、プレビューの形をもう一度確かめたとき、AI の読みが入口の読みに
    # なり、検証モードの確かめ・担当者名の読みは必須の決まりが抜ける（2026-10-17 の見直し：I1 と同じ穴が名乗りから開いていた）
    copied = {n["text"]: readings.normalize(n.get("reading")) for n in names
              if _is_name(n) and n.get("reading_by") in ("ai", "person")}
    caller_given = {c["text"]: c["reading"] for c in (caller.get(k) for k in ("company", "person"))
                    if isinstance(c, dict) and isinstance(c.get("text"), str) and isinstance(c.get("reading"), str)
                    and c["reading"].strip()
                    and not (c["text"] in copied and copied[c["text"]] == readings.normalize(c["reading"]))}
    to_confirm = []
    for n in names:
        if not _is_name(n):
            continue
        # 入口が持ってきた読みは、読みを決めたものが入口（または無い）の読みだけ。プレビューの形をもう一度確かめたとき、
        # AI の読み・契約者が確かめた読みを入口の読みにしない（2026-10-16 の見直し I1：検証モードの確かめが抜けた）
        by = n.get("reading_by")
        given = n.get("reading") if by in (None, "given") else None
        ai_here = by_key.get(unicodedata.normalize("NFKC", n["text"]), readings.UNDECIDED)
        if by == "ai" and n.get("reading") and ai_here.reading is None:
            ai_here = readings.Reading(readings.normalize(n["reading"]), "ai")
        here_confirmed = dict(confirmed)
        if by == "person" and n.get("reading") and n["text"] not in here_confirmed:
            here_confirmed[n["text"]] = n["reading"]
        # 名乗りの自社名・担当者名に入口の読みがあれば、それが名前の読み（AI の読みで上書きしない＝I3）
        if n["text"] in caller_given:
            if given and readings.normalize(given) != readings.normalize(caller_given[n["text"]]):
                problems.append(f"名前「{n['text']}」の読みが、名乗りと名前の一覧で違います"
                                f"（「{caller_given[n['text']]}」と「{given}」）")
            given = given or caller_given[n["text"]]
        try:
            got = readings.resolve(n["text"], given, confirmed=here_confirmed, ai={n["text"]: ai_here})
        except ValueError as e:
            problems.append(str(e))
            got = readings.UNDECIDED
        n.update(got.as_json())
        if readings.needs_person(got, verification=verification):
            to_confirm.append({"text": n["text"], "reading": got.reading, "source": n.get("source")})
    if listed:
        raw["names"] = [{k: n.get(k) for k in business_settings.NAME_KEYS} if isinstance(n, dict) else n for n in names]
    for q in _unit_questions(raw):
        q["unit"] = ai.get("units", {}).get(q["text"]) or None
    for q in _topic_questions(raw):
        q["ask_about"] = ai.get("topics", {}).get(q["text"]) or None
    for key in ("meanings", "replies"):                              # 選択肢の質問：入口が持ってこなければ、AI の答えを入れる
        for q, choices in _choice_questions(raw, key):
            q[key] = (ai.get(key) or {}).get(_pair_key(q["text"], choices)) or None
    for key in ("company", "person"):
        c = caller.get(key)
        if isinstance(c, dict) and c.get("text"):
            same = next((n for n in names if _is_name(n) and n.get("text") == c["text"]), None)
            if same is not None:
                c["reading"] = same["reading"]
    try:
        business_settings.check(raw, field_types=_field_types(tx), ai_reading_needs_person=verification,   # 確かめは1か所
                                voice_types=business_settings.voice_types(tx))
    except business_settings.SettingsInvalid as e:
        problems += [p for p in e.problems if p not in problems]
    if where is not None:                                             # ファイルの入口：問題の位置をシートと行にする
        problems = _placed(problems, where)
    return Preview(raw, problems, to_confirm, _digest(business_id, raw, problems), ai)


def _digest(business_id: UUID, raw: dict[str, Any] | None, problems: list[str]) -> str:
    """プレビューと登録が同じ中身かを確かめる指紋（業務と中身と問題。1か所）。問題も入れる：確かめ（check）の外で見つかった問題
    （ファイルの読めない行・重なった行など。中身 raw には残らない）を、プレビューから消して登録できないように（2026-10-17 の見直し）。"""
    return hashlib.sha256(json.dumps({"business_id": str(business_id), "raw": raw, "problems": problems},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def preview(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, mapping: dict[str, Any] | None = None,
            confirmed_names: Any = (), find_names: NameFinder | None = None,
            decide_units: UnitDecider | None = None, decide_topics: TopicDecider | None = None,
            decide_meanings: MeaningDecider | None = None, decide_replies: ReplyDecider | None = None) -> Preview:
    """ファイル（入口の1つ）を業務設定の形にして、AI に1回だけ聞き、確かめた結果を返す（何も書かない）。
    返す Preview の ai（AI の答え）を、登録（`register`）にそのまま渡す。"""
    sheets, problems, notes = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        return Preview(None, problems, [], notes=notes)
    raw, problems, where = _to_raw(sheets, problems, notes)
    ai = ask_ai(raw, find_names=find_names, decide_units=decide_units, decide_topics=decide_topics,
                decide_meanings=decide_meanings, decide_replies=decide_replies)
    p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems, where=where)
    p.notes = notes
    return p


class PreviewChanged(Exception):
    """プレビューのあとで、ファイル・対応付け・名前の確かめが変わった（もう一度プレビューを確かめる）。"""


def register_reviewed(tx: Tx, business_id: UUID, p: Preview, *, expected_digest: str) -> UUID:
    """契約者が確かめたプレビューと同じ中身なら、新しい版を作る（どの入口も、ここを通る。1つの取引。問題が1つでもあれば何も書かない）。"""
    # 指紋は、渡されたものを信じず、中身から出し直す（プレビューは書き換えられる。業務も指紋に入れる。2026-10-16 の見直し I8）
    if _digest(business_id, p.raw, p.problems) != expected_digest:
        raise PreviewChanged("プレビューのあとで中身が変わりました。もう一度プレビューを確かめてください")
    if p.problems or p.raw is None:
        raise business_settings.SettingsInvalid(p.problems)
    return business_settings.save_version(tx, business_id, p.raw)


def register(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, expected_digest: str,
             mapping: dict[str, Any] | None = None, confirmed_names: Any = (),
             ai: dict[str, Any] | None = None) -> UUID:
    """ファイルの入口の登録：読み直して、プレビューと同じ中身なら版を作る。AI には聞き直さない（ai＝プレビューの AI の答え）。"""
    sheets, problems, _ = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        raise business_settings.SettingsInvalid(problems)
    raw, problems, where = _to_raw(sheets, problems)
    p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems, where=where)
    return register_reviewed(tx, business_id, p, expected_digest=expected_digest)
