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
from typing import Any

from salescall.core import ai_troubles, business_import, errors, exclusions, gate, notify, stops


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

# 止まっていることの画面の言葉（1か所。2026-11-21 の見直し：「止めています」「何で止まったか」と画面ごとに違い、一覧から再開できることが
# 分からなかった）。上の帯は「止まっている」、全体の画面の名前は「止まっているもの」（page("stops")）
STOP_STATE = "止まっています"            # 依頼者の札・ホームの表の札
STOP_PAGE = "止まった理由と再開"          # 1つの停止の画面の名前（理由を見て、ここで再開する）
STOP_GO = "理由を確かめて再開する"        # 一覧・札から、その画面へ移る言葉

# 次の予定がまだ1つも無いお客様（取り込んだまま、まだ一度もかけていない）の、画面の呼び方（ホームの依頼者ごとの表・お客様リストの札＝parts.next_tag）。
# 2026-11-21 の見直し：前は「これから」で、何の数か分からなかった
NOT_PLANNED = "まだかけていない"

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
RING_PURPOSE_WORDS: dict[str, str] = {"resume_check": "再開の確かめ", "no_rows": "かけるお客様が残っていない回線の確かめ"}


# 通話のやり取りで、話した側の名前（正本は core の conversation_io.SPEAKERS。試験が突き合わせる。契約者の画面だけ）
SPEAKER_WORDS: dict[str, str] = {"callee": "お客様", "ai": "こちら（AI）"}


# 設定の、画面の見出しに出す短い名前（1か所。設定の定義＝salescall.setting_definitions のすべての名前にあるかを試験が突き合わせる）。
# 説明の文の正本は CL-2 の setting_definitions.meaning（開いたときに出す）。
SETTING_LABELS: dict[str, str] = json.loads((Path(__file__).with_name("setting_labels.json")).read_text(encoding="utf-8"))
# ↑ 画面の言葉のデータ（設定の名前 → 短い名前）。値を読むものではないので、コードの中に設定の名前を並べず、データのファイルに置く
#   （設定の値を読んでよいのは決めた1か所だけ＝CL-2 の見張り。その見張りとぶつからないように）。


_DEV_MARK = re.compile(r"Intent|DESIGN|ユーザー|管理者「|✅|⏳|⛔|20\d\d-\d\d|CL-\d|0\d{3}|計画書|設計書|G-\d+|\d+-\d+ の\d+")   # G-10＝確かめの番号・18-2 の17＝文書の番号
# 説明の文の中の内部の言い方 → 契約者の画面の言い方（1か所）
_INNER_WORDS: dict[str, str] = {"見守り役": "システム", "電話接続モジュール": "電話をつなぐ部品", "別の耳": "聞き取りの確かめ",
                                "門の不許可の記録": "かける直前の確かめで見送った記録", "門の不許可": "かける直前の確かめで見送ったこと", "門": "かける直前の確かめ",
                                "ユーザー": "管理者",   # 計画書の「ユーザー」は、画面の呼び方では管理者（計画書 1-4）
                                # 緊急の道の取り次ぎの名前は、画面では「予備の窓口」（お客様の言葉が米国を通ることは、言い換えても残す）
                                "OpenRouter を通して Google が預かる Claude": "予備の窓口から同じ AI に頼む道",
                                "OpenRouter（米国）を通して Google（米国）へ": "米国の予備の窓口を通して、米国の AI へ",
                                # 止まったものを動かすのは、画面では「再開」（✅ 2026-11-21 管理者「伺い1：ア」）。CL-2 の停止の部品の文の「解く」を
                                # 形ごとに言い換える（長い形から先に。CL-2 へのお願い 65 で直していただくまでのあいだ）
                                "解かれていない": "再開していない", "解かずに": "再開せずに", "解きました": "再開しました", "解けたら": "再開できたら",
                                "解かない": "再開しない", "解いたあと": "再開したあと", "解いた": "再開した", "解くと": "再開すると", "解ける": "再開できる",
                                "解く": "再開する"}
