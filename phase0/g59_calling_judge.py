"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._calling_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import functools
import json
import time

from common import base_parser, require_python, save_result, say
from convo import answers
from flowctl import (ABOUT, ABSENT_WHY, ABSENT_WHY_NOT_SAID, CALLBACK_LABEL, END, END_HURRY, END_NONE, END_UNCLEAR, END_WHO, HEAR, HEAR_LOST, HEAR_OK,
                     OFFER, OFFER_NOT_SAID, PHONE, SPEAKER, UNDECIDED, WHO)
from g53_meaning import spread_record
from meaning_judge import OTHER_JUDGES, NoJudgment, add_judge_args, batch_judge, measured_judges

# ── 正解の表（言葉ごとに、項目ごとの正解＝flowctl.target_facets の項目）。正解は、判定に見せた言葉（質問・はじめにお呼びした言葉・その前に相手が
#    言ったこと・お客様の言葉）と項目の説明の文だけから決める。受け取ってよい読み（流れの制御の読み）は、正解の組み合わせを流れの制御の1か所
#    （flowctl.facts_reading）で読みにしたもの（accept_of）＋ご決定で足した読み（EXTRA）。
#    ✅ 2026-10-29 ユーザー：「『不在』と『別の方』というのは、そもそも別の評価軸でしょ」「こちらの質問と答えが間違っていたら、数字や判断が大きく変わって
#       しまうので注意しなさい！」・✅ 2026-10-30：「第1層：『前と同じ方』『前と違う方』／第2層：『前と違う方』の中に、『目的のご本人』『取次ぎの方』
#       『代理の方』、『前と同じ方』の中に『受付など』『ご本人』／第3層：『代理の方』の中に、『後任』『上司』『ご本人の代わりをできる代理』『代わりの
#       できない代理』に分かれますよね。これを判断させないと、誤るのは当然です。」
#    ⛔ 2026-10-29 まで、言葉からは言えない答えまで正解に入れていた（別の目の読み直しで 60件中24件が食い違い）。⛔ 2026-10-30 まで、話している方を
#       「ご本人・別の方・決められない」（＋後任の方か）でしか持てず、受付の方の「岡田ですね。」を「別の方」と書くしかなかった
#    字：(1) 前と同じ方か＝同・違（⛔ 2026-10-30 に足した「決められない」はやめた＝ユーザーの形のとおり2つ）／(2) 誰か＝本（ご本人）・受（受付・取次ぎの方）・後（代理の方：後任）・上（代理の方：上司）・
#    可（代理の方：代わりに決められる）・否（代理の方：代わりに決められない）・代（代理の方：どれかは分からない）・別（別の方：どなたかは分からない）・
#    不（決められない）／(3) 電話のやり取り＝無（何もない）・待（取り次ぎを待つ）／(4) いまお呼びしている方の事情＝無（言っていない）・在（不在）・
#    籍（在籍していない）／(5) お呼びした方が電話に出られない理由（書かなければ「理由は言っていない」＝cases の既定）＝無（理由は言っていない）・在（不在）・
#    休（お休み）・長（長期の不在）・後（あとで）・別（別の番号へ）・決（決められない）。並べた字のどれでもよい（言葉から、どちらとも言える）
_CODES = {SPEAKER: {"同": "前と同じ方", "違": "前と違う方"},
          WHO: {"本": "ご本人", "受": "受付・取次ぎの方", "後": "代理の方（後任）", "上": "代理の方（上司）", "可": "代理の方（代わりに決められる）",
                "否": "代理の方（代わりに決められない）", "代": "代理の方（どれかは分からない）", "別": "別の方（どなたかは分からない）",
                "不": "決められない"},
          PHONE: {"無": "何もない", "待": "取り次ぎを待つ"}, ABOUT: {"無": "言っていない", "在": "不在", "籍": "在籍していない"},
          ABSENT_WHY: {"無": ABSENT_WHY_NOT_SAID, "在": CALLBACK_LABEL["absent"], "休": CALLBACK_LABEL["day_off"], "長": CALLBACK_LABEL["long_absent"],
                       "後": CALLBACK_LABEL["later"], "別": CALLBACK_LABEL["other_number"], "決": UNDECIDED}}


def F(speaker: str, who: str, phone: str, about: str, why: str = "") -> dict[str, set]:
    """行の正解（字は上の表）。why＝お呼びした方が出られない理由の字（書かなければ cases の既定＝理由は言っていない）。"""
    return {k: {_CODES[k][x] for x in c} for k, c in ((SPEAKER, speaker), (WHO, who), (PHONE, phone), (ABOUT, about), (ABSENT_WHY, why)) if c}


