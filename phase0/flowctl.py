"""流れの制御（フェーズ0の試作。DESIGN.md 4-4・6-1・6-2・6-5・6-11 をそのまま形にしたもの）。

会話の位置・項目の状態・聞き返しと確かめの回数は、ここだけが持つ（Q6・Q7）。AIは操作を呼ぶだけで、
ここが確かめて「受け付けた」と返したものだけが記録になる（6-2）。G-6・G-16・G-13・G-5 の確かめは、すべてここを通す。

項目の状態（4-4 の名前だけを使う）：empty / heard / unclear / checking / confirmed / accepted / dont_know / unresolved / refused
すべての質問に、システムが2つの選択肢を足す（業務ごとに書き忘れない。I-19）：
  "unknown"（相手が「わからない」と答えた）⇒ dont_know。「取れた」には入らない。依頼主へは「-」。念のための確認はしない
    ⛔ 2026-10-13 ユーザー決定（CL-2 から依頼）：必須の質問が「わからない」なら、業務は完了ではない。前は accepted（＝取れた）に
       していたので、ほかがそろうと完了になっていた。⇒ 専用の状態にした（「取れた」は状態だけで決まる。条件を見なくてよい）
  "refused"（相手が答えを拒んだ）⇒ refused。依頼主へは「✕」。二度と聞かない
  ✅ 2026-10-13 ユーザー確定（DESIGN.md 6-2・4-4・7-1b）。設計の抜け（拒否を報告する操作が無い／「わからない」と
     答えた項目を念のため聞き直すことになっていた）を埋めたもの。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from phrases import CLOSE_DECLINED
from phrases import ask_again as ask_again_phrase
from phrases import confirm as confirm_phrase
from phrases import lead_in as lead_in_phrase

# 型の一覧（Q19）：その場で言い直して確かめる型。DESIGN.md 4-4
CONFIRM_TYPES = {"date", "time", "datetime", "number", "amount", "name", "phone", "email", "address"}
# 選択肢で答える型：依頼主へは、相手の言葉ではなく、宣言された選択肢を渡す（7-1b）
CHOICE_TYPES = {"yes_no", "choice"}
# 型の一覧（Q19）の全部。確かめない型は、選択肢の型と、自由な意見（free_text。2026-10-13 CL-2 の依頼で名前を決めた）
TYPES = CONFIRM_TYPES | CHOICE_TYPES | {"free_text"}
SYSTEM_CONDITIONS = ("unknown", "refused")
STATES = ("empty", "heard", "unclear", "checking", "confirmed", "accepted", "dont_know", "unresolved", "refused")
# 値の形をそろえる（型の一覧 Q19 の決まり）：確かめる型は、言い直しに使う値なので、言い終わりの「です」「。」などは値に入れない
# ⛔「ね」「よ」「で」などの1文字は外さない（名前の終わり＝「あかね」などを削るため）
# 確かめない型（選択肢・自由な意見）は、相手の言葉のまま（自由な意見の「よかったです」を「よかった」に変えない）
_TAIL = re.compile(r"(?:です|でございます|になります)?[。、．！？!?\s]*$")
# 空白に意味の無い型（日付・時刻・日時・数・金額）は、値の中の空白も外す。
# ⛔ 2026-10-14 G-34：相手の声を文字にしたもの（「15 日の午後 2 時でお願いします。」）を、AIがそのまま写して報告し、
#    値が「15 日の午後 2 時」になった（依頼主へ渡す値に空白が入る）。名前・住所・メールは、空白に意味がありうるので外さない
NO_SPACE_TYPES = {"date", "time", "datetime", "number", "amount"}
_SPACE = re.compile(r"[\s\u3000]+")


def normalize_value(qtype: str, value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if qtype in CONFIRM_TYPES:
        v = _TAIL.sub("", v).strip()
    if qtype in NO_SPACE_TYPES:
        v = _SPACE.sub("", v)
    return v or None
GOT = ("confirmed", "accepted")  # 「取れた」（4-4）

# 値の形の確かめ（型の一覧の決まり。DESIGN.md 6-2。✅ 2026-10-14 ユーザー承認）：日付・時刻・日時の型の値に、日付・時刻の言葉が無く、
# 別の型の値（数「3人」・返事の言葉「はい」・こちらの名前「答えなかった」）になっているときだけ止める。
# ⛔ G-34：AIが答えを報告せずに言い直したあと、次の質問の答え「3人」が日時の答えとして確かめまで済んだ。
# ✅ 2026-10-14 ユーザー：「『いつでも』『なるべく早く』は『即』ということなので、営業としては良いこと。日時よりも良い。こちら次第と
#    いうこと。『-』はありえない」⇒ 日時を決めない答えは、日時の質問の答えとして受け付ける（止めるのは、別の型の値だけ）。
# これまでの試験の値に当てて、取り違え 20回中20回を止め、正しい答え 570回中0回を止める（form_rule_eval.py）。
# 数・金額・名前などの型には当てない（数の型は単位を持たない。⏳ 数の型の案は DESIGN.md 6-2）
DATE_TIME_TYPES = {"date", "time", "datetime"}
DATE_TIME_WORDS = ("日", "時", "分", "月", "曜", "午前", "午後", "今日", "明日", "あした", "あす", "明後日", "あさって", "来週", "今週",
                   "再来週", "週", "朝", "昼", "夕方", "夕刻", "晩", "夜", "半", "正午", "年", "明け", "頃", "ごろ", "以降", "旬", "末", "初め", "中")


_NUMERAL = re.compile(r"[0-9０-９〇一二三四五六七八九十百千万]")
_REPLY_ONLY = re.compile(r"(?:はい|ええ|うん|いいえ|いえ|いや|そう|です|違います|ちがいます|大丈夫|結構|ございます|わかりません|わからない|"
                         r"分かりません|分からない|[、。，．！？!?\s])+")


def form_problem(qtype: str, value: str | None) -> str | None:
    """値の形が型に合わない理由（合っていれば None）。値の形をそろえた（normalize_value）あとの値に当てる。"""
    if not value or qtype not in DATE_TIME_TYPES or any(w in value for w in DATE_TIME_WORDS):
        return None
    if _NUMERAL.search(value):
        return "日付も時刻も無く、数だけがあります"
    if _REPLY_ONLY.fullmatch(value):
        return "日時ではなく、返事の言葉です"
    if value in _LABELS:
        return "相手の答えではなく、こちらの名前です"
    return None  # 「いつでも」「なるべく早く」「そちらのご都合で」など：日時を決めない答え（こちら次第＝営業にはよい答え）

# ── AIに見せる言葉（唯一の場所）。⛔ 2026-10-13 ユーザー指摘「こちらが英語を使っていませんか？」：指示文・操作の宣言・操作への返事に、
#    英語の名前（condition_id・unknown・callee_hurry・confirm_required など）が入っていた。AIが英語で自分の動きを声に出した回があった。
#    ⇒ AIに見せる名前は、すべて日本語にする。中の名前（英字）との置き換えは、AIの入口（Flow.handle）と操作の宣言（tool_declarations）
#    で、この表だけを使って行う。⚠️ 操作の名前（record_answer など）だけは、道具の決まりで英字のまま。
#    依頼主が決める選択肢（「はい」「いいえ」など）は、依頼主が書いたまま（日本語）で、置き換えない。
#    ⛔ 2026-10-13 G-26・G-28：項目名「相手の返事」を、AIが「与えの返事」「合い手の返事」と書き違えた（日本語の形の confirm_field 60回中4回。
#    英字の「result」は 82回中0回。「答えの種類」は 89回中0回）。⇒ 「答えの種類」と同じ形の「返事の種類」にした。
#    書き違えは名前を変えても0にはならない前提で、宣言に無い項目名・選択肢は、すべて差し戻す（Flow.apply の _check_args）。
ARG_LABEL = {"question_id": "質問", "condition_id": "答えの種類", "value": "値", "result": "返事の種類", "correction": "訂正された値",
             "reason": "理由"}
SYSTEM_LABEL = {"unknown": "わからない", "refused": "答えたくない"}
CONFIRM_LABEL = {"affirmed": "合っている", "corrected": "違う値を言われた", "no_answer": "答えなかった"}
END_LABEL = {"done": "質問が終わった", "callee_hurry": "相手が急いでいる", "callee_refused_call": "通話を断られた"}
REPLY_KEY_LABEL = {"result": "結果", "message": "次にすること", "question_id": "質問", "question": "質問の文", "value": "値", "say": "言い方"}
RESULT_LABEL = {"next": "次の質問", "confirm_required": "言い直して確かめる", "rejected": "差し戻し", "ask_back": "聞き返す",
                "recheck": "念のための確認", "end": "締め", "busy_ask": "引き止めの一言", "wait": "待つ"}
INTERNAL_KEYS = {"ack", "ask_n", "ask_reason", "apology"}  # 中で使うだけの項目（電話接続側・数え方が読む）。AIへの返事には入れない
# こちらが使う名前（AIに見せる日本語の名前）。値の欄に入っていたら、相手の答えではない（form_problem）
_LABELS = set(SYSTEM_LABEL.values()) | set(CONFIRM_LABEL.values()) | set(END_LABEL.values()) | {"答えた"}
_FROM_LABEL = {"condition_id": {v: k for k, v in SYSTEM_LABEL.items()}, "result": {v: k for k, v in CONFIRM_LABEL.items()},
               "reason": {v: k for k, v in END_LABEL.items()}}
_ARG_FROM_LABEL = {v: k for k, v in ARG_LABEL.items()}
# AIが呼べる操作（6-2）と、その項目（中の名前）：(必ず要る項目, 無くてもよい項目)。操作の宣言（tool_declarations）と、
# 受け取ったときの確かめ（Flow._check_args）は、この表だけを使う（宣言と確かめが食い違わないように）
OP_ARGS = {"record_answer": (("question_id", "condition_id"), ("value",)),
           "report_unclear": (("question_id",), ()),
           "confirm_field": (("question_id", "result"), ("correction",)),
           "request_end": (("reason",), ()),
           # 相手が考えている（比べる形 B。G-36）：相づちはプログラムが録音で流し、AIは黙って待つ。記録は書き換えない
           "report_thinking": (("question_id",), ())}
OPS = tuple(OP_ARGS)
SILENT_OPS = frozenset({"report_thinking"})  # 返事でAIに話させない操作（live_util の silent_ops）
# 選択肢が決まっている項目（中の名前 → その表）。答えの種類（condition_id）は質問ごとに違うので、record_answer の中で確かめる
_ENUM_ARGS = {"result": CONFIRM_LABEL, "reason": END_LABEL}


@dataclass
class Limits:
    """回数の上限（システム共通の設定の値。8章の「厳しいほう」）。DESIGN.md 6-11"""
    # その場の聞き返し（聞き取れない・答えが質問に合わない）：必須の項目は3回まで。
    # ✅ 2026-10-14 ユーザー：「『聞き返しは1項目1回まで』は大事なポイントは3回までOK」「必須の大事な部分の『分からない』は、極力なくすべき。
    #    安易に『-』にしたらダメ」「いたるところで聞き返すのは相手に不快感を与える」⇒ 必須でない項目は、その場では聞き返さない（これまでどおり）
    ask_back: int = 3
    recheck: int = 1           # 念のための確認：1項目1回まで
    after_correction: int = 1  # 訂正のあとの復唱：1回まで


@dataclass
class Question:
    id: str
    text: str
    type: str                   # 型の一覧の名前（"yes_no" "choice" "datetime" など）
    required: bool = True
    conditions: tuple = ()      # 業務が宣言した選択肢（依頼主が書いたまま。システムの2つは足さなくてよい）
    topic: str = ""             # 聞くこと（「ご希望の日時」など。聞き返しの言い方に使う。業務設定。無ければ言わずに聞き返す）

    def __post_init__(self):
        assert self.type in TYPES, f"{self.id} の型「{self.type}」は、型の一覧にありません（I-16）"
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
                 busy_ask: str | None = None, lead_ins: bool = True, thinking_op: bool = False):
        """confirm：言い直しの言い方を作る関数 (値, 場面, 何回目) → 文（ふだんは phrases.confirm。言い方を比べる試験 G-25 だけが別のものを渡す）。
        busy_ask：相手が「忙しい」と言ったときに1回だけ言う一言（業務設定。None なら丁寧に終える＝既定。Intent.md 9-4）。"""
        self.confirm = confirm
        self.busy_ask = busy_ask
        self.busy_asked = False
        self.confirm_counts: dict[str, int] = {}   # 言い直しの場面ごとの回数（同じ言い方が続かないよう、順番に回す）
        self.awaiting_say: str | None = None       # いま返事を待っている言い直しの、言い方（差し戻しのときも同じ言い方を使う）
        # lead_ins：プログラムの返事のあとに話す文の頭に、受けの言葉を置くか（DESIGN.md 6-11。✅ 2026-10-13 ユーザー承認＝既定で置く。
        # 置かない形は、過去の比べの試験（G-25・G-27 の W0）を同じ意味で動かすためだけに残す）
        self.lead_ins = lead_ins
        # thinking_op：相手が考えているとき、AIは黙って report_thinking を呼び、相づちはプログラムが録音で流す形（比べる形 B。G-36）。
        # ✅ 2026-10-14 ユーザー：S2・S4 が「えーとですね」のあとに言う「はい」が、物欲しそう・期待しているように聞こえる（L4 は問題なし）
        self.thinking_op = thinking_op
        self.lead_counts: dict[str, int] = {}
        self.ack_count = 0               # 次の質問の前に流す相づちを、順番に回す（_ack）
        self.unreported_correction: str | None = None  # AIが報告せずに言い直した値を、相手が直した質問（その答えの報告の相づちをお詫びにする）
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

    # ── AIの操作を受ける唯一の入口（AIに見せる言葉＝日本語）。ここで1回だけ中の名前に置き換え、返事を日本語に戻す。
    #    相づち（ack）は電話接続側が流すもので、AIには見せない（AIの返事から外す）
    def handle(self, fc) -> dict:
        return self._reply_out(self.apply(fc.name, self._args_in(dict(fc.args or {}))))

    # ── 中の名前での入口（handle と試験は、ここを通す）。記録（log）は中の名前で残す
    def apply(self, name: str, args: dict) -> dict:
        ops = OPS if self.thinking_op else tuple(o for o in OPS if o not in SILENT_OPS)
        if name not in ops:
            reply = self._reject(f"その操作はありません（使える操作は {'・'.join(ops)}）。")
        else:
            why = self._check_args(name, args)
            reply = self._reject(why) if why else getattr(self, f"_op_{name}")(args)
        reply["ack"] = self._ack(name, args, reply)
        self.log.append({"op": name, "args": args, "reply": reply, "callee_turns": self.callee_turns})
        return reply

    def _ack(self, name: str, args: dict, reply: dict) -> str | None:
        """報告が届いた瞬間に、電話接続側が流す相づち（phrases.ACK の中の名前。流さないなら None）。DESIGN.md 6-15。
        ✅ 2026-10-14 ユーザー：基本は「はい、」。違和感のある場面や「はい」が続くときは、別の相づち。
        - 言い直し（AIの言葉は「ありがとうございます。」などの受けの言葉から始まる）・締め・引き止めの一言 ⇒「はい、」
        - 次の質問・念のための確認 ⇒「承知しました、」「ありがとうございます、」を順番に（言い直しの「はい、」と交互になり、
          「はい」が続かない。AIの言葉は「それでは、」などのつなぎの言葉から始まるので、受けの言葉が2つ重ならない）
        - 相手が「わからない」「答えたくない」と答えた・通話を断られた ⇒「承知しました、」（お礼は場面に合わない）
        - 聞き返しても聞き取れず、先へ進む ⇒「はい、」
        - 言い直した値が違っていた ⇒「失礼いたしました。」／聞き取れなかった ⇒「申し訳ございません、」（お詫びの決まり。6-11）
        - 差し戻し ⇒ 流さない（AIはまだ受け付けられていない報告を出し直す）"""
        result = reply["result"]
        if result == "rejected":
            return None
        if result == "wait":
            return "aizuchi"  # 相手が考えている：相づち風に読んだ「はい。」（比べる形 B）
        if result == "ask_back":
            # 1回目の「答えが質問に合わない」は「はい、」（続けてAIが「恐れ入ります。（聞くこと）を、もう一度…」）。
            # 聞き取れなかったとき・2回目からは、申し訳なさそうに読んだ「申し訳ございません、」（✅ 2026-10-14 ユーザー）
            return "hai" if (reply.get("ask_reason") == "form" and reply.get("ask_n") == 1) else "moushiwake"
        if result == "confirm_required":
            return "shitsurei" if reply.get("apology") else "hai"  # お詫びかは、言い直しを作る所（_ask_confirm）が決めたもの
        if name == "record_answer" and args.get("condition_id") in SYSTEM_CONDITIONS:
            return "shouchi"
        if name == "report_unclear":
            return "hai"  # 聞き返しを使い切って先へ進む：聞き取れていないので、「承知しました」「ありがとうございます」は合わない
        if result in ("next", "recheck"):
            self.ack_count += 1
            return ("shouchi", "arigatou")[(self.ack_count - 1) % 2]
        if result == "end" and args.get("reason") == "callee_refused_call":
            return "shouchi"
        return "hai"

    @staticmethod
    def _check_args(name: str, args: dict) -> str | None:
        """宣言（OP_ARGS）に合わない報告は、記録を書き換える前に、どこが違うかを言って差し戻す。合っていれば None。
        ⛔ 2026-10-13 G-26・G-28：AIが項目名を書き違えた報告（「与えの返事」）を、そのまま受け取っていた。返事の種類が無いので
           「相手が答えなかった」とされ、相手が「はい」と認めた値が「-」になった（4回。どれも黙って起きた）。
           ⇒ 宣言に無い項目名・要る項目の抜け・選択肢に無い値・文字でない値は、すべて差し戻す。"""
        required, optional = OP_ARGS[name]
        usable = "・".join(ARG_LABEL[k] for k in required + optional)
        for k, v in args.items():
            if k not in required + optional:
                return f"「{k}」という項目は、{name} にはありません（使える項目は {usable}）。"
            if v is not None and not isinstance(v, str):
                return f"「{ARG_LABEL[k]}」は、文字で入れてください。"
        for k in required:
            if not (args.get(k) or "").strip():
                return f"{name} には「{ARG_LABEL[k]}」が要ります（使える項目は {usable}）。"
        for k, table in _ENUM_ARGS.items():
            if k in args and args[k] not in table:
                return f"{ARG_LABEL[k]}「{table.get(args[k], args[k])}」は選べません（選べるのは {'、'.join(table.values())}）。"
        return None

    @staticmethod
    def _args_in(args: dict) -> dict:
        out = {}
        for k, v in args.items():
            key = _ARG_FROM_LABEL.get(k, k)
            out[key] = _FROM_LABEL.get(key, {}).get(v, v)
        return out

    @staticmethod
    def _reply_out(reply: dict) -> dict:
        return {REPLY_KEY_LABEL.get(k, k): (RESULT_LABEL.get(v, v) if k == "result" else v) for k, v in reply.items()
                if k not in INTERNAL_KEYS}

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
        if cond not in SYSTEM_CONDITIONS and (why := form_problem(q.type, value)):
            return self._bad_form(qid, value, why)
        if value is None and q.type not in CHOICE_TYPES and cond not in SYSTEM_CONDITIONS:
            # ⛔ 2026-10-13 G-25 で見つけた漏れ：値の要る質問で値の無い答えを受け付け、言い直しに「Noneで、よろしいでしょうか。」を
            #    入れていた（確かめない型なら、依頼主へ値の代わりに条件の名前が渡っていた）
            return self._reject(f"答えの値がありません。相手が答えた値を「{ARG_LABEL['value']}」に入れて、もう一度報告してください。")
        it.condition, it.value = cond, value
        if cond == "refused":
            it.set("refused", "相手が答えを拒んだ")
            return self._advance("受け付けました（答えたくない）。この質問は二度としません。")
        if cond == "unknown":
            it.set("dont_know", "相手が「わからない」と答えた")
            return self._advance("受け付けました（わからない）。")
        it.set("heard", "聞き取れた")
        apology = self.unreported_correction == qid  # AIが報告せずに言い直した値を、相手が直していた
        self.unreported_correction = None
        if q.confirm or it.ask_backs or self.phase == "rechecking" or apology:
            return self._ask_confirm(qid, apology=apology)
        it.set("accepted", "確かめない型で、はっきり聞き取れた")
        return self._advance("受け付けました。")

    def _op_report_unclear(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is not None:
            return self._reject("いまは report_unclear では受け付けません。")
        if qid != self.current:
            return self._reject(f"{qid} は、いまの質問ではありません。")
        q, it = self.questions[qid], self.items[qid]
        return self._ask_again(qid, "unclear", "聞き取れない・曖昧")

    def _op_confirm_field(self, a: dict) -> dict:
        qid = a.get("question_id")
        if self.awaiting_confirm is None and qid == self.current:
            # ⛔ 2026-10-14 G-34（2回）：AIが答えを報告せずに自分で言い直し、相手の返事を confirm_field で報告してきた。前の差し戻しは
            #    「相手が答えたら record_answer で」としか言わず、相手がもう答えているのに AIが待つと読めた。1回は報告し直さず、次の質問の
            #    答え「3人」が日時の答えとして記録され、確かめまで済んだ。⇒ 答えがまだ報告されていないことと、報告する値を、はっきり言う
            v = normalize_value(self.questions[qid].type, a.get("correction"))
            self.unreported_correction = qid if a.get("result") == "corrected" else None
            shown = f"（「{v}」）" if v else ""
            return self._reject(f"{qid} の答えは、まだ record_answer で報告されていません。相手が答えた値{shown}を、いま record_answer で"
                                "報告してください。言い直しの言い方は、プログラムの返事のとおりにします。")
        if self.awaiting_confirm != qid:
            return self._reject(f"いまは、{qid} の言い直しを求めていません。")
        if self.callee_turns <= (self.confirm_requested_at or 0):  # I-24：相手の返事が届く前の報告は差し戻す
            return self._reject("相手の返事をまだ聞いていません。言い直したら、相手が返事をするまで待ってから報告してください。")
        it = self.items[qid]
        result = a.get("result")
        correction = normalize_value(self.questions[qid].type, a.get("correction"))
        if result == "corrected" and correction is None:  # 訂正の値の無い報告は、記録を書き換える前に差し戻す（上と同じ漏れ）
            return self._reject(f"訂正された値がありません。相手が言い直した値を「{ARG_LABEL['correction']}」に入れて、もう一度報告してください。")
        if result == "corrected" and (why := form_problem(self.questions[qid].type, correction)):
            self.awaiting_confirm = None  # 言い直しの返事待ちをやめて、質問から聞き直す（6-2）
            return self._bad_form(qid, correction, why)
        self.awaiting_confirm = None
        if result == "affirmed":
            it.set("confirmed", "相手が肯定した")
            return self._advance("確認できました。")
        if result == "corrected" and it.corrections < self.limits.after_correction:
            it.corrections += 1
            it.value = correction
            it.set("heard", "相手が訂正した")
            return self._ask_confirm(qid, apology=True)
        it.set("unresolved", "訂正のあとも合わない" if result == "corrected" else "相手が答えなかった")
        return self._advance("「-」にします。")

    def _op_report_thinking(self, a: dict) -> dict:
        """相手が考えている（「えーとですね、」で止まった）。記録は書き換えない。相づちの録音（aizuchi）を流し、AIは黙って待つ。
        電話接続の側は、ここから相手が黙っている秒数を数える（5秒で「いかがなさいましたか？」。DESIGN.md 6-15）。"""
        qid = a.get("question_id")
        if qid != self.current and qid != self.awaiting_confirm:
            return self._reject(f"{qid} は、いまの質問ではありません。")
        return {"result": "wait", "message": "相づちはプログラムが流しました。何も言わずに、相手が話し終えるまで待ちます。"
                                               "相手が答えたら、いつものとおり報告します。"}

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
            return {"result": "end", "say": CLOSE_DECLINED,
                    "message": f"「{CLOSE_DECLINED}」と言って終えてください。この言い方だけを使い、言葉を足しません"
                               f"（またお電話します、などの約束をしない）。念のための確認はしません。"}
        if self.current is not None:
            # いま聞いている質問がある間は、終えられない（まだ聞いていない・言い直しの返事待ち・念のための確認の最中）
            # ⛔ 2026-10-13 の試験で見つけた漏れ：「まだ聞いていない項目」だけを数えていたため、言い直しの返事待ちや
            #    念のための確認の最中に「終わり」と報告されると、確認を飛ばして通話を終えていた
            return self._reject("まだ終えられません。")
        return self._advance("")

    # ── 内部
    def _bad_form(self, qid: str, value: str, why: str) -> dict:
        """答えの値の形が質問の型に合わない（DESIGN.md 6-2）。値は記録せず、聞き返し（_ask_again）へ。"""
        q = self.questions[qid]
        return self._ask_again(qid, "form", f"値の形が合わない（{value}：{why}）",
                               f"{q.id}「{q.text}」の答えとして「{value}」が報告されましたが、{why}。")

    def _ask_again(self, qid: str, reason: str, why: str, shown: str = "") -> dict:
        """聞き返しの唯一の場所（DESIGN.md 6-11。聞き取れない・曖昧＝"unclear"／答えが質問に合わない＝"form"）。AIに選ばせず、ここで
        決める：必須の項目は、その場で3回まで聞き返す（言い方は段ごとに phrases.ask_again）。使い切っていたら・必須でない・念のための確認の
        最中なら、「-」にして次へ（必須なら、締めの前の念のための確認で1回）。⇒ どの場合も、AIへの返事に次にすることが必ず入る。"""
        q, it = self.questions[qid], self.items[qid]
        if self.phase == "asking" and q.required and it.ask_backs < self.limits.ask_back:
            it.ask_backs += 1
            it.set("unclear", f"{why}・聞き返す（{it.ask_backs}回目）")
            say = ask_again_phrase(it.ask_backs, reason, q.topic)
            return {"result": "ask_back", "question_id": q.id, "say": say, "ask_n": it.ask_backs, "ask_reason": reason,
                    "message": f"{shown}「{say}」と聞き返して、相手の答えを待ってください。相手が答えたら record_answer で報告します。"}
        it.set("unresolved", f"{why}・聞き返しを使い切った" if self.phase == "asking" else f"{why}・念のための確認でも聞けなかった")
        return self._advance(f"{shown}「-」にします。")

    def _ask_confirm(self, qid: str, apology: bool = False) -> dict:
        """言い直して確かめる。apology＝相手に値を直させた（こちらが違っていた）ので、お詫びの相づち「失礼いたしました。」を流し、
        言い直しの頭は、お礼・承りの言葉ではなく、言い直す合図の言葉にする（⛔ 2026-10-14 ユーザー：「失礼いたしました。」のあとの
        「かしこまりました」はおかしい）。"""
        it = self.items[qid]
        it.set("checking", "言い直して確かめる")
        self.awaiting_confirm = qid
        self.confirm_requested_at = self.callee_turns
        # 場面：聞き返した・念のための確認・訂正のあとは「聞き取りにくかったあと」、それ以外は「ふつう」（phrases.CONFIRM）
        situation = "聞き取りにくかったあと" if (it.ask_backs or it.corrections or self.phase == "rechecking") else "ふつう"
        n = self.confirm_counts.get(situation, 0)
        self.confirm_counts[situation] = n + 1
        # 言い直す値：値の無い選択肢の型（はい／いいえなど。聞き返したあとは復唱する＝6-11）は、選んだ選択肢で言い直す。
        # ⛔ 2026-10-14 に見つけた穴：値だけを渡していたため、「Noneで、よろしいでしょうか。」という文を作っていた
        shown = it.value or SYSTEM_LABEL.get(it.condition, it.condition)
        say = self.awaiting_say = self._lead("お詫びのあとの言い直し" if apology else "言い直し") + self.confirm(shown, situation, n)
        return {"result": "confirm_required", "value": shown, "say": say, "apology": apology,
                "message": f"言い直して確かめます。「{say}」と言って、相手の返事を待ってください。"}

    def _remaining(self) -> list[str]:
        return [qid for qid in self.order if self.items[qid].state == "empty"]

    def _advance(self, prefix: str) -> dict:
        rest = self._remaining()
        if self.phase == "asking" and rest:
            self.current = rest[0]
            q = self.questions[self.current]
            return {"result": "next", "question_id": q.id, "question": q.text,
                    "message": f"{prefix}次の質問は {q.id}「{q.text}」です。" + self._lead_note("次の質問")}
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

    def _lead(self, kind: str) -> str:
        """受けの言葉（lead_ins が無効なら空）。種類ごとに順番に回す。"""
        if not self.lead_ins:
            return ""
        n = self.lead_counts.get(kind, 0)
        self.lead_counts[kind] = n + 1
        return lead_in_phrase(kind, n)

    def _lead_note(self, kind: str) -> str:
        lead = self._lead(kind)
        return f"「{lead}」から話し始めてください。" if lead else ""

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
        # ⛔ 2026-10-13 G-28：AIが答えを報告せずに言い直し、差し戻されたあと、「いまの質問は Q2 です」としか伝わらず、
        #    報告しないまま言い直しを繰り返した。⇒ いますることの操作まで書く
        return (f"いまの質問は {q.id}「{q.text}」です。相手が答えたら record_answer で報告してください"
                f"（聞き取れなかったら report_unclear）。")

    # ── 依頼主へ渡す書き方（7-1b。Q20 の唯一の場所）
    def export(self, unknown_mark: str = "-", refused_mark: str = "✕") -> dict:
        out = {}
        for qid, it in self.items.items():
            if it.state == "refused":
                out[qid] = refused_mark
            elif it.state in GOT:
                if self.questions[qid].type in CHOICE_TYPES or it.value in (None, ""):
                    out[qid] = it.condition  # 選択肢の型は、宣言された選択肢（相手の言葉そのものではない）
                else:
                    out[qid] = it.value
            else:
                out[qid] = unknown_mark
        return out


def tool_declarations(questions: list[Question], thinking_op: bool = False):
    """AIが呼べる操作（6-2）の宣言を作る唯一の場所。条件の選択肢は、質問の宣言＋システムの2つ。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形は採らない（AIが受けの言葉を先に言わなかった）。
    thinking_op：相手が考えていることの報告（report_thinking）を足す（比べる形 B。G-36）。返事を待たない宣言にし、返事は話させない形
    （SILENT）で返す ⇒ AIは黙ったまま、相づちはプログラムが流す。"""
    from google.genai import types

    def s(enum=None, desc=None):
        return types.Schema(type=types.Type.STRING, enum=enum, description=desc)

    qids = [q.id for q in questions]
    conds = sorted({c for q in questions for c in q.labels})
    A = ARG_LABEL

    def fd(name, desc, props, required):
        # 項目と「必ず要る項目」は、受け取ったときの確かめと同じ表（OP_ARGS）から作る
        req, opt = OP_ARGS[name]
        assert set(props) == {A[k] for k in req + opt} and required == [A[k] for k in req], name
        behavior = types.Behavior.NON_BLOCKING if name in SILENT_OPS else None
        return types.FunctionDeclaration(name=name, description=desc, behavior=behavior,
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
    ] + ([fd("report_thinking", "相手が考えている（「えーと」「その」など、まだ答え終わっていない）ことを報告する。声は出さずに呼ぶ",
             {A["question_id"]: s(qids)}, [A["question_id"]])] if thinking_op else []))]
