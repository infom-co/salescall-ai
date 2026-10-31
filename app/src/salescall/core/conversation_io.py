"""会話の処理が書き込む入口（DESIGN.md 4-4・7-1）。持ち主 CL-2。

会話の処理（CL-1。サーバーで動く通話を進める部品 `supervisor.plugins.CallRunner`・通話後の検証）は、データベースへ直接書かず、
ここだけを呼んで書く（CL-1 の連絡帳 4-2 のお願い (a)。2026-10-24 ユーザー：入口は「CL-2」）。項目の値（`item_values`）を書けるのは
この部品だけ（機械の見張り app/checks/rules.py の WRITERS）。

- 取引は呼ぶ側が持つ（`access.in_client(actor, 契約者, 依頼者, lambda tx: conversation_io.write_item(tx, …))`。ほかの書く部品
  ＝target_recheck.record・attempts.record_verification と同じ形）。1つの取引に、状態の移り・答え・報告をまとめてもよい。
- **項目の値を1つ書く**（`write_item`）：通話の途中で答えが決まるたびに呼べる（会話の処理が落ちても、取れた答えは残る）。同じ
  質問をもう一度書けば、いまの値を書き換える。どの通話で取ったか（attempt_id）は必ず入る。前の通話の答えを上書きするときは、
  前の答えをデータベースが残す（0108）。書けるのは、次のときだけ（確かめは、それぞれ今の1か所を使う。2つ目を作らない）：
  - お客様が出た通話（`salescall.reached_answer`。発信禁止の申告 dnc.register_declared と同じ見方）で、通話中か通話後の検証が
    済む前（`verification` が無い・pending。検証が済むと締めが答えを読むので、そのあとは書き換えない）。
  - そのタスクのいちばん新しい通話（データベースの守り item_values_from_newest_call＝0108）。
  - 質問がその通話の業務設定の版にある（質問は版の業務設定 `business_settings.load` の1か所）。
  - 状態が項目の状態の一覧にある（データベースの種類 salescall.item_state が正本＝DESIGN.md 4-4）。
  - 選んだ選択肢は、その質問で返りうる答え（`business_settings.possible_answers`）のどれか。
  - 「わからない」「答えない」の理由は、その状態のときだけで（データベースの守り item_values_reason_only_when_not_answered と同じ
    決まり）、名前は業務の決まりの一覧（`business_rules.DONT_KNOW_REASONS`・`REFUSED_REASONS`）のどれか。理由が無いのは受け取る
    （判定が「理由が無い」＝reason_missing として、直す担当へ修正依頼を出す。答えそのものは失わない）。
  - ⏳ 値の形（型ごとの値の決まり）は、CL-1 が持つ予定の core/value_types.py（CL-1 の連絡帳 4-5・CL-2 の連絡帳 4-24 の約束）が
    できるまで見ない（勝手に2つ目の決まりを作らない）。
- **通話の途中に、通話ごとの書き手（supervisor.plugins.CallControl.write）へ頼む形**（`item_work`・`report_work`。0141。見直し20a の (7)）：
  受け口の決まりのうち、データベースを読まずに分かるもの（`check_item`・報告の形＝call_report.from_json）は、頼んだその場で確かめ、
  合わなければその場で ItemInvalid・ReportInvalid を返す（通話の部品が、その場で聞き直せる）。データベースを読む確かめ（質問が版に
  あるか など）は書き手が書くときに行い、書けなかった分は書き手が持って、通話の部品の flush（と通話の終わり）で返す。
  確かめの決まりは、どちらも `write_item`・`save_report` と同じ1か所（2つ目を作らない）。
  **状態の移り**も、書き手へは `move_work` で頼む（0143）：通話ごとの口が、頼まれた移り（話し終えた ending・ended など）を手元で持ち、
  データベースを読み直す前に、通話が終わりに向かっているかを決める（書き込みが遅くても、話し終えた電話におわびを流さない）。
- **会話の報告を置く**（`save_report`。形の確かめは call_report.from_json の1か所。1回だけ・変更不可）と、**録音から作り直した
  報告を置く**（`save_rebuilt_report`。通話後の検証が呼ぶ）。
- **「もうかけないで」をその場で台帳へ**（`declare_do_not_call`。dnc.register_declared をそのまま呼ぶ。原則14）。
- **会話に渡す材料を読む**（2026-10-24 CL-1 の連絡帳 4-37・4-41 の (5)。どれも今ある正本を読むだけ）：前の通話で取れた答え
  （`earlier`。続きから聞く電話＝目的 resume で、取れた答えの質問を伺わない）・質問ごとの「理由×扱い」（`handlings`。業務の決まり
  business_rules の1か所）。
- **データベースを通さないもの**（ここには無い）：通話の途中の「動いています」の知らせ（`beat`）と、甚大な問題でその電話をお詫びして
  終えること（`end_for_trouble`）は、通話を進める部品が受け取る通話ごとの口（supervisor.plugins.CallControl）で行う（お詫びして
  切るには、その回線の電話の部品が要るため。⛔ 0130 の「通話が進んだ知らせ」progress はデータベースに書いていた＝0134 で外した）。
- **使ったキーの識別名**（`record_keys`。CL-1 の連絡帳 4-34 の 4-67）：[{"service": "live"|"judge"|"ear"|"own_check",
  "key": "名前#指紋8桁"}]。値は残さない（データベースが形を守る）。
- **やり取りの文字**（`write_utterance`・通話ごとの書き手へ頼む形 `utterance_work`。読むのは `utterances`。2026-10-30 管理者のご決定 (6)
  「人が録音やその場のやり取りを確認できる状態にする必要があります」。計画書 11-1。表の作り 0152）：お客様と AI の発話の文字と時刻を、
  通話ごとに起きた順に残す（書き換えない。消すのは、録音と同じ Fly.io に残す日数を過ぎた分だけ＝`purge_utterances`・0157。依頼者ごとに
  分け、読める範囲は今の決まり＝11章）。形は `Utterance` の1か所：
  speaker＝"callee"（お客様。会話AIの聞き取り＝会話の側の Flow.callee_said）／"ai"（AI が言った文・流した録音の文）、turn＝相手の発話の
  番号（会話の側の Flow.callee_turns。通話の事実の根拠の発話の turn と同じ物差し。AI の発話は、そのときの番号）、said＝文字、at_s＝その
  通話の録音の上の位置（秒。会話の側の試作の通話の記録 turns の said_events・heard_events の at_s と同じ物差しにするかは CL-1 と決める）。
  書けるのは、お客様が出た通話で、通話後の検証が済む前（答えと同じ見方）。
"""

