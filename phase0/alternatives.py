"""選択肢から選んでいただく業務の質問を、お客様の意向がまだ分からないまま伺い直すときの、選択肢を並べた質問の文を取込のときに決める（人は確かめない）。

✅ 2026-11-14 日本時間22時15分15秒 ユーザー「・お客様：そうなんですね。こちら：はい、それではお支払いの方はカードと振込のどちらにされますか？⇒まだ、どちらの意向
なのか分からない状況で、「それでは」は、あたかも強制的に決められたかのように受け止められてしまいます。しかし、否定的な返事でもないときは、「はい、振り込みに
なさいますか、それともカードの方がよろしいでしょうか？」のような聞き方の方が流れが自然ではないでしょうか？」

使う所：flowctl.Question.alternatives（流す形は flowctl.soft_form の1か所。頭の「はい、」は相づちの録音＝プログラムが付ける）。作る質問は flowctl.alternatives_fits
（業務の選ぶ質問で、選択肢が2つ以上）。

決め方（言い換えた質問 rewords と同じ1か所＝import_ai._drafted）：2つの文字のAIの文がそろい、形に合う（fits）ときだけ候補。そろわない・形に合わないときは、
考える Sonnet（import_ai.decide_alternatives の referee）にその質問だけを書かせ、形に合えば候補。候補は中身の判定の1か所（askitems.grounded＝grounding の問い）で、
もとの質問に無い事実を足していないかを考える Sonnet に読ませ、通ったときだけ使う。ほかは空＝「はい、」＋言い換えた質問で伺い直す。
選択肢を並べる順は質問の選択肢の順。そのほかに、選択肢ごとにその選択肢を先に並べた文も同じ問い合わせで作る（orders の1か所。流れの制御が、直前のこちらの
ひとことが1つだけに触れた選択肢を先に並べた文で伺う＝flowctl.alternatives_form）。✅ 2026-11-14 日本時間23時45分00秒 ユーザー「伺い16はイでOK」。
質問の順の文からの並べ替え（文字の入れ替え）では作らない：選択肢に付く「お」（「お振込」）と、選択肢ごとの言い方（「メールでお送りしましょうか」）が選択肢と
一緒に動くため、入れ替えた文が崩れる（「おカード」）。
"""
from __future__ import annotations

import rewords
from announces import keeps_facts
from phrases import head_word

# 頭に付けない言葉（「はい、」は相づちの録音でこちらが付ける・つなぎの言葉は意向を決めつける＝お言葉）
LEADS = ("はい", *rewords.LEADS)
# 最後の選択肢の前に入れる言葉（選択肢を並べて伺う形のしるし＝「どちら」で伺う元の質問の形と分ける）
JOIN = "それとも"

PROMPT = (
    "電話でお客様に選んでいただく質問の一覧です。お客様のご意向がまだ分からないので、どれかに決めつけずに、選択肢を順に並べて同じことを伺い直します。"
    "質問ごとに、その伺い直す質問の文を line に答えてください（頭の「はい、」はこちらで付けるので書きません）。\n"
    "決まり：\n"
    "- 例：「お支払いはカードと振込のどちらがよろしいでしょうか？」（選択肢：カード・振込）なら「カードになさいますか" + JOIN + "お振込の方がよろしいでしょうか？」。\n"
    "- 同じ質問を、選択肢の順を変えて何度か並べることがあります。順を変えたものは「（選択肢：振込・カード）なら「お振込になさいますか" + JOIN
    + "カードの方がよろしいでしょうか？」」のように書きます。\n"
    "- 選択肢の言葉は、「選択肢：」に書いた順のまま全部入れ（質問の文の中の順とは違うことがあります）、最後の選択肢の前に「" + JOIN + "」を入れます。"
    "数・金額・日にち・期限は、書いてあるとおりに入れます。\n"
    "- 質問の文と選択肢に無い事実は足しません。\n"
    "- 「？」で終わる1つの問いの文にします。読点（「、」）は入れません。頭に「" + "」「".join(LEADS) + "」などのつなぎの言葉は付けません。\n"
    "質問（番号：質問の文／選択肢）：\n"
)
FIELD, DESC = "line", "選択肢を順に並べて伺い直す質問の文"
TOOL = ("listed_choices", "質問ごとの、選択肢を並べた質問の文")


def _answers(conditions: tuple) -> str:
    return f"選択肢：{'・'.join(conditions)}"


def orders(conditions: tuple) -> list[tuple]:
    """質問の順のほかに作る並べ方（唯一の決まり＝取込で作る import_ai.decide_alternatives・流す形 flowctl.alternatives_form）：2つ目からの選択肢ごとに、その選択肢を
    先にして、残りは質問の順。並びの k 番目（0から）は、選択肢の k+1 番目（0から）を先に並べた文（flowctl.Question.alternatives_led の同じ番目）。"""
    return [(c, *(x for x in conditions if x != c)) for c in conditions[1:]]


def fits(text: str, conditions: tuple, qtype: str, line: str | None) -> bool:
    """選択肢を並べた質問として使えるか（形の決まりの唯一の場所）：選ぶ質問（選択肢が2つ以上）・「？」で終わる1つの問いの文・読点が無い（10-15）・元の文と同じで
    ない・頭に「はい」・つなぎの言葉が無い・選択肢の言葉を質問の順のまま全部入れ、最後の選択肢の前に「それとも」・数と期限の言葉を落としていない
    （announces.keeps_facts＝言い換えた質問と同じ）。"""
    if qtype != "choice" or len(conditions) < 2:
        return False
    if not line or line == text or not line.endswith("？") or line.count("？") != 1 or any(m in line for m in ("?", "。", "、", "，")):
        return False
    if line.startswith(LEADS) or head_word(line) or not keeps_facts(text, line):
        return False
    at = 0
    for n, c in enumerate(conditions):
        i = line.find(c, at)
        if i < 0 or (n == len(conditions) - 1 and JOIN not in line[at:i]):
            return False
        at = i + len(c)
    return True


def by_model(client, model: str, items: list[tuple[str, tuple, str]], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。items（(質問の文, 選択肢の並び, 型)）と同じ並びの答え（無ければ None）を返す（問い合わせは
    askitems.ask の1か所。think＝考える形で聞く＝決まらなかった質問だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{text}／{_answers(conds)}" for n, (text, conds, _) in enumerate(items, 1))
    got_items = ask(client, model, PROMPT + body, {"no": ("integer", "質問の番号"), FIELD: ("string", DESC)}, tool=TOOL, think=think)
    got = {x.get("no"): x.get(FIELD) for x in got_items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(items) + 1)]


def decide(text: str, conditions: tuple, qtype: str, model_lines: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその文・ほかは None（考える Sonnet に聞く＝import_ai.decide_alternatives）。"""
    from askitems import agreed

    got = agreed(model_lines, None)
    return got if fits(text, conditions, qtype, got) else None


def grounded(client, model: str, text: str, conditions: tuple, qtype: str, line: str) -> bool:
    """選択肢を並べた質問が、元の質問（選択肢も）に無い事実を足していないか（中身の判定の1か所＝askitems.grounded）。通ったときだけ True。"""
    from askitems import grounded as check

    return check(client, model, [text, _answers(conditions)], line, ("grounding", "選択肢を並べた質問の中身"))