# 契約者の画面に出さない内部の言い方（1か所。上の言い換えの表の元の言葉と、画面の作りの言い方「この段」）。全部の画面を回る試験が見張る
# （専門の「門」・門番は当てない）
CONTRACTOR_INNER = re.compile("|".join([r"(?<!専)門(?!番)" if w == "門" else re.escape(w) for w in _INNER_WORDS]
                                      + ["この段", "OpenRouter", "サイトの住所", "クライアント ID"]))   # 作りの言い方（サーバーの設定の名前）も出さない
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


# 必須の質問の答えの数の分け方（core の judge.ITEM_MARKS と、判定が伺っていないとした not_asked のすべてを書く。試験が突き合わせる）。
# 依頼者へ渡す印とそろえる（わからない＝「-」・答えたくない＝「✕」。CL-2 4-449）
ITEM_MARK_WORDS: dict[str, str] = {"taken": "取れた", "dont_know": "わからない（-）", "refused": "答えたくない（✕）",
                                   "open": "まだ取れていない", "not_asked": "伺っていない（対象外）"}


# 電話の台本を作る画面がまだ無いことを伝える1つの文（業務のタブの「はじめかた」・実施回を作る小窓・台本の画面が、同じこの文を出す。
# 2026-11-20 の見直し：3か所が「取り込んで」「準備中」とばらばらに書き、行き止まりが分からなかった。作り直しの形は計画書 6-1）
# 電話の進め方（業務ごと。計画書 原則9）の名前と説明の1か所。画面では「本番」と呼ばず「フルオート」にそろえる（1つのものに1つの呼び方。
# 2026-11-21 の見直し：誤判定率の画面だけ「本番」と書き、説明も無かった）。業務の画面の札と誤判定率の画面が同じ文を出す
MODE_WORDS: dict[str, tuple[str, str]] = {
    "verification": ("検証モード", "発信の前にお客様ごとに承認し、結果を確かめてから確定する進め方です。少ない件数の段から始め、指標が基準の内なら次の段へ進みます。"),
    "full": ("フルオート", "承認なしでかけ、判定した結果をそのまま確定する進め方です（電話の中身は検証モードと同じです）。"),
}


SCRIPT_NOT_READY = ("電話の台本は、依頼者の資料一式を読み込んで AI が案を組み、契約者が確かめて決める形で作ります（その画面は、いま作って"
                    "いるところです）。台本が入ると、ここに中身が出て、お客様リストを作って電話を始められます。")


# 電話の目的（データベースの attempts.purpose の決まり attempts_purpose_kind のすべて。試験が突き合わせる）。記録のすべての書き出しで使う
CALL_PURPOSE_WORDS: dict[str, str] = {"normal": "ふつうの電話", "ask_number_again": "番号を伺い直す電話", "apology": "お詫びの電話",
                                      "resume": "途中で切れたあとのかけ直し"}


# 結果の版の出どころ（データベースの result_kind のすべて。試験が突き合わせる）。記録のすべての書き出しの「結果の版」のシートで使う
RESULT_KIND_WORDS: dict[str, str] = {"judged": "判定で決めた", "human": "契約者・管理者が直した", "rejudged": "判定し直した"}


# 判定の中身（core の judge._detail が書く task_results.detail）の種類の名前。記録のすべての書き出し（output/record_book の「お客様のお話」の
# シート）で使う。judge._detail が書く鍵のすべてを書く（試験が judge の作りと突き合わせる）。契約者の手元の控えだけ（依頼者へは出さない）
DETAIL_KIND_WORDS: dict[str, str] = {
    "cut": "切れ方（判定の読み）", "cut_recorded": "切れ方（記録したもの）", "target_unconfirmed": "目的のお客様だと確かめられなかった",
    "not_asked": "伺っていない質問", "not_agreed": "ご納得いただけなかった点", "other_wish": "ほかのご希望", "unfinished": "手順を伺い終えていない質問",
    "concern": "言っていただいた気になる点", "elsewhere": "ほかの会社に頼んでいると言われて伺った問題・心配事",
    "handoff": "詳しい者へ引き継ぐ申し出への答え", "close_offer": "締める前の申し出への答え", "offer_terms": "条件つきで受けていただいた申し出",
    "carried": "新しい担当の方に引き継ぎを確かめた答え", "refusal_said": "お断りのお言葉（断り文句）", "clock24": "言い直して確かめた時刻",
    "another_day": "日を改めて締めた", "callee_questions": "お客様からのご質問", "brief": "手短にと言われて続けた答え",
    "elsewhere_whole": "ご案内全体をほかの会社に頼んでいると言われた", "elsewhere_point": "ほかの会社に決めた決め手",
    "cut_before_choice": "方法を選んでいただく前に切れた",   # ほかのご希望の1件の印（CL-2 の 4-463。読む所は call_report.OtherWish.cut_before_choice）
}


