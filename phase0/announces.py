"""ご承認を伺う質問の、すらっと伝えるお知らせの文（「（説明）…必要がございますのでご了承ください。」）と、資料がお手元に届いているかを伺う質問を、取込のときに
決める（人は確かめない）。

✅ 2026-11-12 日本時間19時32分21秒 ユーザー：「…こちらの内容でご承認いただけますでしょうか？⇒こんな言い方だと誰でも、警戒してしまうので、「ご予約の内容ですが
キャンセルは前日までにご連絡いただく必要がございますのでご了承ください。（お客様手元に資料などがあれば、「ご確認ください。」）」のようにすらっと言わないといけない
んじゃないですか？」・11-13 日本時間20時06分19秒「伺い1の(1)はウでいいけど…(2)ウ (3)b」（(1) ウ＝取込の文字のAI2つが説明と質問の文から作る・(3) b＝お客様が
資料が届いていると答えたときだけ「ご確認ください。」）。

✅ 2026-11-16 日本時間0時42分38秒 ユーザー：「・ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく決まりになっております。：決まりになっております。だと
相手の印象が悪いので、「ご予約の内容ですがキャンセルや変更は前日までにご連絡いただければ料金はかかりません。それ以降になってしまいますとキャンセル料（変更料）が
かかってしまうのでお気を付けください。」の方がいいんじゃないですか？」＝お知らせの文は、決まり・規則として言わず、説明にあるお客様にとっての良い点（「〜いただければ…」）
と気をつけていただくこと（「…のでお気を付けください。」）の形で書く（説明に無い事実は足さない決まりは今のまま。終わりと文の数は phrases.ANNOUNCE_ENDS の1か所）。

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
- お知らせの中身を常体にした句（flowctl.Question.matter_clause。聞き取れないまま「-」にしたお知らせの締めの前の確かめ「（句）件のお返事のお声が聞き取れなかった
  のですがご了承ということでよろしかったでしょうか？」＝flowctl.accepted_line の1か所）：名詞と同じ1回の問い合わせに項目 clause を足した（MATTER_PROMPT・
  matter_by_model。問い合わせの数は増えない）。決め方も名詞と同じ1か所（import_ai._settled の項目の組）＝2つの文字のAIがそろって形に合う（clause_fits）句・
  そろわない文は考える Sonnet に聞き、形に合えばその句。ほかは空＝名詞の形（「キャンセルの件のお返事の…」）。✅ 2026-11-15 日本時間11時59分07秒 ユーザー
  「・(3)今：…⇒こちら「ありがとうございます。申し訳ございません、キャンセルは前日までにご連絡いただく必要がある件のお返事のお声が聞き取れなかったのですがご了承
  ということでよろしかったでしょうか？」」。句は、お客様が聞いた文（heard の1か所＝取込で決まったお知らせの文、無ければ入る一言＋質問の文）から、決まり・
  規則として言わない形で作る（11-16 0時42分38秒のお言葉を句の問い合わせにも当てた＝NOT_A_RULE。句の言葉の出どころ clause_fits にお客様が聞いた文を入れる。
  説明と質問の文にもお客様が聞いた文にも無い言葉は足さない）。取込は、お知らせの文を決めてから（import_ai.decide_announces）、その答えを渡して句を決める
  （import_ai.decide_matters の受け口＝(説明, 質問の文, お知らせの文)）。⛔ bcdf81a4 まで、句の問いの例が「…ご連絡いただく決まりになっている」で、説明が
  「決まりになっております」だけの業務では決まりとして言わない句（「…必要がある」）を作れなかった（「必要」が説明に無い）
"""
from __future__ import annotations

import re

from phrases import ANNOUNCE_ACCEPTED, ANNOUNCE_CAUTION_END, ANNOUNCE_END, ANNOUNCE_ENDS, unheard_form

