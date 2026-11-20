"""依頼者へ渡す1件の結果の行（docs/DESIGN.md 7-3-1 の (1)・7-3-3・7-3-4。持ち主 CL-3）。

- 出力の処理は、判定し直さない・項目の状態を変えない・設定を自分で合成しない。読むのは答える場所の答えだけ（行の進み具合
  `customer_rows.progress`・次の予定の最新・確定の印 `result_releases`・試行）。1つの依頼者の取引の中で、1つの実施回の行だけを並べる。
- **出す結果は確定した版だけ**（確定の印のある版のうち、段が `DELIVERABLE_STAGES` のもの）。✅ 2026-11-18 管理者：サンプル検証（＝模擬通話）は
  渡さない・実検証（＝検証モード）は渡せる・そのあとフルオート（＝本番）。⇒ verification と production。模擬通話は結果の版を作らない。
  ⏳ 依頼者のシステムへの書き戻し（`deliveries`）は、データベースの守りが本番の版だけのまま（CL-2 へのお願い 17）。ファイルはこの決まりで出す。
- **判定待ち（次の予定が hold）の行と、最新の結果の版がまだ確定していない行は出さない**（古い確定の版を、今の答えとして渡さない）。
- **質問ごとの2列（答え／お客様の言葉）**（DESIGN.md 7-3-2。`answer_cells` の1か所）：✅ 2026-11-08 日本時間3時48分03秒・2026-11-20 日本時間
  1時00分04秒 管理者「あなたに指示の中で「-」「✕」以外で記載することを伝えていますよね」（CL-1 の連絡帳 4-206）＝取れなかった答えは、答えの欄に
  「-」「✕」ではなく、その答えをいただいたお客様の言葉（会話の側の1か所 `Flow.customer_words`＝しまう所は `hedged`）。言葉の無い答えは状態の
  短い言葉（`words.ITEM_STATES` の依頼者へ出す言葉）。取れた答えは値のまま、言い切っていない返事のときだけ横の欄にお客様の言葉（2026-10-18 案C）。
  伺わなかった質問（判定の中身の not_asked）は「伺っていない（対象外）」。答えは、結果を決めた通話の担当の方の答え（core の
  `conversation_io.answers` の1か所。担当の方ごとの決まりをここで決め直さない）。
- ⏳ 判定の中身の列（ほかのご希望・ご納得いただけなかった点 など）は、まだ（出し方を決めてから。DESIGN.md 7-3-3）。
- 「お答えできなかったご質問」の列（計画書 11-3「お答えできなかったご質問は必ず載せる」）：判定の中身の callee_questions（CL-2 4-441）の
  答えを持っていなかったもの（`unanswered_questions` の1か所）。通話の報告を直に読まない（目的のお客様だと確かめていない方の言葉を渡さない決まりを、
  判定の1か所で守る）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import json

from salescall.core import business_rules, business_settings, call_report, calling_hours, conversation_io, customer_rows, judge, phone
from salescall.core.access import Tx
from salescall.output import words

DELIVERABLE_STAGES: tuple[str, ...] = ("verification", "production")

COLUMNS: tuple[str, ...] = ("管理番号", "会社名", "お呼びする方", "お呼びする方のフルネーム", "お呼びする方の部署", "電話番号", "処理", "結果",
                            "通話の終わり方", "かけた回数", "最後にかけた日時", "お答えできなかったご質問")

# 依頼者に埋めていただく欄（「お名前だけではどの方か分からなかった」行。どの行のどの欄かは core の business_settings.who_unclear_blanks の
# 1か所）。✅ 2026-11-18 管理者「(ア)でOK」：結果のファイルに「フルネーム」「部署」の欄を足し、色の付かない形（CSV）では空欄に
# 「（ご記入ください）」と書き、色の付く形（Excel・画面の見本）では、その欄だけを赤い背景にする（✅ 2026-11-02 管理者「一覧の入れるべき
# 空欄部分が、その行だけ赤背景になると分かりやすいですね」）。欄の名前 → 列の名前の組はここ1か所。
TO_FILL_COLUMNS: dict[str, str] = {"full_name": "お呼びする方のフルネーム", "department": "お呼びする方の部署"}
PLEASE_FILL = "（ご記入ください）"
# 赤い背景の色（1か所。Excel の出力と画面の見本で同じ色＝画面の色の値 --stop-soft・--stop と同じかを試験が見る）。✅ 2026-11-18 管理者「文字が
# 見えにくい奇抜な赤にならないように」⇒ 淡い赤の地に濃い赤の文字。文字が読める差（コントラスト比）4.5 以上を試験が確かめる。
TO_FILL_BACKGROUND = "#FDECEC"
TO_FILL_TEXT = "#C42B2B"


@dataclass(frozen=True)
class ResultRow:
    client_ref: str
    company: str
    person: str
    full_name: str
    department: str
    phone: str
    handling: str          # 処理（済み／対応中）
    result: str            # 結果（確定した版の業務の状態の、依頼者へ出す言葉。まだ無ければ空）
    call_end: str          # 通話の終わり方（7-3-4）
    dialed: int            # かけた回数（発信まで進んだ電話）
    last_at: str           # 最後にかけた日時（日本時間）
    questions: str = ""    # お答えできなかったお客様からのご質問（計画書 11-3「必ず載せる」。判定の中身 callee_questions の answered が偽のもの）
    to_fill: tuple[str, ...] = ()   # 依頼者に埋めていただく欄の列の名前（TO_FILL_COLUMNS の値）
    headers: tuple[str, ...] = ()   # 質問ごとの2列の見出し（実施回のいまの版の質問の順。`question_headers`）
    answers: tuple[str, ...] = ()   # 質問ごとの2列の値（headers と同じ並び）
    ours: tuple[str, ...] = ()      # 質問ごとの値のうち、こちらが作った言葉（状態の短い言葉。依頼者へ出さない言葉の確かめに通す）

    @property
    def columns(self) -> tuple[str, ...]:
        return COLUMNS + self.headers

    def values(self) -> list[str]:
        """列の順の値（そのまま。埋めていただく欄は空）。"""
        return [self.client_ref, self.company, self.person, self.full_name, self.department, self.phone, self.handling, self.result,
                self.call_end, str(self.dialed), self.last_at, self.questions] + list(self.answers)

    def cells(self) -> list[str]:
        """色の付かない形（CSV）の値：埋めていただく欄には PLEASE_FILL を書く。"""
        return [PLEASE_FILL if c in self.to_fill else v for c, v in zip(self.columns, self.values())]

    def our_words(self) -> list[str]:
        """こちらが作った言葉（依頼者へ出さない言葉の確かめに通すもの。お客様・依頼者のデータ＝会社名・お客様の言葉・値などは入れない）。"""
        return [self.handling, self.result, self.call_end] + ([PLEASE_FILL] if self.to_fill else []) + list(self.ours)


NOT_ASKED = "伺っていない（対象外）"   # 伺わなかった質問（決めるのは判定の not_asked の1か所＝判定の中身）

# お客様の言葉の書き方（✅ 2026-11-20 管理者：40字で切るのは「場合によっては残した方がいいね」・選び「ウ」（依頼者ごとに選べる）・「業務ごと」）。
# 業務ごとの設定 `WORDS_SETTING`（short＝縮める／full＝全部残す。初めの値は short＝2026-11-08「お客様の言葉で簡潔に渡せるように！」のまま）。
# 縮め方はここ1か所（`shorten`）：発話を「／」でつないだもののうち最後の2つまで・1つ40字まで（会話の側の今の縮め方 Flow.customer_words と同じ）。
# ⏳ 設定の定義は CL-2 へのお願い 54（無い間は short）。⏳ 会話の側が、しまう前に40字で切っているので、全部残す業務でも、いまは切れた言葉が
# 届く（縮めずに全部しまうのは CL-1 へのお願い 4-2 の6）。
WORDS_SETTING = "output.customer_words"
WORDS_MAX_SAID = 2
WORDS_MAX_CHARS = 40


def shorten(text: str) -> str:
    """お客様の言葉を縮める（1か所）：最後の2つの発話まで・1つ40字まで。"""
    said = [p.strip() for p in text.split("／") if p.strip()]
    return "／".join(p[:WORDS_MAX_CHARS] for p in said[-WORDS_MAX_SAID:])


def words_mode(tx: Tx, business_id: Any) -> str:
    """その業務のお客様の言葉の書き方（設定 `WORDS_SETTING` の1か所。無ければ short）。"""
    from salescall.core import settings
    try:
        mode = settings.resolve(tx, WORDS_SETTING, tenant_id=tx.one("SELECT salescall.cur_tenant() AS t")["t"],
                                client_id=tx.one("SELECT salescall.cur_client() AS c")["c"], business_id=business_id)
    except settings.SettingMissing:
        return "short"
    return "full" if mode == "full" else "short"
WORDS_SUFFIX = "（お客様の言葉）"       # 質問ごとの2列目の見出しの後ろ


def question_headers(questions: Any) -> tuple[str, ...]:
    """質問ごとの2列の見出し（1か所）：答えの欄は質問の「聞くこと」（無ければ質問の文）、横の欄はそれに「（お客様の言葉）」。"""
    out: list[str] = []
    for q in questions:
        name = q.ask_about or q.text
        out += [name, name + WORDS_SUFFIX]
    return tuple(out)


def _value_text(value: Any) -> str:
    """取れた値を文字にする（値の形は型ごと。文字はそのまま、{"text": …} はその文字、ほかは JSON の文字）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return json.dumps(value, ensure_ascii=False)


