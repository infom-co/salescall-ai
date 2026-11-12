"""お客様が「どちらの場合もありうる」と言ったとき（「どっちでもいいですよ」「当日次第ですね」）に、依頼者が知りたい側の答えの可能性を確かめる、その答え
（選択肢）と言葉を、質問の文と選択肢から決める（取込のとき。人は確かめない）。

⛔✅ 2026-11-09 日本時間4時36分49秒 ユーザー：「どっちでもいいですよ。に対して…こちら「はい、恐れ入ります。もう一度お聞かせいただいてもよろしいでしょうか？」で
本当に良いと思っているんですか？…「お車で来る可能性もあるということでよろしいでしょうか？」とか、「駐車場が必要な可能性もあるということでよろしいでしょうか？」
というような確認になるんじゃないんですか？大事なのは、最終的に依頼者は何を知りたいのかということですよね。それがわからないトークスクリプトなんて普通ないと
思いますよ。」

使う所：flowctl.Question.maybe（(選択肢, 言葉)。あれば、その質問の選べるものに「どちらでも」を出し＝all_conditions、判定がそう読んだら受け取らずに
「はい、（言葉）可能性もあるということでよろしいでしょうか？」で確かめる＝flowctl.Flow._ask_confirm の maybe・言い方は phrases.MAYBE_ASK）。

決め方（unclear.decide と同じ形）：文字のモデル2つに、1回の問い合わせでまとめて答えさせる（by_model）。2つの答えがそろい、選択肢がその質問の選択肢にあり、
言葉が決まりの形（check）のときだけ決まる。そろわない・答えが無いときは空（今の扱い＝「どちらでも」を出さない質問は判定が「決められない」と読み、質問から
聞き直す）。⚠️ どちらに倒しても誤りは残りうる：こちらに選ぶのを任せただけの質問（お支払いの方法など）に可能性の確かめを付けると、お客様に要らない確かめを
1回する（答えは正しい）。可能性のある質問で空になると、今までどおり聞き直す ⇒ 依頼者へ誤った答えは渡らない（どちらの側も、確かめへのお客様ご本人の答えで決まる）。
"""
from __future__ import annotations

import unicodedata

