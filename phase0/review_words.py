"""整理が済んでからの1回の測りに足す試しの言葉と正解（見直し (12) の組 Q。流さない＝料金はかけない。測る前に料金の見込みを出して伺う）。

材料：担当 D の報告の「測りに足す試しの言葉（正解つき）」と、帳面 docs/team/CL-1.md の 1 の (1) の ⏳ にある例（分身 E・G・I・J・N・O1・O2・Q の行）。
- ANSWER_WORDS：答えの段（G-53）。正解の表の行の形（g53_meaning._label の label＝accept・hedged・別のお話か AWAY・こちらへ尋ねた問いか QUERY・
  声かけへのお返事だけか PROMPT_REPLY）。読む口は g53_meaning.set_r（数えは flow_miss の1か所）。
- GROUND_WORDS：中身の判定（G-63）。いま伺っている質問と選択肢を添えた形（通話と同じ make_grounder の ground(…, asking, options)）と、選択肢だけ外した形・
  質問を添えない形の正解。読む口は g63_grounding.touch_cases。
- IMPORT_WORDS：取込の中身の判定（askitems.grounded の same＝元の質問と同じことを伺っているか）。
- TALK_WORDS：会話AIの通話の試験（G-57）の別のお話。会話AIへの決まり（flowctl.TALK_RECEIVE）の例の言葉（「うちは月曜が定休日なんですよ。」）と違う言葉を
  主に使い、支払日の事実の無い業務（KNOW_NO_DAY）でも流す（決まりの例を写しただけで通らないように＝担当 D の D-13）。
"""
from __future__ import annotations

from flowctl import (AWAY, AWAY_NO, AWAY_YES, DONT_KNOW_LABEL, HESITATING, PROMPT_REPLY, PROMPT_REPLY_NO, PROMPT_REPLY_YES, QUERY, QUERY_NO,
                     QUERY_YES, THINKING, WHY_NOT_SAID, Question, accepted_line, alternatives_form, aside_form, since_form)
from grounding import TOUCHED, UNTOUCHED, one_touched
from meaning_judge import UNDECIDED

PAY = "お支払いはカードと振込のどちらがよろしいでしょうか？"
PAY_REWORDED = "お支払いの方はカードと振込のどちらにされますか？"
PAY_LISTED = "カードになさいますかそれともお振込の方がよろしいでしょうか？"
CARD_PAY = ("カード", "振込")

# 業務の知っていること（中身の判定の材料）。支払日の事実のある業務と無い業務
KNOW = ["お支払い：カード・振込のどちらでも承っております。振込は月曜日以外でもご対応いただけます。カードは曜日を問わずお使いいただけます。",
        "振込手数料はお客様のご負担となります。", "ご予算：お客様ごとに異なりますのでご相談させていただいております。"]
KNOW_NO_DAY = ["お支払い：カード・振込のどちらでも承っております。", "振込手数料はお客様のご負担となります。"]


def questions() -> dict[str, Question]:
    """試しの言葉の質問（番号 → 質問）。形は流れの制御の1か所（aside_form・alternatives_form・since_form・accepted_line）から作る＝本番でお客様が聞く文。"""
    pay = Question("R1", PAY, "choice", conditions=CARD_PAY, required=True, reworded=PAY_REWORDED, alternatives=PAY_LISTED)
    budget = Question("R3", "ご予算はおいくらほどでしょうか？", "amount", conditions=("答えた",), required=True, since="ご予算はおいくらほど")
    person = Question("R4", "ご担当者様のお名前を教えていただけますでしょうか？", "name", conditions=("答えた",), required=True, asks_fact=True,
                      since="ご担当者様のお名前")
    # お知らせの中身を常体にした句（matter_clause）は、聞き取れないまま「-」にしたお知らせの締めの前の確かめ「（句）件のお返事のお声が聞き取れなかったのですが…」に
    # 入る（11-15 日本時間11時59分07秒の (3)。名詞と同じく取込で決まったとした例＝お知らせの文の中身を常体にした句）
    approval = Question("R7", "キャンセルについてご了承いただけますでしょうか？", "yes_no", conditions=("はい", "いいえ"), required=True, approval=True,
                        announce="当日のキャンセルは前日までにご連絡いただく必要がございますのでご了承ください。", matter="キャンセル",
                        matter_clause="当日のキャンセルは前日までにご連絡いただく必要がある")
    return {"R1": pay, "R2": Question("R2", "ご訪問の日にちはいつがよろしいでしょうか？", "date", conditions=("答えた",), required=True),
            "R3": since_form(budget), "R4": since_form(person), "R5": aside_form(pay), "R6": alternatives_form(pay), "R7": approval}


