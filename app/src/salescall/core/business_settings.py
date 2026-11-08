"""業務設定の版（引き継ぎの (10)。Intent.md 6章・DESIGN.md 3-2・6-3・6-4）。持ち主 CL-2。

**業務設定は1つの形（`check` が受ける形）だけ**。画面で入れても、ファイルから取り込んでも、この形にしてから同じ確かめを通す
（取込は、ファイルをこの形にするだけ。確かめを2つ作らない）。確かめに通らなければ、何も置かない（全部か、何も無いか）。

- 版を作る（`save_version`）：1つの取引で、版・質問（番号・並び・型・必須か）・業務の決まり（core/business_rules.py）・
  中身（名乗り・目的・目的のお客様・忙しいときの一言・声の型・質問の文と選択肢と分岐・名前の一覧）を書く。版は変えない。
  直すときは新しい版（`restore` は、前の版の中身から新しい版を作る＝前の版へ戻す）。
- 実施回に版を割り当てる（`assign`。記録つき。DESIGN.md 3-2）：割り当てたら、品質の段の始め方（core/quality_stage.py の
  `prepare`）を呼ぶ（模擬通話を待つか、最初の段を開く）。
- 確かめること（Intent.md 6-4）：形の誤り・必須の欄の不足／分岐先が無い／どこからも行かない質問（必須の質問へ行く道が無いを含む）／
  終わらない輪／受け皿（どの答えにも当たらないときの行き先）が無い／**必須の質問を飛ばして終わる道**（その答えが「断り」でも
  システムの答えでもないのに飛ばすと、判定が「一部だけ取れた」になり、断ったお客様にかけ直すことになる）／読みを確かめていない名前／
  必須の質問が1つも無い／選択肢の名前が、出力の印・システムが足す答えと同じ／断りの答えが選択肢に無い／業務の決まりの確かめ。
  「終了条件の矛盾」は、この形では起きない（終わりは、分岐の「終わり」か最後の質問の次の1つだけ）。
- 名前の読み（Intent.md 6-8。決め方は core/readings.py の1か所）：読みごとに、誰が決めたか（`reading_by`：入口が持ってきた／
  AI が決めた／契約者が確かめた）を持つ。**読みが決まらない名前も置ける**（読みは空、考えられる読み `candidates` と、まず言って
  みる読み `likely` を持つ。会話の中で確かめる。2026-10-16 ユーザー・CL-1 の連絡帳 4-15）。**契約者が確かめるのは、検証モードの
  業務の、AI が決めた読みだけ**（「検証中はAIが認識したものを私が確認します」。検証モードかは承認の部分＝core/approval.py に聞く）。
  **名乗りの担当者名の読みは必ず入口で入れる**（2026-10-16 ユーザー決定＝案C。名乗りでは言い換えられず、個人向けでは名乗ることが
  法律の確認事項。AI では決めない。CL-1 の連絡帳 4-16）。名前の読みの正本は名前の一覧（`names`）で、名乗り（`caller`）の読みは、
  その写し（同じでなければ通さない）。名前の洗い出しと読みの決め方は CL-1（`names.decide`。2つのモデルが合うときだけ決まる）。
- 数の質問の単位（`unit`。CL-1 の連絡帳 4-14）：数の型の質問だけが持つ（空でもよい＝補わない）。決めるのは取込のときのプログラム
  （CL-1 の決め方。管理者も契約者も確かめない）。この部品は形を確かめるだけ。
- 想定問答（`answers`。Intent.md 5-3 (2)「AIが答えてよいこと」・9-4「範囲外の質問」。CL-1 の連絡帳 4-26）：(問いの名前, 答えの文)
  の並び（無くてもよい）。お客様に聞かれたら、AI は問いの名前を選ぶだけで、答えの文は依頼者の単位の録音で流す（録音の部分
  `answer_問いの名前`。部分の一覧は会話の側＝CL-1 の `instruction.Business.replay_parts`）。問いの名前は録音のファイル名に入るので、
  Windows で使えない文字を使わない。AI が選ぶ録音の部分の名前（`ANSWER_RESERVED`）と同じ名前にしない（取り違える）。
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import (approval, attempts, business_rules, calendar, call_report, calling_hours, notify, quality_stage,
                            readings)
from salescall.core.access import Tx

END = "END"                                       # 分岐の行き先「終わり」
# 質問の番号に使えない名前（1か所）。質問の番号は、会話の側で、システムが足す質問の番号（phase0/flowctl.py の CALLBACK_IDS・
# SUBSTITUTE_ID＝C1〜C5・M1・S1。メール・ファクス・住所・伝言・お立場）と同じ並びに入り、録音の部分の名前にもなる（業務の質問の録音
# ＝依頼者の単位の組の、質問の番号の部分）。同じ依頼者の単位の組には名乗りの部分（instruction.Business.replay_parts の鍵＝greeting・
# intro・purpose・ai・target・target_confirm・target_reconfirm・voicemail・message、想定問答の答え＝ANSWER_PART_PREFIX で始まる名前）が
# 入るので、同じ名前だと取り違える（見直し9）。契約者の単位の組（締め closing・closing_declined・相づち・申し出に応じる一言など
# ＝phrases.RECORDED）は別の組で、流すときも別の種類（会話の側の play の kind が recorded・closing）なので、ぶつからない＝ここに入れない
# （⛔ 見直し17d：前は closing・closing_declined の2つだけを入れ、ほかの契約者の単位の名前は入れていなかった＝半端。逆に、依頼者の
# 単位の target_reconfirm と、想定問答の answer_ で始まる名前が抜けていた）。会話の側に名前が増えたら、ここに足す（CL-2.md 4-121）。
# 2026-10-24 CL-1 の連絡帳 4-32・4-41 で足した：システムが足す質問 C6（本日のお戻りのご予定）・C7（改めてお掛けしてよいか）・
# P1（約束の電話の一言）・C8（分かるお客様）・C9（分かる時）と、部分の名前 promise_again・target_successor（お名前の入る一言。
# 流すのは名前の録音の台帳からだが、流す部品の中で質問の番号と同じ並びに入る）。CL-1 の試験 test_offline が、会話の側の番号と
# 部分の名前がすべてここに入っているかを確かめる。
# 2026-11-01 CL-1 の連絡帳 4-63 (b) で足した：R1（ご本人へのご遠慮の確かめ「今後このようなお電話はご遠慮したほうがよろしいでしょうか？」。
# 依頼主へ渡す項目には入らない＝ほかのシステムの質問と同じ）。4-64 の (c) で足した：H1（急いでいる方への「それではまた改めてお掛けしても
# よろしいでしょうか？」）。4-65 の (d) で足した：target_full・target_successor_full（お名前だけではどの方か分からないと言われたときに、
# 部署とフルネームでお呼びし直す一言。流すのは名前の録音の台帳から＝target・target_successor と同じ）。4-66 の (d) で足した：N1（説明した
# 内容をご承認いただけなかったときの「ご納得いただけなかった点はどの部分になりますでしょうか？」。答えは通話の事実 not_agreed＝依頼者へ
# 返すが、項目には入らない＝ほかのシステムの質問と同じ）。4-66 のあとの (f) で足した：N2（「ほかにご納得いただけない点はございますで
# しょうか？」）・N3（「ご説明した内容でご納得いただけましたでしょうか？」。どちらもご納得いただけなかった点のご説明のあとに伺う）。
# D1（CL-1 の連絡帳 4-69：ご遠慮の確かめ「今後このようなお電話はご遠慮したほうがよろしいでしょうか？」の番号を R1 から替える。
# R1 は待たせた方が戻ったかの問いの番号として残る＝どちらも使えない）。
# CL-1 の連絡帳 4-72 の (k) で足した：W1（選ぶ質問の「どれも要らない」・最終目的の質問への言い切っていないお断りのあとに、ほかのご希望を伺う
# 「それでは（選択肢）のほかにご希望はございますでしょうか？」）・C10（ご連絡の方法に LINE を言っていただいたときの「LINEのID」。✅ 2026-11-03
# 日本時間20時48分 ユーザー：「「LINEならいいです」と回答いただけたら、LINEアドレスを伺うというステップを忘れないように。」）。
# CL-1 の連絡帳 4-81 の (4) で足した：C11（LINE の連絡先の伺い方＝ID を伺うか SMS かメールからご招待するか）・C12（SMS でご招待する携帯電話の番号）
# （✅ 2026-11-04 日本時間19時26分 ユーザー：「LINE IDでは友達追加できないことが多い。SMSかメールを選ばれたら、もし、携帯電話番号やメールを伺っていなければ
# ここで確認。」）。
# CL-1 の連絡帳 4-113 で足した：D2（本題に入ったあとの「もういいです。」に、ご遠慮の一言 D1 より先に1通話1回伺う「何か問題でもございましたでしょうか？」。
# 録音は共通の一言 ai_answer をそのまま流す。答えの気になる点は通話の事実 concern＝依頼者へ返すが、項目には入らない＝ほかのシステムの質問と同じ。
# ✅ 2026-11-08 日本時間2時21分15秒 ユーザー）。使えると、業務の質問の答えと本心を伺った答えを取り違える。
RESERVED_IDS = frozenset({END, "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12", "M1", "S1", "P1", "R1", "D1", "D2", "H1", "H2", "N1", "N2", "N3", "W1",
                          "greeting", "intro",
                          "purpose", "ai", "target", "target_confirm", "target_reconfirm", "promise_again", "target_successor",
                          "target_full", "target_successor_full", "unavailable_confirm",
                          "voicemail", "message", "busy"})   # busy＝引き止めの一言の業務の録音の部分（CL-1 の連絡帳 4-75。ai・voicemail・message と同じ）
ANSWER_PART_PREFIX = "answer_"                    # 想定問答の答えの録音の部分の名前の頭（会話の側の flowctl.ANSWER_PREFIX と同じ）
ANY = "*"                                         # 分岐の受け皿（ほかのどの答えでも）
SYSTEM_CHOICES = ("わからない", "答えたくない")      # システムがどの質問にも足す答え（CL-1 の連絡帳 2-1。名前の正本はここ）
ANY_CHOICE = "どちらでも"                          # 決まった選択肢（choice）の質問にだけシステムが足す答え：お客様がこちらに任せた
#   （「どちらでも大丈夫です」）。答えとして受ける＝「取れた」（「わからない」「答えたくない」とは違う。断りにもしない。2026-10-17
#   ユーザー：「いつでも」と同じ。CL-1 の連絡帳 4-17。会話の中の名前は any）。はい／いいえの質問には足さない
NONE_CHOICE = "どれも要らない"                      # 決まった選択肢（choice）の質問にだけシステムが足す答え「どれも要らない」：どの選択肢も要らない・どれもしないで
#   ほしい（「連絡自体控えてほしいです」「どちらもお断りします」。CL-1 の連絡帳 4-66。会話の中の名前は none）。✅ 2026-11-02 日本時間4時16分
#   ユーザー：「連絡しないと最終目的を果たせないような流れだったのなら営業拒否になるし、登録など別の手段が目的の営業であれば電話やメールは
#   控えるという意味合いになるんじゃないですか。」⇒ どの質問でも、その申し出を断る答え（`declined_answers` の1か所。会話の側の Question.declined と
#   同じ）。用件を断られたことになるのは、最終目的の申し出の質問（`Question.final_goal`）で目的が果たせなくなったときだけ（会話の側が決め、
#   判定は通話の事実 goal_refused を読む）。ほかの質問では、断ったまま分岐のとおり続ける。
#   「どちらでも」と同じく、事実を伺う質問（asks_fact）とはい／いいえの質問には足さない（はい／いいえは「いいえ」がこの役）
CHOICE_TYPES = ("yes_no", "choice")               # 選択肢で答える型（意味の言葉・曖昧な言葉の意味を持てる）
REPLY_WORDS = ("大丈夫", "結構", "いい", "平気")
#   返事の言葉だけの答え（曖昧な言葉の意味の鍵）。正本は会話の側（CL-1 の flowctl.AMBIGUOUS_WORDS＝受け入れにも断りにも取れる言葉）。
#   会話の側が変えたら、ここも合わせる（2026-10-18 CL-1 の連絡帳 4-20：「はい」「いいえ」を外した＝頭の言葉の確かめのためだけに
#   足していたもの。答えを受け取る決まりは会話の側の1か所 Flow._compare）
VALUE_CHOICE = "答えた"                            # 値を聞く質問の選択肢（値そのものは項目の値に入る）
YES_NO_CHOICES = ("はい", "いいえ")
OUTPUT_MARKS = ("-", "✕")                         # 依頼者へ渡す印の既定（DESIGN.md 7-1b。依頼者ごとの印は出力の型で。⏳ 出力の型を作るとき足す）
TARGET_CLASSES = ("corporate", "individual")      # お客様の区分：法人／個人（Intent.md 6-2・13章。業務ごとに必ず書く）
TARGET_CLASSES_OPEN = ("corporate",)              # いま選べる区分：法人だけ（Intent.md 3-1・18-3）。個人は、13章の確かめが済み、
#                                                   名乗りの法定事項と拒否の即時登録をシステムが強制する形を作ってから開く（設定では開けない）
TARGET_PERSON_KINDS = ("role", "role_and_name", "name", "anyone")
ANYONE = "anyone"                                  # 目的のお客様のいない業務（最初に出たお客様が目的のお客様）
# 役職のみ（決裁権者を含む）／役職（部署）＋名前／名前のみ／その店・会社のお客様なら誰でも（Intent.md 6-2。2026-10-13 ユーザー）
NAME_SOURCES = ("caller", "script")               # 名乗り（自社名・担当者名）／トークスクリプトの文章（商品名・地名など）
READING_BY = readings.BY                          # 読みを決めたもの（正本は core/readings.py）
UNIT_TYPE = "number"                              # 単位を持てる型（金額には補わない。DESIGN.md 6-2 の ⚠️）
# 値を伺う質問の型（日時・数・金額。会話の側の flowctl.ESTIMATE_TYPES＝DATE_TIME_TYPES と NUMBER_TYPES の写し）：値を伺う質問だけの欄（`VALUE_ONLY_KEYS`＝
# 変更の受付の期限・確定した値で登録する）を書ける型（CL-1 の連絡帳 4-115 の (1)・4-116 の (1)。ほかの型の質問は、確定でない値を受けない＝どちらも使わない）
ESTIMATE_TYPES = ("date", "time", "datetime", "number", "amount")
# 値を伺う質問（`ESTIMATE_TYPES`）だけが書ける質問の欄（欄 → 言葉）。ほかの質問に書いたら止める決まりは `_value_fields` の1か所（取込も同じ道）
VALUE_ONLY_KEYS = {"change_by": "変更の受付の期限", "definite_value": "確定した値で登録する"}
# 変更の受付の期限の終わりに書かない言葉（会話の側が「（期限）までにご連絡いただけましたら」と続ける＝CL-1 の phrases.CHANGE_BY。重ねると「前日までまでに」）
CHANGE_BY_TAILS = ("までに", "まで")
QUESTION_KEYS = {"id", "text", "ask_about", "type", "required", "choices", "next", "unit", "meanings", "replies", "asks_fact",
                 "do_words", "away", "intro", "ask_if", "approval", "final_goal", "declines", "tell_words", "ends_on", "unclear_none", "doubt_words",
                 "asks_way", "option_notes",
                 "change_by", "definite_value"}
GOAL_MODES = call_report.GOAL_MODES               # 最終目的の果たし方の名前（正本は会話の報告の形＝事実 goal_refused の mode と同じ）
ASK_IF_KEYS = {"after", "answers", "hedged"}      # 前の答えで伺う質問の条件：前の質問の番号・伺う答えの並び（CL-1 の連絡帳 4-66 の (b)）・
#                                                   その答えが言い切っていないときだけ伺うか（4-71 の (b)）
# 最短で対応できる日の場所（業務設定の質問の文・入る一言に書く。会話の側が電話の日から見た言い方「明日以降」「来週以降」などに置き換える
# ＝CL-1 の flowctl.day_words の1か所。名前の正本は会話の側の flowctl.DAY_SLOT。CL-1 の連絡帳 4-71 の (c)）。日にちを決めるのは
# 業務設定の「最短の日」（`Earliest`）の1か所
DAY_SLOT = "{最短の日}"
# 質問の文の頭に書かない言葉（1か所）：2つ目からの質問の頭には、会話の側がつなぎの言葉か入る一言を付けて流す（CL-1 の flowctl.spoken_question の
# 1か所。作る録音の形の一覧は flowctl.question_forms の1か所）。つなぎの言葉は、次の項目として初めて伺う質問は「続きまして」・もう伺った質問を伺い直す
# とき（戻る一言・「失礼いたしました。」・名乗り直し・尋ねられて答えたあと）と前の答えで伺う質問は「それでは」・言い切っていないときだけ伺う質問は
# 「はい、」＋「それでは」・相づちの「ありがとうございます。」など、続きの電話でお詫びのすぐあとに最初に伺う質問は「確認させていただきたい内容なのですが」
# （CL-1 の連絡帳 4-101〜4-103。✅ 2026-11-06 12時54分30秒・15時20分30秒 ユーザー。⛔ それまでは並びの順番で「それでは」「続きまして」「では」を回していた）。
# 文にも書くと「続きましてそれでは、…」「それではそれでは、…」と重なる（CL-1 の連絡帳 4-72 の (c)。
# ⛔ 見直しで見つけた：「たぶん行けなくなりそうです」に「ありがとうございます。続きましてそれでは、キャンセルに…」）。会話の側に言葉が増えたら、ここに足す
# ⇒ CL-1 の連絡帳 4-75：会話の側は、相づち（「承知しました」「かしこまりました」「失礼いたしました」）も質問の頭に付ける（会話の側の正本は phrases.QUESTION_HEADS＝
# つなぎの言葉の表 LEAD_IN["次の質問"] と相づちの表 ACK から作る1か所。比べる試験は CL-1 の test_question_heads_match_cl2）
# ⇒ CL-1 の連絡帳 4-102・4-103 でも、この一覧の中身は変わらない（会話の側の QUESTION_HEADS が変わっていない。「では」は今は質問の頭に付かないが、会話の側の
# 一覧に残る）。続きの電話の頭「確認させていただきたい内容なのですが」は会話の側の一覧に入っていないので、ここにも入れない（CL-2 の連絡帳 4-234 で CL-1 に伺う）
# ⇒ CL-1 の連絡帳 4-118 の (1)（4-234 の (b) への答え）：会話の側の一覧に、続きの電話の始めの頭「確認させていただきたい内容なのですが」（phrases.QUESTION_RESUME_LEAD）と
# 3回目からの戻る一言「確認の途中でしたのでもう一度お伺いさせていただきますが」（phrases.BACK_TO_MIDWAY）が入った（どちらも会話の側が業務の質問の文のすぐ前に
# 付ける＝業務の文がこの言葉で始まると2回続く）ので、ここにも足した。「ほかに」（phrases.OTHERS_HEAD）は、業務の文がもう「ほかに」で始まっていれば会話の側が
# 足さない（flowctl.others_head）ので重ならない＝足さない（業務の文に「ほかに」と書いてよい）
QUESTION_LEADS = ("それでは", "続きまして", "では", "はい", "ありがとうございます", "承知しました", "かしこまりました", "失礼いたしました",
                  "確認させていただきたい内容なのですが", "確認の途中でしたのでもう一度お伺いさせていただきますが")
# こちらがすることの言葉の形（1か所。会話の側の同じ決まりは CL-1 の meanings.check の kind "do"）：会話の側は「それでは（言葉）ね？」で言うので、
# 言葉は文の終わりまで持ち、終わりは「させていただきます」か「承ります」だけ（「。」は付けない）。へりくだりを重ねない（「差し上げさせ」「お伺いさせ」）。
# ✅ 2026-11-03 ユーザー（(D) の案への答え）＝CL-1 の連絡帳 4-72 の答えの (2)・(g)。⛔ 2026-11-03 まで：言葉の途中で止める前の形（「パンフレットをお送り」）を
# 受け、取込の列の例も「はい：予定通りの方向で進め」だった＝「それでは予定通りの方向で進めね？」になる形。業務設定の確かめ（依頼者が書いた言葉＝止める）と、
# 取込の AI の答えの受け取り（合わない言葉は落とす＝会話の側は意味の言葉で「それでは（意味の言葉）で承りますね？」）と、前に置いた版の読み方（合わない
# 言葉は無いとして読む）が、どれもここを通る（`valid_answer_words`・`_current_shape`）
# あわせて、区切りや問いかけの記号（会話の側の meanings.check と同じ並び）と、頭のつなぎの言葉（`QUESTION_LEADS`。会話の側が「それでは」を頭に付けて
# 言うので「それではそれでは…」と重なる）も使わない（⛔ 2026-11-03 夜の見直し：終わりの形だけを見ていて、依頼者が「それでは…」「…、…」と書いた言葉を
# 通していた。取込の AI の言葉は会話の側で先にふるいにかけられるが、依頼者が書いた言葉はこの1か所しか通らない）
DO_WORDS_END = re.compile(r"(?:させていただきます|承ります)$")
DO_WORDS_DOUBLED = re.compile(r"(?:差し上げ|お伺い|伺わ)させ")
DO_WORDS_MARKS = re.compile(r"[、。，．？?！!「」『』]")


def do_words_ok(words: str) -> bool:
    """こちらがすることの言葉として使える形か（決まりは上の1か所）。"""
    w = words.strip()
    return (bool(DO_WORDS_END.search(w)) and not DO_WORDS_DOUBLED.search(w) and not DO_WORDS_MARKS.search(w)
            and not w.startswith(QUESTION_LEADS))


# いまの話の向きを伺う言葉（Question.doubt_words）の形の決まり（会話の側の取込の確かめ meanings.check の doubt と同じ。CL-1 の連絡帳 4-90）：
# 会話の側が「はい、」＋「（言葉）ということでしょうか？」で言うので、問いかけの終わり・「ということ」「…こと」の言葉は重なる。記号・頭のつなぎの
# 言葉の決まりは、こちらがすることの言葉と同じ
DOUBT_WORDS_BAD_END = re.compile(r"(?:でしょうか|ですか|ますか|よろしい)$")


def doubt_words_ok(words: str) -> bool:
    """いまの話の向きを伺う言葉として使える形か（決まりは上の1か所）。"""
    w = words.strip()
    return (bool(w) and not DO_WORDS_MARKS.search(w) and not w.startswith(QUESTION_LEADS) and not DOUBT_WORDS_BAD_END.search(w)
            and "ということ" not in w and not w.endswith("こと"))


# 選択肢ごとの言葉の種類ごとの形の決まり（1か所。業務設定の確かめ `_answer_words` と、取込の AI の答えの受け取り `valid_answer_words` が使う）
WORD_FORMS = {"do": do_words_ok, "doubt": doubt_words_ok}

# ご連絡の方法の言葉（会話の側の flowctl.WISH_MEANS の、方法 → その方法の言葉＝大文字にそろえて含むかを見る。CL-1 の連絡帳 4-91。⚠️ 会話の側と同じ
# 写し＝CL-1 の比べる試験が文字まで比べる）。業務の「ご案内できる方法」の名前は、どれも方法がちょうど1つに当たるものだけ（`means_in` の1か所。
# 当たらない名前は会話で言わない＝その方法の連絡先を伺えない）
MEANS_WORDS = {"line": ("LINE", "ライン"), "email": ("メール", "MAIL"), "fax": ("ファクス", "ファックス", "FAX"), "post": ("郵送", "郵便"),
               "phone": ("電話", "TEL"),
               "online": ("オンライン", "ZOOM", "ズーム", "TEAMS", "チームズ", "GOOGLE MEET", "WEB会議", "ウェブ会議", "ビデオ通話")}


def means_in(text: str) -> list[str]:
    """名前に言葉の入っているご連絡の方法（MEANS_WORDS の鍵。並びのまま）。会話の側の flowctl.means_in と同じ決まり（NFKC で大文字にそろえて含むか）。"""
    t = unicodedata.normalize("NFKC", text).upper()
    return [k for k, words in MEANS_WORDS.items() if any(w in t for w in words)]


# 業務の録音の部分の名前は、会話の側が、質問の番号やシステムの名前の後ろに「_」と何かを続けて作る（CL-1 の make_ack_clips.script_table：日によって
# 変わる言い方「_d番号」・つなぎの言葉付き「_lead_…」・その答えのあとに伝える一言「_tell番号」・ほかのご希望を伺う一言「_other」・読みの違いごと「_v番号」と、
# その組み合わせ）。⇒ 質問の番号は、ほかの質問の番号やシステムの名前（`RESERVED_IDS`）に「_」を続けた形にはできない（`_derived_part_problems` の1か所。
# 取り違えるため。CL-1 の連絡帳 4-71 の (d)・4-72 の (i)(k) の (4)）。⛔ 2026-11-03 夜の見直し：はじめは「_d番号」「_tell番号」「_other」の3つの形だけを
# 写して見ていた＝会話の側の作り方の全部ではなく、形が増えるたびに漏れる。作り方を写さず「_」の続きで決める
# ⇒ CL-1 の連絡帳 4-100〜4-103（2026-11-06）で、保留で締める一言「_pending」・質問の録音の形「_lead_tsuzukimashite」「_lead_soredeha」「_lead_resume」・
# 入る一言の頭を替えた形「_intro_lead_…」が増えた（作る形の一覧は会話の側の flowctl.question_forms の1か所）。どれも「_」の続きなので、ここは変えない
# ⇒ CL-1 の連絡帳 4-117 の (3)：「（番号）_intro_lead_tsuzukimashite」が業務の録音の表に入り、「（番号）_other_again_midway」は作らなくなった。これも「_」の続きの
# 形の中のことで、業務の録音の部分の名前は今までどおり会話の側の一覧（make_ack_clips.script_table）から作られる＝ここも組を作る道（recordings.complete）も変えない
DERIVED_JOIN = "_"
EARLIEST_KEYS = {"business_days", "closed_weekdays", "closed_on_holidays", "closed_dates"}   # 業務設定の「最短の日」の項目
EARLIEST_MAX_DAYS = 60                            # 「N営業日以降」の N の上限（祝日の暦が届く先＝翌年の分まで、に収まる長さ）
_EARLIEST_SEARCH_DAYS = 366                       # 最短の日を探す先の限り（休みの日が続いても、1年の先までは探さない＝`Earliest.day`）
ANSWER_KEYS = {"name", "text"}                    # 想定問答の1つ：問いの名前・答えの文
# 想定問答の問いの名前に使えない名前（1か所）：AI が流し直しで選ぶ録音の部分の、中の名前と AI に見せる名前の両方（正本は会話の側＝
# CL-1 の flowctl.RESERVED_PART_NAMES＝PART_LABEL の鍵と値）。会話の側は、同じ名前の問いがあると通話を始めない（ValueError）ので、
# 業務設定の確かめで先に断る（⛔ 2026-10-22 の見直し13：前は中の名前だけで、「用件」などが確かめを通り、その版の通話がすべて
# 会話を始めるところで落ちた）。会話の側に名前が増えたら、ここに足す（CL-2.md 4-129）
# ⇒ 2026-10-22 CL-1 の連絡帳 4-28：「答えられない質問」（unknown）は取り下げ、AI が自分の言葉で答える「自分で答える質問」（own）・
# 「答えを持っていない質問」（no_answer）に替わった。⇒ CL-1 の連絡帳 4-30：「受け答え」（talk）が増えた（⛔ 見直し17d：知らせが無く、
# 抜けていた＝「受け答え」と書いた業務設定が通り、その版の通話が始まりで落ちた）。会話の側の一覧と同じかは、会話の側の試験が
# この値を読み込まずに読んで突き合わせる（app/ の試験は試作を読まない＝14章 I-30。CL-2.md 4-144 で CL-1 に頼んだ）。そのため、この値は
# 1つの書き下しの値で持つ（tests/test_business_settings が見張る）
# ⇒ CL-1 の連絡帳 4-65 の (f)：「お呼びした方」（target。受付の方に「どちらの岡田でしょうか？」と聞かれたときに、AI が流し直しで選ぶ）が増えた。
ANSWER_RESERVED = frozenset({"intro", "purpose", "all", "handover", "ai", "target", "resume", "own", "no_answer", "talk",
                             "名乗り", "用件", "最初から", "代わった方へ", "エーアイか聞かれた", "お呼びした方", "質問に戻る",
                             "自分で答える質問", "答えを持っていない質問", "受け答え"})
ANSWER_NAME_MAX = 30                              # 問いの名前の長さの上限（録音のファイル名に入る）
_FILE_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')    # Windows のファイル名に使えない文字


class SettingsInvalid(Exception):
    """業務設定の形が違う・抜けがある（理由は日本語。すべての理由を並べて返す）。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Name:
    text: str
    reading: str | None                          # ひらがな。None＝読みが決まらない（会話の中で確かめる）
    source: str
    reading_by: str | None = "given"
    candidates: tuple[str, ...] = ()             # 読みが決まらないときの、考えられる読み
    likely: str | None = None                    # 読みが決まらないときの、まず言ってみる読み


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    ask_about: str | None        # 聞くこと（聞き返しの言い方に使う。無くてもよい。CL-1 の 4-10）
    type: str
    required: bool
    choices: tuple[str, ...]     # 依頼者が書いた選択肢（値を聞く質問は「答えた」、はい／いいえは「はい」「いいえ」）
    unit: str | None = None      # 数の質問の単位（例：「人」。空なら補わない。CL-1 の 4-14）
    # 選択肢の質問（はい／いいえ・決まった選択肢）の、選択肢ごとの意味の言葉（選択肢 → 言葉。例：「はい」→「ご予約はされているということ」）。
    # 会話が答えを言い直して確かめるときに使う。空でもよい（空なら選択肢の名前で言い直す）。宣言した選択肢にだけ（CL-1 の 4-17）
    meanings: dict[str, str] = field(default_factory=dict)
    # 返事の言葉だけの答え（REPLY_WORDS）が、この質問でどの選択肢の意味になるか（言葉 → 選択肢。例：「パンフレットをお送りしても
    # よろしいでしょうか？」の「結構」→「いいえ」）。決まらない言葉は入れない（会話の中で確かめる。CL-1 の 4-17）
    replies: dict[str, str] = field(default_factory=dict)
    # お客様に事実を伺う質問か（もうしたこと・持っているもの・使っているもの・いまの状態・数など、お客様の希望や選びで変わらないこと。
    # CL-1 の連絡帳 2-1 の版 0.6。✅ 2026-11-01 ユーザー：(a)「OK」）。事実を伺う質問では「どちらでも」は答えにならない（「お任せします」は
    # 聞き直す）ので、返りうる答え（`possible_answers` の1か所）から外す。決めるのは取込のときの AI（CL-1 の `facts.decide`。人は確かめない）。
    # 決まらなければ真（誤った値の重いほう＝事実を伺う質問を希望の質問と取り違えるのを避ける側。CL-1 の決め方と同じ）
    asks_fact: bool = True
    # 選択肢の質問の、選択肢ごとの「こちらがすること（お客様がされること）」の言葉（選択肢 → 言葉。CL-1 の連絡帳 4-65 の (a)。例：
    # 「パンフレットをお送りしてもよろしいでしょうか？」の「はい」→「パンフレットをお送り」・「いいえ」→「パンフレットの送付は控え」）。
    # 事実を伺う質問ではない質問（希望・申し出）で、言い切っていないが向きの決まった最初の答えを「それでは（言葉）させていただきますね？」で
    # 確かめる（会話の側の Flow._hedged_after。✅ 2026-11-01 日本時間20時56分・11-02 1時22分 ユーザー）。決めるのは取込のときの AI
    # （意味の言葉と同じ作り方＝文字の AI 2つ。人は確かめない）。無い選択肢は今までの「念のため（意味の言葉）でよろしかったでしょうか？」。
    # 事実を伺う質問（asks_fact）には付けない（会話の側が使わない＝「それでは（意味の言葉）ということで…させていただいて大丈夫でしょうか？」）。
    # 業務設定（依頼者のトークスクリプト）に書いた選択肢の言葉は、取込の AI の言葉より先に使う（選択肢ごと。CL-1 の連絡帳 4-72 の (d)：「当日の
    # キャンセルはございませんか？」の「はい」＝「予定通りの方向で進め」。✅ 2026-11-03 日本時間8時10分 ユーザー＝業務の言葉で伺う）
    do_words: dict[str, str] = field(default_factory=dict)
    # 業務の目的から遠ざかる向きの選択肢（会話の側の Question.away。CL-1 の連絡帳 4-71 の (a)・4-72 の (a)）。遠ざかる向きの選択肢の、言い切って
    # いない最初の答えは「それでは（こちらがすることの言葉）させていただきますね？」で確かめない（✅ 2026-11-03 ユーザー (2)：「目的から遠ざかる向きの
    # 答えは、その向きで「させていただきますね？」とは言わず…」。会話の側の Flow._hedged_after の1か所）。目的につなげて伺う一言は、業務設定の
    # 言い切っていないときだけ伺う質問で書く（`AskIf.hedged`。⛔ 2026-11-03 日本時間8時10分 ユーザー：「今の一言や案は全くダメです。あなたは
    # チェックしたのですか？いい加減な提案をするな！！」＝取込の AI が作る「目的につなげて伺う言葉」keep_words は取り下げ）。決めるのは取込の
    # ときの AI（CL-1 の meanings.decide_away。どちらか1つの AI でも遠ざかる向きと読むか、読めない選択肢は入れる＝安全側）。事実を伺う質問には
    # 付けない（会話の側が使わない＝do_words と同じ）
    away: tuple[str, ...] = ()
    # この質問に入る一言（依頼者のトークスクリプト。CL-1 の連絡帳 4-65 の (b)。例「それでは予約番号の確認からさせていただきます。」）。前の
    # 質問のあと、つなぎの言葉（それでは・続きまして）の代わりに、質問の録音の頭に1本で入る（文を作るのは会話の側の1か所
    # flowctl.spoken_question。録音を作る道具もそこから読む）。最初の質問（名乗りと一緒に流す）には付けない。空なら今のつなぎの言葉
    intro: str | None = None
    # 前の質問のこの答えのときだけ伺う質問（依頼者のトークスクリプト。CL-1 の連絡帳 4-66 の (b)。会話の側の Question.ask_if）。None＝いつも
    # 伺う。会話の側は、その答えを受け取ったら前の質問のすぐあとに伺い、答えが違えば伺わない。伺わなかった質問は、依頼者へは「伺っていない
    # （対象外）」で返す（答えが取れなかった質問と分ける。決めるのは `judge.not_asked` の1か所）。✅ 2026-11-02 日本時間4時16分 ユーザー：
    # 「相手が資料を見ていなくても業務の進展ができるのであれば、そのまま進めたらいいと思いますし、確認しないと進められないのであれば、
    # こちらから重要な事項を説明して、それの承認をもらう形になるんじゃないでしょうか。」（形だけ作る＝4時36分「今回は形だけ作ります：OK」）
    ask_if: AskIf | None = None
    # 説明した内容のご承認を伺う質問か（はい／いいえの型だけ。説明の文は、この質問に入る一言＝intro）。「いいえ」（ご承認いただけない）の
    # ときは、会話の側がシステムの質問 N1「ご納得いただけなかった点はどの部分になりますでしょうか？」を1回伺い、答えは通話の事実
    # not_agreed（依頼者へ「ご納得いただけなかった点」として返す。CL-1 の連絡帳 4-66 の (b)(c)。4時36分 ユーザー：「(b) いいえ＝「ご納得
    # いただけなかった点はどの部分になりますでしょうか？」のような確認が必要ではないですか？」）
    approval: bool = False
    # 最終目的の申し出か（依頼者のトークスクリプト。CL-1 の連絡帳 4-64 の (e)・4-69 の 4-182 の (e)。会話の側の Question.final_goal）。最終目的の
    # 申し出が断られて業務の目的が果たせなくなったら（果たし方＝`Settings.goal_mode`）、会話の側がその場で締め、通話の事実 goal_refused を
    # 残す。判定はその事実だけを読んで「用件を断られた」とする（決め直さない）。最終目的でない質問の断る答えは、用件を断られたことにしない
    # （✅ 2026-11-01 日本時間20時11分 ユーザー：「登録とかが目的なら、営業を拒否したのではなく、担当者からの折り返しを拒否しただけ」）
    final_goal: bool = False
    # その質問の申し出を断る答え（依頼者の選択肢の名前。会話の側の Question.declines）。「どれも要らない」・ご承認の質問の「いいえ」は書かなくても
    # 断る答え（`declined_answers` の1か所）。⛔ 2026-11-02 まで：業務の決まりの「断りの答え」（business_rules の decline＝質問 → 答え）に
    # 持ち、判定がそれを読んで、最終目的かを問わず「用件を断られた」と決め直していた（同じ中身を会話の側と2か所に持っていた）
    declines: tuple[str, ...] = ()
    # その選択肢の答えを受け取ったあとに伝える一言（選択肢 → 一言。依頼者のトークスクリプト。取込の AI は作らない。会話の側の Question.tell_words）。
    # 言い切っていても、いなくても、受け取ったら1回だけ言ってから次へ進む（会話の側の Flow._advance の1か所。録音の名前は「質問の番号_tell選択肢の番号」）。
    # はい／いいえ・決まった選択肢の質問の、宣言した選択肢にだけ付く（事実を伺う質問にも付く：「当日はお車でお越しでしょうか？」の「はい」→
    # 「○○駐車場をご利用いただけますのでどうぞご利用ください。」）。✅ 2026-11-03 日本時間19時台 ユーザー：「「当日はお車でお越しということで
    # 承ります」や「○○駐車場をご利用いただけますのでどうぞご利用ください」などでしょ！」（CL-1 の連絡帳 4-72 の (i)）
    tell_words: dict[str, str] = field(default_factory=dict)
    # この答えなら、ここで電話を終える答え（依頼者のトークスクリプトの「この答えなら、ここで終える」。答えの名前の並び。会話の側の Question.ends_on＝
    # 中の名前へは会話へ渡すところで置き換える）。この質問よりあとの業務の質問は、この質問の答えが取れて、終える答えでないときだけ伺う（決まりは
    # 会話の側の1か所 flowctl.gate_of。聞き取れない・わからない・答えたくないときは先へ進まない＝締めの前の念のための確認でもう1回伺う）。終える答えなら、
    # 残りは伺わずに今の「全部伺えた」で締める（断りではない）。伺わなかった質問は会話の報告の事実 ended_on で返り、判定は「伺っていない（対象外）」と
    # 数える。書けるのは、はい／いいえ・選択肢の質問の、宣言した選択肢と「どちらでも」「どれも要らない」（返りうるとき）だけで、この欄のある質問は必須の
    # 質問（必須でないと聞き返さない＝先へ進むかを決める答えが1回で「-」になる）。✅ 2026-11-04 日本時間2時10分54秒 管理者「OK」（CL-1 の連絡帳 4-74。
    # ⛔ 前の話し合いの案＝あとの質問すべてに「a 以外なら伺う」を足す形は、聞き取れないまま締める・「どれも要らない」にほかのご希望を伺わない、で取り下げ）
    ends_on: tuple[str, ...] = ()
    # ほかに分からない点が無いかを伺う質問（「ご不明な点はございますでしょうか？」）の、「無い」にあたる選択肢の名前（伺う質問でなければ空）。
    # 取込で文字の AI 2つが質問の文と選択肢から決める（そろわなければ空＝伺う。人は確かめない＝CL-1 の import_ai.decide_unclear）。会話の側の
    # Question.unclear_none：ご承認いただけなかったときの手順で「ほかにご納得いただけない点は…」に「分かった」と答えていただいたら、この質問は
    # 伺わずにこの答えにする（重ねて伺わない＝9-3）。✅ 2026-11-04 日本時間20時27分 管理者「(1)…OK」（CL-1 の連絡帳 4-82）
    unclear_none: str = ""
    # いまの話の向きを伺う言葉（選択肢 → 言葉。会話の側の Question.doubt_words）。判定でも答えが決まらないときに、答えの向きが1つに読めれば
    # 「はい、」＋「（言葉）ということでしょうか？」で確かめる（✅ 2026-11-01 日本時間20時56分・22時31分 ユーザー：「…「パンフレットはお送りしない方が
    # いいということでしょうか？」のような流れが普通じゃないですか？」）。無い選択肢は「念のため（意味の言葉）でよろしいでしょうか？」。決めるのは取込の
    # ときの文字の AI 2つ（CL-1 の import_ai.decide_doubt_words。人は確かめない）。事実を伺う質問には付けない（do_words と同じ。CL-1 の連絡帳 4-90）
    doubt_words: dict[str, str] = field(default_factory=dict)
    # ご案内の方法（お電話・ご訪問・説明会・オンラインの面談 など）でご案内してよいかを伺う質問か（会話の側の Question.asks_way。CL-1 の連絡帳 4-91）。
    # 決めるのは取込のときの文字の AI 2つ（CL-1 の import_ai.decide_ways。そろって真のときだけ真＝人は確かめない）。真の、はい／いいえの質問では、
    # 言い切っていないお断りのあとのほかのご希望（W1）で、業務の「ご案内できる方法」を申し出る（`Settings.offer_ways_of` の1か所で渡す）
    asks_way: bool = False
    # 選択肢ごとの良い点・気をつける点（選択肢 → 文。会話の側の Question.option_notes。CL-1 の連絡帳 4-110）。文は業務の情報と同じく、そのまま
    # 声に出せる文（例：「カード」→「カードですとカード会社のポイントがつきます。」）。会話AIに渡す知っていることの1行になり、どれにするか迷っていると
    # 言い終えたお客様に、選ぶ助けになることを会話AIが自分の言葉で伝える材料（会話の側の Flow._hesitation）。無い質問は「どういった点でお迷い
    # でしょうか？」を1回伺う（共通の一言の録音 hesitate_point）。はい／いいえ・決まった選択肢の質問の、宣言した選択肢にだけ付く（その答えのあとに
    # 伝える一言と同じ確かめ＝`_answer_words`。事実を伺う質問にも付く＝会話の側は業務の質問すべてで選ぶ助けを伝える＝flowctl.helps_choose）。
    # 依頼者のトークスクリプト・資料から取込が入れる（管理者・契約者は書かない）。✅ 2026-11-07 日本時間13時05分06秒・18時03分49秒 ユーザー「(1) ア」
    option_notes: dict[str, str] = field(default_factory=dict)
    # 答えの変更を受け付ける期限（「前日」「3日前」「前週の金曜日」など。言い方はそのまま文に入る。会話の側の Question.change_by。CL-1 の連絡帳
    # 4-115 の (1)）。値を伺う質問（日時・数・金額＝`ESTIMATE_TYPES`）だけ。お客様の答えの値が確定でない（「15日あたりですかね。」）ときに、ひとまずその
    # 値にして「変更があった場合には（期限）までにご連絡いただけましたらご対応可能です。」を添えて伺う（会話の側の Flow._tentative）。空なら無い
    # （確定した値で登録する質問＝`definite_value` でなければ、お客様の言い方のまま「承知しました。それでは15日あたりでお考えということで承っておきますね。」）。
    # ✅ 2026-11-08 日本時間3時48分03秒 ユーザー：「…（～までにご連絡いただけましたら）（～までであれば）ご対応可能です。」～は日付や曜日、前日など。」。
    # 依頼者のトークスクリプトから取込が入れる
    change_by: str = ""
    # 確定した値で登録する質問か（確定日付の登録が要る業務の質問。会話の側の Question.definite_value。CL-1 の連絡帳 4-116 の (1)）。値を伺う質問（日時・数・
    # 金額＝`ESTIMATE_TYPES`）だけ。真なら、確定でない値の答え（「15日あたりですかね。」）を、値の芯でひとまずとする（「はい、それではひとまず15日とさせて
    # いただきますね？」・値は「15日」。期限＝`change_by` があれば変更の受付の一文を添える）。偽なら、お客様の言い方の値（期限があれば「ひとまず15日あたりと
    # させていただきますね？…」・値は「15日あたり」、無ければ承っておく形）。期限が空でも効く（✅ 2026-11-08 日本時間5時33分05秒 ユーザー：「…相手が「あたり」を
    # つけたら、こちらも「あたり」をつけた方がいいでしょう。ただし、確定日付の登録が必要な業務の場合は、現状のままで大丈夫です。」・6時04分57秒「イでOK」）。
    # 依頼者のトークスクリプトから取込が入れる
    definite_value: bool = False


