"""G-59：目的の人を呼んでいる段の意味の判定が、取り次ぎ・代わった方・名乗ったご本人を見分けるか（DESIGN.md 4-3・Flow._calling_reported）。
⛔ 2026-10-19 G-57 の本番の形：「お電話代わりました。岡田です。」を、いまの判定は「代わった人が話し始めた」と読み、流れの制御は名乗った方に
   「「おかだ」様でいらっしゃいますでしょうか？」と聞き直した（2つ目の判定は「ご本人」）。判定の説明に、名乗った名前がお呼びした方なら
   「ご本人」を足して、同じ言葉で測る。正解はここに書いた（作らせたモデルの正解ではない）。"""
from __future__ import annotations

import asyncio
import json
import time

from common import base_parser, require_python, save_result, say
from meaning_judge import NoJudgment, add_judge_args, make_judges

# 質問の言葉の、目的の人の判定で受け取ってよい読み（下の LINES の注を参照）
ON_QUESTION = ["取り次ぎを待つ", "代わった人が話し始めた", "決められない", "わからない", "別の方"]
# (相手の言葉, 受け取ってよい読み)。「岡田ですね。」は、聞き取りが途中で切れた形（G-57：続きの「少々お待ちください。」が次の発話に入った）
LINES = [
    ("お電話代わりました。岡田です。", ["ご本人"]),
    ("お電話代わりました、営業部の岡田です。", ["ご本人"]),
    ("はい、岡田です。", ["ご本人"]),
    ("岡田は私ですが。", ["ご本人"]),
    # 前の言葉の無い「お電話代わりました。」は、代わった方がお呼びした方かどうか分からない＝確かめる（決められない）も正しい（✅ 2026-10-19
    # ユーザー：取り次いだ方がまた出たのか代わった方が出たのか分からない言葉は確かめる＝WHO_CALLING。どちらの読みも、名乗ってから
    # お呼びし直すか確かめる＝繋がない。⛔ 2026-10-25 G-59 race：考えない Sonnet 5.5 が3回中2回「決められない」と読み、正解を読み直した）
    ("お電話代わりました。", ["代わった人が話し始めた", "決められない"]),
    ("お電話代わりました。山本です。", ["代わった人が話し始めた", "決められない"]),
    ("岡田ですね。少々お待ちください。", ["取り次ぎを待つ"]),
    ("少々お待ちください。", ["取り次ぎを待つ"]),
    ("岡田は本日休みをいただいております。", ["不在"]),
    ("岡田ですね。", ["決められない", "取り次ぎを待つ", "別の方"]),
    # こちらへの質問（✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）。質問かは別の問い合わせ（ASKING・下）が
    # 読む。目的の人の判定は「ご本人」でも「不在」でもなければよい（ご本人＝この方の読みとして残り、答えたあと、AIの読みとそろえば繋ぐ側・
    # 不在＝答えたあと、かけ直しへ進む。取り次ぎ・代わった人は、尋ねた発話なら区切りを動かさないか、尋ねた方を新しい方として名乗りから流すだけ）
    ("岡田ですね。失礼ですが、どちら様でしょうか？", ON_QUESTION),
    ("どういったご用件でしょうか？", ON_QUESTION),
    ("どちらの岡田でしょうか？", ON_QUESTION),
    ("あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", ["不在"]),
    # 不在を伝えてから、別の方に取り次ぐ（流れの制御は「取り次ぎを待つ」が1つでもあれば黙って待つ＝どちらでもよい）
    (AWAY_TO_OTHER := "岡田は外出しておりますので、別の者に代わります。少々お待ちください。", ["不在", "取り次ぎを待つ"]),
    # 名乗り・用件・お呼びした方のほかのことを尋ねる（⛔ 2026-10-25 J10b：番号の出どころに AIが用件の録音で答えた ⇒ 尋ねているかの問い合わせで、
    # 何を聞かれたかも読む＝flowctl.ASKS_OTHER）。目的の人の判定は、質問の言葉と同じ（ご本人でも不在でもなければよい）
    ("この番号はどちらでお知りになりましたか？", ON_QUESTION),
    ("失礼ですが、御社はどちらにある会社なんですか？", ON_QUESTION),
    ("キャンセル料ってかかるんですか？", ON_QUESTION),
    ("岡田ですね。失礼ですが、この番号はどちらでお知りになりましたか？", ON_QUESTION),
]
# その前に相手が言ったこと（取り次いだ方の言葉）と、そのあとの言葉 ⇒ 受け取ってよい読み（Flow.judge_context の before）
# ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、『ご本人』になりますよね」
TO_TARGET, TO_OTHER = "岡田ですね。少々お待ちください。", "担当の者に代わりますので、少々お待ちください。"
LINES_AFTER = [
    ("はい、お電話代わりました。", ["ご本人"], TO_TARGET),
    ("お電話代わりました。", ["ご本人"], TO_TARGET),
    ("はい、お電話代わりました。", ["ご本人"], "それでは岡田にお繋ぎします。"),
    ("お電話代わりました。岡田です。", ["ご本人"], TO_TARGET),
    ("お電話代わりました。山本です。", ["代わった人が話し始めた", "決められない"], TO_TARGET),
    # ✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）
    ("はい、お電話代わりました。", ["ご本人", "代わった人が話し始めた", "決められない"], TO_OTHER),
    # 前の言葉で不在が分かっている：この言葉も「不在」と読めば、流れの制御は代わりの方として扱う（Flow._handed が先＝下の山田様の行と同じ）
    ("お電話代わりました。", ["代わった人が話し始めた", "決められない", "不在"], "岡田は外出しておりますので、別の者に代わります。"),
    ("岡田は本日休みをいただいております。", ["不在"], TO_TARGET),
    # ⚠️ 2026-10-19 ユーザー（2回目）：「取り次いだ後に、『岡田は取り込んでおり、対応できません』ともう一度受付が言うこともある」
    ("岡田は取り込んでおりまして、対応できかねます。", ["不在"], TO_TARGET),
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", ["不在"], TO_TARGET),
    ("お待たせいたしました。", ["決められない", "代わった人が話し始めた"], TO_TARGET),
    # 違うお名前の方が出て、お呼びした方がいないとも言った（✅ 2026-10-22 ユーザー：「違う名乗りの方が、岡田はいないと言っても、また、岡田様を
    # 呼ぶんですか？」）⇒ 不在（答え）を選ぶ。「代わった人」を選ぶと、流れの制御は岡田様をお呼びし直す
    ("お電話代わりました。担当の山田です。岡田は本日不在にしております。", ["不在"], TO_OTHER),
    ("お電話代わりました、山田です。あいにく岡田は外出しておりまして。", ["不在"], None),
    # 受付の方が不在を伝えてから代わった別の方：お呼びした方はいない（前の言葉）⇒ 不在でも「代わった人」でもよい（どちらでも、流れの制御は
    # 代わりの方として扱う＝Flow._handed。✅ 2026-10-22 ユーザー（4回目）。test_away_text_quotes_the_words_read_as_away）。⛔ 2026-10-29 まで
    # 正解を「不在」だけにしていた（注も 10-22 の前の形「『代わった人』を選ぶと岡田様をお呼びし直す」のまま）＝2つの判定の正しい読みを外れに数えていた
    ("お電話代わりました。山田です。", ["不在", "代わった人が話し始めた"], AWAY_TO_OTHER),
]
# 目的の人かを確かめたあとの返事（判定に、確かめた言い方を添える＝Flow.judge_context の asked）⇒ 受け取ってよい読み。
# 2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても正しく設計できているんですか？」で足した（確かめに「いいえ」と答えた方を
# 「ご本人」と読めば、AIの読み違いと重なったときに別の方に繋ぐ＝Flow._calling_reported）
NOT_TARGET = ["別の方", "代わった人が話し始めた", "決められない"]  # どれでも繋がない（Flow._calling_reported）。⛔ 不在は誤り（かけ直しへ進む）
LINES_CONFIRMED = [
    ("はい、そうです。", ["ご本人"], None),
    ("はい、私です。", ["ご本人"], None),
    ("はい、そうです。", ["ご本人"], TO_TARGET),
    ("いいえ、私は山田と申します。", NOT_TARGET, None),
    ("いいえ、私は山田と申します。", NOT_TARGET, "お電話代わりました。担当の山田です。"),
    ("いえ、違います。", NOT_TARGET, None),
    ("いえ、受付の者です。少々お待ちください。", ["取り次ぎを待つ"], None),
    ("いえ、岡田は席を外しております。", ["不在"], None),
    ("いいえ、山田と申します。岡田は本日おりません。", ["不在"], None),
    ("えっと、どちら様でしょうか？", NOT_TARGET + ON_QUESTION, None),
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


# お呼びした方が在籍しておらず、後任の方をお呼びしたあと（Flow._to_successor＝判定の問いは「後任のご担当者様はいらっしゃいますでしょうか？」）。
# ⛔ 2026-10-25 J12b（在籍していない・L4）：受付の方の「後任は田中になります。少々お待ちください。」（聞き取りは「公認は田中に…」）のあとの
#    「お電話代わりました。田中です。」を、3.5 Flash Lite は「代わった人が話し始めた」と読み、田中様に後任の方をお呼びし直した
TO_SUCCESSOR = "後任は田中になります。少々お待ちください。"
LINES_SUCCESSOR = [
    ("お電話代わりました。田中です。", ["ご本人"], TO_SUCCESSOR),
    ("お電話代わりました。田中です。", ["ご本人"], "公認は田中になります。少々お待ちください。"),
    ("はい、お電話代わりました。", ["ご本人"], "後任の者に代わりますので、少々お待ちください。"),
    ("お電話代わりました。山本です。", ["代わった人が話し始めた", "決められない"], TO_SUCCESSOR),
    ("田中はただいま席を外しております。", ["不在"], TO_SUCCESSOR),
]


# ── 事実ごとの正解（目的のお客様の判定を項目ごとに答える形＝flowctl.target_facets。✅ 2026-10-29 ユーザー：「『不在』と『別の方』というのは、
#    そもそも別の評価軸でしょ」「偶然、このシーンだけ…上手くいった。でも他のシーンでは上手くいかないということがないように」）。
#    (電話に出ている方, 電話のやり取り, お呼びした方の事情)：本＝ご本人・別＝別の方・不＝決められない／無＝何もない・待＝取り次ぎを待つ・
#    代＝代わった人が話し始めた／無＝言っていない・在＝不在。並べた字のどれでもよい。「*」はどれでもよい（質問の言葉＝答える道へ回る）
_CODES = {0: {"本": "ご本人", "別": "別の方", "不": "決められない"}, 1: {"無": "何もない", "待": "取り次ぎを待つ", "代": "代わった人が話し始めた"},
          2: {"無": "言っていない", "在": "不在"}}


def F(who: str, phone: str, about: str) -> tuple[set, set, set]:
    return tuple(set(_CODES[i].values()) if c == "*" else {_CODES[i][x] for x in c} for i, c in enumerate((who, phone, about)))


# (相手の言葉, その前に相手が言ったこと, 確かめたあとか) ⇒ 事実ごとの正解。言葉の正解（受け取ってよい読み）と同じ事実を、項目ごとに書いた
_Q = F("別不", "*", "無")   # こちらへの質問の言葉（ご本人とも不在とも読まない）
FACTS = {
    ("お電話代わりました。岡田です。", None, False): F("本", "代", "無"), ("お電話代わりました、営業部の岡田です。", None, False): F("本", "代", "無"),
    ("はい、岡田です。", None, False): F("本", "無", "無"), ("岡田は私ですが。", None, False): F("本", "無", "無"),
    ("お電話代わりました。", None, False): F("不別", "代", "無"), ("お電話代わりました。山本です。", None, False): F("別", "代", "無"),
    ("岡田ですね。少々お待ちください。", None, False): F("別不", "待", "無"), ("少々お待ちください。", None, False): F("別不", "待", "無"),
    ("岡田は本日休みをいただいております。", None, False): F("別不", "無", "在"), ("岡田ですね。", None, False): F("別不", "無待", "無"),
    ("岡田ですね。失礼ですが、どちら様でしょうか？", None, False): _Q, ("どういったご用件でしょうか？", None, False): _Q,
    ("どちらの岡田でしょうか？", None, False): _Q,
    ("あいにく岡田は外出しております。失礼ですが、どちら様でしょうか？", None, False): F("別不", "無", "在"),
    (AWAY_TO_OTHER, None, False): F("別不", "待", "在"),
    ("この番号はどちらでお知りになりましたか？", None, False): _Q, ("失礼ですが、御社はどちらにある会社なんですか？", None, False): _Q,
    ("キャンセル料ってかかるんですか？", None, False): _Q, ("岡田ですね。失礼ですが、この番号はどちらでお知りになりましたか？", None, False): _Q,
    ("はい、お電話代わりました。", TO_TARGET, False): F("本", "代", "無"), ("お電話代わりました。", TO_TARGET, False): F("本", "代", "無"),
    ("はい、お電話代わりました。", "それでは岡田にお繋ぎします。", False): F("本", "代", "無"),
    ("お電話代わりました。岡田です。", TO_TARGET, False): F("本", "代", "無"),
    ("お電話代わりました。山本です。", TO_TARGET, False): F("別", "代", "無"),
    ("はい、お電話代わりました。", TO_OTHER, False): F("本不別", "代", "無"),
    ("お電話代わりました。", "岡田は外出しておりますので、別の者に代わります。", False): F("別不", "代", "無在"),
    ("岡田は本日休みをいただいております。", TO_TARGET, False): F("別不", "無", "在"),
    ("岡田は取り込んでおりまして、対応できかねます。", TO_TARGET, False): F("別不", "無", "在"),
    ("申し訳ございません、岡田はただいま電話に出られないとのことです。", TO_TARGET, False): F("別不", "無", "在"),
    ("お待たせいたしました。岡田は会議中でして、本日は難しいようです。", TO_TARGET, False): F("別不", "無", "在"),
    ("お待たせいたしました。", TO_TARGET, False): F("不", "無代", "無"),
    ("お電話代わりました。担当の山田です。岡田は本日不在にしております。", TO_OTHER, False): F("別", "代", "在"),
    ("お電話代わりました、山田です。あいにく岡田は外出しておりまして。", None, False): F("別", "代", "在"),
    ("お電話代わりました。山田です。", AWAY_TO_OTHER, False): F("別", "代", "無在"),
    ("はい、そうです。", None, True): F("本", "無", "無"), ("はい、私です。", None, True): F("本", "無", "無"),
    ("はい、そうです。", TO_TARGET, True): F("本", "無", "無"),
    ("いいえ、私は山田と申します。", None, True): F("別", "無", "無"),
    ("いいえ、私は山田と申します。", "お電話代わりました。担当の山田です。", True): F("別", "無", "無"),
    ("いえ、違います。", None, True): F("別", "無", "無"), ("いえ、受付の者です。少々お待ちください。", None, True): F("別", "待", "無"),
    ("いえ、岡田は席を外しております。", None, True): F("別", "無", "在"),
    ("いいえ、山田と申します。岡田は本日おりません。", None, True): F("別", "無", "在"),
    ("えっと、どちら様でしょうか？", None, True): _Q,
    # 後任の方をお呼びしたあと（お呼びした方＝後任の方）
    ("お電話代わりました。田中です。", TO_SUCCESSOR, False): F("本", "代", "無"),
    ("お電話代わりました。田中です。", "公認は田中になります。少々お待ちください。", False): F("本", "代", "無"),
    ("はい、お電話代わりました。", "後任の者に代わりますので、少々お待ちください。", False): F("本", "代", "無"),
    ("お電話代わりました。山本です。", TO_SUCCESSOR, False): F("別", "代", "無"),
    ("田中はただいま席を外しております。", TO_SUCCESSOR, False): F("別不", "無", "在"),
}
# 2つの事実が重なる言葉（2026-10-29 に足した。⛔ 1つの選びの形では、どちらかの事実を捨てる）：(相手の言葉, その前に相手が言ったこと, 確かめたあとか,
# 事実ごとの正解)。受け取ってよい読みは、事実ごとの正解の組み合わせを流れの制御の読みにしたもの（flowctl.target_reading）
LINES_BOTH = [
    # 違う方が、お呼びした方は出られないと伝えた（✅ ユーザー：「いいえ、山田と申します。岡田は本日おりません。」だと、「別の方」+「不在」）
    ("いいえ、受付の田中です。岡田はただいま外出しております。", None, True, F("別", "無", "在")),
    ("違います。岡田は会議中です。", None, True, F("別", "無", "在")),
    ("いいえ、私は部下の山田です。岡田は出張中でして、戻りは来週になります。", None, True, F("別", "無", "在")),
    ("受付の田中と申します。あいにく岡田は本日休みをいただいております。", None, False, F("別", "無", "在")),
    ("岡田ですね。ただいま外出しておりまして、戻りは3時ごろの予定です。", None, False, F("別", "無", "在")),
    ("岡田はただいま外出しておりますので、私が代わりにご用件をお伺いします。", None, False, F("別", "無", "在")),
    # 違う方が、取り次ぐので待つように言った
    ("いいえ、山田です。岡田に代わりますので少々お待ちください。", None, True, F("別", "待", "無")),
    # 電話を代わった違う方が、お呼びした方は出られないと伝えた
    ("お電話代わりました、鈴木です。岡田は本日お休みをいただいております。", TO_TARGET, False, F("別", "代", "在")),
    ("お電話代わりました。佐藤と申します。岡田は席を外しておりまして。", TO_TARGET, False, F("別", "代", "在")),
    # ご本人が、いまは話せないと言った（お呼びし直さず・繋がずに、あとでかけ直す）
    ("はい、そうですが、いまちょっと手が離せないので後でかけ直してもらえますか。", None, True, F("本", "無", "在")),
    ("はい、岡田です。申し訳ありません、いま会議中で手が離せないのですが。", TO_TARGET, False, F("本", "代無", "在")),
]


def accept_of(facts: tuple[set, set, set]) -> list[str]:
    """事実ごとの正解 ⇒ 受け取ってよい読み（流れの制御の読み＝flowctl.target_reading の1か所）。"""
    from itertools import product

    from flowctl import target_reading
    return sorted({target_reading(*c)[0] for c in product(*facts)})


def facts_ok(got: dict | None, facts: tuple[set, set, set]) -> bool:
    """項目ごとの答え（判定の返事＝Reading.facts）が、事実ごとの正解にどれも当たるか。"""
    from flowctl import ABOUT, PHONE, WHO
    return got is not None and all(got.get(k) in f for k, f in zip((WHO, PHONE, ABOUT), facts))


def cases(confirm: str) -> list[tuple[str, list[str], str | None, str | None, bool, tuple]]:
    """測る言葉の全部：(相手の言葉, 受け取ってよい読み, その前に相手が言ったこと, 確かめた言い方, 後任の方をお呼びしたあとか, 事実ごとの正解)。"""
    return ([(t, o, None, None, False, FACTS[t, None, False]) for t, o in LINES]
            + [(t, o, b, None, False, FACTS[t, b, False]) for t, o, b in LINES_AFTER]
            + [(t, o, b, confirm, False, FACTS[t, b, True]) for t, o, b in LINES_CONFIRMED]
            + [(t, o, b, None, True, FACTS[t, b, False]) for t, o, b in LINES_SUCCESSOR]
            + [(t, accept_of(f), b, confirm if c else None, False, f) for t, b, c, f in LINES_BOTH])


async def run(args) -> dict:
    from flowctl import ASKED, ASKS_OTHER, ASKS_YES, Flow, Question, label, unsure_counts
    from instruction import Target
    target = Target(name="岡田", reading="おかだ", role="営業部部長")
    from g22_opening import BUSINESS
    import dataclasses
    b = dataclasses.replace(BUSINESS, target=target)
    f = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True, judge_names=b.judge_names())
    fs = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True, judge_names=b.judge_names())
    fs._to_successor()  # 後任の方をお呼びしたあとの問い（本番と同じ1か所で作り直す）
    judges = {k: j for k, j in make_judges(args).items() if k != "grounder"}  # 測るのは意味の判定だけ（中身の判定は g63_grounding）
    rows = []
    for text, ok, before, asked, successor, facts in cases(b.target.confirm()):
        fl = fs if successor else f
        # 確かめたあとの返事は、確かめる一言を質問にする（Flow.judge_context）。判定に見せる名前の形も本番と同じ1か所（Flow.for_judge）
        q = fl.for_judge(fl.confirm_questions["target_confirm"] if asked else fl.calling_question)
        for rep in range(args.repeat):
            for j in {j.panel for j in judges.values() if j is not None}:
                j.reset()  # 言葉ごとに測る（前の言葉の時間切れで、この言葉の問い合わせを飛ばさない）
            want = None if text in ASKS_EITHER else text in ASKING or text in ASKING_OTHER
            kind = ASKS_YES if text in ASKING else ASKS_OTHER if text in ASKING_OTHER else None
            row = {"text": text, "accept": ok, "asks_accept": want, "asks_kind": kind, "before": before, "asked": asked, "rep": rep,
                   "successor": successor, "facts_accept": [sorted(f) for f in facts], "both": (text, before, bool(asked), facts) in
                   [(t, b, c, f) for t, b, c, f in LINES_BOTH]}
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
            # 目的の人の判定と、こちらに尋ねているかの問い合わせ（本番と同じに、同じ言葉・同じ前の言葉で同時に出す＝convo.check_handler）
            await asyncio.gather(*(one(k, j, col, question) for k, j in judges.items() if j is not None
                                   for col, question in ((k, q), (f"{k}_asks", fl.for_judge(fl.asks_questions[q.id])))))
            # 尋ねているかは「尋ねている」だけが流れを変える（Flow._asking_now）＝ほかの読みは全部「尋ねていない」と数える
            row["ok"] = all(row.get(k) in ok and (want is None or (row.get(f"{k}_asks") in ASKED) == want) for k in judges if judges[k] is not None)
            row["facts_ok"] = all(facts_ok(row.get(f"{k}_facts"), facts) for k in judges if judges[k] is not None)
            rows.append(row)
            say(f"（前：{before}）" if before else "", text, {k: (row.get(k), row.get(f"{k}_asks")) for k in judges}, "OK" if row["ok"] else "×")
            if args.pace_s:   # 使えるキーが少ない無料の枠で、1分の上限に削られないように（⛔ 2026-10-29：2本で流して49行中40行が届かなかった）
                await asyncio.sleep(args.pace_s)
    summary = {k: f"{sum(1 for r in rows if r.get(k) in r['accept'])}/{len(rows)}" for k in judges if judges[k] is not None}
    # 事実ごと（項目ごとに答える形だけ）：3つの項目とも正解か・項目ごと・2つの事実が重なる言葉（LINES_BOTH）だけ
    def fok(r, k):
        return facts_ok(r.get(f"{k}_facts"), tuple(set(x) for x in r["facts_accept"]))
    summary |= {f"{k}（事実ごとに全部）": f"{sum(1 for r in rows if fok(r, k))}/{len(rows)}" for k in judges if judges[k] is not None}
    from flowctl import ABOUT, PHONE, WHO
    for i, (key, name) in enumerate(((WHO, "電話に出ている方"), (PHONE, "電話のやり取り"), (ABOUT, "お呼びした方の事情"))):
        summary |= {f"{k}（{name}）": f"{sum(1 for r in rows if (r.get(f'{k}_facts') or {}).get(key) in r['facts_accept'][i])}/{len(rows)}"
                    for k in judges if judges[k] is not None}
    both = [r for r in rows if r["both"]]
    summary |= {f"{k}（2つの事実の言葉：読み・事実ごと）": [f"{sum(1 for r in both if r.get(k) in r['accept'])}/{len(both)}",
                                                    f"{sum(1 for r in both if fok(r, k))}/{len(both)}"] for k in judges if judges[k] is not None}
    summary |= {f"{k}（尋ねているか）": f"{sum(1 for r in rows if r['asks_accept'] is None or (r.get(f'{k}_asks') in ASKED) == r['asks_accept'])}/{len(rows)}"
                for k in judges if judges[k] is not None}
    # 何を聞かれたか（質問の言葉だけ）：名乗り・用件の質問を「ほかのこと」と読むと、正しい名乗り・用件の答えを差し戻しうる（もう1つの読みが名乗り・用件
    # なら差し戻さない＝Flow._asked_other）／ほかの質問を「名乗り・用件」と読むと、用件の録音の答えを止められない
    asked_rows = [r for r in rows if r["asks_kind"]]
    summary |= {f"{k}（何を聞かれたか）": f"{sum(1 for r in asked_rows if r.get(f'{k}_asks') == r['asks_kind'])}/{len(asked_rows)}"
                for k in judges if judges[k] is not None}
    summary["名乗り・用件の質問を、どの判定もほかと読んだ（差し戻しうる）"] = (
        f"{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_YES and any(r.get(f'{k}_asks') == ASKS_OTHER for k in judges) and not any(r.get(f'{k}_asks') == ASKS_YES for k in judges))}"
        f"/{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_YES)}")
    summary["ほかの質問を差し戻せた（ほかの読みがあり、名乗り・用件の読みが無い）"] = (
        f"{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_OTHER and any(r.get(f'{k}_asks') == ASKS_OTHER for k in judges) and not any(r.get(f'{k}_asks') == ASKS_YES for k in judges))}"
        f"/{sum(1 for r in asked_rows if r['asks_kind'] == ASKS_OTHER)}")
    summary |= {f"{k}（前の言葉つき）": f"{sum(1 for r in rows if r['before'] and r.get(k) in r['accept'])}/{sum(1 for r in rows if r['before'])}"
                for k in judges if judges[k] is not None}
    # 取り次ぎの読み（流れの制御は、読みのどれか1つでも「取り次ぎを待つ」なら黙って待つ＝flowctl.HOLD_READINGS）。⛔ 2026-10-25 J10b：不在と取り次ぎの
    # 2つの意味の言葉で、どの読みも「取り次ぎを待つ」にならず、取り次ぎの最中にかけ直しの質問を流した ⇒ 取り次ぎを含む言葉では、どれかの読みが
    # 「取り次ぎを待つ」であること（効き目）／取り次ぎでない言葉に、尋ねているかの問い合わせが「取り次ぎを待つ」を返した回（間違えて黙る側）を数える
    from flowctl import HOLD
    held = [r for r in rows if HOLD in r["accept"]]
    summary["取り次ぎの言葉に、どれかの読みが取り次ぎ"] = (f"{sum(1 for r in held if any(r.get(c) == HOLD for k in judges for c in (k, f'{k}_asks')))}"
                                              f"/{len(held)}")
    summary["取り次ぎでない言葉に、尋ねているかが取り次ぎ"] = (f"{sum(1 for r in rows if HOLD not in r['accept'] for k in judges if r.get(f'{k}_asks') == HOLD)}"
                                               f"/{sum(1 for r in rows if HOLD not in r['accept']) * len(judges)}")
    # 判定の速さ（目的の人の判定の問い合わせ。中央・遅いほうから1割・最長の秒）
    for k in judges:
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
    p.add_argument("--pace-s", type=float, default=0.0, help="行ごとに待つ秒（無料の枠のキーが少ないとき。2本×1分15回なら 4.5）")
    p.add_argument("--tag", default="")
    args = p.parse_args()
    save_result(f"G-59{args.tag}", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