# わからない・答えたくない理由の正解（label の reason＝g53_labels.json の行と同じ欄・同じ決め方＝F5 の 11-15）：答えが「決められない」を含む行は、流れの制御が
# 理由の読みを使う（Flow._not_deciding）＝理由も正解を持つ。流れの制御で同じ動きになる理由の名前を並べる（理由を言っていない言葉＝相づち・別のお話・ほかのことを
# 話した・答えた、は「とは言っていない」「理由を言わなかった」「決められない」のどれでも同じ動き。迷っている言葉は「まだ決まっていない」との境目）
WHY_OTHER = (WHY_NOT_SAID, DONT_KNOW_LABEL["unspecified"], UNDECIDED)
WHY_HESITATING = (DONT_KNOW_LABEL["undecided"], *WHY_OTHER)


def _row(qid: str, text: str, accept, away=None, query=QUERY_NO, prompt=None, hedged=False, asked: str | None = None, why: str = "",
         reason=None) -> dict:
    label = {"accept": list(accept), "why": why}
    if hedged:
        label["hedged"] = True
    if reason:
        label["reason"] = list(reason)
    for key, value in ((AWAY, away), (QUERY, query), (PROMPT_REPLY, prompt)):
        if value is not None:
            label[key] = [value]
    return {"set": "R", "maker": "見直し (12)", "qid": qid, "text": text, "asked": asked, "label": label}


