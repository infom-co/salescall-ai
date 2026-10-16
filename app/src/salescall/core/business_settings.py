"""業務設定の版（引き継ぎの (10)。Intent.md 6章・DESIGN.md 3-2・6-3・6-4）。持ち主 CL-2。

**業務設定は1つの形（`check` が受ける形）だけ**。画面で入れても、ファイルから取り込んでも、この形にしてから同じ確かめを通す
（取込は、ファイルをこの形にするだけ。確かめを2つ作らない）。確かめに通らなければ、何も置かない（全部か、何も無いか）。

- 版を作る（`save_version`）：1つの取引で、版・質問（番号・並び・型・必須か）・業務の決まり（core/business_rules.py）・
  中身（名乗り・目的・目的の人・忙しいときの一言・声の型・質問の文と選択肢と分岐・名前の一覧）を書く。版は変えない。
  直すときは新しい版（`restore` は、前の版の中身から新しい版を作る＝前の版へ戻す）。
- 実施回に版を割り当てる（`assign`。記録つき。DESIGN.md 3-2）：割り当てたら、品質の段の始め方（core/quality_stage.py の
  `prepare`）を呼ぶ（模擬通話を待つか、最初の段を開く）。
- 確かめること（Intent.md 6-4）：形の誤り・必須の欄の不足／分岐先が無い／どこからも行かない質問（必須の質問へ行く道が無いを含む）／
  終わらない輪／受け皿（どの答えにも当たらないときの行き先）が無い／**必須の質問を飛ばして終わる道**（その答えが「断り」でも
  システムの答えでもないのに飛ばすと、判定が「一部だけ取れた」になり、断った相手にかけ直すことになる）／読みを確かめていない名前／
  必須の質問が1つも無い／選択肢の名前が、出力の印・システムが足す答えと同じ／断りの答えが選択肢に無い／業務の決まりの確かめ。
  「終了条件の矛盾」は、この形では起きない（終わりは、分岐の「終わり」か最後の質問の次の1つだけ）。
- 名前の洗い出し（Intent.md 6-8）は枠だけ：取込の処理が候補を出し、人がプレビューで読みを確かめる（確かめた印 confirmed）。
  ⏳ 候補の出し方（単語に分ける処理か、文字のモデルか）は CL-1 が比べている（PHASE0 G-23）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import business_rules, quality_stage
from salescall.core.access import Tx

END = "END"                                       # 分岐の行き先「終わり」
ANY = "*"                                         # 分岐の受け皿（ほかのどの答えでも）
SYSTEM_CHOICES = ("わからない", "答えたくない")      # システムがどの質問にも足す答え（CL-1 の連絡帳 2-1。名前の正本はここ）
VALUE_CHOICE = "答えた"                            # 値を聞く質問の選択肢（値そのものは項目の値に入る）
YES_NO_CHOICES = ("はい", "いいえ")
OUTPUT_MARKS = ("-", "✕")                         # 依頼主へ渡す印の既定（DESIGN.md 7-1b。依頼主ごとの印は出力の型で。⏳ 出力の型を作るとき足す）
TARGET_PERSON_KINDS = ("role", "role_and_name", "name", "anyone")
# 役職のみ（決裁権者を含む）／役職（部署）＋名前／名前のみ／その店・会社の人なら誰でも（Intent.md 6-2。2026-10-13 ユーザー）
NAME_SOURCES = ("caller", "script")               # 名乗り（自社名・担当者名）／トークスクリプトの文章（商品名・地名など）


class SettingsInvalid(Exception):
    """業務設定の形が違う・抜けがある（理由は日本語。すべての理由を並べて返す）。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Name:
    text: str
    reading: str
    source: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    ask_about: str | None        # 聞くこと（聞き返しの言い方に使う。無くてもよい。CL-1 の 4-10）
    type: str
    required: bool
    choices: tuple[str, ...]     # 依頼主が書いた選択肢（値を聞く質問は「答えた」、はい／いいえは「はい」「いいえ」）
    next: dict[str, str]         # 答え → 次の質問の番号か END（空＝並びの次へ）


