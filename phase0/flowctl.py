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

from phrases import confirm as confirm_phrase

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

# ── AIに見せる言葉（唯一の場所）。⛔ 2026-10-13 ユーザー指摘「こちらが英語を使っていませんか？」：指示文・操作の宣言・操作への返事に、
#    英語の名前（condition_id・unknown・callee_hurry・confirm_required など）が入っていた。AIが英語で自分の動きを声に出した回があった。
#    ⇒ AIに見せる名前は、すべて日本語にする。中の名前（英字）との置き換えは、AIの入口（Flow.handle）と操作の宣言（tool_declarations）
#    で、この表だけを使って行う。⚠️ 操作の名前（record_answer など）だけは、道具の決まりで英字のまま。
#    依頼主が決める選択肢（「はい」「いいえ」など）は、依頼主が書いたまま（日本語）で、置き換えない。
ARG_LABEL = {"question_id": "質問", "condition_id": "答えの種類", "value": "値", "result": "相手の返事", "correction": "訂正された値",
             "reason": "理由"}
SYSTEM_LABEL = {"unknown": "わからない", "refused": "答えたくない"}
CONFIRM_LABEL = {"affirmed": "合っている", "corrected": "違う値を言われた", "no_answer": "答えなかった"}
END_LABEL = {"done": "質問が終わった", "callee_hurry": "相手が急いでいる", "callee_refused_call": "通話を断られた"}
REPLY_KEY_LABEL = {"result": "結果", "message": "次にすること", "question_id": "質問", "question": "質問の文", "value": "値", "say": "言い方"}
RESULT_LABEL = {"next": "次の質問", "confirm_required": "言い直して確かめる", "rejected": "差し戻し", "ask_back": "聞き返す",
                "recheck": "念のための確認", "end": "締め", "busy_ask": "引き止めの一言"}
_FROM_LABEL = {"condition_id": {v: k for k, v in SYSTEM_LABEL.items()}, "result": {v: k for k, v in CONFIRM_LABEL.items()},
               "reason": {v: k for k, v in END_LABEL.items()}}