@dataclass(frozen=True)
class AskIf:
    """前の答えで伺う質問の条件（`Question.ask_if`）：前の質問の番号と、伺う答え（その質問の返りうる答え＝`possible_answers` の名前。
    「わからない」「答えたくない」「どちらでも」「どれも要らない」も書ける。会話の側の中の名前 unknown・refused・any・none へは、会話へ
    渡すところで置き換える）。"""
    after: str
    answers: tuple[str, ...]
    # その答えが言い切っていないときだけ伺うか（CL-1 の連絡帳 4-71 の (b)・会話の側の Question.ask_if_hedged）。真なら、前の質問の言い切って
    # いない最初の答え（「曖昧」の印＝項目の hedged）のときだけ伺い、言い切った答えなら伺わない（判定は「伺っていない（対象外）」＝
    # `judge.not_asked` の1か所）。✅ 2026-11-03 ユーザー (3)(4)「OK」：お困りごとに業務の手立てがある答えは、手立てを並べて伺う（「それでは、
    # キャンセルになさいますか？それとも、日時を変更なさいますか？」）・時期の答えが前向きなら「（{最短の日}なら対応できますが、）いつ頃が
    # よろしいでしょうか？」
    hedged: bool = False


@dataclass(frozen=True)
class Earliest:
    """業務設定の「最短の日」（「N営業日以降」。CL-1 の連絡帳 4-71 の (c)）：質問の文・入る一言の `DAY_SLOT` を、電話の日から見た最短で対応
    できる日にする材料。営業日は、休みの曜日（`calling_hours.DAY_KEYS` の名前）・祝日（休みにするか。祝日は暦＝core/calendar.py）・休業日
    （日にち）でない日。✅ 2026-11-03 ユーザー：「業務設定では「3営業日以降」などの設定になるので、それを自動で置き換える仕組みが必要です。」"""
    business_days: int                           # N（0＝当日から。電話の日が休みなら、次の営業日から）
    closed_weekdays: tuple[str, ...] = ("sat", "sun")
    closed_on_holidays: bool = True
    closed_dates: tuple[date, ...] = ()

    def day(self, today: date, is_holiday: Callable[[date], bool]) -> date:
        """電話の日（日本時間の日付）から見た、最短で対応できる日（営業日を数える1か所）：電話の日の次の日から数えて N 番目の営業日
        （N＝0 なら、電話の日から数えて最初の営業日）。祝日が分からない日（暦の範囲の外）は `calendar.CalendarOutOfRange`（判断できない）。
        1年先までに見つからなければ ValueError（業務設定の誤り）。"""
        count, d = 0, today
        if self.business_days == 0 and self._open(d, is_holiday):
            return d
        for _ in range(_EARLIEST_SEARCH_DAYS):
            d += timedelta(days=1)
            if self._open(d, is_holiday):
                count += 1
                if count >= self.business_days:
                    return d
        raise ValueError(f"最短の日が、{today} から1年のあいだに見つかりません（休みの曜日・休業日を確かめてください）")

    def _open(self, d: date, is_holiday: Callable[[date], bool]) -> bool:
        return (calling_hours.DAY_KEYS[d.weekday()] not in self.closed_weekdays and d not in self.closed_dates
                and not (self.closed_on_holidays and is_holiday(d)))


