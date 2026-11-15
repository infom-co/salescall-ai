"""G-53：答えの意味の2つ目の判定（meaning_judge。DESIGN.md 6-10 の (6)）を、たくさんの言い方で測る。

✅ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
材料（どれも正解が決まっているもの）：
 (A) G-14 の記録の通話の言葉（会話のAIの聞き取りの文字と、Soniox の文字。G-15）＝正解は通話の記録の want
 (B) G-52 の答え 756個（G-48 の31問 × 選択肢ごとに6つ × 文字のモデル2つ。打ち消しを混ぜた言い方）＝正解はその選択肢
 (C) この試験で作る答え：わからない・答えたくない・どちらでも（選択肢から選ぶ質問だけ）・決められない（はい／いいえの質問だけ）を、
     文字のモデル2つ（3.7 Flash・3.6 Flash）に、質問ごとに3つずつ作らせる
 (D) 言い直しへの返事（2026-10-18。DESIGN.md 1章：言い直しへの返事も、最初の答えと同じ確かめに当てる）：G-48 の質問の選択肢ごと・値を聞く
     質問（VALUE_QS）ごとに、流れの制御が実際に言う言い直し（Flow._ask_confirm）への返事を、認める・直す・違うとだけ言う・迷う・わからない の
     5つの返し方で2つずつ、判定の候補に入れないモデル2つ（MAKERS_DE）に作らせる
 (E) 答えでない言葉（2026-10-18。DESIGN.md 6-8）：取り次ぎを待つように言う言葉・代わった人の最初の言葉を、MAKERS_DE に作らせ、G-50 の
     取り次ぎの場面の台詞も足す。最初の答えの場面と、言い直しの返事の場面の両方で判定させる
 (F) 電話を終える言葉の試しの表（end_words.ANSWER・2026-11-01 のご判断）の言葉＝正解は表のもの（答え・言い切っていないか・電話を終える言葉か）。
     ほかの組の電話を終える言葉かの正解は「言っていない」（当たりうる言葉だけ、付け直した正解の帳面に end を付けた＝end_accept）
判定は1つの答えに1回ずつ問い合わせる（本番と同じ形）。かかった時間も残す（通話の中で、会話のAIの報告を待つ間に間に合うか）。
判定のモデルが自分で作った答え（C の 3.6 Flash・3.7 Flash）は、そのモデルの数えに入れない。
"""
from __future__ import annotations

import glob
import json
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from common import HERE, KeyRing, base_parser, describe_error, require_python, save_partial, save_result, say
from flowctl import (ANY, END_DOUBT_WHY, before_words, CHOICE_TYPES, END, HEAR, SIDE_ITEMS, WHY, QUERY, PROMPT_REPLY, AWAY, ELSEWHERE, ELSEWHERE_YES, ELSEWHERE_NO, QUERY_YES, QUERY_NO, END_ANGRY, END_HURRY, END_NONE,
                     END_REASON, END_REFUSED, HANDOVER, HOLD, HOLD_BACK, LATER, NONE, NOT_ANSWERS, REPEAT, SYSTEM_CONDITIONS, THINKING, WAIT_KINDS,
                     WAIT_OR_LATER, WAIT_PLAIN, Flow, Question, JUDGMENTS, AWAY_NO, AWAY_YES, later_offer, off_talk, wait_reading)
from g48_meanings import QUESTIONS as G48
from meaning_judge import GROUPS, JUDGE_MODELS, OTHER_JUDGES, UNDECIDED, Reading, judge, judge_other

JUDGES = JUDGE_MODELS + tuple(OTHER_JUDGES) + tuple(GROUPS)   # 組の名前は、本番と同じ組の流れ（JudgePanel）で1件ずつ判定する（run_group）
BASE = "3.5 Flash Lite"  # いまの判定（2つ目の判定の候補は、これと同じ答えで比べる）
MAKERS = ("3.7 Flash", "3.6 Flash")
# D・E を作るモデル（表示名）：判定の候補（JUDGE_MODELS）に入れないもの（自分で作った答えを判定すると、正しさが高く出る）
MAKERS_DE = ("Gemini 3.5 Flash", "Gemini 3 Flash Preview")
# 値を聞く質問の言い直しへの返事（D）：(番号, 質問の文, 型, 言い直す値)
VALUE_QS = (("V1", "ご希望の日時をお聞かせいただけますでしょうか？", "datetime", "16日の午後2時"),
            ("V2", "何名様でのご来店でしょうか？", "number", "3人"),
            ("V3", "お電話番号をお伺いしてもよろしいでしょうか？", "phone", "090-1234-5678"),
            ("V4", "ご予算はおいくらくらいでしょうか？", "amount", "5万円"),
            ("V5", "ご来店のご希望日はいつでしょうか？", "date", "来週の火曜日"),
            ("V6", "ご住所をお伺いしてもよろしいでしょうか？", "address", "東京都港区芝5丁目"))
REPLY_KINDS = ("認める", "直す", "違うだけ", "迷う", "わからない")
KINDS = {"わからない": "unknown", "答えたくない": "refused", "どちらでも": ANY, "決められない": UNDECIDED}

MAKE_PROMPT = (
    "電話でお客様に、次の質問をしました。質問ごとに、お客様が次の答え方をするときの、自然な一言の答えを、それぞれ3つずつ書いてください。\n"
    "答え方：\n"
    "- わからない：知らない・覚えていない・確かめていない・まだ決めていない、と答える\n"
    "- 答えたくない：答えを断る\n"
    "- どちらでも：どれでもよいと、こちらに任せる（選択肢から選ぶ質問だけ。はい／いいえの質問には書かない）\n"
    "- 決められない：受け入れにも断りにも取れて、その言葉だけでは、どの選択肢か決められない一言（はい／いいえの質問だけ）\n"
    "決まり：選択肢の言葉そのもの（「カード」「はい」「いいえ」など）は入れません。電話で実際に言いそうな、短い言い方にします。\n"
    "質問（番号：質問の文／選択肢／質問の種類）：\n"
)


# G-52 の答えの正解の向きを、意味の言葉（G-48）にそろえる質問（打ち消しの問いの「はい」「いいえ」）：(質問, 作ったモデル。None＝どちらも)。
# ⛔ 2026-10-17 に見つけた：G-52 で答えを作ったモデルには意味の言葉を渡しておらず、「ご不明な点はございませんでしょうか？」の「はい」を
#    「分からない点がある」の向きで書いていた（意味の言葉は「はい」＝ご不明な点はないということ）。向きはモデルどうしでもそろわなかった
#    （「当日のキャンセルはございませんか？」は 3.8 Flash だけが逆）。⇒ 1つずつ読んで決めた（中身の向きで数える）
FLIP = {("G26", None), ("G27", None), ("G28", "3.8 Flash")}


def truth(it: dict) -> str:
    """答えの正解の1つ（数えの群の名前・付け直した正解の無い行の正解）。正解を行の label に持つ組（組 R など）は、その受け取ってよい読みの1つ目。
    ⛔ 2026-11-15 日本時間13時台：組 R の行で it["truth"] を読んで止まった（小さな試しの数え）"""
    if "truth" not in it and "label" in it:
        return it["label"]["accept"][0]
    if it["set"] == "B" and ((it["qid"], None) in FLIP or (it["qid"], it["maker"]) in FLIP):
        return {"はい": "いいえ", "いいえ": "はい"}[it["truth"]]
    return it["truth"]


_LABELS: dict | None = None


def accept(it: dict) -> list[str]:
    """受け取ってよい読み（正解。数えるのはすべてここ）。付け直した正解（g53_labels.json）があればそれ、無ければ truth の1つ。
    ⛔ 2026-11-15 F4 の (c)：「決められない」と「わからない」を同じに数えていた（どちらでも誤った値は渡らない、として）＝流れの制御の動きが違う
       （ご希望を伺う質問の最初の答えで「わからない」は迷いの点・分かる時を伺い、「決められない」は聞き直す）ので、正解は書いたとおりの名前だけ。
       2つの説明の境目は meaning_judge の1か所（SYSTEM_DESC["unknown"]・KIND_DESC[UNDECIDED]）・正解の表は境目の行だけ両方を持つ。
    ⛔ 2026-10-18 ユーザー：「『メールのほうが助かりますが、電話でも大丈夫です』なら…決まらないではないですよね」＝作らせたモデルの付けた
       正解を、中身を見ずに使っていた ⇒ CL-1 が1つずつ読んで付け直した（望みが言えていれば答え・事実がはっきりしなければ確かめる・
       どちらとも言っていなければ決められない。境目は、どちらの読みも正解）"""
    got = _label(it)
    if got:
        return got["accept"]
    return [truth(it)]


def _label(it: dict) -> dict | None:
    """付け直した正解（組 F は電話を終える言葉の試しの表の正解＝end_words.answer_truths・ほかは帳面 g53_labels.json）。正解を測る言葉と一緒に
    書いて持つ道具（前の話の答えの G-70 など）は、行の label（accept・either・talk・hedged・end）をそのまま正解にする＝数えはどの道具も
    flow_miss の1か所（正解の動きの決め方を写さない）。"""
    if "label" in it:
        return it["label"]
    if it["set"] == "F":
        from end_words import answer_truths
        return answer_truths().get((it["question"], it["text"]))
    global _LABELS
    if _LABELS is None:
        p = HERE / "g53_labels.json"
        _LABELS = json.loads(p.read_text(encoding="utf-8"))["labels"] if p.exists() else {}
    return _LABELS.get(f"{it['set']}|{it['qid']}|{it['text']}|{it.get('asked') or ''}")


def end_accept(it: dict) -> list[str]:
    """電話を終える言葉かの正解（受け取ってよい読み。数えるのはここ）：付け直した正解の end（組 F は表の正解）、無ければ「言っていない」。"""
    got = _label(it)
    return list(got["end"]) if got and got.get("end") else [END_NONE]


# 電話を終える言葉かの読みから、流れの制御がすること（Flow._end_due_now・_end_unsure の向き）。急いでいる＝急いでいる方への伺い（_hurry_ask）
END_ACTS = {END_REFUSED: "締める（もうかけない）", END_HURRY: "急いでいる方への伺い", END_ANGRY: "締める（人に引き継ぐ）"}
END_TOPIC, END_ASK = "いまの話の向きを伺う", "ご遠慮を伺う"


def end_act(q: Question, got, end) -> str | None:
    """判定の読み（答え got・電話を終える言葉か end）で、流れの制御がすること（読みが無ければ None）：1か所の数え act_of（本番の流れの制御）を、
    言葉の無い1つの発話（組 F の場面＝最初の答え）で通す。⛔ 2026-11-15 まで、電話を終える読みの向き（締める・急ぎを伺う・続ける・いまの話の向き・
    ご遠慮）を手で写していた（F1）。"""
    if end is None:
        return None
    r = Reading(got, True)
    setattr(r, END, end)
    return act_of({"set": "F", "qid": q.id, "text": ""}, q, r)


def end_step(act: str) -> bool:
    """流れの制御がすること act が、電話を終える読みによる動き（締める・急いでいる方への伺い・いまの話の向きを伺う・ご遠慮を伺う）か（添えた質問に先に答えて
    からの動きも同じ）。"""
    act = act.removeprefix(QUESTION_FIRST)
    return act in (END_TOPIC, END_ASK) or act in END_ACTS.values() or act.startswith("締める（")


def step_act(it: dict, q: Question, key: str) -> str | None:
    """1つの発話で、判定 key の読みから流れの制御がすること（数えるのはここ）：記録した読み（答え・言い切り・答えとは別に読む項目・前の話の答え）を
    本番と同じ入口で流れの制御に渡す（act_of の1か所）。電話を終える言葉・待つ・尋ねている・別のお話などの順は、流れの制御そのもの。"""
    r = reading(it, key)
    if r is not None:
        r.prior = prior_of(it, key, q)
    return act_of(it, q, r)


# 前の話の答えを、組の1か所で1回読んで入れる欄（read_priors。本番の判定の組 JudgePanel.ask と同じく、どの判定の読みにも同じものを添える）
PRIOR_KEY = "前の話"


def prior_of(it: dict, key: str, q: Question) -> str | None:
    """判定 key の読みに添える前の話の答え（唯一の読み口）：判定ごとに読んだもの（G-70 の行）が無ければ、組の1か所で読んだもの（read_priors）。
    本番が前の話を問う答え（meaning_judge.prior_asked の1か所＝前の言葉があり、選択肢の質問の最初の答え）のときだけ。⛔ 2026-11-15 日本時間13時台：
    前の言葉のあった頃（11-07 の基準の記録）に読んだ「無い」が、前の言葉の無くなった W1（F3＝システムの質問に前の話を渡さない）に残り、
    正しい読みを「違う答えで確かめる」と数えていた"""
    from meaning_judge import prior_asked
    if not prior_asked(q, it.get("asked"), it.get("before")):
        return None
    return it[f"{key}前の話"] if f"{key}前の話" in it else it.get(PRIOR_KEY)