# 録音の組の単位の、画面の名前（承認する録音の種類。core の recordings.create_set の unit のうち、契約者が承認するもの）
RECORDING_UNIT_WORDS: dict[str, str] = {"business": "業務の録音", "name_role": "名前の録音（役職の部分）",
                                        "name": "名前の録音（お名前の部分）", "name_tail": "名前の録音（続く言葉）"}


# 業務・期間・声の型の指標（計画書 9-8。数えるのは core の metrics.business の1か所＝画面は並べるだけ）の、画面の名前と形。
# 鍵は metrics.business の答えのすべて（unpriced と ai_voice は別の扱い。試験が突き合わせる）。形：count＝本数・件数／rate＝{hits, total, value}／
# seconds＝秒／yen＝円
METRIC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("dialed", "かけた電話", "count"), ("answered", "電話に出た", "count"), ("talked", "お客様と話した", "count"),
    ("reached", "目的のお客様と話せた", "count"), ("completion", "業務完了率", "rate"), ("required_taken", "必須の質問の取得率", "rate"),
    ("cut", "途中で切られた率", "rate"), ("average_talk_seconds", "平均通話時間", "seconds"), ("redial", "再発信率", "rate"),
    ("yen_per_call", "1本あたりの費用", "yen"), ("technical_error", "音声の問題・こちらの不具合の率", "rate"),
    ("ear_disagreement", "聞き取りが食い違った率", "rate"), ("forbidden_speech", "言ってはいけないことを言った件数", "count"),
    ("complaint", "苦情の率", "rate"),
)
# 会話AIの声の率の種類（core の attempts.VOICE_KINDS のすべて。試験が突き合わせる）
AI_VOICE_WORDS: dict[str, str] = {"silent": "会話AIが黙った率", "cut_off": "会話AIの声が途切れた率", "words_missing": "会話AIの言葉が抜けた率",
                                  "too_long": "会話AIが同じ文を2回言った率", "wrong_language": "会話AIが英語を話した率"}


def metric_text(kind: str, v: Any) -> str:
    """指標の値の書き方（1か所）。「まだ数えていない」「まだ無い」を 0 と書かない（原則10）。"""
    if kind == "rate":
        if v is None:
            return "まだ数えていません"
        if not v.get("total"):
            return "まだありません（0件）"
        return f"{v['value'] * 100:.1f}%（{v['hits']}／{v['total']}）"
    if v is None:
        return "まだありません"
    if kind == "seconds":
        m, sec = divmod(int(round(float(v))), 60)
        return f"{m}分{sec:02d}秒" if m else f"{sec}秒"
    if kind == "yen":
        return f"{float(v):,.1f}円"
    return f"{v:,}"


# 電話の型の、画面の名前（型の正本は core の business_settings.CALL_TYPES。そのすべてを書く＝試験が突き合わせる。計画書 9-9・
# ✅ 2026-11-20 管理者「伺い78：…ア」＝業務ごとに選ぶ電話の型）
CALL_TYPE_WORDS: dict[str, str] = {
    "confirm": "確認の電話（お客様のほうからご連絡をくださった方への電話）",
    "acquire": "顧客獲得型（こちらからおかけする営業の電話）",
}


# 結果の書き出しの名前（1か所。依頼者へ渡す書き出し＝控えを残す と、確かめ用の書き出し＝依頼者へは渡さない・控えなし を、名前で
# 必ず見分けられるようにする。2026-11-20 の見直し：どちらも「CSV で書き出す」で並び、確かめ用を依頼者へ送りかねなかった）
EXPORT_WORDS: dict[str, str] = {
    "handover_xlsx": "渡すファイルを書き出す（Excel）", "handover_csv": "渡すファイルを書き出す（CSV）",
    "check": "確かめ用に書き出す（依頼者へは渡さない・控えは残さない）",
    "check_xlsx": "確かめ用（Excel）", "check_csv": "確かめ用（CSV）",
    # 記録のすべて（基本の形＝設計書 7-3-5b。✅ 2026-11-21 管理者 伺い3「イ」＝契約者の手元の控え）
    "records": "記録のすべてを書き出す（手元の控え・Excel）",
}