@dataclass(frozen=True)
class TargetNeeds:
    name: bool                                    # 一覧に、目的のお客様の名前（と読み）が要る
    role: bool                                    # 一覧に、目的のお客様の役職が要る

    def missing(self, *, name: str | None, role: str | None) -> tuple[str, str] | None:
        """その行に足りないもの（理由の名前と日本語）。足りていれば None（判断はここだけ）。役職が「ご担当者」だけ（何の担当か無い）の行も
        足りない（`role_problem`）。"""
        if self.name and not name:
            return ("target_name_missing", "目的のお客様の名前がありません（この業務は、目的のお客様を名前で呼びます）")
        if self.role and not role:
            return ("target_role_missing", "目的のお客様の役職がありません（この業務は、目的のお客様を役職で呼びます）")
        if self.role_unclear(role) is not None:
            return ("target_role_unclear", self.role_unclear(role))
        return None

    def role_unclear(self, role: str | None) -> str | None:
        """一覧の行の役職が、この業務でお呼びするのに使えない理由（「ご担当者」だけ＝何の担当か無い。`role_problem`）。この業務が一覧の役職で
        お呼びしないなら None（1か所。取込・版の割り当て・門＝`missing` と、ダッシュボードの行の直し＝core/row_fixes.fix が読む。
        ⛔ 2026-11-07 の見直し：行の直しだけ業務によらず断り、取込は業務が役職で呼ぶときだけ止めていた＝同じ欄に2つの決まり）。"""
        return role_problem(role) if self.role and role else None


# 何の担当かを持たない「担当」だけの役職（敬語の頭・後ろを外した形）。✅ 2026-11-05 日本時間20時29分50秒 ユーザー：「「ご担当の方は
# いらっしゃいますでしょうか？」って、何のご担当者かわからない質問をしても相手はこたえられないでしょ！」・20時42分50秒：役職の欄には
# 役割も書ける（「～のご担当者様」「～をされているご担当者様」「～を受け持ちのご担当者様」）。CL-2 の連絡帳 4-216・Intent.md 7-1
# 後ろに付く敬称・呼び方は、ひらがなと漢字の両方（方・かた・方々・かたがた・者・様・さま・さん・殿・どの・各位・宛・あて・御中・
# 皆様・みなさま。⛔ 2026-11-07 の第三者の見直し (2) の7：「ご担当のかた」「担当者さん」「ご担当者さん」「ご担当の方々」「ご担当者各位」を
# 通していた）。間に入る「の」「へ」も外す（「ご担当の方へ」「担当者様宛」）
_TANTOU_TAIL = r"(?:方々|かたがた|方|かた|者|様|さま|さん|殿|どの|各位|宛て?|あて|御中|皆様|皆さま|みなさま|の|へ)"
_BARE_TANTOU = re.compile(rf"^(?:ご|御)?担当{_TANTOU_TAIL}*$")
# 役職が使えない理由の、役職によらない部分（門の説明 core/gate.py の target_role_unclear と、`role_problem` が読む1か所）
ROLE_UNCLEAR = ("何のご担当か分からない役職は使えません（お客様が答えられないため）。"
                "「〇〇のご担当者様」のように、何の担当かを書いてください")


def role_problem(role: str) -> str | None:
    """目的のお客様の役職として使えない理由（使えれば None。判断はここだけ＝業務設定の役職・お客様リストの行の役職・ダッシュボードで
    直す行の役職＝core/row_fixes.fix）。「ご担当者」「ご担当の方」「担当者様」など、何の担当かが無いものは使えない。"""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", role))
    if _BARE_TANTOU.match(text):
        return f"役職「{role}」：{ROLE_UNCLEAR}"
    return None


@dataclass(frozen=True)
class Settings:
    purpose: str                                  # 業務の目的・達成目標
    target_class: str                             # お客様の区分（corporate＝法人／individual＝個人）
    company: Name                                 # 名乗りの自社名
    person: Name                                  # 名乗りの担当者名
    purpose_line: str | None                      # 用件の一文（空でもよい）
    target_person: dict[str, Any]                 # 目的のお客様 {kind, role}
    busy_line: str | None                         # 「忙しい」と言われたときの引き止めの一言（業務の録音の部分 busy。空なら引き止めず、締める前に「それではまた改めてお掛けしてもよろしいでしょうか？」を伺う（✅ 2026-11-01 日本時間20時56分 ユーザー。CL-1 の連絡帳 4-75））
    voicemail_message: str | None                 # 留守番電話に残す伝言（空なら残さない。CL-1 の 4-22・Intent.md 9-4）
    ai_answer: str | None                         # 「AIですか」と聞かれたときの答え（空なら会話の既定の文。CL-1 の 4-22）
    answers: tuple[tuple[str, str], ...]          # 想定問答：(問いの名前, 答えの文) の並び（CL-1 の 4-26。無くてもよい）
    voice: str                                    # 声の型の名前（CL-1。Intent.md 9-8）
    questions: tuple[Question, ...]
    names: tuple[Name, ...]
    rules: business_rules.Rules | None            # 業務の決まり（版を作るまでは None。版の番号が要るため）
    raw: dict[str, Any]                           # 受けた形のまま（新しい版を作る＝戻すときに使う）
    # 相手の申し出への応じ方と業務の情報（CL-1 の連絡帳 4-28・2-1 の版 0.5。✅ 2026-10-22 ユーザー決定。書かなければ既定＝POLICIES の最初）
    substitute_policy: str = "decision_maker"     # 代わりの方：判断いただけるお立場の方なら話す／目的のお客様だけ／どなたでも
    message_policy: str = "decline"               # 伝言：お礼を言って断り、戻る時刻を聞く／伝言を残す
    message: str | None = None                    # 伝言の文（伝言を残す業務は必ず要る。依頼者の単位の録音＝名乗りの部分 message）
    departed_policy: str = "successor"            # 在籍していない：後任の方をお呼びする／お礼を言って終える
    main_talk_policy: str = "asked"               # 本題の始まり：最初の業務の質問を流したあと／業務の答えを1ついただいたあと（CL-1 の連絡帳 4-114）
    info: tuple[str, ...] = ()                    # 業務の情報（AI が自分の言葉で答える材料。1つに1文。無くてもよい）
    follow_up: bool = False                       # 担当から改めて連絡すると申し出てよいか（既定はいいえ＝約束しない）
    # 依頼者がすること（登録・記録・手配など。CL-1 の連絡帳 4-65 の (c)・instruction.Business.client_does）。事実を伺う質問の、言い切って
    # いない最初の答えの確かめ「それでは（意味の言葉）ということで（依頼者がすること）させていただいて大丈夫でしょうか？」に入る（✅ 2026-11-01
    # 日本時間6時05分・6時27分 ユーザー）。書かなければ None＝会話の側の既定「承っておいて」（phrases.NOTED_DOES。「それでは10名ということで承っておいて
    # 大丈夫でしょうか？」。CL-1 の連絡帳 4-116 の (2)。⛔ 前の既定「記録」＝phrases.CLIENT_DOES は外れた）
    client_does: str | None = None
    # 最終目的の果たし方（`GOAL_MODES`：any＝最終目的のどれか1つで果たせる／all＝全部が要る。会話の側の instruction.Business.goal_mode）。
    # 最終目的の質問が2つ以上の業務だけ書く（必ず書く＝こちらで推し量らない）。1つ以下なら None（どちらでも同じ）
    goal_mode: str | None = None
    # 最短の日（「N営業日以降」）。質問の文か入る一言に `DAY_SLOT` を書いた業務だけ書く（必ず書く）。ほかは None
    earliest: Earliest | None = None
    # ご案内できる方法（お客様に言う名前のまま。例 ("メール", "LINE", "ZOOM")。業務で1つ。無くてもよい。CL-1 の連絡帳 4-91 の (1)＝✅ 2026-11-05
    # 日本時間23時19分06秒 ユーザー「案でOK」）。どの名前も、ご連絡の方法がちょうど1つに当たる（`means_in`）。質問ごとに渡すのは `offer_ways_of`
    offer_ways: tuple[str, ...] = ()
    # 重ねた標準の版（0174。無ければ None＝標準を使わない前の版）と、answers のうち標準から来た問いの名前（業務が上書きしていない・
    # 「使わない」にしていないもの。録音は標準の録音＝`standard_parts`。業務の録音には入れない＝`own_answers`）
    standard_id: UUID | None = None
    standard_answers: frozenset[str] = frozenset()

    def target_needs(self) -> TargetNeeds:
        """目的のお客様を呼ぶのに、発信対象の一覧から何が要るか（決め方はここだけ。取込・版の割り当て・門が使う）。
        名前を使う種類（役職と名前・名前のみ）は名前（と読み）。役職を使う種類（役職のみ・役職と名前）は、業務で役職を1つ
        書いていなければ、一覧の役職。誰でもは、何も要らない（Intent.md 6-2）。"""
        kind, role = self.target_person.get("kind"), self.target_person.get("role")
        return TargetNeeds(name=kind in ("name", "role_and_name"),
                           role=kind in ("role", "role_and_name") and role is None)

    def offer_ways_of(self, question: Question) -> tuple[str, ...]:
        """その質問で申し出るご案内の方法（会話の側の Question.offer_ways に渡すもの。決めるのはここだけ）：ご案内の方法を伺う質問なら業務の
        「ご案内できる方法」、ほかの質問は無し（業務の欄は業務で1つ＝質問ごとに写して持たない）。"""
        return self.offer_ways if question.asks_way else ()


KEYS = {"purpose", "target_class", "caller", "target_person", "busy_line", "voicemail_message", "ai_answer", "answers", "voice",
        "questions", "names", "rules"}
# 相手の申し出への応じ方・本題の始まり（選択肢の正本は call_report.RESPONSE_POLICIES の1か所。最初が既定＝2026-10-22・2026-11-08 ユーザー決定）と、
# 業務の情報（CL-1 の連絡帳 4-28・4-114）。書かなければ既定（ユーザー決定の既定。2026-10-22・2026-11-08 より前に作った版にも無い＝既定として読む）
POLICIES = {f"{k}_policy": v for k, v in call_report.RESPONSE_POLICIES.items()}
RESPONSE_KEYS = {*POLICIES, "message", "info", "follow_up"}
OPTIONAL_KEYS = RESPONSE_KEYS | {"client_does", "goal_mode", "earliest", "answers_off", "offer_ways"}   # 無くてもよい項目（書かなければ既定。client_does＝依頼者がすること。
#                                                                 4-65 の (c)。goal_mode＝最終目的の果たし方。最終目的が2つ以上の業務だけ書く。
#                                                                 earliest＝最短の日。DAY_SLOT を書いた業務だけ書く。4-71 の (c)。answers_off＝使わない
#                                                                 標準の想定問答の問いの名前＝業務の上書き。0174）


# ═════════ システムの標準（設計書 3-8-3 の作る順の (4)・0174） ═════════
# ✅ 2026-11-02 管理者 伺い2「案1でOKだが、システムの想定問答やトークスクリプトにデフォルトの想定をダッシュボードのトークスクリプト設定
# 画面に表示しておき、基本はそれを使う。上書きが必要な業務は、トークスクリプト設定画面か、エクセルなどのシートで上書きし、その分の録音を
# 実施する。」
# 標準を持てる項目と、業務への重ね方（1か所）。answers＝想定問答：問いの名前ごとに、業務が同じ名前で書いた答えが勝つ（業務の録音）・業務が
# 「使わない」（answers_off）にした問いは使わない・残りは標準の答え（標準の録音）。⏳ ほかの項目（トークスクリプトの標準の一文など）は
# CL-1 と決めてから足す（足すときは、この表に重ね方を書き、`check_standard`・`_with_standard` の1か所に足す）。
# 標準を持たない項目：「AIですか」の答え（標準は共通の一言 ai_answer＝CL-1 の連絡帳 4-75）・引き止めの一言（標準を置かない＝9-4）・
# 名乗り・用件・質問・業務の情報（その業務のもの）。
STANDARD_ITEMS: dict[str, str] = {"answers": "by_name"}


@dataclass(frozen=True)
class Standard:
    """標準の版の中身（`check_standard` の答え）。standard_id＝標準の版（無い＝標準を使わない）。"""
    standard_id: UUID | None
    answers: tuple[tuple[str, str], ...] = ()


NO_STANDARD = Standard(None)
LATEST = "latest"   # save_version の standard_id：いまの標準（いちばん新しい標準の版。無ければ標準を使わない）