# (相手の言葉, 正解)。呼ぶ一言への言葉（その前の言葉は無い＝前と同じ方は、電話に出た方そのもの）
LINES = [
    ("お電話代わりました。岡田です。", F("違", "本", "無", "無")),
    ("お電話代わりました、営業部の岡田です。", F("違", "本", "無", "無")),
    ("はい、岡田です。", F("同", "本", "無", "無")),
    ("岡田は私ですが。", F("同", "本", "無", "無")),
    # 前の言葉の無い「お電話代わりました。」は、代わった方がお呼びした方かどうか分からない（✅ 2026-10-19 ユーザー：取り次いだ方がまた出たのか
    # 代わった方が出たのか分からない言葉は確かめる＝話している方は誰かの「決められない」の例そのもの＝flowctl.target_facets）
    ("お電話代わりました。", F("違", "不", "無", "無")),
    ("お電話代わりました。山本です。", F("違", "別", "無", "無")),       # 電話を代わった違うお名前の方（取り次ぐ方か代理の方かは言っていない）
    ("岡田ですね。少々お待ちください。", F("同", "受", "待", "無")),     # お呼びした方の名前をくり返して確かめた＝受付など
    ("少々お待ちください。", F("同", "受", "待", "無")),                 # はじめに出た方が待つように言った＝受付など
    ("岡田は本日休みをいただいております。", F("同", "受", "無", "在", "休")),  # お呼びした方を、ほかの人のこととして話した
    # 聞き取りが途中で切れた形（G-57：続きの「少々お待ちください。」が次の発話に入った）。いまの言葉は、待つようには言っていない
    ("岡田ですね。", F("同", "受", "無", "無")),
    # こちらへの質問（✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）。質問かは別の問い合わせ（ASKING・下）が
    # 読む。電話のやり取りは、取り次ぎの話は無い（⛔ 前は「どれでもよい」にしていた＝判定が質問を「取り次ぎを待つ」と読み違えても見つけられない）
    ("岡田ですね。失礼ですが、どちら様でしょうか？", F("同", "受", "無", "無")),
    ("どういったご用件でしょうか？", F("同", "不", "無", "無")),          # 誰が尋ねているかの手がかりは無い（ご本人が尋ねることもある）
    ("どちらの岡田でしょうか？", F("同", "受", "無", "無")),              # お呼びした方を、ほかの人のこととして尋ねた
    ("あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", F("同", "受", "無", "在", "在")),
    (AWAY_TO_OTHER := "岡田は外出しておりますので、別の者に代わります。少々お待ちください。", F("同", "受", "待", "在", "在")),
    # 名乗り・用件・お呼びした方のほかのことを尋ねる（⛔ 2026-10-25 J10b：番号の出どころに AIが用件の録音で答えた ⇒ 尋ねているかの問い合わせで、
    # 何を聞かれたかも読む＝flowctl.ASKS_OTHER）
    ("この番号はどちらでお知りになりましたか？", F("同", "不", "無", "無")),
    ("失礼ですが、御社はどちらにある会社なんですか？", F("同", "不", "無", "無")),
    ("キャンセル料ってかかるんですか？", F("同", "不", "無", "無")),
    ("岡田ですね。失礼ですが、この番号はどちらでお知りになりましたか？", F("同", "受", "無", "無")),
]
# (相手の言葉, その前に相手が言ったこと＝取り次いだ方の言葉, 正解)（Flow.judge_context の before）
# ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』になりますよね」
TO_TARGET, TO_OTHER = "岡田ですね。少々お待ちください。", "担当の者に代わりますので、少々お待ちください。"
LINES_AFTER = [
    ("はい、お電話代わりました。", TO_TARGET, F("違", "本", "無", "無")),
    ("お電話代わりました。", TO_TARGET, F("違", "本", "無", "無")),
    ("はい、お電話代わりました。", "それでは岡田にお繋ぎします。", F("違", "本", "無", "無")),
    ("お電話代わりました。岡田です。", TO_TARGET, F("違", "本", "無", "無")),
    ("お電話代わりました。山本です。", TO_TARGET, F("違", "別", "無", "無")),
    # ✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）
    ("はい、お電話代わりました。", TO_OTHER, F("違", "本不別代", "無", "無")),
    # お呼びした方がいないので別の者に代わると言ったあとに出た方＝代理の方（どれかは分からない）。不在は前の言葉にしか無い（いまの言葉は言っていない）
    ("お電話代わりました。", "岡田は外出しておりますので、別の者に代わります。", F("違", "代", "無", "無")),
    # 取り次ぎのあとに話した方が、取り次いだ方が戻ったのか、代わった方なのかは、言葉からは決められないが、電話を代わったという言葉が無い＝前と同じ方。
    # 名乗らずにお呼びした方の事情だけを伝えたので、戻った方でも代わった方でも取り次ぐ側の方（受付・取次ぎの方）。ご本人ではない（お呼びした方のことを話した）
    ("岡田は本日休みをいただいております。", TO_TARGET, F("同", "受", "無", "在", "休")),
    # ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともある」
    ("岡田は取り込んでおりまして、対応できかねます。", TO_TARGET, F("同", "受", "無", "在", "後")),
    # 「とのことです」「ようです」は伝える言い方だが、取り次いだ方が戻ったのか、代わった方が伝えたのかは言っていない（2026-10-30 の別の目の読み直し）
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", TO_TARGET, F("同", "受", "無", "在")),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", TO_TARGET, F("同", "受", "無", "在", "後")),
    ("お待たせいたしました。", TO_TARGET, F("同", "不", "無", "無")),
    # 違うお名前の方が出て、お呼びした方がいないとも言った（✅ 2026-10-22 ユーザー：「違う名乗りの方が、岡田はいないと言っても、また、岡田様を
    # 呼ぶんですか？」）
    # 「担当の者」は、代わりの者とは言っていない＝代理の方とは決められない（2026-10-30 の別の目の読み直し）
    ("お電話代わりました。担当の山田です。岡田は本日不在にしております。", TO_OTHER, F("違", "別", "無", "在", "長")),
    ("お電話代わりました、山田です。あいにく岡田は外出しておりまして。", None, F("違", "別", "無", "在", "在")),   # 電話を代わった方が事情を伝えただけ
    ("お電話代わりました。山田です。", AWAY_TO_OTHER, F("違", "代", "無", "無")),
]
# 目的の人かを確かめたあとの返事（判定の質問は確かめる一言＝Flow.judge_context）。2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても
# 正しく設計できているんですか？」で足した（確かめに「いいえ」と答えた方を「ご本人」と読めば、AIの読み違いと重なったときに別の方に繋ぐ）。
# 前の言葉は、本番と同じに、確かめるきっかけになった言葉（ASKED_AFTER）か、取り次いだ方の言葉。⛔ 2026-10-30 まで、前の言葉の無い確かめへの返事を
# 測っていた（本番では、呼んでいる段の相手の言葉は消えない＝Flow.judge_context。前の言葉が無いのは、はじめにお呼びしたあとに初めて話した言葉だけ＝
# Flow._called_words。第一声でお呼びする方ご本人が名乗った電話は確かめる一言から始まり、その確かめへのお返事にも前の言葉は無い＝第一声は渡さない。この場面の行は表に無い）
# 確かめのきっかけは、前の言葉の最後に必ずある（本番は、目的のお客様か決まらない読みとご本人の報告のあとにだけ確かめる一言を流す＝取り次ぎの言葉のすぐあと・
# 「別の方」と読んだ言葉のすぐあとには流さない）。⛔ e055c397 まで、取り次ぎの言葉「岡田ですね。少々お待ちください。」のすぐあとの「はい、そうです。」（正解
# 前と違う方・ご本人）と、「お電話代わりました。担当の山田です。」のすぐあとの「いいえ、私は山田と申します。」を確かめへの返事として測っていた＝本番では組めない
# 場面（帳面 1 の (13) の (c)・頼み rv123/Y3 の (2)）
ASKED_AFTER = "はい。"
LINES_CONFIRMED = [
    ("はい、そうです。", ASKED_AFTER, F("同", "本", "無", "無")),
    ("はい、私です。", ASKED_AFTER, F("同", "本", "無", "無")),
    # 取り次ぎのあとに出た方の「はい。」で確かめた（取り次ぐと言われたことだけでは前と違う方にしない＝「はい。」の方と同じ方）。⛔ e055c397 まで前の言葉は
    # 取り次ぎの言葉だけで、正解は前と違う方だった
    ("はい、そうです。", f"{TO_TARGET}／{ASKED_AFTER}", F("同", "本", "無", "無")),
    ("いいえ、私は山田と申します。", ASKED_AFTER, F("同", "受", "無", "無")),
    # 電話を代わった方が違うお名前を名乗ったのでお呼びし直し、「はい。」で確かめた（前の言葉で「代わって出た」山田様が、違うお名前を名乗っただけ＝別の方）
    ("いいえ、私は山田と申します。", f"お電話代わりました。担当の山田です。／{ASKED_AFTER}", F("同", "別", "無", "無")),
    ("いえ、違います。", ASKED_AFTER, F("同", "受", "無", "無")),
    ("いえ、受付の者です。少々お待ちください。", ASKED_AFTER, F("同", "受", "待", "無")),
    ("いえ、岡田は席を外しております。", ASKED_AFTER, F("同", "受", "無", "在", "在")),
    ("いいえ、山田と申します。岡田は本日おりません。", ASKED_AFTER, F("同", "受", "無", "在", "長")),
    ("えっと、どちら様でしょうか？", ASKED_AFTER, F("同", "不", "無", "無")),     # こちらに聞き返しただけ（WHO_CONFIRM の「決められない」の例そのもの）
]
# こちらに尋ねている言葉（呼んでいる段の、別の問い合わせ Flow.asks_context が「尋ねている」と読むべきもの。ほかの言葉は全部「尋ねていない」）。
# ⛔ 2026-10-22：目的の人の判定の選択肢に「こちらに聞いている」を足す形は、質問は読めたが「お待たせいたしました。」を Sonnet が3回とも
#    「ご本人」と読むなど、ほかの言葉の読み方まで変わった（3.5 Flash Lite 103/111・Sonnet 102/111）＝取り下げて、別の問い合わせにした
ASKING = {"岡田ですね。失礼ですが、どちら様でしょうか？", "どういったご用件でしょうか？", "どちらの岡田でしょうか？",
          "あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", "えっと、どちら様でしょうか？"}
# ほかのことを尋ねている言葉（「ほかのことを尋ねている」と読むべきもの。ASKING は「名乗り・用件・お呼びした方のことを尋ねている」と読むべきもの）
ASKING_OTHER = {"この番号はどちらでお知りになりましたか？", "失礼ですが、御社はどちらにある会社なんですか？", "キャンセル料ってかかるんですか？",
                "岡田ですね。失礼ですが、この番号はどちらでお知りになりましたか？"}
# 尋ねているかは、どちらと読んでもよい言葉：代わりにご用件を伺うという申し出（人が読んでも「ご用件をお聞かせください」と取れる。流れの制御は、
# 「尋ねている」でも、問いの形の申し出として代わりの方の報告を止めない＝Flow._offer_while_asking）。2026-10-29 G-59facets_r1：Sonnet は「尋ねている」
ASKS_EITHER = {"岡田はただいま外出しておりますので、私が代わりにご用件をお伺いします。"}


def asks_truths(text: str, only: bool = False) -> tuple[str, ...]:
    """行の言葉の「尋ねているか」の正しい読み（唯一の決まり＝測り run の asks_accept・asks_kind と、測りの数えが場面に渡す正しい読み
    g59_flow_check.calling_acts）：名乗り・用件・お呼びした方のことを尋ねている（ASKING）・ほかのことを尋ねている（ASKING_OTHER）・どちらと読んでもよい
    （ASKS_EITHER＝尋ねている・尋ねていない）・尋ねていない。聞こえ方だけを数える行（only）は正解を付けていない＝どれも。"""
    from flowctl import ASKS_NO, ASKS_OTHER, ASKS_YES
    if only:
        return ASKS_YES, ASKS_OTHER, ASKS_NO
    if text in ASKS_EITHER:
        return ASKS_YES, ASKS_NO
    return (ASKS_YES,) if text in ASKING else (ASKS_OTHER,) if text in ASKING_OTHER else (ASKS_NO,)


