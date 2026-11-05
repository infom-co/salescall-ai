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
from flowctl import (ANY, CHOICE_TYPES, asks_sense, END_ANGRY, END_HURRY, END_NONE, END_REASON, END_REFUSED, HANDOVER, HOLD, NONE, SYSTEM_CONDITIONS, Flow, Question,
                     off_talk, prior_answer, restates)
from g48_meanings import QUESTIONS as G48
from meaning_judge import GROUPS, JUDGE_MODELS, OTHER_JUDGES, UNDECIDED, firm_matters, judge, judge_other

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
    if it["set"] == "B" and ((it["qid"], None) in FLIP or (it["qid"], it["maker"]) in FLIP):
        return {"はい": "いいえ", "いいえ": "はい"}[it["truth"]]
    return it["truth"]


_LABELS: dict | None = None


def accept(it: dict) -> list[str]:
    """受け取ってよい読み（正解。数えるのはすべてここ）。付け直した正解（g53_labels.json）があればそれ、無ければ truth の1つ。
    「決められない」と「わからない」は、どちらで受け取っても誤った値は渡らないので、同じに数える。
    ⛔ 2026-10-18 ユーザー：「『メールのほうが助かりますが、電話でも大丈夫です』なら…決まらないではないですよね」＝作らせたモデルの付けた
       正解を、中身を見ずに使っていた ⇒ CL-1 が1つずつ読んで付け直した（望みが言えていれば答え・事実がはっきりしなければ確かめる・
       どちらとも言っていなければ決められない。境目は、どちらの読みも正解）"""
    got = _label(it)
    if got:
        return got["accept"]
    want = truth(it)
    return [UNDECIDED, "unknown"] if want in (UNDECIDED, "unknown") else [want]


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
END_GO, END_TOPIC, END_ASK = "続ける", "いまの話の向きを伺う", "ご遠慮を伺う"


def end_act(q: Question, got, end) -> str | None:
    """判定の読み（答え got・電話を終える言葉か end）から、流れの制御がすること（読みが無ければ None）：電話を終える言葉なら締める・急いでいる方への
    伺い／言っていないなら続ける（答えは flow_act のとおり）／決められないなら、いまの質問の選択肢の答えとも読めれば、その向きを伺う（_topic_first）、
    読めなければ、ご遠慮を伺う（_refusal_confirm）。答えの段の読みに、取り次げない3つは無い（end_choices）。"""
    if end is None:
        return None
    if end in END_REASON:
        return END_ACTS.get(end, f"締める（{end}）")
    if end == END_NONE:
        return END_GO
    return END_TOPIC if q.type in CHOICE_TYPES and got in q.conditions else END_ASK


def end_step(act: str) -> bool:
    """流れの制御がすること act が、電話を終える読みによる動き（締める・急いでいる方への伺い・いまの話の向きを伺う・ご遠慮を伺う）か。"""
    return act in (END_TOPIC, END_ASK) or act in END_ACTS.values() or act.startswith("締める（")


def step_act(it: dict, q: Question, key: str) -> str | None:
    """1つの発話で、判定 key の読みから流れの制御がすること（数えるのはここ）：電話を終える言葉か「決められない」と読めば、会話AIの報告より
    先にその読みで動く（Flow._end_due_now）＝end_act。「言っていない」・項目を足す前の記録（読みに無い）は、答えの読みで動く＝flow_act。"""
    end = it.get(f"{key}終わり")
    if end is not None and end != END_NONE:
        return end_act(q, it.get(key), end)
    return flow_act(it, q, it.get(key), it.get(f"{key}言い切り"), prior_of(it, key))


# 前の話の答えを、組の1か所で1回読んで入れる欄（read_priors。本番の判定の組 JudgePanel.ask と同じく、どの判定の読みにも同じものを添える）
PRIOR_KEY = "前の話"


def prior_of(it: dict, key: str) -> str | None:
    """判定 key の読みに添える前の話の答え（唯一の読み口）：判定ごとに読んだもの（G-70 の行）が無ければ、組の1か所で読んだもの（read_priors）。"""
    return it[f"{key}前の話"] if f"{key}前の話" in it else it.get(PRIOR_KEY)