def check_standard(raw: Any, standard_id: UUID | None = None) -> Standard:
    """標準の版の中身を確かめて読む（ただ1つの確かめ。想定問答の形は業務の想定問答と同じ1か所＝`_answers`）。"""
    problems: list[str] = []
    if not isinstance(raw, dict) or not set(raw) <= set(STANDARD_ITEMS):
        raise SettingsInvalid([f"標準の版は {sorted(STANDARD_ITEMS)} です"])
    answers = _answers(raw.get("answers", []), problems)
    if problems:
        raise SettingsInvalid(problems)
    return Standard(standard_id, answers)


def standard(tx: Tx, standard_id: UUID | None) -> Standard:
    """その標準の版（None＝標準を使わない）。中身は置いた時に確かめ済み。"""
    if standard_id is None:
        return NO_STANDARD
    row = tx.one("SELECT content FROM salescall.standard_versions WHERE standard_id = %s", (standard_id,))
    if row is None:
        raise LookupError(f"その標準の版はありません：{standard_id}")
    return check_standard(row["content"], standard_id)


def current_standard(tx: Tx) -> Standard:
    """いまの標準（いちばん新しい標準の版。無ければ標準を使わない）。新しい業務の版が重ねるのはこれ（1か所）。"""
    row = tx.one("SELECT standard_id FROM salescall.standard_versions ORDER BY standard_no DESC LIMIT 1")
    return standard(tx, None if row is None else row["standard_id"])


def save_standard(tx: Tx, raw: dict[str, Any]) -> UUID:
    """標準の新しい版を作る（確かめに通らなければ何も置かない）。作れるのは管理者だけ（システムの取引で）＝データベースが守る。
    いまの業務の版は、作ったときの標準の版のまま（新しい標準に替えるのは、作る順の (5) の1か所）。"""
    std = check_standard(raw)   # 置くのは確かめた形（前後の空白を除いた文。標準の録音の文と比べる守り＝0174 と同じ文）
    return tx.one("INSERT INTO salescall.standard_versions (content) VALUES (%s) RETURNING standard_id",
                  (json.dumps({"answers": [{"name": a, "text": t} for a, t in std.answers]}, ensure_ascii=False),))["standard_id"]


def _with_standard(answers: tuple[tuple[str, str], ...], off: Any, std: Standard,
                   problems: list[str]) -> tuple[tuple[tuple[str, str], ...], frozenset[str]]:
    """業務の想定問答に標準を重ねる（1か所＝`STANDARD_ITEMS` の answers の重ね方）。返すのは、重ねた想定問答（業務の答えが先、標準の
    答えはそのあと）と、そのうち標準から来た問いの名前。"""
    names = {a for a, _ in std.answers}
    if not isinstance(off, list) or not all(isinstance(x, str) for x in off) or len(set(off)) != len(off):
        problems.append("使わない標準の想定問答（answers_off）は、問いの名前の並び（同じ名前は1回）です")
        off = []
    own = {a for a, _ in answers}
    for x in off:
        if x not in names:
            problems.append(f"使わない標準の想定問答「{x}」は、標準の想定問答にありません（{sorted(names)}）")
        elif x in own:
            problems.append(f"想定問答「{x}」は、業務の答えで上書きしているので、「使わない」にはできません（どちらか1つ）")
    used = tuple((a, t) for a, t in std.answers if a not in own and a not in off)
    return answers + used, frozenset(a for a, _ in used)


def with_answer(raw: dict[str, Any], name: str, text: str) -> dict[str, Any]:
    """業務設定の中身に、想定問答を1つ置く（1か所。依頼者の解決策・取り込み直したときに残す解決策が使う）：同じ名前の業務の答えが
    あれば置き換え、無ければ足す。業務が「使わない」にした標準の問いの名前なら、使わないを外して業務の答えにする（その問いに答える）。"""
    answers = [dict(a) for a in (raw.get("answers") or [])]
    for a in answers:
        if a.get("name") == name:
            a["text"] = text
            break
    else:
        answers.append({"name": name, "text": text})
    off = [n for n in (raw.get("answers_off") or []) if n != name]
    return dict(raw, answers=answers, **({"answers_off": off} if "answers_off" in raw else {}))


def own_answers(s: Settings) -> tuple[tuple[str, str], ...]:
    """その業務の版が、業務の録音で話す想定問答（業務だけの問答と、標準を上書きした問答。標準から来た問答は標準の録音＝`standard_parts`）。
    業務の録音の組の部分（answer_＋問いの名前）は、これだけから作る（1か所）。"""
    return tuple(a for a in s.answers if a[0] not in s.standard_answers)


def standard_parts(s: Settings) -> tuple[str, ...]:
    """その業務設定が、標準の録音から流す部分の名前（名前の順。1か所）。"""
    return tuple(sorted(ANSWER_PART_PREFIX + a for a in s.standard_answers))


def standard_parts_of(tx: Tx, version_id: UUID) -> tuple[str, ...]:
    """置いた版が、標準の録音から流す部分の名前（名前の順。話してよい声・足りない部分が読む＝core/recordings）。版の中身の想定問答・
    使わない標準の問いと、版が指す標準の版を、`check` と同じ1か所（`_answers`・`_with_standard`）で重ねて決める（業務の決まり・読みは
    要らないので、版の全部は読まない）。標準を指さない版・中身の無い版は空。"""
    row = tx.one("SELECT v.standard_id, c.content FROM salescall.business_versions v"
                 " LEFT JOIN salescall.business_contents c ON c.version_id = v.version_id WHERE v.version_id = %s", (version_id,))
    if row is None or row["standard_id"] is None or row["content"] is None:
        return ()
    problems: list[str] = []
    _, used = _with_standard(_answers(row["content"].get("answers", []), problems), row["content"].get("answers_off", []),
                             standard(tx, row["standard_id"]), problems)
    if problems:   # 置いた時に確かめ済み。ここで落ちるのは、表の中身を直に書き換えたとき
        raise SettingsInvalid(problems)
    return tuple(sorted(ANSWER_PART_PREFIX + a for a in used))


def answer_rows(s: Settings, std: Standard) -> list[dict[str, Any]]:
    """画面（トークスクリプト設定画面）に出す想定問答の行（1か所）：標準の問答・業務が上書きした問答・業務だけの問答・業務が使わない
    標準の問答を、どこから来たか（source＝standard／overridden／business／off）つきで並べる（標準の順、そのあと業務だけの問答）。"""
    own = dict(own_answers(s))
    rows = []
    for a, t in std.answers:
        if a in s.standard_answers:
            rows.append({"name": a, "text": t, "source": "standard"})
        elif a in own:
            rows.append({"name": a, "text": own[a], "standard_text": t, "source": "overridden"})
        else:
            rows.append({"name": a, "text": t, "source": "off"})
    std_names = {a for a, _ in std.answers}
    rows += [{"name": a, "text": t, "source": "business"} for a, t in own.items() if a not in std_names]
    return rows


def _text(v: Any, where: str, problems: list[str], *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where} が空です" if v in (None, "") else f"{where} は文字です")
        return None
    return v.strip()


NAME_KEYS = {"text", "reading", "source", "reading_by", "candidates", "likely"}
NAME_BY = tuple(b for b in READING_BY if b not in ("conversation", "recording"))   # 業務設定の中身に書く名前の読みを決めるもの
#   （会話で確かめるのは発信対象の名前。録音で選んだ読みは版の中身に書かず、読むときに `load` が重ねる）


def _kana(v: Any) -> bool:
    return isinstance(v, str) and readings.normalize(v) == v


def _names_entry(x: Any, where: str, problems: list[str], *, ai_reading_needs_person: bool,
                 placed: frozenset[tuple[str, str]] = frozenset()) -> Name | None:
    """名前の一覧の1つ（形の確かめはここだけ）。placed＝前に置いた版の、AI が決めた読みの (表記, 読み)（置いたときに確かめた＝確かめ
    直さない。`check` の説明）。"""
    if not isinstance(x, dict) or set(x) != NAME_KEYS or x.get("source") not in NAME_SOURCES:
        problems.append(f"{where}は {sorted(NAME_KEYS)} です（source は {list(NAME_SOURCES)}）")
        return None
    text = _text(x["text"], f"{where}の表記", problems)
    reading, by, cands, likely = x["reading"], x["reading_by"], x["candidates"], x["likely"]
    if not isinstance(cands, list) or not all(_kana(c) for c in cands) or (likely is not None and not _kana(likely)):
        problems.append(f"{where}の考えられる読み・まず言ってみる読みは、ひらがなです")
        return None
    if reading is None:
        if by is not None:
            problems.append(f"{where}は読みが無いのに、読みを決めたもの（reading_by）があります")
            return None
    elif not _kana(reading) or by not in NAME_BY or cands or likely is not None:
        problems.append(f"{where}の読みは、ひらがなで、読みを決めたもの（{list(NAME_BY)}）と組です（決まった読みには候補を付けません）")
        return None
    if by == "ai" and ai_reading_needs_person and (str(x["text"]).strip(), reading) not in placed:
        problems.append(f"名前「{x['text']}」の読み「{reading}」は AI が決めた読みです。検証モードでは、契約者が確かめてから使います"
                        "（Intent.md 6-8）")
    return None if text is None else Name(text, reading, x["source"], by, tuple(cands), likely)


