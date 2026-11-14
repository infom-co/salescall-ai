"""ご承認を伺う質問の、すらっと伝えるお知らせの文（「（説明）…必要がございますのでご了承ください。」）と、資料がお手元に届いているかを伺う質問を、取込のときに
決める（人は確かめない）。

✅ 2026-11-12 日本時間19時32分21秒 ユーザー：「…こちらの内容でご承認いただけますでしょうか？⇒こんな言い方だと誰でも、警戒してしまうので、「ご予約の内容ですが
キャンセルは前日までにご連絡いただく必要がございますのでご了承ください。（お客様手元に資料などがあれば、「ご確認ください。」）」のようにすらっと言わないといけない
んじゃないですか？」・11-13 日本時間20時06分19秒「伺い1の(1)はウでいいけど…(2)ウ (3)b」（(1) ウ＝取込の文字のAI2つが説明と質問の文から作る・(3) b＝お客様が
資料が届いていると答えたときだけ「ご確認ください。」）。

使う所：flowctl.Question.announce（お知らせの文。流す形は flowctl.announce_form の1か所）・flowctl.Question.at_hand（「届いている」の選択肢の名前。読むのは
flowctl.at_hand の1か所）。「ご確認ください。」の文は、お知らせの文の終わりを替えてプログラムが作る（phrases.announce_check＝お言葉の決まった言い方）。

決め方（since と同じ形＝問い合わせと読み取りは askitems.ask、合わせ方は askitems.agreed）：
- お知らせの文：2つの文字のAIの文がそろい、形に合う（fits）ときだけ候補。そろわない・形に合わないときは、考える Sonnet（import_ai.decide_announces の referee）に
  その文だけを聞き、形に合えば候補。候補は、中身の判定の1か所（grounding.prompt・CHOICES＝会話AIの自分の言葉を声に出す前に読むのと同じ問い）で、説明と質問の文に無い
  事実を足していないかを考える Sonnet に読ませ、通った（grounding.PASSES）ときだけ使う。ほかは空＝今の形（入る一言＋質問の文）で伺う（違う中身を言うことはない側へ倒す）。
- 資料が届いているかを伺う質問：unclear と同じ形（2つがそろって「伺う質問」で同じ選択肢を挙げ、その選択肢が質問の選択肢にあるときだけ。ほかは空＝「ご了承ください。」）。
- お知らせの中身の名詞（flowctl.Question.matter。最終の確かめ「それでは（名詞）の件はご了承ということでよろしかったでしょうか？」＝flowctl.accepted_line の1か所）：
  用件の名詞（purposes）と同じ形＝2つの文字のAIがそろい、説明と質問の文の中の言葉のときだけ、その言葉。そろわない・文に無いときは考える Sonnet
  （import_ai.decide_matters の referee）に、その文だけを聞き、文の中の言葉ならその言葉（考える Sonnet に聞く所は import_ai._settled の1か所）。ほかは空＝
  「先ほどの内容」（違う中身を言うことはない側へ倒す）。✅ 2026-11-14 日本時間14時31分38秒 ユーザー「●伺い10 「はい、それでは、キャンセルの件はご了承という
  ことでよろしかったでしょうか？」」
"""
from __future__ import annotations

import re

from phrases import ANNOUNCE_ACCEPTED, ANNOUNCE_END

PROMPT = (
    "電話でお客様に、こちらの決まり（説明）をお伝えしてから、その内容でよいかを伺う質問の一覧です。伺う形は、お客様が身構えてしまうので、説明の内容を1つの文で"
    f"すらっとお伝えし、「{ANNOUNCE_END}」で終える文にします。質問ごとに、その文を line に答えてください。\n"
    "決まり：\n"
    "- 例：説明「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく決まりになっております。」・質問「こちらの内容でご承認いただけますでしょうか？」なら、"
    f"「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく必要がございます{ANNOUNCE_END}」。\n"
    "- 説明と質問の文にある内容だけを使います。数・金額・日にち・期限・回数は、書いてあるとおりに入れます。無い事実は足しません。\n"
    f"- 1つの文にし、問いかけ（「？」）にしません。終わりは必ず「{ANNOUNCE_END}」です。「ご承認」という言葉は使いません。\n"
    "質問（番号：説明／質問の文）：\n"
)
FIELD, DESC = "line", f"説明の内容を1つの文でお伝えし「{ANNOUNCE_END}」で終える文"
TOOL = ("announce_lines", "質問ごとの、お知らせの文")