# お客様リストから移る行き先の名前（業務の画面の表とお客様リストの画面で同じ言葉）
LIST_GO_WORDS: dict[str, str] = {"rows": "お客様リスト", "results": "依頼者へ渡す結果"}   # results は、その画面の題とパンくずの名前と同じ


# 色の役割（1か所。札・数の色は、ここから引く。2026-11-20 の見直し：色の名前を画面ごとに書いていて、止めた知らせが青・急ぎが赤・
# 休みが赤と橙・元気が緑と、ずれていた）。設計書 7-1「ふだんの画面は白と灰。止まっている・承認待ちだけに色が付く」：
# stopped（止まっている・できなかった・基準の外）＝赤／waiting（決めること・待っていること・休み）＝橙／done（済んだ・通った）＝緑／
# info（区別のための情報）＝青／normal（ふだんどおり・何もない・システムが進めているだけ）＝色なし
TONES: dict[str, str] = {"stopped": "stop", "waiting": "wait", "done": "ok", "info": "info", "normal": ""}
# 知らせの種類の役割（無い種類は、急ぎなら waiting・ほかは info。急ぎは色ではなく文の「【緊急】」で示す）
NOTICE_ROLES: dict[str, str] = {"stopped": "stopped", "pc_down": "stopped", "line_unusable": "waiting", "resumed": "done"}


def tone(role: str) -> str:
    """色の役割の CSS の名前（TONES の1か所）。"""
    return TONES[role]


def notice_tone(kind: str, urgent: bool) -> str:
    return TONES[NOTICE_ROLES.get(kind) or ("waiting" if urgent else "info")]


def line_tone(stage: Any) -> str:
    """回線の段の色：元気＝色なし・それ以外（確かめ中・休み）＝待っていること（段の正本は core の health_rules.LineStage）。"""
    return TONES["normal" if getattr(stage, "name", "") == "HEALTHY" else "waiting"]


# 全体の画面の名前と行き先（左の道しるべ・ページの見出し・ホームの数の札・上の帯の印が、同じ名前をここから引く1か所。並びは道しるべの順。
# 2026-11-20 の見直し：左は「見張り」、ホームの札は「気をつけること」と、同じ画面が2つの名前で出ていた）
PAGES: dict[str, tuple[str, str]] = {
    "home": ("ホーム", "/t"), "inbox": ("承認待ち", "/t/inbox"), "stops": ("止まっているもの", "/t/stops"),
    "watch": ("気をつけること", "/t/watch"), "notices": ("知らせ", "/t/notices"), "costs": ("費用と報酬", "/t/costs"),
    "lines": ("回線と PC", "/t/lines"), "dnc": ("共通の発信禁止", "/t/dnc"), "settings": ("契約者の設定", "/t/settings"),
}
ADMIN_PAGES: dict[str, tuple[str, str]] = {"admin_tenants": ("契約者を選ぶ", "/admin/tenants"), "system_settings": ("システムの初めの値", "/admin/settings")}


# 依頼者・業務の中の画面（左の道しるべの「依頼者『○○』」「業務『○○』」のまとまりと、ページの上の行き先が、同じ名前をここから引く。
# 鍵は、依頼者・業務の住所のあとに付く部分）
CLIENT_NAV: dict[str, str] = {"": "概要（全業務）", "/dnc": "発信禁止", "/destinations": "結果の送り先", "/settings": "この依頼者の設定"}
BUSINESS_NAV: dict[str, str] = {"": "業務の概要", "/metrics": "電話の様子と指標", "/samples": "誤判定率と抜き取り", "/reward": "報酬の決まり", "/settings": "この業務の設定"}


def page(key: str) -> str:
    """画面の名前（PAGES・ADMIN_PAGES の1か所）。"""
    return (PAGES.get(key) or ADMIN_PAGES[key])[0]