def _responses(raw: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """相手の申し出への応じ方・伝言の文・業務の情報・改めて連絡すると申し出てよいか（確かめはここだけ。CL-1 の flowctl.check_policies と
    同じ決まり＝選べない値・伝言を残すのに文が無い、を止める）。書かれていない項目は既定（Settings の既定値＝ユーザー決定）。"""
    out: dict[str, Any] = {}
    for key, allowed in POLICIES.items():
        v = raw.get(key, allowed[0])
        if v not in allowed:
            problems.append(f"相手の申し出への応じ方「{key}」に「{v}」は選べません（選べるのは {'・'.join(allowed)}）")
            v = allowed[0]
        out[key] = v
    out["message"] = _text(raw.get("message"), "伝言の文", problems, optional=True)
    if out["message_policy"] == "leave" and out["message"] is None:
        problems.append("伝言を残す業務（message_policy＝leave）には、伝言の文（message）が要ります")
    info = raw.get("info", [])
    if not isinstance(info, list) or not all(isinstance(x, str) and x.strip() for x in info):
        problems.append("業務の情報（info）は、文の並び（1つに1文。空の文は書かない）です")
        info = []
    out["info"] = tuple(x.strip() for x in info)
    follow_up = raw.get("follow_up", False)
    if not isinstance(follow_up, bool):
        problems.append("担当から改めて連絡すると申し出てよいか（follow_up）は true / false です")
        follow_up = False
    out["follow_up"] = follow_up
    return out


def _answers(v: Any, problems: list[str]) -> tuple[tuple[str, str], ...]:
    """想定問答（形の確かめはここだけ）：問いの名前と答えの文の並び。問いの名前は重ならない・録音のファイル名に使える・AI が選ぶ
    録音の部分の名前と同じでない。"""
    if not isinstance(v, list):
        problems.append("想定問答（answers）は並びです（無ければ空の並び）")
        return ()
    out: list[tuple[str, str]] = []
    for n, x in enumerate(v, 1):
        where = f"想定問答 {n} 番目"
        if not isinstance(x, dict) or set(x) != ANSWER_KEYS:
            problems.append(f"{where}は、問いの名前（name）と答えの文（text）です")
            continue
        name = _text(x["name"], f"{where}の問いの名前", problems)
        text = _text(x["text"], f"{where}の答えの文", problems)
        if name is None or text is None:
            continue
        if _FILE_UNSAFE.search(name) or len(name) > ANSWER_NAME_MAX:
            problems.append(f"{where}の問いの名前「{name}」は、録音のファイル名に入るので、{ANSWER_NAME_MAX} 文字までで、"
                            "\\ / : * ? \" < > | を使えません")
        elif name in ANSWER_RESERVED:
            problems.append(f"{where}の問いの名前「{name}」は、AI が選ぶ録音の部分の名前と同じなので使えません（{sorted(ANSWER_RESERVED)}）")
        elif name in {a for a, _ in out}:
            problems.append(f"想定問答に、同じ問いの名前「{name}」が2つあります（問いごとに1つ）")
        else:
            out.append((name, text))
    return tuple(out)


def _caller(v: Any, where: str, names: list[Name], problems: list[str], *, reading_required: bool) -> Name | None:
    """名乗りの名前（表記と読み）。読みの正本は名前の一覧で、ここはその写し（同じでなければ通さない）。"""
    if not isinstance(v, dict) or set(v) != {"text", "reading"}:
        problems.append(f"{where} は表記（text）と読み（reading）です")
        return None
    t = _text(v["text"], f"{where}の表記", problems)
    if t is None:
        return None
    entry = next((n for n in names if n.text == t and n.source == "caller"), None)
    if entry is None:
        problems.append(f"{where}「{t}」が、名前の一覧（名乗り）にありません")
        return None
    if v["reading"] != entry.reading:
        problems.append(f"{where}「{t}」の読みが、名前の一覧の読みと違います")
        return None
    if reading_required and (entry.reading is None or entry.reading_by != "given"):
        problems.append(f"{where}「{t}」の読みを、ひらがなで入れてください（担当者名の読みは必ず入れます。AI では決めません。"
                        "2026-10-16 ユーザー決定）")
        return None
    return entry


def possible_answers(q: Question) -> tuple[str, ...]:
    """その質問で返りうる答え（依頼者の選択肢＋システムが足す答え。決まった選択肢の質問には「どちらでも」も。ただし、お客様に事実を
    伺う質問には足さない＝`Question.asks_fact`。会話の側の同じ1か所は CL-1 の `flowctl.Question.all_conditions`）。"""
    return q.choices + ((ANY_CHOICE, NONE_CHOICE) if q.type == "choice" and not q.asks_fact else ()) + SYSTEM_CHOICES


def declined_answers(q: Question) -> frozenset[str]:
    """その質問で、申し出を断る答え（1か所。会話の側の同じ決まりは CL-1 の `flowctl.Question.declined`）：書いた断る答え（`Question.declines`）・
    返りうるときの「どれも要らない」・ご承認の質問の「いいえ」（ご承認いただけない）。分岐の確かめ（最終目的の質問で断られると、会話の側が
    その場で締めうる＝必須を聞かずに終わってよい道）が読む。⛔ 2026-11-02 まで：業務の決まりの business_rules.decline_answers（断りの答えの
    ある質問だけ）で、判定もこれを読んで「用件を断られた」と決め直していた"""
    possible = possible_answers(q)
    return frozenset(q.declines) | ({NONE_CHOICE} & set(possible)) | (
        {q.choices[1]} if q.approval and len(q.choices) > 1 else set())



def declared_choices(type_name: Any, raw: Any) -> tuple[str, ...] | None:
    """型と書かれた選択肢から、その質問の選択肢（読めなければ None）。決め方はここだけ（確かめ `_choices` と取込が使う）。
    決まった選択肢（choice）だけ依頼者が書く。ほかは型で決まる（書いてあれば同じであること）。"""
    fixed = {"yes_no": YES_NO_CHOICES}.get(type_name, None if type_name == "choice" else (VALUE_CHOICE,))
    if fixed is not None:
        return fixed if raw is None or (isinstance(raw, list) and tuple(raw) == fixed) else None   # 形の違う値でも落ちない（I9）
    if not isinstance(raw, list) or not raw or not all(isinstance(c, str) and c.strip() for c in raw):
        return None
    return tuple(c.strip() for c in raw)


def _choices(raw: Any, type_name: str, where: str, problems: list[str]) -> tuple[str, ...]:
    """型ごとの選択肢（`declared_choices`）。読めなければ問題に出す。"""
    got = declared_choices(type_name, raw)
    if type_name != "choice":
        fixed = YES_NO_CHOICES if type_name == "yes_no" else (VALUE_CHOICE,)
        if got is None:
            problems.append(f"{where}の選択肢は、型 {type_name} では {list(fixed)} に決まっています")
        return fixed
    if got is None:
        problems.append(f"{where}は決まった選択肢（choice）なので、選択肢を1つ以上書いてください")
        return ()
    if len(set(got)) != len(got):
        problems.append(f"{where}の選択肢に、同じ名前が2つあります")
    return got


def valid_answer_words(raw: Any, choices: tuple[str, ...], *, replies: bool, form: str | None = None) -> dict[str, str]:
    """意味の言葉・こちらがすることの言葉・その答えのあとに伝える一言（選択肢 → 言葉）か、曖昧な言葉の意味（言葉 → 選択肢）のうち、使える組だけ
    （決め方はここだけ。確かめと、取込の AI の答えの受け取りが使う）。宣言した選択肢にだけ付く（システムの「わからない」「答えたくない」
    「どちらでも」には付けない）。form＝言葉の種類（`WORD_FORMS` の鍵。こちらがすることの言葉・いまの話の向きを伺う言葉は、その形の決まりも当てる）。"""
    if not isinstance(raw, dict):
        return {}
    if replies:
        return {k: v for k, v in raw.items() if k in REPLY_WORDS and isinstance(v, str) and v in choices}
    return {k: v.strip() for k, v in raw.items() if k in choices and isinstance(v, str) and v.strip()
            and (form is None or WORD_FORMS[form](v))}


def _answer_words(raw: Any, type_name: str, choices: tuple[str, ...], where: str, problems: list[str], *,
                  replies: bool, what: str | None = None, form: str | None = None) -> dict[str, str]:
    """質問の意味の言葉・曖昧な言葉の意味・こちらがすることの言葉（do）・その答えのあとに伝える一言（what）を読む（無ければ空）。合わない組が
    あれば問題に出す。"""
    if raw is None or raw == {}:
        return {}
    what = what or ("曖昧な言葉の意味" if replies else "意味の言葉")
    if type_name not in CHOICE_TYPES:
        problems.append(f"{where}の{what}は、はい／いいえ・決まった選択肢の質問だけが持てます")
        return {}
    good = valid_answer_words(raw, choices, replies=replies, form=form)
    if not isinstance(raw, dict) or len(good) != len(raw):
        problems.append(f"{where}の{what}は、" + (f"言葉（{list(REPLY_WORDS)}）→ 宣言した選択肢" if replies
                                                  else "宣言した選択肢 → 言葉（文字）") + " の表です"
                        + ("（言葉は文の終わりまで書き、終わりは「させていただきます」か「承ります」。「、」「。」などの記号を使わず、"
                           f"頭に「{'」「'.join(QUESTION_LEADS)}」を付けず、「差し上げさせ」「お伺いさせ」のようにへりくだりを重ねない）" if form == "do"
                           else "（「（言葉）ということでしょうか？」の中に入る言葉。「でしょうか」「ですか」などで終えず、「ということ」を使わず、"
                           f"記号を使わず、頭に「{'」「'.join(QUESTION_LEADS)}」を付けない）" if form == "doubt" else ""))
    return good


def check(raw: Any, *, standard: Standard, field_types: frozenset[str], ai_reading_needs_person: bool,
          voice_types: frozenset[str] | None = None, placed_names: Any = ()) -> Settings:
    """業務設定を確かめて読む（ただ1つの確かめ。業務の決まりは版を作るときに business_rules が確かめる）。
    ai_reading_needs_person：AI の読みの案を、契約者が確かめていなければ通さないか（版を作るときは設定 `ai_reading_needs_person`
    の答え。置いた版を読むときは False＝置いた時に確かめ済み）。placed_names＝この中身の元にした版に置いた名前の一覧（版を足す＝`derive`・
    依頼者の解決策＝core/client_solutions）：同じ表記・同じ読みの名前は、置いたときの確かめを信じて、検証モードでも確かめ直さない
    （⛔ 2026-11-07 の第三者の見直し (2) の3：検証モードに切り替えたあと、前からある AI の読みのせいで、解決策・標準の版を足せなかった）。standard＝重ねる標準の版（新しい版はいまの標準＝`current_standard`、
    置いた版は、その版が指す標準の版。重ね方は `_with_standard` の1か所）。合わなければ SettingsInvalid（見つけた理由をすべて並べる）。"""
    problems: list[str] = []
    if not isinstance(raw, dict) or not KEYS <= set(raw) <= KEYS | OPTIONAL_KEYS:
        raise SettingsInvalid([f"業務設定は {sorted(KEYS)} の {len(KEYS)} つと、無くてもよい {sorted(OPTIONAL_KEYS)} です"])
    purpose = _text(raw["purpose"], "業務の目的", problems)
    target_class = raw["target_class"]
    if target_class not in TARGET_CLASSES:
        problems.append(f"お客様の区分（target_class）は {list(TARGET_CLASSES)}（法人／個人）のどちらかです")
    elif target_class not in TARGET_CLASSES_OPEN:
        problems.append("お客様が個人の業務は、まだ選べません（法令の確かめ＝計画書 13章が済み、名乗りの法定事項と拒否の即時登録を"
                        "システムが強制する形を作ってから開きます。いまは法人だけ）")
    names: list[Name] = []
    placed = frozenset((x["text"].strip(), x["reading"]) for x in (placed_names or ())
                       if isinstance(x, dict) and isinstance(x.get("text"), str) and x.get("reading_by") == "ai")
    if not isinstance(raw["names"], list):
        problems.append("名前の一覧（names）は並びです")
    else:
        for n, x in enumerate(raw["names"], 1):
            nm = _names_entry(x, f"名前 {n} 番目", problems, ai_reading_needs_person=ai_reading_needs_person, placed=placed)
            if nm is not None:
                names.append(nm)
        if len({n.text for n in names}) != len(names):   # 同じ表記に読みが2つあると、どちらで言うか決まらない
            problems.append("名前の一覧に、同じ表記が2つあります（表記ごとに1つ。名乗りの名前は source を caller にします）")
    caller = raw["caller"]
    company = person = None
    purpose_line = None
    if not isinstance(caller, dict) or set(caller) != {"company", "person", "purpose_line"}:
        problems.append("名乗り（caller）は、自社名（company）・担当者名（person）・用件の一文（purpose_line）です")
    else:
        company = _caller(caller["company"], "名乗りの自社名", names, problems, reading_required=False)
        person = _caller(caller["person"], "名乗りの担当者名", names, problems, reading_required=True)
        purpose_line = _text(caller["purpose_line"], "用件の一文", problems, optional=True)
    # 目的のお客様：役職は、業務で1つ書く（role）か、書かずに発信対象データの役職の列を使う（None。取込で列があるかを確かめる）。
    # 名前のみ・誰でもは、役職を持たない
    tp = raw["target_person"]
    if not isinstance(tp, dict) or set(tp) != {"kind", "role"} or tp.get("kind") not in TARGET_PERSON_KINDS:
        problems.append(f"目的のお客様は、種類（kind：{list(TARGET_PERSON_KINDS)}）と役職（role）です")
        tp = {"kind": None, "role": None}
    elif tp["kind"] in ("name", "anyone") and tp["role"] is not None:
        problems.append("目的のお客様が「名前のみ」「誰でも」のときは、役職を書きません")
    elif tp["role"] is not None:
        if _text(tp["role"], "目的のお客様の役職", problems) is not None and role_problem(tp["role"]) is not None:
            problems.append(role_problem(tp["role"]))
    busy_line = _text(raw["busy_line"], "忙しいと言われたときの一言", problems, optional=True)
    voicemail_message = _text(raw["voicemail_message"], "留守番電話に残す伝言", problems, optional=True)
    ai_answer = _text(raw["ai_answer"], "「AIですか」と聞かれたときの答え", problems, optional=True)
    answers, from_standard = _with_standard(_answers(raw["answers"], problems), raw.get("answers_off", []), standard, problems)
    responses = _responses(raw, problems)
    client_does = _text(raw.get("client_does"), "依頼者がすること", problems, optional=True)
    voice = _text(raw["voice"], "声の型", problems)
    if voice is not None and voice_types is not None and voice not in voice_types:
        problems.append(f"声の型「{voice}」は、声の型の一覧にありません（{sorted(voice_types)}）")

    questions: list[Question] = []
    nexts: dict[str, dict[str, str]] = {}   # 分岐のシートの表（質問の番号 → 答え → 行き先）。読むのは置き換え `_branch_flow` だけ
    if not isinstance(raw["questions"], list) or not raw["questions"]:
        problems.append("質問が1つもありません")
    else:
        seen: set[str] = set()
        for n, q in enumerate(raw["questions"], 1):
            where = f"質問 {n} 番目"
            if not isinstance(q, dict) or set(q) != QUESTION_KEYS:
                problems.append(f"{where}は {sorted(QUESTION_KEYS)} です")   # 項目の名前は QUESTION_KEYS の1か所から（2026-10-22 の見直し）
                continue
            qid = q["id"]
            if not isinstance(qid, str) or not qid or len(qid) > 32 or not all(c.isascii() and (c.isalnum() or c in "_-")
                                                                              for c in qid) or qid in RESERVED_IDS \
                    or qid.startswith(ANSWER_PART_PREFIX):
                problems.append(f"{where}の番号 {qid!r} は、英数字・「_」「-」の 32 文字までです（{'・'.join(sorted(RESERVED_IDS))} と、"
                                f"「{ANSWER_PART_PREFIX}」で始まる名前は、会話の側で使うので使えません）")
                continue
            where = f"質問 {qid}"
            if qid in seen:
                problems.append(f"{where}が2つあります")
            seen.add(qid)
            if not isinstance(q["type"], str) or q["type"] not in field_types:   # 形の違う値でも落ちずに問題として出す（I9）
                problems.append(f"{where}の型 {q['type']!r} は型の一覧にありません（{sorted(field_types)}）")
                continue
            if not isinstance(q["required"], bool):
                problems.append(f"{where}の必須か（required）は true / false です")
            choices = _choices(q["choices"], q["type"], where, problems)
            for c in choices:
                if c in SYSTEM_CHOICES or c in (ANY_CHOICE, NONE_CHOICE) or c in OUTPUT_MARKS or c in (END, ANY):
                    problems.append(f"{where}の選択肢「{c}」は、システムが足す答え（{list(SYSTEM_CHOICES)}・{ANY_CHOICE}・{NONE_CHOICE}）・"
                                    "出力の印・分岐の印と同じ名前です")
            nxt = q["next"] if q["next"] is not None else {}
            if not isinstance(nxt, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in nxt.items()):
                problems.append(f"{where}の分岐（next）は、答え → 次の質問の番号か {END} の表です")
                nxt = {}
            unit = _text(q["unit"], f"{where}の単位", problems, optional=True)
            meanings = _answer_words(q["meanings"], q["type"], choices, where, problems, replies=False)
            replies = _answer_words(q["replies"], q["type"], choices, where, problems, replies=True)
            if unit is not None and q["type"] != UNIT_TYPE:
                problems.append(f"{where}の単位は、数の型（{UNIT_TYPE}）の質問だけが持てます")
            if not isinstance(q["asks_fact"], bool):
                problems.append(f"{where}の事実を伺う質問か（asks_fact）は true / false です")
            if not isinstance(q["asks_way"], bool):
                problems.append(f"{where}のご案内の方法を伺う質問か（asks_way）は true / false です")
            do_words = _answer_words(q["do_words"], q["type"], choices, where, problems, replies=False, what="こちらがすることの言葉",
                                     form="do")
            doubt_words = _answer_words(q["doubt_words"], q["type"], choices, where, problems, replies=False,
                                        what="いまの話の向きを伺う言葉", form="doubt")
            if doubt_words and q["asks_fact"] is not False:
                problems.append(f"{where}は事実を伺う質問なので、いまの話の向きを伺う言葉（doubt_words）は付けません（会話の側が使わない）")
            tell_words = _answer_words(q["tell_words"], q["type"], choices, where, problems, replies=False,
                                       what="その答えのあとに伝える一言")
            option_notes = _answer_words(q["option_notes"], q["type"], choices, where, problems, replies=False,
                                         what="選択肢ごとの良い点・気をつける点")
            change_by, definite_value = _value_fields(q, where, problems)
            if do_words and q["asks_fact"] is not False:
                problems.append(f"{where}は事実を伺う質問なので、こちらがすることの言葉（do_words）は付けません（会話の側が使わない）")
            away = _away(q["away"], q["type"], choices, q["asks_fact"] is not False, where, problems)
            intro = _text(q["intro"], f"{where}に入る一言", problems, optional=True)
            if intro is not None and n == 1:
                problems.append(f"{where}は最初の質問（名乗りと一緒に流す）なので、入る一言（intro）は付けません")
            lead = next((w for w in QUESTION_LEADS if isinstance(q["text"], str) and q["text"].lstrip().startswith(w)), None)
            if lead is not None and n > 1:
                problems.append(f"{where}の文は「{lead}」で始めません（会話の側が、つなぎの言葉か入る一言を頭に付けて流すので、重なります。"
                                "頭の言葉を変えたいときは、入る一言（intro）に書きます）")
            if not isinstance(q["approval"], bool):
                problems.append(f"{where}のご承認を伺う質問か（approval）は true / false です")
            elif q["approval"] and q["type"] != "yes_no":
                problems.append(f"{where}は、はい／いいえの質問ではないので、ご承認を伺う質問（approval）にできません（ご承認いただけない＝"
                                "「いいえ」のときに、ご納得いただけなかった点を伺うため）")
            declines = _declines(q["declines"], q["type"], choices, where, problems)
            ends_on = _ends_on(q, choices, where, problems)
            unclear_none = _unclear_none(q["unclear_none"], q["type"], choices, where, problems)
            if not isinstance(q["final_goal"], bool):
                problems.append(f"{where}の最終目的の申し出か（final_goal）は true / false です")
                continue
            if declines is None:
                continue
            nexts[qid] = dict(nxt)
            question = Question(qid, _text(q["text"], f"{where}の文", problems) or "",
                                _text(q["ask_about"], f"{where}の聞くこと", problems, optional=True),
                                q["type"], bool(q["required"]), choices, unit, meanings, replies,
                                q["asks_fact"] is not False, do_words, away, intro,
                                _ask_if(q["ask_if"], where, questions, problems), q["approval"] is True,
                                q["final_goal"] is True, declines, tell_words, ends_on, unclear_none, doubt_words, q["asks_way"] is True,
                                option_notes=option_notes, change_by=change_by, definite_value=definite_value)
            questions.append(question)
    # 分岐のシートは、すべての質問が読めたときだけ置き換える（読めなかった質問を除くと「どこからも行きません」などのうその理由が出る）。
    # 置き換えたあとの質問に、書いた欄と同じ確かめ（最終目的の断る答え・必須の数・果たし方）を当てる
    flowed = questions and len(questions) == len(raw["questions"]) and len({q.id for q in questions}) == len(questions)
    if flowed:
        questions = _branch_flow(questions, nexts, problems)
    for question in questions:
        if question.final_goal and not declined_answers(question):
            problems.append(f"質問 {question.id} は最終目的の申し出ですが、断る答えがありません（はい／いいえ・選択肢の質問で、断る答え（declines）を"
                            "書きます。事実を伺わない選択肢の質問は「どれも要らない」も断る答え）")
    if questions and not any(q.required for q in questions):
        problems.append("必須の質問が1つもありません（どんな通話でも「すべて取れた」になる。Intent.md 6-4）")
    elif questions and not any(q.required and q.ask_if is None for q in questions):
        problems.append("いつも伺う必須の質問が1つもありません（必須の質問がすべて前の答えで伺う質問（ask_if）だと、どれも伺わない通話が"
                        "「すべて取れた」になる。Intent.md 6-4）")
    goal_mode = raw.get("goal_mode")
    goals = sum(q.final_goal for q in questions)
    if goals >= 2 and goal_mode not in GOAL_MODES:
        problems.append(f"最終目的の申し出が {goals} つあるので、最終目的の果たし方（goal_mode）を書きます（{list(GOAL_MODES)}：any＝どれか1つで"
                        "果たせる・all＝全部が要る）")
    elif goals < 2 and goal_mode is not None:
        problems.append("最終目的の申し出が1つ以下なので、最終目的の果たし方（goal_mode）は書きません（どちらでも同じ）")
    earliest = _earliest(raw.get("earliest"), problems)
    offer_ways = _offer_ways(raw.get("offer_ways"), problems)
    problems += _day_slot_problems(raw, earliest is not None or raw.get("earliest") is not None)
    problems += _derived_part_problems(raw)
    s = Settings(purpose or "", target_class if target_class in TARGET_CLASSES else "", company or Name("", None, "caller", None), person or Name("", None, "caller", None), purpose_line,
                 dict(tp), busy_line, voicemail_message, ai_answer, answers, voice or "", tuple(questions), tuple(names), None, raw,
                 **responses, client_does=client_does, goal_mode=goal_mode if goal_mode in GOAL_MODES else None, earliest=earliest,
                 offer_ways=offer_ways,
                 standard_id=standard.standard_id, standard_answers=from_standard)
    if flowed:
        try:   # 業務の決まりの確かめ（core/business_rules.py の1か所。版の番号は、版を作るときに付く）
            business_rules.check(UUID(int=0), tuple(q.id for q in questions if q.required), raw["rules"])
        except business_rules.RulesInvalid as e:
            problems.append(f"業務の決まり：{e}")
    if problems:
        raise SettingsInvalid(problems)
    return s


def _value_fields(q: dict[str, Any], where: str, problems: list[str]) -> tuple[str, bool]:
    """値を伺う質問だけの欄（`VALUE_ONLY_KEYS`。確かめはここだけ）：(変更の受付の期限 `Question.change_by`, 確定した値で登録する `Question.definite_value`)。
    - 書けるのは値を伺う質問（`ESTIMATE_TYPES`）だけ（ほかの質問に書いたら止める＝CL-1 の連絡帳 4-115 の (1)・4-116 の (1)。会話の側は使わない）。
      書いたとは、期限が空でない・確定した値で登録するが真。
    - 期限は空（無い）か文字。期限だけを書く（終わりに「まで」「までに」を書かない・「。」を書かない＝会話の側が「（期限）までにご連絡いただけましたら…」と
      続ける）。
    - 確定した値で登録するは true / false（期限が空でも書ける＝期限の無いひとまずの形。4-116 の (1) の ✅ 6時04分57秒「イでOK」）。"""
    raw, definite = q["change_by"], q["definite_value"]
    text = ""
    if raw is not None and raw != "":
        if not isinstance(raw, str) or not raw.strip():
            problems.append(f"{where}の変更の受付の期限（change_by）は文字か空です")
        else:
            text = raw.strip()
            if text.endswith(CHANGE_BY_TAILS) or "。" in text:
                problems.append(f"{where}の変更の受付の期限「{text}」は、「前日」「3日前」のように期限だけを書きます（「まで」「までに」「。」は書きません。"
                                "会話の側が「（期限）までにご連絡いただけましたらご対応可能です。」と続けます）")
    if not isinstance(definite, bool):
        problems.append(f"{where}の確定した値で登録するか（definite_value）は true / false です")
        definite = False
    given = {"change_by": bool(text), "definite_value": definite}   # 書いた欄（鍵は VALUE_ONLY_KEYS と同じ）
    written = [f"{VALUE_ONLY_KEYS[key]}（{key}）" for key, on in given.items() if on]
    if written and q["type"] not in ESTIMATE_TYPES:
        problems.append(f"{where}は値を伺う質問（日時・数・金額）ではないので、{'・'.join(written)}は書けません（会話の側が使いません）")
    return text, definite


def _offer_ways(raw: Any, problems: list[str]) -> tuple[str, ...]:
    """業務の「ご案内できる方法」（無ければ空）。どの名前も、ご連絡の方法がちょうど1つに当たり（`means_in`）、同じ方法を2つ書かない
    （会話の側は当たらない名前・同じ方法の2つ目を言わない＝伺えない方法を書いたままにしない。CL-1 の連絡帳 4-91 の (1)）。"""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        problems.append("ご案内できる方法（offer_ways）は、名前の並びです")
        return ()
    seen: dict[str, str] = {}
    for name in raw:
        found = means_in(name)
        if len(found) != 1:
            problems.append(f"ご案内できる方法「{name}」は、ご連絡の方法（メール・LINE・ファクス・郵送・お電話・オンライン（ZOOM など））の"
                            f"どれか1つが分かる名前にします{'（' + '・'.join(found) + ' の2つ以上に当たります）' if found else ''}")
        elif found[0] in seen:
            problems.append(f"ご案内できる方法「{name}」は「{seen[found[0]]}」と同じ方法です（1つの方法は1つだけ書きます）")
        else:
            seen[found[0]] = name
    return tuple(x.strip() for x in raw)


def _declines(raw: Any, type_name: str, choices: tuple[str, ...], where: str, problems: list[str]) -> tuple[str, ...] | None:
    """断る答え（`Question.declines`）を確かめて読む：その質問の選択肢の名前の並び（重ならない）。はい／いいえ・選択肢の質問だけ。
    読めなければ None（問題を足す。その質問は読めなかったとして、分岐を確かめない＝崩れた断る答えで「必須を聞かずに終わる道」のうその理由を
    出さない。2026-10-17 の見直しと同じ）。"""
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list) or not all(isinstance(a, str) and a.strip() for a in raw) or len(set(raw)) != len(raw):
        problems.append(f"{where}の断る答え（declines）は、選択肢の名前の並びです（重ならない）")
        return None
    if type_name not in CHOICE_TYPES:
        problems.append(f"{where}は選択肢で答える質問ではないので、断る答え（declines）は付けません")
        return None
    unknown = [a for a in raw if a not in choices]
    if unknown:
        problems.append(f"{where}の断る答え {unknown} は、その質問の選択肢にありません（{list(choices)}）")
        return None
    return tuple(raw)


def _ends_on(q: dict[str, Any], choices: tuple[str, ...], where: str, problems: list[str]) -> tuple[str, ...]:
    """ここで終える答え（`Question.ends_on`）を読む（無ければ空）：はい／いいえ・選択肢の質問の、返りうる答えのうちシステムの「わからない」「答えたくない」で
    ないもの（宣言した選択肢と、返りうるときの「どちらでも」「どれも要らない」）の並び（重ならない）。この欄のある質問は必須の質問（確かめはここだけ。
    会話の側の Question.__post_init__ と同じ決まり）。"""
    raw = q["ends_on"]
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw) or len(set(raw)) != len(raw):
        problems.append(f"{where}のここで終える答え（ends_on）は、答えの名前の並びです（重ならない）")
        return ()
    if q["type"] not in CHOICE_TYPES or q["required"] is not True:
        problems.append(f"{where}のここで終える答え（ends_on）は、必須の、はい／いいえ・決まった選択肢の質問だけが持てます（必須でないと聞き返さないので、"
                        "先へ進むかを決める答えが1回で「-」になるため）")
        return ()
    allowed = [a for a in possible_answers(Question(q["id"], "", None, q["type"], True, choices,
                                                    asks_fact=q["asks_fact"] is not False)) if a not in SYSTEM_CHOICES]
    unknown = [a for a in raw if a not in allowed]
    if unknown:
        problems.append(f"{where}のここで終える答え {unknown} は書けません（書けるのは {allowed}。「わからない」「答えたくない」は先へ進むかが決まらない"
                        "答えなので書きません）")
        return ()
    return tuple(raw)


def _unclear_none(raw: Any, type_name: str, choices: tuple[str, ...] | None, where: str, problems: list[str]) -> str:
    """ほかに分からない点が無いかを伺う質問の「無い」にあたる選択肢（`Question.unclear_none`）を読む（無ければ空）：はい／いいえ・決まった選択肢の
    質問の、宣言した選択肢の1つ（確かめはここだけ。会話の側の Question.__post_init__ と同じ決まり）。"""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or type_name not in CHOICE_TYPES or raw not in (choices or ()):
        problems.append(f"{where}のほかに分からない点は無いにあたる答え（unclear_none）は、はい／いいえ・決まった選択肢の質問の、"
                        f"宣言した選択肢の1つです（{list(choices or ())}）")
        return ""
    return raw