# 2つの事実が重なる言葉（2026-10-29 に足した。⛔ 1つの選びの形では、どちらかの事実を捨てる）：(相手の言葉, その前に相手が言ったこと, 確かめたあとか, 正解)
LINES_BOTH = [
    # 違う方が、お呼びした方は出られないと伝えた（✅ ユーザー：「いいえ、山田と申します。岡田は本日おりません。」だと、「別の方」+「不在」）
    ("いいえ、受付の田中です。岡田はただいま外出しております。", ASKED_AFTER, True, F("同", "受", "無", "在", "在")),
    ("違います。岡田は会議中です。", ASKED_AFTER, True, F("同", "受", "無", "在", "後在")),
    ("いいえ、私は部下の山田です。岡田は出張中でして、戻りは来週になります。", ASKED_AFTER, True, F("同", "受", "無", "在", "長")),
    ("受付の田中と申します。あいにく岡田は本日休みをいただいております。", None, False, F("同", "受", "無", "在", "休")),
    ("岡田ですね。ただいま外出しておりまして、戻りは3時ごろの予定です。", None, False, F("同", "受", "無", "在", "在")),
    ("岡田はただいま外出しておりますので、私が代わりにご用件をお伺いします。", None, False, F("同", "代", "無", "在", "在")),   # 代わりに用件を伺う
    # 違う方が、取り次ぐので待つように言った
    ("いいえ、山田です。岡田に代わりますので少々お待ちください。", ASKED_AFTER, True, F("同", "受", "待", "無")),
    # 電話を代わった違う方が、お呼びした方は出られないと伝えた
    ("お電話代わりました、鈴木です。岡田は本日お休みをいただいております。", TO_TARGET, False, F("違", "別", "無", "在", "休")),
    ("お電話代わりました。佐藤と申します。岡田は席を外しておりまして。", TO_TARGET, False, F("違", "別", "無", "在", "在")),
    # ご本人が、いまは話せないと言った（お呼びし直さず・繋がずに）：かけ直してほしいと頼んだ＝かけ直しの申し出（電話を終える言葉ではない＝あとで
    # かけ直す）／忙しいとだけ言った＝電話を終える言葉の「急いでいる」（急いでいる方への伺い。✅ 11-01 23時59分「(a) OK」の電話を終える言葉の表の
    # ご本人「すみません、いま手が離せないので失礼します。」＝「10-13『忙しい』とだけ＝急いでいる」と同じ理由。判定への説明 end_choices も同じ境目＝
    # 「何時ならよい・かけ直してほしい、とまでは言っていない」）。⛔ 2026-11-05 に直した：11-01 に項目を足したとき、この言葉も読まずに「言っていない」
    # にしていた（11-02・11-03 の測りで、d1-ultra と考えない Sonnet が3回とも「急いでいる」と読んだのを外れと数えていた＝判定のほうが正しかった）
    ("はい、そうですが、いまちょっと手が離せないので後でかけ直してもらえますか。", ASKED_AFTER, True, F("同", "本", "無", "在", "後")),
    ("はい、岡田です。申し訳ありません、いま会議中で手が離せないのですが。", TO_TARGET, False, F("違", "本", "無", "在", "後") | {END: {END_HURRY}}),
]
# 後任の方の話が出る言葉（はじめの場面。✅ 2026-10-29 ユーザー：「『別の方』の中に『後任』『上司』『同僚』などがいることになるでしょ！」）：
# (相手の言葉, その前に相手が言ったこと, 確かめたあとか, 正解)
TO_SUCCESSOR_NOW = "岡田は退職いたしまして、後任の田中に代わりますので、少々お待ちください。"
LINES_DEPARTED = [
    ("岡田は異動になりまして、今は私が担当しております。", None, False, F("同", "後", "無", "籍", "無決")),
    (TO_SUCCESSOR_NOW, None, False, F("同", "受", "待", "籍", "無決")),            # 後任の方を、ほかの人のこととして話して取り次ぐ
    ("お電話代わりました。田中です。", TO_SUCCESSOR_NOW, False, F("違", "後", "無", "無")),
    ("はい、お電話代わりました。", "岡田は退職しておりますので、後任の者に代わります。少々お待ちください。", False, F("違", "後", "無", "無")),
    ("いえ、岡田は退職しまして、私が後任の田中です。", ASKED_AFTER, True, F("同", "後", "無", "籍", "無決")),
    # 本日だけの代わりの方（後任の方ではない＝お呼びした方は在籍している）
    ("岡田は本日休みでして、本日は私が代わりに担当しております。", None, False, F("同", "代", "無", "在", "休")),
]
# 後任の方をお呼びしたあと（Flow._to_successor＝判定の問いは「後任のご担当者様はいらっしゃいますでしょうか？」・はじめにお呼びした言葉も見せる・
# 確かめる一言は「後任のご担当者様でいらっしゃいますでしょうか？」）。「ご本人」は、はじめにお呼びした岡田様のまま：(相手の言葉, その前に相手が
# 言ったこと, 確かめたあとか, 正解)。⛔ 2026-10-25 J12b（在籍していない・L4）：受付の方の「後任は田中になります。少々お待ちください。」（聞き取りは
# 「公認は田中に…」）のあとの「お電話代わりました。田中です。」を、3.5 Flash Lite は「代わった人が話し始めた」と読み、田中様に後任の方をお呼びし直した
TO_SUCCESSOR = "後任は田中になります。少々お待ちください。"
DEPARTED_SAID = "岡田は先月退職いたしました。"   # 後任の方へ切り替えた言葉（本番では、後任の方の場面の言葉の前に必ずある＝Flow.judge_context）
LINES_SUCCESSOR = [
    ("お電話代わりました。田中です。", TO_SUCCESSOR, False, F("違", "後", "無", "無")),
    # 前の言葉は聞き取りの誤り（「後任」が「公認」）：字のとおりなら後任の方とは言っていない（✅ 2026-10-29 ユーザー：「『お電話代わりました。田中です。』
    # だけで判断するとなると、『別の方』は正解で、『後任』の可能性もある」）。後任の方をお呼びした問いへの答えとして田中様を出したので、代理の方とも
    # 読める・字のとおりなら、いまお呼びしている方かは決められない（2026-10-30 の別の目の読み直し）。どちらも「別の方」の側ではない（J12b）
    ("お電話代わりました。田中です。", "公認は田中になります。少々お待ちください。", False, F("違", "後不", "無", "無")),
    ("はい、お電話代わりました。", "後任の者に代わりますので、少々お待ちください。", False, F("違", "後", "無", "無")),
    ("お電話代わりました。山本です。", TO_SUCCESSOR, False, F("違", "別", "無", "無")),   # 後任の方だと言われたお名前と違う
    ("田中はただいま席を外しております。", TO_SUCCESSOR, False, F("同", "受", "無", "在", "在")),  # 後任の田中様を、ほかの人のこととして話した（取り次ぎのあと）
    ("はい、私が後任の田中です。", DEPARTED_SAID, False, F("同", "後", "無", "無")),
    (TO_SUCCESSOR, DEPARTED_SAID, False, F("同", "受", "待", "無")),
    ("後任はおりません。", DEPARTED_SAID, False, F("同", "受", "無", "籍", "無決")),          # そういう方はいない＝在籍していない
    ("はい、そうです。", f"{DEPARTED_SAID}／{ASKED_AFTER}", True, F("同", "後", "無", "無")),
    ("いえ、私は受付の者です。", f"{DEPARTED_SAID}／{ASKED_AFTER}", True, F("同", "受", "無", "無")),
]
# ご決定で足した、受け取ってよい読み（流れの制御の読み）：受付の方が不在を伝えてから代わった別の方は、組み合わせた読みが「不在」でもよい（どちらでも、
# 流れの制御は代わりの方として扱う＝Flow._handed。✅ 2026-10-22 ユーザー（4回目）。test_away_text_quotes_the_words_read_as_away）。項目ごとの
# 正解は、いまの言葉の事情（言っていない）のまま
EXTRA = {("お電話代わりました。山田です。", AWAY_TO_OTHER): ["不在"],
         ("お電話代わりました。", "岡田は外出しておりますので、別の者に代わります。"): ["不在"]}
