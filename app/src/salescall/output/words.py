"""管理者・契約者・依頼者に見せる言葉の1か所（持ち主 CL-3。docs/DESIGN.md 7-3-1・docs/DASHBOARD.md 6-2）。

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

import json
import re
import unicodedata
from pathlib import Path
from dataclasses import dataclass

from salescall.core import ai_plans, ai_troubles, business_import, errors, exclusions, gate, notify, stops


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
    "unknown": Word("判定できない（決め直し中）", None),
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
# 依頼者へ出す言葉は、取れなかった答えで、お客様の言葉が無いときに答えの欄に書く「状態の短い言葉」（✅ 2026-11-08・2026-11-20 管理者
# 「「-」「✕」以外で記載する」＝CL-1 の連絡帳 4-206：お客様の言葉があれば、その言葉を書く。言葉の無い答えの書き方は CL-3 が決める）
ITEM_STATES: dict[str, Word] = {
    "empty": Word("まだ伺っていない", "伺っていない"),
    "heard": Word("聞き取った（まだ確かめていない）", "確かめる前に通話が終わった"),
    "unclear": Word("聞き取れなかった", "聞き取れなかった"),
    "checking": Word("確かめている途中", "確かめる前に通話が終わった"),
    "confirmed": Word("確かめ済み", None),     # 依頼者へは値（言葉ではない）
    "accepted": Word("取れた", None),         # 依頼者へは値・選んだ選択肢
    "dont_know": Word("「わからない」と答えた", "わからないとのこと"),
    "unresolved": Word("聞けなかった", "お答えをいただけなかった"),
    "refused": Word("答えたくないと言われた", "お答えを控えられた"),
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
    "hold": Word("結果の決め直し中", None),
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


def gate_reason(name: str) -> str:
    """門が止めた理由の言葉（正本は core の gate.REASONS。無い名前は NO_WORD＝試験が見つける）。"""
    return gate.REASONS[name].message if name in gate.REASONS else NO_WORD


def end_reason(name: str | None) -> str:
    """終わりの理由（次の予定の end_reason）を管理者・契約者に見せる言葉（画面だけ。依頼者へは出さない＝処理の列は「済み」だけ）。"""
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


# 電話を途中で終えた理由（core の errors.ENDS）のうち、こちらの不具合でないもの（errors.TROUBLES に無い）の画面の言葉。
# こちらの不具合の理由の言葉は、core の errors.LABELS が正本（ここに写さない。2026-11-18 CL-2 4-421 の (1)：worker_lost も、そこを読む）。
CALL_ENDS_NOT_OURS: dict[str, str] = {"poor_audio": "お声が聞き取れず終えた（こちらの不具合には数えない）"}


def call_end(name: str | None) -> str:
    """電話を途中で終えた理由の画面の言葉（1か所）。こちらの不具合＝errors.LABELS を読む／それ以外＝CALL_ENDS_NOT_OURS。
    依頼者へは出さない（依頼者へは、試行の状態の依頼者の言葉＝「こちらの都合で途中で終わりました」など）。"""
    if not name:
        return ""
    kind = errors.TROUBLES.get(name)
    if kind is not None:
        return errors.LABELS.get(kind, NO_WORD)
    return CALL_ENDS_NOT_OURS.get(name, NO_WORD)


# お客様の行の番号の名札と、どこから来たか（core の customer_contacts.NUMBER_LABELS・ORIGINS のすべてを書く。試験が突き合わせる）
NUMBER_LABEL_WORDS: dict[str, str] = {"company": "会社の番号", "designated": "お客様が指定した番号", "mobile": "携帯電話",
                                      "line": "LINE の番号", "said": "お電話で伺った番号", "fax": "ファクス番号"}
ORIGIN_WORDS: dict[str, str] = {"list": "お客様リスト", "call": "お電話で伺った", "dashboard": "ダッシュボードで足した"}


# お客様リストの取込の欄の画面の言葉（core の importer.FIELDS のすべてを書く。試験が突き合わせる。CL-2 4-217「見出しの文字は画面の言葉で」）
IMPORT_FIELD_WORDS: dict[str, str] = {
    "phone": "会社の電話番号", "client_ref": "管理番号", "name": "会社名", "name_reading": "会社名の読み",
    "person_name": "お呼びする方の名前", "person_name_reading": "お呼びする方の名前の読み", "person_role": "お呼びする方の役職",
    "person_full_name": "お呼びする方のフルネーム", "person_full_name_reading": "フルネームの読み", "person_department": "お呼びする方の部署",
    "exclude": "かけない", "designated_phone": "お客様が指定した電話番号", "mobile": "携帯電話番号", "email": "メールアドレス",
    "fax": "ファクス番号", "address": "住所", "line_id": "LINE ID", "meeting_id": "オンライン面談用の ID",
    "meeting_service": "オンライン面談のサービス名",
    # 連絡先の出どころ（2026-11-20 ユーザー「ア(お客様から伺ったものや会社の方から伺ったもの)…」。CL-2 の 0265）
    "contact_source": "連絡先の出どころ（この行のすべて）", "phone_source": "会社の電話番号の出どころ",
    "designated_phone_source": "指定の電話番号の出どころ", "mobile_source": "携帯電話番号の出どころ",
    "email_source": "メールアドレスの出どころ", "fax_source": "ファクス番号の出どころ", "address_source": "住所の出どころ",
    "line_id_source": "LINE ID の出どころ",
}


# テスト番号の結果（line_ring_tests.rang：鳴った／鳴らない／見分けられない＝core の ring_test の決まり）と、鳴らした目的（purpose の
# すべての値＝データベースの決まり。試験が突き合わせる）。契約者の画面だけ。
RING_WORDS: dict[bool | None, str] = {True: "鳴った（回線は元気）", False: "鳴らなかった（回線のせい）", None: "見分けられなかった"}
RING_PURPOSE_WORDS: dict[str, str] = {"resume_check": "再開の確かめ", "no_rows": "かける行が無い回線の確かめ"}


# 通話のやり取りで、話した側の名前（正本は core の conversation_io.SPEAKERS。試験が突き合わせる。契約者の画面だけ）
SPEAKER_WORDS: dict[str, str] = {"callee": "お客様", "ai": "こちら（AI）"}


# 設定の、画面の見出しに出す短い名前（1か所。設定の定義＝salescall.setting_definitions のすべての名前にあるかを試験が突き合わせる）。
# 説明の文の正本は CL-2 の setting_definitions.meaning（開いたときに出す）。
SETTING_LABELS: dict[str, str] = json.loads((Path(__file__).with_name("setting_labels.json")).read_text(encoding="utf-8"))
# ↑ 画面の言葉のデータ（設定の名前 → 短い名前）。値を読むものではないので、コードの中に設定の名前を並べず、データのファイルに置く
#   （設定の値を読んでよいのは決めた1か所だけ＝CL-2 の見張り。その見張りとぶつからないように）。


_DEV_MARK = re.compile(r"Intent|DESIGN|ユーザー|管理者「|✅|⏳|⛔|20\d\d-\d\d|CL-\d|0\d{3}|計画書|設計書")
# 説明の文の中の内部の言い方 → 契約者の画面の言い方（1か所）
_INNER_WORDS: dict[str, str] = {"見守り役": "システム", "電話接続モジュール": "電話をつなぐ部品", "別の耳": "聞き取りの確かめ",
                                "ユーザー": "管理者"}   # 計画書の「ユーザー」は、画面の呼び方では管理者（計画書 1-4）
# 修正依頼の担当（正本は core の fix_requests.ASSIGNEES。試験が突き合わせる）の、画面の呼び方。知らせの本文などに「CL-2 に修正依頼」のように
# 担当の名前が入るので、画面では役割の名前にする（契約者あての知らせにも同じ本文が出る）。
ASSIGNEE_WORDS: dict[str, str] = {"CL-1": "会話の担当", "CL-2": "業務の土台の担当", "CL-3": "画面と出力の担当"}
_ASSIGNEE = re.compile(r"\s?(CL-\d+)\s?")   # 英字の名前の前後の空き（「CL-2 に」）も一緒に（日本語の中では空けない）
_OPEN, _CLOSE = "（(", "）)"


def _drop_dev_parens(text: str) -> str:
    """括弧書き（入れ子も1つのまとまりとして数える）のうち、開発の覚え書きのしるしを含むものを、まとまりごと外す。"""
    out, stack = [], []   # stack：開いた括弧の、out の中の位置
    for ch in text:
        if ch in _OPEN:
            stack.append(len(out))
            out.append(ch)
        elif ch in _CLOSE and stack:
            start = stack.pop()
            out.append(ch)
            if not stack and _DEV_MARK.search("".join(out[start:])):
                del out[start:]
        else:
            out.append(ch)
    return "".join(out)


def screen_help(key: str, text: str) -> str:
    """設定の説明の文を、契約者の画面に出す形にする1か所：選び肢・設定の名前を言葉に置き換え（screen_meaning）、開発の覚え書きの入った
    括弧書き（決めた日付・文書の名前・担当の名前など）は出さず、内部の言い方は画面の言い方にする。⏳ 契約者向けの説明を定義に持って
    もらう＝CL-2 へのお願い 45（できたら、ここはそれを出すだけになる）。"""
    return plain(screen_meaning(key, text))


def plain(text: str) -> str:
    """データに入っている文（設定の説明・仮の値の見直す時・知らせの本文・修正依頼の中身 など）を、管理者・契約者の画面に出す形にする
    1か所：開発の覚え書きの入った括弧書きを外し、内部の言い方・担当の名前を画面の言い方にする。お客様の言葉には使わない（言い換えない）。"""
    out = _drop_dev_parens(text or "")
    for inner, word in _INNER_WORDS.items():
        out = out.replace(inner, word)
    out = _ASSIGNEE.sub(lambda m: ASSIGNEE_WORDS.get(m.group(1), "直す担当"), out)
    # 内部の英字の名前（approval.awaiting_ai_recheck など）を含む、ひと区切りの文（句点・括弧の内側まで）を外す（名前だけ外すと文が壊れる）
    out = _INTERNAL_CLAUSE.sub("", out)
    out = re.sub(r"[（(]\s*[）)]", "", out)   # 中身が無くなった括弧
    return re.sub(r"\s+([。、）])", r"\1", out).strip()


# 再発信の方針（結果ごとに、かけ直すか・回数・間隔）の結果の名前（鍵の正本は core の business_rules.POLICY_KEYS＝業務で決める・
# SYSTEM_POLICIES＝システムが決める。試験が突き合わせる）。業務設定の版の画面に出す（計画書 6-2「再発信の方針」）。
RETRY_POLICY_WORDS: dict[str, str] = {
    "open": "会話に至らなかった（お客様が出なかった）", "incomplete": "一部だけ取れた",
    "not_reached_absent": "目的のお客様が不在（戻る時刻は分からなかった）", "callback_unspecified_time": "時刻の決まっていないかけ直しの約束",
    "not_reached_not_transferred": "事情があり、取り次いでいただけなかった", "other_means": "ほかの方法でのご連絡を希望された",
    "not_reached_departed": "お呼びした方が在籍していない", "not_reached_who_unclear": "お名前だけではどの方か分からなかった",
}
INTERVAL_WORDS: dict[str, str] = {"hours": "{n}時間おき", "days": "{n}日後"}   # 鍵の正本は core の business_rules.INTERVAL_UNITS


def retry_policy(p: object) -> str:
    """再発信の方針1つを、画面の言葉にする（1か所）。"""
    if getattr(p, "action", None) == "end":
        return "かけ直さずに終える"
    unit, n = p.interval   # type: ignore[attr-defined]
    return f"かけ直す：{p.max_times}回まで・{INTERVAL_WORDS[unit].format(n=n)}"   # type: ignore[attr-defined]


# 赤字の確認ボタンの名前（承認待ちと、費用と報酬・依頼者のタブの知らせで同じ名前＝1か所）
DEFICIT_CONFIRM = "確かめた（これ以上お知らせしない）"


# 発信禁止の一覧の取り込みの欄の名前（欄の正本は core の dnc_import.FIELDS。試験が突き合わせる）
DNC_IMPORT_FIELD_WORDS: dict[str, str] = {"phone": "電話番号", "client_ref": "依頼者の管理番号"}


# 設定のまとまりの名前（設定の名前の頭。データベースの設定の定義のすべての頭を書く。試験が突き合わせる）
SETTING_GROUPS: dict[str, str] = {
    "calling": "かける時間帯", "gate": "かける前の確かめ（回数・間隔）", "callback": "かけ直し", "retry": "かけ直しの回数",
    "call": "通話の進め方", "callee": "お客様の扱い", "conversation": "会話", "line": "回線", "dial": "発信", "pc": "PC",
    "stop": "止める・解く", "quality": "品質の段（検証モード）", "verification": "通話のあとの確かめ", "judge": "判定",
    "decision": "取り消せない決定", "approval": "承認", "recording": "録音", "budget": "費用と予算", "backup": "控え",
    "measure": "測り", "dnc": "発信禁止", "ai": "AI", "business": "業務", "notify": "知らせ", "login": "ログイン", "output": "結果の出力",
}
SCOPE_WORDS: dict[str, str] = {"system": "初めの値（システム）", "tenant": "契約者の値", "client": "依頼者の値", "business": "業務の値",
                               "merged": "組み合わせた値"}


# 発信禁止に入った理由（core の dnc.Reason のすべてを書く。試験が突き合わせる）。契約者・管理者の画面の言葉（依頼者へは出さない）
DNC_REASON_WORDS: dict[str, str] = {
    "refused_in_call": "お電話の中で断られた", "refused_unclear_checked": "切れ方の分からない電話のあと、確かめの電話に出られなかった（断りとみなした）",
    "wrong_party": "番号違い（別の方の番号）", "fax_line": "ファクスの回線", "manual": "ダッシュボードで登録した",
    "client_list": "依頼者の一覧（かけないでとのお申し出）",
}


# 通話の終わり方の、依頼者へ出す言葉のうち、試行の状態の言葉（ATTEMPT_STATES の client）に無いもの（docs/DESIGN.md 7-3-4。順番の決まりは
# output/result_rows.call_end の1か所）。電話網のお知らせ（attempts.notice）・切った側（hangup_side）・電話が1本も無いとき
CALL_END_CLIENT: dict[str, str] = {
    "not_yet": "まだおかけしていません", "no_call_ended": "お電話していません",
    "notice:power_off": "電源が入っていないか圏外", "notice:out_of_range": "電源が入っていないか圏外", "notice:not_in_use": "使われていない番号",
    "side:callee": "お客様が途中で切られました", "side:line": "回線が途中で切れました", "side:unknown": "途中で切れました",
    "unknown": "不明",
}


# 単価の表の言葉と、見せる単位（データベースの price_versions の source・unit・tier の決まりのすべて。試験が突き合わせる）。
# 単価はデータベースでは「1単位あたりの円」。画面では管理者・契約者が読みやすい量あたりで見せ・入れる（PRICE_UNITS の倍数。決めるのはここだけ）
PRICE_SOURCE_WORDS: dict[str, str] = {"ai": "会話AI", "ear": "聞き取りの確かめ", "line": "回線", "judge": "意味の判定"}
PRICE_TIER_WORDS: dict[str, str] = {"free": "無料枠", "paid": "有料枠"}
PRICE_UNITS: dict[str, tuple[int, str]] = {"input_tokens": (1_000_000, "入力 100万トークンあたり"),
                                           "output_tokens": (1_000_000, "出力 100万トークンあたり"), "seconds": (60, "1分あたり")}


# 品質の段の指標の名前（core の quality_stage.METRICS のすべてを書く。試験が突き合わせる）。契約者・管理者の画面だけ
QUALITY_METRIC_WORDS: dict[str, str] = {
    "unknown_judgments": "結果を判定できなかった電話（同じ原因の件数）", "forbidden_speech": "言ってはいけないことを言った",
    "ear_disagreement": "聞き取りの食い違い（別の耳）", "complaint": "苦情・ご不満", "technical_error": "こちらの不具合で終えた電話",
}


# 業務設定の中身を見せる言葉（質問の型＝データベースの field_types のすべて・目的のお客様の種類＝business_settings.TARGET_PERSON_KINDS の
# すべて。試験が突き合わせる）。契約者・管理者の画面だけ
FIELD_TYPE_WORDS: dict[str, str] = {"date": "日付", "time": "時刻", "datetime": "日時", "number": "数", "amount": "金額", "name": "名前",
                                    "phone": "電話番号", "email": "メールアドレス", "address": "住所", "yes_no": "はい／いいえ",
                                    "choice": "選択肢", "free_text": "自由な答え"}
TARGET_PERSON_WORDS: dict[str, str] = {"role": "役職の方", "role_and_name": "役職（部署）とお名前の方", "name": "お名前の方",
                                       "anyone": "その店・会社の方ならどなたでも"}


# 選ぶ設定の選び肢の言葉（設定ごと。データベースの setting_definitions.choices のすべてを書く。試験が突き合わせる）
SETTING_CHOICE_WORDS: dict[str, dict[str, str]] = {
    "callback.month_ahead": {"never": "かけない（別の日に、時刻の決まっていない約束の方針でかける）", "with_approval": "契約者が認めたらかける"},
    "callback.until_other_party": {"now": "今から〇時まで", "after": "〇時から", "slot_after": "〇時から1回分"},
    "callback.until_preparing": {"now": "今から〇時まで", "after": "〇時から", "slot_after": "〇時から1回分"},
    "callback.until_available": {"now": "今から〇時まで", "after": "〇時から", "slot_after": "〇時から1回分"},
    "callback.until_unavailable": {"now": "今から〇時まで", "after": "〇時から", "slot_after": "〇時から1回分"},
    "gate.before_other_promise": {"call_other_voice": "声を替えてかける", "wait": "約束の時刻が過ぎるまで待つ", "never": "その番号へはかけない"},
    "output.customer_words": {"short": "縮める（最後の2つの言葉まで・1つ40字まで）", "full": "全部残す（言ったとおりに、縮めない）"},
    "judge.models": {"jev_and_sonnet": "先に安い判定を出し、確かでなければ Claude Sonnet 5.5", "sonnet": "Claude Sonnet 5.5 だけで判定する"},
}
# 契約プランの言葉（プランの名前の正本は core の ai_plans.PLANS。プランの設定の名前は ai_plans の1か所＝ここに書かない）
PLAN_WORDS: dict[str, str] = {"included": "AI料金込み（システムのキーで呼ぶ）", "own": "自分で API を契約する（契約者のキーで呼ぶ）"}
# 値の特別な名前（値の形 gap_or_never・day_or_gap の文字の値。値を見せる web/setting_forms と、説明の文の置き換えが同じ表を読む）
SETTING_VALUE_WORDS: dict[str, str] = {"never": "かけない", "next_day": "その日はかけない"}
_SETTING_KEY = re.compile(r"(?<![A-Za-z0-9_])([a-z]+)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*(?![A-Za-z0-9_])")   # 漢字の隣でも当てる（\b は漢字を語の一部と見る）


# 会話AI・業務を取り込む AI の枠の言葉（枠の名前の正本は core の ai_plans.TIERS。枠の設定の名前は ai_plans.TIER_SETTINGS の1か所＝ここに書かない。
# CL-2 4-446 の 55 で外から読める名前になったので、選び肢のそろいで推し量る形をやめた）
TIER_WORDS: dict[str, str] = {"free": "無料枠", "paid": "有料枠（おすすめ）"}


def _choices(key: str, choices: Any = None) -> dict[str, str]:
    if key in ai_plans.PLAN_SETTINGS.values():   # 契約プランの設定（どれがプランの設定かは ai_plans の1か所）
        return PLAN_WORDS
    if key in SETTING_CHOICE_WORDS:
        return SETTING_CHOICE_WORDS[key]
    if key in ai_plans.TIER_SETTINGS.values():   # 会話AI・業務を取り込む AI の枠の設定（どれが枠の設定かは ai_plans の1か所）
        return TIER_WORDS
    return {}


def setting_choice(key: str, value: str, choices: Any = None) -> str:
    return _choices(key, choices).get(value, NO_WORD)


def screen_meaning(key: str, text: str) -> str:
    """設定の説明の文（正本は CL-2 の setting_definitions.meaning）を画面に出す形にする1か所：選び肢の英字の名前はその言葉に、
    設定の名前（a.b の形）は、そのまとまりの名前の設定に置き換える。⏳ 説明の文そのものを画面に出せる日本語だけにする（CL-2 へのお願い 27）。"""
    out = text
    for value, label in sorted({**SETTING_VALUE_WORDS, **_choices(key)}.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"(?<![a-z_]){re.escape(value)}(?![a-z_])", f"「{label}」", out)
    return _SETTING_KEY.sub(lambda m: f"「{SETTING_GROUPS.get(m.group(1), '別')}」の設定", out)


def stop_scope(name: str) -> str:
    """停止の範囲の名前（正本は stops.SCOPE_NAMES）。"""
    return stops.SCOPE_NAMES.get(name, NO_WORD)


# 知らせの種類の、画面に出す短い名前（1か所）。core の notify.KINDS は種類の定義（開発の説明）なので、画面には出さない。
# 種類のすべてに名前があるかは、試験が notify.KINDS と突き合わせる（種類が増えたのに名前が無ければ落ちる）。
NOTICE_KIND_WORDS: dict[str, str] = {
    "stopped": "発信を止めました", "resumed": "止めていた発信を再開しました", "quality_stage": "品質の段が進みました",
    "judge_unknown": "結果を判定できなかった電話があります", "error_notice": "不具合がありました（直す担当へ依頼済み）",
    "calendar_sync_failed": "祝日の暦を取りに行けませんでした", "calendar_next_year_missing": "来年の祝日の暦がまだありません",
    "budget": "費用が上限を超えました", "forbidden_speech": "言ってはいけないことを言った電話があります",
    "complaint": "苦情のあった電話があります", "apology": "お詫びの電話の結果", "deficit_warning": "赤字になっています",
    "program_update": "PC のプログラムの更新が要ります", "backup_failed": "控えを取れませんでした",
    "answer_rate_drop": "電話に出てもらえる割合が下がりました", "sample_ready": "正解を付ける電話を抜き取りました",
    "old_promise_choice": "前の担当の方との約束を選んでください", "correction_call": "直した結果でおかけしてよいか確かめてください",
    "more_calls": "お約束・お詫びの電話で話せずに終えた行があります（さらにかけるか選んでください）",
    "month_ahead_approval": "1か月以上先のお電話を確かめてください", "month_ahead_unanswered": "【緊急】1か月以上先のお電話の答えがありません",
    "solution_held": "依頼者の解決策を、まだ入れていません", "solution_earlier": "入っていない前の解決策があります",
    "solution_unconfirmed": "解決策を入れるか選んでください", "declared_after_settle": "「もうかけないで」が電話のあとに届きました",
    "recording_needed": "録音が要る部分があります", "recording_list_needed": "録音の一覧がまだありません",
    "version_not_derived": "業務設定の新しい版を作れませんでした", "setting_missing": "設定がされていません",
    "pc_down": "PC からの合図が途切れました", "line_undecided": "回線のせいか番号のせいか決められません",
    "task_resting": "こちらの不具合で休ませたお客様がいます", "line_none_to_switch": "発信番号を替える回線がありません",
    "pc_line_conflict": "同じ回線を2台の PC がつないでいます", "decision_approval": "取り消せない決定の確かめを待っています",
    "decision_assumed": "決められなかった決定を、設定どおり決まったものとしました",
    "emergency_route": "緊急の道で再開しました", "ai_newer_version": "使っている AI に新しい版が出ました",
    "ai_version_missing": "使っている AI の版が見つかりません", "ai_version_unchecked": "AI の新しい版を確かめられませんでした",
}


def notice_kind(name: str) -> str:
    """知らせの種類の、画面に出す短い名前（NOTICE_KIND_WORDS の1か所。種類の正本は core の notify.KINDS）。"""
    return NOTICE_KIND_WORDS.get(name, NO_WORD) if name in notify.KINDS else NO_WORD


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


# ── 依頼者へ出さない言葉（docs/DESIGN.md 7-3-6。✅ 2026-11-18 管理者が決めた一覧）────────────────────────
# 依頼者は、契約者に電話業務を頼んでいると思っている（2026-10-16）。依頼者へ出すもの（ファイル・報告書・書き戻し・出力の型の言い換え）に、
# 仕組みの言葉を出さない。確かめるのは、こちらが作る言葉だけ（お客様の言葉・依頼者のファイルから来た値は材料なので確かめない）。
NOT_FOR_CLIENT: tuple[str, ...] = ("AI", "人工知能", "自動", "システム", "system", "データベース", "ボット", "bot", "ロボット",
                                   "判定", "検証", "模擬")
_ASCII_WORD = re.compile(r"[A-Za-z]+")
# 内部の英字の名前（下線・点でつないだ名前。例：do_not_call・DO_NOT_CALL・gate.same_contact）。漢字の隣でも当てる（\b は使わない）。
_INTERNAL_NAME = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]+(?:[_.][A-Za-z0-9]+)+(?![A-Za-z0-9_])")
_INTERNAL_CLAUSE = re.compile(r"[^（(。\n]*" + _INTERNAL_NAME.pattern + r"[^。）)\n]*。?")   # その名前を含むひと区切り（plain が外す）


def not_for_client(text: str) -> list[str]:
    """こちらが作った言葉のうち、依頼者へ出してはいけない言葉（見つかったもの。空なら出してよい）。英字の言葉は大文字・小文字を問わず、
    英字の語として当たったときだけ数える（「Taibot」のような語の一部では数えない）。内部の英字の名前（下線でつないだ名前）も当てる。"""
    t = unicodedata.normalize("NFKC", text)   # 全角の英字（ＡＩ）・半角のカタカナ（ｼｽﾃﾑ）も同じ字として当てる
    found = [w for w in NOT_FOR_CLIENT if not w.isascii() and w in t]
    ascii_words = {m.group(0).lower() for m in _ASCII_WORD.finditer(t)}
    found += [w for w in NOT_FOR_CLIENT if w.isascii() and w.lower() in ascii_words]
    found += [m.group(0) for m in _INTERNAL_NAME.finditer(t)]
    alone = {m.group(0).lower() for m in _ASCII_WORD.finditer(_INTERNAL_NAME.sub(" ", t))}   # つないだ名前の中の語は、上で当てた
    found += sorted(w for w in alone & _internal_words() if w not in {f.lower() for f in found})   # 1語の内部の名前（例：hold）
    return found


def _internal_words() -> frozenset[str]:
    """内部の英字の名前（1語のものも）。正本は、この言葉の表の鍵（データベースの値・core の名前と突き合わせて試験している）。"""
    global _INTERNAL_WORDS
    if _INTERNAL_WORDS is None:
        keys: set[str] = set()
        for v in list(globals().values()):
            if isinstance(v, dict):
                keys |= {k for k in v if isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]*", k)}
        _INTERNAL_WORDS = frozenset(w for k in keys for w in re.findall(r"[a-z]+", k) if len(w) >= 3) | frozenset(keys)
    return _INTERNAL_WORDS


_INTERNAL_WORDS: frozenset[str] | None = None