def step_wants(it: dict, q: Question, has_end: bool) -> set[str]:
    """正解から決まる、1つの発話で流れの制御がすべきこと（どれをしても正しい）＝正解の表の正しい読みの組み合わせ（label_readings）を、読みの動き
    （step_act）と同じ1か所 act_of で流れの制御に通したもの。has_end＝判定の読みに電話を終える言葉かがある（無い記録は、電話を終える読みを読んで
    いない形で通す＝読みの動きと同じ）。"""
    return {act_of(it, q, r) for r in label_readings(it, q, has_end)}


def hedged(it: dict) -> bool:
    """お客様が、答えに寄っているが言い切っていないか（正解の「曖昧」の印。数えるのはここ）。付け直した正解にあればそれ、無ければ言い切って
    いない答えの決まり（flowctl.UNCERTAIN＝✅ ユーザー確定の言葉）に当たるか。✅ 2026-10-18 ユーザー：「yes,but 曖昧」"""
    from flowctl import UNCERTAIN

    got = _label(it)
    if got and "hedged" in got:
        return got["hedged"]
    return bool(UNCERTAIN.search(it["text"]))


def reading(it: dict, key: str):
    """記録した判定の答えを、流れの制御に渡す形（言い切っているかを添える）に戻す。"""
    from meaning_judge import Reading

    got = it.get(key)
    if got is None:
        return None
    r = Reading(got, it.get(f"{key}言い切り") is not False)
    for k, suffix in SIDE_SUFFIX.items():
        setattr(r, k, it.get(f"{key}{suffix}"))   # 答えとは別に読む項目の読み（記録の見出しの1か所 SIDE_SUFFIX。項目を足す前の記録は None＝読んでいない）
    # 確からしさ（返す判定だけ）：確かな読みかを本番と同じ決まり（meaning_judge.sure_reading）で数えるため。流れの制御は読まない
    r.sure, r.firm_p = it.get(f"{key}確からしさ"), it.get(f"{key}言い切りの確からしさ")
    return r


def query_truths(it: dict) -> tuple:
    """正解の表の行の、こちらへ尋ねた問いがあるか（flowctl.QUERY）の正解（中の名前の並び）。付けていない行は、どちらの読みも正解（数えない＝わからない理由の
    正解と同じ形）。✅ 2026-11-08 日本時間8時55分08秒 ユーザー「(イ) 判定に「こちらへ尋ねた問いがあるか」を1項目足し、1か所で読む」（⛔ それまでは言葉が
    「？」で終わるかで、正解の動きを「先に答える」にしていた＝本番の決まりと同じ言葉の形の写し）"""
    got = _label(it)
    return tuple((got or {}).get(QUERY) or (QUERY_YES, QUERY_NO))


def either(it: dict) -> bool:
    """言い切っていても、いなくても正しい答え（付け直した正解の either。境目＝どちらの読みも正解）。⚠️ 2026-10-31：「〜のほうがいいですかね」
    「決まってないわけではないです」「メールが埋もれてしまって」のように、答えは決まるが、言い切りかは人でも分かれるもの"""
    got = _label(it)
    return bool(got and got.get("either"))


# 答えとは別に読む項目（flowctl.SIDE_ITEMS）の読みを記録する見出し（{項目の名前: 判定の名前のあとに付ける語}。唯一の表＝記録 side_record・使い回すときに
# 外す語）。⛔ 2026-11-06 日本時間15時台まで、記録する3か所と外す語の並びが2つの項目を名前で手書きしていた（項目を足すと、記録の漏れで数えなくなる）
SIDE_SUFFIX = {END: "終わり", HEAR: "聞こえ", WHY: "理由", QUERY: "問い", PROMPT_REPLY: "声かけ", AWAY: "別の話", ELSEWHERE: "他社"}
assert set(SIDE_SUFFIX) == set(SIDE_ITEMS), "記録の見出しは、答えとは別に読む項目の表と同じ項目"
# 判定の確からしさの広がりを記録する見出し（{判定の名前のあとに付ける語: 読みの属性}。唯一の表＝記録と、使い回すときに外す語）。
# ✅ 2026-11-07 日本時間10時29分25秒 ユーザー「d1-ultraの確からしさが低いのは、こちらの設問か選択肢、説明の問題ということでしょ！」＝
# どの選べるものの間で割れたか・どの問いが低かったかを、記録から読めるようにする（決め方には使わない）
SPREAD_RECORD = {"選べるものの確からしさ": "probs", "問いごとの確からしさ": "confs"}


def side_record(it: dict, key: str, got) -> None:
    """判定 key の読み got の、答えとは別に読む項目を、行 it に記録する（唯一の場所）。"""
    for k, suffix in SIDE_SUFFIX.items():
        it[f"{key}{suffix}"] = getattr(got, k, None)


def why_accept(it: dict) -> list[str] | None:
    """わからない・答えたくない理由の正解（唯一の読み口。付け直した正解の reason のうち、選べる理由の名前＝flowctl.why_choices。無ければ None＝数えない）。
    ⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直し（J1）：正解の表の why（正解を付けた理由のメモの文）を理由の正解として読み、文の一部が一致するかで
    「正しい」と数えていた＝理由の正解は別の鍵 reason（正解の表にはまだ付いていない＝整理が済んでからの1回の測りの前に付ける）"""
    from flowctl import why_choices

    got = _label(it)
    names = set(why_choices(False))
    want = [w for w in (got or {}).get("reason") or [] if w in names]
    return want or None


def talk(it: dict) -> bool | None:
    """前の質問（と前の会話）への返事として会話になっているか（付け直した正解の talk。無ければ None＝なっている）。✅ 2026-10-31 ユーザー：
    「この部分だけの判断ならAIがあっています。前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」"""
    got = _label(it)
    return got.get("talk") if got else None


def mark(it: dict, key: str) -> str | None:
    """「曖昧」の印の数え：寄っている答えを正しく選んだときだけ（値が誤っていれば、印の前に誤り）。"""
    if it.get(key) not in accept(it) or it[key] in (UNDECIDED, "unknown", *NOT_ANSWERS) or either(it):   # 答えではない読みの並びは flowctl.NOT_ANSWERS の1か所
        return None
    unsure = it.get(f"{key}言い切り") is False
    if hedged(it):
        return "印が付いた" if unsure else "印が落ちた（値は正しい）"
    return "要らない印（最初の答えなら要らない確かめ）" if unsure else None


def asks_fact(text: str) -> bool:
    """取込で決める「事実を伺う質問か」（facts.decide）を、保存した2つのモデルの答え（G-71）から、取込と同じ決め方で決める（手で書かない）。
    G-71 に無い質問（電話を終える言葉の試しの表にしか無い質問）は、G-72 が同じ決め方で決めたもの。結果が無ければ偽（決める前の形＝G-71 を
    流す前の測りと同じ）。"""
    from facts import decide
    global _FACTS
    if _FACTS is None:
        files = sorted((HERE / "results").glob("G-71_*.json"))
        d = json.loads(files[-1].read_text(encoding="utf-8")) if files else {"questions": [], "rows": {}}
        _FACTS = {q: decide([v[i] for v in d["rows"].values()]) for i, q in enumerate(d["questions"])}
    return _FACTS.get(text, imported().get("facts", {}).get(text, False))


_FACTS = None
_G72 = None


def imported() -> dict:
    """取込で選択肢ごとに作る言葉の、保存した結果（G-72 の最新。唯一の読み場所）：いまの話の向きを伺う言葉（words.doubt）・こちらがすることの言葉
    （words.do）と、表にしか無い質問（new_questions）の意味の言葉（meanings）・事実を伺う質問か（facts）。どれも2つのモデルの言葉が確かめに通り、
    向きがそろったときだけ決まったもの（meanings.decide）。結果が無ければ空（作る前の形）。"""
    global _G72
    if _G72 is None:
        files = sorted((HERE / "results").glob("G-72_2*.json"))
        _G72 = json.loads(files[-1].read_text(encoding="utf-8")) if files else {}
    return _G72


def word_key(text: str, conditions) -> str:
    """取込で作る質問ごとの言葉の鍵（唯一の場所＝作る G-72 と読む with_words）：質問の文と選択肢の組。⛔ 2026-11-03 の見直しで見つけた：文だけを鍵に
    していたので、同じ文で選択肢の違う質問（G10「変更なし・変更あり」・G11「はい・いいえ」）の G11 に言葉が付かず、遠ざかる向きは G10 の名前だった。"""
    return f"{text}／{'・'.join(conditions)}"


def with_words(q: Question) -> Question:
    """質問に、取込で作った選択肢ごとの言葉（向きを伺う言葉・こちらがすることの言葉）と、目的から遠ざかる向きの選択肢を付ける（選択肢の型だけ。
    事実を伺う質問は作らない）。"""
    import dataclasses
    words, key = imported().get("words", {}), word_key(q.text, q.conditions)
    if q.type not in CHOICE_TYPES or q.asks_fact:
        return q
    return dataclasses.replace(q, doubt_words=dict(words.get("doubt", {}).get(key, {})), do_words=dict(words.get("do", {}).get(key, {})),
                               away=tuple(words.get("away", {}).get(key, ())))


def end_questions(facts: bool = True) -> list[Question]:
    """電話を終える言葉の試しの表（end_words）にしか無い質問（E1・E2…）。文と選択肢・意味の言葉・事実を伺う質問かは、取込と同じ形で作った G-72 の
    結果から（手で書かない）。"""
    d = imported()
    out = []
    for n, (text, conditions) in enumerate(d.get("new_questions", []), 1):
        kind = "choice" if ("どちら" in text or "どれ" in text) else "yes_no"
        out.append(Question(f"E{n}", text, kind, conditions=tuple(conditions), meanings=dict(d.get("meanings", {}).get(word_key(text, conditions), {})),
                            asks_fact=facts and asks_fact(text)))
    return out


def set_f() -> list[dict]:
    """電話を終える言葉の試しの表（end_words.ANSWER）の答えの段の言葉（同じ質問・同じ言葉は1つ＝業務の最終目的かは判定の読みを変えない）。
    質問は測りの質問の表から（question_by_text）。正解は表から読む（_label）。"""
    from end_words import ANSWER, answer_truths
    out, seen = [], set()
    for qtext, words, *_ in ANSWER:
        if (qtext, words) not in seen:
            seen.add((qtext, words))
            out.append({"set": "F", "maker": "表", "qid": question_by_text(qtext).id, "question": qtext, "text": words,
                        "truth": answer_truths()[(qtext, words)]["accept"][0]})
    return out


def g73_words() -> list[dict]:
    """G-73（ご承認いただけなかったときのシステムの質問への答えと、こちらの声が届いていない様子の試しの言葉。✅ 2026-11-02 日本時間23時15分58秒
    ユーザー「特にない」＝正解は作らせた種類のまま）の言葉（読み口は g73_system_words.latest の1か所）。"""
    from g73_system_words import latest
    return latest()["words"]


def set_n() -> list[dict]:
    """組 N1〜N3・W1：システムの質問（ご承認いただけなかったときの N1〜N3・ほかのご希望 W1）への答えの試しの言葉（G-73）。正解は作らせた種類
    （どちらとも取れない＝決められない）。質問の鍵と、本番で判定に渡る前の言葉は、場面の1か所（g73_scene）から。"""
    from g73_system_words import answer_scenes
    scenes = answer_scenes()
    return [g73_scene({"set": w["set"], "maker": w["maker"], "question": w["question"], "text": w["text"], "truth": w["truth"]})
            for w in g73_words() if w["question"] in scenes]


def g73_scene(it: dict) -> dict:
    """試しの言葉（G-73）の答えの段の答えに、場面の1か所（g73_system_words.answer_scenes）の質問の鍵と、本番で判定に渡る前の言葉を当てたもの
    （新しく読む組 set_n も、--reuse の前の記録も、ここを通す。場面の無い答えはそのまま）。⛔ 2026-11-05 日本時間20時台に見つけた：前の記録の
    N1〜N3 の言葉には前の言葉が無く、使い回すと本番と違う形で測ることになった"""
    from g73_system_words import answer_scenes
    sc = answer_scenes().get(it.get("question"))
    return {**it, "qid": sc["qid"], "before": sc["before"]} if sc else it


def set_r() -> list[dict]:
    """組 R（と言い直しへのお返事の組 D の行）：見直し (12) の組 Q で、整理が済んでからの1回の測りに足した試しの言葉と正解（review_words の1か所。正解は行の
    label＝数えは flow_miss の1か所・質問は review_words.questions＝qmap に足す）。"""
    from review_words import answer_words
    return answer_words()


def set_h() -> list[dict]:
    """組 HEAR：こちらの声が届いていない様子かの試しの言葉（G-73）。数えるのは聞こえ方の読みだけ（答えの正解は付けていない＝hear_truth）。
    質問は測りの質問の表から（question_by_text）。"""
    return [{"set": "HEAR", "maker": w["maker"], "qid": question_by_text(w["question"]).id, "question": w["question"], "text": w["text"],
             "truth": None, "hear_truth": w["truth"]} for w in g73_words() if w["set"] == "HEAR"]