def _away(raw_away: Any, type_name: str, choices: tuple[str, ...], asks_fact: bool, where: str, problems: list[str]) -> tuple[str, ...]:
    """目的から遠ざかる向きの選択肢（away）を読む（無ければ空）：その質問の宣言した選択肢の並び（重ならない）。事実を伺う質問ではない選択肢の
    質問だけ。"""
    if raw_away is None or raw_away == []:
        away: tuple[str, ...] = ()
    elif not isinstance(raw_away, list) or not all(isinstance(a, str) for a in raw_away) or len(set(raw_away)) != len(raw_away):
        problems.append(f"{where}の目的から遠ざかる向きの選択肢（away）は、選択肢の名前の並びです（重ならない）")
        away = ()
    else:
        away = tuple(raw_away)
        unknown = [a for a in away if a not in choices]
        if unknown:
            problems.append(f"{where}の目的から遠ざかる向きの選択肢 {unknown} は、その質問の選択肢にありません（{list(choices)}）")
    if away and type_name not in CHOICE_TYPES:
        problems.append(f"{where}の目的から遠ざかる向きの選択肢（away）は、はい／いいえ・決まった選択肢の質問だけが持てます")
    if away and asks_fact:
        problems.append(f"{where}は事実を伺う質問なので、目的から遠ざかる向きの選択肢（away）は付けません（会話の側が使わない）")
    return away


def _earliest(raw: Any, problems: list[str]) -> Earliest | None:
    """業務設定の「最短の日」を読む（無ければ None）：営業日の数 N（0〜`EARLIEST_MAX_DAYS`）・休みの曜日（曜日の名前の並び。7つ全部は
    休めない）・祝日を休みにするか・休業日（"YYYY-MM-DD" の並び）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != EARLIEST_KEYS:
        problems.append(f"最短の日（earliest）は {sorted(EARLIEST_KEYS)} です")
        return None
    n, weekdays, holidays, dates = (raw[k] for k in ("business_days", "closed_weekdays", "closed_on_holidays", "closed_dates"))
    bad = len(problems)
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= EARLIEST_MAX_DAYS:
        problems.append(f"最短の日の営業日の数（business_days）は 0〜{EARLIEST_MAX_DAYS} の数です（0＝当日から）")
    if not isinstance(weekdays, list) or not all(w in calling_hours.DAY_KEYS for w in weekdays) or len(set(weekdays)) != len(weekdays):
        problems.append(f"最短の日の休みの曜日（closed_weekdays）は、{list(calling_hours.DAY_KEYS)} の名前の並びです（重ならない）")
    elif len(weekdays) == len(calling_hours.DAY_KEYS):
        problems.append("最短の日の休みの曜日（closed_weekdays）が7つ全部です（営業日がありません）")
    if not isinstance(holidays, bool):
        problems.append("最短の日の祝日を休みにするか（closed_on_holidays）は true / false です")
    days: list[date] = []
    if not isinstance(dates, list):
        problems.append("最短の日の休業日（closed_dates）は、日にち（YYYY-MM-DD）の並びです")
    else:
        for d in dates:
            try:
                if not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                    raise ValueError(d)
                days.append(date.fromisoformat(d))
            except ValueError:
                problems.append(f"最短の日の休業日 {d!r} は、日にち（YYYY-MM-DD）ではありません")
        if len(set(days)) != len(days):
            problems.append("最短の日の休業日に、同じ日が2つあります")
    if len(problems) > bad:
        return None
    return Earliest(n, tuple(weekdays), holidays, tuple(sorted(days)))


def _day_slot_problems(raw: dict[str, Any], has_earliest: bool) -> list[str]:
    """最短の日の場所（`DAY_SLOT`）の確かめ（1か所）：書けるのは質問の文と入る一言だけ（会話の側が置き換えるのはそこだけ＝ほかに書くと、
    そのまま声になる）。書いた質問があれば最短の日（earliest）が要り（無いと会話の側が通話を始めない＝ValueError）、無ければ最短の日は
    書かない（使われない値）。言い方ごとの録音の部分「質問の番号_d番号」との取り違えは `_derived_part_problems` の1か所。"""
    problems: list[str] = []
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    slotted = [q.get("id") for q in qs if isinstance(q, dict)
               and any(isinstance(q.get(k), str) and DAY_SLOT in q[k] for k in ("text", "intro"))]

    def walk(v: Any, path: str) -> None:
        if isinstance(v, str) and DAY_SLOT in v:
            problems.append(f"{path} に {DAY_SLOT} があります（{DAY_SLOT} を書けるのは、質問の文と入る一言だけです）")
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(k, path)
                walk(x, f"{path} の {k}")
        elif isinstance(v, list):
            for x in v:
                walk(x, path)
    for k, v in raw.items():
        if k == "questions" and isinstance(v, list):
            for n, q in enumerate(v, 1):
                if isinstance(q, dict):
                    walk({x: y for x, y in q.items() if x not in ("text", "intro")}, f"質問 {q.get('id') or n}")
                else:
                    walk(q, f"質問 {n} 番目")
        else:
            walk(v, f"業務設定の {k}")
    if slotted and not has_earliest:
        problems.append(f"質問 {', '.join(map(str, slotted))} に {DAY_SLOT} があるので、最短の日（earliest：「N営業日以降」）を書きます")
    elif has_earliest and not slotted:
        problems.append(f"{DAY_SLOT} を書いた質問が無いので、最短の日（earliest）は書きません（使われない）")
    return problems


def _derived_part_problems(raw: dict[str, Any]) -> list[str]:
    """業務の録音の名前（質問の番号から作る名前）の確かめ（1か所。上の `DERIVED_JOIN`）：
    - 質問の番号が、ほかの質問の番号やシステムの名前に「_」を続けた形でないか（取り違えるため）。
    - 質問の番号から作る業務の録音の名前（質問の番号そのものと、その後ろに「_」と何かを続けた名前＝会話の側の作り方を写さない）が、共通の一言の
      名前（core/recordings.COMMON_PARTS の1か所）と、ちょうど重ならないか。重なると、業務の録音の組を作れない（recordings.create_set が
      共通の一言の名前を業務の組に入れない）。例：質問の番号 callback_email ⇒ 業務の録音 callback_email・callback_email_again が、共通の一言
      callback_email・callback_email_again と同じ名前（⛔ CL-2 の連絡帳 4-290 の (b)：前は確かめず、版を置けても録音の組が作れなかった）。
      断るのは、重なる共通の一言の名前があるときだけ（その名前を理由に書く。共通の一言の名前を、まとめて使えない名前にはしない）。"""
    from salescall.core import recordings   # 共通の一言の名前の1か所（録音の部品が業務設定を読むので、ここで読む＝読み合いにしない）
    qs = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    ids = [q.get("id") for q in qs if isinstance(q, dict) and isinstance(q.get("id"), str)]
    problems = []
    for base in sorted(set(ids) | RESERVED_IDS):
        clash = [x for x in ids if x.startswith(base + DERIVED_JOIN)]
        if clash:
            problems.append(f"質問の番号 {clash} は、{'質問' if base in ids else 'システムの名前'} {base} に「{DERIVED_JOIN}」を続けた形です（会話の側は、"
                            f"録音の部分の名前を {base}_d番号・{base}_tell番号・{base}_other・{base}_v番号 などの形で作るので、取り違えます）")
    for q in ids:
        same = sorted(c for c in recordings.COMMON_PARTS if c == q or c.startswith(q + DERIVED_JOIN))
        if same:
            problems.append(f"質問の番号 {q} から作る業務の録音の名前が、共通の一言の名前 {same} と重なります（業務の録音の組を作れません。"
                            "質問の番号を変えてください）")
    return problems


def _ask_if(raw: Any, where: str, earlier: list[Question], problems: list[str]) -> AskIf | None:
    """前の答えで伺う質問の条件を読む（無ければ None）。前の質問は、この質問より前に並ぶ質問（会話の側は、その答えを受け取ったら前の質問の
    すぐあとに伺う。前に並ぶ質問だけにするので、条件が輪になることも、最初の質問が伺わない質問になることもない）。伺う答えは、前の質問の
    返りうる答え（`possible_answers`）の1つ以上（同じ答えは1回）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != ASK_IF_KEYS:
        problems.append(f"{where}の伺う条件（ask_if）は、前の質問の番号（after）と伺う答えの並び（answers）と言い切っていないときだけ"
                        "伺うか（hedged）か null です")
        return None
    after, answers, hedged = raw["after"], raw["answers"], raw["hedged"]
    if not isinstance(hedged, bool):
        problems.append(f"{where}の伺う条件の言い切っていないときだけ伺うか（hedged）は true / false です")
        return None
    src = next((q for q in earlier if isinstance(after, str) and q.id == after), None)
    if src is None:
        problems.append(f"{where}の伺う条件の前の質問 {after!r} は、この質問より前に並ぶ質問ではありません")
        return None
    if not isinstance(answers, list) or not answers or not all(isinstance(a, str) for a in answers) \
            or len(set(answers)) != len(answers):
        problems.append(f"{where}の伺う条件の答えは、前の質問 {after} の答えの名前の並び（1つ以上・同じ名前は1回）です")
        return None
    unknown = [a for a in answers if a not in possible_answers(src)]
    if unknown:
        problems.append(f"{where}の伺う条件の答え {unknown} は、前の質問 {after} の答えにありません（{list(possible_answers(src))}）")
        return None
    if hedged and src.type not in CHOICE_TYPES:
        # 言い切っていない答えを、言い直さずに受け取ってこの質問で伺うのは、選択肢の質問の答えだけ（会話の側の Flow._hedged_follow_up。
        # 値を伺う型の言い切っていない答えは、値を言い直して確かめる＝この質問が伺われない）
        problems.append(f"{where}の伺う条件の前の質問 {after} は、はい／いいえ・決まった選択肢の質問ではないので、言い切っていないとき"
                        "だけ伺う（hedged）にできません")
        return None
    return AskIf(after, tuple(answers), hedged)


def notices(raw: dict[str, Any]) -> list[str]:
    """確かめに通った業務設定について、登録は止めない知らせ（取込のプレビュー・画面が出す。1か所）：書いてあるが、決まりで使わないもの。
    - 分岐のシートの「わからない」「答えたくない」の行き先（答えが決まらないうちは先へ進まない＝✅ 2026-11-04 日本時間1時25分39秒。
      先へ進むかを決める質問として、聞き返しと締めの前の念のための確認で伺い、それでも決まらなければ締める＝`_branch_flow`）。
    - 目的から遠ざかる向きの選択肢に書いた、こちらがすることの言葉（その向きの言葉では言い直さない＝✅ 2026-11-03「最終目的に繋がるクロージングは
      マイナスの言葉で締めないように」・CL-1 の連絡帳 4-78 の 4-199 の (2) 案B。目的につなげて伺いたいときは「言い切っていないときに伺う質問」に
      書く）は、依頼者が書いた言葉だけを知らせるので、書いた言葉を知っている入口が `unused_do_words` で出す（取込の AI の言葉は知らせない）。"""
    out: list[str] = []
    for q in raw.get("questions") or []:
        nxt = q.get("next") if isinstance(q, dict) else None
        for a in (a for a in SYSTEM_CHOICES if isinstance(nxt, dict) and a in nxt):
            out.append(f"質問 {q['id']} の分岐の「{a}」の行き先は使いません（答えが決まらないうちは先へ進まず、聞き返しと締めの前の念のための"
                       "確認で伺い、それでも決まらなければ締めます。そのときの扱いは「わからない・答えたくない」のシートで決まります）")
    return out


def unused_do_words(q: Question, words: dict[str, str] | None = None) -> list[str]:
    """こちらがすることの言葉のうち、会話が使わない選択肢（目的から遠ざかる向きの選択肢＝その向きの言葉では言い直さない。CL-1 の連絡帳 4-78 の
    4-199 の (2) 案B。会話の側の `Flow._ask_confirm` の頭の `_keep_toward_goal`）。words を渡せば、その言葉の選択肢だけを見る。"""
    return [a for a in (words if words is not None else q.do_words) if a in q.away]


def _branch_flow(questions: list[Question], nexts: dict[str, dict[str, str]], problems: list[str]) -> list[Question]:
    """分岐のシート（質問ごとの「答え → 次の質問の番号か終わり」）を、会話が読む形に置き換える（ただ1つの置き換え。どの入口の業務設定も、
    置いた版を読むときも、`check` がここを通す＝分岐の表しを2つ持たない。会話の側は分岐の表を読まない＝CL-1 の連絡帳 4-78 の 4-203 の (3)）。
    置き換え先は、管理者が決めた形だけ（人に伺わない＝CL-1 の連絡帳 4-78 の (1)・CL-2 の連絡帳 4-206。✅ 2026-11-04 日本時間6時44分 管理者：
    「契約者に決める形じゃなくて、あなたと私がどうするのかもうすでに決めているだろうが」）：
    - 飛ばし（Q の答え a の行き先が、並びの次より後ろの質問）：あいだの質問 Y の伺う条件（`AskIf`）＝Q の、Y を飛ばさず終わりでもない答え
      （宣言した選択肢と「どちらでも」）。「どれも要らない」は入れない（入れると、ほかのご希望を伺わずに次へ進む＝✅ 11-03 20時48分）。
      Y にすでに伺う条件がある（「かつ」）・伺う答えが1つも無いときは、会話の形で表せないので問題にする。
    - 終わり（並びの最後でない質問の答えの行き先が終わり）：どの答えでも（目的から遠ざかる向きでも・「どれも要らない」でも）、ここで終える答え
      （`ends_on`。断りではない終わり＝✅ 11-04 2時10分54秒）。最終目的（`final_goal`・`declines`）は、依頼者が「最終目的」の列で決めたものだけで、
      「→ 終わり」から推し量らない（✅ 11-04 日本時間7時台 管理者：「業務の決め方が「パンフレットか説明会のどちらか1つで目的が果たせる」のに、
      「ご来店のご意向 いいえ 」だけで、どうして締めるんですか？来店は目的ではないという前提ですよね？」＝CL-1 の連絡帳 4-79。最終目的の断る答えに
      「→ 終わり」もあれば両方。会話の側は両方を受けて決まりどおり動く）。先へ進むかを決める質問なので必須にする（会話の側の決まり：必須でないと
      聞き返さず、1回で「-」になる）。
    - 続きの終わり（その質問のどの答えでも終わり）：その質問が伺う条件 (S, B) で伺われる続きの質問なら、S の答え B を終わりとして上のとおりに
      置き換える（続きの質問は、会話の側が伺う条件をたどって伺う＝4-74 の (3)）。B の行き先がどれもその質問でないときは表せない。
    - 「わからない」「答えたくない」の行き先は使わない（答えが決まらないうちは先へ進まない＝✅ 11-04 1時25分39秒）。
    - 前の質問・自分へ戻る行き先は、伺い終えた質問を重ねて伺わない（Intent.md 9-3）ので表せない。どこからも行かない質問も問題にする（Intent.md 6-4）。
    必須の質問を飛ばして終わる道は、もう問題にしない（伺わなかった質問は、判定が「伺っていない（対象外）」と数える＝`judge.not_asked`）。"""
    ids = [q.id for q in questions]
    at = {qid: i for i, qid in enumerate(ids)}
    by_id = {q.id: q for q in questions}
    before = len(problems)
    flow_answers = {q.id: tuple(a for a in possible_answers(q) if a not in SYSTEM_CHOICES) for q in questions}
    for q in questions:
        table = nexts.get(q.id) or {}
        for k, target in table.items():
            if k != ANY and k not in possible_answers(q):
                problems.append(f"質問 {q.id} の分岐の答え「{k}」は、その質問の答えにありません（選択肢＋{list(SYSTEM_CHOICES)}）")
            if target != END and target not in at:
                problems.append(f"質問 {q.id} の分岐先 {target} がありません")
        missing = [a for a in flow_answers[q.id] if a not in table]
        if table and ANY not in table and missing:
            problems.append(f"質問 {q.id} の分岐に、受け皿がありません（{missing} の行き先。「{ANY}」で、ほかのどの答えでもの行き先を書けます）")
    if len(problems) > before:
        return questions
    # 行き先（並びの次＝分岐の無い質問・書いていない答えの受け皿の無いとき。最後の質問の次は終わり）
    def to(q: Question, a: str) -> str:
        table = nexts.get(q.id) or {}
        nxt = ids[at[q.id] + 1] if at[q.id] + 1 < len(ids) else END
        return table.get(a, table.get(ANY, nxt)) if table else nxt
    for q in questions:
        for a in flow_answers[q.id]:
            t = to(q, a)
            if t != END and at[t] <= at[q.id]:
                problems.append(f"質問 {q.id} の答え「{a}」の行き先 {t} は、前の質問（または同じ質問）です。伺い終えた質問は重ねて伺わないので、"
                                "前へ戻る分岐は書けません（Intent.md 9-3・6-4）")
    if len(problems) > before:
        return questions
    reached, todo = {ids[0]}, [ids[0]]
    while todo:
        q = by_id[todo.pop()]
        for a in flow_answers[q.id]:
            t = to(q, a)
            if t != END and t not in reached:
                reached.add(t)
                todo.append(t)
    for qid in ids:
        if qid not in reached:
            problems.append(f"質問 {qid} へは、どこからも行きません")
    if len(problems) > before:
        return questions
    # 終わり：答えの行き先が終わり（並びの最後の質問の答えは、もともと終わり）。どの答えでも終わる質問は「続きの終わり」＝その質問へ来た前の
    # 質問の答えで終わる（伺う条件は、飛ばしから作る分も含めて、この前に1回作って探す）
    def skip_conditions(ends: dict[str, set[str]]) -> tuple[dict[str, AskIf | None], list[str]]:
        """飛ばし → 飛ばされる質問 Y の伺う条件。元の質問は、Y を飛ばす答えを持つ質問のうち Y にいちばん近い質問 Q（それより前で Y を飛ばす
        答えは、Q も飛ばす＝Q を伺わなければ Y も伺わない＝会話の側の決まり：伺う条件の元の質問を伺わなければ、その質問も伺わない）。Q を
        通らずに Y へ来る道（Q より前の質問の答えの行き先が、Q のあとで Y まで）があると、Q の答えで決められないので表せない。伺う答えは、
        Q の答えのうち行き先が Y まで（終わりでない）の答え。ただし、その答えで終わる（ends＝続きの質問のあとで終わる）なら、行き先そのものの
        ときだけ。「どれも要らない」は入れない。"""
        cond = {q.id: q.ask_if for q in questions}
        why: list[str] = []
        for y in ids:
            skippers = [q for q in questions[:at[y]] if any((t := to(q, a)) != END and at[t] > at[y] for a in flow_answers[q.id])]
            if not skippers:
                continue
            q = skippers[-1]
            if cond[y] is not None:
                why.append(f"質問 {y} は、伺う条件（この答えのときだけ伺う）がもうあるので、質問 {q.id} の分岐で飛ばす形を、会話の形で"
                           "表せません（2つの条件を合わせた形は、まだ書けません）")
                continue
            around = [(p.id, a) for p in questions[:at[q.id]] for a in flow_answers[p.id]
                      if (t := to(p, a)) != END and at[q.id] < at[t] <= at[y]]
            if around:
                p, a = around[0]
                why.append(f"質問 {y} は、質問 {q.id} の分岐で飛ばされますが、質問 {p} の答え「{a}」で {q.id} を通らずに来る道があるので、"
                           f"{q.id} の答えで伺うかを決められず、会話の形（この答えのときだけ伺う）で表せません")
                continue
            keep = [a for a in flow_answers[q.id] if a != NONE_CHOICE and (t := to(q, a)) != END and at[t] <= at[y]
                    and (a not in ends[q.id] or t == y)]
            if not keep:
                why.append(f"質問 {y} は、質問 {q.id} のどの答えでも伺わないので、会話の形（この答えのときだけ伺う）で表せません")
                continue
            cond[y] = AskIf(q.id, tuple(keep))
        return cond, why
    direct = {q.id: set() for q in questions}   # 行き先が終わりの答え（選択肢の質問の一部の答え）
    follow = {q.id: set() for q in questions}   # 続きの質問のあとで終わる答え（その質問の伺う条件の答え）
    first_cond, why = skip_conditions(direct)
    problems += why
    for q in questions:
        end_answers = [a for a in flow_answers[q.id] if to(q, a) == END and at[q.id] < len(ids) - 1]
        if not end_answers:
            continue
        if q.type in CHOICE_TYPES and len(end_answers) < len(flow_answers[q.id]):
            direct[q.id] |= set(end_answers)
            continue
        c = first_cond[q.id]
        src = by_id[c.after] if c is not None else None
        if c is None or c.hedged or src.type not in CHOICE_TYPES or any(to(src, b) != q.id for b in c.answers):
            problems.append(f"質問 {q.id} のあとで終わる分岐を、会話の形で表せません（終わりにできるのは、はい／いいえ・選択肢の質問の答えか、"
                            "その答えのときだけ伺う続きの質問（行き先がその質問）のあとだけです）")
            continue
        follow[src.id] |= set(c.answers)
    if len(problems) > before:
        return questions
    ask_if, why = skip_conditions({k: direct[k] | follow[k] for k in direct})
    problems += why
    if len(problems) > before:
        return questions
    out = []
    for q in questions:
        end = [a for a in flow_answers[q.id] if (a in direct[q.id] or a in follow[q.id]) and a not in q.ends_on]
        out.append(replace(q, ask_if=ask_if[q.id], required=q.required or bool(end), ends_on=q.ends_on + tuple(end)))
    return out


