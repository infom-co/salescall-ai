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

✅ 2026-11-16 日本時間12時48分22秒 ユーザー：「・(a) お知らせ：「ありがとうございます。ご予約の内容ですがキャンセルや変更は前日までにご連絡いただければ料金はかかりません。
それ以降になってしまいますとキャンセル料（変更料）がかかってしまうのでお気を付けください。」片方しか無いときは、今の「…必要がございますのでご了承ください。」の1つの
文です。⇒良い方のみの場合は、良い方のみの方がいいでしょ。」「・伺い33：業務によって、キャンセル料がかかるかかからない、変更料もかかるかかからないが変わってくるので、
片方ならどちらかで、両方なら、「キャンセル料や変更料が」になります。」＝説明に良い点だけがあれば良い点の1つの文だけ（「〜いただければ…」。「ご了承ください。」「お気を
付けください。」を付けない）・気をつけていただくことだけ・どちらも無ければ今の「…のでご了承ください。」の1つの文・両方なら今の2つの文（表は phrases.ANNOUNCE_ENDS の
1か所）。声で言う文にかっこを使わず、説明にある料金だけを言う（2つあれば「キャンセル料や変更料」）＝問い合わせ PROMPT が頼み、形の確かめ fits・句の確かめ clause_fits は
かっこの入った文を通さない（決まりは spoken の1か所）。

使う所：flowctl.Question.announce（お知らせの文。流す形は flowctl.announce_form の1か所）・flowctl.Question.at_hand（「届いている」の選択肢の名前。読むのは
flowctl.at_hand の1か所）。「ご確認ください。」の文は、お知らせの文の終わりを替えてプログラムが作る（phrases.announce_check＝お言葉の決まった言い方）。

決め方（since と同じ形＝問い合わせと読み取りは askitems.ask、合わせ方は askitems.agreed）：
- お知らせの文：2つの文字のAIの文がそろい、形に合う（fits）ときだけ候補。そろわない・形に合わないときは、考える Sonnet（import_ai.decide_announces の referee）に
  その文だけを聞き、形に合えば候補。候補は、中身の判定の1か所（grounding.prompt・CHOICES＝会話AIの自分の言葉を声に出す前に読むのと同じ問い）で、説明と質問の文に無い
  事実を足していないかを考える Sonnet に読ませ、通った（grounding.PASSES）ときだけ使う。ほかは空＝今の形（入る一言＋質問の文）で伺う（違う中身を言うことはない側へ倒す）。
- 資料が届いているかを伺う質問：unclear と同じ形（2つがそろって「伺う質問」で同じ選択肢を挙げ、その選択肢が質問の選択肢にあるときだけ。ほかは空＝「ご了承ください。」）。
- お知らせの中身の名詞（flowctl.Question.matter。最終の確かめ「それでは（名詞）の件はご了承ということでよろしかったでしょうか？」＝flowctl.accepted_line の1か所）：
  用件の名詞（purposes）と同じ形＝2つの文字のAIがそろい、説明と質問の文の中の言葉のときだけ、その言葉。そろわない・文に無いときは考える Sonnet
  （import_ai.decide_matters の referee）に、その文だけを聞き、文の中の言葉ならその言葉（考える Sonnet に聞く所は import_ai._settled の1か所）。名詞も句も、
  説明で並べた事柄（「キャンセルや変更」）の一部だけを言わない（keeps_items の1か所＝matter_fits・clause_fits。問い合わせの例の名詞も「キャンセルや変更」）。ほかは空＝
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
  「決まりになっております」だけの業務では決まりとして言わない句（「…必要がある」）を作れなかった（「必要」が説明に無い）。
  良い点と気をつけていただくことの2つの文のお知らせの句は、気をつけていただく文の中身だけから作り、「それ以降」は良い点の文の期限の言葉で「（期限）を過ぎた」と
  言う（BACK・PAST・確かめは clause_fits の1か所＝「前日以降」は通さない）。✅ 2026-11-21 日本時間14時59分44秒 ユーザー「伺い96〜101：すべてアでOK、進めて
  ください」＝伺い96 ア「…前日を過ぎたキャンセルや変更はキャンセル料や変更料がかかる件のお返事の…」。⛔ それまで、2つの文の中身を全部持つ句（一言が112字）