# 行の「その前に相手が言ったこと」の1つずつの正しい読み（g59_flow_check の読みの字）と、そのときの会話AIの報告（相手）。場面を本番の流れの制御に
# 組む1か所（steps_before＝測りの数え g59_flow_check.row_scene と、後任の方の行の前の言葉 successor_before）が使う。取り次ぎの言葉＝受付の方が取り次ぎを
# 待つように言った（お呼びした方の事情は言葉のとおり）。確かめのきっかけ＝目的のお客様か決まらない読みと「ご本人」の報告（流れの制御が確かめる一言を
# 流す）。表の行にある言葉は、その行の正解と同じ読み（試験 MeasureCallingFlowTest が確かめる）。見直し (13) の D1・D2（2026-11-18）で、測りの数えの
# 手の写し（g59_tally.flow_flags と、その写しが使った取り次ぎの言葉の表 HELD_BEFORE＝取り次ぎの言葉のあとの方の「前と違う方」は動きを変えない、という
# Flow._new_speaker の決まりの写し）を外して本番の流れの制御に通すために足した
# 値＝(読みの字, 会話AIの操作, 足す読み)：操作は相手の報告の名前（report_party）か (操作, 引数)。足す読み（電話を終える言葉か など）は無ければ省く。
# ご事情を伺う一言・詳しい呼ぶ一言・確かめたあとに答えてお呼びし直した一言の場面（候補の行 CANDIDATES）の前の言葉も同じ形（頼み rv123/Y3 の (1)。
# 読みと操作は分身 M の場面の作り rv100/M/tools/labels.py のとおり。表の行にある言葉は、その行の正解の中の読み＝「結構です。」の電話を終える言葉かは表の正解の
# 「決められない」）
_HOLDING = ("同受待x", "reception")
UNCLEAR_CALL = "申し訳ございませんが、お取り次ぎすることはできません。"   # 理由を言わずに取り次げない（ご事情を伺う一言を流す言葉）
WHO_UNCLEAR_CALL = "お名前だけではどの者か分かりかねますので、お繋ぎできかねます。"   # どの方か分からず取り次げない（詳しい呼ぶ一言を流す言葉）
WHOIS_AFTER = "失礼ですが、どちら様でしょうか？"   # 確かめる一言のあとに聞かれた名乗り（名乗りと呼ぶ一言で答える言葉）
BEFORE_READ = {TO_TARGET: _HOLDING, "それでは岡田にお繋ぎします。": _HOLDING, TO_OTHER: _HOLDING,
               "岡田は外出しておりますので、別の者に代わります。": ("同受待在", "reception"), AWAY_TO_OTHER: ("同受待在", "reception"),
               TO_SUCCESSOR_NOW: ("同受待籍", "reception"), "岡田は退職しておりますので、後任の者に代わります。少々お待ちください。": ("同受待籍", "reception"),
               TO_SUCCESSOR: _HOLDING, "公認は田中になります。少々お待ちください。": _HOLDING, "後任の者に代わりますので、少々お待ちください。": _HOLDING,
               ASKED_AFTER: ("同不xx", "target"), "お電話代わりました。担当の山田です。": ("違別xx", "target"),
               UNCLEAR_CALL: ("同受xx", ("request_end", {"reason": "transfer_unclear"}), {END: END_UNCLEAR}),
               "結構です。": ("同受xx", ("request_end", {"reason": "callee_refused_call"}), {END: UNDECIDED}),
               "どちらの岡田でしょうか？": ("同受xx", ("replay_opening", {"part": "target"})),
               WHO_UNCLEAR_CALL: ("同受xx", ("request_end", {"reason": "who_unclear"}), {END: END_WHO}),
               WHOIS_AFTER: ("同不xx", ("replay_opening", {"part": "intro"}))}


def steps_before(before: str | None) -> list:
    """行の「その前に相手が言ったこと」を、場面を組む1か所（g59_flow_check.scene）の手順 [(相手の言葉, 読み, 会話AIの操作)] にする（唯一の作り）。
    読みは読みの字（足す読みがあれば項目ごとの答えに足した形）。後任の方へ切り替えた退職の言葉（DEPARTED_SAID）は、場面が後任の方の場面として先に流すので入れない。"""
    from g59_flow_check import facts
    out = []
    for p in (before or "").split("／"):
        if not p or p == DEPARTED_SAID:
            continue
        code, op, *side = BEFORE_READ[p]
        out.append((p, facts(code) | side[0] if side else code, ("report_party", {"party": op}) if isinstance(op, str) else op))
    return out


def lines_end() -> list[tuple[str, str | None, dict[str, set]]]:
    """電話を終える言葉の試しの表（end_words.CALLING・唯一の場所）のお呼びする段の言葉：(相手の言葉, その前に相手が言ったこと, 正解)。正解は、表の
    項目ごとの字（F）と、電話を終える言葉かの正解。ご本人の行は、受付の方の「岡田ですね。少々お待ちください。」のあとに出た方の言葉。"""
    from end_words import BEFORE_TARGET, CALLING
    return [(t, BEFORE_TARGET if who == "ご本人" else None, F(*codes) | {END: set(ends)}) for who, t, ends, _, codes in CALLING]


def fill(row, k, col, got, s, tried=None):
    """1行の判定 col の答えを書く（ふつうに送る測り・まとめて送る測りの1か所）。got が None＝答えにならなかった（Jev の確からしさが線より下・
    止まっている・読めない返事）：どれも選ばなかったと数え、確からしさが線より下の読みは本番と同じに数える（flowctl.unsure_counts＝Flow.on_unsure
    と同じ決まり）。s＝答えが届くまでの秒（まとめて送る測りは None）。"""
    row[f"{col}_s"] = s
    if got is None:
        row[f"{col}_tried"] = (tried.tried if isinstance(tried, NoJudgment) else tried) or []
        row[col] = unsure_of(col == k, tried) if isinstance(tried, NoJudgment) else None   # 確かでない読みは本番と同じ優先の順（unsure_of の1か所）
        return
    from flowctl import label

    row[col] = label(got) if got else None
    if getattr(got, "facts", None) is not None:   # 項目ごとの答え（目的のお客様の判定）
        row[f"{col}_facts"], row[f"{col}_away"], row[f"{col}_new"] = got.facts, got.away, got.new
    if got.by:
        row[f"{col}_by"], row[f"{col}_sure"] = got.by, got.sure
    spread_record(row, col, got)   # 問いごとの確からしさ（G-53 と同じ1か所＝今の決まり meaning_judge.settle_sure で出し直せる。見直し (13) の D10）
    if getattr(got, "weak", None) is not None:   # 代わりが答えたときの、受け持ちの組の確かでない読み（本番は Flow.on_unsure で数える）
        row[f"{col}_weak"] = got.weak


def unsure_of(target: bool, exc: NoJudgment) -> str | None:
    """答えが無かった行（組も代わりも答えなかった＝NoJudgment）で、本番と同じに数える確かでない読み（唯一の場所）：本番の組が優先の順に並べた確かでない読み
    （exc.unsure＝meaning_judge.unsure_order）の最初を、本番と同じ決まり（flowctl.unsure_counts＝Flow.on_unsure）で数えた名前。target＝目的のお客様の判定か
    （でなければ尋ねているか）。⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（J2）：問い合わせた記録 tried の届いた順の最初を使っていた（本番は 15時台に
    優先の順へ直した＝写し漏れ）"""
    from flowctl import label, unsure_counts
    first = next(iter(exc.unsure), None)
    counted = unsure_counts("judge" if target else "asks", first["unsure"]) if first else None
    return label(counted) if counted else None