def voice_types(tx: Tx) -> frozenset[str]:
    """声の型の一覧の名前（中身は CL-1 の持ち物。DESIGN.md 5-2-2 の順10）。"""
    return frozenset(r["code"] for r in tx.all("SELECT code FROM salescall.voice_types"))


def _field_types(tx: Tx) -> frozenset[str]:
    return frozenset(r["type_name"] for r in tx.all("SELECT type_name FROM salescall.field_types"))


def ai_reading_needs_person(tx: Tx, business_id: UUID) -> bool:
    """その業務で、AI の読みの案を契約者が確かめてから使うか（検証モードのあいだだけ。原則9：違いは契約者の承認の段だけ）。"""
    return approval.human_for_business(tx, business_id)


def _content(s: Settings) -> dict[str, Any]:
    """中身の表に置く形（業務の決まりは business_rules に置くので入れない）。名前の表記は、確かめと同じく前後の空白を除いて置く
    （データベースが、この表記で名前の読みの表に入れる＝0101）。"""
    content = {k: v for k, v in s.raw.items() if k != "rules"}
    return dict(content, names=[dict(x, text=x["text"].strip()) for x in content.get("names", [])])


def save_version(tx: Tx, business_id: UUID, raw: dict[str, Any], *, standard_id: UUID | str | None = LATEST,
                 placed_names: Any = ()) -> UUID:
    """業務設定を確かめて、新しい版を作る（1つの取引。確かめに通らなければ何も置かない）。版の番号を返す。standard_id＝重ねる標準の版
    （LATEST＝いまの標準。取込は契約者がプレビューで見た標準の版を渡す・前の版から作るときは、その版の標準の版を渡す。None＝標準を
    使わない）。版は、その標準の版を指す（0174）。placed_names＝元にした版に置いた名前（`check`。版を足す `derive` が渡す）。
    中身をそのまま写す道（`save_from`）は、ここを通らない（置いたときの確かめを信じる）。"""
    std = current_standard(tx) if standard_id == LATEST else standard(tx, standard_id)
    s = check(raw, standard=std, field_types=_field_types(tx), ai_reading_needs_person=ai_reading_needs_person(tx, business_id),
              voice_types=voice_types(tx), placed_names=placed_names)
    return _place(tx, business_id, s, std)


def _place(tx: Tx, business_id: UUID, s: Settings, std: Standard) -> UUID:
    """確かめた業務設定を、その業務の新しい版として置く（版・質問・業務の決まり・中身。置くのはここだけ＝`save_version` と、中身を
    そのまま写す `save_from`）。"""
    version = tx.one(
        "INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, created_by, standard_id)"   # 版の番号はデータベースが決める（next_seq。0123）
        " SELECT b.tenant_id, b.client_id, b.business_id, salescall.cur_actor(), %s"
        " FROM salescall.businesses b WHERE b.business_id = %s RETURNING version_id", (std.standard_id, business_id))
    if version is None:
        raise LookupError(f"その業務は無い（または、取引の範囲の外）：{business_id}")
    vid = version["version_id"]
    for n, q in enumerate(s.questions, 1):
        tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s)",
                   (vid, q.id, n, q.type, q.required))
    try:
        business_rules.save(tx, vid, s.raw["rules"])
    except business_rules.RulesInvalid as e:
        raise SettingsInvalid([f"業務の決まり：{e}"]) from None
    tx.execute("INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())",
               (vid, json.dumps(_content(s), ensure_ascii=False)))
    return vid


def stored_names(tx: Tx, version_id: UUID) -> tuple[Name, ...]:
    """その版に置いた名前の一覧そのもの（録音で選んだ読みを重ねる前。中身が無ければ空）。録音の確かめ＝core/recordings が、入口の
    読み・考えられる読みを見るのに使う。ふつうは `load` を使う。"""
    row = tx.one("SELECT content FROM salescall.business_contents WHERE version_id = %s", (version_id,))
    if row is None:
        return ()
    problems: list[str] = []
    names = [_names_entry(x, "名前", problems, ai_reading_needs_person=False) for x in row["content"].get("names", [])]
    if problems:   # 置いた時に確かめ済み。ここで落ちるのは、表の中身を直に書き換えたとき
        raise SettingsInvalid(problems)
    return tuple(n for n in names if n is not None)


def load(tx: Tx, version_id: UUID) -> Settings:
    """その版の業務設定（中身・質問・業務の決まりを、置いたときと同じ確かめで読む）。名前の読みは、その版のいまの読み（見方
    current_name_readings の1か所。版の中身に書いた決まった読みに、契約者が録音を聞いて選んだ読みが重なる。Intent.md 6-2）。"""
    raw = _stored(tx, version_id)
    pinned = tx.one("SELECT standard_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))["standard_id"]
    s = check(raw, standard=standard(tx, pinned), field_types=_field_types(tx),   # 置いた時に確かめ済み（読み・声の型）。標準はその版が指す版
              ai_reading_needs_person=False)
    s = replace(s, rules=business_rules.load(tx, version_id))
    now = {r["name_text"]: r for r in tx.all(
        "SELECT name_text, reading, reading_by FROM salescall.current_name_readings WHERE version_id = %s", (version_id,))}
    def heard(n: Name) -> Name:
        r = now.get(n.text)
        return n if r is None else replace(n, reading=r["reading"], reading_by=r["reading_by"], candidates=(), likely=None)
    return replace(s, names=tuple(heard(n) for n in s.names), company=heard(s.company), person=heard(s.person))


def _stored(tx: Tx, version_id: UUID) -> dict[str, Any]:
    """置いた版の業務設定（中身と業務の決まり）を、いまの形で読む（1か所。`load`・`ask_ifs` が読む）。"""
    row = tx.one("SELECT c.content, r.rules FROM salescall.business_contents c JOIN salescall.business_rules r USING (version_id)"
                 " WHERE c.version_id = %s", (version_id,))
    if row is None:
        raise LookupError(f"その版の業務設定の中身がありません：{version_id}")
    return _current_shape(row["content"], row["rules"])


def _current_shape(content: dict[str, Any], rules: Any) -> dict[str, Any]:
    """置いた版の中身と業務の決まりを、いまの業務設定の形にする（1か所。版の中身・業務の決まりの表は書き換えられない＝0023 の frozen ので、
    形に項目が増えたら、読むときにここで前の版を今の形に直す。確かめ `check` は今の形を必ず求めたまま）。返す形は業務設定の形（rules つき）。
    - 事実を伺う質問か（asks_fact。2026-11-01 に足した＝CL-1 の連絡帳 2-1 の版 0.6）が無い前の版の質問は、真（事実を伺う質問）として読む
      （取込で決まらないときと同じ側＝`business_import._decided_facts`。⛔ 見直し28：前は、足す前に置いた版が読めなくなり、その版の
      実施回はかけることも答えを書くこともできなかった）。
    - こちらがすることの言葉（do_words）・入る一言（intro）（2026-11-02 に足した＝CL-1 の連絡帳 4-65 の (a)(b)）が無い前の版の質問は、
      どちらも無い（空の表・None＝今までの確かめの言い方・つなぎの言葉）として読む。
    - 伺う条件（ask_if）・ご承認を伺う質問か（approval）（2026-11-02 に足した＝CL-1 の連絡帳 4-66 の (b)）が無い前の版の質問は、いつも
      伺う・ご承認の質問ではない（None・偽）として読む。同じとき「どれも要らない」が返りうる答えに増えた（4-66 の (a)）ので、その前の版で
      分岐（next）を答えごとに書き、受け皿（*）の無い質問は、「どれも要らない」の行き先を並びの次の質問（最後の質問なら終わり）として
      読む（会話の側は、最終目的の質問でなければ答えとして受け取って続ける＝並びの次。⛔ 足さないと、受け皿の無い分岐の版が読めなくなる）。
    - 最終目的の申し出か（final_goal）・断る答え（declines）（2026-11-02 に業務の決まりの「断りの答え」decline から移した＝CL-1 の連絡帳
      4-69 の 4-182 の (e)）：前の版は、業務の決まりに断りの答えを書いた質問を、最終目的の申し出（断る答えはそのまま）として読み、
      ほかの質問は最終目的でない・断る答え無しとして読む。前の判定は、断りの答えのどれか1つが取れたら「用件を断られた」としたので、
      その質問が2つ以上なら果たし方を all（1つでも断られたら果たせない）として読む（前と同じ結果）。業務の決まりからは decline を外す。
    - 目的から遠ざかる向きの選択肢（away）・伺う条件の言い切っていないときだけ伺うか（ask_if の hedged）（2026-11-03 に足した＝CL-1 の
      連絡帳 4-71・4-72）が無い前の版の質問は、どちらも無い（空・偽＝今までの確かめの言い方・伺う答えならいつも伺う）として読む。最短の日（earliest）は無くてもよい項目なので、前の版はそのまま（無い）。
    - その答えのあとに伝える一言（tell_words。2026-11-03 に足した＝CL-1 の連絡帳 4-72 の (i)）が無い前の版の質問は、無い（空の表）として読む。
    - ここで終える答え（ends_on。2026-11-04 に足した＝CL-1 の連絡帳 4-74）が無い前の版の質問は、無い（空）として読む。
    - ほかに分からない点は無いにあたる答え（unclear_none。2026-11-05 に足した＝CL-1 の連絡帳 4-82）が無い前の版の質問は、無い（空＝伺う）として読む。
    - いまの話の向きを伺う言葉（doubt_words。2026-11-05＝4-90）・ご案内の方法を伺う質問か（asks_way。2026-11-06＝4-91）が無い前の版の質問は、無い（空・偽）として
      読む。ご案内できる方法（offer_ways）は無くてもよい項目なので、前の版はそのまま（無い）。
    - 選択肢ごとの良い点・気をつける点（option_notes。2026-11-07＝CL-1 の連絡帳 4-110）が無い前の版の質問は、無い（空の表＝迷っているお客様には
      「どういった点でお迷いでしょうか？」を1回伺う）として読む。
    - 変更の受付の期限（change_by。2026-11-08＝CL-1 の連絡帳 4-115 の (1)）が無い前の版の質問は、無い（空＝承っておく形）として読む。
    - 確定した値で登録するか（definite_value。2026-11-08＝CL-1 の連絡帳 4-116 の (1)）が無い前の版の質問は、偽（お客様の言い方の値）として読む。
    - 分岐（next）は、読むたびに `check` の置き換え `_branch_flow` が、今の決まりで置き換える（前の版も同じ1か所）。
    - こちらがすることの言葉（do_words）のうち、文の終わりまで持たない前の形の言葉（「パンフレットをお送り」。2026-11-03 に形を決めた＝`do_words_ok`）は、
      無いとして読む（会話の側は「それでは（言葉）ね？」で言うので、そのままでは文にならない。無い選択肢は、会話の側が意味の言葉で
      「それでは（意味の言葉）で承りますね？」と言う）。"""
    rules = rules if isinstance(rules, dict) else {}
    old_decline = rules.get("decline") if isinstance(rules.get("decline"), dict) else {}
    rules = {k: v for k, v in rules.items() if k != "decline"}
    questions = content.get("questions")
    if not isinstance(questions, list):
        return dict(content, rules=rules)
    added = {"asks_fact": True, "do_words": {}, "away": [], "intro": None, "ask_if": None,   # 足した項目と、足す前の版で読む値
             "approval": False, "final_goal": False, "declines": [], "tell_words": {}, "ends_on": [], "unclear_none": "", "option_notes": {},
             "doubt_words": {}, "asks_way": False, "change_by": "", "definite_value": False}
    ids = [q.get("id") if isinstance(q, dict) else None for q in questions]

    def shaped(i: int, q: Any) -> Any:
        if not isinstance(q, dict):
            return q
        out = {**{k: v for k, v in added.items() if k not in q}, **q}
        if isinstance(out.get("do_words"), dict):   # 文の終わりまで持たない前の形の言葉は無いとして読む（`do_words_ok` の1か所）
            out["do_words"] = {k: v for k, v in out["do_words"].items() if not isinstance(v, str) or do_words_ok(v)}
        if isinstance(out.get("ask_if"), dict) and "hedged" not in out["ask_if"]:
            out["ask_if"] = {**out["ask_if"], "hedged": False}
        if "final_goal" not in q and out.get("id") in old_decline:
            out["final_goal"], out["declines"] = True, list(old_decline[out["id"]])
        nxt = out.get("next")
        if "ask_if" not in q and isinstance(nxt, dict) and nxt and ANY not in nxt and NONE_CHOICE not in nxt \
                and out.get("type") == "choice" and out.get("asks_fact") is False:
            out["next"] = {**nxt, NONE_CHOICE: ids[i + 1] if i + 1 < len(ids) else END}
        return out
    out = dict(content, questions=[shaped(i, q) for i, q in enumerate(questions)], rules=rules)
    if "goal_mode" not in content and len(old_decline) >= 2:
        out["goal_mode"] = "all"
    return out


def ask_ifs(tx: Tx, version_id: UUID) -> dict[str, AskIf]:
    """その版の、前の答えで伺う質問（質問の番号 → 条件）。判定の入力（judge.JudgeInput.ask_if）が読む1か所。中身は置いた時に確かめ済み。"""
    return {q["id"]: AskIf(q["ask_if"]["after"], tuple(q["ask_if"]["answers"]), q["ask_if"]["hedged"])
            for q in _stored(tx, version_id)["questions"] if q["ask_if"] is not None}


def day_of_call(tx: Tx, version_id: UUID, at: datetime) -> dict[str, date | None]:
    """その版の電話の、最短で対応できる日と電話の日（1か所。会話の側の Flow(earliest_day=…, today=…) にそのまま渡す形＝CL-1 の連絡帳 4-71 の
    (c)）。電話の日は、電話をかける時刻 at の日本時間の日付（`calling_hours.local` の1か所）。最短の日を書いていない業務は earliest_day が
    None（その業務の質問には `DAY_SLOT` が無い＝業務設定の確かめ）。営業日の数え方は `Earliest.day` の1か所、祝日は暦（core/calendar.py の
    `holiday_checker`）。暦の範囲の外の日は `calendar.CalendarOutOfRange`（判断できない＝その電話は始めない）。"""
    today = calling_hours.local(at).date()
    e = load(tx, version_id).earliest   # 置いた時と同じ確かめで読む（読み方は1か所）
    return {"earliest_day": None if e is None else e.day(today, calendar.holiday_checker(tx)), "today": today}


def restore(tx: Tx, version_id: UUID) -> UUID:
    """前の版の中身から、新しい版を作る（以前の版へ戻す。Intent.md 5-3 の (11)。版は変えないので、戻すのも新しい版）。"""
    v = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))
    if v is None:
        raise LookupError(f"その版は無い（または、取引の範囲の外）：{version_id}")
    return save_from(tx, v["business_id"], version_id)


def save_from(tx: Tx, business_id: UUID, version_id: UUID) -> UUID:
    """いまある版の写しを、その業務の新しい版として作る（中身をそのまま写す道の1か所：以前の版へ戻す・手で前の版に戻す＝`assign`・業務を
    複製する）。中身は `load` と同じく置いたときの確かめを信じて読み、確かめ直さない（⛔ 2026-11-07 の第三者の見直し (2) の3：前は版を
    作るのと同じ確かめ＝`save_version` を通したので、検証モードに切り替えたあと、前からある AI の読みのせいで前の版へ戻せなかった）。
    標準も、その版が指す標準の版のまま（替えるのは (5) の1か所）。写しの元を残し（version_copies＝版の元の1か所。0197）、契約者が録音を
    聞いて選んだ名前の読みも引き継ぐ（読みは版ごとの表にあり、版の中身には入っていない。⚠️ 前は中身だけを写したので、戻した版・複製した
    版では読みが決まらない名前に戻り、契約者が選び直すまで、その名前を話す録音が使えなかった＝見直し8）。"""
    old = load(tx, version_id)
    new = _place(tx, business_id, old, standard(tx, old.standard_id))
    tx.execute("INSERT INTO salescall.version_copies (tenant_id, client_id, version_id, copied_from, recorded_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, salescall.cur_actor())", (new, version_id))
    from salescall.core import recordings   # 録音の部品が業務設定を読むので、ここで読む（読み合いにしない）
    recordings.carry_readings(tx, version_id, new)
    recordings.carry(tx, new)   # 中身が同じなので、元の版の録音をそのまま引き継ぐ（電話を止めない。0194・計画書 6-1 (4)）
    return new


def version_origins(tx: Tx, version_id: UUID) -> list[UUID]:
    """その版の元をたどった版（近い順。版の元＝つながりの前の版か、写しの元。たどるのはデータベースの関数 salescall.version_origins の
    1か所＝0197）。録音の引き継ぎ（core/recordings）が読む。"""
    return [r["version_id"] for r in tx.all("SELECT version_id FROM salescall.version_origins(%s) ORDER BY depth", (version_id,))]


