"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._calling_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import json
import time

from common import base_parser, require_python, save_result, say
from convo import answers
from flowctl import ABOUT, PHONE, SPEAKER, WHO
from meaning_judge import NoJudgment, add_judge_args, make_judges

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
#    籍（在籍していない）。並べた字のどれでもよい（言葉から、どちらとも言える）
_CODES = {SPEAKER: {"同": "前と同じ方", "違": "前と違う方"},
          WHO: {"本": "ご本人", "受": "受付・取次ぎの方", "後": "代理の方（後任）", "上": "代理の方（上司）", "可": "代理の方（代わりに決められる）",
                "否": "代理の方（代わりに決められない）", "代": "代理の方（どれかは分からない）", "別": "別の方（どなたかは分からない）",
                "不": "決められない"},
          PHONE: {"無": "何もない", "待": "取り次ぎを待つ"}, ABOUT: {"無": "言っていない", "在": "不在", "籍": "在籍していない"}}


def F(speaker: str, who: str, phone: str, about: str) -> dict[str, set]:
    return {k: {_CODES[k][x] for x in c} for k, c in ((SPEAKER, speaker), (WHO, who), (PHONE, phone), (ABOUT, about))}


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
    ("岡田は本日休みをいただいております。", F("同", "受", "無", "在")),  # お呼びした方を、ほかの人のこととして話した
    # 聞き取りが途中で切れた形（G-57：続きの「少々お待ちください。」が次の発話に入った）。いまの言葉は、待つようには言っていない
    ("岡田ですね。", F("同", "受", "無", "無")),
    # こちらへの質問（✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）。質問かは別の問い合わせ（ASKING・下）が
    # 読む。電話のやり取りは、取り次ぎの話は無い（⛔ 前は「どれでもよい」にしていた＝判定が質問を「取り次ぎを待つ」と読み違えても見つけられない）
    ("岡田ですね。失礼ですが、どちら様でしょうか？", F("同", "受", "無", "無")),
    ("どういったご用件でしょうか？", F("同", "不", "無", "無")),          # 誰が尋ねているかの手がかりは無い（ご本人が尋ねることもある）
    ("どちらの岡田でしょうか？", F("同", "受", "無", "無")),              # お呼びした方を、ほかの人のこととして尋ねた
    ("あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", F("同", "受", "無", "在")),
    (AWAY_TO_OTHER := "岡田は外出しておりますので、別の者に代わります。少々お待ちください。", F("同", "受", "待", "在")),
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
    ("岡田は本日休みをいただいております。", TO_TARGET, F("同", "受", "無", "在")),
    # ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともある」
    ("岡田は取り込んでおりまして、対応できかねます。", TO_TARGET, F("同", "受", "無", "在")),
    # 「とのことです」「ようです」は伝える言い方だが、取り次いだ方が戻ったのか、代わった方が伝えたのかは言っていない（2026-10-30 の別の目の読み直し）
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", TO_TARGET, F("同", "受", "無", "在")),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", TO_TARGET, F("同", "受", "無", "在")),
    ("お待たせいたしました。", TO_TARGET, F("同", "不", "無", "無")),
    # 違うお名前の方が出て、お呼びした方がいないとも言った（✅ 2026-10-22 ユーザー：「違う名乗りの方が、岡田はいないと言っても、また、岡田様を
    # 呼ぶんですか？」）
    # 「担当の者」は、代わりの者とは言っていない＝代理の方とは決められない（2026-10-30 の別の目の読み直し）
    ("お電話代わりました。担当の山田です。岡田は本日不在にしております。", TO_OTHER, F("違", "別", "無", "在")),
    ("お電話代わりました、山田です。あいにく岡田は外出しておりまして。", None, F("違", "別", "無", "在")),   # 電話を代わった方が事情を伝えただけ
    ("お電話代わりました。山田です。", AWAY_TO_OTHER, F("違", "代", "無", "無")),
]
# 目的の人かを確かめたあとの返事（判定の質問は確かめる一言＝Flow.judge_context）。2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても
# 正しく設計できているんですか？」で足した（確かめに「いいえ」と答えた方を「ご本人」と読めば、AIの読み違いと重なったときに別の方に繋ぐ）。
# 前の言葉は、本番と同じに、確かめるきっかけになった言葉（ASKED_AFTER）か、取り次いだ方の言葉。⛔ 2026-10-30 まで、前の言葉の無い確かめへの返事を
# 測っていた（本番では、呼んでいる段の相手の言葉は消えない＝Flow.judge_context。前の言葉が無いのは、呼ぶ一言のあとに初めて話した言葉だけ）
ASKED_AFTER = "はい。"
LINES_CONFIRMED = [
    ("はい、そうです。", ASKED_AFTER, F("同", "本", "無", "無")),
    ("はい、私です。", ASKED_AFTER, F("同", "本", "無", "無")),
    ("はい、そうです。", TO_TARGET, F("違", "本", "無", "無")),            # 前の言葉は取り次いだ方（ご本人ではない）＝話している方は違う方
    ("いいえ、私は山田と申します。", ASKED_AFTER, F("同", "受", "無", "無")),
    ("いいえ、私は山田と申します。", "お電話代わりました。担当の山田です。", F("同", "別", "無", "無")),   # 電話を代わった方が違うお名前を名乗っただけ
    ("いえ、違います。", ASKED_AFTER, F("同", "受", "無", "無")),
    ("いえ、受付の者です。少々お待ちください。", ASKED_AFTER, F("同", "受", "待", "無")),
    ("いえ、岡田は席を外しております。", ASKED_AFTER, F("同", "受", "無", "在")),
    ("いいえ、山田と申します。岡田は本日おりません。", ASKED_AFTER, F("同", "受", "無", "在")),
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

# 2つの事実が重なる言葉（2026-10-29 に足した。⛔ 1つの選びの形では、どちらかの事実を捨てる）：(相手の言葉, その前に相手が言ったこと, 確かめたあとか, 正解)
LINES_BOTH = [
    # 違う方が、お呼びした方は出られないと伝えた（✅ ユーザー：「いいえ、山田と申します。岡田は本日おりません。」だと、「別の方」+「不在」）
    ("いいえ、受付の田中です。岡田はただいま外出しております。", ASKED_AFTER, True, F("同", "受", "無", "在")),
    ("違います。岡田は会議中です。", ASKED_AFTER, True, F("同", "受", "無", "在")),
    ("いいえ、私は部下の山田です。岡田は出張中でして、戻りは来週になります。", ASKED_AFTER, True, F("同", "受", "無", "在")),
    ("受付の田中と申します。あいにく岡田は本日休みをいただいております。", None, False, F("同", "受", "無", "在")),
    ("岡田ですね。ただいま外出しておりまして、戻りは3時ごろの予定です。", None, False, F("同", "受", "無", "在")),
    ("岡田はただいま外出しておりますので、私が代わりにご用件をお伺いします。", None, False, F("同", "代", "無", "在")),   # 代わりに用件を伺う
    # 違う方が、取り次ぐので待つように言った
    ("いいえ、山田です。岡田に代わりますので少々お待ちください。", ASKED_AFTER, True, F("同", "受", "待", "無")),
    # 電話を代わった違う方が、お呼びした方は出られないと伝えた
    ("お電話代わりました、鈴木です。岡田は本日お休みをいただいております。", TO_TARGET, False, F("違", "別", "無", "在")),
    ("お電話代わりました。佐藤と申します。岡田は席を外しておりまして。", TO_TARGET, False, F("違", "別", "無", "在")),
    # ご本人が、いまは話せないと言った（お呼びし直さず・繋がずに、あとでかけ直す）
    ("はい、そうですが、いまちょっと手が離せないので後でかけ直してもらえますか。", ASKED_AFTER, True, F("同", "本", "無", "在")),
    ("はい、岡田です。申し訳ありません、いま会議中で手が離せないのですが。", TO_TARGET, False, F("違", "本", "無", "在")),
]
# 後任の方の話が出る言葉（はじめの場面。✅ 2026-10-29 ユーザー：「『別の方』の中に『後任』『上司』『同僚』などがいることになるでしょ！」）：
# (相手の言葉, その前に相手が言ったこと, 確かめたあとか, 正解)
TO_SUCCESSOR_NOW = "岡田は退職いたしまして、後任の田中に代わりますので、少々お待ちください。"
LINES_DEPARTED = [
    ("岡田は異動になりまして、今は私が担当しております。", None, False, F("同", "後", "無", "籍")),
    (TO_SUCCESSOR_NOW, None, False, F("同", "受", "待", "籍")),            # 後任の方を、ほかの人のこととして話して取り次ぐ
    ("お電話代わりました。田中です。", TO_SUCCESSOR_NOW, False, F("違", "後", "無", "無")),
    ("はい、お電話代わりました。", "岡田は退職しておりますので、後任の者に代わります。少々お待ちください。", False, F("違", "後", "無", "無")),
    ("いえ、岡田は退職しまして、私が後任の田中です。", ASKED_AFTER, True, F("同", "後", "無", "籍")),
    # 本日だけの代わりの方（後任の方ではない＝お呼びした方は在籍している）
    ("岡田は本日休みでして、本日は私が代わりに担当しております。", None, False, F("同", "代", "無", "在")),
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
    ("田中はただいま席を外しております。", TO_SUCCESSOR, False, F("同", "受", "無", "在")),  # 後任の田中様を、ほかの人のこととして話した（取り次ぎのあと）
    ("はい、私が後任の田中です。", DEPARTED_SAID, False, F("同", "後", "無", "無")),
    (TO_SUCCESSOR, DEPARTED_SAID, False, F("同", "受", "待", "無")),
    ("後任はおりません。", DEPARTED_SAID, False, F("同", "受", "無", "籍")),          # そういう方はいない＝在籍していない
    ("はい、そうです。", f"{DEPARTED_SAID}／{ASKED_AFTER}", True, F("同", "後", "無", "無")),
    ("いえ、私は受付の者です。", f"{DEPARTED_SAID}／{ASKED_AFTER}", True, F("同", "受", "無", "無")),
]
# その前の言葉のうち、取り次ぎの言葉（その読みで、次に話す方は、もう「名乗りを聞いていない方」になっている＝Flow.on_check の _new_speaker(after=True)。
# このあとの言葉の「前と違う方」は、流れの制御の動きを変えない。g59_tally が数える）
HELD_BEFORE = {TO_TARGET, TO_OTHER, AWAY_TO_OTHER, "それでは岡田にお繋ぎします。", "岡田は外出しておりますので、別の者に代わります。",
               TO_SUCCESSOR_NOW, "岡田は退職しておりますので、後任の者に代わります。少々お待ちください。", TO_SUCCESSOR,
               "公認は田中になります。少々お待ちください。", "後任の者に代わりますので、少々お待ちください。"}
# ご決定で足した、受け取ってよい読み（流れの制御の読み）：受付の方が不在を伝えてから代わった別の方は、組み合わせた読みが「不在」でもよい（どちらでも、
# 流れの制御は代わりの方として扱う＝Flow._handed。✅ 2026-10-22 ユーザー（4回目）。test_away_text_quotes_the_words_read_as_away）。項目ごとの
# 正解は、いまの言葉の事情（言っていない）のまま
EXTRA = {("お電話代わりました。山田です。", AWAY_TO_OTHER): ["不在"],
         ("お電話代わりました。", "岡田は外出しておりますので、別の者に代わります。"): ["不在"]}


def accept_of(facts: dict[str, set], seeks_successor: bool = False) -> list[str]:
    """正解 ⇒ 受け取ってよい読み（正解の組み合わせを、流れの制御の1か所＝flowctl.facts_reading で読みにしたもの）。"""
    from itertools import product

    from flowctl import facts_reading
    keys = list(facts)
    return sorted({facts_reading(dict(zip(keys, c)), seeks_successor)[0] for c in product(*(facts[k] for k in keys))})


def facts_ok(got: dict | None, facts: dict[str, set]) -> bool:
    """項目ごとの答え（判定の返事＝Reading.facts）が、項目ごとの正解にどれも当たるか。"""
    return got is not None and all(got.get(k) in f for k, f in facts.items())


def cases(confirm: str) -> list[tuple[str, list[str], str | None, str | None, bool, dict]]:
    """測る言葉の全部：(相手の言葉, 受け取ってよい読み, その前に相手が言ったこと, 確かめた言い方, 後任の方をお呼びしたあとか, 正解)。"""
    from phrases import SUCCESSOR
    rows = ([(t, None, False, False, f) for t, f in LINES] + [(t, b, False, False, f) for t, b, f in LINES_AFTER]
            + [(t, b, True, False, f) for t, b, f in LINES_CONFIRMED]
            + [(t, b, c, False, f) for t, b, c, f in LINES_BOTH + LINES_DEPARTED] + [(t, b, c, True, f) for t, b, c, f in LINES_SUCCESSOR])
    return [(t, sorted(set(accept_of(f, s)) | set(EXTRA.get((t, b), ()))), b, (SUCCESSOR["target_confirm"] if s else confirm) if c else None, s, f)
            for t, b, c, s, f in rows]


def flows():
    """測る業務と、2つの流れの制御（ふつうの場面・後任の方をお呼びしたあと）。測る所（run）と送る文の指紋（prompts_fingerprint）の1か所。"""
    from flowctl import Flow
    from instruction import Target
    from g22_opening import BUSINESS
    import dataclasses
    b = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長"))
    mk = lambda: Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True, judge_names=b.judge_names())
    f, fs = mk(), mk()
    fs._to_successor()  # 後任の方をお呼びしたあとの問い（本番と同じ1か所で作り直す）
    return b, f, fs


