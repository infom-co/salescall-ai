"""質問が「ほかに分からない点が無いかを伺う質問か」と、その「無い」にあたる選択肢を、質問の文と選択肢から決める（取込のとき。人は確かめない）。

✅ 2026-11-04 日本時間20時27分 ユーザー：「(1) トークスクリプトの「ご不明な点は…」の重ね聞き：「この質問は、ほかに分からない点が無いかを伺う質問か」を
決めます。（ご不明な点は無い）として扱い、重ねて伺いません。OK」（19時26分のご質問「お客様「だいたい分かりました。」の後に、こちら「ありがとうございます。
それではご不明な点はございますでしょうか？」となぜ、再度、不明点を確認するんですか？」）。

使う所：flowctl.Question.unclear_none（ご承認いただけなかったときの手順で「ほかにご納得いただけない点は…」に「分かった」と答えていただいたら、その質問は
伺わずに、この答えにする＝flowctl.Flow._no_doubts_step の1か所）。

決め方（facts.decide と同じ形）：文字のモデル2つに、1回の問い合わせでまとめて答えさせる（by_model）。2つの答えがそろい、「無い」にあたる選択肢がその
質問の選択肢にあるときだけ決まる。そろわない・答えが無いときは空（伺う）。⚠️ どちらに倒しても誤りは残りうる：ほかの質問を、分からない点が無いかを伺う
質問と取り違えると、伺うべき答えを伺わずに決めてしまう（依頼者へ誤った答えが渡る）。取り違えなければ、重ねて伺うだけ（お客様には同じことを2回伺うが、
答えは正しい）⇒ 誤った答えの重いほう（伺わずに決める）を避ける側に倒す。
"""
from __future__ import annotations


PROMPT = (
    "電話でお客様に聞く質問の一覧です。質問ごとに、次の2つを答えてください。\n"
    "- asks：この質問が、お客様に、分からない点・気になる点・確かめたいことがほかに無いかを伺う質問か（true／false）。\n"
    "- none：asks が true のときは、選択肢のうち「分からない点は無い」という意味になる選択肢の名前を、選択肢に書かれたとおりに1つ書きます。"
    "asks が false のときは空の文字を書きます。\n"
    "決まり：\n"
    "- 質問の文の意味だけで決めます。\n"
    "- 「無いでしょうか」と打ち消しの形で伺う質問では、「はい」が「分からない点は無い」の意味になります。\n"
    "質問（番号：質問の文／選択肢）：\n"
)


def _body(questions: list[tuple[str, tuple]]) -> str:
    return "\n".join(f"{n}：{text}／選択肢：{'・'.join(conds)}" for n, (text, conds) in enumerate(questions, 1))


# 答えの欄（問い合わせは askitems.ask の1か所。Gemini には説明つき・Anthropic には型だけ）
FIELDS = {"no": ("integer", "質問の番号"), "asks": ("boolean", "ほかに分からない点が無いかを伺う質問か"),
          "none": ("string", "「分からない点は無い」にあたる選択肢の名前（無ければ空）")}
TOOL = ("unclear", "質問ごとの、ほかに分からない点が無いかを伺う質問か")


def by_model(client, model: str, questions: list[tuple[str, tuple]]) -> list[dict | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。questions（(質問の文, 選択肢の並び)）と同じ並びの答え（無ければ None）を返す。
    送り先は部品の作りで決まる（Gemini・Anthropic とも同じ文・同じ読み取り＝askitems.ask）。"""
    from askitems import ask

    return _read_items(ask(client, model, PROMPT + _body(questions), FIELDS, tool=TOOL), len(questions))


def _read_items(items: list, n: int) -> list[dict | None]:
    """答えの items を、質問と同じ並び（1〜n 番）の {"asks", "none"}（無い・形の違うものは None）にそろえる（読み取りの唯一の場所＝Gemini も Anthropic も）。"""
    got = {x.get("no"): x for x in items or [] if isinstance(x, dict)}
    out = []
    for i in range(1, n + 1):
        x = got.get(i)
        ok = x is not None and isinstance(x.get("asks"), bool) and isinstance(x.get("none"), str)
        out.append({"asks": x["asks"], "none": x["none"]} if ok else None)
    return out


# Anthropic（Claude Haiku 5.5）に同じ文・同じ読み取りで答えさせる（facts.by_model_anthropic と同じ）。送り方は askitems.ask の1か所
by_model_anthropic = by_model


def decide(conditions: tuple, model_rows: list[dict | None]) -> str:
    """「無い」にあたる選択肢を1つに決める（取込のとき。人は確かめない）：2つ以上のモデルの答えがそろって「伺う質問」で、同じ選択肢を挙げ、その選択肢が
    質問の選択肢にあるときだけ、その名前。それ以外は空（伺う）。"""
    if len(model_rows) < 2 or any(r is None or not r["asks"] for r in model_rows):
        return ""
    nones = {r["none"] for r in model_rows}
    return next(iter(nones)) if len(nones) == 1 and next(iter(nones)) in conditions else ""