def hear_truth(it: dict) -> str:
    """こちらの声が届いていない様子かの正解：組 HEAR は作らせた種類、ほかの組の言葉はどれも「そういう様子は無い」（ふつうの答え・言葉）。"""
    from flowctl import HEAR_OK
    return it.get("hear_truth") or HEAR_OK


def g48_question(n: int, text: str, conditions: tuple, meanings: dict, facts: bool = True) -> Question:
    """G-48 の質問を、流れの制御の質問の形に（選択肢から選ぶ質問＝「どちら」「どれ」を聞く文。ほかは、はい／いいえの型）。facts＝事実を伺う質問かを
    取込と同じ決め方で付ける（G-71 を流す道具だけ偽＝決める前の質問の文が要る）。"""
    kind = "choice" if ("どちら" in text or "どれ" in text) else "yes_no"
    return Question(f"G{n}", text, kind, conditions=conditions, meanings={c: w for c, w in meanings.items() if c in conditions},
                    asks_fact=facts and asks_fact(text))


def renamed() -> list[tuple[str, tuple]]:
    """文を直した G-48 の質問（元の一覧＝g48_meanings.QUESTIONS の文が、G-52 の材料の文と違う）：(文, 選択肢)。意味の言葉と事実を伺う質問かは、
    取込と同じ決め方で作り直す（G-72 の new_questions。唯一の決め方）。⛔ 2026-11-03：「前の結果に意味の言葉が無い質問」で決めていたので、作った次の
    流しでは作り直す質問から外れ、結果に残らず、その次の流しで意味の言葉が消えた（1回おき）。"""
    with open(sorted(glob.glob(str(HERE / "results" / "G-52_*.json")))[-1], encoding="utf-8") as fh:
        d = json.loads(fh.read())
    return [(text, tuple(c)) for (text, c), (was, _) in zip(G48, d["questions"]) if text != was]


def g48_questions(facts: bool = True) -> list[Question]:
    """G-48 の質問（G1〜）。文と選択肢は元の一覧（g48_meanings.QUESTIONS）から、意味の言葉は G-52 の材料から（文が同じときだけ）。文を直した質問の
    意味の言葉は、取込と同じ決め方で作り直したもの（G-72 の meanings）。⚠️ お客様の答えの材料（組 B）と正解は、質問の番号のまま（お客様の言葉）。
    ✅ 2026-11-03 ユーザー：「「折り返し」だとすぐだと思われ断られる可能性が高いので、「また」に変更するように言ったよね！」（G7）"""
    with open(sorted(glob.glob(str(HERE / "results" / "G-52_*.json")))[-1], encoding="utf-8") as fh:
        d = json.loads(fh.read())
    redone = imported().get("meanings", {})
    out = []
    for n, ((text, c), (was, _), m) in enumerate(zip(G48, d["questions"], d["meanings"]), 1):
        out.append(g48_question(n, text, tuple(c), m if text == was else dict(redone.get(word_key(text, c), {})), facts))
    return out


def set_b(qs: list[Question]) -> list[dict]:
    d = json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-52_*.json")))[-1], encoding="utf-8").read())
    out = []
    for maker, per_q in d["answers"].items():
        for q, got in zip(qs, per_q):
            for cond, texts in got.items():
                out += [{"set": "B", "maker": maker, "qid": q.id, "text": t, "truth": cond} for t in texts]
    return out


def make_c(client, model: str, qs: list[Question]) -> list[dict]:
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "kind": types.Schema(type=types.Type.STRING, enum=list(KINDS)),
        "answers": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["no", "kind", "answers"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    body = "\n".join(f"{n}：{q.text}／{'・'.join(q.conditions)}／{'選択肢から選ぶ質問' if q.type == 'choice' else 'はい／いいえの質問'}"
                     for n, q in enumerate(qs, 1))
    r = client.models.generate_content(model=model, contents=MAKE_PROMPT + body, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.7))
    out = []
    for x in json.loads(r.text).get("items", []):
        n, kind = x.get("no"), x.get("kind")
        if not (isinstance(n, int) and 1 <= n <= len(qs)) or kind not in KINDS:
            continue
        q = qs[n - 1]
        if (kind == "どちらでも") != (q.type == "choice") and kind in ("どちらでも", "決められない"):
            continue  # 種類に合わない答え方（はい／いいえの質問の「どちらでも」・選ぶ質問の「決められない」）は使わない
        out += [{"set": "C", "maker": model, "qid": q.id, "text": t.strip(), "truth": KINDS[kind]} for t in x.get("answers", [])[:3] if t.strip()]
    return out


def question_map(facts: bool = True, words: bool = True) -> dict:
    """答えの段の測りが使う質問の表（唯一の場所。測りの道具はどれもここから取る＝同じ質問に、取込で決める事実を伺う質問か＝asks_fact も同じに付く）：
    G-48 の質問・値を聞く質問・業務の試しの質問（Q1・Q2・Q3・Q5）と、words のとき、電話を終える言葉の試しの表にしか無い質問（E1・E2）。words の
    ときは、選択肢ごとの言葉（向きを伺う言葉・こちらがすることの言葉＝G-72）も付ける。電話を終える言葉の試しの表（end_words）も、質問はここから取る。
    取込の言葉を作る道具（G-71・G-72）は words を偽にする（作る前の質問の表が要る）。⛔ 2026-11-01：同じ表を5つの道具が別々に書いていた・
    ⛔ 2026-11-02：電話を終える言葉の表が、意味の言葉・事実を伺う質問か・こちらがすることの言葉を、取込とは別に手で書いていた（「大丈夫だということ」
    ⇔ 取込は「よろしいということ」）"""
    import dataclasses

    from g14_listen import business
    biz = [business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")]
    biz = [dataclasses.replace(q, asks_fact=facts and asks_fact(q.text)) for q in biz]
    qs = g48_questions(facts) + value_questions(facts) + biz
    if words:
        qs = [with_words(q) for q in qs + end_questions(facts)]
    # 曖昧な言葉の意味も、本番の取込と同じく業務の質問の全部に、同じ記録・同じ決め方（g14_listen.replies_for）で付ける。⛔ 2026-11-07 日本時間
    # 11時台まで、業務の試しの質問（Q1〜Q5）にだけ付いていて、同じ文の G8 の「結構です。」を本番と違う形で数えていた
    from g14_listen import replies_for
    qs = [dataclasses.replace(q, reply_meanings=replies_for(q) if q.conditions else {}) for q in qs]
    # ご承認いただけなかったときのシステムの質問（N1〜N3＝流れの制御の callback_question の1か所。試しの言葉は G-73＝組 N1〜N3）
    from flowctl import callback_question
    qs += [callback_question(r) for r in ("not_agreed", "other_point", "agreed_now")]
    out = {q.id: q for q in qs}
    if words:   # ほかのご希望を伺う W1（試しの言葉は G-73＝組 W1。元の質問の意味の言葉が要る＝words のときだけ）
        from g73_system_words import wish_scenes
        out.update({key: w for key, (_, w, _) in wish_scenes(out).items()})
    return out


def question_by_text(text: str) -> Question:
    """質問の文から、測りの質問の表（question_map）の質問（同じ文は最初のもの）。無ければ KeyError。"""
    by: dict = {}
    for q in question_map().values():
        by.setdefault(q.text, q)
    return by[text]


def value_questions(facts: bool = True) -> list[Question]:
    return [Question(n, text, kind, conditions=("答えた",), asks_fact=facts and asks_fact(text)) for n, text, kind, _ in VALUE_QS]


def asked_of(q: Question, cond: str | None, value: str | None = None) -> str | None:
    """流れの制御が、その答えを言い直して確かめるときに実際に言う言葉（Flow._ask_confirm＝言い直しの場面と同じ1か所 restated。言い直せなければ None）。"""
    r = restated(q, cond, value)[1]
    return r.get("say") if r.get("result") == "confirm_required" else None


DE_PROMPT = (
    "電話でお客様に質問をして、お客様の答えを、こちらが言い直して確かめました。確かめごとに、お客様が次の返し方をするときの、自然な一言の返事を、"
    "返し方ごとに2つずつ書いてください。\n"
    "返し方：\n"
    "- 認める：言い直した内容で合っていると認める（「はい」だけでなく、いろいろな言い方で）\n"
    "- 直す：違うと言い、正しい答えを言う（選択肢の質問は、言い直したのと別の選択肢の意味を言い、その選択肢の名前を choice に入れる。値の質問は、別の値を言う）\n"
    "- 違うだけ：違うとだけ言い、正しい答えは言わない\n"
    "- 迷う：認めるとも違うとも決めきれない（あいまいに言う）\n"
    "- わからない：わからない・覚えていない\n"
    "決まり：電話で実際に言いそうな、短い言い方にします。言い直した言葉をそのまま繰り返すだけの返事は書きません。\n"
    "確かめ（番号：質問の文／こちらが言い直した言葉／選択肢）：\n"
)
E_PROMPT = (
    "電話でお客様に質問をしたところ、次のような、答えではない言葉が返ってくることがあります。それぞれ、自然な一言を8つずつ書いてください。\n"
    "- 取り次ぐ：その場の人が、担当の人などに電話を代わるので、待つように言う（「担当に代わりますので少々お待ちください」のような言い方。"
    "この例そのものは書かない）\n"
    "- 代わった：電話を代わった別の人が、最初に話す言葉（名乗る・用件を聞く など。この例「お電話代わりました」そのものは書かない）\n"
    "決まり：電話で実際に言いそうな、短い言い方にします。質問への答えは入れません。\n"
)


def make_d(client, model: str, qs: list[Question]) -> list[dict]:
    """(D) 言い直しへの返事。正解：選択肢の質問＝認める→言い直した選択肢・直す→作ったモデルが言った選択肢・違うだけ→はい／いいえの質問は
    もう片方（ほかの質問は決められない）・迷う→決められない・わからない→わからない。値の質問＝認める→合っている・直す／違うだけ→違う値を
    言われた・迷う／わからない→決められない（どれも「合っている」ではない）。"""
    from google.genai import types

    pairs = []  # (質問, 言い直した選択肢, 言い直した言葉)
    for q in qs:
        if q.type in CHOICE_TYPES:
            pairs += [(q, c, a) for c in q.conditions if (a := asked_of(q, c))]
        else:
            pairs.append((q, None, asked_of(q, "答えた", dict((n, v) for n, *_, v in VALUE_QS)[q.id])))
    ans = types.Schema(type=types.Type.OBJECT, properties={"text": types.Schema(type=types.Type.STRING),
                                                           "choice": types.Schema(type=types.Type.STRING)}, required=["text"])
    item = types.Schema(type=types.Type.OBJECT, properties={
        "no": types.Schema(type=types.Type.INTEGER), "kind": types.Schema(type=types.Type.STRING, enum=list(REPLY_KINDS)),
        "answers": types.Schema(type=types.Type.ARRAY, items=ans)}, required=["no", "kind", "answers"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    body = "\n".join(f"{n}：{q.text}／{a}／{'・'.join(q.conditions) if q.type in CHOICE_TYPES else '（値の質問）'}"
                     for n, (q, _, a) in enumerate(pairs, 1))
    r = client.models.generate_content(model=model, contents=DE_PROMPT + body, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.7))
    out = []
    for x in json.loads(r.text).get("items", []):
        n, kind = x.get("no"), x.get("kind")
        if not (isinstance(n, int) and 1 <= n <= len(pairs)) or kind not in REPLY_KINDS:
            continue
        q, c, asked = pairs[n - 1]
        for a in x.get("answers", [])[:2]:
            text = (a.get("text") or "").strip()
            if not text:
                continue
            if q.type not in CHOICE_TYPES:
                want = {"認める": "affirmed", "直す": "corrected", "違うだけ": "corrected"}.get(kind, UNDECIDED)
            elif kind == "認める":
                want = c
            elif kind == "直す":
                want = a.get("choice") if a.get("choice") in q.conditions and a.get("choice") != c else None
            elif kind == "違うだけ":
                others = [o for o in q.conditions if o != c]
                want = others[0] if q.type == "yes_no" and len(others) == 1 else UNDECIDED
            else:
                want = UNDECIDED if kind == "迷う" else "unknown"
            if want is not None:  # 直す の選択肢が読めない答えは使わない
                out.append({"set": "D", "maker": model, "qid": q.id, "text": text, "asked": asked, "truth": want, "kind": kind,
                            "asked_choice": c})
    return out


def make_e(client, model: str, qs: list[Question]) -> list[dict]:
    """(E) 答えでない言葉。1つの言葉を、最初の答えの場面と、言い直しの返事の場面（選択肢の質問を順に）で判定させる。G-50 の台詞も足す。"""
    from google.genai import types
    from make_callee_samples import UTTERANCES as LINES

    item = types.Schema(type=types.Type.OBJECT, properties={
        "kind": types.Schema(type=types.Type.STRING, enum=["取り次ぐ", "代わった"]),
        "answers": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))}, required=["kind", "answers"])
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=item)}, required=["items"])
    r = client.models.generate_content(model=model, contents=E_PROMPT, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.7))
    lines = [(t.strip(), {"取り次ぐ": HOLD, "代わった": HANDOVER}[x["kind"]]) for x in json.loads(r.text).get("items", [])
             if x.get("kind") in ("取り次ぐ", "代わった") for t in x.get("answers", [])[:8] if t.strip()]
    if model == MAKERS_DE[0]:
        lines += [(LINES["transfer"], HOLD), (LINES["taken_over"], HANDOVER)]
    choice_qs = [q for q in qs if q.type in CHOICE_TYPES]
    out = []
    for i, (text, want) in enumerate(lines):
        q = choice_qs[i % len(choice_qs)]
        out.append({"set": "E", "maker": model, "qid": q.id, "text": text, "truth": want, "asked": None})
        q2 = choice_qs[(i + 7) % len(choice_qs)]
        out.append({"set": "E", "maker": model, "qid": q2.id, "text": text, "truth": want, "asked": asked_of(q2, q2.conditions[0])})
    return out