# 決まり・規則として言わない（お客様にとっての形で）言い方の決まり（唯一の言葉＝お知らせの文の問い PROMPT・お知らせの中身を常体にした句の問い MATTER_PROMPT。
# ✅ 2026-11-16 日本時間0時42分38秒 ユーザー「…決まりになっております。だと相手の印象が悪いので…」）
NOT_A_RULE = "決まり・規則として言わず（「決まりになっております」のような言い方をしません）、お客様にとっての形で"
# 2つの問いの例（説明・質問の文と、その説明から作るお知らせの文＝お言葉の文。句の問い MATTER_PROMPT は、お客様が聞いたこの文から句を作る例に引く）
EXAMPLE_EXPL = "キャンセルや変更は前日までのご連絡でしたら料金はかからない決まりになっております。それ以降のご連絡ですとキャンセル料がかかる決まりになっております。"
EXAMPLE_ASK = "こちらの内容でご承認いただけますでしょうか？"
EXAMPLE_LINE = f"キャンセルや変更は前日までにご連絡いただければ料金はかかりません。それ以降になってしまいますとキャンセル料がかかってしまう{ANNOUNCE_CAUTION_END}"

PROMPT = (
    "電話でお客様に、こちらの決まり（説明）をお伝えしてから、その内容でよいかを伺う質問の一覧です。伺う形は、お客様が身構えてしまうので、説明の内容をすらっと"
    "お伝えする文にします。質問ごとに、その文を line に答えてください。\n"
    "決まり：\n"
    f"- {NOT_A_RULE}伝えます。\n"
    "- 説明に、お客様がそうしていただいたときの良い点と、そうでないときに気をつけていただくことの両方があれば、良い点を「〜いただければ…」の形の1つの文で、"
    f"気をつけていただくことを「{ANNOUNCE_CAUTION_END}」で終える1つの文で書きます（2つの文）。\n"
    f"- 説明に良い点と気をつけていただくことの両方が無ければ、説明の内容を1つの文でお伝えし「{ANNOUNCE_END}」で終えます。\n"
    f"- 例：説明「{EXAMPLE_EXPL}」・質問「{EXAMPLE_ASK}」なら「{EXAMPLE_LINE}」。\n"
    f"- 例：説明「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく決まりになっております。」・質問「{EXAMPLE_ASK}」なら、"
    f"「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく必要がございます{ANNOUNCE_END}」（説明に料金の話が無いので料金は言いません）。\n"
    "- 説明と質問の文にある内容だけを使います。数・金額・日にち・期限・回数は、書いてあるとおりに入れます。無い事実は足しません。\n"
    f"- 問いかけ（「？」）にしません。終わりは必ず「{ANNOUNCE_CAUTION_END}」か「{ANNOUNCE_END}」です。「ご承認」という言葉は使いません。\n"
    "質問（番号：説明／質問の文）：\n"
)
FIELD, DESC = "line", f"説明の内容をすらっとお伝えし「{ANNOUNCE_CAUTION_END}」か「{ANNOUNCE_END}」で終える文"
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
    """お知らせの文として使えるか（形の決まりの唯一の場所）：終わりがお言葉の終わり（phrases.ANNOUNCE_ENDS＝「…のでご了承ください。」は1つの文・「…のでお気を付け
    ください。」は良い点の文と気をつけていただく文の2つまで。「。」は文の終わりだけ）・問いでない・「ご承認」を言わない・説明と質問の文の数と期限の言葉を落として
    いない（数は同じ字で・期限の言葉はそのまま）。決まり・規則として言わない形は問い合わせ（PROMPT）が頼む（言葉の形では止めない）。"""
    end = next((e for e in ANNOUNCE_ENDS if line and line.endswith(e)), None)
    if end is None or line.count("。") > ANNOUNCE_ENDS[end] or "。。" in line or any(m in line for m in ("？", "?", "ご承認")):
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


