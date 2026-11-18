"""人に見せる言葉の1か所（持ち主 CL-3。docs/DESIGN.md 7-3-1・docs/DASHBOARD.md 6-2）。

画面（契約者・管理者が見る）と、依頼者へ渡す出力の両方が、ここだけを読む。

決まり：
- core に言葉の正本があるもの（停止の原因・範囲＝stops.CAUSES・SCOPE_NAMES／門の理由＝gate.REASONS／知らせの種類＝notify.KINDS／
  「かけない」の段＝exclusions.LEVEL_NAMES／再発信の方針の鍵＝business_import.LABELS["retry_key"]）は、写さずに読む。
- ここが持つのは、core に言葉の無いもの（業務の状態・試行の状態・項目の状態・連携の状態・次の予定の種類・終わりの理由の一部）だけ。
- 1つの名前に「画面の言葉」と「依頼者へ出す言葉」の2つを持つ。依頼者へ出さない名前は client を None にする（依頼者へは、AI で
  電話したこと・仕組みのことを一切知らせない＝2026-10-16 ユーザー。docs/DESIGN.md 7-3-6）。
- 名前の一覧（データベースの値の一覧・core の名前）と突き合わせる試験がある（tests/test_output_words.py）。名前が増えたのに言葉が
  無ければ、試験が落ちる。画面に英字の名前を出さない。
"""

from __future__ import annotations

from dataclasses import dataclass

from salescall.core import ai_troubles, business_import, exclusions, gate, notify, stops


@dataclass(frozen=True)
class Word:
    screen: str            # 画面の言葉（契約者・管理者向け）
    client: str | None     # 依頼者へ出す言葉。None＝依頼者へ出さない（仕組みの中のこと）


# 業務の状態（docs/DESIGN.md 4-1・7-1。データベースの business_state）
BUSINESS_STATES: dict[str, Word] = {
    "open": Word("まだ結果なし", None),
    "completed": Word("完了", "完了"),
    "callback_scheduled": Word("折り返しのお約束", "折り返しのお約束"),
    "incomplete": Word("必須の質問が残っている", "一部のみお伺い"),
    "declined": Word("用件を断られた", "お断り"),
    "do_not_call": Word("今後の電話を断られた", "今後のお電話はお断り"),
    "out_of_scope": Word("範囲外（番号違いなど）", "対象外（番号違いなど）"),
    "not_reached": Word("目的のお客様に繋がらなかった", "ご担当の方に繋がらず"),
    "concluded": Word("結論として終えた", "ご回答をいただき終了"),
    "unknown": Word("判定できない（判定待ち）", None),
}

# 試行（1回の電話）の状態（docs/DESIGN.md 4-2。データベースの attempt_state）
ATTEMPT_STATES: dict[str, Word] = {
    "claimed": Word("予約", None),
    "dialing": Word("発信中", None),
    "ringing": Word("呼び出し中", None),
    "answered": Word("応答", None),
    "in_conversation": Word("通話中", None),
    "ending": Word("終えている途中", None),
    "ended": Word("終了", "最後までお話しできました"),
    "call_failed": Word("発信失敗", "つながりませんでした"),
    "no_answer": Word("応答なし", "応答なし"),
    "busy": Word("話し中", "話し中"),
    "dropped": Word("途中で切れた", "途中で切れました"),
    "technical_error": Word("こちらの不具合で終わった（結果不明）", "こちらの都合で途中で終わりました"),
    "released": Word("予約の取り消し（発信していない）", None),
}

# 項目（質問ごとの答え）の状態（docs/DESIGN.md 4-4。依頼者へ出す書き方は 7-1b・7-3-2 の決まり＝出力の処理が使う）
ITEM_STATES: dict[str, Word] = {
    "empty": Word("まだ伺っていない", "-"),
    "heard": Word("聞き取った（まだ確かめていない）", "-"),
    "unclear": Word("聞き取れなかった", "-"),
    "checking": Word("確かめている途中", "-"),
    "confirmed": Word("確かめ済み", None),     # 依頼者へは値（言葉ではない）
    "accepted": Word("取れた", None),         # 依頼者へは値・選んだ選択肢
    "dont_know": Word("「わからない」と答えた", "-"),
    "unresolved": Word("-（聞けなかった）", "-"),
    "refused": Word("✕（答えたくない）", "✕"),
}

# 連携（書き戻し）の状態（docs/DESIGN.md 4-5）
DELIVERY_STATES: dict[str, Word] = {
    "pending": Word("未送信", None),
    "sending": Word("送信中", None),
    "sent": Word("送信済み", None),
    "failed": Word("失敗（送り直します）", None),
    "retrying": Word("送り直し中", None),
    "dead": Word("送り先を休ませている（直ると自動で再開）", None),
}

# 次の予定の種類（docs/DESIGN.md 7-2。current_next_actions の kind）
NEXT_KINDS: dict[str, Word] = {
    "call": Word("かけ直し待ち", "対応中"),
    "hold": Word("判定待ち", None),
    "end": Word("済み", "済み"),
}