def set_a() -> list[dict]:
    """G-14 の記録の通話の言葉（質問ごとに重ねない）。正解は、その言葉が来た通話の want（曖昧な答えは「決められない」）。"""
    from g15_ear import live_calls

    soniox = {r["wav"]: r.get("ear") for r in json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-15-soniox_*.json")))[-1],
                                                             encoding="utf-8").read())["rows"]}
    seen, out = set(), []
    for c in live_calls():
        qid, want = c.get("question") or "Q1", c.get("want")
        if want in (None, "follow") or qid == "Q4":
            continue
        for src, text in (("会話AI", c["heard"]), ("Soniox", soniox.get(c["wav"]))):
            if text and (qid, text) not in seen:
                seen.add((qid, text))
                out.append({"set": "A", "maker": src, "qid": qid, "text": text, "truth": UNDECIDED if want == "ambiguous" else want})
    return out


def misread_as_affirmed(it: dict, key: str) -> bool:
    """言い直しへの返事を、認めていないのに認めたと読んだ（受け取ると誤りになる向き＝G-53 の決め手。数えるのも、先に判定させるのも、ここ）。"""
    got = it.get(key)
    return it["set"] == "D" and got not in accept(it) and (got == "affirmed" or got == it.get("asked_choice"))


def dangerous(it: dict, q: Question, key: str) -> bool:
    """受け取ると誤りになる向きに読んだ（言い直しへの返事は misread_as_affirmed・ほかは、正解でない選択肢か「どちらでも」を選んだ）。
    最初の答えで、言い切っていないと読んだものは、流れの制御が言い直して確かめるので入れない（Flow._judged_unsure）。"""
    if it["set"] == "D":
        return misread_as_affirmed(it, key)
    got = it.get(key)
    return got not in accept(it) and q.offers(got) and it.get(f"{key}言い切り") is not False   # 選べる答えかは本番と同じ1か所（Question.offers）


# 流れの制御がすること（判定1つの読みから。Flow._agree の向き：最初の答えは、言い切っていなければ念のため確かめる・決められなければ聞き直す／
#    言い直しへの返事は、認めたら受け取る（言い切っていなければ「曖昧」の印）・打ち消したら言い回しを変えて確かめ直す・決められなければ「-」で
#    締めの前に聞き直す）。✅ 2026-10-30 ユーザー：「何が問題で間違えたのかを徹底的に洗い出し…」＝判定の答えの正誤だけでなく、お客様に聞こえる
#    動きの正誤で数える（決められない言葉を判定が「寄っているが言い切っていない」と読んでも、流れの制御は確かめるので、お客様に聞こえる動きは同じ）
ASK_AGAIN, AFFIRMED, AFFIRMED_HEDGED, DENIED = "聞き直す", "認めた（受け取る）", "認めた（受け取る・曖昧の印）", "打ち消した（確かめ直す）"
# 言い直しへの返事が会話になっていず、判定の読みに選択肢がある：その選択肢で、念のため確かめ直す（Flow._confirm_again ⇒ _check_again）
RECHECK = "会話になっていない（念のため確かめ直す）"
# 言い直さない質問への会話がつながらない答えで、判定の読みの選択肢で意味合いを尋ねる（flowctl.asks_sense の1か所＝Flow._check_again。✅ 2026-11-05 日本時間
# 23時56分48秒 ユーザー「ア」）
SENSE = "意味合いを尋ねる："
# 待つ・かけ直すの読み（答えの段だけ＝flowctl.WAIT_KINDS）で流れの制御がすること（Flow._wait_route の1か所と同じ。✅ 2026-11-05 日本時間23時19分06秒・
# 23時56分48秒 ユーザー（会話2）。取り次ぎを待つも「はい、」のあと待つ＝✅ 2026-11-06 日本時間2時28分41秒 ユーザー「「はい、」のような相づちがないと失礼です。」）
# まだ考えている途中（✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(1) ア」＝Flow._thinking）：「はい。」のあと待つ
# 理由を言わずに待つ（✅ 2026-11-15 日本時間21時55分35秒 ユーザー（伺い24）＝Flow._hold の plain）：「はい、」のあと待ち、黙られたら一言・しばらくして待つかかけ直すかを伺う
# （黙ったときの場面 "wait"＝flowctl.QUIET_STEPS）
WAIT_ACT = {HOLD: "「はい、」のあと待つ", HOLD_BACK: "待つ一言のあと待つ", WAIT_PLAIN: "「はい、」のあと待つ（黙られたら一言のあと待つかかけ直すかを伺う）",
            LATER: "かけ直しを伺う", WAIT_OR_LATER: "待つかかけ直すかを1回伺う", THINKING: "「はい。」のあと待つ"}
# お客様のほうから折り返すと申し出られた（お礼の一言を言うかは flowctl.later_offer の1か所）：「お気遣いありがとうございます。…」のあとかけ直しを伺う
THANKED = "お礼の一言のあと"


def wait_act(got: str) -> str:
    """2つの判定がそろって待つ・かけ直すの読み got と読んだときに流れの制御がすること（唯一の数え）。組み合わせ（flowctl.wait_reading）とお礼の一言
    （flowctl.later_offer）は本番の1か所を通す。⛔ 2026-11-06 日本時間23時22分41秒のご質問の見直しで見つけた：16時台に判定の選べるものへ「お客様から
    折り返す」（LATER_THEIRS）を足したとき、この表に入れず（数えが止まった）、入れてもお礼の一言の有無を見ない形だった"""
    check = dict.fromkeys(JUDGMENTS, got)
    act = WAIT_ACT[wait_reading(check)]
    return THANKED + act if later_offer(check, None) == "callback" else act
# 答えの代わりにこちらへ尋ねた（「〜ですかね？」＝flowctl.asks_us の1か所・流れの制御は入口 Flow._apply_one で報告より先に答えさせる）：尋ねられたことに先に答え、
# お客様の返事を待つ（✅ 2026-11-06 日本時間4時23分33秒 ユーザー：「…こちらは「そうですね、お読みになられるとより理解が深まるかと思います。」のような返答が自然です。」）
ANSWER_FIRST = "尋ねられたことに先に答える"
# もう一度言ってほしいと頼まれた（flowctl.REPEAT・流れの制御は Flow._repeat_now の1か所）：「失礼いたしました。」のあと、伺ったことを流し直す（✅ 2026-11-06 日本時間
# 15時20分30秒 ユーザー「(1)…OK」）
REPEAT_ACT = "「失礼いたしました。」のあと伺ったことを流し直す"
# ほかに分からない点が無いかを伺う質問への、言い切っていない「無い」（flowctl.takes_for_now の1か所＝流れの制御 Flow._keep_toward_goal・_tell_due）：確かめ直さずに
# 受け取り、「何かわからないことがございましたら…」を添える（✅ 2026-11-07 日本時間13時05分06秒 ユーザー）。言い切った「無い」は「受け取る：」のまま
ANYTIME_TAKE = "一言を添えて受け取る："
# 迷っていると言い終えた（flowctl.hesitates の1か所＝流れの制御 Flow._hesitation）：選択肢ごとの良い点・気をつける点（Question.option_notes）があれば選ぶ助けを
# 伝え（True）、無ければ「どういった点でお迷いでしょうか？」を1回伺う（False）。✅ 2026-11-07 日本時間13時05分06秒・18時03分49秒 ユーザー「(1) ア」
HESITATE_ACT = {True: "選ぶ助けを伝える", False: "どういった点でお迷いかを伺う"}


# 別のお話にひとこと応えて「ところで」で伺い直す動き（流れの制御 Flow._unlinked_base の番＝flowctl.away_turn の1か所）
AWAY_ACT = "別のお話にひとこと応えて伺い直す"


def away_truths(it: dict) -> tuple:
    """正解の表の行の、別のお話か（flowctl.AWAY）の正解（中の名前の並び）。付けていない行は、どちらの読みも正解（query_truths と同じ形）。"""
    got = _label(it)
    return tuple((got or {}).get(AWAY) or (AWAY_YES, AWAY_NO))


def elsewhere_truths(it: dict) -> tuple:
    """正解の表の行の、ほかの会社に頼んでいる・使っていると言ったか（flowctl.ELSEWHERE）の正解（中の名前の並び）。✅ 2026-11-15 日本時間21時55分35秒 ユーザー
    （伺い25）で足した項目。付けていない行は「言っていない」（⚠️ 一括で付けた既定＝帳面 0-3 の (2) の外。ほかの会社のことを言いうる行は候補の一覧にして、答えを伏せた
    読み手2人の読み直しのあとに正解を付ける＝それまでの測りでは、候補の行の「伺う」を外れと数えうる。どちらの読みも正解にすると、ほかの会社のことを言っていない
    行にも「伺う」が正しい動きに入る）。"""
    got = _label(it)
    return tuple((got or {}).get(ELSEWHERE) or (ELSEWHERE_NO,))


# ── F1：測りの数えは、行の場面を本番の流れの制御（Flow）に組み、判定の読みを本番と同じ入口で渡し、返事から動きの名前を作る1か所（唯一の数え）。
#    ✅ 2026-11-08 日本時間11時45分51秒 ユーザー「はい」（F1・F5 → 1回の測りの見込み）。⛔ それまでの flow_act・flow_wants・step_act・end_act は、流れの制御を
#    呼ばずに共通の関数を並べる順を手で写していた（本番の直しを測りへ写し直す修正が要る形＝11-06 の5つのコミットで3回。11-15 に全部の行で比べて写しの外れを
#    分けた＝PHASE0 G-53 の 11-15 の段）。
SCENE_NEXT = Question("ZZ", "お支払いはカードと振込のどちらがよろしいでしょうか？", "choice", conditions=("カード", "振込"))
# 値を伺う型の、会話AIが渡す値の文字（判定は値の文字を読まない＝値の文字だけは会話AIが渡す。11-08 12時11分40秒）：(はじめの値, 直された値)
SCENE_VALUES = {kind: (value, again) for (_, _, kind, value), again in zip(VALUE_QS, ("17日の午後3時", "4人", "090-9876-5432", "6万円", "来週の水曜日", "東京都港区芝6丁目"))}
SCENE_VALUES.update({"name": ("田中", "佐藤")})
SCENE_MISSING = "場面を組めない："   # 本番の流れの制御で場面を組む作りがまだ無い行（数えずに、この名前で出す＝数え漏れを黙って出さない）


class NoScene(Exception):
    """行の場面を、本番の流れの制御で組めない（理由つき）。"""


def _scene_value(q: Question, again: bool = False) -> str:
    return SCENE_VALUES.get(q.type, ("15日の14時", "16日の15時"))[again]


def restated(q: Question, cond: str | None, value: str | None = None, flow: Flow | None = None) -> tuple[Flow, dict]:
    """その答えを言い直して確かめる所の流れの制御と、その返事（唯一の場所＝言い直しの文 asked_of と、言い直しへの返事の場面 flow_scene）。
    言い直しは流れの制御の1か所（Flow._ask_confirm）。"""
    f = flow or Flow([q])
    f.items[q.id].condition, f.items[q.id].value = cond, value
    return f, f._ask_confirm(q.id, after="ambiguous")


def _say(f: Flow, words: str, op: str, **args) -> dict:
    """場面の台本の1つの発話（会話AIの報告で進める＝試験 ApprovalTest・NoneOfThemTest の場面と同じ）。"""
    f.on_callee(); f.on_heard(words)
    return f.apply(op, args)


def _approval_scene(step: str) -> Flow:
    """ご承認いただけなかったときの手順（N1〜N3）の場面＝試しの言葉を作った場面（g73_system_words の APPROVAL_SAID・EXPLAINED）と同じ台本。"""
    from flowctl import CALLBACK_IDS
    from g73_system_words import APPROVAL_SAID, EXPLAINED
    f = Flow([Question("Q1", "資料はお手元に届いていますでしょうか？", "yes_no", conditions=("はい", "いいえ"), asks_fact=True),
              Question("Q2", "以上の内容でよろしいでしょうか？", "yes_no", conditions=("はい", "いいえ"), approval=True, ask_if=("Q1", ("いいえ", "unknown"))),
              SCENE_NEXT], play_recordings=True, answers={"料金": EXPLAINED})
    _say(f, APPROVAL_SAID[0], "record_answer", question_id="Q1", condition_id="いいえ")
    _say(f, APPROVAL_SAID[1], "record_answer", question_id="Q2", condition_id="いいえ")
    if step != "not_agreed":
        _say(f, APPROVAL_SAID[2], "record_answer", question_id=CALLBACK_IDS["not_agreed"], condition_id="点を言った")
        f.apply("replay_opening", {"part": "answer_料金"})
        _say(f, APPROVAL_SAID[3], "replay_opening", part="resume")
        if step == "agreed_now":
            _say(f, APPROVAL_SAID[4], "record_answer", question_id=CALLBACK_IDS["other_point"], condition_id="unknown")
    return f


