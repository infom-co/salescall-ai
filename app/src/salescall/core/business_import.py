"""業務設定の取込（引き継ぎの (10)。Intent.md 6-1 の方式B・6-4・6-5・6-8）。持ち主 CL-2。

入口ごとに、業務設定の1つの形（core/business_settings.py の `check` が受ける形）にするだけ。入口は、Excel のファイル
（標準のひな形。シートと列の名前は、依頼者の形に合わせて対応付けられる＝`preview`）と、依頼者の画面から抜き出したもの・画面の
入力（同じ形を作って `preview_shaped` に渡す。2026-10-16 ユーザー：「依頼主の画面から抽出する場合もある」）。**どの入口も
`preview_shaped`（解決策を残す → AI に聞く → 確かめ `review`）と `register_shaped`（解決策を残す → 確かめ → 登録 `register_reviewed`）の
同じ道を通る**。確かめそのものは business_settings の1か所。ファイルを読むのは core/import_files.py。

流れ（Intent.md 6-5）：`preview`（何も書かない）⇒ 契約者が問題を直す（検証モードでは、AI の読みの案も確かめる）⇒ `register`
（プレビューと同じ中身のときだけ、新しい版を作る。全部か、何も無いか）。

- 標準のひな形の、シート・列・言葉の名前の正本は、この部品の `SHEETS`・`LABELS`（画面のひな形の書き出しも、これを読む）。
- **AI（読みの案・単位を決める処理）に聞くのは、プレビューの1回だけ**（`ask_ai`）。登録は、プレビューの答え（`Preview.ai`）を
  そのまま使う（もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になり、登録できなくなる）。
- 名前と読み（Intent.md 6-8。CL-1 の連絡帳 4-15・4-16）：台本（業務設定の文）から名前を洗い出して読みを決めるのは CL-1 の決め方
  （`readings.NameFinder`＝`names.decide`。管理者も契約者も確かめない。2026-10-16 ユーザー）。入口が名前と読みを持ってくれば、
  それを使う。決め方の順は core/readings.py の1か所。**読みが決まらない名前でも止めない**（考えられる読みとまず言ってみる読みを
  持って登録し、会話の中で確かめる）。**契約者が確かめるのは、検証モードの業務の、AI が決めた読みだけ**（「検証中はAIが認識した
  ものを私が確認します」。確かめるかを決めるのは business_settings の1か所）。**名乗りの担当者名の読みは必ず入口で入れる**
  （案C。AI では決めない）。
- 数の質問の単位（CL-1 の連絡帳 4-14）：入口が単位を持ってこなければ、単位を決める処理（`UnitDecider`。決め方は CL-1 の1か所）
  の答えを入れる。管理者も契約者も確かめない。決まらなければ空（補わない）。
- 質問の「聞くこと」（CL-1 の連絡帳 4-15・G-43）：入口が持ってこなければ、聞くことを決める処理（`TopicDecider`。CL-1 の
  `topics.decide`）の答えを入れる。管理者も契約者も確かめない。決まらなければ空（聞くことを言わずに聞き返す）。
- 「わからない・答えたくない」のシートは、質問ID「全部」の行で、必須の質問すべての扱いを1度に書ける（質問ごとの行があれば、
  そちらが勝つ）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from salescall.core import business_rules, business_settings, readings
from salescall.core.access import Tx
from salescall.core.import_files import FileRejected, Sheet, read

# ── 標準のひな形（正本はここ）。役目 → (シートの名前, 列の役目 → 見出し, 必ず要るシートか)
SHEETS: dict[str, tuple[str, dict[str, str], bool]] = {
    "basic": ("基本", {"item": "項目", "value": "値"}, True),
    "questions": ("質問", {"id": "質問ID", "text": "質問の文", "ask_about": "聞くこと", "type": "型", "required": "必須",
                          "choices": "選択肢", "decline": "断りの答え", "intro": "入る一言", "ask_after": "伺う条件の質問ID",
                          "ask_answers": "伺う条件の答え", "ask_hedged": "言い切っていないときだけ伺う", "approval": "ご承認を伺う",
                          "final_goal": "最終目的", "do_words": "こちらがすることの言葉",
                          "tell_words": "その答えのあとに伝える一言", "ends_on": "ここで終える答え",
                          "option_notes": "選択肢ごとの良い点・気をつける点",
                          "change_by": "変更の受付の期限（日時・数・金額の質問だけ。例：前日）",
                          "definite_value": "確定した値で登録する（日時・数・金額の質問だけ。例：予約の日付）"}, True),
    "branches": ("分岐", {"id": "質問ID", "answer": "回答条件", "next": "次の質問ID"}, False),
    "handling": ("わからない・答えたくない", {"id": "質問ID", "state": "答え", "reason": "理由", "kind": "扱い",
                                          "conclusion": "結論の名前"}, True),
    "retry": ("かけ直し", {"key": "結果", "action": "かけ直すか", "max_times": "回数", "interval": "間隔"}, True),
    "forbidden": ("言ってはいけないこと", {"key": "名前", "say_not": "言ってはいけないこと", "correct": "正しい案内"}, False),
    "names": ("名前", {"text": "表記", "reading": "読み"}, False),
    "answers": ("想定問答", {"name": "問いの名前", "text": "答え", "off": "標準を使わない"}, False),   # お客様に聞かれたら答えてよいこと（CL-1 の 4-26）
}
# 無くてもよい列（役目.列の役目）：見出しが無ければ空として読む（前のひな形にこの列が無いため）。対応付けで見出しを指定したのに無ければ問題。
# 質問に入る一言（依頼者のトークスクリプト。CL-1 の連絡帳 4-65 の (b)。最初の質問には書かない＝業務設定の確かめ）。
# 前の質問のこの答えのときだけ伺う（伺う条件の質問ID・伺う条件の答え。答えは「／」か改行で区切る）と、説明した内容のご承認を伺う質問か
# （はい／いいえの質問だけ）（どちらも依頼者のトークスクリプト。CL-1 の連絡帳 4-66 の (b)。空ならいつも伺う・ご承認の質問ではない）
# 最終目的の申し出か（「はい」の質問が最終目的。断られて目的が果たせなくなったら、会話の側がその場で締め、判定は「用件を断られた」とする。
# 断りの答えの列は、その質問の申し出を断る答え＝最終目的でない質問では、断られても用件を断られたにしない。CL-1 の連絡帳 4-69 の 4-182 の (e)）
# 伺う条件の答えが言い切っていないときだけ伺うか（「はい」なら、前の質問の答えが言い切っていないときだけ伺う。依頼者のトークスクリプト。
# CL-1 の連絡帳 4-71 の (b)。空なら、伺う答えならいつも伺う）
# こちらがすることの言葉（選択肢ごと。「選択肢：言葉」を「／」か改行で区切る。例「はい：予定通りの方向で進めさせていただきます」＝言葉は文の終わり
# まで（終わりは「させていただきます」か「承ります」＝`business_settings.do_words_ok`）。書いた選択肢は取込の AI の言葉より先に使う。CL-1 の連絡帳
# 4-72 の (d) と、その答えの (2)）
# その答えのあとに伝える一言（選択肢ごと。同じ「選択肢：一言」の形。例「はい：○○駐車場をご利用いただけますのでどうぞご利用ください。」。
# 取込の AI は作らない。CL-1 の連絡帳 4-72 の (i)）
# ここで終える答え（「この答えなら、ここで終える」。答えを「／」か改行で区切る。この列のある質問は必須の質問。CL-1 の連絡帳 4-74。✅ 2026-11-04 管理者「OK」）
# 選択肢ごとの良い点・気をつける点（選択肢ごと。同じ「選択肢：文」の形。例「カード：カードですとカード会社のポイントがつきます。」。依頼者のトークスクリプト・
# 資料から入る＝管理者・契約者は書かない。会話AIが、迷っているお客様に選ぶ助けを伝える材料。CL-1 の連絡帳 4-110。✅ 2026-11-07 ユーザー「(1) ア」）
# 変更の受付の期限（値を伺う質問＝日時・数・金額だけ。「前日」「3日前」のように期限だけ。お客様の答えの値が確定でないとき、ひとまずその値にして
# 「変更があった場合には（期限）までにご連絡いただけましたらご対応可能です。」を添えて伺う。ほかの質問に書いたら業務設定の確かめが止める。依頼者の
# トークスクリプトから入る。CL-1 の連絡帳 4-115 の (1)。✅ 2026-11-08 日本時間3時48分03秒 ユーザー）
# 確定した値で登録する（「はい」の質問は、お客様の答えの値が確定でない「15日あたりですかね。」のとき、値の芯でひとまずとする＝「はい、それではひとまず15日と
# させていただきますね？」・値は「15日」。空・「いいえ」なら、お客様の言い方の値「15日あたり」。値を伺う質問＝日時・数・金額だけ。変更の受付の期限が空でも
# 書ける。ほかの質問に「はい」と書いたら業務設定の確かめが止める＝変更の受付の期限と同じ1か所。依頼者のトークスクリプトから入る。CL-1 の連絡帳 4-116 の (1)。
# ✅ 2026-11-08 日本時間5時33分05秒・6時04分57秒 ユーザー）
# 想定問答の「標準を使わない」（「はい」の行は、その名前のシステムの標準の想定問答を、この業務では使わない＝答えは書かない。答えを書いた行は、
# 同じ名前の標準の想定問答を、この業務の答えで上書きする＝その業務の録音。設計書 3-8-3 の作る順の (4)・0174。✅ 2026-11-02 管理者 伺い2
# 「上書きが必要な業務は、トークスクリプト設定画面か、エクセルなどのシートで上書きし、その分の録音を実施する。」）
OPTIONAL_COLUMNS = frozenset({"questions.intro", "questions.ask_after", "questions.ask_answers", "questions.ask_hedged",
                              "questions.approval", "questions.final_goal", "questions.do_words", "questions.tell_words",
                              "questions.ends_on", "questions.option_notes", "questions.change_by",
                              "questions.definite_value", "answers.off"})
BASIC_ITEMS = {"業務の目的": "purpose", "お客様の区分": "target_class", "自社名": "company", "自社名の読み": "company_reading", "担当者名": "person",
               "担当者名の読み": "person_reading", "用件の一文": "purpose_line", "目的のお客様": "target_kind",
               "目的のお客様の役職": "target_role", "忙しいと言われたときの一言": "busy_line",
               "留守番電話に残す伝言": "voicemail_message", "「AIですか」への答え": "ai_answer", "声の型": "voice",
               # 相手の申し出への応じ方と業務の情報（CL-1 の連絡帳 4-28。書かなければ既定＝ユーザー決定）
               "代わりの方への応じ方": "substitute_policy", "伝言への応じ方": "message_policy", "伝言の文": "message",
               "在籍していないときの応じ方": "departed_policy", "業務の情報": "info",
               # 本題の始まり（書かなければ既定「最初の質問を流したあと」。CL-1 の連絡帳 4-114・4-115 の (4)）
               "本題の始まり": "main_talk_policy",
               "担当から改めて連絡すると申し出てよいか": "follow_up",
               # 依頼者がすること（登録・記録・手配など。書かなければ会話の側の既定「承っておいて」＝phrases.NOTED_DOES。CL-1 の連絡帳 4-65 の (c)・
               # 4-116 の (2)）
               "依頼者がすること": "client_does",
               # 最終目的の果たし方（最終目的の質問が2つ以上の業務だけ書く。業務設定の goal_mode）
               "最終目的の果たし方": "goal_mode",
               # 最短の日（質問の文・入る一言に {最短の日} を書いた業務だけ書く。業務設定の earliest。CL-1 の連絡帳 4-71 の (c)）：
               # 「N営業日以降」の N・休みの曜日（書かなければ土と日）・祝日は休みか（書かなければ休み）・休業日（日にちを「／」か改行で区切る）
               "最短の日（営業日の数）": "earliest_days", "休みの曜日": "closed_weekdays", "祝日は休みか": "closed_on_holidays",
               "休業日": "closed_dates",
               # ご案内できる方法（業務で1つ。名前を「／」か改行で区切る。例「メール／LINE／ZOOM」。業務設定の offer_ways。CL-1 の連絡帳 4-91）
               "ご案内できる方法": "offer_ways",
               # 営業に引き継ぐ架電の、詳しい者からの改めてのお話の方法（名前を「／」か改行で区切る。例「訪問／オンラインでのご説明／お電話」。業務設定の
               # handoff_ways。CL-1 の連絡帳 4-122）
               "詳しい者からのお話の方法": "handoff_ways",
               # 営業に引き継がない架電の、締める前の申し出（業務設定の close_offer。CL-1 の連絡帳 4-124・4-125）：申し出の文・聞くこと・方法（「／」か改行で
               # 区切る）・受けていただいたときにこちらがすること（「…させていただきます」。書かなければ取込の AI が申し出の文から決め、決まらなければ
               # 締める前の申し出は入れない＝`review`）
               "締める前の申し出の文": "close_offer_text", "締める前の申し出の聞くこと": "close_offer_topic",
               "締める前の申し出の方法": "close_offer_ways", "締める前の申し出でこちらがすること": "close_offer_do"}
LABELS: dict[str, dict[str, str]] = {
    "type": {"日付": "date", "時刻": "time", "日時": "datetime", "数": "number", "金額": "amount", "名前": "name",
             "電話番号": "phone", "メール": "email", "住所": "address", "はい／いいえ": "yes_no", "選択肢": "choice",
             "自由な意見": "free_text"},
    "target_class": {"法人": "corporate", "個人": "individual"},
    "target_kind": {"役職のみ": "role", "役職と名前": "role_and_name", "名前のみ": "name", "誰でも": "anyone"},
    "state": {"わからない": "dont_know", "答えたくない": "refused"},
    "reason": {"担当ではない": "not_in_charge", "まだ決まっていない": "undecided", "いまは分からない": "unknown_now",
               "理由を言わなかった": "unspecified", "決まりで言えない": "policy", "答えたくない": "unwilling"},
    "kind": {"分かるお客様を聞く": "ask_who", "分かる時を聞く": "ask_when", "日を改める": "retry", "結論として終える": "conclude"},
    "retry_key": {"会話に至らなかった": "open", "一部だけ取れた": "incomplete", "不在": "not_reached_absent",
                  "かけ直しの時刻が決まっていない": "callback_unspecified_time"},
    "action": {"かける": "call", "終える": "end"},
    # 相手の申し出への応じ方（business_settings.POLICIES の値。空なら書かない＝既定）
    "substitute_policy": {"判断いただけるお立場の方なら話す": "decision_maker", "目的のお客様だけと話す": "target_only",
                          "どなたとでも話す": "anyone"},
    "message_policy": {"お礼を言って断り戻る時刻を聞く": "decline", "伝言を残す": "leave"},
    "departed_policy": {"後任の方をお呼びする": "successor", "お礼を言って終える": "end"},
    "main_talk_policy": {"最初の質問を流したあと": "asked", "業務の答えを1ついただいたあと": "answered"},
    "follow_up": {"はい": True, "いいえ": False},
    "approval": {"はい": True, "○": True, "いいえ": False, "": False},
    "final_goal": {"はい": True, "○": True, "いいえ": False, "": False},
    "ask_hedged": {"はい": True, "○": True, "いいえ": False, "": False},
    "definite_value": {"はい": True, "○": True, "いいえ": False, "": False},
    "answer_off": {"はい": True, "○": True, "いいえ": False, "": False},
    "closed_on_holidays": {"はい": True, "いいえ": False},
    "weekday": {f"{w}{x}": k for w, k in zip("月火水木金土日", ("mon", "tue", "wed", "thu", "fri", "sat", "sun")) for x in ("", "曜日")},
    "goal_mode": {"どれか1つで果たせる": "any", "全部が要る": "all"},
    "required": {"はい": True, "○": True, "いいえ": False, "": False},
}
# かけ直しのシートで、システムが決める結果（業務では変えない＝business_rules.SYSTEM_POLICIES）。前のひな形にはこの行があったので、
# 問題にせず読み飛ばし、知らせに出す（⛔ 2026-10-22 の見直し13：前は「使えない結果」として登録を止めていた）
SYSTEM_RETRY_ROWS = {"取り次いでもらえなかった": "日を改めて1回"}
ALL_QUESTIONS = "全部"            # 「わからない・答えたくない」の質問ID：必須の質問すべて
OTHER_ANSWER = "その他"           # 分岐の回答条件：受け皿（その質問に「その他」という選択肢があれば、その選択肢＝下の ANY_LABEL で受け皿）
ANY_LABEL = "＊"                  # 分岐の回答条件：いつでも受け皿
END_LABEL = "終わり"              # 分岐の次の質問ID：終わり
_SPLIT = re.compile(r"[／\n]")    # 選択肢・断りの答えの区切り（全角の「／」か、欄の中の改行）
_INTERVAL = re.compile(r"^\s*(\d+)\s*(時間おき|日後)\s*$")


@dataclass
class Preview:
    raw: dict[str, Any] | None                    # 業務設定の形（読めなかったら None）
    problems: list[str]                           # 直すこと（シート・行つき）。空なら登録できる
    names_to_confirm: list[dict[str, str]]        # 契約者が読みを確かめる名前（検証モードの、AI が決めた読み。表記・読み・出どころ）
    digest: str = ""                              # 登録のときに同じ中身かを確かめる指紋
    ai: dict[str, Any] = field(default_factory=dict)   # AI の答え（登録にそのまま渡す。聞き直さない）
    notes: list[str] = field(default_factory=list)     # 知らせ（登録は止めない。例：無くてもよいシートが無かった）
    standard_id: UUID | None = None               # 重ねた標準の版（プレビューのときのいまの標準。登録はこの版で作る＝指紋に入る。0174）


def _sheets(file_name: str, data: bytes, mapping: dict[str, Any]
            ) -> tuple[dict[str, tuple[Sheet, dict[str, int]]], list[str], list[str]]:
    """役目ごとのシートと列の位置。mapping＝{"sheets": {役目: シートの名前}, "columns": {"役目.列の役目": 見出し}}（無い所は標準）。"""
    out: dict[str, tuple[Sheet, dict[str, int]]] = {}
    problems: list[str] = []
    notes: list[str] = []
    for role, (default_name, columns, required) in SHEETS.items():
        mapped = role in mapping.get("sheets", {})
        name = mapping.get("sheets", {}).get(role, default_name)
        try:
            sheet = read(file_name, data, sheet_name=name)
        except FileRejected as e:
            if str(e) == "指定のシートがありません" and not required and not mapped:
                # 無くてもよいシート（分岐・言ってはいけないこと・名前）：無いものとして読み、プレビューに知らせる（黙って
                # 読み飛ばさない。シートの名前の打ち違いに気づけるように。2026-10-16 の見直し I5）。対応付けで名前を
                # 指定したシートが無ければ、問題（下）
                notes.append(f"シート「{name}」がありません（無いものとして読みました。シートの名前を確かめてください）")
                continue
            problems.append(f"シート「{name}」：{e}")
            continue
        cols: dict[str, int] = {}
        for col, default_title in columns.items():
            title = mapping.get("columns", {}).get(f"{role}.{col}", default_title)
            hits = [i for i, h in enumerate(sheet.header) if h is not None and h.strip() == title]
            if not hits and f"{role}.{col}" in OPTIONAL_COLUMNS and f"{role}.{col}" not in mapping.get("columns", {}):
                continue   # 無くてもよい列（空として読む）
            if len(hits) != 1:
                problems.append(f"シート「{name}」：見出し「{title}」の列が{'ありません' if not hits else '2つ以上あります'}")
                continue
            cols[col] = hits[0]
        out[role] = (sheet, cols)
    return out, problems, notes


def _rows(sheets: dict[str, tuple[Sheet, dict[str, int]]], role: str, problems: list[str]) -> list[tuple[int, dict[str, str | None]]]:
    """そのシートの、空でない行（行番号と、列の役目 → 文字）。値を信じられない欄（エラーの値など）は、問題に足す。"""
    if role not in sheets:
        return []
    sheet, cols = sheets[role]
    out = []
    for r in sheet.rows:
        if r.is_empty:
            continue
        if not r.width_ok:
            problems.append(f"シート「{sheet.sheet_name}」の {r.row_no} 行目：列の数が見出しと合いません")
            continue
        values = {}
        for col, i in cols.items():
            c = r.cell(i)
            if c.problem:
                problems.append(f"シート「{sheet.sheet_name}」の {r.row_no} 行目「{sheet.header[i]}」：{c.problem}")
            values[col] = c.text.strip() if c.text else None
        out.append((r.row_no, values))
    return out


def _responses(basic: dict[str, str | None], problems: list[str], notes: list[str]) -> dict[str, Any]:
    """シート「基本」の、相手の申し出への応じ方・本題の始まり・伝言の文・業務の情報（1行に1つ）・改めて連絡すると申し出てよいか。書いていない
    項目は入れない（業務設定の確かめが既定にする＝ユーザー決定の既定）。業務の情報が無ければ、知らせる（登録は止めない）：会話の側は、
    業務の情報・想定問答の答えなど業務設定に無い、こちらの会社・業務の事実を AI が言うと止める（CL-1 の連絡帳 4-30＝中身の判定）ので、
    無いと、所在地・営業時間などを聞かれても「担当の者に申し伝えます」で受けるだけになる。"""
    out: dict[str, Any] = {}
    for key in (*business_settings.POLICIES, "follow_up"):
        if basic.get(key):
            v = _label(key, basic.get(key), f"シート「基本」の{_basic_name(key)}", problems)
            if v is not None:
                out[key] = v
    if basic.get("message"):
        out["message"] = basic["message"]
    if basic.get("info"):
        out["info"] = [x.strip() for x in basic["info"].split("\n") if x.strip()]
    if not out.get("info"):
        notes.append("シート「基本」の業務の情報がありません：所在地・営業時間など、お客様に聞かれたら答えてよいことを1行に1つ"
                     "書いてください。ここに無いこちらの会社・業務のことは、AI は答えません（「担当の者に申し伝えます」で受けます）")
    return out


NO_CLOSED_WEEKDAYS = "なし"      # 休みの曜日：休みの曜日が無い（毎日が営業日。祝日・休業日は別に書く）
_DAY_IN_SHEET = re.compile(r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:T00:00:00)?\s*$")   # 日付のセルは「…T00:00:00」で来る（import_files）


def _earliest(basic: dict[str, str | None], problems: list[str]) -> dict[str, Any]:
    """シート「基本」の最短の日（営業日の数・休みの曜日・祝日は休みか・休業日）を、業務設定の最短の日（earliest）にする。営業日の数が
    無ければ入れない（ほかの項目だけ書いてあれば問題）。休みの曜日を書かなければ土と日、祝日は休みかを書かなければ休み（営業日の
    ふつうの数え方。業務設定の `business_settings.Earliest` の既定と同じ）。形の確かめは業務設定の確かめ（1か所）。"""
    keys = ("closed_weekdays", "closed_on_holidays", "closed_dates")
    if not basic.get("earliest_days"):
        if any(basic.get(k) for k in keys):
            problems.append("シート「基本」：休みの曜日・祝日は休みか・休業日は、最短の日（営業日の数）を書いた業務だけ書きます")
        return {}
    n = basic["earliest_days"].strip()
    days = int(n) if n.isdecimal() else n
    given = (basic.get("closed_weekdays") or "").strip()
    weekdays = [] if given == NO_CLOSED_WEEKDAYS else \
        [_label("weekday", w, "シート「基本」の休みの曜日", problems) for w in _split(given) or []] if given \
        else list(business_settings.Earliest.closed_weekdays)
    holidays = _label("closed_on_holidays", basic["closed_on_holidays"], "シート「基本」の祝日は休みか", problems) \
        if basic.get("closed_on_holidays") else True
    dates = []
    for d in _split(basic.get("closed_dates")) or []:
        m = _DAY_IN_SHEET.match(d)
        dates.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else d)   # 日にちでなければ確かめが問題に出す
    return {"earliest": {"business_days": days, "closed_weekdays": [w for w in weekdays if w is not None],
                         "closed_on_holidays": holidays if holidays is not None else True, "closed_dates": dates}}


_WORD_PAIR = re.compile(r"^\s*([^：:]+?)\s*[：:]\s*(.+?)\s*$")


def _choice_words(value: str | None, where: str, problems: list[str]) -> dict[str, str] | None:
    """「質問」のシートの選択肢ごとの言葉の列（こちらがすることの言葉・その答えのあとに伝える一言。「選択肢：言葉」を「／」か改行で区切る）を、
    選択肢 → 言葉の表にする（空なら None）。形の違う区切りは問題。選択肢に無い名前・言葉の形は、業務設定の確かめが問題に出す（1か所）。"""
    out: dict[str, str] = {}
    for part in _split(value) or []:
        m = _WORD_PAIR.match(part)
        if m is None:
            problems.append(f"{where}「{part}」は「選択肢：言葉」の形です")
            continue
        out[m.group(1)] = m.group(2)
    return out or None


def _basic_name(key: str) -> str:
    return next(k for k, v in BASIC_ITEMS.items() if v == key)


def _policy_words(p: business_rules.Policy) -> str:
    """方針を、ひな形の言葉で書く（知らせの文）。"""
    if p.action == "end":
        return "終える"
    unit, n = p.interval
    return f"かける・{p.max_times}回・{n}{'時間おき' if unit == 'hours' else '日後'}"


def _label(kind: str, value: str | None, where: str, problems: list[str]) -> Any:
    table = LABELS[kind]
    v = "" if value is None else value
    if v not in table:
        problems.append(f"{where}：「{value or ''}」は使えません（{[k for k in table if k]} のどれか）")
        return None
    return table[v]


def _split(value: str | None) -> list[str] | None:
    return None if not value else [x.strip() for x in _SPLIT.split(value) if x.strip()]


def _to_raw(sheets: dict[str, tuple[Sheet, dict[str, int]]], problems: list[str], notes: list[str] | None = None
            ) -> tuple[dict[str, Any], list[str], dict[str, list[str]]]:
    """シートを業務設定の形にする。3つ目は、形の並びの何番目が、どのシートの何行目か（問題の位置の書き換えに使う＝`_placed`）。
    notes には、登録は止めない知らせを足す（システムが決めるので読み飛ばした行など）。"""
    notes = [] if notes is None else notes
    basic: dict[str, str | None] = {}
    for row_no, v in _rows(sheets, "basic", problems):
        item = BASIC_ITEMS.get(v.get("item") or "")
        if item is None:
            problems.append(f"シート「基本」の {row_no} 行目：項目「{v.get('item')}」は使えません（{list(BASIC_ITEMS)}）")
        elif item in basic:
            problems.append(f"シート「基本」：項目「{v.get('item')}」が2つあります")
        else:
            basic[item] = v.get("value")
    tkind = _label("target_kind", basic.get("target_kind"), "シート「基本」の目的のお客様", problems)
    tclass = _label("target_class", basic.get("target_class"), "シート「基本」のお客様の区分", problems)

    questions = []
    where_q: list[str] = []
    for row_no, v in _rows(sheets, "questions", problems):
        where = f"シート「質問」の {row_no} 行目"
        where_q.append(where)
        qtype = _label("type", v.get("type"), f"{where}の型", problems)
        required = _label("required", v.get("required"), f"{where}の必須", problems)
        hedged = _label("ask_hedged", v.get("ask_hedged"), f"{where}の言い切っていないときだけ伺う", problems)
        ask_if = None if not v.get("ask_after") and not v.get("ask_answers") and not hedged else \
            {"after": v.get("ask_after"), "answers": _split(v.get("ask_answers")), "hedged": hedged is True}   # 欠けていれば、業務設定の確かめが問題に出す
        questions.append({"id": v.get("id"), "text": v.get("text"), "ask_about": v.get("ask_about"), "type": qtype,
                          "required": required, "choices": _split(v.get("choices")) if qtype == "choice" else None,
                          "next": None, "unit": None, "meanings": None, "replies": None, "asks_fact": None,
                          "do_words": _choice_words(v.get("do_words"), f"{where}のこちらがすることの言葉", problems), "doubt_words": None,
                          "tell_words": _choice_words(v.get("tell_words"), f"{where}のその答えのあとに伝える一言", problems) or {},
                          "option_notes": _choice_words(v.get("option_notes"), f"{where}の選択肢ごとの良い点・気をつける点", problems) or {},
                          "ends_on": _split(v.get("ends_on")) or [], "unclear_none": None, "asks_way": None, "maybe": None,
                          "change_by": v.get("change_by") or "",
                          "definite_value": _label("definite_value", v.get("definite_value"), f"{where}の確定した値で登録する", problems),
                          "away": None, "intro": v.get("intro"), "ask_if": ask_if,
                          "approval": _label("approval", v.get("approval"), f"{where}のご承認を伺う", problems),
                          "final_goal": _label("final_goal", v.get("final_goal"), f"{where}の最終目的", problems),
                          "declines": _split(v.get("decline")) or []})
    by_id = {q["id"]: q for q in questions}
    for row_no, v in _rows(sheets, "branches", problems):
        q = by_id.get(v.get("id"))
        if q is None:
            problems.append(f"シート「分岐」の {row_no} 行目：質問ID {v.get('id')} は「質問」のシートにありません")
            continue
        # 「その他」は受け皿。ただし、その質問に「その他」という選択肢があれば、その選択肢（受け皿は「＊」で書く）。
        # 前は、いつも受け皿にしていたので、選択肢の「その他」へ分けられず、「わからない」「答えたくない」まで「その他」の
        # 行き先へ行った（2026-10-16 の見直し I6）
        literal = OTHER_ANSWER in (q.get("choices") or [])
        answer = business_settings.ANY if v.get("answer") == ANY_LABEL or (v.get("answer") == OTHER_ANSWER and not literal) \
            else v.get("answer")
        nxt = business_settings.END if v.get("next") == END_LABEL else v.get("next")
        q["next"] = q["next"] or {}
        if answer in q["next"]:
            problems.append(f"シート「分岐」の {row_no} 行目：質問 {v.get('id')} の回答条件「{v.get('answer')}」が2つあります")
        q["next"][answer] = nxt

    handling: dict[str, dict[str, dict[str, Any]]] = {}
    seen_handling: dict[tuple[Any, str, str], int] = {}
    for row_no, v in _rows(sheets, "handling", problems):
        where = f"シート「わからない・答えたくない」の {row_no} 行目"
        state = _label("state", v.get("state"), f"{where}の答え", problems)
        reason = _label("reason", v.get("reason"), f"{where}の理由", problems)
        kind = _label("kind", v.get("kind"), f"{where}の扱い", problems)
        if None in (state, reason, kind):
            continue
        h = {"kind": kind} if kind != "conclude" else {"kind": kind, "conclusion": v.get("conclusion")}
        row_key = (v.get("id"), state, reason)
        if row_key in seen_handling:   # 同じ質問・答え・理由の行が2つ：黙って後ろの行にしない（2026-10-16 の見直し I7）
            problems.append(f"{where}：質問 {v.get('id')} の「{v.get('state')}」「{v.get('reason')}」の行が2つあります"
                            f"（{seen_handling[row_key]} 行目と）")
            continue
        seen_handling[row_key] = row_no
        # 「全部」は、業務の決まりの「必須の質問すべて」の鍵のまま渡す（どの質問が必須かは、分岐のシートの置き換えのあとに業務設定の確かめが
        # 決める＝business_rules.check が広げる。質問ごとの行は、答え・理由ごとにそれより勝つ）
        qid = business_rules.ALL_REQUIRED if v.get("id") == ALL_QUESTIONS else v.get("id")
        handling.setdefault(qid, {"dont_know": {}, "refused": {}})[state][reason] = h

    retry: dict[str, Any] = {}
    seen_retry: dict[str, int] = {}
    named_retry: set[str] = set()                  # 行のある結果（形の誤りがあっても）
    for row_no, v in _rows(sheets, "retry", problems):
        where = f"シート「かけ直し」の {row_no} 行目"
        if v.get("key") in SYSTEM_RETRY_ROWS:
            notes.append(f"{where}：結果「{v.get('key')}」は、システムが決めるので読み飛ばしました"
                         f"（{SYSTEM_RETRY_ROWS[v.get('key')]}）")
            continue
        key = _label("retry_key", v.get("key"), f"{where}の結果", problems)
        action = _label("action", v.get("action"), f"{where}のかけ直すか", problems)
        if key is not None:
            named_retry.add(key)
        if key is None or action is None:
            continue
        if key in seen_retry:
            problems.append(f"{where}：結果「{v.get('key')}」の行が2つあります（{seen_retry[key]} 行目と）")
            continue
        seen_retry[key] = row_no
        if action == "end":
            retry[key] = {"action": "end"}
            continue
        m = _INTERVAL.match(v.get("interval") or "")
        times = v.get("max_times")
        if m is None or not (times or "").isdecimal() or int(times) < 1 or int(m.group(1)) < 1:
            problems.append(f"{where}：かけるときは、回数（1以上の数）と間隔（「2時間おき」「1日後」の形。1以上）が要ります")
            continue
        retry[key] = {"action": "call", "max_times": int(times),
                      "interval": {"hours" if m.group(2) == "時間おき" else "days": int(m.group(1))}}
    for key, p in business_rules.DEFAULT_POLICIES.items():
        if key not in named_retry:         # 行が無いときだけ（行があって形が違えば、上で問題に出している）
            retry[key] = business_rules.policy_raw(p)
            name = next(k for k, v in LABELS["retry_key"].items() if v == key)
            notes.append(f"シート「かけ直し」：結果「{name}」の行が無いので、初めの値（{_policy_words(p)}）で入れました")

    forbidden: dict[str, Any] = {}
    seen_forbidden: dict[str, int] = {}
    for row_no, v in _rows(sheets, "forbidden", problems):
        key = v.get("key") or f"行{row_no}"
        if key in seen_forbidden:
            problems.append(f"シート「言ってはいけないこと」の {row_no} 行目：名前「{key}」が2つあります（{seen_forbidden[key]} 行目と）")
            continue
        seen_forbidden[key] = row_no
        forbidden[key] = {"say_not": v.get("say_not"), "correct": v.get("correct")}

    answers, where_a, answers_off = [], [], []
    for row_no, v in _rows(sheets, "answers", problems):   # 形と重なりの確かめは業務設定の1か所（business_settings._answers・_with_standard）
        where = f"シート「想定問答」の {row_no} 行目"
        if _label("answer_off", v.get("off"), f"{where}の標準を使わない", problems):
            if v.get("text"):
                problems.append(f"{where}：答えを書いた問いは「標準を使わない」にできません（答えを書けば、標準の答えをこの業務の答えで"
                                "上書きします。使わないなら答えを消します）")
            answers_off.append(v.get("name"))
            continue
        answers.append({"name": v.get("name"), "text": v.get("text")})
        where_a.append(where)

    names, where_n = [], []
    pairs = [(basic.get("company"), basic.get("company_reading"), "caller", "シート「基本」の自社名"),
             (basic.get("person"), basic.get("person_reading"), "caller", "シート「基本」の担当者名")]
    pairs += [(v.get("text"), v.get("reading"), "script", f"シート「名前」の {row_no} 行目")
              for row_no, v in _rows(sheets, "names", problems)]
    for text, reading, source, where in pairs:
        if text:                                   # 読みは無くてもよい（AI の読みの案を使う。`review`）
            names.append({"text": text, "reading": reading, "source": source, "reading_by": "given" if reading else None,
                          "candidates": [], "likely": None})
            where_n.append(where)
    raw = {
        "purpose": basic.get("purpose"), "target_class": tclass,
        "caller": {"company": {"text": basic.get("company"), "reading": basic.get("company_reading")},
                   "person": {"text": basic.get("person"), "reading": basic.get("person_reading")},
                   "purpose_line": basic.get("purpose_line")},
        "target_person": {"kind": tkind, "role": basic.get("target_role")},
        "busy_line": basic.get("busy_line"), "voicemail_message": basic.get("voicemail_message"),
        "ai_answer": basic.get("ai_answer"), "answers": answers, "voice": basic.get("voice"),
        **({"answers_off": answers_off} if answers_off else {}),
        **_responses(basic, problems, notes),
        **({"client_does": basic["client_does"]} if basic.get("client_does") else {}),
        **({"goal_mode": _label("goal_mode", basic["goal_mode"], "シート「基本」の最終目的の果たし方", problems)}
           if basic.get("goal_mode") else {}),
        **_earliest(basic, problems),
        **({"offer_ways": _split(basic["offer_ways"])} if basic.get("offer_ways") else {}),
        **({"handoff_ways": _split(basic["handoff_ways"])} if basic.get("handoff_ways") else {}),
        **_close_offer(basic, problems),
        "questions": questions, "names": names,
        "rules": {"handling": handling, "retry": retry, "forbidden": forbidden},
    }
    return raw, problems, {"名前": where_n, "質問": where_q, "想定問答": where_a}


def _close_offer(basic: dict[str, str | None], problems: list[str]) -> dict[str, Any]:
    """シート「基本」の締める前の申し出の4つの項目を、業務設定の close_offer の形にする（文が無ければ入れない。ほかの項目だけ書いた行は問題に出す）。
    こちらがすることを書いていなければ None（取込の AI が決める＝`review`）。"""
    rest = ("close_offer_topic", "close_offer_ways", "close_offer_do")
    if not basic.get("close_offer_text"):
        if any(basic.get(k) for k in rest):
            problems.append("シート「基本」：締める前の申し出の文が無いので、締める前の申し出の聞くこと・方法・こちらがすることは使えません"
                            "（申し出の文を書きます）")
        return {}
    return {"close_offer": {"text": basic["close_offer_text"], "topic": basic.get("close_offer_topic") or "",
                            "ways": _split(basic.get("close_offer_ways")) or [], "do": basic.get("close_offer_do") or None}}


_POSITION = re.compile(r"(名前|質問|想定問答) (\d+) 番目")


def _placed(problems: list[str], where: dict[str, list[str]]) -> list[str]:
    """業務設定の確かめ（1か所）の問題の「名前 n 番目」「質問 n 番目」を、ファイルのシートと行に書き換える
    （前は、名前の一覧の頭に自社名・担当者名が入るので「名前」シートの1行目が「名前 3 番目」と出た。2026-10-16 の見直し I10）。"""
    def place(m: re.Match) -> str:
        rows = where.get(m.group(1), [])
        i = int(m.group(2)) - 1
        return rows[i] if 0 <= i < len(rows) else m.group(0)   # AI が足した名前など、ファイルに無いものはそのまま
    return [_POSITION.sub(place, p) for p in problems]


def _field_types(tx: Tx) -> frozenset[str]:
    return frozenset(r["type_name"] for r in tx.all("SELECT type_name FROM salescall.field_types"))


NameFinder = readings.NameFinder                    # 台本から名前と読みを出す処理（正本は core/readings.py。作るのは CL-1）

UnitDecider = Callable[[list[tuple[str, str]]], dict[str, str]]
"""数の質問の単位を決める処理（(業務の目的, 質問の文) の並び → 文ごとの単位。決まらない文は入れないか空）。決め方は CL-1 の1か所
（CL-1 の連絡帳 4-14。質問の文の数える言葉か、文字のモデル2つが合うときだけ。CL-1 の import_ai.decide_units）。管理者も契約者も
確かめない。業務の目的と一緒に聞く（CL-1 の連絡帳 4-69 の 4-184 の (a)：G-41 はこの形で測った。⛔ 2026-11-02 まで：文だけを渡していた）。"""


def _is_name(n: Any) -> bool:
    """名前の一覧の1つとして読める形か（表記が文字）。読めない形は、そのまま残して、確かめ（check）が問題に出す。"""
    return isinstance(n, dict) and isinstance(n.get("text"), str) and bool(n["text"].strip())


def _names(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in (raw.get("names") if isinstance(raw.get("names"), list) else []) if _is_name(n)]


TopicDecider = Callable[[list[str]], dict[str, str]]
"""質問の「聞くこと」（聞き返しの言い方に使う）を決める処理（質問の文の並び → 文ごとの聞くこと。決まらない文は入れないか空）。
決め方は CL-1 の1か所（`topics.decide`：質問の文の形の決まりか、文字のモデル2つが合うときだけ。G-43。CL-1 の連絡帳 4-15）。
管理者も契約者も確かめない。"""


def _topic_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and not q.get("ask_about") and isinstance(q.get("text"), str)
            and q["text"].strip()]


MeaningDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[dict[str, str]]]
"""はい／いいえ・選択肢の質問の「意味の言葉」を決める処理（(質問の文, 選択肢の並び) の並び → 同じ並びで {選択肢: 意味の言葉}。
決まらない選択肢は入れない）。決め方は CL-1 の1か所（`meanings.by_model` を2つのモデルで1回ずつ → `meanings.decide`。CL-1 の
連絡帳 4-17）。管理者も契約者も確かめない。"""

ReplyDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[dict[str, str]]]
"""はい／いいえ・選択肢の質問の「曖昧な言葉の意味」を決める処理（(質問の文, 選択肢の並び) の並び → 同じ並びで {言葉: 選択肢}。
決まらない言葉は入れない）。決め方は CL-1 の1か所（`meanings.by_model_replies` を2つのモデルで1回ずつ → `meanings.decide_replies`。
CL-1 の連絡帳 4-17）。管理者も契約者も確かめない。"""


DoWordsDecider = Callable[[list[tuple[str, tuple[str, ...], dict[str, str]]]], list[dict[str, str]]]
"""事実を伺う質問ではない（希望・申し出の）選択肢の質問の「こちらがすること（お客様がされること）」の言葉を決める処理（(質問の文, 選択肢の
並び, その質問の意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで {選択肢: 言葉}。決まらない選択肢は入れない）。決め方は CL-1 の1か所
（`meanings.by_model(…, kind="do")` を2つのモデルで1回ずつ → `meanings.decide(…, kind="do", meanings=意味の言葉)`。言葉の向きが意味の言葉の
向きとそろうときだけ決まる＝G-72。CL-1 の連絡帳 4-65 の (a) と 11-02 2時台の追記。CL-1 が全部の言葉を見る）。意味の言葉は、入口が
持ってきたものか、同じ取込で先に決めたもの（`ask_ai`）。管理者も契約者も確かめない。"""


AwayDecider = Callable[[list[tuple[str, tuple[str, ...], dict[str, str]]]], list[tuple[str, ...]]]
"""事実を伺う質問ではない（希望・申し出の）選択肢の質問の、業務の目的から遠ざかる向きの選択肢を決める処理（(質問の文, 選択肢の並び, その質問の
意味の言葉 {選択肢: 意味の言葉}) の並び → 同じ並びで、遠ざかる向きの選択肢の並び）。決め方は CL-1 の1か所（`import_ai.decide_away`＝
`meanings.by_model(…, "away")` を2つのモデルで1回ずつ → `meanings.decide_away`。どちらか1つのモデルでも遠ざかる向きと読むか、読めない選択肢は
入れる＝安全側。意味の言葉を添えて読ませる＝打ち消しの問いの「はい」を取り違えない。CL-1 の連絡帳 4-72 の (a)）。管理者も契約者も確かめない。
⛔ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は全くダメです。…いい加減な提案をするな！！」＝前の形（目的につなげて伺う言葉も AI が作る
decide_keep_words）は取り下げ。目的につなげて伺う一言は、業務設定の言い切っていないときだけ伺う質問で書く。"""


UnclearDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[str]]
"""ほかに分からない点が無いかを伺う質問（「ご不明な点はございますでしょうか？」）の、「無い」にあたる選択肢を決める処理（(質問の文, 選択肢の並び) の並び →
同じ並びで選択肢の名前。伺う質問でない・決まらないなら空）。決め方は CL-1 の1か所（`import_ai.decide_unclear`＝`unclear.by_model` を2つのモデルで1回ずつ →
`unclear.decide`。2つがそろったときだけ決まる＝伺うべき答えを伺わずに決める誤りを避ける側）。業務の選ぶ質問（はい／いいえ・選択肢）だけを渡す。
管理者も契約者も確かめない。✅ 2026-11-04 日本時間20時27分 管理者「(1)…OK」（CL-1 の連絡帳 4-82）"""


FactDecider = Callable[[list[str]], dict[str, bool]]
"""質問ごとに「お客様に事実を伺う質問か」を決める処理（質問の文の並び → 文ごとの真偽。決まらない文は入れない）。決め方は CL-1 の
1か所（`facts.by_model` を2つのモデルで1回ずつ → `facts.decide`。G-71。CL-1 の連絡帳 2-1 の版 0.6）。管理者も契約者も確かめない。"""


MaybeDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[tuple[str, ...]]]
"""どちらの場合もありうると言われたとき（「どっちでもいいですよ」）に確かめる (答え, 言葉) を決める処理（(質問の文, 選択肢の並び) の並び → 同じ並びで
(選択肢, 言葉) か空）。決め方は CL-1 の1か所（`import_ai.decide_maybes`＝2つのモデルがそろったときだけ）。業務の選ぶ質問（はい／いいえ・選択肢。事実を伺う
質問も）だけを渡す。管理者も契約者も確かめない。⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー（CL-1 の連絡帳 4-123）"""


PurposeDecider = Callable[[list[str]], dict[str, str]]
"""用件の一文の中の用件の名詞を決める処理（用件の一文の並び → 文ごとの名詞。決まらなければ空）。決め方は CL-1 の1か所（`import_ai.decide_purposes`＝2つの
モデルがそろって、文の中の言葉のときだけ）。管理者も契約者も確かめない。✅ 2026-11-09 日本時間9時48分09秒の (4)（CL-1 の連絡帳 4-128）"""


WayWordsDecider = Callable[[list[tuple[str, tuple[str, ...]]]], list[dict[str, str]]]
"""方法を申し出の中身に入れる言い方を決める処理（(こちらがすることの言葉, 方法の並び) の並び → 同じ並びで {方法: その方法を入れた言葉}。決まらない方法は入れない）。
決め方は CL-1 の1か所（`import_ai.decide_way_words`＝2つのモデルの言葉がどちらも確かめに通るときだけ）。管理者も契約者も確かめない。2026-11-12 まとめ役の (1)
（CL-1 の連絡帳 4-132）"""


WayPairs = Callable[[dict[str, Any]], list[tuple[str, tuple[str, ...]]]]
"""方法を申し出の中身に入れる言い方を決めるために、業務設定から (こちらがすることの言葉, 方法の並び) の並びを作る処理（確かめに通った業務設定の中身＝版に置く形 →
並び）。並べ方は会話の側の1か所（CL-1 の `flowctl.way_word_pairs`。申し出の質問 N4・N6 の言葉・業務の申し出の質問の受ける答えの言葉・ほかのご希望の言葉と、
それぞれの方法）で、こちらは並べ方を持たない（2か所にしない。CL-1 の連絡帳 4-132「flowctl.way_word_pairs の1か所で…そのまま渡してください」）。決める処理と
一緒に、つなぐ所が渡す（会話の側の業務設定に直して呼ぶ）。"""


OfferQuestion = Callable[[str], tuple[str, tuple[str, ...], dict[str, str]]]
"""締める前の申し出のこちらがすることの言葉を決める問いを作る処理（申し出の文 → (申し出の文, 選択肢の並び, 意味の言葉)。CL-1 の `flowctl.close_offer_do_question`）。
申し出のシステムの質問（N6）の意味の言葉は会話の側の決まり（N6 で使う意味の言葉）で、取込で別に決めない（同じ質問の意味の言葉を2か所に置かない＝CL-1 の連絡帳
4-138 の (3)）。決める処理と一緒に、つなぐ所が渡す。"""


def _decided_maybes(raw: dict[str, Any], decide: MaybeDecider | None) -> dict[str, list[str]]:
    """選ぶ質問ごとに、どちらの場合もありうるときに確かめる (答え, 言葉) を、決める処理に1回だけ聞く（答え＝質問の文と選択肢 → [選択肢, 言葉] か空）。
    答えが無い・形が違う・選択肢に無い名前・言葉が空・決める処理が渡されないときは空（確かめない＝今までどおり「どちらでも」は聞き直す。CL-1 の
    maybes.decide と同じ側）。"""
    pairs = list(dict.fromkeys((q["text"], c) for q, c in _choice_questions(raw, "maybe")))
    if not pairs or decide is None:
        return {}
    got = decide(pairs)
    if not isinstance(got, list) or len(got) != len(pairs):
        return {}
    return {_pair_key(text, choices): ([d[0], d[1].strip()] if isinstance(d, (tuple, list)) and len(d) == 2 and d[0] in choices
                                       and isinstance(d[1], str) and d[1].strip() else [])
            for (text, choices), d in zip(pairs, got)}


def _decided_purposes(raw: dict[str, Any], decide: PurposeDecider | None) -> dict[str, str]:
    """用件の一文の中の用件の名詞を、決める処理に1回だけ聞く（答え＝用件の一文 → 名詞。文の中の言葉だけ受ける＝業務設定の確かめと同じ決まり）。
    入口が書いた名詞があれば聞かない。"""
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    line = caller.get("purpose_line")
    if raw.get("purpose_noun") is not None or not isinstance(line, str) or not line.strip() or decide is None:
        return {}
    got = decide([line])
    noun = got.get(line) if isinstance(got, dict) else None
    return {line: noun.strip()} if isinstance(noun, str) and noun.strip() and noun.strip() in line else {}


def _offer_text(raw: dict[str, Any]) -> str | None:
    """こちらがすることの言葉を決める、締める前の申し出の文（入口がこちらがすることを書いていないときだけ。無ければ None）。"""
    offer = raw.get("close_offer")
    if not isinstance(offer, dict) or offer.get("do") or not isinstance(offer.get("text"), str) or not offer["text"].strip():
        return None
    return offer["text"]


def _offer_question(raw: dict[str, Any], offer_question: OfferQuestion | None) -> dict[str, Any] | None:
    """締める前の申し出を、こちらがすることの言葉を決めるための、はい／いいえの質問の形にしたもの（`_offer_text` のあるときだけ。無ければ None）。
    業務の選ぶ質問のこちらがすることの言葉（do_words）と同じ1か所（意味の言葉 → こちらがすることの言葉）で、申し出の文の「はい」の言葉を決める（CL-1 の連絡帳
    4-125）。意味の言葉は会話の側が N6 で使う言葉（offer_question＝CL-1 の flowctl.close_offer_do_question の答え。CL-1 の連絡帳 4-138 の (3)）を質問に持たせる
    ので、意味の言葉を決める処理には渡らない（`_choice_questions` は意味の言葉のある質問を聞かない）。offer_question が渡されない・答えの形が違う（はい／いいえで
    ない・意味の言葉が使えない）ときは None（決めない＝締める前の申し出は入れない＝`review` が知らせる）。⛔ 2026-11-13 まで：申し出の文を業務の選ぶ質問と
    同じく意味の言葉を決める処理に渡していた（同じ質問の意味の言葉が、会話の側と取込の2か所になる）。"""
    text = _offer_text(raw)
    if text is None or offer_question is None:
        return None
    got = offer_question(text)
    if not (isinstance(got, tuple) and len(got) == 3 and got[0] == text and tuple(got[1]) == business_settings.YES_NO_CHOICES):
        return None
    meanings = business_settings.valid_answer_words(got[2], business_settings.YES_NO_CHOICES, replies=False)
    if set(meanings) != set(business_settings.YES_NO_CHOICES):
        return None
    return {"id": None, "text": text, "type": "yes_no", "choices": None, "asks_fact": False, "meanings": meanings, "do_words": None}


def _with_offer(raw: dict[str, Any], offer_question: OfferQuestion | None) -> dict[str, Any]:
    """こちらがすることの言葉を聞く並びに、締める前の申し出（`_offer_question`）を足した中身（受けたものは変えない）。"""
    q = _offer_question(raw, offer_question)
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return raw if q is None else {**raw, "questions": [*qs, q]}


def _decided_way_words(p: Preview, decide: WayWordsDecider | None, pairs_of: WayPairs | None) -> dict[str, dict[str, str]]:
    """方法を申し出の中身に入れる言い方を、決める処理に1回だけ聞く（答え＝{こちらがすることの言葉: {方法: 言葉}}＝業務設定 way_words の形）。聞くのは、確かめに
    通った中身（p。問題が無い）に、入口の言い方が無いときだけ（並べるのは会話の側の1か所＝pairs_of。こちらは並べ方を持たない）。決める処理か並べる処理が
    渡されない・並びが空・答えの形が違うときは空（入れない＝会話の側は「（方法）で」の今の形）。その並びの方法でない名前・空の言葉は落とす。"""
    if decide is None or pairs_of is None or p.raw is None or p.problems or p.raw.get("way_words") is not None:
        return {}
    pairs = [(d, tuple(w)) for d, w in pairs_of(p.raw) if isinstance(d, str) and d.strip() and w]
    if not pairs:
        return {}
    got = decide(pairs)
    if not isinstance(got, list) or len(got) != len(pairs):
        return {}
    out: dict[str, dict[str, str]] = {}
    for (do, ways), words in zip(pairs, got):
        for way, text in (words.items() if isinstance(words, dict) else ()):
            if way in ways and isinstance(text, str) and text.strip():
                out.setdefault(do, {})[way] = text.strip()
    return out


def _undecided(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """その真偽の欄（事実を伺う質問か asks_fact・ご案内の方法を伺う質問か asks_way）が、まだ入口から来ていない質問（文のあるもの）。"""
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and q.get(key) is None and isinstance(q.get("text"), str) and q["text"].strip()]


def _fact_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """事実を伺う質問か（asks_fact）が、まだ入口から来ていない質問。"""
    return _undecided(raw, "asks_fact")


def _decided_facts(texts: list[str], decide: FactDecider | None) -> dict[str, bool]:
    """文ごとに「事実を伺う質問か」を決める処理に、1回だけ聞く。偽と答えた文だけが偽（形の違う答えは真＝決まらないときと同じ側。
    答えの無い文は入れない＝`review` が真で入れる）。決め方の正本は CL-1（`facts.decide`）で、ここは答えの読み方の1か所。"""
    if not texts or decide is None:
        return {}
    got = decide(texts)
    return {t: v is not False for t, v in got.items() if t in texts} if isinstance(got, dict) else {}


WaysDecider = Callable[[list[str]], dict[str, bool]]
"""質問ごとに「ご案内の方法（お電話・ご訪問・説明会・オンラインの面談 など）でご案内してよいかを伺う質問か」を決める処理（質問の文の並び →
文ごとの真偽。形は FactDecider と同じ）。決め方は CL-1 の1か所（`import_ai.decide_ways`＝2つのモデルがそろって真のときだけ真。CL-1 の連絡帳
4-91）。管理者も契約者も確かめない。"""


def _decided_ways(texts: list[str], decide: WaysDecider | None) -> dict[str, bool]:
    """文ごとに「ご案内の方法を伺う質問か」を決める処理に、1回だけ聞く。真と答えた文だけが真（形の違う答え・答えの無い文は偽＝`review` が偽で
    入れる。そろわなければ偽＝CL-1 の決め方と同じ側：申し出る方法を言わないだけで、伺うことは変わらない）。答えの読み方の1か所。"""
    if not texts or decide is None:
        return {}
    got = decide(texts)
    return {t: v is True for t, v in got.items() if t in texts} if isinstance(got, dict) else {}


def _asks_fact(q: dict[str, Any], facts: dict[str, bool]) -> bool:
    """その質問が事実を伺う質問か（入口に無ければ AI の答え、決まらなければ真＝`review` が入れる値と同じ1か所）。"""
    return q["asks_fact"] if isinstance(q.get("asks_fact"), bool) else facts.get(q.get("text"), True)


_WISH_KEYS = ("do_words", "doubt_words", "away")   # 事実を伺う質問ではない（希望・申し出の）選択肢の質問にだけ付くもの
_WORD_FORM = {"do_words": "do", "doubt_words": "doubt"}   # 選択肢ごとの言葉の種類（形の決まりは business_settings.WORD_FORMS の1か所）


def _choice_questions(raw: dict[str, Any], key: str,
                      facts: dict[str, bool] | None = None) -> list[tuple[dict[str, Any], tuple[str, ...]]]:
    """意味の言葉（key＝meanings）・曖昧な言葉の意味（replies）・こちらがすることの言葉（do_words）・目的から遠ざかる向きの選択肢（away）が、
    まだ入口から来ていない選択肢の質問と、その選択肢（1か所）。こちらがすることの言葉は、入口（業務設定に書いた言葉）がすべての選択肢に
    書いていなければ聞く（書いた選択肢は入口の言葉を使う＝`review`。CL-1 の連絡帳 4-72 の (d)）。do_words・away は、事実を伺う質問ではない
    質問だけ（facts＝事実を伺う質問かの AI の答え。`_asks_fact`）。"""
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    out = []
    for q in qs:
        if key in _WISH_KEYS and (not isinstance(q, dict) or _asks_fact(q, facts or {})):
            continue
        if not (isinstance(q, dict) and q.get("type") in business_settings.CHOICE_TYPES
                and isinstance(q.get("text"), str) and q["text"].strip()):
            continue
        choices = business_settings.declared_choices(q["type"], q.get("choices"))
        given = q.get(key)
        missing = not given if key not in _WORD_FORM else not (isinstance(given, dict) and set(choices or ()) <= set(given))
        if choices and missing:
            out.append((q, choices))
    return out


def _with_meanings(found: list[tuple[dict[str, Any], tuple[str, ...]]], meanings: dict[str, dict[str, str]]
                   ) -> tuple[list[tuple[str, tuple[str, ...]]], list[tuple[str, tuple[str, ...], dict[str, str]]]]:
    """決める処理に渡す (質問の文, 選択肢, その質問の意味の言葉) の並び（1か所。こちらがすることの言葉と目的から遠ざかる向きは、どちらも意味の
    言葉を添えて聞く＝向きを取り違えない。CL-1 の G-72）。同じ文と選択肢の質問は1回だけ聞く（意味の言葉は、その最初の質問のもの）。"""
    first: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {}
    for q, c in found:
        first.setdefault((q["text"], c), _meanings_of(q, c, meanings))
    pairs = list(first)
    return pairs, [(t, c, first[(t, c)]) for t, c in pairs]


def _decided_away(raw: dict[str, Any], decide: AwayDecider | None, facts: dict[str, bool],
                  meanings: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    """質問ごとに、目的から遠ざかる向きの選択肢を、決める処理に1回だけ聞く（答え＝質問の文と選択肢 → 遠ざかる向きの選択肢の並び）。答えが
    無い・形が違う質問は、すべての選択肢を遠ざかる向きとして入れる（読めない＝安全側＝CL-1 の decide_away と同じ側。「させていただきますね？」を
    使わず、今までの「念のため…」で確かめる）。決める処理が渡されないときも「答えが無い」と同じ（⛔ 2026-11-03 夜の見直し：決める処理が無いときだけ
    遠ざかる向きの無いこと＝反対の側に倒れ、会話の側が `decide_away` を渡す前に main に入ると、管理者が取り下げさせた「それでは…させていただきますね？」の
    言い方に戻る形だった）。選択肢に無い名前は落とす。"""
    pairs, items = _with_meanings(_choice_questions(raw, "away", facts), meanings)
    if not pairs:
        return {}
    got = decide(items) if decide is not None else None
    got = got if isinstance(got, list) and len(got) == len(pairs) else [None] * len(pairs)
    out = {}
    for (text, choices), d in zip(pairs, got):
        readable = isinstance(d, (tuple, list)) and all(isinstance(c, str) for c in d)
        out[_pair_key(text, choices)] = [c for c in choices if c in d] if readable else list(choices)
    return out


def _decided_unclear(raw: dict[str, Any], decide: UnclearDecider | None) -> dict[str, str]:
    """選ぶ質問ごとに、ほかに分からない点は無いにあたる選択肢を、決める処理に1回だけ聞く（答え＝質問の文と選択肢 → 選択肢の名前か空）。答えが
    無い・形が違う・選択肢に無い名前・決める処理が渡されないときは空（伺う質問として扱わない＝重ねて伺うだけで、答えは正しい側。CL-1 の unclear.decide と
    同じ側）。"""
    pairs = list(dict.fromkeys((q["text"], c) for q, c in _choice_questions(raw, "unclear_none")))
    if not pairs or decide is None:
        return {}
    got = decide(pairs)
    if not isinstance(got, list) or len(got) != len(pairs):
        return {}
    return {_pair_key(text, choices): (d if isinstance(d, str) and d in choices else "") for (text, choices), d in zip(pairs, got)}


def _pair_key(text: str, choices: tuple[str, ...]) -> str:
    """AI の答えの鍵（質問の文と選択肢の組。同じ文でも選択肢が違えば別）。"""
    return json.dumps([text, list(choices)], ensure_ascii=False)


def _decided_pairs(raw: dict[str, Any], key: str, decide: MeaningDecider | ReplyDecider | DoWordsDecider | None,
                   facts: dict[str, bool] | None = None, meanings: dict[str, dict[str, str]] | None = None
                   ) -> dict[str, dict[str, str]]:
    """選択肢の質問ごとに決める処理に、1回だけ聞く（使える組だけ返す＝`business_settings.valid_answer_words`。答えが無い・形が
    違うときは空：会話は止まらない＝空なら選択肢の名前で言い直し、曖昧な言葉は通話の中で確かめる、こちらがすることの言葉が無ければ
    今までの確かめの言い方）。こちらがすることの言葉（key＝do_words）は、組ごとにその質問の意味の言葉も渡す（入口の意味の言葉か、
    meanings＝同じ取込で決めた意味の言葉。`_meanings_of` の1か所）。"""
    found = _choice_questions(raw, key, facts)
    pairs = list(dict.fromkeys((q["text"], c) for q, c in found))
    if not pairs or decide is None:
        return {}
    if key in _WORD_FORM:
        got = decide(_with_meanings(found, meanings or {})[1])
    else:
        got = decide(pairs)
    if not isinstance(got, list) or len(got) != len(pairs):
        return {}
    out = {}
    for (text, choices), d in zip(pairs, got):
        good = business_settings.valid_answer_words(d, choices, replies=key == "replies", form=_WORD_FORM.get(key))
        if good:
            out[_pair_key(text, choices)] = good
    return out


def _meanings_of(q: dict[str, Any], choices: tuple[str, ...], decided: dict[str, dict[str, str]]) -> dict[str, str]:
    """その質問の意味の言葉（入口が持ってきたもの。無ければ同じ取込で AI が決めたもの。使える組だけ＝`valid_answer_words`）。
    `review` が入れる意味の言葉と同じ決め方（入口のもの＞AI の答え）。"""
    return business_settings.valid_answer_words(q.get("meanings") or decided.get(_pair_key(q["text"], choices)),
                                                choices, replies=False)


def _decided(texts: list[str], decide: Callable[[list[Any]], dict[str, str]] | None, *,
             asked: Callable[[list[str]], list[Any]] = lambda texts: texts) -> dict[str, str]:
    """文ごとに決める処理に、1回だけ聞く（決まった答えだけ返す。文 → 答え）。asked＝聞く並びの形（既定は文だけ。単位は業務の目的と組で）。"""
    if not texts or decide is None:
        return {}
    return {t: u.strip() for t, u in decide(asked(texts)).items() if t in texts and isinstance(u, str) and u.strip()}


def _decided_units(raw: dict[str, Any], decide: UnitDecider | None) -> dict[str, str]:
    """数の質問の単位を、業務の目的と一緒に1回だけ聞く（文 → 単位）。"""
    purpose = raw.get("purpose") if isinstance(raw.get("purpose"), str) else ""
    return _decided(sorted({q["text"] for q in _unit_questions(raw)}), decide, asked=lambda texts: [(purpose, t) for t in texts])


def _unit_questions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    return [q for q in qs if isinstance(q, dict) and q.get("type") == business_settings.UNIT_TYPE
            and not q.get("unit") and isinstance(q.get("text"), str) and q["text"].strip()]


# 質問の、選択肢ごとの声になる言葉（`script` が台本に入れる）。選択肢ごとの良い点・気をつける点（option_notes）は録音ではなく会話AIが
# 自分の言葉で話す材料だが、業務の情報と同じく名前の読みが要る（会話の側が読みへ置き換えて渡す）ので入れる
SPOKEN_WORDS = ("meanings", "do_words", "tell_words", "option_notes")


def script(raw: dict[str, Any]) -> str:
    """名前を洗い出す台本（業務設定の文をまとめたもの。名乗り・用件・目的・忙しいときの一言・留守番電話の伝言・「AIですか」への答え・
    想定問答の答え（録音で話すので、名前の読みが要る）・業務の情報（AI が自分の言葉で答える材料。会話の側も名前の読みへ置き換えて
    渡す＝CL-1 の連絡帳 4-29：「港区」をAIの声で言うと3本中1本しか聞き取れず、ひらがなの読みで渡すと6本中6本）・質問の文と質問に入る一言
    （どちらも録音で話す。⛔ 2026-11-03 まで入る一言を入れていなかった＝入る一言の中の名前に読みが付かなかった）・入口が持ってきた選択肢ごとの
    言葉（意味の言葉・こちらがすることの言葉・その答えのあとに伝える一言＝`SPOKEN_WORDS`。どれも「それでは（言葉）…」などで声になる。⛔ 2026-11-03 まで
    入れていなかった＝依頼者が書いた「○○駐車場をご利用いただけます…」の名前に読みが付かなかった。取込の AI が決める言葉は質問の文と選択肢から
    作るので、台本の名前の中にある）・入口の名前）。
    最短の日の場所（`business_settings.DAY_SLOT`）は外す（声になるのは会話の側が置き換えた言い方＝名前ではない）。"""
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    parts = [((caller.get("company") or {}).get("text") if isinstance(caller.get("company"), dict) else None),
             caller.get("purpose_line"), raw.get("purpose"), raw.get("busy_line"), raw.get("voicemail_message"),
             raw.get("ai_answer"), raw.get("message")]   # 伝言の文も録音で話す（CL-1 の連絡帳 4-28）
    questions = [q for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else []) if isinstance(q, dict)]
    parts += [q.get(k) for q in questions for k in ("text", "intro")]
    parts += [w for q in questions for k in SPOKEN_WORDS if isinstance(q.get(k), dict) for w in q[k].values()]
    parts += [a.get("text") for a in (raw.get("answers") if isinstance(raw.get("answers"), list) else []) if isinstance(a, dict)]
    # 申し出の方法（録音の「それでは（方法）と（方法）のどちらが…」）・締める前の申し出の文（録音）とこちらがすること（会話AIの伺い直しの声）
    offer = raw.get("close_offer") if isinstance(raw.get("close_offer"), dict) else {}
    parts += [w for k in ("handoff_ways",) if isinstance(raw.get(k), list) for w in raw[k]]
    parts += [offer.get("text"), offer.get("do"), *(offer.get("ways") if isinstance(offer.get("ways"), list) else [])]
    parts += raw.get("info") if isinstance(raw.get("info"), list) else []
    parts += [n["text"] for n in _names(raw) if n.get("source") == "script"]
    return "\n".join(p.replace(business_settings.DAY_SLOT, "") for p in parts if isinstance(p, str) and p.strip())


def ask_ai(raw: dict[str, Any], *, find_names: NameFinder | None = None, decide_units: UnitDecider | None = None,
           decide_topics: TopicDecider | None = None, decide_meanings: MeaningDecider | None = None,
           decide_replies: ReplyDecider | None = None, decide_facts: FactDecider | None = None,
           decide_do_words: DoWordsDecider | None = None, decide_away: AwayDecider | None = None,
           decide_unclear: UnclearDecider | None = None, decide_doubt_words: DoWordsDecider | None = None,
           decide_ways: WaysDecider | None = None, decide_maybes: MaybeDecider | None = None,
           decide_purposes: PurposeDecider | None = None, offer_question: OfferQuestion | None = None) -> dict[str, Any]:
    """業務設定の形について、AI（名前と読み・単位・聞くこと・選択肢の意味の言葉・曖昧な言葉の意味・事実を伺う質問かを決める処理。
    どれも CL-1 の決め方）に1回だけ聞く。答え＝{"names": 表記 → 読み（readings.to_json の形）, "units": 質問の文 → 単位, "topics":
    質問の文 → 聞くこと, "meanings": 質問の文と選択肢 → {選択肢: 意味の言葉}, "replies": 質問の文と選択肢 → {言葉: 選択肢},
    "facts": 質問の文 → 事実を伺う質問か, "do_words": 質問の文と選択肢 → {選択肢: こちらがすることの言葉}（事実を伺う質問ではない質問
    だけ＝事実を伺う質問かが決まってから聞く）, "away": 質問の文と選択肢 → 目的から遠ざかる向きの選択肢の並び（do_words と同じ質問）,
    "unclear": 質問の文と選択肢 → ほかに分からない点は無いにあたる選択肢か空（選ぶ質問すべて）, "ways": 質問の文 → ご案内の方法を伺う質問か,
    "maybes": 質問の文と選択肢 → どちらの場合もありうるときに確かめる [選択肢, 言葉] か空（選ぶ質問すべて）, "purposes": 用件の一文 → 用件の名詞}。
    締める前の申し出（入口がこちらがすることを書いていないとき）は、会話の側が N6 で使う意味の言葉（offer_question）を持つはい／いいえの質問として、
    こちらがすることの言葉の聞く並びに入れる（`_with_offer`。同じ1回の問い合わせで、業務の選ぶ質問と同じ決め方。意味の言葉は聞かない）。方法を申し出の
    中身に入れる言い方は、確かめに通った中身から並べて聞く（`preview_shaped` の `_decided_way_words`）。
    **プレビューで聞いた答えを、登録でもそのまま使う**（もう一度聞くと答えが変わりうる＝契約者が見たものと違う中身になる）。"""
    facts = _decided_facts(sorted({q["text"] for q in _fact_questions(raw)}), decide_facts)
    offered = _with_offer(raw, offer_question)   # 締める前の申し出のこちらがすることの言葉も、業務の選ぶ質問と同じ道で決める（CL-1 の連絡帳 4-125・4-138 の (3)）
    meanings = _decided_pairs(offered, "meanings", decide_meanings)   # こちらがすることの言葉の向きを、意味の言葉の向きとそろえるので先に
    return {"names": readings.to_json(readings.find(script(raw), find_names)),
            "units": _decided_units(raw, decide_units),
            "topics": _decided(sorted({q["text"] for q in _topic_questions(raw)}), decide_topics),
            "meanings": meanings,
            "replies": _decided_pairs(raw, "replies", decide_replies),
            "facts": facts,
            "do_words": _decided_pairs(offered, "do_words", decide_do_words, facts, meanings),
            "doubt_words": _decided_pairs(raw, "doubt_words", decide_doubt_words, facts, meanings),
            "away": _decided_away(raw, decide_away, facts, meanings),
            "unclear": _decided_unclear(raw, decide_unclear),
            "ways": _decided_ways(sorted({q["text"] for q in _undecided(raw, "asks_way")}), decide_ways),
            "maybes": _decided_maybes(raw, decide_maybes),
            "purposes": _decided_purposes(raw, decide_purposes)}


def review(tx: Tx, business_id: UUID, raw: dict[str, Any], *, confirmed_names: Any = (),
           ai: dict[str, Any] | None = None, problems: list[str] | None = None,
           where: dict[str, list[str]] | None = None, standard_id: UUID | str | None = business_settings.LATEST,
           placed_names: Any = ()) -> Preview:
    """業務設定の形を確かめる（どの入口も、ここを通る：取込の入口＝`preview_shaped`・`register_shaped` と、依頼者の解決策を足す道＝
    core/client_solutions＝`review_solution`）。何も書かない。AI には聞かない（AI の答えは `ask_ai` で1回だけ聞いて、ai に渡す）。
    - placed_names＝この中身の元にした版に置いた名前（依頼者の解決策を足すとき）：同じ表記・同じ形の名前は、置いた版のまま変えない
      （AI の読みを読み直さない・検証モードでも確かめ直さない。⛔ 2026-11-07 の第三者の見直し (2) の1：前は台本全体の名前を探し直して、
      版の名前の一覧が増え、前からある名前の AI の読みも変わりえた＝録音を引き継げなかった）。
    - 名前と読み：入口の名前に、AI が洗い出した名前を足す（同じ表記は1つ）。読みの決め方は core/readings.py の1か所（入口の読み
      ＞契約者が確かめた読み＞AI が決めた読み＞決まらない＝候補つき）。検証モードの業務では、AI が決めた読みを契約者が確かめる
      （確かめていなければ問題に出る）。名乗りの自社名・担当者名の読みは、名前の一覧の読みの写し。
    - 数の質問の単位：入口が持ってこなければ、AI の答えの単位を入れる（無ければ空）。
    - 依頼者の解決策で足した想定問答を、取り込み直しても残すのは、取込の入口の共通の1か所 `preview_shaped`・`register_shaped`（AI に名前を
      探させる前に足す）。ここではしない。
    standard_id＝重ねる標準の版（LATEST＝いまの標準＝入口の取込。解決策のように前の版から作るときは、その版の標準の版を渡す＝標準を
    替えるのは business_settings.derive_for_standard の1か所。見直し(B) の2）。"""
    problems = list(problems or [])
    ai = ai or {}
    raw = json.loads(json.dumps(raw, ensure_ascii=False))           # 受けたものを変えない
    verification = business_settings.ai_reading_needs_person(tx, business_id)
    # 重ねる標準（入口の取込は、いまの標準。登録も同じ版で作る＝Preview.standard_id）
    standard = business_settings.current_standard(tx) if standard_id == business_settings.LATEST \
        else business_settings.standard(tx, standard_id)
    confirmed = {t: r for t, r in confirmed_names}
    found = readings.from_json(ai.get("names"))
    # 名前の一覧は、読めない形の行も順番どおり残す（落とすと、確かめが問題に出せず、問題の位置もずれる。2026-10-17 の見直し）。
    # 一覧そのものが並びでなければ、AI の名前を足さず、そのまま確かめに渡す
    listed = isinstance(raw.get("names"), list) or raw.get("names") is None
    names = list(raw.get("names") or []) if listed else []
    have = {unicodedata.normalize("NFKC", n["text"]) for n in names if _is_name(n)}
    placed = {unicodedata.normalize("NFKC", n["text"]): n for n in (placed_names or ()) if _is_name(n)}
    for text in sorted(found) if listed else ():                     # AI が洗い出した名前（入口に無いもの）
        if unicodedata.normalize("NFKC", text) not in have:
            names.append({"text": text, "reading": None, "source": "script"})
            have.add(unicodedata.normalize("NFKC", text))
    by_key = {unicodedata.normalize("NFKC", t): r for t, r in found.items()}
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    # 名乗りの読みは、確かめのたびに名前の一覧の読みを写す（下）。なので、名前の一覧で AI・契約者が決めた読みと同じなら、それは
    # 写し（入口の読みではない）。写しを入口の読みとして読むと、プレビューの形をもう一度確かめたとき、AI の読みが入口の読みに
    # なり、検証モードの確かめ・担当者名の読みは必須の決まりが抜ける（2026-10-17 の見直し：I1 と同じ穴が名乗りから開いていた）
    copied = {n["text"]: readings.normalize(n.get("reading")) for n in names
              if _is_name(n) and n.get("reading_by") in ("ai", "person")}
    caller_given = {c["text"]: c["reading"] for c in (caller.get(k) for k in ("company", "person"))
                    if isinstance(c, dict) and isinstance(c.get("text"), str) and isinstance(c.get("reading"), str)
                    and c["reading"].strip()
                    and not (c["text"] in copied and copied[c["text"]] == readings.normalize(c["reading"]))}
    to_confirm = []
    for n in names:
        if not _is_name(n) or placed.get(unicodedata.normalize("NFKC", n["text"])) == n:   # 置いた版のまま
            continue
        # 入口が持ってきた読みは、読みを決めたものが入口（または無い）の読みだけ。プレビューの形をもう一度確かめたとき、
        # AI の読み・契約者が確かめた読みを入口の読みにしない（2026-10-16 の見直し I1：検証モードの確かめが抜けた）
        by = n.get("reading_by")
        given = n.get("reading") if by in (None, "given") else None
        ai_here = by_key.get(unicodedata.normalize("NFKC", n["text"]), readings.UNDECIDED)
        if by == "ai" and n.get("reading") and ai_here.reading is None:
            ai_here = readings.Reading(readings.normalize(n["reading"]), "ai")
        here_confirmed = dict(confirmed)
        if by == "person" and n.get("reading") and n["text"] not in here_confirmed:
            here_confirmed[n["text"]] = n["reading"]
        # 名乗りの自社名・担当者名に入口の読みがあれば、それが名前の読み（AI の読みで上書きしない＝I3）
        if n["text"] in caller_given:
            if given and readings.normalize(given) != readings.normalize(caller_given[n["text"]]):
                problems.append(f"名前「{n['text']}」の読みが、名乗りと名前の一覧で違います"
                                f"（「{caller_given[n['text']]}」と「{given}」）")
            given = given or caller_given[n["text"]]
        try:
            got = readings.resolve(n["text"], given, confirmed=here_confirmed, ai={n["text"]: ai_here})
        except ValueError as e:
            problems.append(str(e))
            got = readings.UNDECIDED
        n.update(got.as_json())
        if readings.needs_person(got, verification=verification):
            to_confirm.append({"text": n["text"], "reading": got.reading, "source": n.get("source")})
    if listed:
        raw["names"] = [{k: n.get(k) for k in business_settings.NAME_KEYS} if isinstance(n, dict) else n for n in names]
    for q in _unit_questions(raw):
        q["unit"] = ai.get("units", {}).get(q["text"]) or None
    for q in _topic_questions(raw):
        q["ask_about"] = ai.get("topics", {}).get(q["text"]) or None
    for key in ("meanings", "replies"):                              # 選択肢の質問：入口が持ってこなければ、AI の答えを入れる
        for q, choices in _choice_questions(raw, key):
            q[key] = (ai.get(key) or {}).get(_pair_key(q["text"], choices)) or None
    for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else []):
        # 事実を伺う質問か：入口に無ければ AI の答え、決まらなければ（文が空で聞いていない質問も）真（`FactDecider`・CL-1 の決め方と
        # 同じ側。文が空の質問は「文が空」の問題だけが出る＝同じ質問に問題を2つ出さない）
        if isinstance(q, dict) and q.get("asks_fact") is None:
            text = q.get("text") if isinstance(q.get("text"), str) else None
            q["asks_fact"] = (ai.get("facts") or {}).get(text, True)
        # ご案内の方法を伺う質問か：入口に無ければ AI の答え、決まらなければ偽（`_decided_ways`・CL-1 の決め方と同じ側）
        if isinstance(q, dict) and q.get("asks_way") is None:
            q["asks_way"] = (ai.get("ways") or {}).get(q.get("text") if isinstance(q.get("text"), str) else None, False)
    # 目的から遠ざかる向きの選択肢：入口が持ってこなければ、AI の答えを入れる（事実を伺う質問ではない選択肢の質問だけ）
    for q, choices in _choice_questions(raw, "away"):
        q["away"] = (ai.get("away") or {}).get(_pair_key(q["text"], choices)) or None
    # ほかに分からない点は無いにあたる選択肢：入口が持ってこなければ、AI の答えを入れる（決まらなければ空＝伺う。選ぶ質問すべて）
    for q, choices in _choice_questions(raw, "unclear_none"):
        q["unclear_none"] = (ai.get("unclear") or {}).get(_pair_key(q["text"], choices)) or ""
    # どちらの場合もありうるときに確かめる答え：入口が持ってこなければ、AI の答えを入れる（決まらなければ空＝確かめない。選ぶ質問すべて＝CL-1 の連絡帳 4-123）
    for q, choices in _choice_questions(raw, "maybe"):
        q["maybe"] = (ai.get("maybes") or {}).get(_pair_key(q["text"], choices)) or []
    # 用件の名詞：入口に無ければ AI の答え（決まらなければ入れない＝続きの電話でも今の用件の一文。CL-1 の連絡帳 4-128）
    line = caller.get("purpose_line") if isinstance(caller.get("purpose_line"), str) else None
    if raw.get("purpose_noun") is None and line and (ai.get("purposes") or {}).get(line):
        raw["purpose_noun"] = ai["purposes"][line]
    # 方法を申し出の中身に入れる言い方：入口に無ければ AI の答え（決まらなければ入れない＝会話の側は「（方法）で」。CL-1 の連絡帳 4-132）
    if raw.get("way_words") is None and ai.get("way_words"):
        raw["way_words"] = ai["way_words"]
    # 締める前の申し出のこちらがすること：入口に無ければ AI の答え（申し出の文の「はい」の言葉で、申し出の形＝business_settings.offer_do_ok のときだけ）。
    # 決まらなければ締める前の申し出は入れない（今のお礼の締め。CL-1 の連絡帳 4-125：「決まらなければ、締める前の申し出は作らないでください」）
    dropped: list[str] = []
    if (offered := _offer_text(raw)) is not None:
        do = ((ai.get("do_words") or {}).get(_pair_key(offered, business_settings.YES_NO_CHOICES)) or {}).get("はい")
        if isinstance(do, str) and business_settings.offer_do_ok(do):
            raw["close_offer"]["do"] = do
        else:
            del raw["close_offer"]
            dropped.append("締める前の申し出は、受けていただいたときにこちらがすることの言葉（「…させていただきます」）が決まらなかったので入れていません"
                           "（締めは今のお礼の締めです。入れるときは「締める前の申し出でこちらがすること」を書きます）")
    # こちらがすることの言葉：入口（業務設定に書いた言葉）が書いていない選択肢に、AI の答えを入れる（書いた選択肢は入口の言葉が先＝CL-1 の連絡帳
    # 4-72 の (d)。事実を伺う質問ではない選択肢の質問だけ。事実を伺う質問かは上で決まった）
    written: dict[Any, dict[str, str]] = {}   # 依頼者が書いたこちらがすることの言葉（使わない言葉の知らせ＝下）
    for q in (raw.get("questions") if isinstance(raw.get("questions"), list) else []):
        if isinstance(q, dict) and isinstance(q.get("do_words"), dict):
            written[q.get("id")] = dict(q["do_words"])
    for key in _WORD_FORM:   # いまの話の向きを伺う言葉も同じ（入口の言葉が先・事実を伺う質問ではない質問だけ。CL-1 の連絡帳 4-90）
        for q, choices in _choice_questions(raw, key):   # 事実を伺う質問かは、上で質問に入れた（asks_fact）
            given = q[key] if isinstance(q.get(key), dict) else {}
            q[key] = {**(ai.get(key) or {}).get(_pair_key(q["text"], choices), {}), **given} or None
    for key in ("company", "person"):
        c = caller.get(key)
        if isinstance(c, dict) and c.get("text"):
            same = next((n for n in names if _is_name(n) and n.get("text") == c["text"]), None)
            if same is not None:
                c["reading"] = same["reading"]
    notes: list[str] = list(dropped)
    try:
        s = business_settings.check(raw, standard=standard, field_types=_field_types(tx),   # 確かめは1か所
                                    ai_reading_needs_person=verification, voice_types=business_settings.voice_types(tx),
                                    placed_names=placed_names)
        notes = dropped + business_settings.notices(raw) + [
            f"質問 {q.id} の選択肢「{a}」は目的から遠ざかる向きなので、書いてあるこちらがすることの言葉「{written[q.id][a]}」は使いません"
            "（その向きの言葉では言い直しません。目的につなげて伺いたいときは「言い切っていないときに伺う質問」に書きます）"
            for q in s.questions for a in business_settings.unused_do_words(q, written.get(q.id) or {})]
    except business_settings.SettingsInvalid as e:
        problems += [p for p in e.problems if p not in problems]
    if where is not None:                                             # ファイルの入口：問題の位置をシートと行にする
        problems = _placed(problems, where)
    return Preview(raw, problems, to_confirm, _digest(business_id, raw, problems, standard.standard_id), ai, notes=notes,
                   standard_id=standard.standard_id)


def _digest(business_id: UUID, raw: dict[str, Any] | None, problems: list[str], standard_id: UUID | None) -> str:
    """プレビューと登録が同じ中身かを確かめる指紋（業務と中身と問題と重ねた標準の版。1か所）。問題も入れる：確かめ（check）の外で
    見つかった問題（ファイルの読めない行・重なった行など。中身 raw には残らない）を、プレビューから消して登録できないように（2026-10-17
    の見直し）。標準の版も入れる：プレビューのあとで管理者が標準を新しくしたら、契約者が見ていない標準で版を作らない（0174）。"""
    return hashlib.sha256(json.dumps({"business_id": str(business_id), "raw": raw, "problems": problems,
                                      "standard_id": None if standard_id is None else str(standard_id)},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _kept(tx: Tx, business_id: UUID, raw: Any) -> tuple[Any, list[str], list[UUID]]:
    """取り込み直した中身に、依頼者の解決策で足した想定問答を残す（✅ 2026-11-06 ユーザー「残す」。中身に同じ問いがあれば中身が先＝
    client_solutions.keep_in の1か所）。(中身, 残した知らせ, 取り込んだ中身が先になった解決策) を返す。"""
    from salescall.core import client_solutions   # 解決策の部品が取込を読むので、ここで読む（読み合いにしない）
    if isinstance(raw, dict) and (raw.get("answers") is None or isinstance(raw.get("answers"), list)):
        return client_solutions.keep_in(tx, business_id, raw)
    return raw, [], []


def preview_shaped(tx: Tx, business_id: UUID, raw: Any, *, problems: list[str] | None = None,
                   where: dict[str, list[str]] | None = None, confirmed_names: Any = (),
                   decide_way_words: WayWordsDecider | None = None, way_pairs: WayPairs | None = None, **deciders: Any) -> Preview:
    """業務設定の形にした中身のプレビュー（取込のどの入口も通る共通の1か所：ファイル＝`preview`・⏳ 依頼者の画面から抜き出したもの
    （方式C）・業務の資料一式・画面の入力）。何も書かない。形にしたあと、AI に聞く前に、依頼者の解決策で足した想定問答を残し（`_kept`。
    ⛔ 2026-11-07 の第三者の見直し (2) の6：前はファイルの入口だけが残していた。⛔ 2026-11-06 の2回目の見直し (3) の A-1：残す文の名前も
    洗い出すので、AI に聞く前）、AI に1回だけ聞き（`ask_ai`。deciders＝その決め方）、確かめる（`review`）。返す Preview の ai を、登録
    （`register_shaped`）にそのまま渡す。方法を申し出の中身に入れる言い方は、確かめに通った中身から会話の側が並べる（way_pairs）ので、確かめのあとに1回だけ
    聞き（`_decided_way_words`）、決まれば AI の答えに入れて確かめ直す（登録も同じ AI の答えから同じ中身になる）。"""
    raw, kept, _ = _kept(tx, business_id, raw)
    ai = ask_ai(raw, **deciders) if isinstance(raw, dict) else {}
    p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems, where=where)
    if words := _decided_way_words(p, decide_way_words, way_pairs):
        p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai={**ai, "way_words": words}, problems=problems, where=where)
    p.notes = kept + p.notes
    return p


def review_solution(tx: Tx, business_id: UUID, raw: dict[str, Any], *, confirmed_names: Any, ai: dict[str, Any],
                    standard_id: UUID | None, placed_names: Any) -> Preview:
    """依頼者の解決策を足す道（core/client_solutions._add_line）の確かめ（入口の1つ。取込と同じ確かめ＝`review`）。取込の入口と違い、解決策を
    残す1か所（`_kept`）は通さない（元の版に問答を1つ足すだけ）。重ねる標準は、元の版の標準のまま（standard_id）。元の版に置いた名前は変えない
    （placed_names）。`review`・`register_reviewed` を呼んでよいのは、この部品の中だけ（app/checks の PY_ENTRY_CALLERS。⛔ 2026-11-07 の第三者の
    見直し (2) の8：取込の外から直に呼ぶと、解決策を残す1か所を通らずに版を作れた）。"""
    return review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, standard_id=standard_id, placed_names=placed_names)


def register_shaped(tx: Tx, business_id: UUID, raw: Any, *, expected_digest: str, ai: dict[str, Any] | None,
                    problems: list[str] | None = None, where: dict[str, list[str]] | None = None,
                    confirmed_names: Any = ()) -> UUID:
    """業務設定の形にした中身の登録（取込のどの入口も通る共通の1か所。`preview_shaped` と同じ道：解決策を残す → 確かめる）。プレビューと
    同じ中身のときだけ、新しい版を作る（AI には聞き直さない＝ai はプレビューの答え）。取り込んだ中身が先になった解決策（同じ問いの名前の
    答えを書いた・「使わない」にした）は、置き換えられたと残す（client_solutions.replaced＝もう「流れに入っていない前の解決策」に出さない。
    ⛔ 2026-11-07 の第三者の見直し (2) の4）。"""
    from salescall.core import client_solutions   # 解決策の部品が取込を読むので、ここで読む（読み合いにしない）
    raw, _, overridden = _kept(tx, business_id, raw)
    p = review(tx, business_id, raw, confirmed_names=confirmed_names, ai=ai, problems=problems, where=where)
    version = register_reviewed(tx, business_id, p, expected_digest=expected_digest)
    client_solutions.replaced(tx, overridden, version)
    return version


def preview(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, mapping: dict[str, Any] | None = None,
            confirmed_names: Any = (), find_names: NameFinder | None = None,
            decide_units: UnitDecider | None = None, decide_topics: TopicDecider | None = None,
            decide_meanings: MeaningDecider | None = None, decide_replies: ReplyDecider | None = None,
            decide_facts: FactDecider | None = None, decide_do_words: DoWordsDecider | None = None,
            decide_away: AwayDecider | None = None, decide_unclear: UnclearDecider | None = None, decide_doubt_words: DoWordsDecider | None = None,
            decide_ways: WaysDecider | None = None, decide_maybes: MaybeDecider | None = None,
            decide_purposes: PurposeDecider | None = None, decide_way_words: WayWordsDecider | None = None,
            way_pairs: WayPairs | None = None, offer_question: OfferQuestion | None = None) -> Preview:
    """ファイル（入口の1つ）を業務設定の形にして、AI に1回だけ聞き、確かめた結果を返す（何も書かない）。
    返す Preview の ai（AI の答え）を、登録（`register`）にそのまま渡す。way_pairs・offer_question は、決める処理と一緒につなぐ所が渡す会話の側の1か所
    （`WayPairs`・`OfferQuestion`）。"""
    sheets, problems, notes = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        return Preview(None, problems, [], notes=notes)
    raw, problems, where = _to_raw(sheets, problems, notes)
    p = preview_shaped(tx, business_id, raw, problems=problems, where=where, confirmed_names=confirmed_names,
                       find_names=find_names, decide_units=decide_units, decide_topics=decide_topics,
                       decide_meanings=decide_meanings, decide_replies=decide_replies, decide_facts=decide_facts,
                       decide_do_words=decide_do_words, decide_away=decide_away, decide_unclear=decide_unclear,
                       decide_doubt_words=decide_doubt_words, decide_ways=decide_ways, decide_maybes=decide_maybes,
                       decide_purposes=decide_purposes, decide_way_words=decide_way_words, way_pairs=way_pairs,
                       offer_question=offer_question)
    p.notes = notes + p.notes
    return p


class PreviewChanged(Exception):
    """プレビューのあとで、ファイル・対応付け・名前の確かめが変わった（もう一度プレビューを確かめる）。"""


def register_reviewed(tx: Tx, business_id: UUID, p: Preview, *, expected_digest: str) -> UUID:
    """契約者が確かめたプレビューと同じ中身なら、新しい版を作る（どの入口も、ここを通る。1つの取引。問題が1つでもあれば何も書かない）。"""
    # 指紋は、渡されたものを信じず、中身から出し直す（プレビューは書き換えられる。業務も指紋に入れる。2026-10-16 の見直し I8）
    if _digest(business_id, p.raw, p.problems, p.standard_id) != expected_digest:
        raise PreviewChanged("プレビューのあとで中身が変わりました。もう一度プレビューを確かめてください")
    if p.problems or p.raw is None:
        raise business_settings.SettingsInvalid(p.problems)
    return business_settings.save_version(tx, business_id, p.raw, standard_id=p.standard_id)


def register(tx: Tx, business_id: UUID, file_name: str, data: bytes, *, expected_digest: str,
             mapping: dict[str, Any] | None = None, confirmed_names: Any = (),
             ai: dict[str, Any] | None = None) -> UUID:
    """ファイルの入口の登録：読み直して、プレビューと同じ中身なら版を作る。AI には聞き直さない（ai＝プレビューの AI の答え）。"""
    sheets, problems, _ = _sheets(file_name, data, mapping or {})
    if problems and not sheets:
        raise business_settings.SettingsInvalid(problems)
    raw, problems, where = _to_raw(sheets, problems)
    return register_shaped(tx, business_id, raw, expected_digest=expected_digest, ai=ai, problems=problems, where=where,
                           confirmed_names=confirmed_names)