def step_wants(it: dict, q: Question, has_end: bool) -> set[str]:
    """正解から決まる、1つの発話で流れの制御がすべきこと（どれをしても正しい）＝step_act と同じ組み立て：電話を終える言葉かの正解ごとに、
    「言っていない」なら答えの動き（flow_wants）、ほかは end_act。has_end＝判定の読みに電話を終える言葉かがある（無い記録は答えの動きだけ）。"""
    if not has_end:
        return flow_wants(it, q)
    out: set[str] = set()
    for e in end_accept(it):
        out |= flow_wants(it, q) if e == END_NONE else {end_act(q, a, e) for a in accept(it)}
    return out


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
    return Reading(got, it.get(f"{key}言い切り") is not False)


def either(it: dict) -> bool:
    """言い切っていても、いなくても正しい答え（付け直した正解の either。境目＝どちらの読みも正解）。⚠️ 2026-10-31：「〜のほうがいいですかね」
    「決まってないわけではないです」「メールが埋もれてしまって」のように、答えは決まるが、言い切りかは人でも分かれるもの"""
    got = _label(it)
    return bool(got and got.get("either"))


def talk(it: dict) -> bool | None:
    """前の質問（と前の会話）への返事として会話になっているか（付け直した正解の talk。無ければ None＝なっている）。✅ 2026-10-31 ユーザー：
    「この部分だけの判断ならAIがあっています。前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」"""
    got = _label(it)
    return got.get("talk") if got else None


def mark(it: dict, key: str) -> str | None:
    """「曖昧」の印の数え：寄っている答えを正しく選んだときだけ（値が誤っていれば、印の前に誤り）。"""
    if it.get(key) not in accept(it) or it[key] in (UNDECIDED, "unknown", HOLD, HANDOVER) or either(it):
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
    """流れの制御が、その答えを言い直して確かめるときに実際に言う言葉（Flow._ask_confirm。言い直せなければ None）。"""
    f = Flow([q])
    f.items[q.id].condition, f.items[q.id].value = cond, value
    r = f._ask_confirm(q.id, after="ambiguous")
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
    return got not in accept(it) and (got in q.conditions or got in (ANY, NONE)) and it.get(f"{key}言い切り") is not False


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


def flow_act(it: dict, q: Question, got, firm, prior=None) -> str | None:
    """判定の読み got（firm＝言い切っているか・prior＝前の話の答え）から、流れの制御がすること（数えるのはここ）。会話になっていない
    返事（前の話の答えと食い違う選択肢の読み）は、その読みで念のため確かめる（言い直しへの返事なら確かめ直す。flowctl.TALK_WHY・
    Flow._check_again。✅ 2026-10-31 ユーザー：前に言ったことと逆の返事は「(イ)」＝言い直して確かめる）。言い直さない質問（W1・C11・N1〜N3＝
    flowctl.restates の1か所）は、どちらも質問から聞き直す。"""
    if got is None:
        return None
    if off_talk(q, got, prior):   # 会話になっていない返事（流れの制御 Flow._judged_off_talk と同じ1か所の決まり。読みはいつも宣言した選択肢）
        if not restates(q):
            return SENSE + got if asks_sense(q, got) else ASK_AGAIN
        return RECHECK if it["set"] == "D" else f"念のため確かめる：{got}"
    if (pa := prior_answer(q, got, prior)) is not None:   # いまの言葉だけでは決められず、前の話の答えを確かめる（Flow._compare と同じ）
        return ASK_AGAIN if not restates(q) else f"念のため確かめる：{pa}"
    if it["set"] == "D":
        if got == "affirmed" or got == it.get("asked_choice"):
            return AFFIRMED_HEDGED if firm is False else AFFIRMED
        return ASK_AGAIN if got in (UNDECIDED, "unknown") else DENIED
    if got in (UNDECIDED, "unknown"):
        return ASK_AGAIN
    # 言い切っていない読みは、選択肢も「どちらでも」「どれも要らない」も確かめる（流れの制御は「わからない」「答えたくない」のほかを確かめる＝
    # Flow._checks。⛔ 2026-11-02 に見つけた：この数えは「どちらでも」を確かめずに受け取ると数えていた＝流れの制御の動きと違う）
    # 言い切りで動きが変わる読みかは meaning_judge.firm_matters の1か所（判定の組が確かな読みかを決めるのと同じ）
    return f"念のため確かめる：{got}" if firm_matters(q, got) and firm is False else f"受け取る：{got}"


