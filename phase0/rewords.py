"""申し出でない業務の質問を、会話が繋がらないお返事のあとに「ところで」に続けて伺い直すときの、言い換えた質問の文を取込のときに決める（人は確かめない）。

✅ 2026-11-13 日本時間22時17分01秒 ユーザー「9は「さようでございますか。ところで、お支払いの方はカードと振込のどちらにされますか？」」（伺い9。同じ問いを同じ
言い方で2度言わない＝11-01「全く同じ質問を2度したら、機械だと思われてしまいませんか？」）。

使う所：flowctl.Question.reworded（流す形は flowctl.aside_form の1か所。頭の「ところで」は phrases.ASIDE_LEAD＝プログラムが付ける）。

決め方（announces のお知らせの文と同じ1か所＝import_ai._drafted）：2つの文字のAIの文がそろい、形に合う（fits）ときだけ候補。そろわない・形に合わないときは、
考える Sonnet（import_ai.decide_rewords の referee）にその質問だけを書かせ、形に合えば候補。候補は中身の判定の1か所（askitems.grounded＝grounding の問い）で、
もとの質問に無い事実を足していないかを考える Sonnet に読ませ、通ったときだけ使う。ほかは空＝「ところで」＋元の質問の文で伺い直す。
"""
from __future__ import annotations

from announces import keeps_facts
from phrases import ASIDE_LEAD, head_word

# 頭に付けない言葉（「ところで」はプログラムが付ける・ほかの頭は「ところで」に続かない）
LEADS = (ASIDE_LEAD, "それでは", "では", "恐れ入りますが", "続きまして", "さて")
KINDS = {"yes_no": "はい／いいえで答える", "date": "日にち", "time": "時刻", "datetime": "日時", "number": "数", "amount": "金額", "name": "お名前",
         "phone": "電話番号", "email": "メールアドレス", "address": "ご住所", "line_id": "LINE の ID", "mobile": "携帯電話の番号", "free_text": "お考え・ご事情"}

PROMPT = (
    "電話でお客様に伺う質問の一覧です。お客様が質問に答えずに別のお話をされたので、こちらはお話にひとこと応えてから、「" + ASIDE_LEAD + "」に続けて、"
    "同じことを違う言い方でもう一度伺います。質問ごとに、その伺い直す質問の文を line に答えてください（「" + ASIDE_LEAD + "」はこちらで付けるので書きません）。\n"
    "決まり：\n"
    "- 例：「お支払いはカードと振込のどちらがよろしいでしょうか？」なら「お支払いの方はカードと振込のどちらにされますか？」。\n"
    "- 同じ答えで答えられる質問にします。選択肢のある質問は、質問の文にある選択肢の言葉をそのまま入れます。数・金額・日にち・期限は、書いてあるとおりに入れます。\n"
    "- 質問の文に無い事実は足しません。元の文と同じ文にはしません。\n"
    "- 「？」で終わる1つの問いの文にします。読点（「、」）は入れません。頭に「" + "」「".join(LEADS) + "」などのつなぎの言葉は付けません。\n"
    "質問（番号：質問の文／答え方）：\n"
)
FIELD, DESC = "line", "「" + ASIDE_LEAD + "」に続けて同じことを違う言い方で伺う質問の文"
TOOL = ("reworded_questions", "質問ごとの、言い換えた質問の文")


def _answers(conditions: tuple, qtype: str) -> str:
    return f"選択肢：{'・'.join(conditions)}" if qtype == "choice" else KINDS.get(qtype, "")


def fits(text: str, conditions: tuple, qtype: str, line: str | None) -> bool:
    """言い換えた質問として使えるか（形の決まりの唯一の場所）：「？」で終わる1つの問いの文・読点が無い（10-15）・元の文と同じでない・頭につなぎの言葉が無い
    （「ところで」に続けて自然）・数と期限の言葉を落としていない（announces.keeps_facts）・選ぶ質問は元の文にある選択肢の言葉を落としていない。"""
    if not line or line == text or not line.endswith("？") or line.count("？") != 1 or any(m in line for m in ("?", "。", "、", "，")):
        return False
    if line.startswith(LEADS) or head_word(line):
        return False
    if not keeps_facts(text, line):
        return False
    return qtype != "choice" or all(c in line for c in conditions if c in text)


def by_model(client, model: str, items: list[tuple[str, tuple, str]], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。items（(質問の文, 選択肢の並び, 型)）と同じ並びの答え（無ければ None）を返す（問い合わせは
    askitems.ask の1か所。think＝考える形で聞く＝決まらなかった質問だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{text}／{_answers(conds, qtype)}" for n, (text, conds, qtype) in enumerate(items, 1))
    got_items = ask(client, model, PROMPT + body, {"no": ("integer", "質問の番号"), FIELD: ("string", DESC)}, tool=TOOL, think=think)
    got = {x.get("no"): x.get(FIELD) for x in got_items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(items) + 1)]


def decide(text: str, conditions: tuple, qtype: str, model_lines: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその文・ほかは None（考える Sonnet に聞く＝import_ai.decide_rewords）。"""
    from askitems import agreed

    got = agreed(model_lines, None)
    return got if fits(text, conditions, qtype, got) else None


def grounded(client, model: str, text: str, conditions: tuple, qtype: str, line: str) -> bool:
    """言い換えた質問が、元の質問（選択肢・答え方も）に無い事実を足していないか（中身の判定の1か所＝askitems.grounded）。通ったときだけ True。"""
    from askitems import grounded as check

    return check(client, model, [text, _answers(conditions, qtype)], line, ("grounding", "言い換えた質問の中身"))