def answer_words() -> list[dict]:
    """答えの段（G-53）の試しの言葉と正解。"""
    qs = questions()
    accepted = accepted_line(qs["R7"])[1]
    unheard = accepted_line(qs["R7"], heard=False)[1]
    return [
        # 担当 D の表（基本の質問）
        _row("R1", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="聞かれたことと関わりのないご事情", reason=WHY_OTHER),
        _row("R1", "いやあ最近は暑くてねえ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER),
        _row("R1", "そういえばこの前の台風、大変だったんですよ。", [UNDECIDED], AWAY_YES, why="世間話", reason=WHY_OTHER),
        _row("R1", "そうですね。", [UNDECIDED, THINKING], AWAY_NO, reason=WHY_OTHER,
             why="相づちだけ＝答えの話。言いよどみ・言い終えていない言葉＝考えている途中も正解（11-06 11時19分55秒「(1) ア」で足した選べるもの・F5 の 11-15）。"
                 "「わからない」（unknown）は、わからない・まだ決めていない、とは言っていないので入れない（選べるものの説明のとおり）"),
        _row("R1", "振込じゃないほうがいいかなとは思いますけど", ["カード"], AWAY_NO, hedged=True, why="寄った考え（言い切っていない）"),
        _row("R1", "うーん、どっちがいいですかねえ。", [HESITATING, THINKING, UNDECIDED, "unknown"], AWAY_NO, None, reason=WHY_HESITATING,
             why="迷っている・考えている途中・こちらの考えを尋ねた（「〜ですかね」＝11-06 4時11分28秒の2つの意味）の境目（F5 の 11-15）。問いは付けない"
                 "＝どちらの読みも正解（正解の表の「〜のほうがいいですかね」の行と同じ）"),
        _row("R1", "振込だと手数料はどちら持ちですか？", [UNDECIDED], AWAY_NO, QUERY_YES, why="こちらへ尋ねた問い", reason=WHY_OTHER),
        _row("R1", "うちは振込だと手間なんですよね。", [UNDECIDED], AWAY_NO, why="答えの手がかりになるご事情", reason=WHY_OTHER),
        # ほかの形の質問（いちばん大事な境目＝日にちの質問へのご事情は答えの手がかり）
        _row("R2", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_NO, why="日にちの答えの手がかり", reason=WHY_OTHER),
        _row("R3", "はい、決まりました。", [UNDECIDED], AWAY_NO, why="その後の形に、値の無い「決まりました」", reason=WHY_OTHER),
        _row("R3", "うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, why="その後の形への別のお話", reason=WHY_OTHER),
        _row("R4", "はい、分かりました。", [UNDECIDED], why="事実を伺うその後の形に、値の無い「分かりました」", reason=WHY_OTHER),
        _row("R4", "はい、田中です。", ["答えた"], why="事実を言った"),
        _row("R5", "そうなんですね。", [UNDECIDED], AWAY_NO, why="お話に沿ったひとことのあとの相づちだけ", reason=WHY_OTHER),
        _row("R5", "じゃあ振込で。", ["振込"], AWAY_NO),
        _row("R6", "はい。", [UNDECIDED], AWAY_NO, why="並べた文への「はい」はどれも選んでいない", reason=WHY_OTHER),
        _row("R6", "お振込で。", ["振込"], AWAY_NO),
        _row("R6", "カードかなあ。", ["カード"], AWAY_NO, hedged=True),
        # 言い直し・最終の確かめ（分身 O2・N の行）
        # ご承認の質問（はい／いいえ）の最終の確かめは、判定の選べるものが「はい」「いいえ」（⛔ F5 の 11-15 まで、値の言い直しへの返事の名前 affirmed・denied で
        # 書いていた＝判定が選べない名前＝正しい読みも外れと数えた）
        {**_row("R7", "はい、大丈夫です。", ["はい"], asked=accepted, why="最終の確かめへのご了承"), "set": "D"},
        {**_row("R7", "いや、それはちょっと困るんですけど。", ["いいえ"], asked=accepted, why="ご了承いただけない"), "set": "D"},
        {**_row("R7", "はい、大丈夫ですよ。", ["はい"], asked=unheard, why="聞き取れなかったお知らせの最終の確かめへのご了承（分身 O2）"), "set": "D"},
        # 黙ったあとの一言のあと（分身 N・O2）
        _row("R7", "あ、それとですね、うちは月曜が定休日なんですよ。", [UNDECIDED], AWAY_YES, prompt=PROMPT_REPLY_NO, why="声かけへのお返事だけではない別のお話",
             reason=WHY_OTHER),
        _row("R7", "いえ、大丈夫です。", [UNDECIDED], AWAY_NO, prompt=PROMPT_REPLY_YES, why="声かけへのお返事だけ", reason=WHY_OTHER),
    ]


# 中身の判定（G-63）：いま伺っている質問「お支払いの方はカードと振込のどちらにされますか？」・選択肢カード・振込。touch＝選択肢を添えたときの正解・
# touch_plain＝質問だけ添えて選択肢を添えないときの正解（1つの選択肢に触れても「触れている」）・質問を添えない形は通す・止めるだけを数える
def _g(draft: str, want: bool, touch: str, asking: str = PAY_REWORDED, options=CARD_PAY, said: str = "うちは月曜が定休日なんですよ。") -> dict:
    plain = TOUCHED if touch not in (UNTOUCHED, "") else touch
    return {"said": said, "draft": draft, "want": want, "asking": asking, "options": list(options), "touch": touch, "touch_plain": plain}


GROUND_WORDS = [
    _g("さようでございますか。お振込でしたら月曜日以外でもご対応いただけますよ。", True, one_touched("振込")),
    _g("さようでございますか。カードでしたら曜日を問わずお使いいただけますよ。", True, one_touched("カード")),
    _g("さようでございますか。お支払いはカードでもお振込でも月曜日以外で問題ございませんよ。", True, TOUCHED),
    _g("さようでございますか。お支払いは月曜日以外でも問題ございませんよ。", True, UNTOUCHED),
    _g("さようでございますか。月曜日がお休みでいらっしゃるのですね。", True, UNTOUCHED),
    _g("さようでございますか。お振込ですね。", True, one_touched("振込")),
    _g("お振込は月曜日以外でもご対応いただけます。", True, one_touched("振込")),
    _g("本当に暑い日が続いておりますね。", True, UNTOUCHED, said="いやあ最近は暑くてねえ。"),
    _g("振込手数料はお客様のご負担となります。", True, one_touched("振込"), said="振込だと手数料はどちら持ちですか？"),
    _g("お支払いはカードと振込のどちらでも承っております。", True, TOUCHED, said="振込だと手数料はどちら持ちですか？"),
    _g("さようでございますか。ご予算はお客様ごとに異なりますのでご相談させていただいております。", True, TOUCHED,
       asking="ご予算はだいたいおいくらくらいでお考えですか？", options=(), said="うちは月曜が定休日なんですよ。"),
]

# 取込の中身の判定（askitems.grounded の same）：(元の質問, 選択肢, 型, 作った文, 同じことを伺っているか)
IMPORT_WORDS = [
    ("現在のお支払いはカードと振込のどちらでしょうか？", CARD_PAY, "choice", PAY_LISTED, False),
    (PAY, CARD_PAY, "choice", "お支払いの時期はいつ頃がよろしいですか？", False),
    (PAY, CARD_PAY, "choice", PAY_REWORDED, True),
    (PAY, CARD_PAY, "choice", PAY_LISTED, True),
]

# 会話AIの通話の試験（G-57）の別のお話：(業務の知っていること, お客様の言葉, 正解)。正解はどれも「お話の中身を受けたひとこと・知らない事実を言わない
# （中身の判定を通る）・問いかけと約束を入れない」。決まりの例の言葉は、支払日の事実の無い業務でだけ使う
TALK_WORDS = [
    (KNOW, "うちは水曜がお休みなんですよ。", "中身を受けたひとこと・知っていることだけ"),
    (KNOW, "最近ちょっと腰を痛めちゃってね。", "中身を受けたひとこと（お見舞い）・事実を言わない"),
    (KNOW_NO_DAY, "うちは水曜がお休みなんですよ。", "中身を受けたひとこと・支払日の事実を作らない"),
    (KNOW_NO_DAY, "うちは月曜が定休日なんですよ。", "中身を受けたひとこと・支払日の事実を作らない（決まりの例の答えを写さない）"),
]


def counts() -> dict[str, int]:
    """足した試しの言葉の数（表ごと）。G-63 は添える形ごと（選択肢つき・質問だけ・添えない）に流すので、その数も。"""
    return {"G-53": len(answer_words()), "G-63": len(GROUND_WORDS), "G-63（3つの形で流す行）": len(GROUND_WORDS) * 3,
            "取込の中身の判定": len(IMPORT_WORDS), "G-57": len(TALK_WORDS)}