def flow_scene(it: dict, q: Question) -> tuple[Flow, str]:
    """行 it（質問 q）の場面：質問を伺って、お客様の言葉を待っている所までの本番の流れの制御と、答えを待っている質問の番号（唯一の場面の組み立て）。
    組めない行は NoScene。"""
    from flowctl import CALLBACK_IDS
    from g73_system_words import wish_scenes
    s = it.get("set")
    if it.get("prompted"):
        raise NoScene("黙ったあとの一言のある行")
    if s in ("N1", "N2", "N3"):
        role = {v: k for k, v in CALLBACK_IDS.items()}[s]
        f = _approval_scene(role)
    elif s == "W1" or "@" in str(it.get("qid")):
        scenes = wish_scenes(question_map())
        if it.get("qid") not in scenes:
            raise NoScene(f"ほかのご希望の場面が無い（{it.get('qid')}）")
        of, w, said = scenes[it["qid"]]
        f = Flow([of, SCENE_NEXT], play_recordings=True)
        f.use_checks("judge")
        firm = of.id == "G17"   # 組 G17 は「どちらも要らない」と言い切った・組 E1 は言い切らずに断った（試しの場面の作り＝g73_system_words.WISH_SCENES）
        r = Reading(NONE if of.id == "G17" else "いいえ", firm)
        setattr(r, QUERY, QUERY_NO)
        f.on_callee(); f.on_heard(said); f.on_check("judge", r); f.on_decided()
        f.apply("record_answer", {"question_id": of.id, "condition_id": "unknown"})
    elif s == "R" and it.get("qid") not in ("R1", "R2"):
        raise NoScene(f"見直し (12) の形の質問（{it.get('qid')}）")
    else:
        f = Flow([q, SCENE_NEXT], play_recordings=True)
        if s == "D":
            if it.get("qid") == "R7":
                raise NoScene("ご承認のお知らせの最終の確かめ（R7）")
            said = restated(q, it.get("asked_choice") if q.type in CHOICE_TYPES else "答えた", None if q.type in CHOICE_TYPES else _scene_value(q), f)[1]
            if said.get("result") != "confirm_required":
                raise NoScene(f"言い直せない答え（{it.get('asked_choice')}）")
            if q.type in CHOICE_TYPES and f.items[q.id].condition != it.get("asked_choice"):
                # 今の本番は、この答えを目的に進む向きの別の答えで言い直す（「それでは予定通りの方向で…」）＝判定が読んだ記録の言い直し（「念のため…変更が
                # おありということで…」）と向きが違う場面は組めない（数えない。次の測りの前に組 D の言い直しの文を今の本番で作り直す＝帳面 1 の (1) の F1）
                raise NoScene(f"記録の言い直しと今の本番の言い直しの向きが違う（{it.get('asked_choice')}→{f.items[q.id].condition}）")
            return f, q.id
    cur = f.current
    if cur is None or f.questions[cur].text != q.text:
        raise NoScene(f"場面の質問が違う（{cur}）")
    return f, cur


def _neutral_report(f: Flow, qid: str, r) -> tuple[str, dict]:
    """測りで置く会話AIの報告（読みに左右されない中立の形）：会話AIは判定に加えない（✅ 2026-11-08 日本時間12時11分40秒 ユーザー「そもそも会話 AI がなぜ判定に
    加わっているんですか？」）＝答えの意味は判定の組の読みで決まり、会話AIの報告は判定を待つきっかけと値の文字だけ。言い直しへの返事は「合っているかは言えない」
    （no_answer）、最初の答えは選ぶ質問なら「わからない」、値を伺う型なら「答えた」と値の文字。判定が「違う値を言われた」と読んだ言い直しへの返事だけは直した値の
    文字を渡す（値の文字は会話AIが渡す＝渡さなければ報告し直させる作り）。"""
    q = f.questions[qid]
    if f.items[qid].state == "checking":
        if q.type not in CHOICE_TYPES and str(r) == "corrected":
            return "confirm_field", {"question_id": qid, "result": "corrected", "correction": _scene_value(q, True)}
        return "confirm_field", {"question_id": qid, "result": "no_answer"}
    if q.type in CHOICE_TYPES or not q.conditions or q.conditions == ():
        return "record_answer", {"question_id": qid, "condition_id": "unknown"}
    return "record_answer", {"question_id": qid, "condition_id": "答えた", "value": _scene_value(q)}


def _without_prior(r):
    """前の話の答えを外した同じ読み（本番は、前の話を渡さない質問では前の話を問わない＝flowctl.before_words）。"""
    out = Reading(str(r), r.firm)
    for k, v in vars(r).items():
        setattr(out, k, v)
    out.prior = None
    return out


def flow_reply(it: dict, q: Question, r, report: str = "neutral") -> tuple[Flow, str, dict, str | None]:
    """行 it の場面で、お客様の言葉を判定が r と読んだときの本番の流れの制御の返事（唯一の入口）：(流れの制御, 答えを待っていた質問の番号, 返事,
    報告より先に会話AIが応える番の種類＝決めた所の Flow._asked_kind。応えたあとは消えるので、返事の前に読む)。
    本番と同じ順：お客様の発話 → 判定の読み（on_check）→ 待つのをやめて決める（on_decided）→ 報告の無いターンの入口（judged_turn。判定の読みで動く所＝待つ・
    かけ直す・電話を終える・もう一度・考えている途中など）→ そこで動かなければ（答えの読み＝会話AIの報告を待つ）中立の報告を1つ（_neutral_report）。"""
    f, qid = flow_scene(it, q)
    f.use_checks("judge")
    f.on_callee(); f.on_heard(it.get("text") or "…")
    if r is not None:
        if getattr(r, "prior", None) is not None and before_words(f.questions[qid], ["前の話"]) is None:
            # 前の話を判定に渡さない質問（システムの質問＝flowctl.before_words の1か所）には、記録にある前の話の答えを渡さない（本番は問わない）。前の話のある
            # 業務の質問は、記録の前の話の有無で問うたか（prior_of＝meaning_judge.prior_asked）が決まっている（場面には前の言葉を組まない）
            r = _without_prior(r)
        f.on_check("judge", r)
    f.on_decided()
    asked = f._asked_kind()
    rep = f.judged_turn(voiced=False) if report == "neutral" else None
    if rep is None:
        op, args = _neutral_report(f, qid, r)
        if report == "other":   # 確かめ用：中立でない報告（選んだ答えを替えた報告）でも動きが同じか（会話AIは判定に加えない＝同じでなければ本番の穴）
            if op == "record_answer" and args["condition_id"] == "unknown" and f.questions[qid].conditions:
                args["condition_id"] = f.questions[qid].conditions[0]
            elif op == "confirm_field" and args["result"] == "no_answer":
                args["result"] = "affirmed"
        rep = f.apply(op, args)
    return f, qid, rep, asked


# 返事から動きの名前を作る1か所の表（唯一の名前）。お客様に聞こえる一言（録音の名前の頭）で決まる動き：{録音の名前の頭: 動きの名前}
WHICH_ACT, ESTIMATE_ACT, HOW_NOW_ACT, TROUBLE_ACT = "どのあたりかを伺う", "目安を伺う", "いかがなさいますかと伺う", "何か問題があったかを伺う"
ELSEWHERE_ACT = "ほかの会社で解決できない問題や心配事を伺う"   # ✅ 2026-11-15 日本時間21時55分35秒 ユーザー（伺い25。flowctl.Flow._elsewhere_ask）
PLAY_ACTS = (("callback_hurry", END_ACTS[END_HURRY]), ("callback_refusal", END_ASK), ("callback_topic", END_TOPIC), ("callback_concern", TROUBLE_ACT), ("ai_answer", TROUBLE_ACT),
             ("callback_elsewhere", ELSEWHERE_ACT),
             ("callback_wait_or_later", WAIT_ACT[WAIT_OR_LATER]), ("callback_later_time", WAIT_ACT[LATER]), ("callback_not_agreed_which", WHICH_ACT),
             ("callback_other_point_which", WHICH_ACT), ("estimate_ask", ESTIMATE_ACT), ("how_now", HOW_NOW_ACT), ("hesitate_point", HESITATE_ACT[False]))
PENDING_ACT = "保留でよいかを伺う"
WISH_ACT = "ほかのご希望を伺う"
DASH_ACT = "「-」にして次へ"
REJECT_ACT = "差し戻す（お客様には何も聞こえない）"
HANDOVER_ACT = "代わった方へ名乗りから伺い直す（会話AIへ知らせる）"
CONCERN_ACT = "言っていただいた点に応える"
MAYBE_ACT = "可能性を確かめる："
OTHER_ACT = "その他："
QUESTION_FIRST = "添えた質問に先に答えてから："
# 報告より先に会話AIが応える番（Flow._asked_kind）の動き
ASKED_ACTS = {"asked": ANSWER_FIRST, "offer": AWAY_ACT, "help": HESITATE_ACT[True], "concern": CONCERN_ACT}


def flow_name(f: Flow, qid: str, rep: dict, it: dict, asked: str | None = None) -> str:
    """流れの制御の返事 rep から、動きの名前（唯一の名前の作り＝読みの動き flow_act・正解の動き flow_wants が通る）。名前はお客様に聞こえること（締める・伺う一言・
    確かめ・受け取って次へ・待つ）で付ける。どの名前にも当たらない返事は「その他：」（数え漏れを黙って出さない）。"""
    res, item = rep.get("result"), f.items.get(qid)
    plays = [str(p.get("id") or "") for p in rep.get("play") or []]
    from flowctl import CALLBACK_IDS
    if rep.get("ahead_offer") and rep.get("question_id") == CALLBACK_IDS["time"]:
        # かけ直しの時刻・日の質問を、かけ直しを頼んだお話の中の時刻・日の読み直しのあとに流す番（flowctl.Flow._callback_ahead）：動きは、その質問を伺うのと同じ
        # （その質問の録音で名前を付ける＝かけ直しを伺う動き。お話に時刻・日があれば言い直し、無ければその質問＝どちらも同じ申し出の受け方）
        plays.append(str(f.questions[rep["question_id"]].recording or ""))
    if f.end_reason is not None:
        by_reason = {v: k for k, v in END_REASON.items()}
        return END_ACTS.get(by_reason.get(f.end_reason), f"締める（{f.end_reason}）")
    if res == "rejected":
        # 差し戻し：代わった方への名乗りから伺い直す知らせか、報告より先に会話AIが応える番の知らせ（報告の無いターンと同じ番＝_asked_kind）か、ほか
        if "代わった方が話し始めた" in str(rep.get("message") or ""):
            return HANDOVER_ACT
        return ASKED_ACTS.get(asked or "", REJECT_ACT)
    if res == "hold":
        return WAIT_ACT[HOLD_BACK] if "wait_reply" in plays else WAIT_ACT[WAIT_PLAIN] if rep.get("silence_scene") == "wait" else WAIT_ACT[HOLD]
    if res == "thinking":
        return WAIT_ACT[THINKING]
    if res == "replayed":
        return REPEAT_ACT
    if res == "callee_asked":
        # 報告より先に応える番（決めた所の種類 asked・返事の中で決まった番＝気がかりを伺ったあとのお話）か、受け取った答えに添えた質問に先に答える番
        # （Flow._question_first＝after_answer）
        kind = asked or f._asked_kind()
        if kind is None and f.after_answer is not None:
            # 受け取った答えに添えた質問に先に答え、そのあとに取っておいた返事（Flow._question_first）
            return QUESTION_FIRST + flow_name(f, qid, f.after_answer[1], it)
        return ASKED_ACTS.get(kind or "", OTHER_ACT + f"callee_asked|{kind}")
    for head, act in PLAY_ACTS:
        if any(p.startswith(head) for p in plays):
            return THANKED + act if act == WAIT_ACT[LATER] and any(p.startswith("offer_thanks") for p in plays) else act
    if res == "hesitate":
        return HESITATE_ACT[True]
    if any(p.endswith("_pending") for p in plays):
        return PENDING_ACT
    if any(p.endswith("_other") for p in plays) and item is not None and item.state in ("accepted", "confirmed") and item.condition == NONE:
        return WISH_ACT
    if res == "ask_back":
        return ASK_AGAIN
    if res in ("own_answer", "aside", "talk"):
        return AWAY_ACT
    if item is None:
        return OTHER_ACT + f"{res}|{','.join(plays)}"
    cond = {"dont_know": "unknown", "refused": "refused"}.get(item.state, item.condition)
    q = f.questions[qid]
    asked = (it.get("asked_choice") if q.type in CHOICE_TYPES else "答えた") if it.get("set") == "D" else None
    same = asked is not None and cond == asked and (q.type in CHOICE_TYPES or item.value == _scene_value(q))
    if res == "confirm_required":
        if rep.get("after") == "sense":
            return SENSE + str(cond)
        if rep.get("after") == "end_doubt" or any(END_DOUBT_WHY in str(h) for h in item.history[-3:]):
            return END_TOPIC   # 電話を終える言葉か決まらず、いまの話の向きを伺う（Flow._topic_first。取込の言葉の無い質問は曖昧な答えと同じ確かめの形）
        if "可能性" in str(rep.get("say") or ""):
            return MAYBE_ACT + str(cond)
        if asked is not None and not same:
            return DENIED   # 言い直しへの返事で答えが替わり、替わった答えで確かめ直す
        return f"念のため確かめる：{cond}"
    if item.state in ("accepted", "confirmed", "refused", "dont_know"):
        if same:
            return AFFIRMED_HEDGED if item.hedged else AFFIRMED
        return (ANYTIME_TAKE if any(p.startswith("unclear_anytime") for p in plays) else "受け取る：") + str(cond)
    if item.state == "unresolved":
        return DASH_ACT
    return OTHER_ACT + f"{res}|{item.state}|{','.join(plays)}"