def unanswered_questions(detail: dict[str, Any] | None, mode: str = "short") -> str:
    """お答えできなかったお客様からのご質問（1か所）：判定の中身 callee_questions（決め方は判定の1か所 judge._asked＝目的のお客様だと確かめ
    られなかった方のご質問は入らない＝CL-2 4-441）のうち、答えを持っていなかったもの（answered が偽）を、言われた言葉のまま「／」でつなぐ。
    言葉の縮め方は、ほかのお客様の言葉と同じ業務の設定（`words_mode`）。"""
    said = [(q.get("said") or "").strip() for q in (detail or {}).get("callee_questions") or () if q.get("answered") is False]
    # 数は減らさない（どのご質問も必ず載せる）。縮める業務でも、1つずつの長さだけを縮める
    return "／".join(x[:WORDS_MAX_CHARS] if mode == "short" else x for x in said if x)


def answer_cells(q: Any, item: dict[str, Any] | None, not_asked: set[str], mode: str = "short") -> tuple[str, str, str | None]:
    """質問1つの2列（答え・お客様の言葉）と、こちらが作った言葉（あれば。確かめに通す）。決まりは DESIGN.md 7-3-2 の1か所。
    mode＝お客様の言葉の書き方（short＝縮める／full＝全部残す。`words_mode`）。"""
    if q.id in not_asked:
        return NOT_ASKED, "", NOT_ASKED
    if item is None:
        word = words.ITEM_STATES["empty"].client or ""
        return word, "", word
    state, said = item["state"], (item.get("hedged") or "").strip()
    said = shorten(said) if mode == "short" else said
    if state in judge.TAKEN:   # 「取れた」の定義は判定の1か所（DESIGN.md 4-4）
        answer = item.get("condition") if q.type in business_settings.CHOICE_TYPES or item.get("value") in (None, "") else _value_text(item.get("value"))
        return answer or "", judge.hedged_words(state, said) or "", None   # 言い切っていない返事のときだけ横の欄（判定の1か所）
    if said:   # 取れなかった答え：答えの欄にお客様の言葉（✅ 2026-11-08・11-20 管理者）
        return said, "", None
    word = words.ITEM_STATES[state].client or "" if state in words.ITEM_STATES else ""
    return word, "", word