# お知らせの中身の名詞と、中身を常体にした句を聞く問い（1回の問い合わせで2つの項目。例の確かめの文は、言い方の表の1か所 phrases.ANNOUNCE_ACCEPTED・
# phrases.unheard_form から作る）。句は、お客様が聞いた文（heard）から作る＝並びは「番号：説明／質問の文／お客様が聞いた文」。決まり・規則として言わない形は
# お知らせの文の問い PROMPT と同じ1か所の言葉（NOT_A_RULE）・例も同じ文（EXAMPLE_EXPL・EXAMPLE_ASK・EXAMPLE_LINE）。言葉の形では止めない（fits と同じ方針＝問い合わせが頼む）
EXAMPLE_CLAUSE = "キャンセルや変更は前日までにご連絡いただければ料金がかからずそれ以降はキャンセル料がかかる"   # 期限の言葉（前日・以降）を落とさない（clause_fits）
MATTER_PROMPT = (
    "電話でお客様に、こちらの決まり（説明）の内容をお伝えしてから、その内容でよいかを伺う質問の一覧です。お客様には、質問ごとの「お客様が聞いた文」をお伝え"
    f"しています。お伝えしたあと、最後に「{ANNOUNCE_ACCEPTED.format(t='（中身）の')}」と確かめます。お返事が聞き取れなかったときは"
    f"「{unheard_form('（句）')}」と確かめます。質問ごとに、次の2つを答えてください。\n"
    "- matter：（中身）に入る、お伝えする決まりの中身を表す短い言葉を、説明と質問の文の中の文字のまま。\n"
    "- clause：（句）に入る、お客様が聞いた文の内容を1つの句にしたもの。お客様が聞いた文の言葉のまま、終わりだけを「です」「ます」「ございます」を使わない"
    "言い切りの形（「必要がございます」なら「必要がある」・「かかりません」なら「かからない」）にします。\n"
    "決まり：\n"
    f"- clause は、{NOT_A_RULE}書きます（言い切りの形でも「決まりになっている」とは書きません）。お客様が聞いた文が決まり・規則としての言い方のままで、"
    "そう言わない1つの句にできなければ、clause は空にします。\n"
    f"- 例：説明「{EXAMPLE_EXPL}」・質問「{EXAMPLE_ASK}」で、お客様が聞いた文が「{EXAMPLE_LINE}」なら、matter は「キャンセル」、"
    f"clause は「{EXAMPLE_CLAUSE}」。\n"
    "- matter に「の件」は入れません。説明と質問の文の中に無い言葉・言い換えた言葉は書きません。中身を短い言葉で表せなければ空にします。\n"
    "- clause には、頭の「（何々）の内容ですが」のような前置きと「件」を入れません。数・金額・日にち・期限・回数は、お客様が聞いた文にあるものを全部、書いてある"
    "とおりに入れます。説明と質問の文にも、お客様が聞いた文にも無い言葉・事実は足しません。「、」「。」「？」は入れません。1つの句にできなければ空にします。\n"
    "質問（番号：説明／質問の文／お客様が聞いた文）：\n"
)
MATTER_FIELD, MATTER_DESC = "matter", "お伝えする決まりの中身を表す短い言葉（説明と質問の文の中の文字のまま）"
CLAUSE_FIELD, CLAUSE_DESC = "clause", "お客様が聞いた文の内容を、その文の言葉のまま終わりだけ言い切りの形にした句（「、」「。」「件」を入れない・決まりとして言わない）"
MATTER_TOOL = ("announce_matters", "質問ごとの、お知らせの中身を表す言葉と、中身を常体にした句")

# お知らせの中身を常体にした句の終わり（「件」の前に置ける形＝言い切りの形）：う段のかな（「ある」「いただく」）・「ない」・「た」で終わる。丁寧な言い方の
# 終わり（「ございます」「です」「ます」…）は常体でない
CLAUSE_ENDS = (*"うくぐすつぬぶむる", "ない", "た")
POLITE_ENDS = ("ます", "です", "ました", "でした", "ません", "でしょう", "ましょう", "ください")
# 句の中の言葉（漢字・カタカナ・英字のまとまり）：どれも説明か質問の文にあること（説明に無い言葉・事実を足さない）
_WORDS = re.compile(r"[一-鿿々〆]+|[ァ-ヺー]+|[A-Za-zＡ-Ｚａ-ｚ]+")


def heard(intro: str, text: str, announce: str = "") -> str:
    """お客様が聞いた文（唯一の決まり＝句の問い合わせ matter_by_model の並び・句の言葉の出どころ clause_fits）：取込で決まったお知らせの文（announce＝同じ質問の
    import_ai.decide_announces の答え＝flowctl.Question.announce。流すのは flowctl.announce_form）があればその文、無ければ今の形（入る一言＋質問の文）。"""
    return announce or f"{intro}{text}"