def hear_before(asked: bool) -> str | None:
    """試しの言葉（G-73）の行の「その前に相手が言ったこと」（唯一の決まり＝正解の表 cases・聞こえ方だけを数える行の見分け g59_tally.hear_only・G-73 の
    場面の質問 g73_system_words.calling_questions）：確かめる一言のあとの行は、確かめのきっかけ（ASKED_AFTER）。呼ぶ一言のあとの行は無し。
    ⛔ e055c397 まで、確かめる一言のあとの行も前の言葉が無かった（ふつうの電話は確かめる一言の前に必ずお客様の言葉がある。前の言葉の無い確かめへのお返事は、第一声で
    お呼びする方ご本人が名乗った電話だけ＝第一声は判定に渡さない Flow._called_words。表の行は、ふつうの電話の確かめのきっかけのあと）"""
    return ASKED_AFTER if asked else None


def lines_hear() -> list[tuple[str, bool, dict[str, set]]]:
    """試しの言葉（G-73 の組 HEAR_CALL・読み口は g73_system_words.latest の1か所）：お呼びする段の2つの一言のあとの、こちらの声が届いていない様子か
    の言葉 (相手の言葉, 確かめる一言のあとか, 正解)。✅ 2026-11-02 日本時間23時15分58秒 ユーザー「特にない」＝正解は作らせた種類のまま。
    数えるのは聞こえ方だけ：ほかの項目は正解を付けていない（どれでもよい）＝読み・ほかの項目・尋ねているかの数えには入れない（run の hear_only）。"""
    from flowctl import target_facets
    from g73_system_words import latest
    return [(w["text"], w["asked"], {fc.key: set(fc.choices) for fc in target_facets(w["asked"], False)} | {HEAR: {w["truth"]}})
            for w in latest()["words"] if w["set"] == "HEAR_CALL"]


def accept_of(facts: dict[str, set], seeks_successor: bool = False) -> list[str]:
    """正解 ⇒ 受け取ってよい読み（正解の組み合わせを、流れの制御の1か所＝flowctl.facts_reading で読みにしたもの）。"""
    from itertools import product

    from flowctl import facts_reading
    keys = list(facts)
    return sorted({facts_reading(dict(zip(keys, c)), seeks_successor)[0] for c in product(*(facts[k] for k in keys))})


ADDED = (END, HEAR, OFFER, ABSENT_WHY)   # あとから足した項目（足す前の記録は、この項目を数えない＝facts_ok・facts_whole の1か所。相手の申し出・お呼びした方が
#    出られない理由＝2026-11-18 の rv92/H で足し、2026-11-19 日本時間21時台に正解を付けた）


def facts_ok(got: dict | None, facts: dict[str, set]) -> bool:
    """項目ごとの答え（判定の返事＝Reading.facts）が、項目ごとの正解にどれも当たるか。"""
    return got is not None and all(got.get(k) in f for k, f in facts.items() if not (k in ADDED and got.get(k) is None))   # 項目を足す前の記録は、足した項目を数えない


def facts_whole(got: dict | None, facts: dict) -> bool:
    """項目ごとの答えが、正解の表の項目をそろえて持っているか（足す前の記録は、足した項目 ADDED が無くてもそろっている＝facts_ok と同じ決まり。
    測りの数え g59_tally が、読みを流れの制御に当てる前に見る1か所）。⛔ 2026-11-06 日本時間8時台に見つけた：数えは「項目の数が正解の表と同じか」を
    手で見ていて、足す前の記録（10-31 の Jev・d1-ultra）の読みを、流れの制御の動きが同じなのに外れと数えた"""
    return bool(got) and all(k in got for k in facts if k not in ADDED)


def successor_before(text: str, before: str | None, asked: bool) -> str | None:
    """後任の方をお呼びしたあとの行（LINES_SUCCESSOR）の「その前に相手が言ったこと」を、本番の流れの制御が同じ場面で判定に渡す前の言葉
    （Flow.judge_context）で作る（唯一の作り。場面は g59_flow_check.scene＝退職の言葉 → 後任の方へ切り替え → 行の前の言葉 → 行の言葉）。
    ⛔ 2026-11-18 日本時間15時台（見直し (13) の D6）：行の前の言葉を手で書き、後任の方へ切り替えた退職の言葉（DEPARTED_SAID）が5行で抜けていた
    （本番は「岡田は先月退職いたしました。／後任は田中になります。…」）。正解は読み直して変えていない（足した言葉は前の方の退職の事実だけ＝いまの言葉の
    話している方・電話のやり取り・いまお呼びしている後任の方の事情は変わらない）。"""
    from g59_flow_check import scene
    f, _, _ = scene(steps_before(before), successor=True)   # 前の言葉の読みと報告は、測りの数えと同じ1か所（steps_before）
    f.on_callee(); f.on_heard(text)
    return f.judge_context()[2]


def cases(confirm: str) -> list[tuple[str, list[str], str | None, str | None, bool, dict]]:
    """測る言葉の全部：(相手の言葉, 受け取ってよい読み, その前に相手が言ったこと, 確かめた言い方, 後任の方をお呼びしたあとか, 正解)。"""
    from phrases import SUCCESSOR
    rows = ([(t, None, False, False, f) for t, f in LINES] + [(t, b, False, False, f) for t, b, f in LINES_AFTER]
            + [(t, b, True, False, f) for t, b, f in LINES_CONFIRMED]
            + [(t, b, c, False, f) for t, b, c, f in LINES_BOTH + LINES_DEPARTED]
            + [(t, successor_before(t, b, c), c, True, f) for t, b, c, f in LINES_SUCCESSOR]
            + [(t, b, False, False, f) for t, b, f in lines_end()] + [(t, hear_before(a), a, False, f) for t, a, f in lines_hear()])
    # 電話を終える言葉か（2026-11-01 に項目を足した）：71の言葉の正解は、行に END を書いたもの（ご本人の「…会議中で手が離せないのですが。」＝
    #    急いでいる）のほかは「言っていない」（断り・取り次げない・急ぎ・怒りの言葉は無い。⛔ 11-01 は1つずつ読まずに全部を「言っていない」にしていた＝
    #    2026-11-05 に71の言葉を1つずつ読み直した）。
    #    電話を終える言葉の試しの表の言葉（lines_end）は、表の正解（✅ 2026-11-02 日本時間1時59分 ユーザー：「OK、進めてください」）
    # こちらの声が届いていない様子か（2026-11-02 日本時間21時16分 ユーザー「(ア) OK」で項目を足した）：今までの試しの言葉には、届いていない様子の
    #    言葉が無い＝どれも「そういう様子は無い」。届いていない様子の言葉とそうでない紛らわしい言葉は、試しの言葉（lines_hear）
    # 相手の申し出・お呼びした方が電話に出られない理由（2026-11-18 の rv92/H で足した項目）：2026-11-19 日本時間21時台に、答えを伏せた読み手2人（作業場所
    #    rv150/readers/y3 の A_b・B_b）が全部の行を読み、2人がそろって1つに決めた113行はその読み、割れた16行は理由の説明の文（flowctl.absent_why_choices）と
    #    ご決定（10-25「休みとは言われず…だけなら「お戻り」」・11-15 伺い17 の会議中＝あとで）とお客様に聞こえる伺う一言で CL-1 が決めた（「本日おりません」
    #    「本日終日外出」＝その日のうちには戻らない＝長期の不在・「本日は難しい」＝あとで・お呼びした方がもう在籍していない・受付の方が取り次がない
    #    （営業の電話・お名前だけではどの方か分からない・個人あての電話）＝理由は使わない＝言っていないも決められないも正解）。申し出はどの行も「言っていない」。行に書かなければ「言っていない」
    return [(t, sorted(set(accept_of(f, s)) | set(EXTRA.get((t, b), ()))), b, (SUCCESSOR["target_confirm"] if s else confirm) if c else None, s,
             f | {END: f.get(END, {END_NONE}), HEAR: f.get(HEAR, {HEAR_OK}), OFFER: f.get(OFFER, {OFFER_NOT_SAID}),
                  ABSENT_WHY: f.get(ABSENT_WHY, {ABSENT_WHY_NOT_SAID})})
            for t, b, c, s, f in rows]