def target_of(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """その行で、目的のお客様をどう呼ぶか（会話が読む。1か所）：種類・役職（業務で1つ書いていればそれ、無ければ一覧の役職）・
    目的のお客様の名前とお客様の会社名（どちらも {text, reading, reading_by, candidates, likely}）と、会話の「目的の人」の形
    （target：{kind＝法人／個人, name, reading, role}。誰でもは None）。いまの版の業務設定で決まる。
    名前の読みは、会話の中でお客様に確かめた読みがあれば、それを使う（readings.with_confirmed）。読みが決まらなければ reading は
    空で、candidates・likely を持つ（会話の中で確かめる。CL-1 の flowctl.reading_question）。
    お呼びする方の値は、その通話の担当の期の値（データベースの1か所 salescall.task_call_values：予約した通話の期・予約の前は予定の期の、その期の
    終わりの直前までに効いていた値。0210）：前の担当の方への約束・お詫び・番号の聞き直しの電話では、前の担当の方のお名前でお呼びする（⛔ 0209 まで：
    いまの値＝新しい担当の方のお名前でお呼びした＝見直し J-1）。録音の名前の部分（core/recordings.name_needs）・門の材料も同じ1か所を読む。"""
    t = tx.one("SELECT t.*, salescall.run_current_version(t.run_id) AS version_id, salescall.task_call_period(t.task_id) AS period"
               " FROM salescall.task_call_values(%s) t", (task_id,))
    if t is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    # 会話の中で確かめた読みは、その名前を直したより後に確かめたものだけ（直す前の名前の読みを、直したあとの名前に付けない）。お呼びする方の
    # 名前の読みは、同じ担当の期の通話で確かめたものだけ（ほかの担当の方の通話で確かめた読みを付けない。会社名は、担当の方によらない）
    said = {r["which"]: r["reading"] for r in tx.all(
        "SELECT DISTINCT ON (which) which, reading FROM salescall.task_confirmed_readings c"
        " JOIN salescall.attempts a ON a.attempt_id = c.attempt_id WHERE c.task_id = %s"
        "   AND c.confirmed_at > coalesce(CASE c.which WHEN 'source' THEN %s::timestamptz ELSE %s::timestamptz END, '-infinity')"
        "   AND (c.which = 'source' OR a.person_since IS NOT DISTINCT FROM %s)"
        " ORDER BY which, confirm_id DESC", (task_id, t["source_name_fixed_at"], t["target_name_fixed_at"], t["period"]))}

    def name(which: str) -> dict[str, Any] | None:
        p = f"{which}_name"
        if t[p] is None:
            return None
        r = readings.Reading(t[f"{p}_reading"], t[f"{p}_reading_by"], tuple(t[f"{p}_reading_candidates"]),
                             t[f"{p}_reading_likely"])
        return {"text": t[p], **readings.with_confirmed(r, said.get(which)).as_json()}
    s = load(tx, t["version_id"])
    kind, role = s.target_person["kind"], s.target_person["role"]
    uses_name = kind in ("name", "role_and_name")
    uses_role = kind in ("role", "role_and_name")
    person = name("target") if uses_name else None
    role = (role or t["target_role"]) if uses_role else None
    # 会話の「目的の人」の形（CL-1 の 4-22・phase0 の instruction.Target：区分・名前・読み・部署と役職）。誰でもは None（最初に出た人が
    # 目的の人）。読みが決まらない名前は、まず言ってみる読み（確かめは会話の中で＝candidates・likely）
    # フルネームと部署（CL-1 の連絡帳 4-65 の (d)・instruction.Target の full_name・full_reading・department）：名前で呼ぶ業務の行で、一覧に
    # あるときだけ。ふだんはお名字で呼び、受付の方に「お名前だけではどの方か分からない」と言われたときだけ使う（会話の側の Target.full_call）。
    # フルネームの読みが決まらなければ、まず言ってみる読み（お名字と同じ。それも無ければ、フルネームではお呼びし直さない＝空）。部署は、
    # 呼ぶ一言の役職に入っていないものだけ（入っていれば同じことを2度言わない＝会話の側も同じ判断をする）
    full = name("target_full") if uses_name else None
    full_reading = (full["reading"] or full["likely"] or "") if full else ""
    department = t["target_department"] if uses_name and t["target_department"] and t["target_department"] not in (role or "") \
        else None
    target = None if kind == ANYONE else {
        "kind": "法人" if s.target_class == "corporate" else "個人",
        "name": person["text"] if person else "",
        "reading": (person["reading"] or person["likely"] or "") if person else "",
        "role": role or "",
        "full_name": full["text"] if full and full_reading else "",
        "full_reading": full_reading if full else "",
        "department": department or ""}
    return {"kind": kind, "role": role, "person": person, "company": name("source"), "target": target}


WHO_UNCLEAR_FIELDS = ("full_name", "department")   # 「お名前だけではどの方か分からなかった」行で、依頼者に埋めていただく欄（4-65 の (g)）


def who_unclear_blanks(tx: Tx, task_id: UUID) -> tuple[str, ...]:
    """「お名前だけではどの方か分からなかった」（繋がらなかった理由 who_unclear）で依頼者に返す行の、埋めていただく欄（1か所。CL-1 の連絡帳
    4-65 の (g)・✅ 2026-11-02 日本時間1時54分 ユーザー：「どちらも分からないときは、依頼者に「お名前だけではどの方か分からなかった」と返し、
    フルネームや部署を足していただきます。」「一覧の入れるべき空欄部分が、その行だけ赤背景になると分かりやすいですね。」）：フルネーム・
    部署のうち、その行の一覧で空いている欄（WHO_UNCLEAR_FIELDS の順）。両方あってもまた分からないと言われた行（お呼びし直した＝recalled）は
    空（埋める欄は無い。依頼者には同じく「分からなかった」と返す）。見せ方（赤い背景など）は CL-3。"""
    t = tx.one("SELECT target_full_name, target_department FROM salescall.tasks_now WHERE task_id = %s", (task_id,))
    if t is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    have = {"full_name": t["target_full_name"], "department": t["target_department"]}
    return tuple(f for f in WHO_UNCLEAR_FIELDS if not have[f])


def anyone_target(tx: Tx, version_id: UUID) -> bool:
    """その版の業務が「誰でも」（目的のお客様のいない業務）か（判定の入力＝judge.JudgeInput.anyone_target。読むのはここだけ）。
    「誰でも」の業務は、お客様と話した通話が目的のお客様に繋がった通話（見直し18b の (3)）。中身は置いた時に確かめ済み。"""
    row = tx.one("SELECT content -> 'target_person' ->> 'kind' AS kind FROM salescall.business_contents WHERE version_id = %s",
                 (version_id,))
    if row is None:
        raise LookupError(f"その版の業務設定の中身がありません：{version_id}")
    return row["kind"] == ANYONE


def _open_tasks_missing(tx: Tx, run_id: UUID, needs: TargetNeeds) -> int:
    """その実施回の、まだ終えていない行のうち、目的のお客様を呼ぶ材料が足りない行の数。"""
    rows = tx.all("SELECT t.target_name, t.target_role FROM salescall.tasks_now t WHERE t.run_id = %s"
                  " AND salescall.task_open(t.task_id)",   # まだ終えていない行（0192 の1か所）
                  (run_id,))
    return sum(1 for r in rows if needs.missing(name=r["target_name"], role=r["target_role"]) is not None)


def assign_problems(tx: Tx, run_id: UUID, version_id: UUID) -> list[str]:
    """その実施回にその版を割り当てられない理由（割り当てられれば空。決めるのはここだけ＝`assign` と、版を自動で替える `adopt_ready`）。
    中身・業務の決まりが確かめに通らない版は、読むとき（`load`）に SettingsInvalid。"""
    s = load(tx, version_id)                         # 中身・業務の決まりが確かめに通る版だけ
    missing = _open_tasks_missing(tx, run_id, s.target_needs())
    if not missing:
        return []
    return [f"この版は、目的のお客様を{'名前' if s.target_needs().name else '役職'}で呼びますが、"
            f"この実施回のまだ終えていない行のうち {missing} 行に、その列がありません。"
            "この版は、その列のある一覧を取り込んだ新しい実施回に割り当ててください（取り込んだ行は変えないため、"
            "同じ実施回に取り込み直しても足せません）"]


# ── 版を足して、そろったら自動で替える（1か所。DESIGN.md 3-8-3 の (5)・0183）
#    ✅ 2026-11-02 管理者：(h) の流れ「新しい版の録音が承認されるまでは、今の版のまま電話を続けます。承認されたら、その業務の実施回を
#    自動で新しい版に替えます。電話は止めません。」
SUCCESSION_CAUSES = {"standard": "標準の想定問答が新しくなった", "solution": "依頼者の解決策を想定問答に足した",
                     "override": "契約者が上書きを変えた"}   # 0183 の CHECK と同じ
KEEP = "keep"   # derive の standard：元の版が指す標準の版のまま


def successor_tip(tx: Tx, version_id: UUID) -> UUID:
    """その版から、版のつながり（version_successions）をたどった、いちばん後の版（後の版が無ければ、その版）。「先」を決めるのは
    データベースの関数 salescall.version_tip の1か所（0197。⛔ 前は同じ再帰の問い合わせが、ここ・`adopt_ready`・データベースの
    client_solution_line_guard の3か所に写されていた）。"""
    return tx.one("SELECT salescall.version_tip(%s) AS v", (version_id,))["v"]


def derive(tx: Tx, version_id: UUID, change: Callable[[dict[str, Any]], dict[str, Any]], *, cause: str, note: str,
           standard_id: UUID | str | None = KEEP) -> UUID:
    """今の版から、変えたところだけ違う新しい版を作る（1か所。きっかけは SUCCESSION_CAUSES）。元にするのは、その版のつながりの
    いちばん後の版（`successor_tip`。まだ割り当てていない後の版の変更を失わない＝つながりは1本の列。データベースも、1つの版の後の版を
    1つにする）。change＝元の版の中身（写し）を受けて、新しい版の中身を返す。standard_id＝重ねる標準の版（KEEP＝元の版のまま・LATEST＝
    いまの標準）。確かめは新しい版を作るのと同じ `save_version`（通らなければ SettingsInvalid。何も書かない）。名前の読みは引き継ぐ。
    新しい版を返す。実施回に割り当てるのは `adopt_ready`（そろってから）。"""
    if cause not in SUCCESSION_CAUSES:
        raise ValueError(f"版を足すきっかけは {list(SUCCESSION_CAUSES)} のどれかです（{cause!r}）")
    if not note.strip():
        raise ValueError("何を変えたかを書いてください")
    base = successor_tip(tx, version_id)
    old = load(tx, base)
    business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (base,))["business_id"]
    new = save_version(tx, business, change(copy.deepcopy(old.raw)),   # 名前は、元の版に置いたものを確かめ直さない（`check`）
                       standard_id=old.standard_id if standard_id == KEEP else standard_id, placed_names=old.raw.get("names"))
    from salescall.core import recordings   # 録音の部品が業務設定を読むので、ここで読む（読み合いにしない）
    recordings.carry_readings(tx, base, new)
    tx.execute("INSERT INTO salescall.version_successions (tenant_id, client_id, business_id, from_version, to_version, cause, note,"
               "                                         recorded_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, salescall.cur_actor())",
               (business, base, new, cause, note.strip()))
    # 変わっていない部分は元の版の録音を引き継ぐ（写しと同じ1か所。変わった部分だけ録音していただく＝契約者に知らせる。0194・計画書 6-1 (4)）
    recordings.carry(tx, new)
    return new


def _versions_in_use(tx: Tx, business_id: UUID | None = None) -> list[dict[str, Any]]:
    """まだ終えていない、版のある実施回と、その今の版（取引の依頼者の範囲。business_id＝その業務の実施回だけ）。「使っている版」・
    「まだ終えていない実施回」を決めるのはここだけ（版を足して自動で替える仕事・依頼者の解決策を足す流れ＝client_solutions._lines・
    取り込み直しで入れない解決策＝client_solutions.held_back・手で割り当てるときに流れを分けるか＝`assign`）。終えた実施回＝行がすべて
    終わった実施回（行の終わりは salescall.task_open の1か所＝0192）。行が0の実施回（作ったばかり・まだ取り込んでいない）は、終えていない
    （⛔ 2026-11-07 のつなぎ目の見直し (D)：前は「まだ終えていない行のある実施回」で、行の無い実施回で外した解決策を取り込み直しで入れた）。"""
    return tx.all(   # まだ終えていない実施回かは、データベースの1か所 salescall.run_in_use（0204。手の割り当ての守りも読む）
        "SELECT r.run_id, salescall.run_current_version(r.run_id) AS version_id FROM salescall.runs r"
        " WHERE salescall.run_in_use(r.run_id) AND (%s::uuid IS NULL OR r.business_id = %s)"
        " ORDER BY r.run_id", (business_id, business_id))


def in_use_versions(tx: Tx, business_id: UUID | None = None) -> list[UUID]:
    """まだ終えていない実施回（`_versions_in_use`）の今の版（取引の依頼者の範囲。同じ版は1つ。business_id＝その業務だけ）。版を足して自動で替える
    仕事が、1つずつ別の取引で `derive_for_standard`・`adopt_ready` を呼ぶための並び（1つの失敗が、ほかの版・実施回を止めない＝
    見直し(B) の5）。依頼者の解決策を足す流れ（client_solutions._lines）も、これを読む。"""
    return sorted({r["version_id"] for r in _versions_in_use(tx, business_id)})


def in_use_runs(tx: Tx, business_id: UUID | None = None) -> list[UUID]:
    """まだ終えていない、版のある実施回（`_versions_in_use`）（取引の依頼者の範囲。business_id＝その業務の実施回だけ＝取り込み直しで入れない解決策を
    決める client_solutions.held_back が読む）。"""
    return [r["run_id"] for r in _versions_in_use(tx, business_id)]


def derive_for_standard(tx: Tx, version_id: UUID | None = None) -> list[UUID]:
    """管理者が標準の想定問答を新しくしたら、使っている業務の版（まだ終えていない行のある実施回の今の版。標準を使う版だけ）の
    つながりの先に、いまの標準に合わせた版を作る（きっかけ standard。1か所＝決まった間隔の仕事）。もう、いまの標準の版がつながりの先に
    あれば何もしない。業務の設定と合わずに作れなければ、契約者に知らせて（同じ版と標準で1回）、今の版のまま電話を続ける（書きかけを
    残さない＝戻す地点の中で作る）。version_id＝その版だけ（決まった間隔の仕事は、版ごとに別の取引で呼ぶ）。作った版を返す。"""
    std = current_standard(tx)
    if std.standard_id is None:
        return []
    made = []
    for v in in_use_versions(tx) if version_id is None else [version_id]:
        tip = successor_tip(tx, v)
        used = tx.one("SELECT standard_id FROM salescall.business_versions WHERE version_id = %s", (tip,))["standard_id"]
        if used is None or used == std.standard_id:
            continue
        try:
            with tx.savepoint():
                made.append(derive(tx, tip, lambda raw: raw, cause="standard", note="標準の想定問答の新しい版に合わせた",
                                   standard_id=std.standard_id))
        except SettingsInvalid as e:
            notify.emit(tx, "version_not_derived", key=f"version_not_derived:{tip}:{std.standard_id}",
                        summary="標準の想定問答が新しくなりましたが、業務設定の新しい版を作れませんでした（今の版のまま電話は続けます）："
                                + "／".join(e.problems), refs={"version_id": str(tip), "standard_id": str(std.standard_id)})
    return made


def adopt_ready(tx: Tx, run_id: UUID | None = None) -> list[tuple[UUID, UUID]]:
    """まだ終えていない行のある実施回の今の版に、話せる後の版があれば、自動で割り当てる（1か所。決まった間隔の仕事）。話せる＝
    (1) 今の版で話してよい声が、後の版でもすべて話してよい（声が減らない。今の版に声が無ければ1つ以上） (2) 品質の段で止まらずに
    始められる（quality_stage.start_kind が already か open） (3) 割り当てられない理由が無い（`assign_problems`）。後の版がいくつも
    続けば、話せるものの中でいちばん後の版。手で前の版に戻した実施回は、戻した版の写し（つながりの先の無い版＝`assign`）から先だけを
    たどるので、戻す前の変更を含む版へは替わらない（⛔ 0185 は、手の割り当てより前に作った後の版を時刻で外していたが、そのあとに足した
    解決策・新しい標準の版まで届かなかった＝見直し(B) の1）。割り当ての記録は `assign`（自動の印 auto と、理由に「自動」）。
    run_id＝その実施回だけ（決まった間隔の仕事は、実施回ごとに別の取引で呼ぶ）。割り当てた (実施回, 版) を返す。"""
    from salescall.core import recordings   # 録音の部品が業務設定を読むので、ここで読む（読み合いにしない）
    done = []
    for r in _versions_in_use(tx):
        if run_id is not None and r["run_id"] != run_id:
            continue
        now_voices = set(recordings.ready_voices(tx, r["version_id"]))
        chain = tx.all("SELECT version_id AS v, cause, note FROM salescall.version_line(%s) ORDER BY depth DESC",   # 0197 の1か所
                       (r["version_id"],))
        for c in chain:
            voices = set(recordings.ready_voices(tx, c["v"]))
            if not voices or not voices >= now_voices:
                continue
            if quality_stage.start_kind(tx, c["v"]) not in ("already", "open") or assign_problems(tx, r["run_id"], c["v"]):
                continue
            assign(tx, r["run_id"], c["v"], reason=f"自動：後の版の録音がそろった（{SUCCESSION_CAUSES[c['cause']]}：{c['note']}）",
                   auto=True)
            done.append((r["run_id"], c["v"]))
            break
    return done


def _shared_apart(tx: Tx, run_id: UUID, version_id: UUID) -> bool:
    """手で割り当てる版（version_id）の流れを、その実施回のために分けるか（`assign` だけが読む）：ほかのまだ終えていない実施回の
    いまの版で、その業務の依頼者の解決策（いまの中身＝client_solutions._latest と同じ salescall.business_solutions）が入っていない。
    入っていない解決策を「入れる」は実施回ごとの承認で、流れに版を足すので、流れを分けないと、ほかの実施回の承認になる。解決策が
    すべて入っている版は分けない（分けると、新しい版の品質の段を始め直すことになる）。決めるのはデータベースの1か所
    salescall.version_shared_without_solutions（0204。手の割り当ての守り run_version_guard も、これを読んで、真なら断る＝契約者の取引が
    run_versions へ直接書いても、2つの実施回が同じ流れを使えない。⛔ 2026-11-07 の見直し H：前はここだけが決め、データベースは守らなかった）。"""
    return tx.one("SELECT salescall.version_shared_without_solutions(%s, %s) AS x", (run_id, version_id))["x"]


def assign(tx: Tx, run_id: UUID, version_id: UUID, *, reason: str, auto: bool = False) -> str:
    """実施回に版を割り当てる（記録つき。DESIGN.md 3-2）。割り当てた版の、品質の段の始め方の答えを返す
    （quality_stage.prepare：opened／mock_needed／already／stopped_version）。中身の無い版は割り当てない。
    手で（auto でなく）割り当てる版に後の版がある（つながりの途中の版＝前の版へ戻す・前の版で実施回を作る）なら、その版の写しを
    以前の版へ戻す1か所（`save_from`）で作り、写しを割り当てる。つながりは1本の列なので、途中の版をそのまま割り当てると、そのあとに足す
    版（解決策・新しい標準）が戻す前の変更を含む列の先にしか作れず、この実施回へ届かなかった（見直し(B) の1）。写しはつながりの先が
    無いので、そこから足した版だけが、この実施回へ自動で替わる。自動の割り当て（auto）は、つながりの中の版をそのまま割り当てる。
    手で割り当てる版が、ほかのまだ終えていない実施回のいまの版で、その業務の依頼者の解決策が入っていない（`_shared_apart`）ときも写しを
    作る（流れを実施回ごとに分ける。入っていない解決策は、実施回ごとに「入れる／入れない」を確かめる＝client_solutions の承認の印は
    実施回ごと。⛔ 2026-11-07 のつなぎ目の見直し (A)：前は同じ版を2つの実施回が使い、片方の実施回の「入れる」で作った版が、もう片方の
    流れの先にもなって、もう片方の一覧が空になり、門が開き、自動で替わった）。"""
    if not reason.strip():
        raise ValueError("版を割り当てる理由を書いてください")
    problems = assign_problems(tx, run_id, version_id)
    if problems:                                     # 目的のお客様を呼べない行を残したまま、版を替えない（全部か、何も無いか）
        raise SettingsInvalid(problems)
    if not auto and (successor_tip(tx, version_id) != version_id or _shared_apart(tx, run_id, version_id)):
        business = tx.one("SELECT business_id FROM salescall.business_versions WHERE version_id = %s", (version_id,))["business_id"]
        version_id = save_from(tx, business, version_id)   # 写しの中身は同じ（割り当てられない理由も同じ）
    row = tx.one(
        "INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, version_id, assigned_by, reason, auto)"   # 番号は
        " SELECT r.tenant_id, r.client_id, r.run_id, r.business_id,"                                                           # next_seq（0109）
        "        %s, salescall.cur_actor(), %s, %s FROM salescall.runs r WHERE r.run_id = %s RETURNING seq",
        (version_id, reason, auto, run_id))
    if row is None:
        raise LookupError(f"その実施回は無い（または、取引の範囲の外）：{run_id}")
    # 手で版を替えた・実施回を作るときに古い版を選んだ：その流れにあって、この版に無い依頼者の解決策を「手で外した」と残す（取り込み直しでも
    # 自動では入れない・契約者が確かめるまで、まだかけていない行へかけない＝✅ 2026-11-07 ユーザー）。印を付けるのは、割り当てと同じ取引で
    # データベースの1か所（0201 の salescall.hand_removals。自動の割り当ては除く）。ここは、その印を契約者に知らせる
    if not auto:
        from salescall.core import client_solutions   # 解決策の部品が業務設定を読むので、ここで読む（読み合いにしない）
        client_solutions.removed_by_hand(tx, run_id, version_id)
    # 前の版で取った、まだ発信していない予約を取り消す（門の前提が変わったときの決まり＝DESIGN.md 5-2-1 の2。
    # 取り消さないと、前の版で最大30秒かけうる。2026-10-16 の見直し G7）
    attempts.release(tx, [r["attempt_id"] for r in tx.all(
        "SELECT a.attempt_id FROM salescall.attempts a JOIN salescall.tasks t ON t.task_id = a.task_id"
        " WHERE t.run_id = %s AND a.state = 'claimed' AND a.version_id <> %s", (run_id, version_id))])
    return quality_stage.prepare(tx, version_id)