def call_end(last: dict[str, Any] | None, next_kind: str | None) -> str:
    """通話の終わり方（最後の電話の事実だけから。7-3-4 の上から順に、最初に当たったもの）。言葉は output/words の1か所。"""
    if last is None:
        return words.CALL_END_CLIENT["no_call_ended" if next_kind == "end" else "not_yet"]
    state, notice = last["state"], last.get("notice")
    if notice and f"notice:{notice}" in words.CALL_END_CLIENT:
        return words.CALL_END_CLIENT[f"notice:{notice}"]
    if last.get("machine"):   # 電話口が機械だった（留守番電話・自動音声・ファクス。読むのは core の call_report.machine_party の1か所）
        return words.CALL_END_CLIENT[f"machine:{last['machine']}"]
    if state == "dropped":
        return words.CALL_END_CLIENT.get(f"side:{last.get('hangup_side')}", words.CALL_END_CLIENT["side:unknown"])
    w = words.ATTEMPT_STATES.get(state)
    return w.client if w is not None and w.client else words.CALL_END_CLIENT["unknown"]


def _answers(tx: Tx, task_id: Any, attempt_id: Any, detail: dict[str, Any] | None, questions: Any, mode: str) -> tuple[list[str], list[str]]:
    """結果を決めた通話の担当の方の答え（core の conversation_io.answers の1か所）を、質問ごとの2列（答え・お客様の言葉）にする1か所。
    依頼者へのファイル（rows）と、契約者の確かめの画面（answers_for_check）が同じものを使う。"""
    items = conversation_io.answers(tx, task_id, attempt_id)
    not_asked = set((detail or {}).get("not_asked") or ())
    cells: list[str] = []
    ours: list[str] = []
    for q in questions:
        answer, said, our = answer_cells(q, items.get(q.id), not_asked, mode)
        cells += [answer, said]
        ours += [our] if our else []
    return cells, ours