def matter_by_model(client, model: str, items: list[tuple[str, str, str]], think: bool = False) -> list[tuple[str | None, str | None]]:
    """1回の問い合わせでまとめて答えさせる（by_model と同じ形）。items（(説明, 質問の文, お知らせの文＝決まらなければ空)）と同じ並びの (名詞, 句)（無い項目は
    None）を返す（think＝考える形で聞く＝決まらなかった文だけを聞く referee）。名詞と句は同じ1回の問い合わせ（項目を足しても問い合わせの数は同じ）。並びには
    お客様が聞いた文（heard の1か所）を入れる＝句はその文から作る。"""
    from askitems import ask

    body = "\n".join(f"{n}：{intro}／{text}／{heard(intro, text, announce)}" for n, (intro, text, announce) in enumerate(items, 1))
    fields = {"no": ("integer", "質問の番号"), MATTER_FIELD: ("string", MATTER_DESC), CLAUSE_FIELD: ("string", CLAUSE_DESC)}
    answers = ask(client, model, MATTER_PROMPT + body, fields, tool=MATTER_TOOL, think=think)
    got = {x.get("no"): x for x in answers or [] if isinstance(x, dict)}
    word = lambda x, k: x[k].strip() if isinstance(x, dict) and isinstance(x.get(k), str) else None   # noqa: E731
    return [(word(got.get(n), MATTER_FIELD), word(got.get(n), CLAUSE_FIELD)) for n in range(1, len(items) + 1)]


def matter_fits(intro: str, text: str, matter: str | None) -> bool:
    """お知らせの中身の名詞として使えるか（唯一の決まり＝用件の名詞 purposes.fits と同じ考え）：空でない・説明か質問の文の中の言葉・文の終わりや問いを含まない。"""
    from askitems import noun_fits

    return noun_fits(matter, (intro, text))


def clause_fits(intro: str, text: str, clause: str | None, announce: str = "") -> bool:
    """お知らせの中身を常体にした句として使えるか（形の決まりの唯一の場所＝取込の clause_decide・import_ai.decide_matters の考える Sonnet の答え）：空でない・読点も
    句点も問いも含まない（askitems.noun_fits の1か所）・常体（終わりが POLITE_ENDS でない）・「件」の前に置ける終わり（CLAUSE_ENDS）・言葉の出どころの数と期限の
    言葉を落とさない・出どころに無い数と期限の言葉を足さない（どちらも keeps_facts の1か所を両向きに）・出どころに無い言葉（漢字・カタカナ・英字のまとまり）を
    足さない。言葉の出どころ＝説明と質問の文と、お客様が聞いた文（heard の1か所＝取込で決まったお知らせの文 announce・無ければ入る一言＋質問の文。お知らせの文は
    説明に無い事実を足さない中身の判定を通った文）。決まり・規則として言わない形は問い合わせ（MATTER_PROMPT）が頼む（言葉の形では止めない＝fits と同じ）。"""
    from askitems import noun_fits

    if not noun_fits(clause) or clause.endswith(POLITE_ENDS) or not clause.endswith(CLAUSE_ENDS):
        return False
    source = "\n".join((intro, text, heard(intro, text, announce)))   # お知らせの文が無いとき heard は入る一言＋質問の文（重なりは確かめに効かない）
    return keeps_facts(source, clause) and keeps_facts(clause, source) and all(w in source for w in _WORDS.findall(clause))


def _decided(model_words: list[str | None], fits) -> str | None:
    """2つの文字のAIの答えから1つの項目を決める（名詞 matter_decide・句 clause_decide の1か所）：そろって形に合えばその言葉・そろって空なら空・ほかは None
    （考える Sonnet に聞く＝import_ai.decide_matters）。"""
    from askitems import agreed

    got = agreed(model_words, None)
    if got is None:
        return None
    return got if got == "" or fits(got) else None


def matter_decide(intro: str, text: str, model_matters: list[str | None]) -> str | None:
    """2つの文字のAIの名詞の答えから決める（取込のとき）：そろって形に合えばその言葉・そろって空なら空（中身を短い言葉で表せない）・ほかは None（考える Sonnet に
    聞く＝import_ai.decide_matters）。"""
    return _decided(model_matters, lambda w: matter_fits(intro, text, w))


def clause_decide(intro: str, text: str, model_clauses: list[str | None], announce: str = "") -> str | None:
    """2つの文字のAIの句の答えから決める（名詞と同じ形）：そろって形に合えば（clause_fits＝announce はお知らせの文）その句・そろって空なら空（1つの句に
    できない）・ほかは None（考える Sonnet に聞く）。"""
    return _decided(model_clauses, lambda w: clause_fits(intro, text, w, announce))