def flows(sees_target: bool | None = None):
    """測る業務と、2つの流れの制御（ふつうの場面・後任の方をお呼びしたあと）。測る所（run）と送る文の指紋（prompts_fingerprint）は業務だけを使う
    （判定に見せる質問は行の場面の本番の流れの制御＝judge_questions の1か所。2026-11-19 の頼み rv123/Y3 の (1) まで、ここの流れの制御から道具が組んでいた）。
    sees_target＝お呼びする方の名前と読みを判定に見せるか（既定は本番の形。False＝名前を伏せる形＝--hide-names・CL-1.md 4-165）。"""
    from flowctl import Flow
    import g59_flow_check as fc
    b = fc.b   # 測りの業務の唯一の定義（g59_flow_check.b。⛔ d8d03a78 まで、ここにも同じ定義を書いていた＝頼み rv140/AB の (2) の (b)）
    mk = lambda: Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True, judge_names=b.judge_names(sees_target=sees_target))
    f, fs = mk(), mk()
    fs._to_successor()  # 後任の方をお呼びしたあとの問い（本番と同じ1か所で作り直す）
    return b, f, fs


@functools.cache
def _judge_questions(text: str, before: str | None, asked: bool, successor: bool, full: bool, sees_target: bool | None):
    from g59_flow_check import row_scene
    f = row_scene({"text": text, "before": before, "asked": asked, "successor": successor, "full": full}, sees_target)
    return f.judge_context()[0], f.asks_context()[0]


def judge_questions(r: dict, sees_target: bool | None = None) -> tuple["Question", "Question"]:
    """行 r（text・before・asked・successor・印 full）の場面で判定に見せる質問：(目的のお客様の判定の質問, 尋ねているかの質問)（唯一の作り＝測り run・
    送る文の指紋 prompts_fingerprint・数え g59_tally.cascade・G-73 の場面の質問 g73_system_words.calling_questions）。行の場面を本番の流れの制御に組み
    （g59_flow_check.row_scene）、行の言葉を聞いた所の Flow.judge_context・Flow.asks_context（どちらも本番の1か所 Flow._calling_asked・_calling_heard＝
    お客様が最後に聞いた、お返事を待つ一言の文。ご事情を伺った一言なら印 inquiry）。組めない行は g59_flow_check.NoScene。sees_target＝--hide-names。
    ⛔ e055c397 まで、ここは道具が自分で組んでいた（question_of＝確かめたあとなら確かめる一言・ほかは呼ぶ一言＝ご事情を伺った一言・詳しい呼ぶ一言のあとの
       言葉を本番と違う質問で測る形。分身 M の穴 (c)・分身 K の穴 (d)・頼み rv123/Y3 の (1)）"""
    return _judge_questions(r["text"], r["before"] or None, bool(r["asked"]), bool(r["successor"]), bool(r.get("full")), sees_target)


def hear_scene(asked: bool, text: str = "はい。") -> dict:
    """試しの言葉（G-73）の場面の行（呼ぶ一言のあとか・確かめる一言のあとか）。言葉を作らせる場面の質問（g73_system_words.calling_questions）も、測りと
    同じ1か所（judge_questions）で作る。"""
    return {"text": text, "before": hear_before(asked), "asked": asked, "successor": False}


# 正解の表の候補（正解は付けていない＝答えを伏せた読み手が付ける・rv100/M/label_candidates.md の5場面50行）：(場面, 場面の説明, その前に相手が言ったこと,
# 確かめたあとか, フルネームのある業務か, 相手の言葉の並び)。場面は本番の流れの制御で組む（前の言葉の読みと会話AIの操作は BEFORE_READ）。指紋の道具・測りの
# 道具・数えは正解の表（cases）だけを数える＝候補は数えない（正解を付けてから表に移す）。⚠️ 分身 M の場面は電話に出た方の第一声「はい、テスト商事でござい
# ます。」から始め、業務の呼ぶ一言は「部長の…」だった。ここは表の行と同じ場面の作り（第一声なし・営業部部長）＝判定に見せる前の言葉に第一声が入らない（本番も
# 第一声を判定に渡さない＝Flow._called_words。第一声から組んでも同じ＝試験 CallingBeforeTest。⛔ d8d03a78 まで、本番は第一声も渡していた）
CANDIDATES = [
    ("S1", "ご事情を伺った一言のあと（理由を言わずに取り次げないと言われた）", UNCLEAR_CALL, False, False,
     ["はい、そうです。", "はい。", "ええ、そういうことです。", "はい、申し訳ございません。", "いえ、そういうわけではないのですが、お繋ぎはできかねます。",
      "いえ、営業のお電話はお断りしております。", "いいえ、営業のお電話はお繋ぎしない決まりになっております。", "はい、本日は外出しておりまして。",
      "はい、3時には戻る予定です。", "はい、会議が長引いておりまして。", "いえ、本人はおりますが、お繋ぎはできないことになっております。",
      "はい、今週いっぱい出張しております。", "いいえ、岡田は先月退職いたしました。", "そうですね、戻りましたら折り返しさせましょうか？",
      "はい、ご用件でしたら私が承ります。", "少々お待ちください。", "失礼ですが、どういったご用件でしょうか？", "申し上げられません。", "結構です。"]),
    ("S2", "ご事情を伺った一言のあと（受付の方に「結構です。」とだけ言われた）", "結構です。", False, False,
     ["はい、そうです。", "いえ、営業のお電話でしたら結構です。", "はい、本日は休みをいただいております。", "いえ、そういうことではなくて、お繋ぎできないんです。",
      "ですから、結構です。", "はい。"]),
    ("S3", "詳しい呼ぶ一言のあと（「どちらの岡田でしょうか？」と聞かれた）", "どちらの岡田でしょうか？", False, True,
     ["かしこまりました。少々お待ちください。", "岡田太郎ですね。少々お待ちください。", "岡田太郎は本日休みをいただいております。",
      "あいにく席を外しております。", "営業部には岡田太郎という者はおりませんが。", "岡田太郎は先月退職いたしました。", "はい、私ですが。",
      "失礼ですが、どういったご用件でしょうか？", "はい。"]),
    ("S4", f"詳しい呼ぶ一言のあと（「{WHO_UNCLEAR_CALL}」と言われた）", WHO_UNCLEAR_CALL, False, True,
     ["承知しました。少々お待ちください。", "申し訳ございませんが、それでもお繋ぎできかねます。", "岡田太郎は本日外出しております。",
      "営業部の岡田太郎ですね。少々お待ちください。", "そういった者はおりませんが。", "はい、お電話代わりました。岡田です。"]),
    ("S5", "確かめる一言のあとに「どちら様でしょうか？」と聞かれ、名乗りと呼ぶ一言で答えたあと（確かめたあとに答えてお呼びし直した一言）",
     f"{ASKED_AFTER}／{WHOIS_AFTER}", True, False,
     ["はい。", "岡田ですね。", "岡田ですね。少々お待ちください。", "はい、岡田です。", "ああ、私です。失礼しました。", "岡田はただいま外出しております。",
      "アオバ物流さんですね。少々お待ちください。", "そういったお電話はお繋ぎしておりません。", "失礼ですが、どういったご用件でしょうか？", "はい、それで？"]),
]


def candidates() -> list[dict]:
    """正解の表の候補の行（正解なし）：{番号・場面・場面の説明・相手の言葉・その前に相手が言ったこと・確かめたあとか・後任の方の場面か・フルネームの印}。
    行の形は測りの行と同じ（row_scene・judge_questions にそのまま渡せる）。"""
    return [{"id": f"{sid}-{i}", "scene": sid, "title": title, "text": t, "before": before, "asked": asked, "successor": False, "full": full}
            for sid, title, before, asked, full, words in CANDIDATES for i, t in enumerate(words, 1)]


