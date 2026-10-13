"""流れの制御（フェーズ0の試作。DESIGN.md 4-4・6-1・6-2・6-5・6-11 をそのまま形にしたもの）。

会話の位置・項目の状態・聞き返しと確かめの回数は、ここだけが持つ（Q6・Q7）。AIは操作を呼ぶだけで、
ここが確かめて「受け付けた」と返したものだけが記録になる（6-2）。G-6・G-16・G-13・G-5 の確かめは、すべてここを通す。

項目の状態（4-4 の名前だけを使う）：empty / heard / unclear / checking / confirmed / accepted / unresolved / refused
すべての質問に、システムが2つの選択肢を足す（業務ごとに書き忘れない。I-19）：
  "unknown"（相手が「わからない」と答えた）⇒ 聞けたので accepted。依頼主へは「-」。念のための確認はしない
  "refused"（相手が答えを拒んだ）⇒ refused。依頼主へは「✕」。二度と聞かない
  ✅ 2026-10-13 ユーザー確定（DESIGN.md 6-2・4-4・7-1b）。設計の抜け（拒否を報告する操作が無い／「わからない」と
     答えた項目を念のため聞き直すことになっていた）を埋めたもの。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 型の一覧（Q19）：その場で言い直して確かめる型。DESIGN.md 4-4
CONFIRM_TYPES = {"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address"}
# 選択肢で答える型：依頼主へは、相手の言葉ではなく、宣言された選択肢を渡す（7-1b）
CHOICE_TYPES = {"yes_no", "choice"}
SYSTEM_CONDITIONS = ("unknown", "refused")
STATES = ("empty", "heard", "unclear", "checking", "confirmed", "accepted", "unresolved", "refused")
# 値の形をそろえる（型の一覧 Q19 の決まり）：言い終わりの「です」「。」などは値に入れない
# ⛔「ね」「よ」「で」などの1文字は外さない（名前の終わり＝「あかね」などを削るため）
_TAIL = re.compile(r"(?:です|でございます|になります)?[。、．！？!?\s]*$")


def normalize_value(qtype: str, value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if qtype not in CHOICE_TYPES:
        v = _TAIL.sub("", v).strip()
    return v or None
GOT = ("confirmed", "accepted")  # 「取れた」（4-4）


@dataclass
class Limits:
    """回数の上限（システム共通の設定の値。8章の「厳しいほう」）。DESIGN.md 6-11"""
    ask_back: int = 1          # その場の聞き返し：1項目1回まで
    recheck: int = 1           # 念のための確認：1項目1回まで
    after_correction: int = 1  # 訂正のあとの復唱：1回まで


@dataclass
class Question:
    id: str
    text: str
    type: str                   # 型の一覧の名前（"yes_no" "choice" "datetime" など）
    required: bool = True
    conditions: tuple = ()      # 業務が宣言した選択肢（システムの2つは足さなくてよい）

    @property
    def all_conditions(self) -> tuple:
        return tuple(self.conditions) + SYSTEM_CONDITIONS

    @property
    def confirm(self) -> bool:
        return self.type in CONFIRM_TYPES


@dataclass
class Item:
    state: str = "empty"
    value: str | None = None
    condition: str | None = None
    ask_backs: int = 0
    rechecks: int = 0
    corrections: int = 0
    history: list = field(default_factory=list)

    def set(self, state: str, why: str) -> None:
        assert state in STATES, state
        self.history.append((self.state, state, why))
        self.state = state


class Flow:
    def __init__(self, questions: list[Question], limits: Limits | None = None):
        self.questions = {q.id: q for q in questions}
        self.order = [q.id for q in questions]
        self.limits = limits or Limits()
        self.items = {q.id: Item() for q in questions}
        self.current: str | None = self.order[0]
        self.phase = "asking"            # asking → rechecking → closing
        self.recheck_queue: list[str] = []
        self.awaiting_confirm: str | None = None
        self.callee_turns = 0
        self.confirm_requested_at: int | None = None
        self.hurried = False
        self.log: list[dict] = []        # 呼ばれた操作と返事（確かめの記録）

    # ── 相手の発話が届いた（本番では、相手の音声の発話の検出で数える）
    def on_callee(self) -> None:
        self.callee_turns += 1

    # ── AIの操作を受ける唯一の入口
    def handle(self, fc) -> dict:
        args = dict(fc.args or {})
        fn = getattr(self, f"_op_{fc.name}", None)
        reply = fn(args) if fn else self._reject("知らない操作です。")
        self.log.append({"op": fc.name, "args": args, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    # ── 操作
    def _op_record_answer(self, a: dict) -> dict:
        qid = a.get("question_id")
        if qid != self.current:
            return self._reject(f"いまの質問は {self.current} です。")
        if self.awaiting_confirm == qid:
            # 2026-10-13 G-16：言い直した値に相手が「はい」と答えたのに、AIが record_answer を呼び、言い直しが繰り返された
            return self._reject("いまは、言い直した値への相手の返事を待っています。相手の返事は confirm_field で報告してください"
                                "（合っていれば affirmed、違う値を言われたら corrected と、その値）。")
        q, it = self.questions[qid], self.items[qid]
        cond = a.get("condition_id")
        if cond not in q.all_conditions:
            return self._reject(f"条件 {cond} は、この質問の選択肢にありません。")
        it.condition, it.value = cond, normalize_value(q.type, a.get("value"))
        if cond == "refused":
            it.set("refused", "相手が答えを拒んだ")
            return self._advance("受け付けました（答えたくない）。この質問は二度としません。")
        if cond == "unknown":
            it.set("accepted", "相手が「わからない」と答えた")
            return self._advance("受け付けました（わからない）。")
        it.set("heard", "聞き取れた")
        if q.confirm or it.ask_backs or self.phase == "rechecking":
            return self._ask_confirm(qid)
        it.set("accepted", "確かめない型で、はっきり聞き取れた")
        return self._advance("受け付けました。")

    def _op_report_unclear(self, a: dict) -> dict:
        qid = a.get("question_id")
        if qid != self.current:
            return self._reject(f"いまの質問は {self.current} です。")
        q, it = self.questions[qid], self.items[qid]
        if self.phase == "asking" and q.required and it.ask_backs < self.limits.ask_back:
            it.ask_backs += 1
            it.set("unclear", "聞き取れない・曖昧（聞き返す）")
            return {"result": "ask_back", "message": "決まった言い方で1回だけ聞き返してください。"}
        it.set("unresolved", "聞き返しを使い切った" if self.phase == "asking" else "念のための確認でも聞けなかった")
        return self._advance("「-」にします。")

    def _op_confirm_field(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm != qid:
            return self._reject("いまは、その項目の言い直しを求めていません。")
        if self.callee_turns <= (self.confirm_requested_at or 0):  # I-24：相手の返事が届く前の報告は差し戻す
            return self._reject("相手の返事をまだ聞いていません。言い直したら、相手が返事をするまで待ってから報告してください。")
        it = self.items[qid]
        self.awaiting_confirm = None
        result = a.get("result")
        if result == "affirmed":
            it.set("confirmed", "相手が肯定した")
            return self._advance("確認できました。")
        if result == "corrected" and it.corrections < self.limits.after_correction:
            it.corrections += 1
            it.value = normalize_value(self.questions[qid].type, a.get("correction"))
            it.set("heard", "相手が訂正した")
            return self._ask_confirm(qid)
        it.set("unresolved", "訂正のあとも合わない" if result == "corrected" else "相手が答えなかった")
        return self._advance("「-」にします。")

    def _op_request_end(self, a: dict) -> dict:
        reason = a.get("reason")
        if reason in ("callee_hurry", "callee_refused_call"):
            self.hurried = True
            self.current, self.phase = None, "closing"
            return {"result": "end", "message": "締めの言葉を言って終えてください。念のための確認はしません。"}
        if self.phase == "asking" and self._remaining():
            return self._reject(f"まだ質問が残っています。いまの質問は {self.current} です。")
        return self._advance("")

    # ── 内部
    def _ask_confirm(self, qid: str) -> dict:
        it = self.items[qid]
        it.set("checking", "言い直して確かめる")
        self.awaiting_confirm = qid
        self.confirm_requested_at = self.callee_turns
        return {"result": "confirm_required", "value": it.value,
                "message": f"「{it.value}」と、この値のとおりに言い直して、相手の返事を待ってください。"}

    def _remaining(self) -> list[str]:
        return [qid for qid in self.order if self.items[qid].state == "empty"]

    def _advance(self, prefix: str) -> dict:
        rest = self._remaining()
        if self.phase == "asking" and rest:
            self.current = rest[0]
            q = self.questions[self.current]
            return {"result": "next", "question_id": q.id, "question": q.text,
                    "message": f"{prefix}次の質問は {q.id}「{q.text}」です。"}
        if self.phase == "asking":
            # 締めの前に、「-」の必須項目を1回だけ念のため確かめる（6-11）。急いでいる相手にはしない
            self.recheck_queue = [qid for qid in self.order
                                  if self.items[qid].state == "unresolved" and self.questions[qid].required
                                  and self.items[qid].rechecks < self.limits.recheck]
            self.phase = "rechecking"
        if self.phase == "rechecking" and self.recheck_queue and not self.hurried:
            self.current = self.recheck_queue.pop(0)
            self.items[self.current].rechecks += 1
            q = self.questions[self.current]
            return {"result": "recheck", "question_id": q.id, "question": q.text,
                    "message": f"{prefix}念のための確認です。{q.id}「{q.text}」を、もう1回だけ聞いてください。"}
        self.current, self.phase = None, "closing"
        return {"result": "end", "message": f"{prefix}質問は終わりです。締めの言葉を言って終えてください。"}

    def _reject(self, why: str) -> dict:
        return {"result": "rejected", "message": why}

    # ── 依頼主へ渡す書き方（7-1b。Q20 の唯一の場所）
    def export(self, unknown_mark: str = "-", refused_mark: str = "✕") -> dict:
        out = {}
        for qid, it in self.items.items():
            if it.state == "refused":
                out[qid] = refused_mark
            elif it.state in GOT and it.condition != "unknown":
                if self.questions[qid].type in CHOICE_TYPES or it.value in (None, ""):
                    out[qid] = it.condition  # 選択肢の型は、宣言された選択肢（相手の言葉そのものではない）
                else:
                    out[qid] = it.value
            else:
                out[qid] = unknown_mark
        return out


def tool_declarations(questions: list[Question]):
    """AIが呼べる操作（6-2）の宣言を作る唯一の場所。条件の選択肢は、質問の宣言＋システムの2つ。"""
    from google.genai import types

    def s(enum=None, desc=None):
        return types.Schema(type=types.Type.STRING, enum=enum, description=desc)

    qids = [q.id for q in questions]
    conds = sorted({c for q in questions for c in q.all_conditions})

    def fd(name, desc, props, required):
        return types.FunctionDeclaration(name=name, description=desc,
                                         parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=required))

    return [types.Tool(function_declarations=[
        fd("record_answer", "相手の答えを、いまの質問の選択肢に分類して報告する。相手が「わからない」と答えたら unknown、答えを拒んだら refused",
           {"question_id": s(qids), "condition_id": s(conds), "value": s(desc="聞き取った値（相手の言葉のとおり）")},
           ["question_id", "condition_id"]),
        fd("report_unclear", "相手の答えが聞き取れない・曖昧だったことを報告する",
           {"question_id": s(qids)}, ["question_id"]),
        fd("confirm_field", "言い直した値への、相手の返事を報告する（相手が返事をしてから呼ぶ）",
           {"question_id": s(qids), "result": s(["affirmed", "corrected", "no_answer"]), "correction": s(desc="訂正された値")},
           ["question_id", "result"]),
        fd("request_end", "電話を終えたいときに呼ぶ。相手が急いでいる・切ろうとしているなら callee_hurry、通話自体を断られたなら callee_refused_call",
           {"reason": s(["done", "callee_hurry", "callee_refused_call"])}, ["reason"]),
    ])]
