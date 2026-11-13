"""業務の質問の文（依頼者のトークスクリプト）から、「その後（質問の中身）かお決まりになりましたでしょうか？」の中身の言葉を決める（取込のとき。人は確かめない）。

✅ 2026-11-13 日本時間20時06分19秒 ユーザー「伺い6は「…でお電話いたしました。その後ご予算はおいくらほどかお決まりになりましたでしょうか？」」
＝CL-2 がかけ直す「わからない」（flowctl.Question.unknown_called_back）を、続きの電話で前の電話でお話しした方に伺い直す文（phrases.since_ask）。

使う所：flowctl.Question.since（この形で伺うかは flowctl.since_due の1か所）。

決め方（purposes と同じ形＝問い合わせと読み取りは askitems.ask、合わせ方は askitems.agreed）：2つの文字のAIの答えがそろい、形に合う（fits）ときだけ、その言葉。
そろって空（後から決まることを伺う質問ではない）なら空。そろわない・答えが無い・形に合わないときは、考える Sonnet（import_ai.decide_since の referee）に、
その文だけを聞き、形に合えばその言葉。ほかは空＝今の形で伺い直す（違う中身を言うことはない側へ倒す）。
"""
from __future__ import annotations

from phrases import SINCE_END, SINCE_HEAD

PROMPT = (
    "電話でお客様に伺った質問の一覧です。前の電話でお客様が「まだ分からない・まだ決まっていない」と答えた質問を、次の電話で伺い直すときに、\n"
    f"「{SINCE_HEAD}（中身）{SINCE_END}」の文にします。質問ごとに、（中身）に入る言葉を words に答えてください。\n"
    "決まり：\n"
    "- 例：「ご予算はおいくらほどでしょうか？」なら「ご予算はおいくらほど」。「ご導入の時期はいつ頃をお考えでしょうか？」なら「ご導入の時期はいつ頃」。\n"
    f"- 言葉は、質問の文の言い方のまま短くし、「{SINCE_HEAD}」「か」「{SINCE_END}」は入れません。「？」「。」も入れません。\n"
    "- 後から決まる・分かるようになることを伺う質問でなければ（お名前・すでにしたことなど）、空にします。\n"
    "質問（番号：質問の文）：\n"
)
FIELD, DESC = "words", "その後の文の（中身）に入る言葉（入れる質問でなければ空）"
TOOL = ("since_words", "質問ごとの、その後の文の中身の言葉")


def by_model(client, model: str, texts: list[str], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。texts と同じ並びの答え（無ければ None）を返す（問い合わせは askitems.ask の1か所。
    think＝考える形で聞く＝決まらなかった文だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{t}" for n, t in enumerate(texts, 1))
    items = ask(client, model, PROMPT + body, {"no": ("integer", "質問の番号"), FIELD: ("string", DESC)}, tool=TOOL, think=think)
    got = {x.get("no"): x.get(FIELD) for x in items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(texts) + 1)]


def fits(words: str | None) -> bool:
    """その後の文（phrases.since_ask）の中身に入れられる言葉か（形の決まりの唯一の場所）：空でない・問いや文の終わりを含まない・頭の「その後」や終わりの「か」を
    重ねない。"""
    return (bool(words) and not any(m in words for m in ("？", "?", "。", "お決まりになり", "お分かりになり"))
            and not words.startswith(SINCE_HEAD) and not words.endswith("か"))


def decide(model_words: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその言葉・そろって空なら空（入れる質問ではない）・ほかは None（考える Sonnet に聞く＝
    import_ai.decide_since）。"""
    from askitems import agreed

    got = agreed(model_words, None)
    if got is None:
        return None
    return got if got == "" or fits(got) else None