def prompts_fingerprint(sees_target: bool | None = None) -> dict[str, tuple[int, str]]:
    """判定に送る文（71件の、目的のお客様の判定と尋ねているかの問い合わせの文の全部）の長さと指紋を、問い合わせごとに（0円）。測った文に
    1文字も違わず戻したか・文を変えていないかを確かめる（⛔ 2026-10-30：文をいくつも直して測りが悪くなり、測った中でいちばん良い形に戻すとき、
    その形の写しが作業の場所の外にしか無かった）。
    ⛔ 2026-10-31：目的のお客様の判定の文だけを見ていた＝答えの段の説明の文（1か所）を直したとき、同じ文が入る尋ねているかの文が変わったのに
       「お呼びする段の文は変わらない」と記録した ⇒ 尋ねているかの文も出す
    ⛔ 2026-11-18 日本時間15時台（見直し (13) の D5）：prompt_parts の文だけを指紋にしていた＝先に出す d1-ultra・Jev へ送る中身が変わっても指紋が
       同じ ⇒ 本番の2つ目の判定の組のうち、その質問に答える判定それぞれへ送る中身（meaning_judge.sent_text＝送る1か所 other_request）から作る"""
    import hashlib
    from meaning_judge import sent_text
    b, _, _ = flows(sees_target)
    target = asks = ""
    for text, _, before, asked, succ, _ in cases(b.target.confirm()):
        q, aq = judge_questions({"text": text, "before": before, "asked": asked, "successor": succ}, sees_target)   # 本番の場面の1か所
        target += sent_text(q, text, None, before)
        asks += sent_text(aq, text, None, before)
    return {name: (len(s), hashlib.sha256(s.encode()).hexdigest()[:16]) for name, s in (("目的のお客様の判定", target), ("尋ねているか", asks))}