HAND_PROMPT = (
    "電話でお客様に聞く質問の一覧です。質問ごとに、次の2つを答えてください。\n"
    "- asks：この質問が、お客様のお手元に資料・書類・案内（こちらからお送りしたもの）が届いているか・お手元にあるかを伺う質問か（true／false）。\n"
    "- yes：asks が true のときは、選択肢のうち「お手元に届いている・ある」という意味になる選択肢の名前を、選択肢に書かれたとおりに1つ書きます。"
    "asks が false のときは空の文字を書きます。\n"
    "決まり：\n"
    "- 質問の文の意味だけで決めます。\n"
    "質問（番号：質問の文／選択肢）：\n"
)
HAND_FIELDS = {"no": ("integer", "質問の番号"), "asks": ("boolean", "お手元に資料が届いているかを伺う質問か"),
               "yes": ("string", "「届いている」にあたる選択肢の名前（無ければ空）")}
HAND_TOOL = ("at_hand", "質問ごとの、資料が届いているかを伺う質問か")

# 落としてはいけない言葉（数・期限）：説明と質問の文にあれば、お知らせの文にも同じ字で入っていること（無い事実を足していないかは中身の判定で読む）
_NUMBER = re.compile(r"[0-9０-９][0-9０-９,，.．]*")
KEEP_WORDS = ("前々日", "前日", "当日", "翌日", "翌営業日", "営業日", "以内", "以降", "無料", "有料", "{最短の日}")


def _digits(text: str) -> list[str]:
    table = str.maketrans("０１２３４５６７８９，．", "0123456789,.")
    return [m.translate(table).replace(",", "") for m in _NUMBER.findall(text)]


def fits(intro: str, text: str, line: str | None) -> bool:
    """お知らせの文として使えるか（形の決まりの唯一の場所）：1つの文（「。」は終わりの1つだけ）・問いでない・終わりが「…のでご了承ください。」・「ご承認」を
    言わない・説明と質問の文の数と期限の言葉を落としていない（数は同じ字で・期限の言葉はそのまま）。"""
    if not line or not line.endswith(ANNOUNCE_END) or line.count("。") != 1 or any(m in line for m in ("？", "?", "ご承認")):
        return False
    return keeps_facts(f"{intro}{text}", line)


def keeps_facts(source: str, line: str) -> bool:
    """取込で作った文 line が、もとの文 source の数（同じ字で）と期限の言葉（そのまま）を落としていないか（唯一の決まり＝お知らせの文 fits・言い換えた質問
    rewords.fits）。"""
    have = _digits(line)
    if any(d not in have for d in _digits(source)):
        return False
    return all(w in line for w in KEEP_WORDS if w in source)


def by_model(client, model: str, pairs: list[tuple[str, str]], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。pairs（(説明, 質問の文)）と同じ並びの答え（無ければ None）を返す（問い合わせは
    askitems.ask の1か所。think＝考える形で聞く＝決まらなかった文だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{intro}／{text}" for n, (intro, text) in enumerate(pairs, 1))
    items = ask(client, model, PROMPT + body, {"no": ("integer", "質問の番号"), FIELD: ("string", DESC)}, tool=TOOL, think=think)
    got = {x.get("no"): x.get(FIELD) for x in items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(pairs) + 1)]