# 例の質問は、測りの質問（g53_meaning.question_map）に無いもの（G-48 の決まり＝例に引きずられた答えを測りで数えない。試験が見張る）
EXAMPLES = ("送迎バスはご利用になりますでしょうか？", "お支払いは現金とカードのどちらになさいますか？", "ご契約の書類はもうお手元にございますでしょうか？")
PROMPT = (
    "電話でお客様に聞く、選択肢のある質問の一覧です。お客様が「どっちでもいいですよ」「当日次第ですね」のように、どの答えにもなりうると言ったとき、"
    "依頼者が知りたいのは、そのうち依頼者が前もって備えなければならない答えになる可能性があるかです（来られる手段なら、お車で来られる可能性があれば駐車場を"
    "用意する、など）。その可能性を、次の言い方で確かめます。\n"
    "「（　）可能性もあるということでよろしいでしょうか？」\n"
    "質問ごとに、次の2つを答えてください。\n"
    "- choice：依頼者が備えなければならない答えの選択肢の名前を、選択肢に書かれたとおりに1つ書きます。当たらない質問は空の文字を書きます。\n"
    "- words：その答えになることを、（　）に入れる言葉で書きます。当たらない質問は空の文字を書きます。\n"
    "決まり：\n"
    "- 書くのは、お客様のこれからのご様子・ご予定（来られる手段・当日ご利用になるもの・お連れの方など）で、どの答えになるかがまだ決まっていないことがある"
    "質問だけです。\n"
    "- お客様がどれかを選んでこちらへ伝えるご希望（お支払いの方法・お届けの時間帯など。どれでもよいと言われたら、こちらで選べばよい）と、もう決まっている"
    "事実（届いているか・ご契約の有無など）は、どちらも空にします。\n"
    "- 質問の言葉をできるだけそのまま使い、お客様に向ける丁寧な言い方にします。質問に無い意味を足しません。\n"
    "- 「、」（読点）を入れません。「？」で終えません。「可能性」「ということ」は書きません（あとに続きます）。\n"
    "- 打ち消しの問い（「〜ではございませんか？」）の「はい」は、打ち消しのほうの意味です。\n"
    f"- 例：「{EXAMPLES[0]}」（利用する・利用しない）⇒ choice「利用する」・words「送迎バスをご利用になる」／"
    f"「{EXAMPLES[1]}」⇒ どちらも空／「{EXAMPLES[2]}」⇒ どちらも空\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _body(questions: list[tuple[str, tuple]]) -> str:
    return "\n".join(f"{n}：{text}／選択肢：{'・'.join(conds)}" for n, (text, conds) in enumerate(questions, 1))


# 答えの欄（問い合わせは askitems.ask の1か所。Gemini には説明つき・Anthropic には型だけ）
FIELDS = {"no": ("integer", "質問の番号"), "choice": ("string", "依頼者が備えなければならない答えの選択肢の名前（当たらなければ空）"),
          "words": ("string", "その答えになる可能性を確かめる言い方の（　）に入れる言葉（当たらなければ空）")}
TOOL = ("maybes", "質問ごとの、どの答えにもなりうると言われたときに可能性を確かめる答えと言葉")


def by_model(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。questions（(質問の文, 選択肢の並び)）と同じ並びの答え（無ければ None）を返す。
    送り先は部品の作りで決まる（Gemini・Anthropic とも同じ文・同じ読み取り＝askitems.ask）。"""
    from askitems import ask

    return _read_items(ask(client, model, PROMPT + _body(questions), FIELDS, tool=TOOL), len(questions))


def _read_items(items: list, n: int) -> list[dict | None]:
    """答えの items を、質問と同じ並び（1〜n 番）の {"choice", "words"}（無い・形の違うものは None）にそろえる（読み取りの唯一の場所＝Gemini も Anthropic も）。"""
    got = {x.get("no"): x for x in items or [] if isinstance(x, dict)}
    out = []
    for i in range(1, n + 1):
        x = got.get(i)
        ok = x is not None and isinstance(x.get("choice"), str) and isinstance(x.get("words"), str)
        out.append({"choice": clean(x["choice"]), "words": clean(x["words"])} if ok else None)
    return out


# Anthropic（Claude Haiku 5.5）に同じ文・同じ読み取りで答えさせる（unclear.by_model_anthropic と同じ）。送り方は askitems.ask の1か所
by_model_anthropic = by_model


def clean(s: str | None) -> str:
    """答えの言葉をそろえる（文字の形・前後の空白とかぎかっこ・終わりの「。」）。"""
    return unicodedata.normalize("NFKC", (s or "").strip().strip("「」『』")).rstrip("。")


def check(words: str) -> bool:
    """言葉が決まりの形か（唯一の確かめ）：空でない・読点も問いの記号も無い・あとに続く「可能性」「ということ」を書いていない。"""
    return bool(words) and not any(c in words for c in "、，,？?") and not words.endswith(("可能性", "ということ", "こと"))


def decide(conditions: tuple, model_rows: list[dict | None]) -> tuple:
    """可能性を確かめる (選択肢, 言葉) を1つに決める（取込のとき。人は確かめない）：2つ以上のモデルの答えがそろって同じ選択肢と同じ言葉を挙げ、その選択肢が
    質問の選択肢にあり、言葉が決まりの形（check）のときだけ。それ以外は空（当たらない質問・決まらない）。"""
    if len(model_rows) < 2 or any(r is None for r in model_rows):
        return ()
    got = {(r["choice"], r["words"]) for r in model_rows}
    if len(got) != 1:
        return ()
    choice, words = next(iter(got))
    return (choice, words) if choice in conditions and check(words) else ()