# 設定のまとまりの名前（設定の名前の頭。データベースの設定の定義のすべての頭を書く。試験が突き合わせる）。
# **並びは、設定の画面に出す順**＝契約者が業務を組み立てる仕事の順（業務 → いつかけるか → かける前の確かめ → お客様の扱い → 通話・会話 →
# かけ直し → 決めたことの扱い → 結果 → 承認と品質 → 回線と PC → 費用・控え・知らせなどの裏方）。中の名前の順（英字の順）では並べない
SETTING_GROUPS: dict[str, str] = {
    "business": "業務", "calling": "かける時間帯", "gate": "かける前の確かめ（回数・間隔）", "callee": "お客様の扱い",
    "call": "通話の進め方", "conversation": "会話", "callback": "かけ直し", "retry": "かけ直しの回数", "decision": "取り消せない決定",
    "dnc": "発信禁止", "output": "結果の出力", "approval": "承認", "quality": "品質の段（検証モード）", "verification": "通話のあとの確かめ",
    "judge": "判定", "measure": "測り", "recording": "録音", "line": "回線", "dial": "発信", "pc": "PC", "stop": "止めると再開",
    "budget": "費用と予算", "ai": "AI", "backup": "控え", "notify": "知らせ", "login": "ログイン",
}


# ふだん契約者が決める・変える設定のまとまり（設定の画面で開いて並べる。ほかは「くわしい設定」にたたむ。名前は SETTING_GROUPS の鍵）。
# 選び方：業務の決め事（いつ・何回かけるか・お客様との約束・結果の渡し方・検証モード）と、最初に必ず決める知らせの受け先。
# 「かける前の確かめ」は予約の期限・平均の通話時間を測る日数など裏側の値が多いので、たたむ側（探せば出る）
EVERYDAY_GROUPS = frozenset({"business", "calling", "callee", "callback", "retry", "dnc", "output", "approval", "notify"})


def grouped_settings(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """設定の行を、まとまりごとに SETTING_GROUPS の並び（仕事の順）で返す1か所（設定の画面の目次と本文が使う）。まとまりの中は来た順。"""
    order = {g: i for i, g in enumerate(SETTING_GROUPS)}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["group"], []).append(r)
    return sorted(out.items(), key=lambda gi: order.get(gi[0], len(order)))


# 数の設定の単位（setting_units.json。値の形が integer・limit・integer_list の設定のすべてを書く。試験が突き合わせる。見出しの言葉と同じく
# 言葉の表のファイルに置く＝設定を読む所ではない）。値の横と入れる欄の横に出す。
# 「秒」の並び（integer_list）は、1つずつ人の時間（例：5分）に直して見せる。秒の形（seconds）の設定は、形そのものが秒なので書かない
SETTING_UNITS: dict[str, str] = json.loads((Path(__file__).with_name("setting_units.json")).read_text(encoding="utf-8"))
# はい／いいえの設定の、はいのとき・いいえのときの言葉（setting_yes_no.json。はい／いいえの設定のすべてを書く。試験が突き合わせる）。
# 見出しが問いの形でない設定（例「決められない決定の扱い」）でも、答えが「はい／いいえ」で浮かないように、値そのものを言葉で見せる
SETTING_YES_NO: dict[str, list[str]] = json.loads((Path(__file__).with_name("setting_yes_no.json")).read_text(encoding="utf-8"))


def yes_no(key: str, value: bool) -> str:
    """はい／いいえの設定の値の言葉（1か所。表の無い設定は「はい」「いいえ」）。"""
    w = SETTING_YES_NO.get(key) or ["はい", "いいえ"]
    return w[0] if value else w[1]


UNIT_VALUE_TYPES = ("integer", "limit", "integer_list")   # 単位を書く値の形（試験が突き合わせる）


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
    # 電話口が機械だった（設計書 7-3-4 の4番目。種別の正本は core の call_report.MACHINE_PARTIES＝試験が突き合わせる。CL-2 4-453 の 20）
    "machine:voicemail": "留守番電話", "machine:ivr": "音声ガイダンス", "machine:fax": "ファクス",
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
def question_choices(q: Any) -> str:
    """質問の選択肢の見せ方（1か所）：選択肢で答える型（core の business_settings.CHOICE_TYPES）だけ選択肢を並べる。値を伺う質問は、core が中の印
    （VALUE_CHOICE＝「答えた」）を選択肢に持つので、それを出さずに「（値を伺います）」（2026-11-20：電話の台本の画面に「答えた」と出ていた）。"""
    from salescall.core import business_settings
    if q.type in business_settings.CHOICE_TYPES:
        return "・".join(q.choices)
    return "（値を伺います）"