async def run(args) -> dict:
    from flowctl import ASKED, ASKS_OTHER, ASKS_YES, Question, label
    sees = False if args.hide_names else None
    b, _, _ = flows(sees)
    if args.batch:
        # 正しさだけを見る測り：1つの Anthropic の判定を、まとめて送る（meaning_judge.batch_judge。料金は半分・答えまでの秒は測らない）
        name = args.judge2
        if args.judge or OTHER_JUDGES.get(name, {}).get("maker") != "anthropic":
            raise SystemExit("--batch は、--judge2 に Anthropic の判定を1つだけ選んだときに使えます（組・その場の判定とは一緒に使えません）")
        judges = {"judge": None, "judge2": name}
    else:
        judges = measured_judges(args)   # 測るのは選んだ意味の判定だけ（中身の判定は g63_grounding。選んでいなければ止める）
    rows, items, where = [], [], {}
    hear_only = {(t, hear_before(a), a) for t, a, _ in lines_hear()}   # 聞こえ方だけを数える行（試しの言葉 G-73）

    def finish(row) -> None:
        # 尋ねているかは「尋ねている」だけが流れを変える（Flow._asking_now）＝ほかの読みは全部「尋ねていない」と数える
        want, ok, facts = row["asks_accept"], row["accept"], {k: set(v) for k, v in row["facts_accept"].items()}
        asked_now = [k for k in judges if judges[k] is not None and f"{k}_s" in row]   # この行で問い合わせた受け持ち
        asks_now = [k for k in judges if judges[k] is not None and f"{k}_asks_s" in row]   # 尋ねているかを問い合わせた受け持ち（--asks-only でも数える）
        row["ok"] = (all(row.get(k) in ok for k in asked_now)
                     and all(want is None or (row.get(f"{k}_asks") in ASKED) == want for k in asks_now))
        row["facts_ok"] = all(facts_ok(row.get(f"{k}_facts"), facts) for k in asked_now)
        say(f"（前：{row['before']}）" if row["before"] else "", row["text"], {k: (row.get(k), row.get(f"{k}_asks")) for k in judges},
            "OK" if row["ok"] else "×")
    for text, ok, before, asked, successor, facts in cases(b.target.confirm()):
        if args.only and text not in args.only:   # 外れた言葉だけを、本番と同じ送り方でくり返して確かめるとき（測る言葉を選ぶだけ）
            continue
        # 判定に見せる質問は、行の場面の本番の流れの制御の1か所（judge_questions＝Flow._calling_asked）
        q, aq = judge_questions({"text": text, "before": before, "asked": asked, "successor": successor}, sees)
        for rep in range(args.repeat):
            only = (text, before, bool(asked)) in hear_only
            truths = asks_truths(text, only)   # 尋ねているかの正しい読み（1か所）：1つに決まる言葉だけ数える（「尋ねている」か＝ASKED に入るか）
            want = truths[0] in ASKED if len(truths) == 1 else None
            kind = truths[0] if len(truths) == 1 and truths[0] in ASKED else None
            row = {"text": text, "accept": ok, "asks_accept": want, "asks_kind": None if only else kind, "before": before, "asked": asked, "rep": rep,
                   "hear_only": only,
                   "successor": successor, "facts_accept": {k: sorted(v) for k, v in facts.items()},
                   "both": (text, before, bool(asked)) in {(t, b, c) for t, b, c, _ in LINES_BOTH}}
            rows.append(row)
            # 目的の人の判定と、こちらに尋ねているかの問い合わせ（本番と同じに、同じ言葉・同じ前の言葉で同時に出す＝convo.check_handler）
            pairs = ((q, ""), (aq, "_asks"))
            if only:   # 聞こえ方だけを数える行：数えない尋ねているかは問い合わせない（聞こえ方は目的のお客様の判定の項目＝flowctl.SIDE_ITEMS）
                pairs = () if args.asks_only else pairs[:1]
            elif args.asks_only:   # ⛔ 2026-10-31：文が変わったのは尋ねているかだけなのに、目的のお客様の判定まで流して、お伝えした額を超えた
                pairs = pairs[1:]
            if args.batch:
                for question, tail in pairs:
                    where[f"r{len(items)}"] = (row, "judge2", "judge2" + tail)
                    items.append((f"r{len(items)}", question, text, None, before))
                continue
            for j in {j.panel for j in judges.values() if j is not None}:
                j.reset()  # 言葉ごとに測る（前の言葉の時間切れで、この言葉の問い合わせを飛ばさない）

            async def one(k, j, col, question, row=row, text=text, before=before):
                t0 = time.monotonic()
                try:
                    got = await j(question, text, None, before=before)
                except NoJudgment as exc:
                    fill(row, k, col, None, round(time.monotonic() - t0, 3), exc)
                    return
                fill(row, k, col, got, round(time.monotonic() - t0, 3))   # 答えが届くまでの秒（判定の速さ。2026-10-25：2つ目の判定の替えを比べる）
            # 止める側だけの受け持ち（お呼びする段の 3.5 Flash Lite）には問い合わせない（convo.answers）
            await asyncio.gather(*(one(k, j, k + tail, question) for k, j in judges.items() if j is not None
                                   for question, tail in pairs if answers(j, question)))
            finish(row)
            if args.pace_s:   # 使えるキーが少ない無料の枠で、1分の上限に削られないように（⛔ 2026-10-29：2本で流して49行中40行が届かなかった）
                await asyncio.sleep(args.pace_s)
    if args.batch:
        from common import load_secret
        got = batch_judge(name, load_secret(OTHER_JUDGES[name]["key"], args.env), items, budget=args.budget_yen, poll_s=args.batch_poll_s)
        for cid, (reading, why) in got.items():
            row, k, col = where[cid]
            fill(row, k, col, reading, None, [] if reading is not None else [{"model": name, "error": why}])
        for row in rows:
            finish(row)
    # 測った受け持ち＝答えた行がある受け持ち（お呼びする段の質問では、止める側だけの受け持ちは問い合わせない＝本番と同じ。convo.answers）
    measured = [k for k in judges if judges[k] is not None and any(f"{k}_s" in r for r in rows)]
    # 読み・ほかの項目・尋ねているかは、聞こえ方だけを数える行（試しの言葉 G-73）を入れない。聞こえ方は全部の行（every）で数える
    every, rows = rows, [r for r in rows if not r.get("hear_only")]
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in measured}
    # 事実ごと（項目ごとに答える形だけ）：項目がみな正解か・項目ごと（その項目を見せた言葉だけ）・2つの事実が重なる言葉（LINES_BOTH）だけ
    def fok(r, k):
        return facts_ok(r.get(f"{k}_facts"), {x: set(v) for x, v in r["facts_accept"].items()})
    summary |= {f"{k}（事実ごとに全部）": f"{sum(1 for r in rows if fok(r, k))}/{len(rows)}" for k in measured}
    for key, name in ((SPEAKER, "前と同じ方か"), (WHO, "話している方は誰か"), (PHONE, "電話のやり取り"), (ABOUT, "いまお呼びしている方の事情"),
                      (END, "電話を終える言葉か")):
        shown = [r for r in rows if key in r["facts_accept"]]
        summary |= {f"{k}（{name}）": f"{sum(1 for r in shown if (r.get(f'{k}_facts') or {}).get(key) in r['facts_accept'][key])}/{len(shown)}"
                    for k in measured}
    for want in (HEAR_LOST, HEAR_OK):   # こちらの声が届いていない様子か：正解ごとに（届いていない様子の言葉・そうでない言葉）
        shown = [r for r in every if r["facts_accept"].get(HEAR) == [want]]
        summary |= {f"{k}（こちらの声が届いていない様子か・正解が{want}）":
                    f"{sum(1 for r in shown if (r.get(f'{k}_facts') or {}).get(HEAR) == want)}/{len(shown)}" for k in measured}
    both = [r for r in rows if r["both"]]
    summary |= {f"{k}（2つの事実の言葉：読み・事実ごと）": [f"{sum(1 for r in both if r.get(k) in r['accept'])}/{len(both)}",
                                                    f"{sum(1 for r in both if fok(r, k))}/{len(both)}"] for k in measured}
    summary |= {f"{k}（尋ねているか）": f"{sum(1 for r in rows if r['asks_accept'] is None or (r.get(f'{k}_asks') in ASKED) == r['asks_accept'])}/{len(rows)}"
                for k in measured}
    # 何を聞かれたか（質問の言葉だけ）：名乗り・用件の質問を「ほかのこと」と読むと、正しい名乗り・用件の答えを差し戻しうる（もう1つの読みが名乗り・用件
    # なら差し戻さない＝Flow._asked_other）／ほかの質問を「名乗り・用件」と読むと、用件の録音の答えを止められない
    asked_rows = [r for r in rows if r["asks_kind"]]
    summary |= {f"{k}（何を聞かれたか）": f"{sum(1 for r in asked_rows if r.get(f'{k}_asks') == r['asks_kind'])}/{len(asked_rows)}"
                for k in measured}
    summary["名乗り・用件の質問を、どの判定もほかと読んだ（差し戻しうる）"] = (
        f"{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_YES and any(r.get(f'{k}_asks') == ASKS_OTHER for k in measured) and not any(r.get(f'{k}_asks') == ASKS_YES for k in measured))}"
        f"/{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_YES)}")
    summary["ほかの質問を差し戻せた（ほかの読みがあり、名乗り・用件の読みが無い）"] = (
        f"{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_OTHER and any(r.get(f'{k}_asks') == ASKS_OTHER for k in measured) and not any(r.get(f'{k}_asks') == ASKS_YES for k in measured))}"
        f"/{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_OTHER)}")
    summary |= {f"{k}（前の言葉つき）": f"{sum(1 for r in rows if r['before'] and r.get(k) in r['accept'])}/{sum(1 for r in rows if r['before'])}"
                for k in measured}
    # 取り次ぎの読み（流れの制御は、読みのどれか1つでも「取り次ぎを待つ」なら黙って待つ＝flowctl.HOLD_READINGS）。⛔ 2026-10-25 J10b：不在と取り次ぎの
    # 2つの意味の言葉で、どの読みも「取り次ぎを待つ」にならず、取り次ぎの最中にかけ直しの質問を流した ⇒ 取り次ぎを含む言葉では、どれかの読みが
    # 「取り次ぎを待つ」であること（効き目）／取り次ぎでない言葉に、尋ねているかの問い合わせが「取り次ぎを待つ」を返した回（間違えて黙る側）を数える
    from flowctl import HOLD
    held = [r for r in rows if HOLD in r["accept"]]
    summary["取り次ぎの言葉に、どれかの読みが取り次ぎ"] = (f"{sum(1 for r in held if any(r.get(c) == HOLD for k in judges for c in (k, f'{k}_asks')))}"
                                              f"/{len(held)}")
    summary["取り次ぎでない言葉に、尋ねているかが取り次ぎ"] = (f"{sum(1 for r in rows if HOLD not in r['accept'] for k in judges if r.get(f'{k}_asks') == HOLD)}"
                                               f"/{sum(1 for r in rows if HOLD not in r['accept']) * len(measured)}")
    # 判定の速さ（目的の人の判定の問い合わせ。中央・遅いほうから1割・最長の秒）
    for k in measured:
        s = sorted(r[f"{k}_s"] for r in every if r.get(f"{k}_s") is not None)   # まとめて送る測りは秒を測らない
        if s:
            summary[f"{k}（答えまでの秒：中央・遅いほうから1割・最長）"] = [s[len(s) // 2], s[int(len(s) * 0.9)], s[-1]]
    say("数え：", json.dumps(summary, ensure_ascii=False))
    return {"question": q.text, "summary": summary, "rows": every}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    add_judge_args(p)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--only", nargs="*", default=None, help="この言葉だけを測る（外れた言葉を、本番と同じ送り方でくり返して確かめるとき）")
    p.add_argument("--pace-s", type=float, default=0.0, help="行ごとに待つ秒（無料の枠のキーが少ないとき。2本×1分15回なら 4.5）")
    p.add_argument("--tag", default="")
    p.add_argument("--batch", action="store_true", help="正しさだけを見る測り：--judge2 の Anthropic の判定1つを、まとめて送る（料金は半分・答えはたいてい1時間・"
                                                        "最長24時間・答えまでの秒は測らない。meaning_judge.batch_judge）")
    p.add_argument("--batch-poll-s", type=float, default=30.0, help="まとめて送った問い合わせが終わったかを見る間隔の秒")
    # ⛔ 2026-10-30：--only の言葉の数で料金を見積もり、同じ言葉がほかの場面にもあって行が倍になり、上限の円に届いて10行を測れなかった
    p.add_argument("--count", action="store_true", help="測らずに、測る行と問い合わせの数だけを出す（有料の測りの料金の見込みは、この数から出す）")
    p.add_argument("--fingerprint", action="store_true", help="測らずに、判定に送る文の長さと指紋だけを出す（測った文と同じかを確かめる）")
    p.add_argument("--asks-only", action="store_true", help="尋ねているかだけを問い合わせる（文が変わったのが尋ねているかだけのとき＝指紋で確かめてから）")
    p.add_argument("--g73-partial", action="store_true", help="試しの言葉（G-73）がそろっていなくても測る（ユーザーがその形で測ると選んだときだけ）")
    p.add_argument("--hide-names", action="store_true", help="お呼びする方の名前と読みを伏せた形で判定に見せる（名前を伏せる形と今の形の比べ＝CL-1.md 4-165・"
                                                            "本書 18-3 の 10-30 の (5)。伏せ方は instruction.Business.judge_names の1か所）")
    args = p.parse_args()
    if args.fingerprint:
        # 指紋は、試しの言葉（G-73）の結果のファイルで変わる（お呼びする段の試しの言葉に入る）：使ったファイルの名前を出し、無ければ指紋を出さずに止める
        # （黙って違う指紋を出さない。⛔ 0c55c4e7 まで、results を除いた写しで流すと 7df51a7178317d1a／0d48d4cbc3c5877c を出し、記録の値と違って見えた＝見直し (12) の D-14）
        from g73_system_words import latest
        if (g73 := latest()["path"]) is None:
            raise SystemExit("試しの言葉（G-73）の結果のファイル（results/G-73_2*.json）が無いので、指紋を出しません（指紋はこのファイルの言葉で変わります）")
        say(f"試しの言葉（G-73）：results/{g73}")
        say("判定に送る文：" + "／".join("%s %d 字・指紋 %s" % (name, n, h) for name, (n, h) in prompts_fingerprint(False if args.hide_names else None).items()))
        return
    from g73_system_words import require_complete
    if args.count:
        require_complete(args, "数えるだけ", stop=False)   # 試しの言葉がそろっているかも出す（止めない）
        picked = [(text, bool(asked)) for text, _, _, asked, *_ in cases("x") if not args.only or text in args.only]
        n, h = len(picked), len({x for x in picked} & {(t, a) for t, a, _ in lines_hear()})   # h＝聞こえ方だけを数える行（尋ねているかは問わない）
        say(f"測る行：{n} 行（うち聞こえ方だけ {h} 行）× {args.repeat} 回り（1つの判定につき、問い合わせは "
            + (f"{(n - h) * args.repeat} 回＝尋ねているかだけ）" if args.asks_only
               else f"{(n * 2 - h) * args.repeat} 回＝目的のお客様の判定と、尋ねているか）"))
        return
    require_complete(args, "お呼びする段の測り（G-59）")
    save_result(f"G-59{args.tag}", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