def question_of(fl, asked) -> "Question":
    """判定に見せる質問：確かめたあとの返事は、確かめる一言を質問にする（Flow.judge_context）。名前の形も本番と同じ1か所（Flow.for_judge）。"""
    return fl.for_judge(fl.confirm_questions["target_confirm"] if asked else fl.calling_question)


def prompts_fingerprint() -> tuple[int, str]:
    """判定に送る文（71件の、目的のお客様の判定の問い合わせの文の全部）の長さと指紋（0円）。測った文に1文字も違わず戻したか・文を
    変えていないかを確かめる（⛔ 2026-10-30：文をいくつも直して測りが悪くなり、測った中でいちばん良い形に戻すとき、その形の写しが作業の
    場所の外にしか無かった）。"""
    import hashlib
    from meaning_judge import prompt_parts
    b, f, fs = flows()
    s = "".join("".join(prompt_parts(question_of(fs if succ else f, asked), text, None, before))
                for text, _, before, asked, succ, _ in cases(b.target.confirm()))
    return len(s), hashlib.sha256(s.encode()).hexdigest()[:16]


async def run(args) -> dict:
    from flowctl import ASKED, ASKS_OTHER, ASKS_YES, Question, label, unsure_counts
    b, f, fs = flows()
    judges = {k: j for k, j in make_judges(args).items() if k != "grounder"}  # 測るのは意味の判定だけ（中身の判定は g63_grounding）
    rows = []
    for text, ok, before, asked, successor, facts in cases(b.target.confirm()):
        if args.only and text not in args.only:   # 外れた言葉だけを、本番と同じ送り方でくり返して確かめるとき（測る言葉を選ぶだけ）
            continue
        fl = fs if successor else f
        q = question_of(fl, asked)
        for rep in range(args.repeat):
            for j in {j.panel for j in judges.values() if j is not None}:
                j.reset()  # 言葉ごとに測る（前の言葉の時間切れで、この言葉の問い合わせを飛ばさない）
            want = None if text in ASKS_EITHER else text in ASKING or text in ASKING_OTHER
            kind = ASKS_YES if text in ASKING else ASKS_OTHER if text in ASKING_OTHER else None
            row = {"text": text, "accept": ok, "asks_accept": want, "asks_kind": kind, "before": before, "asked": asked, "rep": rep,
                   "successor": successor, "facts_accept": {k: sorted(v) for k, v in facts.items()},
                   "both": (text, before, bool(asked)) in {(t, b, c) for t, b, c, _ in LINES_BOTH}}
            async def one(k, j, col, question):
                t0 = time.monotonic()
                try:
                    got = await j(question, text, None, before=before)
                except NoJudgment as exc:  # 答えにならなかった（Jev の確からしさが線より下・止まっている）：どれも選ばなかったと数える
                    row[f"{col}_s"] = round(time.monotonic() - t0, 3)
                    row[f"{col}_tried"] = exc.tried
                    # 確からしさが線より下の読みは、本番と同じに数える（flowctl.unsure_counts＝Flow.on_unsure と同じ決まり）
                    unsure = next((t["unsure"] for t in exc.tried if "unsure" in t), None)
                    counted = unsure_counts("judge" if col == k else "asks", unsure) if unsure is not None else None
                    row[col] = label(counted) if counted else None
                    return
                row[f"{col}_s"] = round(time.monotonic() - t0, 3)   # 答えが届くまでの秒（判定の速さ。2026-10-25：2つ目の判定の替えを比べる）
                row[col] = label(got) if got else None
                if getattr(got, "facts", None) is not None:   # 項目ごとの答え（目的のお客様の判定）
                    row[f"{col}_facts"], row[f"{col}_away"], row[f"{col}_new"] = got.facts, got.away, got.new
                if got is not None and got.by:
                    row[f"{col}_by"], row[f"{col}_sure"] = got.by, got.sure
                if getattr(got, "weak", None) is not None:   # 代わりが答えたときの、受け持ちの組の確かでない読み（本番は Flow.on_unsure で数える）
                    row[f"{col}_weak"] = got.weak
            # 目的の人の判定と、こちらに尋ねているかの問い合わせ（本番と同じに、同じ言葉・同じ前の言葉で同時に出す＝convo.check_handler）
            # 代わりだけの受け持ち（お呼びする段の 3.5 Flash Lite）には問い合わせない（convo.answers）
            await asyncio.gather(*(one(k, j, col, question) for k, j in judges.items() if j is not None
                                   for col, question in ((k, q), (f"{k}_asks", fl.for_judge(fl.asks_questions[q.id])))
                                   if answers(j, question)))
            # 尋ねているかは「尋ねている」だけが流れを変える（Flow._asking_now）＝ほかの読みは全部「尋ねていない」と数える
            asked_now = [k for k in judges if judges[k] is not None and f"{k}_s" in row]   # この行で問い合わせた受け持ち
            row["ok"] = all(row.get(k) in ok and (want is None or (row.get(f"{k}_asks") in ASKED) == want) for k in asked_now)
            row["facts_ok"] = all(facts_ok(row.get(f"{k}_facts"), facts) for k in asked_now)
            rows.append(row)
            say(f"（前：{before}）" if before else "", text, {k: (row.get(k), row.get(f"{k}_asks")) for k in judges}, "OK" if row["ok"] else "×")
            if args.pace_s:   # 使えるキーが少ない無料の枠で、1分の上限に削られないように（⛔ 2026-10-29：2本で流して49行中40行が届かなかった）
                await asyncio.sleep(args.pace_s)
    # 測った受け持ち＝答えた行がある受け持ち（お呼びする段の質問では、代わりだけの受け持ちは問い合わせない＝本番と同じ。convo.answers）
    measured = [k for k in judges if judges[k] is not None and any(f"{k}_s" in r for r in rows)]
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in measured}
    # 事実ごと（項目ごとに答える形だけ）：項目がみな正解か・項目ごと（その項目を見せた言葉だけ）・2つの事実が重なる言葉（LINES_BOTH）だけ
    def fok(r, k):
        return facts_ok(r.get(f"{k}_facts"), {x: set(v) for x, v in r["facts_accept"].items()})
    summary |= {f"{k}（事実ごとに全部）": f"{sum(1 for r in rows if fok(r, k))}/{len(rows)}" for k in measured}
    for key, name in ((SPEAKER, "前と同じ方か"), (WHO, "話している方は誰か"), (PHONE, "電話のやり取り"), (ABOUT, "いまお呼びしている方の事情")):
        shown = [r for r in rows if key in r["facts_accept"]]
        summary |= {f"{k}（{name}）": f"{sum(1 for r in shown if (r.get(f'{k}_facts') or {}).get(key) in r['facts_accept'][key])}/{len(shown)}"
                    for k in measured}
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
        s = sorted(r[f"{k}_s"] for r in rows if f"{k}_s" in r)
        if s:
            summary[f"{k}（答えまでの秒：中央・遅いほうから1割・最長）"] = [s[len(s) // 2], s[int(len(s) * 0.9)], s[-1]]
    say("数え：", json.dumps(summary, ensure_ascii=False))
    return {"question": q.text, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    add_judge_args(p)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--only", nargs="*", default=None, help="この言葉だけを測る（外れた言葉を、本番と同じ送り方でくり返して確かめるとき）")
    p.add_argument("--pace-s", type=float, default=0.0, help="行ごとに待つ秒（無料の枠のキーが少ないとき。2本×1分15回なら 4.5）")
    p.add_argument("--tag", default="")
    # ⛔ 2026-10-30：--only の言葉の数で料金を見積もり、同じ言葉がほかの場面にもあって行が倍になり、上限の円に届いて10行を測れなかった
    p.add_argument("--count", action="store_true", help="測らずに、測る行と問い合わせの数だけを出す（有料の測りの料金の見込みは、この数から出す）")
    p.add_argument("--fingerprint", action="store_true", help="測らずに、判定に送る文の長さと指紋だけを出す（測った文と同じかを確かめる）")
    args = p.parse_args()
    if args.fingerprint:
        say("判定に送る文：%d 字・指紋 %s" % prompts_fingerprint())
        return
    if args.count:
        n = sum(1 for text, *_ in cases("x") if not args.only or text in args.only)
        say(f"測る行：{n} 行 × {args.repeat} 回り（1つの判定につき、問い合わせは {n * args.repeat * 2} 回＝目的のお客様の判定と、尋ねているか）")
        return
    save_result(f"G-59{args.tag}", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