def decide(intro: str, text: str, model_lines: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその文・ほかは None（考える Sonnet に聞く＝import_ai.decide_announces）。"""
    from askitems import agreed

    got = agreed(model_lines, None)
    return got if fits(intro, text, got) else None


def grounded(client, model: str, intro: str, text: str, line: str) -> bool:
    """お知らせの文が、説明と質問の文に無い事実を足していないか（中身の判定の1か所＝askitems.grounded）。通ったときだけ True。"""
    from askitems import grounded as check

    return check(client, model, [intro, text], line, ("grounding", "お知らせの文の中身"))


def _hand_body(questions: list[tuple[str, tuple]]) -> str:
    return "\n".join(f"{n}：{text}／選択肢：{'・'.join(conds)}" for n, (text, conds) in enumerate(questions, 1))


def hand_by_model(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict | None]:
    """資料が届いているかを伺う質問か（unclear.by_model と同じ形）。questions と同じ並びの {"asks", "yes"}（無い・形の違うものは None）。"""
    from askitems import ask

    items = ask(client, model, HAND_PROMPT + _hand_body(questions), HAND_FIELDS, tool=HAND_TOOL)
    got = {x.get("no"): x for x in items or [] if isinstance(x, dict)}
    out = []
    for i in range(1, len(questions) + 1):
        x = got.get(i)
        ok = x is not None and isinstance(x.get("asks"), bool) and isinstance(x.get("yes"), str)
        out.append({"asks": x["asks"], "yes": x["yes"]} if ok else None)
    return out


def decide_hand(conditions: tuple, model_rows: list[dict | None]) -> str:
    """「届いている」にあたる選択肢を1つに決める（unclear.decide と同じ形）：2つ以上のモデルがそろって「伺う質問」で同じ選択肢を挙げ、その選択肢が質問の選択肢に
    あるときだけ、その名前。ほかは空（資料が届いているかは読まない＝「ご了承ください。」）。"""
    if len(model_rows) < 2 or any(r is None or not r["asks"] for r in model_rows):
        return ""
    yes = {r["yes"] for r in model_rows}
    return next(iter(yes)) if len(yes) == 1 and next(iter(yes)) in conditions else ""


# お知らせの中身の名詞を聞く問い（例の最終の確かめの文は、言い方の表の1か所 phrases.ANNOUNCE_ACCEPTED から作る）
MATTER_PROMPT = (
    "電話でお客様に、こちらの決まり（説明）をお伝えしてから、その内容でよいかを伺う質問の一覧です。お伝えしたあと、最後に"
    f"「{ANNOUNCE_ACCEPTED.format(t='（中身）の')}」と確かめます。質問ごとに、（中身）に入る、お伝えする決まりの中身を表す短い言葉を、説明と質問の文の中の"
    "文字のまま matter に答えてください。\n"
    "決まり：\n"
    "- 例：説明「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく決まりになっております。」なら「キャンセル」。\n"
    "- 「の件」は入れません。説明と質問の文の中に無い言葉・言い換えた言葉は書きません。中身を短い言葉で表せなければ空にします。\n"
    "質問（番号：説明／質問の文）：\n"
)
MATTER_FIELD, MATTER_DESC = "matter", "お伝えする決まりの中身を表す短い言葉（説明と質問の文の中の文字のまま）"
MATTER_TOOL = ("announce_matters", "質問ごとの、お知らせの中身を表す言葉")


def matter_by_model(client, model: str, pairs: list[tuple[str, str]], think: bool = False) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（by_model と同じ形）。pairs（(説明, 質問の文)）と同じ並びの答え（無ければ None）を返す（think＝考える形で聞く＝
    決まらなかった文だけを聞く referee）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{intro}／{text}" for n, (intro, text) in enumerate(pairs, 1))
    items = ask(client, model, MATTER_PROMPT + body, {"no": ("integer", "質問の番号"), MATTER_FIELD: ("string", MATTER_DESC)}, tool=MATTER_TOOL, think=think)
    got = {x.get("no"): x.get(MATTER_FIELD) for x in items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(pairs) + 1)]


def matter_fits(intro: str, text: str, matter: str | None) -> bool:
    """お知らせの中身の名詞として使えるか（唯一の決まり＝用件の名詞 purposes.fits と同じ考え）：空でない・説明か質問の文の中の言葉・文の終わりや問いを含まない。"""
    return bool(matter) and not any(m in matter for m in ("。", "？", "?")) and (matter in intro or matter in text)


def matter_decide(intro: str, text: str, model_matters: list[str | None]) -> str | None:
    """2つの文字のAIの答えから決める（取込のとき）：そろって形に合えばその言葉・そろって空なら空（中身を短い言葉で表せない）・ほかは None（考える Sonnet に聞く＝
    import_ai.decide_matters）。"""
    from askitems import agreed

    got = agreed(model_matters, None)
    if got is None:
        return None
    return got if got == "" or matter_fits(intro, text, got) else None
