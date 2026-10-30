"""会話から返る報告（試行ごとに1つ）。持ち主 CL-2（形は CL-1 の連絡帳 2-2「会話から返すもの」・版 0.4 の「通話の事実」）。

- 形と確かめは、この1か所（`from_json`）。書く（`save`。書き込む入口 core/conversation_io.py から呼ぶ）ときも、
  読む（`load`。締めの処理 core/settle.py が読む）ときも、同じ確かめを通す。合わない報告は受け取らない（ReportInvalid）。
- 「わからない」を既定値で埋めない（原則10）：分からないものは None のまま。判定が「決められない」として扱う。
- 名前（電話口の種別など）は DESIGN.md 4-3・6-2・7-1c のもの。
- **版 3（2026-10-19。CL-1 の連絡帳 4-22）**：会話の側が決める事実を、CL-1 の「通話の事実」の形のまま受け取る（`facts`＝種類 →
  起きた順の記録の並び。どの記録にも**根拠の発話**＝相手の発話の番号 `turn` とその文字 `said`（80字まで）。Intent.md 9-5・11-1）。
  発信禁止の申告・苦情・範囲外の求め・電話口の種別・かけ直しの申し出・締めた理由。判定が読む「申告があったか」「電話口の種別の
  並び」「かけ直し」などは、**この事実から読む**（`do_not_call`・`parties`・`callback` など。同じことを別の欄で二度受け取らない＝
  食い違いようがない）。
  - かけ直しの番号と時刻は、お客様が言ったまま（確かめ済みの言葉）を受け取る。番号をそろえるのは番号の正規化（Q1。
    `phone.normalize(spoken=True)`）、時刻に直すのは `spoken_time.read`（判定が通話の時刻を起点に読む）。どちらも1か所。
  - 版 2 から外したもの：急いでいて終えたか（締めた理由 `end` から読む＝`hurried`）・そろえた番号（言ったままの番号から読む）・
    かけ直しの時刻（言ったままの言葉から読む）。
- お詫びの電話で、文面を伝えられたか（apology_delivered。無ければ None。版 2 から）。
- **事実 connect（2026-10-22 足した。CL-1 の連絡帳 4-25。無くてもよい＝版は3のまま）**：目的のお客様に繋いだ発話と、繋いだ読み
  （`read`＝"ai"（会話AIの報告）と、「ご本人」と読んだ判定の名前）・判定を使う通話か（`judging`）・繋いだあとに届いた、その発話の
  判定（`late`＝判定の名前 → 読み）・繋ぐときまでに届いていた、その発話の判定（`turn_reads`。同じ形。2026-10-24 CL-1 の 4-127 (a)）。
  判定なしで繋いだ・繋ぐときの判定かあとの判定が「ご本人」でない通話は、通話後の検証が録音で確かめ直す
  （`target_unverified`。判断はこの1か所。記録は core/target_recheck.py。DESIGN.md 6-12 の (e)）。
- **CL-1 の 10-22 の報告（2026-10-22 足した。CL-1 の連絡帳 2-2・4-26・4-27。どれも無くてもよい＝版は3のまま）**：
  - 締めた理由 `not_transferred`（受付の方が、営業のお断りではない事情で取り次げないと言った）と、同じ発話の事実 `not_transferred`。
    判定は、繋がらなかった理由（`not_reached`）で読む（事実は記録だけ）。
  - 事実 `answered`（相手の質問に、決まった答えの録音を流した。`part`＝想定問答の部分の名前 `answer_…` か、答えられない質問 `unknown`）。
    記録だけ（判定には使わない）。
  - 事実 `target_back`（かけ直しの質問の途中で、目的のお客様かを確かめに戻った。`why`）・`callback_resume`（確かめた方が目的のお客様で
    なく、かけ直しの質問の続きへ戻った。`why`・`read`）。記録だけ。
  - かけ直しの記録の `read`（受け取った読み＝"ai" と判定の名前。空もある＝プログラムが進めた）と `connected`（かけ直しの質問の途中で
    目的のお客様に繋がった＝そのかけ直しは要らない）。`read` は記録だけ。`connected` のかけ直しは、判定が読むかけ直し（`callback`）に
    しない（1か所）。
  - 目的のお客様のいる業務の「繋がったか」は、繋いだ事実（connect）だけで決める（`reached`。CL-1 は繋ぐとき必ず connect を残す。
    電話口の種別の target はAIの報告で、判定と食い違うと繋がずに終わることがある）。「誰でも」の業務は、お客様と話した＝繋がった
    （業務設定から判定の入力に入れる。見直し18b の (3)）。「お客様と話したか」は、人の証の表（HUMAN_PARTIES・HUMAN_ENDS・
    HUMAN_FACTS）で決める（`talked` の1か所。見直し18b の (2)）。
- **取り消せない決定の2つ目の判定（2026-10-30 管理者のご決定 (6)。データの形 版 1.07。CL-2 の連絡帳 4-167 の頼み3。無くてもよい＝版は3のまま）**：
  お客様の断り（do_not_call・切れ方の callee_refused_last）・番号違い（電話口の種別 wrong_party）・怒っていた（complaint）は、発信禁止や
  行の終わりになる取り消せない決定。会話AIの報告と2つ目の判定（意味の判定）がそろったか（`irreversible_check`＝agreed：そろった／
  disagreed：食い違った／none：判定が無い）を、報告ごとに1つ受ける。無い・古い報告は none（判定が無い）。そろったときだけ、その場で決める
  （答えを受け取る決まり＝計画書 9-5 の 7 と同じ）。そろわなければ、通話後の録音の確かめ（録音から作り直した報告）で決まるまで保留
  （決めるのは core/approval.decision_wait の1か所。どの決定が取り消せない決定かは core/judge.irreversible の1か所）。録音から作り直した
  報告では、2つの耳がそろったかを同じ項目で返す。
- 取り消せない決定を、その通話の報告から外す形（`CallReport.without`。差し戻し・発信禁止の取り消しのあと、ふつうに締めるため）も、ここの1か所。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from salescall.core.access import Tx
from salescall.core.phone import normalize

VERSION = 3
FACT_KINDS = ("do_not_call", "complaint", "out_of_scope", "party", "callback", "ai_question", "voicemail_message", "end",
              "connect", "answered", "target_back", "callback_resume", "not_transferred", "substitute", "departed", "callee_question",
              "talk", "hold_timeout")
# substitute・departed・callee_question（CL-1 の連絡帳 4-28・2-2 の版 0.5。2026-10-22 ユーザー決定の相手の申し出。どれも無くてもよい）：
#   substitute＝電話口の方が、お呼びした方の代わりにご用件を伺うと申し出た（policy＝業務設定の応じ方・read・answer＝判断いただける
#   お立場かを伺った答え）。departed＝お呼びした方が在籍していない（policy・read・successor＝後任の方へ切り替えたあとか）。
#   callee_question＝お客様からの質問（answered＝業務の情報で答えた true／答えを持っていなかった false／答えに添えられた質問 null）。
#   どれも記録だけ（判定は、繋いだ事実・繋がらなかった理由・かけ直しの申し出で読む。依頼者へ渡すのは CL-3）
# talk：世間話・お礼・気遣いへの AI の一言（受け答え。CL-1 の連絡帳 4-30。記録だけ。依頼者へは渡さない）。
# callee_question と talk の AI の自分の言葉は同じ形（`_own_words` の1か所）：answer＝相手に言った文（確かめた文か、断らない一言の
#   録音の文。受け答えで通らなかったときは null）・drafts＝確かめに通らなかった文と理由の並び（通話後の検証用）・checked＝中身を
#   確かめた判定の名前（判定が届かなければ null）。⛔ 2026-10-23：前の problem（理由1つ）は drafts に置き換わった（CL-1 の 4-30）
# answered：相手の質問に、決まった答えの録音を流した（CL-1 の連絡帳 4-26。part＝部分の名前。記録だけ）
# target_back・callback_resume：かけ直しの質問の途中で目的のお客様かを確かめに戻った・違う方だったので続きへ戻った（4-27。記録だけ）
# not_transferred：受付の方が、事情があり取り次げないと言った発話（締めた理由 not_transferred と同じ発話。判定は not_reached で読む）。
#   again＝締めの前に「後ほど改めてお掛けしてもよろしいでしょうか」と聞いた答え（true＝よいと言われた／false＝断られた・はっきり
#   しなかった／無い＝聞いていない）。約束の電話で、当日の約束の幅の中でかけ直すかを決める（2026-10-22 ユーザー。DESIGN.md 7-2）
# hold_timeout：保留を頼まれ（「少々お待ちください」）、業務の設定の長さ（call.hold_wait。お客様が長さを言えばその長さ）を待ちきった
#   （CL-1 の連絡帳 4-42・4-44。2026-10-24 ユーザー：「業務ごとに選べる：デフォルト…5分、理由があればそれまで待つ。」）。根拠の発話＝
#   保留を頼まれた発話・waited_s＝待った秒（0より大きい数）。締めた理由 hold_timeout と組で出る。記録だけ（判定は、目的のお客様に繋がる
#   前の保留なら繋がらなかった理由 not_transferred＝日を改めて1回、繋がったあと・目的のお客様のいない業務なら「取れていない」のまま
#   ＝どちらも今の判定の1か所が読む。取り次げないと言われた発話＝事実 not_transferred とは別のもの＝通話後の検証が見分ける）
JUDGES = ("judge", "judge2")                             # 意味の判定の受け持ちの名前（CL-1 の flowctl.JUDGMENTS。代わりの Jev も、この名前で入る）
CONNECT_READS = ("ai",) + JUDGES                         # 繋いだ読み：会話AIの報告と、判定の受け持ち
TARGET_IS_THERE = "ご本人"                               # 判定の「目的のお客様ご本人」の読み（CL-1 の flowctl.TARGET_IS_THERE）
MEANING_MAX = 40                                         # 判定の読みの文字の長さの上限
# voicemail_message：留守番電話に伝言を残した（2026-10-19 ユーザー：「留守番電話に入れるのは最初の1回だけ」。同じ依頼者の同じ番号へは
# 2回目を残さない＝`voicemail_left` の1か所。CL-2 の連絡帳 4-106）
# ai_question：「AIですか？」「人間ですか？」と聞かれた（聞かれるたびに1つ。CL-1 の 2-2 の版 0.5＝2026-10-19。判定には使わない。記録だけ）
PARTY_KINDS = ("reception", "target", "substitute", "successor", "voicemail", "ivr", "fax", "wrong_party", "person")   # 4-3（区間ごと）
# substitute＝代わりの方・successor＝後任の方（CL-1 の PARTY_LABEL。2026-10-22 の版 0.5。どちらもお客様と話した区間）
# person＝お客様が出て話したが、役（受付か目的のお客様か）はまだ分からない（2026-10-19 の見直しで足した。CL-1 の連絡帳への頼み
# ＝CL-2 の 4-105）。⚠️ 版3の事実には「お客様が出た」が無く、目的の人のある業務で役が分かる前に切られた区間と、「誰でも」の業務
# （最初に出たお客様が目的のお客様＝相手を報告しない）の区間は、相手が空になり、判定が「会話に至らなかった」と読んで、黙って切った
# お客様の拒否を登録しなかった（原則14）。⇒ 見直し18b：相手が空でも、人の証の表（下の HUMAN_ENDS・HUMAN_FACTS）で「話した」と
# 読み、「誰でも」の業務は話した＝繋がった（業務設定から。judge.JudgeInput.reached_target）
PARTY_SOURCES = ("ai", "signal")                         # 会話AIの報告で分かった／音・回線の信号で分かった（CL-1 の `flowctl._party`
#   の by と同じ値。⚠️ 前は "report" を受け、CL-1 の "ai" の報告をまるごと断っていた＝CL-2 の 4-101 の約束と食い違い。見直し6）
CALLBACK_REASONS = ("absent", "long_absent", "day_off", "other_number", "later", "departed", "other_means", "ask_who", "ask_when", "our_promise")
# long_absent＝長期の不在（出張など、仕事で出ていてその日は戻らない）・day_off＝お休み（本日は休み・休暇・休職など。CL-1 の連絡帳
# 4-55 の (1)。✅ 2026-10-25 ユーザー：お休みに「お戻り」はおかしい⇒「次のご出社はいつか」を伺う。伺う一言が違うだけで、扱いは
# 長期の不在と同じ＝下の ABSENT_REASONS・UNTIL_BY_REASON に同じく入れる）・departed＝在籍していない・other_means＝ほかの手段（電話ではない。CL-1 の CALLBACK_LABEL の
# 版 0.5。2026-10-22 ユーザー決定）
ABSENT_REASONS = ("absent", "long_absent", "day_off")   # 不在の仲間（戻る時刻が取れなければ「不在」の方針。judge の順7 の1か所が読む）
ENDING_CALLBACKS = ("other_means",)                      # 電話ではかけ直さない申し出（約束にしない。judge の順7 の1か所が読む）
CALLBACK_OFFERS = ("callback", "message", "email", "fax", "post")   # お客様の申し出（CL-1 の OFFER_LABEL）：折り返し・伝言・メール・ファクス・郵送
CALLBACK_MEANS = ("email", "fax", "address", "message")  # 申し出で聞いた宛先・伝言の答え（取れた値の文字か null。CL-1 の CALLBACK_IDS）
CONNECT_WHO = ("target", "successor", "substitute")      # 繋いだ方：目的のお客様・後任の方・代わりの方（CL-1 の connect の who）
# 相手の申し出への応じ方の選択肢（業務設定。CL-1 の flowctl.POLICIES と同じ値。最初が既定＝2026-10-22 ユーザー決定）。正本はここだけ：
# 業務設定の確かめ（business_settings.POLICIES＝鍵に _policy を付けたもの）も、会話の報告の事実（代わりの方・在籍していない）も、これを読む
RESPONSE_POLICIES = {"substitute": ("decision_maker", "target_only", "anyone"), "message": ("decline", "leave"),
                     "departed": ("successor", "end")}
# かけ直しの日を選んだ理由の周期（お客様が言ったときだけ）：month＝月ぎめの業務・締め・振り込みなど月単位の理由（2026-10-19 ユーザー：
# 「月ぎめの業務の都合や振り込みなどの入金の間隔などの話の場合は、月単位で考えた方がいい」）。無ければ、言葉だけで読む（spoken_time）
CALLBACK_CYCLES = ("month",)
# 「〇時まで」の言い方の、いま電話を続けられなかった理由（2026-10-23 ユーザー：「かけ直さないといけない理由によるでしょ」「電話を切った
# 理由があるから、電話を続けられなかったと考えるべきでしょ。その理由を確かめないと、何時から3時までの何時が決められないでしょ！」）：
# other_party＝別の番号・別の方へ（「3時までにこちらの番号へ」）・unavailable＝それまでは出られない（「3時までには戻ります」「3時まで
# 会議です」）・preparing＝それまでに確認・準備をしておく（「3時までに担当者に確認しておきます」）。理由ごとの、かけてよい時間の決め方は
# お客様の時間の設定の1か所（calling_hours.CalleeSettings.until_rule。設定 callback.until_*）
UNTIL_WHYS = ("other_party", "unavailable", "preparing")
# かけ直しの理由（why）だけで決まる「〇時まで」の理由（同じことを2つの項目で表さない＝この理由には until_why を書かせない）：別の番号へ・
# 分かるお客様（a1）＝別の方へ／不在・長期の不在・お休み＝それまでは出られない／分かる時（a2）＝それまでに確かめておく
UNTIL_BY_REASON = {"other_number": "other_party", "ask_who": "other_party", "absent": "unavailable", "long_absent": "unavailable",
                   "day_off": "unavailable", "ask_when": "preparing"}
# 不在・別の番号・あとで（お客様に頼まれた。6-2）／分かるお客様・分かる時を聞いた（15-2-3 の a1・a2）／こちらが約束した
END_REASONS = ("done", "callee_hurry", "callee_refused_call", "callee_complaint", "wrong_party", "voicemail", "ivr", "fax",
               "callback", "not_transferred", "departed", "other_means", "hold_timeout")               # 締めた理由（CL-1 の END_LABEL の名前。「AIですか」では終えない＝2026-10-19 夜 ユーザー）
HURRIED_ENDS = frozenset({"callee_hurry", "callee_refused_call", "callee_complaint"})   # 急いでいる・断られた・怒っている（CL-1 の Flow.hurried と同じ）
NOT_REACHED_REASONS = ("absent", "not_transferred", "departed", "other_means")   # 目的のお客様に繋がらなかった理由（7-1。CL-1 の
#   NOT_REACHED_LABEL：在籍していない＝後任の方にも繋がらなかった・終える業務／ほかの手段を望まれた。2026-10-22 の版 0.5）
HUMAN_PARTIES = frozenset({"reception", "target", "substitute", "successor", "wrong_party", "person"})   # お客様と話した区間（person＝役がまだ分からない）
# ── 「お客様と話したか」（CallReport.talked）の材料の表（どの事実が人の証かは、ここだけで決める。⛔ 見直し18b の (2)：前は電話口の
#    種別・繋いだ事実・繋がらなかった理由・かけ直しの申し出だけを見た。「誰でも」の業務では CL-1 は電話口の種別を残さない（最初に出た
#    お客様が目的のお客様）ので、質問に答えて「質問が終わった」で締めた通話も、断られて締めた通話も「会話に至らなかった」と読み、
#    黙って切ったお客様の拒否（7-1c）も見なかった）
# 締めた理由のうち、人の言葉でしか起きないもの（質問が終わった・急いでいる・通話を断られた・苦情・番号違い・かけ直し・取り次げない・
# 在籍していない・ほかの手段・保留で待ちきった＝保留を頼むのは人の言葉。CL-1 の 4-42）。入れないもの（MACHINE_ENDS）：留守番電話・
# 自動音声・FAX。END_REASONS はこの2つにちょうど分かれる
HUMAN_ENDS = frozenset({"done", "callee_hurry", "callee_refused_call", "callee_complaint", "wrong_party", "callback",
                        "not_transferred", "departed", "other_means", "hold_timeout"})
MACHINE_ENDS = frozenset({"voicemail", "ivr", "fax"})
# 相手が人でないと起きない事実（CallReport の欄の名前）：繋いだ・かけ直しの申し出・苦情・範囲外の求め・「AIですか」・お客様の質問に
# 録音で答えた・確かめに戻った・続きへ戻った・取り次げない・代わりの方・在籍していない・お客様からの質問・受け答え・保留を頼まれた。
# 入れないもの：発信の拒否の申告（自動音声の「営業のお電話はお断りしています」も申告になりうる。拒否は判定の (2) が先に決める）・
# 留守番電話に伝言を残した・締めた理由（上の表で見る）・電話口の種別（HUMAN_PARTIES で見る）。
# 取れた答え（項目の値）は会話の報告に入れ物が無い（行の項目は通話をまたいでたまる）ので、報告の中で人の答えと分かる事実
# （お客様からの質問・受け答え・録音で答えた質問）で見る
HUMAN_FACTS = ("connects", "callbacks", "complaints", "out_of_scope_requests", "ai_questions", "answered", "target_backs",
               "callback_resumes", "not_transferred", "substitutes", "departures", "callee_questions", "talks", "hold_timeouts")
SPEAKERS = ("callee", "ai", "none")                      # 切れた瞬間に話していた側（7-1c）
# 取り消せない決定について、会話AIの報告と2つ目の判定（意味の判定）がそろったか（報告ごとに1つ。データの形 版 1.07）：
# agreed＝そろった（その場で決める）／disagreed＝食い違った／none＝判定が無い（無い・古い報告もこれ）。そろわなければ録音の確かめまで保留
IRREVERSIBLE_CHECKS = ("agreed", "disagreed", "none")
# 取り消せない決定の中身の名前（報告から外す単位。`CallReport.without` の1か所）：断り（発信禁止の申告・断りの言葉のあとに切れた）・
# 番号違い（電話口の種別 wrong_party）・怒っていた（苦情）・FAX の回線（電話口の種別 fax。音の信号から分かるので2つ目の判定は当てないが、
# 誤って入れた発信禁止を取り消したときに外す＝2026-10-30 (7)）
DECISION_PARTS = ("refusal", "wrong_party", "complaint", "fax")
SAID_MAX = 80                                            # 根拠の発話の文字の長さの上限（CL-1 の _note と同じ）


class ReportInvalid(Exception):
    """報告の形が違う（理由は日本語）。"""


@dataclass(frozen=True)
class Said:
    """根拠の発話：相手の発話の番号（通話の初めから数えた番号）と、その文字（会話AIの聞き取り。無ければ空）。"""
    turn: int
    said: str


@dataclass(frozen=True)
class Party:
    party: str                   # 電話口の種別
    by: str                      # ai（会話AIの報告）／signal（音・回線の信号）
    evidence: Said


@dataclass(frozen=True)
class Callback:
    reason: str
    time_said: str | None        # かけ直す時刻：お客様が言ったまま（確かめ済み。取れなかったら None）。時刻に直すのは spoken_time.read
    number_said: str | None      # 別の番号：お客様が言ったまま（確かめ済み。取れなかったら None。reason = other_number のときだけ）
    person: str | None           # 分かるお客様（a1）
    evidence: Said
    cycle: str | None = None     # その日を選んだ理由の周期（CALLBACK_CYCLES。お客様が言ったときだけ）
    read: tuple[str, ...] | None = None   # 受け取った読み（CONNECT_READS のうち。空＝プログラムが進めた。無い＝前の形の報告）。記録だけ
    connected: bool = False      # かけ直しの質問の途中で、目的のお客様に繋がった（このかけ直しは要らない）
    offer: str | None = None     # お客様の申し出（CALLBACK_OFFERS。無ければ None）。記録だけ（折り返しの申し出も、こちらからかけ直す約束）
    means: tuple[tuple[str, str | None], ...] = ()   # 申し出で聞いた宛先・伝言の答え（CALLBACK_MEANS → 取れた値。依頼者へ渡す＝CL-3）
    message_left: bool = False   # 伝言を残す業務で、伝言をお願いした（記録だけ）
    again: bool | None = None    # 不在で戻る時刻が分からなかったとき：今日戻る見込みがあり、後でかけてよいと言われたか（None＝聞いて
    #                              いない）。同じ日のうちにかけ直すかを決める（may_call_again の1か所。2026-10-22 ユーザー）
    back_today: bool | None = None   # 「本日お戻りのご予定は」への答え（記録だけ）
    beyond_limit_ok: bool = False    # お客様指定の歯止め時間の外の時刻（「23時以降なら」）を通話の中で確かめ、「はい」と言われた（2026-10-23
    #                                  ユーザー：「案B＆案A(確かめたうえで案A)」。読むのは時刻の読み spoken_time.read の1か所＝judge._when）
    until_why: str | None = None     # 「〇時まで」の言い方のときの、いま電話を続けられなかった理由（UNTIL_WHYS）。かけ直しの理由（reason）
    #                                  だけで決まらないとき（あとで など）だけ書く（UNTIL_BY_REASON の理由に書けば報告の形の誤り）

    @property
    def until_reason(self) -> str | None:
        """「〇時まで」の言い方のときの、いま電話を続けられなかった理由（1か所。UNTIL_WHYS のどれか。分からなければ None＝
        かけてよい時間を決めず、通話の中で確かめる）。"""
        return UNTIL_BY_REASON.get(self.reason, self.until_why)

    @property
    def number_e164(self) -> str | None:
        """番号の正規化（Q1）でそろえた番号（そろえられたときだけ。内線つき・桁が足りないなども、言ったままの番号は依頼者へ渡す。
        DESIGN.md 3-5）。"""
        return None if self.number_said is None else normalize(self.number_said, spoken=True).e164


@dataclass(frozen=True)
class Answered:
    """相手の質問に、決まった答えの録音を流した（事実 answered）。"""
    part: str                    # 想定問答の部分の名前（answer_…）か、答えられない質問（unknown）
    evidence: Said


@dataclass(frozen=True)
class Back:
    """かけ直しの質問の途中の動き（事実 target_back・callback_resume）。"""
    reason: str | None           # そのときのかけ直しの理由（CALLBACK_REASONS。target_back は、理由が決まる前の段なら None）
    read: tuple[str, ...] | None # 受け取った読み（callback_resume だけ。target_back は None）
    evidence: Said


@dataclass(frozen=True)
class Substitute:
    """代わりの方の申し出（事実 substitute）。記録だけ。"""
    policy: str                  # 業務設定の応じ方（RESPONSE_POLICIES["substitute"]）
    read: tuple[str, ...]        # 受け取った読み
    answer: str | None           # 判断いただけるお立場かを伺った答え（伺っていなければ None）
    evidence: Said


@dataclass(frozen=True)
class Departed:
    """お呼びした方が在籍していない（事実 departed）。記録だけ（判定は not_reached で読む）。"""
    policy: str                  # 業務設定の応じ方（RESPONSE_POLICIES["departed"]）
    read: tuple[str, ...]
    successor: bool              # 後任の方へ切り替えたあとに言われたか
    evidence: Said


@dataclass(frozen=True)
class Draft:
    """AI が書いたが、中身の確かめに通らなかった文（声に出していない。通話後の検証用。記録だけ）。"""
    text: str
    problem: str                 # 通らなかった理由（「中身を確かめられませんでした」＝判定が2秒に届かなかった文）


@dataclass(frozen=True)
class OwnWords:
    """AI の自分の言葉（お客様からの質問への答え・受け答えの一言。形は `_own_words` の1か所）。記録だけ。"""
    answer: str | None = None            # 相手に言った文（無ければ None）
    drafts: tuple[Draft, ...] = ()       # 確かめに通らなかった文
    checked: str | None = None           # 中身を確かめた判定の名前（届かなければ None）

    def to_json(self) -> dict[str, Any]:
        return {**({} if self.answer is None else {"answer": self.answer}),
                **({"drafts": [{"text": d.text, "problem": d.problem} for d in self.drafts]} if self.drafts else {}),
                "checked": self.checked}


@dataclass(frozen=True)
class CalleeQuestion:
    """お客様からの質問（事実 callee_question）。記録だけ（依頼者へ渡す＝CL-3）。"""
    answered: bool | None        # 業務の情報で答えた／答えを持っていなかった／答えに添えられた質問（None）
    evidence: Said
    own: OwnWords = OwnWords()   # AI の自分の言葉（答えに添えられた質問で、まだ答えていなければ空）


@dataclass(frozen=True)
class Talk:
    """世間話・お礼・気遣いへの AI の一言（事実 talk。CL-1 の 4-30）。記録だけ（依頼者へは渡さない）。"""
    evidence: Said
    own: OwnWords
    fallback: str | None = None  # AI の文が使えず、AI が選んだ一言の録音を流したとき、その録音の名前（CL-1 の phrases.TALK_FALLBACK。
    #                              例 talk_osewa。main の fda54b8 から。記録だけ。answer はその録音の文）


@dataclass(frozen=True)
class NotTransferred:
    """事情があり取り次げないと言われた（事実 not_transferred）。"""
    evidence: Said
    again: bool | None = None    # あとでまたかけてよいと言われたか（None＝聞いていない）


@dataclass(frozen=True)
class HoldTimeout:
    """保留を頼まれ、待ちきった（事実 hold_timeout。CL-1 の 4-42）。記録だけ。"""
    evidence: Said               # 保留を頼まれた発話
    waited_s: float              # 待った秒（業務の設定 call.hold_wait か、お客様が言った長さ）


@dataclass(frozen=True)
class End:
    reason: str
    evidence: Said


@dataclass(frozen=True)
class Cut:
    speaker: str | None          # 切れた瞬間に話していた側（分からなければ None）
    in_closing: bool             # 締めの言葉に入っていた・締めのあとだった
    callee_refused_last: bool    # お客様の最後の発話が、断りの言葉だった
    # 声が片方に届いていなかった証拠（2026-10-30 管理者の指摘：声が届かないお客様が黙って切った通話を、営業拒否にしない。
    # judge.classify_cut が「声が届かず切れた」＝audio にする）。無い（前の形の報告）＝ false。データの形 版 1.06
    callee_cannot_hear: bool = False   # お客様が「聞こえない」「もしもし？」と言っていた（こちらの声がお客様に届いていない）
    callee_silent: bool = False        # お客様の声がこちらにほとんど届いていなかった（出たあと、聞き取れる言葉が無いまま切れた）


@dataclass(frozen=True)
class Connect:
    """目的のお客様に繋いだ（事実 connect）。"""
    evidence: Said               # 繋いだ発話
    read: tuple[str, ...]        # 繋いだ読み（CONNECT_READS のうち）
    judging: bool                # 判定を使う通話か
    late: tuple[tuple[str, str], ...] = ()   # 繋いだあとに届いた、その発話の判定（受け持ちの名前, 読み）
    who: str | None = None       # 繋いだ方（CONNECT_WHO。無い＝前の形の報告＝目的のお客様）。誰の答えかの記録（依頼者への見せ方は CL-3）
    turn_reads: tuple[tuple[str, str], ...] = ()   # 繋いだ発話そのものに、繋ぐときまでに届いていた判定（受け持ちの名前, 読み）。
    #                                                CL-1 の連絡帳 4-34 の 4-127 (a)。無い＝届いていなかった・前の形の報告

    @property
    def unverified(self) -> bool:
        """判定を使う通話で、判定の読み無しで繋いだ（AIの読みだけ）・繋いだ発話そのものに届いていた判定、または繋いだあとに届いた
        判定に「ご本人」でないものがある（⛔ 2026-10-24 CL-1 の連絡帳 4-34 の 4-127 (a)：前は繋ぐときに届いていた判定の食い違いを
        見なかった。別の方の答えを目的のお客様の答えとして依頼者に渡すのは取り返しがつかないので、確かめ直す）。"""
        return self.judging and (not set(self.read) & set(JUDGES)
                                 or any(v != TARGET_IS_THERE for _, v in (*self.turn_reads, *self.late)))


@dataclass(frozen=True)
class CallReport:
    # 通話の事実（CL-1 の facts。種類ごとに、起きた順の記録）
    refusals: tuple[Said, ...]           # do_not_call：発信の拒否（営業拒否）の申告（原則14）
    complaints: tuple[Said, ...]         # complaint：苦情・怒り
    out_of_scope_requests: tuple[Said, ...]   # out_of_scope：範囲外の求めで終えた
    party_reports: tuple[Party, ...]     # party：電話口の種別（区間ごと）
    callbacks: tuple[Callback, ...]      # callback：かけ直しの申し出（いちばん新しいものが効く）
    ends: tuple[End, ...]                # end：締めた理由（いちばん新しいものが効く）
    # 会話の側の、そのほかの材料
    not_reached: str | None              # 目的のお客様に繋がらなかった理由
    cut: Cut | None                      # 切れ方の材料（お客様が出なかった通話は None）
    apology_delivered: bool | None = None   # お詫びの電話だけ：その通話で話したお客様に、文面を伝えられたか（10-1-2）
    ai_questions: tuple[Said, ...] = ()  # 事実 ai_question：「AIですか？」と聞かれた（記録だけ。CL-1 の版 0.5）
    voicemail_messages: tuple[Said, ...] = ()   # 事実 voicemail_message：留守番電話に伝言を残した
    connects: tuple[Connect, ...] = ()   # 事実 connect：目的のお客様に繋いだ（繋いだ読み）
    answered: tuple[Answered, ...] = ()  # 事実 answered：決まった答えの録音を流した（記録だけ）
    target_backs: tuple[Back, ...] = ()  # 事実 target_back：かけ直しの途中で、目的のお客様かを確かめに戻った（記録だけ）
    callback_resumes: tuple[Back, ...] = ()   # 事実 callback_resume：かけ直しの質問の続きへ戻った（記録だけ）
    not_transferred: tuple[NotTransferred, ...] = ()   # 事実 not_transferred：事情があり取り次げないと言われた発話
    substitutes: tuple[Substitute, ...] = ()   # 事実 substitute：代わりの方の申し出（記録だけ）
    departures: tuple[Departed, ...] = ()      # 事実 departed：お呼びした方が在籍していない（記録だけ）
    callee_questions: tuple[CalleeQuestion, ...] = ()   # 事実 callee_question：お客様からの質問（記録だけ）
    talks: tuple[Talk, ...] = ()         # 事実 talk：受け答えの一言（記録だけ）
    hold_timeouts: tuple[HoldTimeout, ...] = ()   # 事実 hold_timeout：保留を頼まれ、待ちきった（記録だけ）
    irreversible_check: str = "none"     # 取り消せない決定の2つ目の判定がそろったか（IRREVERSIBLE_CHECKS。無い・古い報告は none）

    # ── 判定が読むもの（どれも事実から読む。ここの1か所）
    @property
    def do_not_call(self) -> bool:
        return bool(self.refusals)

    @property
    def complaint(self) -> bool:
        return bool(self.complaints)

    @property
    def out_of_scope(self) -> bool:
        return bool(self.out_of_scope_requests)

    @property
    def parties(self) -> tuple[str, ...]:
        return tuple(p.party for p in self.party_reports)

    @property
    def talked(self) -> bool:
        """お客様と話したか（1か所。判定の切れ方・会話に至ったか・FAX の見分け・「誰でも」の業務の繋がったかが読む）：お客様の電話口の
        種別がある・目的のお客様に繋がらなかった理由がある・人の言葉で締めた（HUMAN_ENDS）・相手が人でないと起きない事実がある
        （HUMAN_FACTS）。どの事実が人の証かは、上の表だけで決める（見直し18b の (2)）。
        2026-10-22 の確かめ：受付の方が電話口の種別の報告なしに「取り次げない」「席を外しています」と言った通話を「会話に至らなかった」
        と読んでいた（繋がらなかった理由・かけ直しの申し出を足した）。⛔ 見直し18b：「誰でも」の業務の話した通話（電話口の種別が無い）も
        同じだった（締めた理由・お客様からの質問・受け答えを足した）。"""
        return (any(p in HUMAN_PARTIES for p in self.parties) or self.not_reached is not None
                or any(e.reason in HUMAN_ENDS for e in self.ends) or any(getattr(self, f) for f in HUMAN_FACTS))

    def reached(self, anyone: bool) -> bool:
        """目的のお客様と話せたか（報告から読む所。業務に目的のお客様がいるか＝anyone は判定の入力から。確かめ直しまで入れた判断は
        judge.JudgeInput.reached_target の1か所＝判定も締めもそれを読む）。
        - 「誰でも」の業務（目的のお客様のいない業務）：お客様と話した＝繋がった（talked。最初に出たお客様が目的のお客様。CL-1 は
          電話口の種別を残さない・役の分からないお客様＝person を残すこともある）
        - 目的のお客様のいる業務：繋いだ事実（connect）だけで決める（CL-1 は繋ぐとき必ず connect を残す。電話口の種別の target は
          AIの報告で、判定と食い違うと繋がずに終わる＝2026-10-22 の確かめ：AIが「目的の人」と報告し、判定は別の方、そのまま
          「取り次げない」で締めた通話を「繋がった」と読みえた）
        ⛔ 見直し18b の (3)：前は業務設定を見ず、報告に「目的のお客様のある通話」の印（繋いだ・繋がらなかった理由・確かめに戻った）が
        無ければ電話口の種別の target で見た。「誰でも」の業務で CL-1 が person を出すと、話した通話がすべて「繋がらなかった」になり、
        繋がらなかった理由が無いので判定待ちがあふれた。"""
        return self.talked if anyone else bool(self.connects)

    @property
    def target_unverified(self) -> bool:
        """目的のお客様だと確かめ直しが要る通話か（判断はこの1か所。CL-1 の連絡帳 4-25・DESIGN.md 6-12 の (e)）：判定を使う通話で、
        判定の読み無しで繋いだ、または繋いだあとに届いた判定が「ご本人」でない繋ぎが1つでもある。"""
        return any(c.unverified for c in self.connects)

    @property
    def may_call_again(self) -> bool:
        """目的のお客様に繋がらなかった通話で、同じ日のうちにまたかけてよいと言われたか（1か所）：その通話を締めた「不在」（戻る時刻の
        無いかけ直しの申し出。理由が不在の仲間）・「取り次げない」の記録のうち、いちばん新しいものの again が true のときだけ。
        断られた・はっきりしない・聞いていないときは False（同じ日にはかけ直さない側）。
        2026-10-22 ユーザー：「本日、本人が戻られる可能性があるのかと、戻られる可能性があるのであれば後ほどおかけしてもよろしい
        でしょうかというお尋ねが必要…同じ日に2回以上、その言葉がなくかけるのであれば煙たがられますよ。（営業拒否につながる。）」
        「OKなら、当日の約束の時刻の幅の中かけ直してもいいと思うけど、そうでなければ、当日の幅はやめたほうがいい」"""
        asked = [(n.evidence.turn, n.again) for n in self.not_transferred]
        asked += [(c.evidence.turn, c.again) for c in self.callbacks if c.reason in ABSENT_REASONS and c.time_said is None]
        return bool(asked) and max(asked, key=lambda x: x[0])[1] is True

    @property
    def departed_end(self) -> bool:
        """お呼びした方が在籍していないので、行を終える通話か（1か所）：業務で「お礼を言って終える」を選んでいた、または後任の方へ
        切り替えたあとに、また在籍していないと言われた（CL-1 の Flow._departed。お呼びできる方がもういない）。"""
        return any(d.policy == "end" or d.successor for d in self.departures)

    @property
    def turned_to_successor(self) -> bool:
        """この通話で、目的のお客様が後任の方に替わったか（1か所）：お呼びした方が在籍しておらず、業務の応じ方が「後任の方をお呼び
        する」（既定）で、行を終えない（departed_end でない）。後任の方に繋がっても繋がらなくても、この行のこれからの電話は後任の
        ご担当者様をお呼びする（2026-10-22 ユーザー：「基本は後任へつないでもらう。選択可能にしたはずです」）。"""
        return bool(self.departures) and not self.departed_end

    @property
    def callback(self) -> Callback | None:
        """判定が読むかけ直し（1か所）：いちばん新しい申し出。ただし、その質問の途中で目的のお客様に繋がった申し出（connected）は、
        もう要らないので None（CL-1 の連絡帳 4-27）。"""
        cb = self.callbacks[-1] if self.callbacks else None
        return None if cb is None or cb.connected else cb

    @property
    def end_reason(self) -> str | None:
        return self.ends[-1].reason if self.ends else None

    @property
    def closing_started(self) -> bool:
        """締めに入っていたか（1か所）：締めた理由（end）がある、または切れた瞬間が締めの言葉の中・締めのあとだった。こちらの仕組みが
        止まった通話でも、締めに入っていれば「途中で切れた」にしない（judge.cut_off。⛔ 2026-10-22 の見直し14：前は締めの言葉の
        あとで止まった通話にも、すぐお詫びの電話をかけた）。"""
        return bool(self.ends) or (self.cut is not None and self.cut.in_closing)

    @property
    def hurried(self) -> bool:
        """お客様が急いでいる・通話を断られた・怒っている、で締めたか。"""
        return self.end_reason in HURRIED_ENDS

    def without(self, parts: frozenset[str] | set[str] | tuple[str, ...]) -> CallReport:
        """取り消せない決定の中身（DECISION_PARTS）を、この報告から外した形（1か所。差し戻し・発信禁止の取り消しのあと、その通話を
        ふつうに締めるため＝core/settle._judge_input）。断り＝発信禁止の申告と、切れ方の「断りの言葉のあとに切れた」／番号違い・FAX＝その
        電話口の種別／怒っていた＝苦情。2つ目の判定の項目はそのまま（外さなかった決定は、そろっていればそのまま決まる）。"""
        unknown = set(parts) - set(DECISION_PARTS)
        if unknown:
            raise ValueError(f"報告から外せるのは {DECISION_PARTS} です：{sorted(unknown)}")
        if not parts:
            return self
        drop = {p for p in ("wrong_party", "fax") if p in parts}   # 電話口の種別の名前と、外す中身の名前は同じ
        cut = self.cut
        if cut is not None and "refusal" in parts:
            cut = replace(cut, callee_refused_last=False)
        return replace(self, refusals=() if "refusal" in parts else self.refusals,
                       complaints=() if "complaint" in parts else self.complaints,
                       party_reports=tuple(p for p in self.party_reports if p.party not in drop), cut=cut)

    def to_json(self) -> dict[str, Any]:
        def said(s: Said) -> dict[str, Any]:
            return {"turn": s.turn, "said": s.said}
        facts: dict[str, list[dict[str, Any]]] = {
            "do_not_call": [said(s) for s in self.refusals],
            "complaint": [said(s) for s in self.complaints],
            "out_of_scope": [said(s) for s in self.out_of_scope_requests],
            "party": [{**said(p.evidence), "party": p.party, "by": p.by} for p in self.party_reports],
            "callback": [{**said(c.evidence), "why": c.reason, "time": c.time_said, "number": c.number_said,
                          "person": c.person, **({"cycle": c.cycle} if c.cycle else {}),
                          **({"read": list(c.read)} if c.read is not None else {}),
                          **({"connected": True} if c.connected else {}),
                          **({"offer": c.offer} if c.offer else {}), **dict(c.means),
                          **({"message_left": True} if c.message_left else {}),
                          **({} if c.again is None else {"again": c.again}),
                          **({} if c.back_today is None else {"back_today": c.back_today}),
                          **({"beyond_limit_ok": True} if c.beyond_limit_ok else {}),
                          **({} if c.until_why is None else {"until_why": c.until_why})} for c in self.callbacks],
            "ai_question": [said(q) for q in self.ai_questions],
            "voicemail_message": [said(v) for v in self.voicemail_messages],
            "end": [{**said(e.evidence), "reason": e.reason} for e in self.ends],
            "connect": [{**said(c.evidence), "read": list(c.read), "judging": c.judging,
                         **({"late": dict(c.late)} if c.late else {}), **({"who": c.who} if c.who else {}),
                         **({"turn_reads": dict(c.turn_reads)} if c.turn_reads else {})}
                        for c in self.connects],
            "answered": [{**said(a.evidence), "part": a.part} for a in self.answered],
            "target_back": [{**said(b.evidence), "why": b.reason} for b in self.target_backs],
            "callback_resume": [{**said(b.evidence), "why": b.reason, "read": list(b.read or ())} for b in self.callback_resumes],
            "not_transferred": [{**said(n.evidence), **({} if n.again is None else {"again": n.again})}
                                for n in self.not_transferred],
            "substitute": [{**said(x.evidence), "policy": x.policy, "read": list(x.read),
                            **({} if x.answer is None else {"answer": x.answer})} for x in self.substitutes],
            "departed": [{**said(x.evidence), "policy": x.policy, "read": list(x.read), "successor": x.successor}
                         for x in self.departures],
            "callee_question": [{**said(x.evidence), "answered": x.answered, **x.own.to_json()} for x in self.callee_questions],
            "talk": [{**said(t.evidence), **t.own.to_json(), **({} if t.fallback is None else {"fallback": t.fallback})}
                     for t in self.talks],
            "hold_timeout": [{**said(h.evidence), "waited_s": h.waited_s} for h in self.hold_timeouts]}
        return {"version": VERSION, "facts": {k: v for k, v in facts.items() if v}, "not_reached": self.not_reached,
                "cut": None if self.cut is None else {"speaker": self.cut.speaker, "in_closing": self.cut.in_closing,
                                                      "callee_refused_last": self.cut.callee_refused_last,
                                                      "callee_cannot_hear": self.cut.callee_cannot_hear,
                                                      "callee_silent": self.cut.callee_silent},
                "apology_delivered": self.apology_delivered, "irreversible_check": self.irreversible_check}


def _bool(d: dict, key: str) -> bool:
    v = d.get(key)
    if not isinstance(v, bool):
        raise ReportInvalid(f"{key} は true / false です")
    return v


def _bool_or_false(d: dict, key: str) -> bool:
    """無ければ false の true / false（あとから足した項目。前の形の報告を受け取る）。"""
    return False if d.get(key) is None else _bool(d, key)


def _choice(v: Any, allowed: tuple[str, ...], key: str, *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if v not in allowed:
        raise ReportInvalid(f"{key} は {allowed} のどれかです（{v!r}）")
    return v


def _until_why(r: dict, reason: str) -> str | None:
    """かけ直しの申し出の until_why（「〇時まで」の理由）。かけ直しの理由（why）だけで決まる理由（UNTIL_BY_REASON）に書いてあれば、
    同じことを2つの項目で表しているので形の誤り（食い違えば、どちらが正しいか決められない）。"""
    if "until_why" not in r:
        return None
    if reason in UNTIL_BY_REASON:
        raise ReportInvalid(f"facts.callback.until_why は、かけ直しの理由が {reason} のときは書きません（理由だけで "
                            f"{UNTIL_BY_REASON[reason]} と決まります）")
    return _choice(r["until_why"], UNTIL_WHYS, "facts.callback.until_why")


def _keys(d: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        raise ReportInvalid(f"{where} の形が違います")
    extra = set(d) - allowed
    if extra:
        raise ReportInvalid(f"{where} に知らない項目があります：{sorted(extra)}")
    return d


def _text(v: Any, key: str) -> str | None:
    """言ったままの言葉（無ければ None。空の文字は受け取らない）。"""
    if v is not None and (not isinstance(v, str) or not v.strip()):
        raise ReportInvalid(f"{key} は、お客様が言ったままの言葉（文字）です")
    return v


def _value(v: Any, key: str) -> str | None:
    """申し出で取れた値（言い直して確かめた値の文字。取れなかったら None）。"""
    if v is not None and (not isinstance(v, str) or not v.strip()):
        raise ReportInvalid(f"{key} は、取れた値の文字か null です")
    return v


def _true(d: dict, key: str) -> bool:
    """あるときは true だけの印。"""
    if d.get(key) is not True:
        raise ReportInvalid(f"{key} は、あるときは true です")
    return True


def _judge_reads(v: Any, key: str) -> tuple[tuple[str, str], ...]:
    """判定の受け持ち → 読み（MEANING_MAX 字まで）の形（無くてよい。繋いだ事実の late・turn_reads の1か所）。"""
    v = v or {}
    if not isinstance(v, dict) or any(k not in JUDGES or not isinstance(x, str) or not x.strip() or len(x) > MEANING_MAX
                                       for k, x in v.items()):
        raise ReportInvalid(f"{key} は、判定の受け持ち {JUDGES} → 読み（{MEANING_MAX} 字まで）です")
    return tuple(sorted(v.items()))


def _reads(v: Any, key: str) -> tuple[str, ...]:
    """受け取った読みの並び（"ai" と判定の名前。重ならない。空もある＝プログラムが進めた）。"""
    if not isinstance(v, list) or any(x not in CONNECT_READS for x in v) or len(set(v)) != len(v):
        raise ReportInvalid(f"{key} は {CONNECT_READS} のうちの並び（重ならない）です")
    return tuple(sorted(v))


def _records(facts: dict, kind: str, extra: set[str]) -> list[tuple[dict, Said]]:
    """その種類の記録の並び（根拠の発話を確かめて取り出す）。"""
    rows = facts.get(kind, [])
    if not isinstance(rows, list):
        raise ReportInvalid(f"facts.{kind} は記録の並びです")
    out = []
    for i, r in enumerate(rows):
        where = f"facts.{kind}[{i}]"
        r = _keys(r, {"turn", "said"} | extra, where)
        turn, said = r.get("turn"), r.get("said")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise ReportInvalid(f"{where}.turn は相手の発話の番号（0 以上の整数）です")
        if not isinstance(said, str) or len(said) > SAID_MAX:
            raise ReportInvalid(f"{where}.said は根拠の発話の文字（{SAID_MAX} 字まで）です")
        out.append((r, Said(turn, said)))
    return out


def from_json(raw: Any) -> CallReport:
    """報告を確かめて読む（ただ1つの確かめ）。"""
    d = _keys(raw, {"version", "facts", "not_reached", "cut", "apology_delivered", "irreversible_check"}, "報告")
    if d.get("version") != VERSION:
        raise ReportInvalid(f"報告の版が違います（{d.get('version')!r}。この部品は {VERSION}）")
    facts = _keys(d.get("facts"), set(FACT_KINDS), "facts")
    refusals = tuple(s for _, s in _records(facts, "do_not_call", set()))
    complaints = tuple(s for _, s in _records(facts, "complaint", set()))
    oos = tuple(s for _, s in _records(facts, "out_of_scope", set()))
    parties = tuple(Party(_choice(r.get("party"), PARTY_KINDS, "facts.party.party"),
                          _choice(r.get("by"), PARTY_SOURCES, "facts.party.by"), s)
                    for r, s in _records(facts, "party", {"party", "by"}))
    callbacks = []
    for r, s in _records(facts, "callback", {"why", "time", "number", "person", "cycle", "read", "connected", "offer",
                                             "message_left", "again", "back_today", "beyond_limit_ok", "until_why",
                                             *CALLBACK_MEANS}):
        reason = _choice(r.get("why"), CALLBACK_REASONS, "facts.callback.why")
        number = _text(r.get("number"), "facts.callback.number")
        if number is not None and reason != "other_number":
            raise ReportInvalid("番号を持つのは、別の番号のかけ直しだけです")
        callbacks.append(Callback(reason, _text(r.get("time"), "facts.callback.time"), number,
                                  _text(r.get("person"), "facts.callback.person"), s,
                                  _choice(r.get("cycle"), CALLBACK_CYCLES, "facts.callback.cycle", optional=True),
                                  None if "read" not in r else _reads(r["read"], "facts.callback.read"),
                                  "connected" in r and _bool(r, "connected"),
                                  _choice(r.get("offer"), CALLBACK_OFFERS, "facts.callback.offer", optional=True),
                                  tuple((k, _value(r[k], f"facts.callback.{k}")) for k in CALLBACK_MEANS if k in r),
                                  "message_left" in r and _true(r, "message_left"),
                                  _bool(r, "again") if "again" in r else None,
                                  _bool(r, "back_today") if "back_today" in r else None,
                                  "beyond_limit_ok" in r and _bool(r, "beyond_limit_ok"),
                                  _until_why(r, reason)))
    ends = tuple(End(_choice(r.get("reason"), END_REASONS, "facts.end.reason"), s)
                 for r, s in _records(facts, "end", {"reason"}))
    # ⚠️ 電話口の種別の target（AIの報告）と not_reached は、両方あってよい（AIが「目的の人」と報告したが、判定と食い違って繋がずに
    # 終わった通話。繋がったかは繋いだ事実で決める＝reached）。前はここで報告ごと断っていた（2026-10-22 の確かめ）
    not_reached = _choice(d.get("not_reached"), NOT_REACHED_REASONS, "not_reached", optional=True)
    cut = None
    if d.get("cut") is not None:
        c = _keys(d["cut"], {"speaker", "in_closing", "callee_refused_last", "callee_cannot_hear", "callee_silent"}, "cut")
        # 声が届いていなかった証拠の2つは、無ければ false（前の形の報告・まだ書かない会話の処理＝データの形 版 1.06 で足した）
        cut = Cut(_choice(c.get("speaker"), SPEAKERS, "cut.speaker", optional=True), _bool(c, "in_closing"),
                  _bool(c, "callee_refused_last"), _bool_or_false(c, "callee_cannot_hear"), _bool_or_false(c, "callee_silent"))
    delivered = d.get("apology_delivered")
    if delivered is not None and not isinstance(delivered, bool):
        raise ReportInvalid("apology_delivered は true / false です")
    ai_questions = tuple(s for _, s in _records(facts, "ai_question", set()))
    voicemail = tuple(s for _, s in _records(facts, "voicemail_message", set()))
    if voicemail and "voicemail" not in (p.party for p in parties):
        raise ReportInvalid("留守番電話の伝言は、電話口の種別が留守番電話の通話だけです")
    connects = []
    for r, s in _records(facts, "connect", {"read", "judging", "late", "who", "turn_reads"}):
        read = r.get("read")
        if not isinstance(read, list) or not read or any(x not in CONNECT_READS for x in read) or len(set(read)) != len(read):
            raise ReportInvalid(f"facts.connect.read は {CONNECT_READS} のうちの並び（重ならず、1つ以上）です")
        connects.append(Connect(s, tuple(sorted(read)), _bool(r, "judging"), _judge_reads(r.get("late"), "facts.connect.late"),
                                _choice(r.get("who"), CONNECT_WHO, "facts.connect.who", optional=True),
                                _judge_reads(r.get("turn_reads"), "facts.connect.turn_reads")))
    if connects and not_reached is not None:
        raise ReportInvalid("目的のお客様に繋いだのに、繋がらなかった理由があります")
    if any(c.connected for c in callbacks) and not connects:
        raise ReportInvalid("かけ直しの途中で目的のお客様に繋がった（connected）のに、繋いだ事実（connect）がありません")
    answered = []
    for r, s in _records(facts, "answered", {"part"}):
        part = r.get("part")
        if not isinstance(part, str) or not part.strip():
            raise ReportInvalid("facts.answered.part は、流した答えの部分の名前（文字）です")
        answered.append(Answered(part, s))
    # target_back の why は、かけ直しの理由がまだ決まっていない段で確かめに戻ったときは null（CL-1 の _target_back が、その時の
    # かけ直しの理由をそのまま残す。記録だけ）
    backs = tuple(Back(_choice(r.get("why"), CALLBACK_REASONS, "facts.target_back.why", optional=True), None, s)
                  for r, s in _records(facts, "target_back", {"why"}))
    resumes = tuple(Back(_choice(r.get("why"), CALLBACK_REASONS, "facts.callback_resume.why"),
                         _reads(r.get("read"), "facts.callback_resume.read"), s)
                    for r, s in _records(facts, "callback_resume", {"why", "read"}))
    not_transferred = tuple(NotTransferred(s, _bool(r, "again") if "again" in r else None)
                            for r, s in _records(facts, "not_transferred", {"again"}))
    substitutes = tuple(Substitute(_choice(r.get("policy"), RESPONSE_POLICIES["substitute"], "facts.substitute.policy"),
                                   _reads(r.get("read"), "facts.substitute.read"),
                                   _value(r.get("answer"), "facts.substitute.answer"), s)
                        for r, s in _records(facts, "substitute", {"policy", "read", "answer"}))
    departures = tuple(Departed(_choice(r.get("policy"), RESPONSE_POLICIES["departed"], "facts.departed.policy"),
                                _reads(r.get("read"), "facts.departed.read"), _bool(r, "successor"), s)
                       for r, s in _records(facts, "departed", {"policy", "read", "successor"}))
    # 確かめに戻った（target_back）のは、かけ直しの申し出の途中か、代わりの方と話している途中（2026-10-22 の CL-1 の直し：代わりの方の
    # あとにご本人が出た）か、約束の電話で「取り次げない」と言われ、約束の一言（P1）を伺っている間に取り次がれたとき（受付の方が
    # 考えを変えた。CL-1 の連絡帳 4-47＝Flow.asking_reception。そのあと繋げば connect が続く）。続きへ戻った（callback_resume）のは、
    # かけ直しの申し出の途中だけ
    if (backs and not (callbacks or substitutes or not_transferred)) or (resumes and not callbacks):
        raise ReportInvalid("かけ直しの申し出・代わりの方の申し出・取り次げなかった記録が無いのに、確かめに戻った記録があります（または、"
                            "かけ直しの申し出が無いのに、続きへ戻った記録があります）")
    questions = []
    for r, s in _records(facts, "callee_question", {"answered"} | OWN_WORDS_KEYS):
        if "answered" not in r or (r["answered"] is not None and not isinstance(r["answered"], bool)):
            raise ReportInvalid("facts.callee_question.answered は true / false / null です")
        questions.append(CalleeQuestion(r["answered"], s, _own_words(r, "facts.callee_question")))
    talks = tuple(Talk(s, _own_words(r, "facts.talk"), _text(r.get("fallback"), "facts.talk.fallback"))
                  for r, s in _records(facts, "talk", OWN_WORDS_KEYS | {"fallback"}))
    holds = []
    for r, s in _records(facts, "hold_timeout", {"waited_s"}):
        w = r.get("waited_s")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not w > 0:
            raise ReportInvalid("facts.hold_timeout.waited_s は、待った秒（0より大きい数）です")
        holds.append(HoldTimeout(s, float(w)))
    # 取り消せない決定の2つ目の判定（データの形 版 1.07）：無い・null（古い報告・まだ書かない会話の処理）は「判定が無い」
    check = "none" if d.get("irreversible_check") is None else \
        _choice(d["irreversible_check"], IRREVERSIBLE_CHECKS, "irreversible_check")
    return CallReport(refusals, complaints, oos, parties, tuple(callbacks), ends, not_reached, cut, delivered, ai_questions,
                      voicemail, tuple(connects), tuple(answered), backs, resumes, not_transferred, substitutes, departures,
                      tuple(questions), talks, tuple(holds), check)


OWN_WORDS_KEYS = {"answer", "drafts", "checked"}


def _own_words(r: dict[str, Any], where: str) -> OwnWords:
    """AI の自分の言葉（callee_question・talk で同じ形。読むのはここだけ）。"""
    drafts = r.get("drafts", [])
    if not isinstance(drafts, list):
        raise ReportInvalid(f"{where}.drafts は、確かめに通らなかった文の並びです")
    out = []
    for i, d in enumerate(drafts):
        d = _keys(d, {"text", "problem"}, f"{where}.drafts[{i}]")
        text, problem = _text(d.get("text"), f"{where}.drafts[{i}].text"), _text(d.get("problem"), f"{where}.drafts[{i}].problem")
        if text is None or problem is None:
            raise ReportInvalid(f"{where}.drafts[{i}] には、文（text）と通らなかった理由（problem）が要ります")
        out.append(Draft(text, problem))
    return OwnWords(_text(r.get("answer"), f"{where}.answer"), tuple(out), _text(r.get("checked"), f"{where}.checked"))


def save(tx: Tx, attempt_id: UUID, report: CallReport) -> None:
    """試行の報告を置く（1回だけ。変更不可）。形はもう一度確かめる（書く前の確かめも、読むときと同じ1つ）。"""
    data = from_json(report.to_json()).to_json()
    tx.execute("INSERT INTO salescall.call_reports (tenant_id, client_id, attempt_id, report)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)",
               (attempt_id, json.dumps(data, ensure_ascii=False)))


def save_rebuilt(tx: Tx, attempt_id: UUID, report: CallReport, *, evidence: str) -> int:
    """通話の録音から作り直した報告を置く（会話の担当が呼ぶ。「判定できない」の行を判定し直すため。0021）。元の報告は変えない。
    形の確かめは、ふつうの報告と同じ1つ。"""
    data = from_json(report.to_json()).to_json()
    return tx.one("INSERT INTO salescall.call_report_rebuilds (tenant_id, client_id, attempt_id, report, evidence, rebuilt_by)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())"
                  " RETURNING rebuild_id", (attempt_id, json.dumps(data, ensure_ascii=False), evidence))["rebuild_id"]


def load_with_source(tx: Tx, attempt_id: UUID) -> tuple[CallReport | None, str | None]:
    """報告と、その出どころ（original＝元の報告／rebuild:番号＝作り直し）。作り直しがあれば、いちばん新しいものを使う。"""
    row = tx.one("SELECT report, rebuild_id FROM salescall.call_report_rebuilds WHERE attempt_id = %s"
                 " ORDER BY rebuild_id DESC LIMIT 1", (attempt_id,))
    if row is not None:
        return from_json(row["report"]), f"rebuild:{row['rebuild_id']}"
    row = tx.one("SELECT report FROM salescall.call_reports WHERE attempt_id = %s", (attempt_id,))
    return (None, None) if row is None else (from_json(row["report"]), "original")


def load(tx: Tx, attempt_id: UUID) -> CallReport | None:
    return load_with_source(tx, attempt_id)[0]


def voicemail_left(tx: Tx, phone_e164: str) -> bool:
    """同じ依頼者（取引の依頼者）の、その番号へ、もう留守番電話の伝言を残したか（1か所。2026-10-19 ユーザー：「留守番電話に入れるのは
    最初の1回だけ」・数える単位は「同じ依頼者のすべての一覧で、同じお客様（同じ番号）」）。会話の処理は、伝言を残す前にこれを読み、
    残していれば何も言わずに切る（依頼者をまたいでは数えない＝サービスが別）。作り直した報告（call_report_rebuilds）も見る。"""
    return tx.one(
        "SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = %s AND ("
        "  EXISTS (SELECT 1 FROM salescall.call_reports r WHERE r.attempt_id = a.attempt_id"
        "          AND jsonb_array_length(coalesce(r.report -> 'facts' -> 'voicemail_message', '[]'::jsonb)) > 0)"
        "  OR EXISTS (SELECT 1 FROM salescall.call_report_rebuilds b WHERE b.attempt_id = a.attempt_id"
        "          AND jsonb_array_length(coalesce(b.report -> 'facts' -> 'voicemail_message', '[]'::jsonb)) > 0))) AS left",
        (phone_e164,))["left"]