@dataclass(frozen=True)
class Settings:
    purpose: str                                  # 業務の目的・達成目標
    company: Name                                 # 名乗りの自社名
    person: Name                                  # 名乗りの担当者名
    purpose_line: str | None                      # 用件の一文（空でもよい）
    target_person: dict[str, Any]                 # 目的の人 {kind, role}
    busy_line: str | None                         # 「忙しい」と言われたときの一言（空なら丁寧に終える）
    voice: str                                    # 声の型の名前（CL-1。Intent.md 9-8）
    questions: tuple[Question, ...]
    names: tuple[Name, ...]
    rules: business_rules.Rules | None            # 業務の決まり（版を作るまでは None。版の番号が要るため）
    raw: dict[str, Any]                           # 受けた形のまま（新しい版を作る＝戻すときに使う）

    def next_of(self, question_id: str, answer: str) -> str:
        """その質問でその答えのとき、次に行く質問の番号か END（決め方はここだけ。会話の流れの制御はこれを読む）。"""
        q = {x.id: x for x in self.questions}[question_id]
        if q.next:
            return q.next.get(answer, q.next.get(ANY, END))
        ids = [x.id for x in self.questions]
        i = ids.index(question_id)
        return ids[i + 1] if i + 1 < len(ids) else END


KEYS = {"purpose", "caller", "target_person", "busy_line", "voice", "questions", "names", "rules"}


def _text(v: Any, where: str, problems: list[str], *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where} が空です" if v in (None, "") else f"{where} は文字です")
        return None
    return v.strip()


def _name(v: Any, where: str, source: str, problems: list[str]) -> Name | None:
    if not isinstance(v, dict) or set(v) != {"text", "reading"}:
        problems.append(f"{where} は表記（text）と読み（reading）です")
        return None
    t, r = _text(v["text"], f"{where}の表記", problems), _text(v["reading"], f"{where}の読み", problems)
    return None if t is None or r is None else Name(t, r, source)


def possible_answers(q: Question) -> tuple[str, ...]:
    """その質問で返りうる答え（依頼主の選択肢＋システムが足す答え）。"""
    return q.choices + SYSTEM_CHOICES


def _choices(raw: Any, type_name: str, where: str, problems: list[str]) -> tuple[str, ...]:
    """型ごとの選択肢（決め方はここだけ）。決まった選択肢（choice）だけ依頼主が書く。ほかは型で決まる（書いてあれば同じであること）。"""
    fixed = {"yes_no": YES_NO_CHOICES}.get(type_name, None if type_name == "choice" else (VALUE_CHOICE,))
    if fixed is not None:
        if raw is not None and tuple(raw) != fixed:
            problems.append(f"{where}の選択肢は、型 {type_name} では {list(fixed)} に決まっています")
        return fixed
    if not isinstance(raw, list) or not raw or not all(isinstance(c, str) and c.strip() for c in raw):
        problems.append(f"{where}は決まった選択肢（choice）なので、選択肢を1つ以上書いてください")
        return ()
    names = tuple(c.strip() for c in raw)
    if len(set(names)) != len(names):
        problems.append(f"{where}の選択肢に、同じ名前が2つあります")
    return names