_ARG_FROM_LABEL = {v: k for k, v in ARG_LABEL.items()}
OPS = ("record_answer", "report_unclear", "confirm_field", "request_end")  # AIが呼べる操作（6-2。宣言は tool_declarations）


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
    conditions: tuple = ()      # 業務が宣言した選択肢（依頼主が書いたまま。システムの2つは足さなくてよい）

    def __post_init__(self):
        clash = set(self.conditions) & (set(SYSTEM_CONDITIONS) | set(SYSTEM_LABEL.values()))
        assert not clash, f"{self.id} の選択肢が、システムの選択肢と同じ名前です：{clash}"

    @property
    def all_conditions(self) -> tuple:
        return tuple(self.conditions) + SYSTEM_CONDITIONS

    @property
    def labels(self) -> tuple:
        """AIに見せる選択肢の名前（業務の選択肢はそのまま、システムの2つは日本語の名前）。"""
        return tuple(self.conditions) + tuple(SYSTEM_LABEL[c] for c in SYSTEM_CONDITIONS)

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
    def __init__(self, questions: list[Question], limits: Limits | None = None, confirm=confirm_phrase,
                 busy_ask: str | None = None):
        """confirm：言い直しの言い方を作る関数 (値, 場面, 何回目) → 文（ふだんは phrases.confirm。言い方を比べる試験 G-25 だけが別のものを渡す）。
        busy_ask：相手が「忙しい」と言ったときに1回だけ言う一言（業務設定。None なら丁寧に終える＝既定。Intent.md 9-4）。"""
        self.confirm = confirm
        self.busy_ask = busy_ask
        self.busy_asked = False
        self.confirm_counts: dict[str, int] = {}   # 言い直しの場面ごとの回数（同じ言い方が続かないよう、順番に回す）
        self.awaiting_say: str | None = None       # いま返事を待っている言い直しの、言い方（差し戻しのときも同じ言い方を使う）
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

    # ── AIの操作を受ける唯一の入口（AIに見せる言葉＝日本語）。ここで1回だけ中の名前に置き換え、返事を日本語に戻す
    def handle(self, fc) -> dict:
        return self._reply_out(self.apply(fc.name, self._args_in(dict(fc.args or {}))))

    # ── 中の名前での入口（handle と試験は、ここを通す）。記録（log）は中の名前で残す
    def apply(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"_op_{name}", None)
        reply = fn(args) if fn else self._reject(f"その操作はありません（使える操作は {'・'.join(OPS)}）。")
        self.log.append({"op": name, "args": args, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    @staticmethod
    def _args_in(args: dict) -> dict:
        out = {}
        for k, v in args.items():
            key = _ARG_FROM_LABEL.get(k, k)
            out[key] = _FROM_LABEL.get(key, {}).get(v, v)
        return out

    @staticmethod
    def _reply_out(reply: dict) -> dict:
        return {REPLY_KEY_LABEL.get(k, k): (RESULT_LABEL.get(v, v) if k == "result" else v) for k, v in reply.items()}

    # ── 操作
    def _op_record_answer(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is not None:
            # 2026-10-13 G-16：言い直した値に相手が「はい」と答えたのに、AIが record_answer を呼び、言い直しが繰り返された
            # 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に、Q3 の答えとして報告された（同じ扱いにする）
            return self._reject("いまは record_answer では受け付けません。")
        if qid != self.current:
            return self._reject(f"{qid} は、いまの質問ではありません。")
        q, it = self.questions[qid], self.items[qid]
        cond = a.get("condition_id")
        if cond not in q.all_conditions:
            shown = SYSTEM_LABEL.get(cond, cond)
            return self._reject(f"{ARG_LABEL['condition_id']}「{shown}」は、この質問の選択肢にありません（選べるのは {'、'.join(q.labels)}）。")
        value = normalize_value(q.type, a.get("value"))
        if value is None and q.type not in CHOICE_TYPES and cond not in SYSTEM_CONDITIONS:
            # ⛔ 2026-10-13 G-25 で見つけた漏れ：値の要る質問で値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を
            #    入れていた（確かめない型なら、依頼主へ値の代わりに条件の名前が渡っていた）
            return self._reject(f"答えの値がありません。相手が答えた値を「{ARG_LABEL['value']}」に入れて、もう一度報告してください。")
        it.condition, it.value = cond, value
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
        if self.awaiting_confirm is not None:
            return self._reject("いまは report_unclear では受け付けません。")
        if qid != self.current:
            return self._reject(f"{qid} は、いまの質問ではありません。")
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
            return self._reject(f"いまは、{qid} の言い直しを求めていません。")
        if self.callee_turns <= (self.confirm_requested_at or 0):  # I-24：相手の返事が届く前の報告は差し戻す
            return self._reject("相手の返事をまだ聞いていません。言い直したら、相手が返事をするまで待ってから報告してください。")
        it = self.items[qid]
        result = a.get("result")
        correction = normalize_value(self.questions[qid].type, a.get("correction"))
        if result == "corrected" and correction is None:  # 訂正の値の無い報告は、記録を書き換える前に差し戻す（上と同じ漏れ）
            return self._reject(f"訂正された値がありません。相手が言い直した値を「{ARG_LABEL['correction']}」に入れて、もう一度報告してください。")
        self.awaiting_confirm = None
        if result == "affirmed":
            it.set("confirmed", "相手が肯定した")
            return self._advance("確認できました。")
        if result == "corrected" and it.corrections < self.limits.after_correction:
            it.corrections += 1
            it.value = correction
            it.set("heard", "相手が訂正した")
            return self._ask_confirm(qid)
        it.set("unresolved", "訂正のあとも合わない" if result == "corrected" else "相手が答えなかった")
        return self._advance("「-」にします。")

    def _op_request_end(self, a: dict) -> dict:
        reason = a.get("reason")
        if reason == "callee_hurry" and self.busy_ask and not self.busy_asked:
            # 業務設定で「引き止める」を選んだとき：1回だけ、決まった一言で聞く。2回目の「急いでいる」は終える
            self.busy_asked = True
            return {"result": "busy_ask", "say": self.busy_ask,
                    "message": f"「{self.busy_ask}」と1回だけ聞いてください。相手が応じたら、いまの質問から続けます。"
                               f"応じなければ、もう一度 request_end を{ARG_LABEL['reason']}「{END_LABEL['callee_hurry']}」で呼んでください。"}
        if reason in ("callee_hurry", "callee_refused_call"):
            self.hurried = True
            self.current, self.phase = None, "closing"
            return {"result": "end", "message": "締めの言葉を言って終えてください。念のための確認はしません。"}
        if self.current is not None:
            # いま聞いている質問がある間は、終えられない（まだ聞いていない・言い直しの返事待ち・念のための確認の最中）
            # ⛔ 2026-10-13 の試験で見つけた漏れ：「まだ聞いていない項目」だけを数えていたため、言い直しの返事待ちや
            #    念のための確認の最中に「終わり」と報告されると、確認を飛ばして通話を終えていた
            return self._reject("まだ終えられません。")
        return self._advance("")

    # ── 内部
    def _ask_confirm(self, qid: str) -> dict:
        it = self.items[qid]
        it.set("checking", "言い直して確かめる")
        self.awaiting_confirm = qid
        self.confirm_requested_at = self.callee_turns
        # 場面：聞き返した・念のための確認・訂正のあとは「聞き取りにくかったあと」、それ以外は「ふつう」（phrases.CONFIRM）
        situation = "聞き取りにくかったあと" if (it.ask_backs or it.corrections or self.phase == "rechecking") else "ふつう"
        n = self.confirm_counts.get(situation, 0)
        self.confirm_counts[situation] = n + 1
        say = self.awaiting_say = self.confirm(it.value, situation, n)
        return {"result": "confirm_required", "value": it.value, "say": say,
                "message": f"言い直して確かめます。「{say}」と言って、相手の返事を待ってください。"}

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
        """差し戻す唯一の場所。理由が何であっても、いま求めていること（_now）を必ず添える。"""
        return {"result": "rejected", "message": f"{why}{self._now()}"}

    def _now(self) -> str:
        """いま求めていること（AIが次にすることを迷わないため）。
        ⛔ 2026-10-13 G-16 の2回目：Q2 の言い直しの返事を待っている間に Q3 を報告され、「いまの質問は Q2 です」とだけ返したため、
           AIは Q2 を最初から聞き直した（言い直しの返事を待っていることが、伝わっていなかった）。"""
        if self.awaiting_confirm is not None:
            say = self.awaiting_say
            return (f"いまは {self.awaiting_confirm} の言い直し「{say}」への、相手の返事を待っています。"
                    f"相手が肯定したら confirm_field を{ARG_LABEL['result']}「{CONFIRM_LABEL['affirmed']}」で、違う値を言ったら"
                    f"「{CONFIRM_LABEL['corrected']}」と{ARG_LABEL['correction']}で報告してください。"
                    f"相手がまだ答えていなければ、「{say}」と、もう一度聞いてください。")
        if self.current is None:
            return "質問は終わりです。締めの言葉を言って終えてください。"
        q = self.questions[self.current]
        return f"いまの質問は {q.id}「{q.text}」です。"

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
    conds = sorted({c for q in questions for c in q.labels})
    A = ARG_LABEL

    def fd(name, desc, props, required):
        return types.FunctionDeclaration(name=name, description=desc,
                                         parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=required))

    return [types.Tool(function_declarations=[
        fd("record_answer", "相手の答えを、いまの質問の選択肢に分類して報告する。"
           f"相手が「わからない」と答えたら「{SYSTEM_LABEL['unknown']}」、答えを拒んだら「{SYSTEM_LABEL['refused']}」",
           {A["question_id"]: s(qids), A["condition_id"]: s(conds), A["value"]: s(desc="聞き取った値（相手の言葉のとおり）")},
           [A["question_id"], A["condition_id"]]),
        fd("report_unclear", "相手の答えが聞き取れない・曖昧だったことを報告する",
           {A["question_id"]: s(qids)}, [A["question_id"]]),
        fd("confirm_field", "言い直した値への、相手の返事を報告する（相手が返事をしてから呼ぶ）",
           {A["question_id"]: s(qids), A["result"]: s(list(CONFIRM_LABEL.values())), A["correction"]: s(desc="相手が言い直した値")},
           [A["question_id"], A["result"]]),
        fd("request_end", f"電話を終えたいときに呼ぶ。相手が急いでいる・切ろうとしているなら「{END_LABEL['callee_hurry']}」、"
           f"通話自体を断られたなら「{END_LABEL['callee_refused_call']}」、質問が全部終わったなら「{END_LABEL['done']}」",
           {A["reason"]: s(list(END_LABEL.values()))}, [A["reason"]]),
    ])]