from __future__ import annotations

import json
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import ai_troubles, attempts, business_rules, business_settings, call_report, dnc, settings, tasks
from salescall.core.access import Access, Actor, Tx
from salescall.core.attempts import AttemptNotFound

__all__ = ["REFUSED", "ItemInvalid", "ItemValue", "MoveWork", "NoticeWork", "Utterance", "call_setting", "check_item", "check_utterance",
           "declare_do_not_call", "earlier", "handlings", "item_work", "move_work", "notice_work", "record_keys", "report_work",
           "save_rebuilt_report", "save_report", "utterance_work", "utterances", "write_item", "write_utterance"]

# 理由を持てる状態と、その理由の名前の一覧（正本は業務の決まり business_rules。ここは対応だけ）
_REASONS: dict[str, tuple[str, ...]] = {"dont_know": business_rules.DONT_KNOW_REASONS,
                                        "refused": business_rules.REFUSED_REASONS}


class ItemInvalid(Exception):
    """書けない項目の値（理由は日本語）。"""


@dataclass(frozen=True)
class ItemValue:
    """会話の処理が書く、1つの質問の答え（DESIGN.md 4-4）。"""
    question_id: str               # 質問の番号（その通話の業務設定の版の質問）
    state: str                     # 項目の状態（salescall.item_state のどれか）
    value: Any = None              # 取れた値（JSON にできる形。⏳ 形の確かめは core/value_types.py ができてから）
    condition: str | None = None   # 選んだ選択肢（business_settings.possible_answers のどれか）
    reason: str | None = None      # 「わからない」「答えない」の理由（dont_know・refused のときだけ）
    hedged: str | None = None      # 言い切っていない返事の言葉（お客様の言葉そのまま。CL-1 の 4-21）


@dataclass(frozen=True)
class Utterance:
    """やり取りの文字の1つ（お客様か AI の1つの発話）。形の確かめは `check_utterance` の1か所。"""
    speaker: str                   # "callee"（お客様）／"ai"（AI）
    turn: int                      # 相手の発話の番号（会話の側の callee_turns。0 以上）
    said: str                      # 発話の文字（空でない）
    at_s: float                    # その通話の録音の上の位置（秒。0 以上）


SPEAKERS = ("callee", "ai")