def check(raw: Any, *, field_types: frozenset[str]) -> Settings:
    """業務設定を確かめて読む（ただ1つの確かめ。業務の決まりは版を作るときに business_rules が確かめる）。
    合わなければ SettingsInvalid（見つけた理由をすべて並べる）。"""
    problems: list[str] = []
    if not isinstance(raw, dict) or set(raw) != KEYS:
        raise SettingsInvalid([f"業務設定は {sorted(KEYS)} の {len(KEYS)} つです"])
    purpose = _text(raw["purpose"], "業務の目的", problems)
    caller = raw["caller"]
    company = person = None
    purpose_line = None
    if not isinstance(caller, dict) or set(caller) != {"company", "person", "purpose_line"}:
        problems.append("名乗り（caller）は、自社名（company）・担当者名（person）・用件の一文（purpose_line）です")
    else:
        company = _name(caller["company"], "名乗りの自社名", "caller", problems)
        person = _name(caller["person"], "名乗りの担当者名", "caller", problems)
        purpose_line = _text(caller["purpose_line"], "用件の一文", problems, optional=True)
    # 目的の人：役職は、業務で1つ書く（role）か、書かずに発信対象データの役職の列を使う（None。取込で列があるかを確かめる）。
    # 名前のみ・誰でもは、役職を持たない
    tp = raw["target_person"]
    if not isinstance(tp, dict) or set(tp) != {"kind", "role"} or tp.get("kind") not in TARGET_PERSON_KINDS:
        problems.append(f"目的の人は、種類（kind：{list(TARGET_PERSON_KINDS)}）と役職（role）です")
        tp = {"kind": None, "role": None}
    elif tp["kind"] in ("name", "anyone") and tp["role"] is not None:
        problems.append("目的の人が「名前のみ」「誰でも」のときは、役職を書きません")
    elif tp["role"] is not None:
        _text(tp["role"], "目的の人の役職", problems)
    busy_line = _text(raw["busy_line"], "忙しいと言われたときの一言", problems, optional=True)
    voice = _text(raw["voice"], "声の型", problems)

    questions: list[Question] = []
    if not isinstance(raw["questions"], list) or not raw["questions"]:
        problems.append("質問が1つもありません")
    else:
        seen: set[str] = set()
        for n, q in enumerate(raw["questions"], 1):
            where = f"質問 {n} 番目"
            if not isinstance(q, dict) or set(q) != {"id", "text", "ask_about", "type", "required", "choices", "next"}:
                problems.append(f"{where}は id・text・ask_about・type・required・choices・next です")
                continue
            qid = q["id"]
            if not isinstance(qid, str) or not qid or len(qid) > 32 or not all(c.isascii() and (c.isalnum() or c in "_-")
                                                                              for c in qid) or qid == END:
                problems.append(f"{where}の番号 {qid!r} は、英数字・「_」「-」の 32 文字までです（END は使えません）")
                continue
            where = f"質問 {qid}"
            if qid in seen:
                problems.append(f"{where}が2つあります")
            seen.add(qid)
            if q["type"] not in field_types:
                problems.append(f"{where}の型 {q['type']!r} は型の一覧にありません（{sorted(field_types)}）")
                continue
            if not isinstance(q["required"], bool):
                problems.append(f"{where}の必須か（required）は true / false です")
            choices = _choices(q["choices"], q["type"], where, problems)
            for c in choices:
                if c in SYSTEM_CHOICES or c in OUTPUT_MARKS or c in (END, ANY):
                    problems.append(f"{where}の選択肢「{c}」は、システムが足す答え・出力の印・分岐の印と同じ名前です")
            nxt = q["next"] if q["next"] is not None else {}
            if not isinstance(nxt, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in nxt.items()):
                problems.append(f"{where}の分岐（next）は、答え → 次の質問の番号か {END} の表です")
                nxt = {}
            questions.append(Question(qid, _text(q["text"], f"{where}の文", problems) or "",
                                      _text(q["ask_about"], f"{where}の聞くこと", problems, optional=True),
                                      q["type"], bool(q["required"]), choices, dict(nxt)))
    names: list[Name] = []
    if not isinstance(raw["names"], list):
        problems.append("名前の一覧（names）は並びです")
    else:
        for n, x in enumerate(raw["names"], 1):
            if not isinstance(x, dict) or set(x) != {"text", "reading", "source", "confirmed"} \
                    or x.get("source") not in NAME_SOURCES:
                problems.append(f"名前 {n} 番目は text・reading・source（{list(NAME_SOURCES)}）・confirmed です")
                continue
            if x["confirmed"] is not True:
                problems.append(f"名前「{x['text']}」の読みを、まだ人が確かめていません（Intent.md 6-8）")
            nm = _name({"text": x["text"], "reading": x["reading"]}, f"名前 {n} 番目", x["source"], problems)
            if nm is not None:
                names.append(nm)
    if questions and not any(q.required for q in questions):
        problems.append("必須の質問が1つもありません（どんな通話でも「すべて取れた」になる。Intent.md 6-4）")
    s = Settings(purpose or "", company or Name("", "", "caller"), person or Name("", "", "caller"), purpose_line,
                 dict(tp), busy_line, voice or "", tuple(questions), tuple(names), None, raw)
    if questions and len({q.id for q in questions}) == len(questions):
        problems += _flow_problems(s, raw["rules"])
    if problems:
        raise SettingsInvalid(problems)
    return s