def flow_wants(it: dict, q: Question) -> set[str]:
    """正解の表から決まる、流れの制御がすべきこと（どれをしても正しい）。会話になっていない返事（talk が False）は、言葉の意味の答えを念のため
    確かめる（言い直しへの返事なら確かめ直す）か、聞き直す。"""
    if talk(it) is False:
        return {ASK_AGAIN} | {SENSE + a if asks_sense(q, a) else RECHECK if it["set"] == "D" else f"念のため確かめる：{a}"
                              for a in accept(it) if a in q.conditions}
    h, e, ok = hedged(it), either(it), set()
    for a in accept(it):
        if it["set"] == "D":
            if a == "affirmed" or a == it.get("asked_choice"):
                ok |= {AFFIRMED, AFFIRMED_HEDGED} if e else {AFFIRMED_HEDGED if h else AFFIRMED}
            else:
                ok.add(ASK_AGAIN if a in (UNDECIDED, "unknown") else DENIED)
        elif a in (UNDECIDED, "unknown"):
            ok.add(ASK_AGAIN)
        elif a in q.conditions or a in (ANY, NONE):
            ok |= {f"受け取る：{a}", f"念のため確かめる：{a}"} if e else {f"念のため確かめる：{a}" if h else f"受け取る：{a}"}
        else:
            ok.add(f"受け取る：{a}")
    return ok


def flow_miss(it: dict, q: Question, key: str) -> str | None:
    """判定 key の読みで、1つの発話の流れの制御の動き（step_act）が正解（step_wants）と違えば、その違いの種類（正しければ None・読みが無ければ
    「読み無し」）。答えの動きと電話を終える読みの動きを1か所で数える（唯一の数え）。⛔ 2026-11-05 に1つにした：前は答えの動き（ここ）と電話を
    終える言葉の動き（end_miss）を別々に数え、正しく急ぎを伺う発話でも答えの読みが違えば外れと数え、電話を終える言葉ではないのに急ぎを伺う
    読みを、答えが正しければ「正しい動き」と数えていた。"""
    act = step_act(it, q, key)
    if act is None:
        return "読み無し"
    want = step_wants(it, q, it.get(f"{key}終わり") is not None)
    if act in want:
        return None
    ends = {w for w in want if end_step(w)}
    closes = {w for w in ends if w not in (END_TOPIC, END_ASK)}
    if end_step(act):
        if act not in (END_TOPIC, END_ASK):
            return "⛔違う理由で締める・伺う" if closes else "⛔電話を終える言葉ではないのに締める・急ぎを伺う"
        if closes:
            return "⛔電話を終える言葉なのに締めない"
        return "違う伺い" if ends else "要らない伺い"
    want -= ends
    if not want:   # 正解は電話を終える読みの動きだけ（答えの読みで続けた）
        return "⛔電話を終える言葉なのに締めない" if closes else "⛔伺わずに続ける"
    pure = set(accept(it)) <= {UNDECIDED, "unknown"}   # どの答えにも寄っていない言葉
    if act in (AFFIRMED, AFFIRMED_HEDGED) or act.startswith("受け取る"):
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