def answers_for_check(tx: Tx, task_id: UUID) -> list[tuple[str, str, str]]:
    """依頼者の取引で、その行のいまの結果の版の答えを、依頼者へ渡す書き方のまま（質問・答え・お客様の言葉）返す。確定の前の版も返す
    （契約者が「この結果でよい」を押す前に、何が依頼者へ渡るかを見るため。2026-11-20 の計画書の読み直し：前は業務の結果だけを見せていた）。
    結果の版が無い・答えを取った通話が無い・業務の中身をまだ置いていない版なら空。"""
    v = tx.one("SELECT l.attempt_id, l.detail, k.run_id, r.business_id FROM salescall.latest_task_results l"
               " JOIN salescall.tasks k ON k.task_id = l.task_id JOIN salescall.runs r ON r.run_id = k.run_id"
               " WHERE l.task_id = %s", (task_id,))
    if v is None or v["attempt_id"] is None:
        return []
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (v["run_id"],))["v"]
    try:
        questions = business_settings.load(tx, version).questions if version is not None else ()
    except LookupError:
        return []
    cells, _ = _answers(tx, task_id, v["attempt_id"], v["detail"], questions, words_mode(tx, v["business_id"]))
    heads = question_headers(questions)[0::2]   # 2列のうち答えの欄の見出し
    return [(heads[i], cells[2 * i], cells[2 * i + 1]) for i in range(len(heads))]