def _flow_problems(s: Settings, rules_raw: Any) -> list[str]:
    """分岐の確かめ（分岐先・受け皿・どこからも行かない質問・終わらない輪・必須を飛ばして終わる道・断りの答え）。"""
    problems: list[str] = []
    by_id = {q.id: q for q in s.questions}
    decline = rules_raw.get("decline", {}) if isinstance(rules_raw, dict) and isinstance(rules_raw.get("decline"), dict) else {}
    for qid, answers in decline.items():
        if qid in by_id and isinstance(answers, list):
            for a in answers:
                if a not in by_id[qid].choices:
                    problems.append(f"質問 {qid} の断りの答え「{a}」は、その質問の選択肢にありません")
    for q in s.questions:
        if not q.next:
            continue
        answers = possible_answers(q)
        for k, target in q.next.items():
            if k != ANY and k not in answers:
                problems.append(f"質問 {q.id} の分岐の答え「{k}」は、その質問の答えにありません（選択肢＋{list(SYSTEM_CHOICES)}）")
            if target != END and target not in by_id:
                problems.append(f"質問 {q.id} の分岐先 {target} がありません")
        if ANY not in q.next and set(answers) - set(q.next):
            problems.append(f"質問 {q.id} の分岐に、受け皿がありません（{sorted(set(answers) - set(q.next))} の行き先。"
                            f"「{ANY}」で、ほかのどの答えでもの行き先を書けます）")
    if problems:
        return problems
    edges = {q.id: {a: s.next_of(q.id, a) for a in possible_answers(q)} for q in s.questions}
    # 終わらない輪（深さ優先で、いま通っている道に戻る辺を探す）
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for t in set(edges[n].values()):
            if t == END:
                continue
            if state.get(t) == 1 or (state.get(t) is None and visit(t)):
                return True
        state[n] = 2
        return False
    first = s.questions[0].id
    if visit(first):
        return ["分岐が輪になっていて、終わりに届かない道があります（終わらない輪。Intent.md 6-4）"]
    unreachable = [q.id for q in s.questions if q.id not in state]
    for qid in unreachable:
        problems.append(f"質問 {qid} へは、どこからも行きません" + ("（必須の質問へ行く道がありません）" if by_id[qid].required else ""))
    if problems:
        return problems
    # 必須の質問を飛ばして終わる道：答えが断り・システムの答えでないのに、必須を聞かずに終わりへ着く道
    required = frozenset(q.id for q in s.questions if q.required)
    found: set[str] = set()
    memo: set[tuple[str, frozenset[str], bool]] = set()

    def walk(n: str, asked: frozenset[str], excused: bool) -> None:
        if (n, asked, excused) in memo:
            return
        memo.add((n, asked, excused))
        asked = asked | {n}
        for answer, t in edges[n].items():
            ok = excused or answer in SYSTEM_CHOICES or answer in decline.get(n, [])
            if t == END:
                if not ok:
                    found.update(required - asked)
            else:
                walk(t, asked, ok)
    walk(first, frozenset(), False)
    for qid in sorted(found):
        problems.append(f"必須の質問 {qid} を聞かずに終わる道があります（その道の答えを「断り」にするか、{qid} を必須から外す。"
                        "そのままだと「一部だけ取れた」になり、かけ直してしまう）")
    return problems