_ACTS: dict = {}


def act_of(it: dict, q: Question, r) -> str | None:
    """判定の読み r（Reading。None＝読み無し）で、本番の流れの制御がすることの名前（唯一の数え＝読みの動き・正解の動きのどちらも通る）。場面を組めない行は
    SCENE_MISSING＋理由。同じ場面・同じ読みは1回だけ流す。"""
    if r is None:
        return None
    side = tuple(getattr(r, k, None) for k in (*SIDE_SUFFIX, "prior"))
    key = (it.get("set"), it.get("qid"), q.id, q.text, q.type, q.conditions, it.get("text"), it.get("asked"), it.get("asked_choice"), str(r), r.firm, side,
           tuple(sorted((q.meanings or {}).items())), bool(q.option_notes), q.asks_fact, q.maybe)
    if key not in _ACTS:
        try:
            f, qid, rep, asked = flow_reply(it, q, r)
            _ACTS[key] = flow_name(f, qid, rep, it, asked)
        except NoScene as e:
            _ACTS[key] = SCENE_MISSING + str(e)
    return _ACTS[key]


def flow_act(it: dict, q: Question, got, firm, prior=None, away=None) -> str | None:
    """判定の読み got（firm＝言い切っているか・prior＝前の話の答え・away＝別のお話かの読み）で流れの制御がすること（act_of の1か所に通す入口。
    記録の行の読みは step_act）。"""
    if got is None:
        return None
    r = _without_prior(got) if isinstance(got, Reading) else Reading(got, firm is not False)   # 読みそのもの（答えとは別に読む項目つき）も渡せる
    r.firm = firm is not False
    if not isinstance(got, Reading) or away is not None:
        setattr(r, AWAY, away)
    r.prior = prior
    return act_of(it, q, r)


def label_readings(it: dict, q: Question, has_end: bool) -> list:
    """正解の表の行 it から、正しい読みの組み合わせを全部作る（唯一の作り＝正解の動き step_wants）：答え（accept）× 言い切り（境目 either なら両方・
    ほかは「曖昧」の印 hedged の逆）× こちらへ尋ねた問いか（query_truths）× 別のお話か（away_truths）× 電話を終える言葉か（has_end なら end_accept・
    無ければ読んでいない）× わからない・答えたくない理由（付けた行は why_accept・付けていない行はどの理由も正しい）× 前の話の答え（会話になっていない
    正解 talk False の行は、その答えと食い違う前の話の答え＝flowctl.off_talk の1か所。ほかは前の話から決まる答えは無い）。聞こえ方は hear_truth。"""
    from flowctl import why_choices
    out = []
    firms = (True, False) if either(it) else (not hedged(it),)
    ends = tuple(end_accept(it)) if has_end else (None,)
    reasons = why_accept(it)
    for a in accept(it):
        whys = tuple(reasons) if reasons else (None, *why_choices(False)) if a in ("unknown", "refused") else (None,)
        priors = tuple(c for c in q.conditions if off_talk(q, a, c)) if talk(it) is False else ()
        for firm in firms:
            for qv in query_truths(it):   # 問いの正解を付けていない行は、どちらの読みも正解（query_truths の決まり＝11-08 8時55分08秒）
                for av in away_truths(it):
                    for ev in elsewhere_truths(it):
                        for e in ends:
                            for w in whys:
                                for p in priors or (None,):
                                    r = Reading(a, firm)
                                    for k, v in ((QUERY, qv), (AWAY, av), (ELSEWHERE, ev), (END, e), (WHY, w), (HEAR, hear_truth(it))):
                                        setattr(r, k, v)
                                    r.prior = p
                                    out.append(r)
    return out


def flow_wants(it: dict, q: Question) -> set[str]:
    """正解の表から決まる、答えの読みで流れの制御がすべきこと（電話を終える言葉かを読んでいない形＝step_wants の has_end なし）。"""
    return step_wants(it, q, False)


def flow_miss(it: dict, q: Question, key: str) -> str | None:
    """判定 key の読みで、1つの発話の流れの制御の動き（step_act）が正解（step_wants）と違えば、その違いの種類（正しければ None・読みが無ければ
    「読み無し」）。答えの動きと電話を終える読みの動きを1か所で数える（唯一の数え）。⛔ 2026-11-05 に1つにした：前は答えの動き（ここ）と電話を
    終える言葉の動き（end_miss）を別々に数え、正しく急ぎを伺う発話でも答えの読みが違えば外れと数え、電話を終える言葉ではないのに急ぎを伺う
    読みを、答えが正しければ「正しい動き」と数えていた。"""
    act = step_act(it, q, key)
    if act is None:
        return "読み無し"
    if act.startswith(SCENE_MISSING):
        return "場面を組めない（数えない）"   # 正解の動きも同じ場面で組めない＝「正しい」と数えない
    want = step_wants(it, q, it.get(f"{key}終わり") is not None)
    if act in want:
        return None
    ends = {w for w in want if end_step(w)}
    closes = {w for w in ends if w.removeprefix(QUESTION_FIRST) not in (END_TOPIC, END_ASK)}
    if end_step(act):
        if act.removeprefix(QUESTION_FIRST) not in (END_TOPIC, END_ASK):
            return "⛔違う理由で締める・伺う" if closes else "⛔電話を終える言葉ではないのに締める・急ぎを伺う"
        if closes:
            return "⛔電話を終える言葉なのに締めない"
        return "違う伺い" if ends else "要らない伺い"
    want -= ends
    if not want:   # 正解は電話を終える読みの動きだけ（答えの読みで続けた）
        return "⛔電話を終える言葉なのに締めない" if closes else "⛔伺わずに続ける"
    helps = set(HESITATE_ACT.values())   # 迷っている方への動き（hesitates の1か所）
    if want & helps and act not in helps:
        return "⛔迷っているのに、選ぶ助けを伝えずに待つ・聞き直す・締める"   # ✅ 13時05分06秒「…急かすのは最悪な手ですよ。」
    if act in helps:
        return "迷っていないのに、迷っている点を伺う・選ぶ助けを伝える"
    if act.startswith(OTHER_ACT):
        return "名前の無い動き（その他）"
    if act.startswith(QUESTION_FIRST) and act.removeprefix(QUESTION_FIRST) in want:
        return "尋ねられていないのに、添えた質問に先に答える（値は正しい）"
    if QUESTION_FIRST + act in want:
        return "⛔添えた質問に答えずに進める（値は正しい）"
    if act == DASH_ACT:
        return "⛔伺わずに「-」にして次へ"
    if act in (HANDOVER_ACT, REJECT_ACT):
        return "代わった方の言葉ではないのに、名乗りから伺い直す・差し戻す"
    if act == ELSEWHERE_ACT:
        return "ほかの会社のことを言っていないのに、解決できない問題や心配事を伺う"
    if ELSEWHERE_ACT in want:
        return "⛔ほかの会社に頼んでいると言われたのに、解決できない問題や心配事を伺わない"
    asks = {PENDING_ACT, HOW_NOW_ACT, ESTIMATE_ACT, WHICH_ACT, WISH_ACT, CONCERN_ACT, TROUBLE_ACT}   # 答えを受け取らずに伺う一言（保留・いかがなさいますか など）
    if act in asks or act.startswith(MAYBE_ACT):
        return "違う伺い（保留・いかがなさいますか・目安・どのあたり・ほかのご希望・可能性）" if want & asks or any(
            w.startswith(MAYBE_ACT) for w in want) else "要らない伺い（保留・いかがなさいますか・目安・どのあたり・ほかのご希望・可能性）"
    pure = set(accept(it)) <= {UNDECIDED, "unknown"}   # どの答えにも寄っていない言葉
    waits = {wait_act(k) for k in WAIT_KINDS}   # 待つ・かけ直すの読みの動き（お礼の一言の有無も分ける＝wait_act の1か所）
    if act in waits:
        return "違う待ち方・かけ直しの伺い方" if want & waits else "待つ・かけ直すの言葉ではないのに、待つ・かけ直しを伺う"
    if act == AWAY_ACT and AWAY_ACT not in want:
        return "答えの話なのに、別のお話として「ところで」で伺い直す"
    if AWAY_ACT in want and act not in want and act == ASK_AGAIN:
        return "⛔別のお話なのに、ひとことも無く聞き直す"
    if act == ANSWER_FIRST:
        return "答えなのに、尋ねられたとして先に答える"
    if ANSWER_FIRST in want and pure:
        return "尋ねられたことに答えずに、推し量って進める・確かめる"
    if act.startswith(ANYTIME_TAKE) and act.replace(ANYTIME_TAKE, "受け取る：") in want:
        return "要らない一言（値は正しい）"   # 言い切った「無い」に「何かわからないことが…」を添えた
    if act.startswith("受け取る：") and act.replace("受け取る：", ANYTIME_TAKE) in want:
        return "一言が落ちた（値は正しい）"   # 今のところの「無い」を言い切りと読み、「何かわからないことが…」を添えなかった
    if act in (AFFIRMED, AFFIRMED_HEDGED) or act.startswith("受け取る") or act.startswith(ANYTIME_TAKE):
        if act == AFFIRMED_HEDGED and AFFIRMED in want:
            return "要らない曖昧の印（値は正しい）"
        if act == AFFIRMED and AFFIRMED_HEDGED in want:
            return "曖昧の印が落ちた（値は正しい）"
        if act.startswith("受け取る") and act.replace("受け取る", "念のため確かめる") in want:
            return "⛔言い切っていない答えを、確かめずに受け取る"
        return "⛔受け取ってはいけないのに受け取る"
    if act == ASK_AGAIN:
        return "要らない聞き直し"
    if act == RECHECK:
        return "違う答えで確かめ直す" if talk(it) is False else "会話になっているのに、念のため確かめ直す"
    if act.startswith("念のため") and act.replace("念のため確かめる", "受け取る") in want:
        return "要らない念のための確かめ"
    return "推し量って確かめる（どの答えにも寄っていない言葉）" if pure else "違う答えで確かめる"


def first(it: dict) -> bool:
    """1日の上限が少ないときに、先に判定させる答え：いまの判定（BASE）が言い直しへの返事を認めたと読み違えたもの。判定し直すときは、
    捨てる前の読み違えの印（前の読み違え）で見る。⛔ 2026-10-18：組の順で流し、上限で止まって、決め手の15個が2回とも後回しになった"""
    return bool(it.get("前の読み違え") or misread_as_affirmed(it, BASE))


def pick_sample(items: list[dict], n: int) -> list[dict]:
    """2つ目の判定の候補（3.6〜3.8 Flash）は1日の上限が少ないので、3.5 Flash Lite が誤った・決められなかった答えと、ほかから無作為に n 個
    （記録の通話の言葉・答えでない言葉は全部）。3.5 Flash Lite が言い直しへの返事を認めたと読み違えたものを先頭に置く
    （⛔ 2026-10-18：組の順で流し、上限で止まって、決め手の15個のうち 3.6 Flash が判定できたのは1個だけだった）。"""
    import random

    hard = [it for it in items if it["set"] in ("A", "E") or it.get("3.5 Flash Lite") not in accept(it)]
    rest = [it for it in items if it not in hard]
    hard.sort(key=lambda it: not first(it))
    return hard + random.Random(53).sample(rest, min(n, len(rest)))


