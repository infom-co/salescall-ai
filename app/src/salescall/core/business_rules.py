"""業務の決まり（業務設定の版ごと）：断りの答え・「わからない」「答えない」の理由×扱い・再発信の方針・言ってはいけないこと。
持ち主 CL-2。
DESIGN.md 7-1・7-2・15-2-3。必須の質問そのものは、質問の表（questions.required）が正本（ここに写さない）。

- 形と確かめは、この1か所（`check`）。置く（`save`。業務設定の取込 (10) から呼ぶ）ときも、読む（`load`。判定・次の動き）
  ときも同じ確かめを通す。決まりの抜け（宣言の無い組）は受け取らない（既定値で埋めない。原則10）。
- 名前の一覧（理由・扱い・再発信の方針の鍵）は、ここが正本。足すときは、ここと試験を直す（処理に場合分けを書かない）。
- **言ってはいけないこと**（forbidden。Intent.md 6-2 の禁止事項）：名前 → 言ってはいけないこと（say_not）と、正しい案内
  （correct。誤って言ったときに、お詫びの電話で伝える正しい内容。無いもの＝失礼な言葉など は null）。会話の処理（CL-1）は
  これを指示に入れ、通話後の検証は、言ってしまったときに、どの名前に当たったかと実際に言ったことを残す。
  お詫びの電話の文面は、この2つからシステムが作る（契約者は確認と承認だけ。2026-10-16 ユーザー。DESIGN.md 10-1-2）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core.access import Tx

DONT_KNOW_REASONS = ("not_in_charge", "undecided", "unknown_now", "unspecified")
# 担当ではない／まだ決まっていない／いまは分からない／理由を言わなかった（受け皿）
REFUSED_REASONS = ("policy", "unwilling")        # 決まりで言えない／答えたくない（受け皿）
HANDLINGS = ("ask_who", "ask_when", "retry", "conclude")   # (a1) 分かるお客様 (a2) 分かる時 (b) 日を改める (d) 結論として終える
REFUSED_HANDLINGS = ("conclude",)                # 「答えない」は二度と聞かない ⇒ (d) だけ（15-2-3）
POLICY_KEYS = ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred", "callback_unspecified_time")
# 会話に至らなかった／一部だけ取れた／不在（戻る時刻は取れなかった）／取り次いでもらえなかった／かけ直しの時刻が決まっていない


class RulesInvalid(Exception):
    """業務の決まりの形が違う・抜けがある（理由は日本語）。"""


class RulesMissing(Exception):
    """その版の業務の決まりが無い（判断できない）。"""


@dataclass(frozen=True)
class Handling:
    kind: str
    conclusion: str | None       # conclude のときの結論の名前（依頼者へ渡す名前）


INTERVAL_UNITS = ("hours", "days")   # かけ直しの間隔：n 時間おき／n 日後（2026-10-15 ユーザー：業務ごとに選べるように）


@dataclass(frozen=True)
class Policy:
    action: str                  # call / end
    max_times: int | None        # その結果のあと、かけ直してよい回数（call のとき）
    interval: tuple[str, int] | None   # かけ直すまでの間隔（call のとき）：("hours", n)＝n 時間おき／("days", n)＝n 日後


@dataclass(frozen=True)
class Forbidden:
    say_not: str                 # 言ってはいけないこと
    correct: str | None          # 正しい案内（お詫びの電話で伝える。無ければ None）


@dataclass(frozen=True)
class Rules:
    version_id: UUID
    required: tuple[str, ...]                          # 必須の質問（並び順）。質問の表から
    decline: dict[str, frozenset[str]]                 # 質問 → 「用件を断られた」とする答え（選択肢）
    handling: dict[tuple[str, str, str], Handling]     # (質問, dont_know / refused, 理由) → 扱い
    retry: dict[str, Policy]                           # 再発信の方針の鍵 → 方針
    forbidden: dict[str, "Forbidden"]                  # 言ってはいけないことの名前 → 中身


def _int(v: Any, where: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise RulesInvalid(f"{where} は1以上の整数です")
    return v


def check(version_id: UUID, required: tuple[str, ...], questions: tuple[str, ...], raw: Any) -> Rules:
    """業務の決まりを確かめて読む（ただ1つの確かめ）。"""
    if not required:
        raise RulesInvalid("必須の質問が1つもありません（どんな通話でも「すべて取れた」になる。Intent.md 6-4）")
    if not isinstance(raw, dict) or set(raw) != {"decline", "handling", "retry", "forbidden"}:
        raise RulesInvalid("決まりは decline・handling・retry・forbidden の4つです")
    if not all(isinstance(raw[k], dict) for k in ("decline", "handling", "retry", "forbidden")):
        raise RulesInvalid("decline・handling・retry・forbidden は、どれも表（名前 → 中身）です")
    forbidden: dict[str, Forbidden] = {}
    for key, f in raw["forbidden"].items():
        if not isinstance(f, dict) or set(f) != {"say_not", "correct"}:
            raise RulesInvalid(f"言ってはいけないこと {key} は say_not（言ってはいけないこと）と correct（正しい案内）です")
        say_not, correct = f["say_not"], f["correct"]
        if not isinstance(say_not, str) or not say_not.strip():
            raise RulesInvalid(f"言ってはいけないこと {key} の say_not が空です")
        if correct is not None and (not isinstance(correct, str) or not correct.strip()):
            raise RulesInvalid(f"言ってはいけないこと {key} の correct は、正しい案内の文か null です")
        forbidden[key] = Forbidden(say_not.strip(), None if correct is None else correct.strip())
    decline: dict[str, frozenset[str]] = {}
    for q, answers in raw["decline"].items():
        if q not in questions:
            raise RulesInvalid(f"断りの答えの質問 {q} がありません")
        if not isinstance(answers, list) or not answers or not all(isinstance(a, str) and a.strip() for a in answers):
            raise RulesInvalid(f"質問 {q} の断りの答えは、選択肢の名前の並びです")
        decline[q] = frozenset(answers)
    handling: dict[tuple[str, str, str], Handling] = {}
    if set(raw["handling"]) != set(required):
        raise RulesInvalid(f"理由×扱いは、必須の質問ごとに1つずつ要ります（必須：{list(required)}）")
    for q, per_state in raw["handling"].items():
        if not isinstance(per_state, dict) or set(per_state) != {"dont_know", "refused"}:
            raise RulesInvalid(f"質問 {q} の理由×扱いは dont_know と refused の両方です")
        for state, reasons, allowed in (("dont_know", DONT_KNOW_REASONS, HANDLINGS),
                                        ("refused", REFUSED_REASONS, REFUSED_HANDLINGS)):
            given = per_state[state]
            if not isinstance(given, dict) or set(given) != set(reasons):
                raise RulesInvalid(f"質問 {q} の {state} は、理由 {list(reasons)} のすべてに扱いが要ります")
            for reason, h in given.items():
                if not isinstance(h, dict) or set(h) - {"kind", "conclusion"} or h.get("kind") not in allowed:
                    raise RulesInvalid(f"質問 {q} の {state}・{reason} の扱いは {list(allowed)} のどれかです")
                conclusion = h.get("conclusion")
                if (h["kind"] == "conclude") != (isinstance(conclusion, str) and bool(conclusion.strip())):
                    raise RulesInvalid(f"質問 {q} の {state}・{reason}：結論として終えるときだけ、結論の名前が要ります")
                handling[(q, state, reason)] = Handling(h["kind"], conclusion)
    if set(raw["retry"]) != set(POLICY_KEYS):
        raise RulesInvalid(f"再発信の方針は {list(POLICY_KEYS)} のすべてに要ります")
    retry: dict[str, Policy] = {}
    for key, p in raw["retry"].items():
        if not isinstance(p, dict) or p.get("action") not in ("call", "end"):
            raise RulesInvalid(f"再発信の方針 {key} は call か end です")
        if p["action"] == "end":
            if set(p) != {"action"}:
                raise RulesInvalid(f"再発信の方針 {key}：終えるときは、回数・間隔を持ちません")
            retry[key] = Policy("end", None, None)
        else:
            if set(p) != {"action", "max_times", "interval"}:
                raise RulesInvalid(f"再発信の方針 {key}：かけ直すときは、回数と間隔が要ります")
            iv = p["interval"]
            if not isinstance(iv, dict) or len(iv) != 1 or next(iter(iv)) not in INTERVAL_UNITS:
                raise RulesInvalid(f"再発信の方針 {key} の間隔は {{\"hours\": n}}（n 時間おき）か {{\"days\": n}}（n 日後）の1つです")
            unit = next(iter(iv))
            retry[key] = Policy("call", _int(p["max_times"], f"{key}.max_times"), (unit, _int(iv[unit], f"{key}.interval.{unit}")))
    return Rules(version_id, required, decline, handling, retry, forbidden)


def _questions(tx: Tx, version_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = tx.all("SELECT question_id, required FROM salescall.questions WHERE version_id = %s ORDER BY position",
                  (version_id,))
    return tuple(r["question_id"] for r in rows if r["required"]), tuple(r["question_id"] for r in rows)


def save(tx: Tx, version_id: UUID, raw: dict[str, Any]) -> Rules:
    """その版の業務の決まりを置く（1回だけ。変更不可）。確かめに通らなければ RulesInvalid（何も置かない）。"""
    required, questions = _questions(tx, version_id)
    rules = check(version_id, required, questions, raw)
    tx.execute("INSERT INTO salescall.business_rules (tenant_id, client_id, version_id, rules, saved_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())",
               (version_id, json.dumps(raw, ensure_ascii=False)))
    return rules


def load(tx: Tx, version_id: UUID) -> Rules:
    row = tx.one("SELECT rules FROM salescall.business_rules WHERE version_id = %s", (version_id,))
    if row is None:
        raise RulesMissing(str(version_id))
    required, questions = _questions(tx, version_id)
    return check(version_id, required, questions, row["rules"])