def _field_types(tx: Tx) -> frozenset[str]:
    return frozenset(r["type_name"] for r in tx.all("SELECT type_name FROM salescall.field_types"))


def _content(s: Settings) -> dict[str, Any]:
    """中身の表に置く形（業務の決まりは business_rules に置くので入れない）。"""
    return {k: v for k, v in s.raw.items() if k != "rules"}


def save_version(tx: Tx, business_id: UUID, raw: dict[str, Any]) -> UUID:
    """業務設定を確かめて、新しい版を作る（1つの取引。確かめに通らなければ何も置かない）。版の番号を返す。"""
    s = check(raw, field_types=_field_types(tx))
    version = tx.one(
        "INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_no, created_by)"
        " SELECT b.tenant_id, b.client_id, b.business_id,"
        "        coalesce((SELECT max(v.version_no) FROM salescall.business_versions v WHERE v.business_id = b.business_id), 0) + 1,"
        "        salescall.cur_actor()"
        " FROM salescall.businesses b WHERE b.business_id = %s RETURNING version_id", (business_id,))
    if version is None:
        raise LookupError(f"その業務は無い（または、取引の範囲の外）：{business_id}")
    vid = version["version_id"]
    for n, q in enumerate(s.questions, 1):
        tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s)",
                   (vid, q.id, n, q.type, q.required))
    try:
        business_rules.save(tx, vid, raw["rules"])
    except business_rules.RulesInvalid as e:
        raise SettingsInvalid([f"業務の決まり：{e}"]) from None
    tx.execute("INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())",
               (vid, json.dumps(_content(s), ensure_ascii=False)))
    return vid


def load(tx: Tx, version_id: UUID) -> Settings:
    """その版の業務設定（中身・質問・業務の決まりを、置いたときと同じ確かめで読む）。"""
    row = tx.one("SELECT content FROM salescall.business_contents WHERE version_id = %s", (version_id,))
    if row is None:
        raise LookupError(f"その版の業務設定の中身がありません：{version_id}")
    rules = business_rules.load(tx, version_id)
    raw = dict(row["content"], rules=_rules_raw(tx, version_id))
    s = check(raw, field_types=_field_types(tx))
    return Settings(**{**s.__dict__, "rules": rules})


def _rules_raw(tx: Tx, version_id: UUID) -> dict[str, Any]:
    return tx.one("SELECT rules FROM salescall.business_rules WHERE version_id = %s", (version_id,))["rules"]


def restore(tx: Tx, version_id: UUID) -> UUID:
    """前の版の中身から、新しい版を作る（以前の版へ戻す。Intent.md 5-3 の (11)。版は変えないので、戻すのも新しい版）。"""
    v = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))
    if v is None:
        raise LookupError(f"その版は無い（または、取引の範囲の外）：{version_id}")
    return save_version(tx, v["business_id"], load(tx, version_id).raw)


def assign(tx: Tx, run_id: UUID, version_id: UUID, *, reason: str) -> str:
    """実施回に版を割り当てる（記録つき。DESIGN.md 3-2）。割り当てた版の、品質の段の始め方の答えを返す
    （quality_stage.prepare：opened／mock_needed／already／stopped_version）。中身の無い版は割り当てない。"""
    if not reason.strip():
        raise ValueError("版を割り当てる理由を書いてください")
    load(tx, version_id)                             # 中身・業務の決まりが確かめに通る版だけ
    row = tx.one(
        "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
        " SELECT r.tenant_id, r.client_id, r.run_id, r.business_id,"
        "        coalesce((SELECT max(x.seq) FROM salescall.run_versions x WHERE x.run_id = r.run_id), 0) + 1,"
        "        %s, salescall.cur_actor(), %s FROM salescall.runs r WHERE r.run_id = %s RETURNING seq",
        (version_id, reason, run_id))
    if row is None:
        raise LookupError(f"その実施回は無い（または、取引の範囲の外）：{run_id}")
    return quality_stage.prepare(tx, version_id)