# 終わりの理由のうち、門の理由・業務の状態・再発信の方針の鍵のどれでもないもの（docs/DESIGN.md 7-2・next_action.py）
END_REASONS: dict[str, str] = {
    "number_unusable": "つながらない電話が続いた番号",
    "alternate_wrong_again": "伺い直した番号も番号違いだった",
    "alternate_not_called": "お客様が言った番号へは、業務の設定でかけない",
    "ask_number_again_unanswered": "番号の伺い直しに答えが無かった",
    "other_client_promise_never": "ほかの依頼者のお約束がある番号（かけない設定）",
    "apology_wrong_party": "お詫びの電話が番号違いだった",
    "apology_fax_line": "お詫びの電話がファクスだった",
    "max_attempts": "かける回数の上限に達した",
    "callback_exact_unreached": "お約束の時刻にかけたが繋がらなかった",
    "callback_unspecified_time": "時刻の決まらないお約束で、決めた日数を過ぎた",
    "callback_later_over": "「後ほど」のお約束の期限を過ぎた",
    "callback_month_ahead": "1か月以上先のお約束（前の決まりの記録）",
    "apology_unreached": "お詫びの電話がお客様に届かなかった",
    "apology_done": "お詫びの電話を終えた",
    "complaint": "苦情があった",
    "forbidden_speech": "言ってはいけないことを言った（契約者へ報告済み）",
    "other_means": "ほかの方法でのご連絡を希望された",
    "not_reached_departed": "目的のお客様は退職・異動された",
    "not_reached_who_unclear": "お名前だけではどの方か分からなかった",
}

# 取り消せない決定の保留で、待っている中身（call_report.DECISION_PARTS）
DECISION_PARTS: dict[str, str] = {
    "refusal": "営業のお断り",
    "wrong_party": "番号違い・別の会社",
    "complaint": "苦情（お怒り）",
    "fax": "ファクスの番号",
}

# 保留の種類（approval.WAIT_KINDS）
DECISION_KINDS: dict[str, str] = {
    "recording": "録音での確かめ待ち",
    "approval": "契約者の承認待ち",
}

POLICY_MAX_PREFIX = "policy_max:"
NO_WORD = "（言葉が登録されていない理由）"   # 試験で落とすもの。画面に英字の名前を出さないための受け皿


def _retry_key_words() -> dict[str, str]:
    """再発信の方針の鍵 → 言葉（正本は業務設定の取込の言葉の表＝写さずに裏返すだけ）。"""
    return {key: label for label, key in business_import.LABELS["retry_key"].items()}


def end_reason(name: str | None) -> str:
    """終わりの理由（次の予定の end_reason）を人に見せる言葉（画面だけ。依頼者へは出さない＝処理の列は「済み」だけ）。"""
    if not name:
        return ""
    if name in gate.REASONS:
        return gate.REASONS[name].message
    if name in BUSINESS_STATES:
        return BUSINESS_STATES[name].screen
    if name in END_REASONS:
        return END_REASONS[name]
    keys = _retry_key_words()
    if name.startswith(POLICY_MAX_PREFIX):
        key = name[len(POLICY_MAX_PREFIX):]
        return f"再発信の方針の回数を使い切った（{keys.get(key, NO_WORD)}）"
    if name in keys:
        return f"再発信の方針で終えた（{keys[name]}）"
    return NO_WORD


def stop_cause(name: str) -> str:
    """停止の原因の短い名前（正本は stops.CAUSES）。"""
    info = stops.CAUSES.get(name)
    return info.label if info else NO_WORD


def stop_scope(name: str) -> str:
    """停止の範囲の名前（正本は stops.SCOPE_NAMES）。"""
    return stops.SCOPE_NAMES.get(name, NO_WORD)


def notice_kind(name: str) -> str:
    """知らせの種類の名前（正本は notify.KINDS）。"""
    return notify.KINDS.get(name, NO_WORD)


def exclusion_level(name: str | None) -> str:
    """「かけない」の印の段（正本は exclusions.LEVEL_NAMES）。印が無ければ「かける」。"""
    return exclusions.LEVEL_NAMES.get(name or "call", NO_WORD)


def word(table: dict[str, Word], name: str | None) -> Word:
    """表から言葉を引く。名前が無い・表に無いときは「言葉が無い」を返す（英字を出さない。試験で落とす）。"""
    if name is None:
        return Word("", None)
    return table.get(name, Word(NO_WORD, None))


def keeping_kind(name: str) -> str:
    """止めているあいだも通す、お客様との約束を果たす電話の種類（正本は stops.KEEPING_KINDS）。"""
    return stops.KEEPING_KINDS.get(name, NO_WORD)


def ai_trouble_stage(name: str) -> str:
    """AI が止まった所（正本は ai_troubles.STAGES）。"""
    return ai_troubles.STAGES.get(name, NO_WORD)