class UtteranceInvalid(ValueError):
    """書けないやり取りの文字（理由は日本語）。"""


# 受け口の決まりに合わないとして断った誤り（データベースの誤りではない＝直すのは頼んだ側。通話ごとの書き手は、見守り役の誤りに数えず、
# 通話の部品へ返す）
REFUSED: tuple[type[Exception], ...] = (ItemInvalid, call_report.ReportInvalid, UtteranceInvalid)


def check_item(item: ItemValue) -> None:
    """答えの形の確かめのうち、データベースを読まずに分かるもの（1か所。`write_item` も、頼んだその場の `item_work` も、これを通る）：
    質問の番号があること・理由はその状態のときだけで名前が決まった一覧にあること・言い切っていない言葉が空でないこと・値が JSON に
    できること。合わなければ ItemInvalid。"""
    if not isinstance(item.question_id, str) or not item.question_id.strip():
        raise ItemInvalid("質問の番号がありません")
    if item.reason is not None:
        allowed = _REASONS.get(item.state)
        if allowed is None:
            raise ItemInvalid("理由は「わからない」（dont_know）「答えない」（refused）のときだけ付けます")
        if item.reason not in allowed:
            raise ItemInvalid(f"理由「{item.reason}」は、「{item.state}」の決まった理由 {allowed} のどれでもありません")
    if item.hedged is not None and not item.hedged.strip():
        raise ItemInvalid("言い切っていない言葉が空です（無ければ None）")
    try:
        json.dumps(item.value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ItemInvalid("取れた値が JSON にできる形ではありません") from None


def item_work(attempt_id: UUID, item: ItemValue) -> Callable[[Tx], None]:
    """答えを1つ書く、通話ごとの書き手へ頼む形（`call.write(conversation_io.item_work(…))`）。データベースを読まずに分かる確かめ
    （`check_item`）は、いま行う（合わなければ、いま ItemInvalid）。書くときの確かめは `write_item` のまま。"""
    check_item(item)
    return lambda tx: write_item(tx, attempt_id, item)


@dataclass(frozen=True)
class MoveWork:
    """試行の状態を移す、通話ごとの書き手へ頼む形（`call.write(conversation_io.move_work(…))`。0143）。to_state を通話ごとの口が手元で
    読む（話し終えた ending・ended、出なかった no_answer などの終わりの移り＝通話は終わりに向かっている）。書くのは attempts.move の1か所。"""
    attempt_id: UUID
    to_state: str

    def __call__(self, tx: Tx) -> dict[str, Any]:
        return attempts.move(tx, self.attempt_id, self.to_state)   # type: ignore[arg-type]


@dataclass(frozen=True)
class NoticeWork(MoveWork):
    """お客様の番号へかけて、電話網のお知らせの声が返った試行を終える、通話ごとの書き手へ頼む形（`notice_work`。0151）。to_state は
    終わり方（attempts.NOTICE_ENDS の1か所が決めたもの＝通話ごとの口が、終わりの移りとして手元で読む）。"""
    notice: str | None = None

    def __call__(self, tx: Tx) -> dict[str, Any]:
        return attempts.end_by_notice(tx, self.attempt_id, self.notice)


def notice_work(attempt_id: UUID, notice: str | None) -> NoticeWork:
    """お知らせの声が返った試行の終わりを、通話ごとの書き手へ頼む形（`call.write(conversation_io.notice_work(試行, "power_off"))`）。
    notice＝link/phone.NOTICES のどれか（見分けられなければ None）。終わり方は attempts.end_by_notice の1か所が決める。"""
    if notice not in attempts.NOTICE_ENDS:
        raise ValueError(f"お知らせの種類は {attempts.NOTICES} のどれか（見分けられなければ None）です：{notice!r}")
    return NoticeWork(attempt_id, attempts.NOTICE_ENDS[notice], notice)


def move_work(attempt_id: UUID, to_state: str) -> MoveWork:
    """試行の状態の移りを、通話ごとの書き手へ頼む形（`call.write(conversation_io.move_work(試行, "ending"))`）。移ってよい先かは、いま
    確かめる（予約・発信・取り消しへは移せない＝attempts.move と同じ決まり。移れる組はデータベースが守る）。"""
    if to_state not in typing.get_args(attempts.LineState):
        raise ValueError(f"通話の部品が移せる状態は {typing.get_args(attempts.LineState)} のどれかです：{to_state!r}")
    return MoveWork(attempt_id, to_state)


@dataclass(frozen=True)
class TroubleWork:
    """止まったAI（core/ai_troubles.note）を書きうる、通話ごとの書き手へ頼む形の印（0155）。書き手は、書けたあと、その契約者の新しい
    電話を止めるかを決める入口（watch.after_ai_troubles）を呼ぶ（通話の終わりを待たない＝止まったAIで、同じ契約者のほかの回線が新しい
    電話をかけない）。"""
    attempt_id: UUID


@dataclass(frozen=True)
class ReportWork(TroubleWork):
    """会話の報告を置く、通話ごとの書き手へ頼む形（`report_work`。報告の ai_trouble も書く＝`save_report`）。"""
    raw: Any = None

    def __call__(self, tx: Tx) -> call_report.CallReport:
        return save_report(tx, self.attempt_id, self.raw)


@dataclass(frozen=True)
class AiTroubleWork(TroubleWork):
    """通話の途中で止まったAIを1つ書く、通話ごとの書き手へ頼む形（`ai_trouble_work`。通話ごとの口 call.ai_trouble が頼む）。"""
    trouble: call_report.AiTrouble | None = None

    def __call__(self, tx: Tx) -> list[str]:
        return ai_troubles.note(tx, self.attempt_id, (self.trouble,))


def verification_ai_trouble(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, attempt_id: UUID,
                            raw: Any) -> list[dict[str, Any]]:
    """通話後の確かめで、使うAI（別の耳など）が止まったときの入口（会話の側の通話後の確かめの部品が呼ぶ。1か所。2026-10-31 管理者のご決定：
    「通話後の確かめに使う別の耳が止まったとき：通話中は止めません。ただし、次の電話はかけず、確かめが通れば自動で再開します。」。0158）。
    raw は会話の報告の ai_trouble の1つと同じ形（形の確かめは call_report.ai_trouble の1か所。合わなければ ReportInvalid）。止まったかを
    決めるのは会話の側（通話中と同じ決め方）。書くのは通話中と同じ1か所（ai_troubles.note。どこで＝通話後の確かめ）、止めるかも同じ1か所
    （watch.after_ai_troubles。もう止まっていれば何もしない）。止めた停止の記録を返す。話している電話は切らない（止めるのは新しい電話だけ）。"""
    from salescall.core import watch   # watch は、ここ（会話の書き込み口）を通らないので、使うときに読む
    trouble = call_report.ai_trouble(raw)
    access.in_client(actor, tenant_id, client_id, lambda tx: ai_troubles.note(tx, attempt_id, (trouble,), stage="verification"))
    return watch.after_ai_troubles(access, actor, tenant_id)


def report_work(attempt_id: UUID, raw: Any) -> ReportWork:
    """会話の報告を置く、通話ごとの書き手へ頼む形（`call.write(conversation_io.report_work(…))`）。報告の形の確かめ（call_report.from_json
    の1か所）は、いま行う（合わなければ、いま ReportInvalid）。"""
    call_report.from_json(raw)
    return ReportWork(attempt_id, raw)


def ai_trouble_work(attempt_id: UUID, raw: Any) -> AiTroubleWork:
    """通話の途中で止まったAI（会話の報告の ai_trouble の1つと同じ形）を書く、通話ごとの書き手へ頼む形。形の確かめ（call_report.ai_trouble
    の1か所）は、いま行う（合わなければ、いま ReportInvalid）。"""
    return AiTroubleWork(attempt_id, call_report.ai_trouble(raw))


def write_item(tx: Tx, attempt_id: UUID, item: ItemValue) -> None:
    """その通話で取った答えを1つ書く（同じ質問なら書き換える）。書けなければ ItemInvalid。"""
    check_item(item)
    a = tx.one("SELECT task_id, version_id, verification::text AS verification,"
               " salescall.reached_answer(attempt_id) AS reached FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    if a is None:
        raise AttemptNotFound(str(attempt_id))
    if not a["reached"]:
        raise ItemInvalid("お客様が出ていない通話には、答えはありません")
    if a["verification"] not in (None, "pending"):
        raise ItemInvalid("通話後の検証が済んだ通話の答えは、書き換えません（締めが読んだあとです）")
    question = next((q for q in business_settings.load(tx, a["version_id"]).questions if q.id == item.question_id), None)
    if question is None:
        raise ItemInvalid(f"質問「{item.question_id}」は、この通話の業務設定の版にありません")
    if not tx.one("SELECT %s = ANY(enum_range(NULL::salescall.item_state)::text[]) AS ok", (item.state,))["ok"]:
        raise ItemInvalid(f"項目の状態「{item.state}」は、決まった状態のどれでもありません")
    if item.condition is not None and item.condition not in business_settings.possible_answers(question):
        raise ItemInvalid(f"選択肢「{item.condition}」は、質問「{item.question_id}」で返りうる答えではありません")
    tx.execute(
        "INSERT INTO salescall.item_values (tenant_id, client_id, task_id, version_id, question_id, state, value, condition,"
        "                                   reason, hedged, attempt_id)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (task_id, question_id) DO UPDATE SET version_id = EXCLUDED.version_id, state = EXCLUDED.state,"
        "   value = EXCLUDED.value, condition = EXCLUDED.condition, reason = EXCLUDED.reason, hedged = EXCLUDED.hedged,"
        "   attempt_id = EXCLUDED.attempt_id, updated_at = now()",
        (a["task_id"], a["version_id"], item.question_id, item.state,
         None if item.value is None else json.dumps(item.value, ensure_ascii=False), item.condition, item.reason, item.hedged, attempt_id))


def save_report(tx: Tx, attempt_id: UUID, raw: Any) -> call_report.CallReport:
    """会話の報告（版3の JSON の形）を確かめて置く（1回だけ。合わなければ call_report.ReportInvalid）。置いた報告を返す。"""
    report = call_report.from_json(raw)
    call_report.save(tx, attempt_id, report)
    ai_troubles.note(tx, attempt_id, report.ai_troubles)   # 止まったAI（通話の途中に書いた分は重ねない。0155）
    return report


def save_rebuilt_report(tx: Tx, attempt_id: UUID, raw: Any, *, evidence: str) -> int:
    """通話の録音から作り直した報告を置く（通話後の検証が呼ぶ。「判定できない」の通話を判定し直すため）。作り直しの番号を返す。"""
    return call_report.save_rebuilt(tx, attempt_id, call_report.from_json(raw), evidence=evidence)


def declare_do_not_call(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """通話の途中の「もうかけないで」を、その場で発信禁止の台帳へ（dnc.register_declared の1か所。何度呼んでも同じ）。"""
    return dnc.register_declared(tx, attempt_id)


def earlier(tx: Tx, task_id: UUID) -> dict[str, dict[str, Any]]:
    """その行で、前の通話までに取れた答え（質問の番号 → {"state", "value", "condition"}）。続きから聞く電話（目的 resume）で、取れた
    答えの質問を伺わないために会話へ渡す（CL-1 の連絡帳 4-37）。前の通話の答えは、この通話の答えとして書かない（取った通話の番号の
    まま残す＝write_item は、この通話で取った答えだけ）。正本は項目の値の表（item_values）。"""
    return {r["question_id"]: {"state": r["state"], "value": r["value"], "condition": r["condition"]} for r in tx.all(
        "SELECT question_id, state::text AS state, value, condition FROM salescall.item_values WHERE task_id = %s"
        " ORDER BY question_id", (task_id,))}


def handlings(tx: Tx, version_id: UUID, question_id: str) -> dict[tuple[str, str], str]:
    """その質問で、お客様が「わからない」「答えない」と言ったときの扱い（(状態, 理由) → 扱いの名前。business_rules.HANDLINGS の
    どれか）。正本は業務の決まり（business_rules.load の1か所）。必須でない質問は扱いを持たない（空）。CL-1 の連絡帳 4-41 の (5)。"""
    rules = business_rules.load(tx, version_id)
    return {(state, reason): h.kind for (q, state, reason), h in rules.handling.items() if q == question_id}


# 会話の処理が読む設定（持ち主 CL-1 の会話の決まり。値を持つのは設定の表の1か所。範囲は、その通話の業務）
CALL_SETTING_PREFIXES = ("call.", "conversation.")


def call_setting(tx: Tx, attempt_id: UUID, key: str) -> Any:
    """その通話の業務で、会話の決まりの設定がいくつか（settings.resolve の1か所。業務 → 依頼者 → 契約者 → システムの順で、細かい
    ほうが勝つ）。読めるのは会話の決まりの設定だけ（`CALL_SETTING_PREFIXES`）。例：受付で保留になったときに待つ長さ
    `call.hold_wait`（秒。初め300。お客様が長く待つ理由を言ったら、その時まで待つのは会話の側で決める）・`call.silence_prompt_after`。"""
    if not key.startswith(CALL_SETTING_PREFIXES):
        raise ValueError(f"会話の処理が読む設定ではありません：{key}")
    row = tx.one("SELECT task_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    return settings.resolve(tx, key, **tasks.scope(tx, row["task_id"]))


def check_utterance(u: Utterance) -> None:
    """やり取りの文字の形の確かめ（1か所。`write_utterance` も、頼んだその場の `utterance_work` も、これを通る）。合わなければ UtteranceInvalid。"""
    if u.speaker not in SPEAKERS:
        raise UtteranceInvalid(f"話した側は {SPEAKERS} のどれかです：{u.speaker!r}")
    if not isinstance(u.turn, int) or isinstance(u.turn, bool) or u.turn < 0:
        raise UtteranceInvalid("相手の発話の番号（turn）は 0 以上の整数です")
    if not isinstance(u.said, str) or not u.said.strip():
        raise UtteranceInvalid("発話の文字（said）が空です")
    if isinstance(u.at_s, bool) or not isinstance(u.at_s, (int, float)) or u.at_s < 0:
        raise UtteranceInvalid("録音の上の位置（at_s）は 0 以上の秒です")


def write_utterance(tx: Tx, attempt_id: UUID, u: Utterance) -> int:
    """やり取りの文字を1つ足す（起きた順の番号はここが付ける。書き換えない）。書けるのは、お客様が出た通話で、通話後の検証が済む前だけ
    （答え＝`write_item` と同じ見方）。付けた順の番号を返す。"""
    check_utterance(u)
    a = tx.one("SELECT verification::text AS verification, salescall.reached_answer(attempt_id) AS reached"
               " FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    if a is None:
        raise AttemptNotFound(str(attempt_id))
    if not a["reached"]:
        raise UtteranceInvalid("お客様が出ていない通話には、やり取りはありません")
    if a["verification"] not in (None, "pending"):
        raise UtteranceInvalid("通話後の検証が済んだ通話のやり取りには、足しません")
    return tx.one(
        "INSERT INTO salescall.call_utterances (tenant_id, client_id, attempt_id, seq, speaker, turn, said, at_s)"
        " SELECT salescall.cur_tenant(), salescall.cur_client(), %s,"
        "        coalesce((SELECT max(u.seq) FROM salescall.call_utterances u WHERE u.attempt_id = %s), 0) + 1, %s, %s, %s, %s"
        " RETURNING seq", (attempt_id, attempt_id, u.speaker, u.turn, u.said.strip(), u.at_s))["seq"]


def utterance_work(attempt_id: UUID, u: Utterance) -> Callable[[Tx], int]:
    """やり取りの文字を1つ足す、通話ごとの書き手へ頼む形（`call.write(conversation_io.utterance_work(…))`）。データベースを読まずに分かる
    確かめ（`check_utterance`）は、いま行う（合わなければ、いま UtteranceInvalid）。"""
    check_utterance(u)
    return lambda tx: write_utterance(tx, attempt_id, u)


def purge_utterances(tx: Tx) -> int:
    """残す日数を過ぎた通話のやり取りの文字を消す（契約者の取引のシステムの処理で。1か所。計画書 11-4。0157）。日数は録音と同じ設定
    （recording.keep_days_cloud＝Fly.io に残す日数。契約者ごと。初め7日）。消さないのは録音と同じ（salescall.recordings_held＝測り終わるまで・
    まだ決まっていない取り消せない決定）。決まりはデータベースの salescall.purge_utterances の1か所（守りも同じ決まりを確かめる）。
    消した数を返す。見守り役の1日1回の仕事が呼ぶ。"""
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    days = int(settings.resolve(tx, "recording.keep_days_cloud", tenant_id=tenant, client_id=None, business_id=None))
    return tx.one("SELECT salescall.purge_utterances(%s) AS n", (days,))["n"]


def utterances(tx: Tx, attempt_id: UUID) -> list[dict[str, Any]]:
    """その通話のやり取りの文字（起きた順。画面＝CL-3 が、録音と一緒に契約者へ見せる。取引の依頼者の分だけが見える）。"""
    return tx.all("SELECT seq, speaker, turn, said, at_s FROM salescall.call_utterances WHERE attempt_id = %s ORDER BY seq",
                  (attempt_id,))


def record_keys(tx: Tx, attempt_id: UUID, keys: list[dict[str, str]]) -> None:
    """その通話で使ったキーの識別名を残す（attempts.record_keys の1か所。形が合わなければデータベースが断る）。"""
    attempts.record_keys(tx, attempt_id, keys)