def versus(items: list[dict], qmap: dict, key: str) -> dict:
    """2つ目の判定の候補（key）を、いまの判定（BASE）と同じ答えで比べる。BASE の読みを会話のAIの報告とみなし、key の読みが、流れの制御の
    決まり（Flow._meaning_disagrees）で食い違うか（＝確かめに回るか）を数える。"""
    c = Counter()
    for it in items:
        if BASE not in it or key not in it or it.get("maker") in (BASE, key) or it["qid"] not in qmap or it["set"] == "HEAR":
            continue
        q, b = qmap[it["qid"]], it[BASE]
        clash = Flow._meaning_disagrees(q, b, it[key], confirming=it["set"] == "D") is not None
        # 最初の答えは、候補が言い切っていないと読んでも確かめに回る（言い直しへの返事は、印を付けて受け取る＝確かめには回らない）
        clash = clash or (it["set"] != "D" and b not in SYSTEM_CONDITIONS and Flow._judged_unsure({"judge": reading(it, key)}))
        c["両方が判定"] += 1
        if dangerous(it, q, BASE):
            c["いまの判定が危ない向き"] += 1
            c["⇒ 候補が食い違う（確かめに回る）" if clash else "⇒ ⛔候補も食い違わない（そろって通る）"] += 1
        elif b in accept(it):
            c["いまの判定が正しい"] += 1
            if clash:
                c["⇒ 候補が食い違う（要らない確かめ）"] += 1
        if dangerous(it, q, key) and not dangerous(it, q, BASE):
            c["候補だけが危ない向き"] += 1
    return dict(c)


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
            if key not in it and it.get("maker") != key and (not args.sets or it["set"] in args.sets):
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
                it[f"{key}終わり"] = getattr(got, "end", None)   # 電話を終える言葉か（flow_miss で数える）
                it[f"{key}聞こえ"] = getattr(got, "hear", None)   # こちらの声が届いていない様子か（hear_miss で数える）
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
                    it[f"{key}終わり"] = getattr(it[key], "end", None)
                    it[f"{key}聞こえ"] = getattr(it[key], "hear", None)
                    it[f"{key}確からしさ"] = getattr(it[key], "sure", None)  # Jev の線（OTHER_JUDGES の sure_min）で、同時に出した組の読みを見積もるため
                    it[f"{key}言い切りの確からしさ"] = getattr(it[key], "firm_p", None)   # 言い切りの線と、どちらとも言えない幅を決めるため
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
            and (not args.sets or it["set"] in args.sets)][:args.limit or None]
    sem, done = asyncio.Semaphore(lanes), Counter()

    async def one(it):
        async with sem:
            panel = JudgePanel({"judge2": fns})   # 答えごとに新しい受け持ち（前の答えの時間切れ・会社の印を持ち越さない）
            t = time.monotonic()
            try:
                got = await panel.ask("judge2", qmap[it["qid"]], it["text"], it.get("asked"), it.get("before"))
                it[name], it[f"{name}言い切り"], it[f"{name}by"], it[f"{name}終わり"] = str(got), got.firm, got.by, getattr(got, "end", None)
                it[f"{name}聞こえ"] = getattr(got, "hear", None)
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
    return [it for it in items if it.get("before") and PRIOR_KEY not in it and it["qid"] in qmap and (not args.sets or it["set"] in args.sets)
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
        t = defaultdict(Counter)
        for it in items:
            if key not in it:
                continue
            if it.get("maker") == key:
                continue  # 自分で作った答え
            if it.get(f"{key}聞こえ") is not None or it["set"] == "HEAR":   # こちらの声が届いていない様子か（項目を足す前の記録は数えない）
                h, hw = it.get(f"{key}聞こえ"), hear_truth(it)
                t["こちらの声が届いていない様子か"][f"{'組 HEAR' if it['set'] == 'HEAR' else 'ほかの組'}・{hw}・"
                                         f"{'正しい' if h == hw else '⛔' + str(h)}"] += 1
            if it["set"] == "HEAR":
                continue  # 組 HEAR は答えの正解を付けていない（聞こえ方だけを数える）
            got, want = it[key], truth(it)
            res = "正しい" if got in accept(it) else ("決められない（確かめる）" if got in (UNDECIDED, "unknown") else "⛔別の答え")
            label = {"B": "選択肢", "D": it.get("kind")}.get(it["set"], want)
            t[f"{it['set']}・{label}"][res] += 1
            t["全体"][res] += 1
            if qmap and it["qid"] in qmap:
                t["流れの制御の動き"][flow_miss(it, qmap[it["qid"]], key) or "正しい動き"] += 1   # 答えと電話を終える読みの1つの動き
                if it.get(f"{key}終わり") is not None:   # 電話を終える言葉か（項目だけ。項目を足す前の記録は数えない）
                    t["電話を終える言葉か"]["正しい" if it[f"{key}終わり"] in end_accept(it) else "⛔別の読み"] += 1
            if (m := mark(it, key)):
                t["全体"][f"曖昧の印・{m}"] += 1
            if misread_as_affirmed(it, key):
                t["全体"]["⛔⛔言い直しを、認めていないのに認めたと読んだ"] += 1
        secs = sorted(it[f"{key}秒"] for it in items if it.get(f"{key}秒") is not None)
        out[key] = {"数え": {k: dict(v) for k, v in t.items()},
                    "かかった秒（中央・遅いほうから1割・最大）": [secs[len(secs) // 2], secs[int(len(secs) * 0.9)], secs[-1]] if secs else None,
                    "⛔別の答えの例": [f"{it['qid']}「{it['text']}」正解={accept(it)} 判定={it[key]}" for it in items
                                   if key in it and it[key] not in accept(it) + [UNDECIDED, "unknown"]][:80],
                    "⛔流れの制御の動きの外れの例": [f"{it['set']}・{it['qid']}「{it['text']}」正解={accept(it)}・{end_accept(it)} 判定={it[key]}・"
                                            f"{it.get(f'{key}終わり')}＝{flow_miss(it, qmap[it['qid']], key)}" for it in items
                                            if qmap and key in it and it["qid"] in qmap and it["set"] != "HEAR"
                                            and flow_miss(it, qmap[it["qid"]], key) not in (None, "読み無し")][:80]}
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


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--judge", action="append", choices=JUDGES, help="判定のモデル（既定は2つとも）")
    p.add_argument("--reuse", help="前の結果のファイル（作った答え・判定済みの分を使い回す）")
    p.add_argument("--tag", default="", help="結果のファイル名に足す印（同時に流す回を分けるため）")
    p.add_argument("--sets", help="判定する組だけ（例 E、DE）。ほかの組は判定しない（数えには入る）")
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
    ids = ring.model_ids()
    qmap = question_map()
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
            items = [{k: v for k, v in it.items() if not any(k == j or (k.startswith(j) and k[len(j):] in ("秒", "誤り", "言い切り", "確からしさ", "言い切りの確からしさ", "会話", "会話の確からしさ", "by", "終わり", "聞こえ"))
                                                             for j in args.judge)} for it in items]
        items = [g73_scene(it) for it in items]   # 前の記録の試しの言葉にも、本番で判定に渡る前の言葉を当てる
        have = {(it["set"], it["qid"], it["text"]) for it in items}
        # 電話を終える言葉の試しの表の言葉・G-73 の試しの言葉（表・言葉を足したら、足した行だけ判定する）
        items += [it for it in set_f() + set_n() + set_h() if (it["set"], it["qid"], it["text"]) not in have]
        if args.add_de and not any(it["set"] in ("D", "E") for it in items):
            names = {m.display_name: m.name.split("/")[-1] for m in ring.client().models.list() if getattr(m, "display_name", None)}
            for m in MAKERS_DE:
                for make, these in ((make_d, qs + vqs), (make_e, qs)):
                    got, _ = ring.call(names[m], lambda c: make(c, names[m], these))
                    say(m, "が作った", make.__name__, len(got))
                    items += got
    else:
        items = set_a() + set_b(qs) + set_f() + set_n() + set_h()
        for m in MAKERS:
            got, _ = ring.call(ids[m], lambda c: make_c(c, ids[m], qs))
            say(m, "が作った答え", len(got))
            items += got
    say("答えの数", dict(Counter(it["set"] for it in items)))
    from g73_system_words import require_complete
    if args.dry_run:
        # 組み立てだけ：判定を送る数だけ出して止める（⛔ 2026-11-02 日本時間7時29分：ここで止まらず、2,840回送った）
        todo = [it for it in items if not args.sets or it["set"] in args.sets]
        say("組み立てだけ（--dry-run）：判定は送りません。送る数", {j: sum(1 for it in todo if j not in it) for j in args.judge},
            "前の話の答え", len(prior_todo(args, items, qmap)))
        require_complete(args, "組み立てだけ", stop=False)   # 試しの言葉がそろっているかも出す（止めない）
        return
    require_complete(args, "答えの段の測り（G-53）")
    say("前の話の答えを読んだ数", read_priors(args, items, qmap))
    run_judges(args, items, qmap, ids)
    summary = tally(items, args.judge, qmap)
    summary["前の話の答え（組の1か所）"] = dict(Counter(f"{it['set']}・{it['qid']}・{it[PRIOR_KEY]}" for it in items if PRIOR_KEY in it))
    summary["記録の通話に当てた見込み"] = {k: predict_calls(items, k) for k in args.judge}
    summary[f"{BASE} と同じ答えで比べた"] = {k: versus(items, qmap, k) for k in args.judge if k != BASE}
    for k, v in summary.items():
        say(k, json.dumps({kk: vv for kk, vv in v.items() if kk != "⛔別の答えの例"}, ensure_ascii=False))
    save_result(f"G-53{args.tag}", {"model_ids": ids, "summary": summary, "items": items})


if __name__ == "__main__":
    main()