def rows(tx: Tx, run_id: UUID) -> list[ResultRow]:
    """その実施回の、依頼者へ渡す結果の行（一覧の行番号の順。判定待ちの行は出さない）。"""
    # 行ごとの最新の版が、渡せる段で確定しているか。最新の版がまだ確定していない行（確かめ待ち・判定できない・直した版の確かめ待ち）は、
    # 古い確定の版を出さずに、行ごと出さない（判定待ちと同じ。古い答えを今の答えとして渡さない）。
    latest = {r["task_id"]: r for r in tx.all(
        "SELECT l.task_id, l.version_no, l.business_state::text AS business_state, l.attempt_id, l.detail,"
        "       EXISTS (SELECT 1 FROM salescall.result_releases x WHERE x.task_id = l.task_id AND x.version_no = l.version_no"
        "               AND x.stage = ANY(%s)) AS released"
        " FROM salescall.latest_task_results l JOIN salescall.tasks k ON k.task_id = l.task_id WHERE k.run_id = %s",
        (list(DELIVERABLE_STAGES), run_id))}
    calls = {r["task_id"]: r for r in tx.all(
        "SELECT DISTINCT ON (a.task_id) a.task_id, a.attempt_id, a.state::text AS state, a.notice, a.hangup_side::text AS hangup_side, a.claimed_at,"
        "       count(*) OVER (PARTITION BY a.task_id) AS dialed"
        " FROM salescall.attempts a JOIN salescall.tasks k ON k.task_id = a.task_id"
        " WHERE k.run_id = %s AND salescall.is_dialed(a.state) ORDER BY a.task_id, a.claimed_at DESC", (run_id,))}
    for c in calls.values():   # 最後の電話の電話口が機械だったか（core の1か所。通話の終わり方の4番目＝設計書 7-3-4）
        c["machine"] = call_report.machine_party(tx, c["attempt_id"])
    full_names = {r["task_id"]: r["target_full_name"] for r in tx.all(
        "SELECT task_id, target_full_name FROM salescall.tasks_now WHERE run_id = %s", (run_id,))}
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (run_id,))["v"]
    try:   # 中身をまだ置いていない版（業務の取り込みの前）は、質問の列を出さない
        questions = business_settings.load(tx, version).questions if version is not None else ()
    except LookupError:
        questions = ()
    headers = question_headers(questions)
    mode = words_mode(tx, tx.one("SELECT business_id FROM salescall.runs WHERE run_id = %s", (run_id,))["business_id"])
    out = []
    for r in customer_rows.progress(tx, run_id):
        kind = r["next_kind"]
        v = latest.get(r["task_id"])
        if kind == "hold" or (v is not None and not v["released"]):
            continue
        state = v["business_state"] if v is not None else None
        result = (words.BUSINESS_STATES[state].client or "") if state in words.BUSINESS_STATES else ""
        last = calls.get(r["task_id"])
        person = (r["target_name"] or "") + (f"（{r['target_role']}）" if r["target_role"] else "")
        if v is not None and v["attempt_id"] is not None:
            cells, ours = _answers(tx, r["task_id"], v["attempt_id"], v["detail"], questions, mode)
        else:
            cells, ours = [""] * len(headers), []
        out.append(ResultRow(
            client_ref=r["client_ref"] or "", company=r["source_name"] or "", person=person,
            full_name=full_names.get(r["task_id"]) or "", department=r["target_department"] or "",
            to_fill=tuple(TO_FILL_COLUMNS[f] for f in business_settings.who_unclear_blanks(tx, r["task_id"]))
            if kind == "end" and r["next_end_reason"] == business_rules.WHO_UNCLEAR else (),
            phone=(phone.normalize(r["dial_phone"]).national or r["dial_phone"]) if r["dial_phone"] else "",
            handling=words.NEXT_KINDS["end"].client if kind == "end" else words.NEXT_KINDS["call"].client,
            result=result, call_end=call_end(last, kind), dialed=int(last["dialed"]) if last else 0,
            last_at=calling_hours.label(last["claimed_at"]) if last else "",
            questions=unanswered_questions(v["detail"], mode) if v is not None else "", headers=headers, answers=tuple(cells), ours=tuple(ours)))
    return out
