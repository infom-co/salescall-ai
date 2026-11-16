"""急いでいるが手短になら話を聞くと言われたときに「ありがとうございます。」のあとに流す、業務の営業の要約（instruction.Business.summary）を取込のときに決める
（人は確かめない）。

✅ 2026-11-16 日本時間0時42分38秒 ユーザー：「伺い30：「ありがとうございます。～（営業の要約）」手短にと言っているのに、いきなり、「お電話とメール」を伺うなんて
ありえないでしょ。」

使う所：instruction.Business.summary（依頼主の単位の録音＝名乗りの部分 summary。流す所は flowctl.Flow._brief_say の1か所。決まらない業務は用件の一文の録音）。

決め方（お知らせの文と同じ1か所＝import_ai._drafted）：2つの文字のAIの文がそろい、形に合う（fits）ときだけ候補。そろわない・形に合わないときは、考える Sonnet
（import_ai.decide_summaries の referee）にその業務だけを書かせ、形に合えば候補。候補は中身の判定の1か所（askitems.grounded＝grounding の問い）で、業務の台本（用件の
一文と質問の文）に無い事実を足していないかを考える Sonnet に読ませ、通ったときだけ使う。ほかは空＝用件の一文の録音。
"""
from __future__ import annotations

SENTENCES = 2   # 要約の文の数の上限（1〜2文）

PROMPT = (
    "電話でお客様に営業のお電話をしたところ、お客様が「急いでいるので手短に」とおっしゃいました。こちらは「ありがとうございます。」に続けて、この電話の用件を短く"
    "お伝えします。業務ごとに、その要約の文を line に答えてください。\n"
    "決まり：\n"
    "- 何の用件で、何をお伺いしたいか（または何をご案内したいか）を、1つか2つの文で短く書きます。\n"
    "- 問いかけ（「？」）にしません。読点（「、」）は入れません。「ありがとうございます」などの頭の言葉は付けません（こちらで付けます）。\n"
    "- 用件の一文と質問の文にある内容だけを使います。数・金額・日にち・期限は、書いてあるとおりに入れます。無い事実は足しません。\n"
    "- 例：用件の一文「ご予約の確認でお電話いたしました。」・質問「ご予約はされていますでしょうか？／お支払いはカードと振込のどちらがよろしいでしょうか？」なら"
    "「ご予約の確認とお支払いの方法をお伺いしたくお電話いたしました。」。\n"
    "業務（番号：用件の一文／質問の文）：\n"
)
FIELD, DESC = "line", "この電話の用件を短くお伝えする要約の文（1〜2文）"
TOOL = ("call_summaries", "業務ごとの、営業の要約の文")


def _body(purpose: str, texts: tuple) -> str:
    return f"{purpose}／{'／'.join(texts)}"


def fits(purpose: str, texts: tuple, line: str | None) -> bool:
    """営業の要約として使えるか（形の決まりの唯一の場所）：1〜2文（「。」で終わる）・問いでない・読点が無い（決まった言い方＝10-15）・業務の台本に無い数と期限の言葉を
    足していない（announces.keeps_facts を、要約から台本へ向けて当てる＝要約は短くするので、台本の数を全部は求めない）。"""
    from announces import keeps_facts

    if not line or not line.endswith("。") or line.count("。") > SENTENCES or "。。" in line or any(m in line for m in ("？", "?", "、", ",")):
        return False
    return keeps_facts(line, _body(purpose, texts))


def by_model(client, model: str, items: list[tuple[str, tuple]], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。items（(用件の一文, 質問の文の並び)）と同じ並びの答え（無ければ None）を返す（問い合わせは
    askitems.ask の1か所。think＝考える形で聞く＝決まらなかった業務だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{_body(p, t)}" for n, (p, t) in enumerate(items, 1))
    got_items = ask(client, model, PROMPT + body, {"no": ("integer", "業務の番号"), FIELD: ("string", DESC)}, tool=TOOL, think=think)
    got = {x.get("no"): x.get(FIELD) for x in got_items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(items) + 1)]


def decide(purpose: str, texts: tuple, model_lines: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその文・ほかは None（考える Sonnet に聞く＝import_ai.decide_summaries）。"""
    from askitems import agreed

    got = agreed(model_lines, None)
    return got if fits(purpose, texts, got) else None


def grounded(client, model: str, purpose: str, texts: tuple, line: str) -> bool:
    """要約が、業務の台本（用件の一文と質問の文）に無い事実を足していないか（中身の判定の1か所＝askitems.grounded）。通ったときだけ True。"""
    from askitems import grounded as check

    return check(client, model, [purpose, *texts], line, ("grounding", "営業の要約の中身"))