def run_judges(args, items: list[dict], qmap: dict, ids: dict) -> None:
    """1つの答えに1回ずつ、判定のモデルごとに問い合わせる。結果は items に書き込む。仕事は1つの山に積み、使えるキーごとに1本の流れが
    山から取って流す（流れは自分のキーだけを使い、そのキーが使えなくなったら抜ける＝残りの仕事はほかの流れが取る）。
    ⛔ 2026-10-17 の1回目：キーの数（17）だけ同時に流し、止められている6本の分が、ほかの流れのキーに重なって1分の上限に当たり続けた
    ⛔ 2026-10-18：キーごとに仕事を前もって分けていたので、流れの途中で止められていると分かったキーの分が、ほかの流れのキーに重なった
       （1分に約25件しか進まなかった）"""
    import queue
    from common import KeyUnusable

    sample = pick_sample(items, args.sample) if args.sample else []
    probe = KeyRing(args)
    usable = [i for i in range(len(probe.keys)) if i not in probe.denied]
    jobs: queue.Queue = queue.Queue()
    order = {s: n for n, s in enumerate(args.sets or "")}  # --sets の並びの順に判定する（上限で止まっても、先の組は測り終える）
    gem = [k for k in args.judge if k not in OTHER_JUDGES]  # Google の判定（キーごとの流れで、1日の上限を飛ばしながら）
    others: queue.Queue = queue.Queue()  # Google 以外の判定（1日の上限が無い有料の枠＝見本に絞らず全部）
    for key in [k for k in args.judge if k not in GROUPS]:  # 判定ごとに、決め手の答え（first）から、--sets の組の順に
        pool = (sample if sample and key not in (BASE, *OTHER_JUDGES)
                else [it for it in items if it.get("見本")] if key == BASE and any(it.get("見本") for it in items) else items)
        for it in sorted(pool, key=lambda it: (not first(it), order.get(it["set"], 0))):
            if key not in it and it.get("maker") != key and in_scope(args, it, qmap):
                (others if key in OTHER_JUDGES else jobs).put((key, it))
    lock = threading.Lock()
    done = Counter()

    def worker(k: int):
        a = base_parser("").parse_args(["--key-index", str(k)] + (["--env", args.env] if args.env else []))
        ring = KeyRing(a, min_interval_s=args.interval)
        skip: set[str] = set()  # このキーで使えなくなったモデル
        while True:
            try:
                key, it = jobs.get_nowait()
            except queue.Empty:
                return
            if key in skip:
                jobs.put((key, it))
                if all(m in skip for m in gem):
                    return
                continue
            q = qmap[it["qid"]]
            took = {}

            def call(client):
                t = time.monotonic()
                got = judge(client, ids[key], q, it["text"], it.get("asked"), it.get("before"))
                took["s"] = round(time.monotonic() - t, 3)
                return got
            try:
                got, _ = ring.call(ids[key], call, rotate=False)
                it[key] = got
                it[f"{key}言い切り"] = getattr(got, "firm", None)
                side_record(it, key, got)   # 答えとは別に読む項目（電話を終える言葉か＝flow_miss・聞こえ方・わからない理由で数える）
                it[f"{key}秒"] = took.get("s")
            except KeyUnusable:
                skip.add(key)
                jobs.put((key, it))  # ほかの流れが取る
                continue
            except Exception as exc:
                it[f"{key}誤り"] = describe_error(exc).get("message", "")[:200]
            with lock:
                done[key] += 1
                if sum(done.values()) % 50 == 0:
                    say("判定", dict(done), "残り", jobs.qsize())
                    save_partial(f"G-53{args.tag}", {"items": items})

    def other_worker(_n: int):
        from common import load_secret
        from meaning_judge import OtherJudgeError, other_client

        clients = {}  # 判定の名前 ⇒ 送る道具（流れごとに1つ）
        while True:
            try:
                key, it = others.get_nowait()
            except queue.Empty:
                return
            q = qmap[it["qid"]]
            if key not in clients:
                clients[key] = other_client(key, load_secret(OTHER_JUDGES[key]["key"], args.env), budget=args.budget_yen)
            for attempt in range(5):
                try:
                    t = time.monotonic()
                    it[key] = judge_other(clients[key], key, q, it["text"], it.get("asked"), it.get("before"))
                    it[f"{key}言い切り"] = getattr(it[key], "firm", None)
                    side_record(it, key, it[key])
                    it[f"{key}確からしさ"] = getattr(it[key], "sure", None)  # Jev の線（OTHER_JUDGES の sure_min）で、同時に出した組の読みを見積もるため
                    it[f"{key}言い切りの確からしさ"] = getattr(it[key], "firm_p", None)   # 言い切りの線と、どちらとも言えない幅を決めるため
                    for suffix, attr in SPREAD_RECORD.items():   # どの選べるものの間で割れたか・どの問いが低かったか（11-07 10時29分25秒）
                        it[f"{key}{suffix}"] = getattr(it[key], attr, None)
                    it[f"{key}秒"] = round(time.monotonic() - t, 3)
                    break
                except OtherJudgeError as exc:  # 上限（429）・混雑（5xx）は待ってやり直す。ほかは記録して次へ
                    if exc.status == 429 or (exc.status or 0) >= 500:
                        time.sleep(exc.retry or 5 * 2 ** attempt)
                        continue
                    it[f"{key}誤り"] = str(exc)[:200]
                    break
                except Exception as exc:
                    it[f"{key}誤り"] = describe_error(exc).get("message", "")[:200]
                    break
            with lock:
                done[key] += 1
                if sum(done.values()) % 50 == 0:
                    say("判定", dict(done), "残り", jobs.qsize() + others.qsize())
                    save_partial(f"G-53{args.tag}", {"items": items})

    n_other = getattr(args, "other_lanes", 4) * len({k for k in args.judge if k in OTHER_JUDGES})  # 有料の枠の1分の上限に収まる数（既定は1つの会社に4本）
    lanes = usable if gem else []
    with ThreadPoolExecutor(max_workers=max(1, len(lanes) + n_other)) as ex:  # Google の流れと、ほかの会社の流れを同時に
        futures = [ex.submit(worker, k) for k in lanes] + [ex.submit(other_worker, n) for n in range(n_other)]
        for f in futures:
            f.result()
    left = jobs.qsize() + others.qsize()
    if left:
        say(f"⚠️ どのキーでも判定できなかった答えが {left} 個残りました（1日の上限）")
    for name in [k for k in args.judge if k in GROUPS]:
        import asyncio
        asyncio.run(run_group(args, items, qmap, name))


async def run_group(args, items: list[dict], qmap: dict, name: str, lanes: int = 4) -> None:
    """組（GROUPS）の判定を、本番と同じ流れ（meaning_judge.JudgePanel・first_of の lead＝Jev を先に出し、確かな読みが無いときだけ Sonnet）で
    1件ずつ判定する。読みは items の「組の名前」の欄（＋言い切り・秒・by・誤り）に書く。--limit があれば、決め手の答えから、その数だけ。
    ⛔ 2026-10-29：判定ごとに全部の答えを別々に送る形（run_judges）では、組の料金が本番の2倍半になる（Jev が確かな読みでも Sonnet を送る）"""
    import asyncio
    from meaning_judge import JudgePanel, NoJudgment, make_judge, members

    fns = tuple((m, make_judge(args, m)) for m in members(name))   # 送る道具は1つずつ（つながりを保つ）
    todo = [it for it in sorted(items, key=lambda it: (not first(it), it["set"])) if name not in it and it["qid"] in qmap
            and in_scope(args, it, qmap)][:args.limit or None]
    sem, done = asyncio.Semaphore(lanes), Counter()

    async def one(it):
        async with sem:
            panel = JudgePanel({"judge2": fns})   # 答えごとに新しい受け持ち（前の答えの時間切れ・会社の印を持ち越さない）
            t = time.monotonic()
            try:
                got = await panel.ask("judge2", qmap[it["qid"]], it["text"], it.get("asked"), it.get("before"))
                it[name], it[f"{name}言い切り"], it[f"{name}by"] = str(got), got.firm, got.by
                side_record(it, name, got)
            except NoJudgment as exc:
                it[f"{name}誤り"] = str(exc)[:200]
            it[f"{name}秒"] = round(time.monotonic() - t, 3)
            done["済"] += 1
            if done["済"] % 50 == 0:
                say(name, "判定", done["済"], "/", len(todo))
                save_partial(f"G-53{args.tag}", {"items": items})
    await asyncio.gather(*(one(it) for it in todo))


def prior_todo(args, items: list[dict], qmap: dict) -> list[dict]:
    """前の話の答えを読む答え（read_priors が読む・組み立てだけ＝--dry-run が数える）：前の言葉があり、本番でも前の話を問う（meaning_judge.prior_asked の
    1か所）、まだ読んでいない答え。"""
    from meaning_judge import prior_asked
    return [it for it in items if it.get("before") and PRIOR_KEY not in it and it["qid"] in qmap and in_scope(args, it, qmap)
            and prior_asked(qmap[it["qid"]], it.get("asked"), it["before"])]


def read_priors(args, items: list[dict], qmap: dict) -> int:
    """前の言葉のある答え（試しの言葉の N1〜N3・W1）の前の話の答えを、本番と同じ読み手（meaning_judge.read_prior_first＝PRIOR_READERS の順。本番の
    判定の組 JudgePanel の prior と同じ）で1件ずつ読み、PRIOR_KEY の欄に入れる（どの判定の読みにも同じものを添える＝prior_of）。本番は発話ごとに読む
    ので、同じ前の言葉でも1件ずつ読む（読みの揺れも数える）。読めなければ None（本番も、前の話の答えが無ければ比べない）。返り値は読んだ数。
    ⛔ 2026-11-05 日本時間20時台に見つけた：測りは前の言葉を渡さず、前の話の答えも読んでいなかった（本番で走る、前の話と食い違えば聞き直す動きを
    数えていなかった）"""
    from common import load_secret
    from meaning_judge import PRIOR_READERS, other_client, read_prior_first
    todo = prior_todo(args, items, qmap)
    if not todo:
        return 0
    clients = {n: other_client(n, load_secret(OTHER_JUDGES[n]["key"], args.env), budget=args.budget_yen) for n in PRIOR_READERS}
    for n, it in enumerate(todo, 1):
        it[PRIOR_KEY], it[f"{PRIOR_KEY}の確からしさ"], _ = read_prior_first(clients, qmap[it["qid"]], it["before"])
        if n % 50 == 0:
            say("前の話の答え", n, "/", len(todo))
            save_partial(f"G-53{args.tag}", {"items": items})
    return len(todo)