"""
from __future__ import annotations

import re

from phrases import (ANNOUNCE_ACCEPTED, ANNOUNCE_BENEFIT, ANNOUNCE_BENEFIT_END, ANNOUNCE_CAUTION_END, ANNOUNCE_END, ANNOUNCE_ENDS,
                     unheard_form)

# 決まり・規則として言わない（お客様にとっての形で）言い方の決まり（唯一の言葉＝お知らせの文の問い PROMPT・お知らせの中身を常体にした句の問い MATTER_PROMPT。
# ✅ 2026-11-16 日本時間0時42分38秒 ユーザー「…決まりになっております。だと相手の印象が悪いので…」）
NOT_A_RULE = "決まり・規則として言わず（「決まりになっております」のような言い方をしません）、お客様にとっての形で"
# 2つの問いの例（説明・質問の文と、その説明から作るお知らせの文＝お言葉の文。句の問い MATTER_PROMPT は、お客様が聞いたこの文から句を作る例に引く）
EXAMPLE_EXPL = "キャンセルや変更は前日までのご連絡でしたら料金はかからない決まりになっております。それ以降のご連絡ですとキャンセル料がかかる決まりになっております。"
EXAMPLE_ASK = "こちらの内容でご承認いただけますでしょうか？"
EXAMPLE_LINE = f"キャンセルや変更は前日までにご連絡いただければ料金はかかりません。それ以降になってしまいますとキャンセル料がかかってしまう{ANNOUNCE_CAUTION_END}"
# 良い点だけの説明の例（お知らせの文は良い点の1つの文だけ＝✅ 2026-11-16 日本時間12時48分22秒「⇒良い方のみの場合は、良い方のみの方がいいでしょ。」）
EXAMPLE_BENEFIT_EXPL = "キャンセルは前日までのご連絡でしたら料金はかからない決まりになっております。"
EXAMPLE_BENEFIT_LINE = "キャンセルは前日までにご連絡いただければ料金はかかりません。"
# 声で言う文に使わないかっこ（唯一の表＝spoken）と、料金の言い方の決まり（唯一の言葉＝お知らせの文の問い PROMPT・句の問い MATTER_PROMPT）。✅ 2026-11-16 日本時間
# 12時48分22秒 ユーザー「・伺い33：業務によって、キャンセル料がかかるかかからない、変更料もかかるかかからないが変わってくるので、片方ならどちらかで、両方なら、
# 「キャンセル料や変更料が」になります。」
BRACKETS = "（）()"
NO_BRACKETS = ("声で言う文なので、かっこ（「（」「）」）は使いません。料金は説明にあるものだけを言い、2つあれば「キャンセル料や変更料」のように「や」でつなぎます"
               "（説明に無い料金は足しません）")


def spoken(text: str) -> bool:
    """声で言う文として使えるか（唯一の決まり＝お知らせの文の形の確かめ fits・お知らせの中身を常体にした句の確かめ clause_fits）：かっこ（BRACKETS）を含まない。"""
    return not any(c in (text or "") for c in BRACKETS)

PROMPT = (
    "電話でお客様に、こちらの決まり（説明）をお伝えしてから、その内容でよいかを伺う質問の一覧です。伺う形は、お客様が身構えてしまうので、説明の内容をすらっと"
    "お伝えする文にします。質問ごとに、その文を line に答えてください。\n"
    "決まり：\n"
    f"- {NOT_A_RULE}伝えます。\n"
    "- 説明に、お客様がそうしていただいたときの良い点と、そうでないときに気をつけていただくことの両方があれば、良い点を「〜いただければ…」の形の1つの文で、"
    f"気をつけていただくことを「{ANNOUNCE_CAUTION_END}」で終える1つの文で書きます（2つの文）。\n"
    f"- 説明に良い点だけがあれば、良い点を「〜{ANNOUNCE_BENEFIT}…」の形の1つの文だけで書きます（「{ANNOUNCE_END}」「{ANNOUNCE_CAUTION_END}」は付けません）。\n"
    f"- 説明に気をつけていただくことだけがあるとき・どちらも無いときは、説明の内容を1つの文でお伝えし「{ANNOUNCE_END}」で終えます。\n"
    f"- {NO_BRACKETS}。\n"
    f"- 例：説明「{EXAMPLE_EXPL}」・質問「{EXAMPLE_ASK}」なら「{EXAMPLE_LINE}」。\n"
    f"- 例：説明「{EXAMPLE_BENEFIT_EXPL}」・質問「{EXAMPLE_ASK}」なら「{EXAMPLE_BENEFIT_LINE}」（気をつけていただくことが説明に無いので、良い点の文だけ）。\n"
    f"- 例：説明「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく決まりになっております。」・質問「{EXAMPLE_ASK}」なら、"
    f"「ご予約の内容ですが当日のキャンセルは前日までにご連絡いただく必要がございます{ANNOUNCE_END}」（説明に料金の話が無いので料金は言いません）。\n"
    "- 説明と質問の文にある内容だけを使います。数・金額・日にち・期限・回数は、書いてあるとおりに入れます。無い事実は足しません。\n"
    f"- 問いかけ（「？」）にしません。良い点だけの文のほかは、終わりは必ず「{ANNOUNCE_CAUTION_END}」か「{ANNOUNCE_END}」です。「ご承認」という言葉は使いません。\n"
    "質問（番号：説明／質問の文）：\n"
)
FIELD, DESC = "line", f"説明の内容をすらっとお伝えする文（「{ANNOUNCE_CAUTION_END}」か「{ANNOUNCE_END}」で終える。説明に良い点だけなら良い点の1つの文だけ）"
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
    """お知らせの文として使えるか（形の決まりの唯一の場所）：終わりと文の数がお言葉の形（phrases.ANNOUNCE_ENDS の1か所＝「…のでご了承ください。」は1つの文・「…のでお気を
    付けください。」は良い点の文と気をつけていただく文の2つ・良い点だけは良い点の1つの文。「。」は文の終わりだけ）・良い点の文（「お気を付けください。」の形の1つ目の文・
    良い点だけの文）は「〜いただければ…」（ANNOUNCE_BENEFIT）・良い点だけの文に「〜ください」を付けない（ほかの「〜ください」に広げない＝10-15）・問いでない・「ご承認」を
    言わない・かっこを使わない（spoken の1か所）・説明と質問の文の数と期限の言葉を落としていない（数は同じ字で・期限の言葉はそのまま）。決まり・規則として言わない形と、
    説明のどの形かは問い合わせ（PROMPT）が頼む（言葉の形では止めない）。"""
    end = next((e for e in ANNOUNCE_ENDS if line and line.endswith(e)), None)
    if (end is None or line.count("。") != ANNOUNCE_ENDS[end] or "。。" in line or any(m in line for m in ("？", "?", "ご承認"))
            or not spoken(line)):
        return False
    if end != ANNOUNCE_END and ANNOUNCE_BENEFIT not in line.split("。")[0]:
        return False   # 良い点の文は「〜いただければ…」
    if end == ANNOUNCE_BENEFIT_END and "ください" in line:
        return False   # 良い点だけの文に「〜ください」を付けない
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


def hand_by_model(client, model: str, questions: list[tuple[str, tuple]], think: bool = False) -> list[dict | None]:
    """資料が届いているかを伺う質問か（unclear.by_model と同じ形）。questions と同じ並びの {"asks", "yes"}（無い・形の違うものは None）。think＝考える形で聞く。"""
    from askitems import ask

    items = ask(client, model, HAND_PROMPT + _hand_body(questions), HAND_FIELDS, tool=HAND_TOOL, think=think)
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
# 良い点と気をつけていただくことの2つの文のお知らせの句は、気をつけていただく文の中身だけから作り、その文が良い点の文の期限を指す言葉（BACK）は、その期限の
# 言葉で「（期限）を過ぎた」（PAST）と言う（唯一の言葉＝句の問い MATTER_PROMPT・句の確かめ clause_fits）。✅ 2026-11-21 日本時間14時59分44秒 ユーザー「伺い96〜101：
# すべてアでOK、進めてください」＝伺い96 ア「ありがとうございます。申し訳ございません、前日を過ぎたキャンセルや変更はキャンセル料や変更料がかかる件のお返事のお声が
# 聞き取れなかったのですがご了承ということでよろしかったでしょうか？」（期限の言葉と料金は残す・「前日以降」とは言わない＝前日も入ってしまい、説明と違う）
BACK, PAST = "それ以降", "を過ぎた"
EXAMPLE_CLAUSE = "前日を過ぎたキャンセルや変更はキャンセル料がかかる"   # 期限（前日）と料金を落とさない・「それ以降」は「前日を過ぎた」（clause_fits）
# 例の名詞は、説明で並べた事柄を全部（「キャンセル」だけにすると、最終の確かめ「それではキャンセルの件は…」が「変更」を落とす＝matter_fits・keeps_items の1か所。L12）
EXAMPLE_MATTER = "キャンセルや変更"
KEEP_ITEMS = "「キャンセルや変更」のように並べた事柄は、一部だけにせず全部を書きます（「キャンセル」だけにしません）"
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
    f"- お客様が聞いた文が、良い点の文と「{ANNOUNCE_CAUTION_END}」で終える気をつけていただくことの文の2つのときは、clause は気をつけていただくことの文の"
    f"中身だけを1つの句にします。その文の「{BACK}」は、良い点の文の期限の言葉を使って「（期限）{PAST}」と言います（「前日まで」なら「前日{PAST}」。"
    "「前日以降」とは書きません＝前日も入ってしまい、説明と違います）。お客様が聞いた文がほかの形のときは、その文の内容を全部1つの句にします。\n"
    f"- 例：説明「{EXAMPLE_EXPL}」・質問「{EXAMPLE_ASK}」で、お客様が聞いた文が「{EXAMPLE_LINE}」なら、matter は「{EXAMPLE_MATTER}」、"
    f"clause は「{EXAMPLE_CLAUSE}」。\n"
    "- matter に「の件」は入れません。説明と質問の文の中に無い言葉・言い換えた言葉は書きません。中身を短い言葉で表せなければ空にします。\n"
    f"- matter も clause も、{KEEP_ITEMS}。\n"
    "- clause には、頭の「（何々）の内容ですが」のような前置きと「件」を入れません。数・金額・日にち・期限・回数は、お客様が聞いた文にあるものを全部、書いてある"
    f"とおりに入れます（2つの文のときの「{BACK}」は、上のとおり期限の言葉で言います）。説明と質問の文にも、お客様が聞いた文にも無い言葉・事実は足しません"
    f"（足すのは、2つの文のときの「{PAST}」だけです）。「、」「。」「？」は入れません。1つの句にできなければ空にします。\n"
    f"- clause も、{NO_BRACKETS}。\n"
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
# 句の「（期限）を過ぎた」の期限の言葉（PAST のすぐ前の、数・漢字・カタカナのまとまり＝「前日」「18時」「3日前」。clause_fits が良い点の文の「（期限）まで」と比べる）
_DEADLINE = re.compile(r"[0-9０-９一-鿿々〆ァ-ヺー]+$")


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
    """お知らせの中身の名詞として使えるか（唯一の決まり＝用件の名詞 purposes.fits と同じ考え）：空でない・説明か質問の文の中の言葉・文の終わりや問いを含まない・
    説明と質問の文で並べた事柄（「キャンセルや変更」）の一部だけを言わない（keeps_items の1か所＝句 clause_fits と同じ決まり。「キャンセルや変更は…」の説明に
    「キャンセル」だけを当てると、最終の確かめ「それではキャンセルの件は…」が「変更」を落とす）。数と期限の言葉は名詞に入れなくてよい（名詞は中身を表す短い言葉＝
    ✅ 2026-11-14 日本時間14時31分38秒 ユーザー「●伺い10 「はい、それでは、キャンセルの件はご了承ということでよろしかったでしょうか？」」）。"""
    from askitems import noun_fits

    return noun_fits(matter, (intro, text)) and keeps_items(f"{intro}\n{text}", matter)


# 並べた事柄の言葉（言葉のまとまりを「や」「と」「・」「および」「または」でつないだ並び＝「キャンセルや変更」「キャンセル料や変更料」）。まとまりは数・漢字・カタカナ・
# 英字の続き（ひらがなと「・」で切れる＝「キャンセル料」は1つ）
_ITEM = r"[0-9０-９一-鿿々〆ァ-ヺーA-Za-zＡ-Ｚａ-ｚ]+"
_ITEMS = re.compile(rf"{_ITEM}(?:(?:や|と|・|および|または){_ITEM})+")


def keeps_items(source: str, words: str) -> bool:
    """取込で作った名詞・句 words が、もとの文 source で並べた事柄の一部だけを言っていないか（唯一の決まり＝お知らせの中身の名詞 matter_fits・句 clause_fits）：
    並びの言葉（まとまり）を1つでも言うなら、並びの言葉を全部言う（つなぐ言葉は問わない）。「キャンセルや変更」を「キャンセル」だけ・「キャンセル料や変更料」を
    「キャンセル料」だけにしない（説明と違う中身＝11-15 日本時間11時59分07秒の句の確かめの数と期限の言葉を落とさないのと同じ理由。L12）。並びの言葉を1つも
    言わない名詞・句は見ない（「料金」）。"""
    have = set(re.findall(_ITEM, words or ""))
    for chain in _ITEMS.findall(source or ""):
        members = set(re.findall(_ITEM, chain))
        if have & members and not members <= have:
            return False
    return True


def clause_fits(intro: str, text: str, clause: str | None, announce: str = "") -> bool:
    """お知らせの中身を常体にした句として使えるか（形の決まりの唯一の場所＝取込の clause_decide・import_ai.decide_matters の考える Sonnet の答え）：空でない・読点も
    句点も問いも含まない（askitems.noun_fits の1か所）・かっこを含まない（spoken の1か所＝お知らせの文と同じ決まり）・常体（終わりが POLITE_ENDS でない）・「件」の前に置ける終わり（CLAUSE_ENDS）・言葉の出どころの数と期限の
    言葉を落とさない・出どころに無い数と期限の言葉を足さない（どちらも keeps_facts の1か所を両向きに）・出どころに無い言葉（漢字・カタカナ・英字のまとまり）を
    足さない。言葉の出どころ＝説明と質問の文と、お客様が聞いた文（heard の1か所＝取込で決まったお知らせの文 announce・無ければ入る一言＋質問の文。お知らせの文は
    説明に無い事実を足さない中身の判定を通った文）。決まり・規則として言わない形は問い合わせ（MATTER_PROMPT）が頼む（言葉の形では止めない＝fits と同じ）。
    良い点と気をつけていただくことの2つの文のお知らせで、気をつけていただく文（caution の1か所）が良い点の文の期限を「それ以降」（BACK）で指すときは、句はその
    期限を「（期限）を過ぎた」（PAST）で1回だけ言う（伺い96 ア）：「を過ぎた」のすぐ前の言葉が、良い点の文で「（その言葉）まで」と言った期限であること・
    数と期限の言葉と出どころの確かめ（from_source の1か所）は、句の「を過ぎた」を「それ以降」を言ったものとして数える（「を過ぎた」の「過」は足してよい言葉。
    「前日以降」は「を過ぎた」が無いので通さない＝前日も入ってしまい、説明と違う。2つの文の中身を全部持つ句も「を過ぎた」が無いので通さない）。ほかの形の
    お知らせ（1つの文・良い点だけ・お知らせの文が無い）は今のまま。"""
    from askitems import spoken_noun_fits

    # 名詞の形・常体・かっこを使わない（声で言う句＝spoken_noun_fits の1か所。かっこは ✅ 2026-11-16 日本時間12時48分22秒「・伺い33：…」）
    if not spoken_noun_fits(clause) or not clause.endswith(CLAUSE_ENDS):
        return False
    source = "\n".join((intro, text, heard(intro, text, announce)))   # お知らせの文が無いとき heard は入る一言＋質問の文（重なりは確かめに効かない）
    if BACK in caution(announce):
        head, past, rest = clause.partition(PAST)
        deadline = _DEADLINE.search(head)
        if not past or PAST in rest or deadline is None or f"{deadline.group()}まで" not in announce.split("。")[0]:
            return False
        clause = f"{head}・{BACK}・{rest}"   # 「・」は言葉のまとまりの切れ目（_WORDS に入らない）
    return from_source(source, clause) and keeps_items(source, clause)


def caution(announce: str) -> str:
    """良い点と気をつけていただくことの2つの文のお知らせ（終わりが ANNOUNCE_CAUTION_END・文の数は phrases.ANNOUNCE_ENDS の1か所）の、気をつけていただく文
    （唯一の見分け＝句の確かめ clause_fits）。ほかの形（1つの文・良い点だけ・お知らせの文が無い）は空。"""
    if not (announce or "").endswith(ANNOUNCE_CAUTION_END) or announce.count("。") != ANNOUNCE_ENDS[ANNOUNCE_CAUTION_END]:
        return ""
    return announce.split("。")[1] + "。"


def from_source(source: str, words: str) -> bool:
    """取込で作った言葉 words が、言葉の出どころ source から作ったものか（唯一の決まり＝お知らせの中身の句 clause_fits・その後の文の中身 since.fits）：出どころの数と
    期限の言葉を落とさない・出どころに無い数と期限の言葉を足さない（keeps_facts の1か所を両向きに）・出どころに無い言葉（漢字・カタカナ・英字のまとまり）を足さない。"""
    return keeps_facts(source, words) and keeps_facts(words, source) and all(w in source for w in _WORDS.findall(words))


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