TARGET_PERSON_WORDS: dict[str, str] = {"role": "役職の方", "role_and_name": "役職（部署）とお名前の方", "name": "お名前の方",
                                       "anyone": "その店・会社の方ならどなたでも"}


# 値の特別な名前（値の形 gap_or_never・day_or_gap の文字の値。値を見せる web/setting_forms と、説明の文の置き換えが同じ表を読む）
SETTING_VALUE_WORDS: dict[str, str] = {"never": "かけない", "next_day": "その日はかけない"}


def choice_word(words_of: Any, value: str) -> str:
    """選ぶ設定の値の言葉（言葉の正本は設定の定義の choice_words＝CL-2 4-450。無ければ NO_WORD＝試験が見つける）。"""
    return (words_of or {}).get(value, NO_WORD)


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
    "more_calls": "お約束・お詫びの電話で話せなかったお客様がいます（さらにかけるか選んでください）",
    "month_ahead_approval": "1か月以上先のお電話を確かめてください", "month_ahead_unanswered": "【緊急】1か月以上先のお電話の答えがありません",
    "solution_held": "依頼者の解決策を、まだ入れていません", "solution_earlier": "入っていない前の解決策があります",
    "solution_unconfirmed": "解決策を入れるか選んでください", "declared_after_settle": "「もうかけないで」が電話のあとに届きました",
    "recording_needed": "録音が要る部分があります", "recording_list_needed": "録音の一覧がまだありません",
    "version_not_derived": "業務設定の新しい版を作れませんでした", "setting_missing": "設定がされていません",
    "pc_down": "PC からの合図が途切れました", "line_unusable": "スマホと電話のアプリのつながりが切れています", "line_undecided": "回線のせいか番号のせいか決められません",
    "task_resting": "こちらの不具合で休ませたお客様がいます", "line_none_to_switch": "発信番号を替える回線がありません",
    "pc_line_conflict": "同じ回線を2台の PC がつないでいます", "decision_approval": "取り消せない決定の確かめを待っています",
    "decision_assumed": "決められなかった決定を、設定どおり決まったものとしました",
    "emergency_route": "緊急の道で再開しました", "ai_newer_version": "使っている AI に新しい版が出ました",
    "ai_version_missing": "使っている AI の版が見つかりません", "ai_version_unchecked": "AI の新しい版を確かめられませんでした",
}


def notice_kind(name: str) -> str:
    """知らせの種類の、画面に出す短い名前（NOTICE_KIND_WORDS の1か所。種類の正本は core の notify.KINDS）。"""
    return NOTICE_KIND_WORDS.get(name, NO_WORD) if name in notify.KINDS else NO_WORD


# 行ごとの「かけない」の印の、画面の名前（段の正本は core の exclusions.LEVEL_NAMES。そのすべてを書く＝試験が突き合わせる）。core の名前
# 「かけない」「強制かけない」は、依頼者のファイルの列の言葉でもあるので変えない。画面では意味が分かる名前にする（2026-11-20 の見直し：
# 発信禁止の「かけない」＝1本もかけない、と印の「かけない」＝お約束・お詫びの電話はかける、が同じ言葉で、取り違えうる）
EXCLUSION_WORDS: dict[str, str] = {"call": "かける", "exclude": "新しくはかけない", "force": "1本もかけない"}
EXCLUSION_HELP = "「新しくはかけない」でも、お約束・お詫び・途中で切れたあとの電話だけはかけます。「1本もかけない」は、どの電話もかけません。"


def exclusion_level(name: str | None) -> str:
    """行ごとの「かけない」の印の画面の名前（EXCLUSION_WORDS の1か所）。印が無ければ「かける」。"""
    return EXCLUSION_WORDS.get(name or "call", NO_WORD)


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