def tally(items: list[dict], judges, qmap: dict | None = None) -> dict:
    """判定ごとの数え。qmap（質問の表）を渡せば、流れの制御の動き（flow_miss）でも数える。"""
    out = {}
    for key in judges:
        t, misses = defaultdict(Counter), []
        for it in items:
            if key not in it:
                continue
            if it.get("maker") == key:
                continue  # 自分で作った答え
            if it.get(f"{key}聞こえ") is not None or it["set"] == "HEAR":   # こちらの声が届いていない様子か（項目を足す前の記録は数えない）
                h, hw = it.get(f"{key}聞こえ"), hear_truth(it)
                t["こちらの声が届いていない様子か"][f"{'組 HEAR' if it['set'] == 'HEAR' else 'ほかの組'}・{hw}・"
                                         f"{'正しい' if h == hw else '⛔' + str(h)}"] += 1
            if it["set"] == "HEAR" and not _label(it):
                continue  # 組 HEAR は、答えの正解を帳面（g53_labels.json）で付けた行のほかは、聞こえ方だけを数える（答えの段の聞き返しの言葉＝もう一度言ってほしい、には付けた）
            got, want = it[key], truth(it)
            res = "正しい" if got in accept(it) else ("決められない（確かめる）" if got in (UNDECIDED, "unknown") else "⛔別の答え")
            label = {"B": "選択肢", "D": it.get("kind")}.get(it["set"], want)
            t[f"{it['set']}・{label}"][res] += 1
            t["全体"][res] += 1
            if qmap and it["qid"] in qmap:
                miss = flow_miss(it, qmap[it["qid"]], key)   # 答えと電話を終える読みの1つの動き（1行に1回だけ流れの制御に通す）
                t["流れの制御の動き"][miss or "正しい動き"] += 1
                if miss not in (None, "読み無し") and len(misses) < 80:
                    misses.append(f"{it['set']}・{it['qid']}「{it['text']}」正解={accept(it)}・{end_accept(it)} 判定={it[key]}・{it.get(f'{key}終わり')}＝{miss}")
                if it.get(f"{key}終わり") is not None:   # 電話を終える言葉か（項目だけ。項目を足す前の記録は数えない）
                    t["電話を終える言葉か"]["正しい" if it[f"{key}終わり"] in end_accept(it) else "⛔別の読み"] += 1
            if it.get(f"{key}理由") is not None and (w := why_accept(it)) is not None:   # わからない・答えたくない理由（正解を付けた行だけ）
                t["わからない・答えたくない理由"]["正しい" if it[f"{key}理由"] in w else "⛔別の読み"] += 1
            if (m := mark(it, key)):
                t["全体"][f"曖昧の印・{m}"] += 1
            if misread_as_affirmed(it, key):
                t["全体"]["⛔⛔言い直しを、認めていないのに認めたと読んだ"] += 1
        secs = sorted(it[f"{key}秒"] for it in items if it.get(f"{key}秒") is not None)
        out[key] = {"数え": {k: dict(v) for k, v in t.items()},
                    "かかった秒（中央・遅いほうから1割・最大）": [secs[len(secs) // 2], secs[int(len(secs) * 0.9)], secs[-1]] if secs else None,
                    "⛔別の答えの例": [f"{it['qid']}「{it['text']}」正解={accept(it)} 判定={it[key]}" for it in items
                                   if key in it and it[key] not in accept(it) + [UNDECIDED, "unknown"]][:80],
                    "⛔流れの制御の動きの外れの例": misses}
    return out


def predict_calls(items: list[dict], key: str) -> dict:
    """判定の結果を、G-14 の記録の通話（g15_ear.live_calls）に当てる：会話のAIの最初の報告・Soniox の文字（G-15。hold_s までに届いたもの）・
    意味の判定（会話のAIの聞き取りの文字の判定＝通話の中と同じ。convo.check_handler）を、流れの制御そのもの（ears_try.flow_outcome）に渡して、
    依頼主へ渡るものを数える。判定が無い形（別の耳だけ）と比べる。⚠️ 判定の結果は、この試験で1回ずつ聞いたもの（通話の中の時間の遅れは入らない）。"""
    from ears_try import TAKEN, flow_outcome
    from flowctl import Limits
    from g14_listen import QUESTIONS, business
    from g15_ear import live_calls

    qs = {qid: business(qid).questions[0] for qid in QUESTIONS}
    rows = json.loads(open(sorted(glob.glob(str(HERE / "results" / "G-15-soniox_*.json")))[-1], encoding="utf-8").read())["rows"]
    ears = {r["wav"]: (r["ear"] if r.get("first_s") is not None and r["first_s"] <= Limits.hold_s else None) for r in rows}
    judged = {(it["qid"], it["text"]): reading(it, key) for it in items if it["set"] == "A"}

    out = {}
    for name, use in (("別の耳だけ", False), ("別の耳＋意味の判定", True)):
        c, bad = Counter(), Counter()
        for call in live_calls():
            qid, want = call.get("question") or "Q1", call.get("want")
            q = qs.get(qid)
            if q is None or want in (None, "follow") or call.get("op") != "record_answer" or q.type not in ("yes_no", "choice"):
                continue
            ear = ears.get(call["wav"])
            meaning = judged.get((qid, call["heard"])) if use else None
            state, cond = flow_outcome(q, call["heard"], call["condition"], {k: v for k, v in (("ear", ear), ("judge", meaning)) if v is not None})
            right = call["condition"] == want
            if state in TAKEN:
                ok = (cond == want) if want != "ambiguous" else state == "dont_know"
                if state == "dont_know" and want == "unknown":
                    ok = True
                # 「わからない」で受け取った（依頼主へは「-」）のに答えがあった：誤った値は渡らない（答えを取り逃しただけ）＝誤りと分ける
                res = ("受け取った・正しい" if ok else "受け取った・「-」（答えがあったのに）" if state == "dont_know"
                       else "⛔受け取った・誤り")
            else:
                res = "確かめる（AIは正しかった）" if right else "確かめる（AIが誤っていた）"
            c["通話"] += 1
            c[res] += 1
            if res.startswith("⛔"):
                bad[f"{call['wav'].rsplit('/', 1)[-1]}「{call['heard']}」AI={call['condition']} 正解={want} 判定={meaning}"] += 1
        out[name] = {**dict(c), "⛔の中身": dict(bad.most_common(20))}
    return out


def chosen_sets(spec: str | None, names) -> tuple | None:
    """--sets の組の名前を、いまある組の名前（names）で読み分ける唯一の所（書いた順のまま。無ければ None＝全部の組）。区切りは「,」「、」・空白で、
    無くてもよい（前からの「DABE」）。長い名前から当てる（「HEAR」を H・E・A・R に分けない）。当たる組の無い名前は止める（黙って0行にしない）。
    ⛔ 2026-11-15：文字の含まれ方で選んでいた（it["set"] in args.sets）ので、組 HEAR を選ぶと1文字の組 A・E も選ばれた（伺い19 ア の小さな試しの
    つもりで、答えの段の全部の行へ送るところだった）"""
    import re

    if not spec:
        return None
    longest, out = sorted(names, key=len, reverse=True), []
    for token in re.split(r"[,、\s]+", spec.strip()):
        while token:
            hit = next((n for n in longest if token.startswith(n)), None)
            if hit is None:
                raise SystemExit(f"--sets の「{token}」に当たる組がありません（いまある組：{'・'.join(sorted(names))}）")
            out.append(hit)
            token = token[len(hit):]
    return tuple(dict.fromkeys(out))


def in_scope(args, it: dict, qmap: dict) -> bool:
    """この測りで判定を送る答えか（唯一の場所。判定を送る所 run_judges・run_group・前の話の答え prior_todo・組み立てだけの数えが同じ1つ）：--sets の組の
    答えで、--after（本番で先に読む判定の名前）を渡したときは、その判定の記録の読みで本番の組が決めきれない答え（読みが無い・失敗・確かでない＝
    meaning_judge.sure_reading・会話の側の待つ長さ＝Limits.judge_wait_s に遅れた）だけ＝2つ目の判定が本番で使われる答え（伺い19 ア）。"""
    if args.sets and it["set"] not in args.sets:
        return False
    lead = getattr(args, "after", None)
    if not lead:
        return True
    from flowctl import Limits
    from meaning_judge import sure_reading
    r, q = reading(it, lead), qmap.get(it["qid"])   # 質問の表に無い答えは、確かかを決められない＝判定する側
    return r is None or q is None or (it.get(f"{lead}秒") or 0) > Limits().judge_wait_s() or not sure_reading(lead, q, r)


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--judge", action="append", choices=JUDGES, help="判定のモデル（既定は2つとも）")
    p.add_argument("--reuse", help="前の結果のファイル（作った答え・判定済みの分を使い回す）")
    p.add_argument("--tag", default="", help="結果のファイル名に足す印（同時に流す回を分けるため）")
    p.add_argument("--sets", help="判定する組だけ（組の名前を並べる：例 N1,W1,HEAR・前からの DE も読める＝chosen_sets）。ほかの組は判定しない（数えには入る）")
    p.add_argument("--after", choices=JUDGES, help="2つ目の判定の測り：本番で先に読むこの判定の記録の読みで、組が決めきれない答えだけを判定する（in_scope）")
    p.add_argument("--rejudge", action="store_true", help="--reuse の、--judge で渡した判定の読みを捨てて測り直す（判定の文・選べるものを変えたとき）")
    p.add_argument("--add-de", action="store_true", help="--reuse の答えに、D（言い直しへの返事）・E（答えでない言葉）を作って足す")
    p.add_argument("--g73-partial", action="store_true", help="試しの言葉（G-73）がそろっていなくても測る（ユーザーがその形で測ると選んだときだけ）")
    p.add_argument("--sample", type=int, default=0, help="2つ目の判定の候補は、3.5 Flash Lite が誤った答えと、ほかから無作為にこの数だけ（1日の上限が少ない）")
    p.add_argument("--limit", type=int, default=0, help="組の判定（GROUPS）だけ：決め手の答えから、この数だけ判定する（はじめに少しだけ流して1件の円を確かめる）")
    p.add_argument("--interval", type=float, default=4.0, help="同じキー・同じモデルに続けて聞く間（秒）。1分の上限に当たったら、言われた秒だけ待つ")
    p.add_argument("--other-lanes", type=int, default=4,
                   help="Google 以外の判定1つにつき、同時に送る流れの本数（少しだけ聞き直すときは1＝前の部分の保存がまだ無いうちに同じ前の部分を"
                        "何本も同時に送ると、どれも書き込みになる。2026-10-30：95件の聞き直しで、4本なら最悪約57円・1本なら約25円の見込み）")
    args = p.parse_args()
    args.judge = args.judge or ["3.5 Flash Lite"]
    ring = KeyRing(args)
    # 表示名 ⇒ モデルID は Gemini に聞く＝送るときだけ引く（答えを作る枝と、判定の前）。⛔ 2026-11-15 日本時間：ここで引いていたので、
    # 組み立てだけ（--dry-run）でも止まりの前に Gemini へ送っていた
    ids = None
    from review_words import questions as review_questions
    qmap = question_map() | review_questions()   # 組 R の質問（見直し (12) の組 Q の試しの言葉）
    qs, vqs = g48_questions(), value_questions()   # ⛔ 2026-11-02 に見つけた：作らずに使っていた（--reuse でない流し方・--add-de で止まっていた）
    if args.reuse:
        items = json.loads(open(args.reuse, encoding="utf-8").read())["items"]
        if args.rejudge:  # 判定の文や選べるものを変えたとき：渡した判定（--judge）の前の読みを捨てて、同じ答えで測り直す（決め手の印は残す＝first）。
            # ほかの判定の読みは残す（新しい形で測った候補と、測り直したいまの判定を、同じ答えで比べるため）
            for it in items:
                it["前の読み違え"] = first(it)
            if args.sample and BASE in args.judge:
                # いまの判定（3.5 Flash Lite）を測り直すときも、見本（前の読みで誤った・決められなかった答え＋ほかから無作為に --sample 個）
                # だけにする。見本は、前の読みを捨てる前に決める（捨てたあとでは、どれが難しい答えか分からない）
                for it in pick_sample(items, args.sample):
                    it["見本"] = True
            items = [{k: v for k, v in it.items() if not any(k == j or (k.startswith(j) and k[len(j):] in ("秒", "誤り", "言い切り", "確からしさ", "言い切りの確からしさ", "会話", "会話の確からしさ", "by", *SIDE_SUFFIX.values(), *SPREAD_RECORD))
                                                             for j in args.judge)} for it in items]
        items = [g73_scene(it) for it in items]   # 前の記録の試しの言葉にも、本番で判定に渡る前の言葉を当てる
        have = {(it["set"], it["qid"], it["text"]) for it in items}
        # 電話を終える言葉の試しの表の言葉・G-73 の試しの言葉（表・言葉を足したら、足した行だけ判定する）
        items += [it for it in set_f() + set_n() + set_h() + set_r() if (it["set"], it["qid"], it["text"]) not in have]
        if args.add_de and not any(it["set"] in ("D", "E") for it in items):
            names = {m.display_name: m.name.split("/")[-1] for m in ring.client().models.list() if getattr(m, "display_name", None)}
            for m in MAKERS_DE:
                for make, these in ((make_d, qs + vqs), (make_e, qs)):
                    got, _ = ring.call(names[m], lambda c: make(c, names[m], these))
                    say(m, "が作った", make.__name__, len(got))
                    items += got
    else:
        ids = ring.model_ids()
        items = set_a() + set_b(qs) + set_f() + set_n() + set_h() + set_r()
        for m in MAKERS:
            got, _ = ring.call(ids[m], lambda c: make_c(c, ids[m], qs))
            say(m, "が作った答え", len(got))
            items += got
    say("答えの数", dict(Counter(it["set"] for it in items)))
    args.sets = chosen_sets(args.sets, {it["set"] for it in items})   # 組を選ぶ所（判定・前の話・組み立てだけの数え）はどれもこれを読む
    from g73_system_words import require_complete
    if args.dry_run:
        # 組み立てだけ：判定を送る数だけ出して止める（⛔ 2026-11-02 日本時間7時29分：ここで止まらず、2,840回送った）
        todo = [it for it in items if in_scope(args, it, qmap)]
        say("組み立てだけ（--dry-run）：判定は送りません。送る数", {j: sum(1 for it in todo if j not in it) for j in args.judge},
            "前の話の答え", len(prior_todo(args, items, qmap)))
        require_complete(args, "組み立てだけ", stop=False)   # 試しの言葉がそろっているかも出す（止めない）
        return
    ids = ids or ring.model_ids()
    require_complete(args, "答えの段の測り（G-53）")
    say("前の話の答えを読んだ数", read_priors(args, items, qmap))
    run_judges(args, items, qmap, ids)
    # 料金をかけた読みは、数える前に残す（⛔ 2026-11-15 日本時間13時台：数えで止まり、判定し終えた最後の46行の読みが写しに残らなかった）
    save_partial(f"G-53{args.tag}", {"items": items})
    summary = tally(items, args.judge, qmap)
    summary["前の話の答え（組の1か所）"] = dict(Counter(f"{it['set']}・{it['qid']}・{it[PRIOR_KEY]}" for it in items if PRIOR_KEY in it))
    summary["記録の通話に当てた見込み"] = {k: predict_calls(items, k) for k in args.judge}
    # ⛔ 2026-11-08 日本時間12時台まで、2つ目の判定の読みを BASE の読み（会話AIの報告に見立てた）と比べて、食い違えば確かめに回ると数えていた（versus）。
    # 会話AIは判定に加えない（✅ 12時11分40秒）＝その比べ方は本番に無いので外した（判定の組の決め方は g59_tally の cascade＝本番の Race の1か所）
    for k, v in summary.items():
        say(k, json.dumps({kk: vv for kk, vv in v.items() if kk != "⛔別の答えの例"}, ensure_ascii=False))
    save_result(f"G-53{args.tag}", {"model_ids": ids, "summary": summary, "items": items})


if __name__ == "__main__":
    main()
