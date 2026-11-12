"""名乗りのあとに言う用件の一文（業務設定の「用件の一文」＝依頼者が書いた文）から、用件そのものを表す名詞の言葉を決める（取込のとき。人は確かめない）。

✅ 2026-11-09 日本時間9時48分09秒 ユーザー：「申し訳ございませんでした。の後に、唐突に、ご予約の確認でお電話いたしました。と言われても何の話？ってなりますよ。
「前回の電話で途中になっておりました、ご予約の確認～」や「前回、お伺いすることができませんでした、ご予約の確認～」などのような一言が必要じゃないですか？」

使う所：instruction.Business.purpose_noun（続きの電話の用件の一文＝phrases.PURPOSE_AGAIN・どれを流すかは flowctl.purpose_part）。

決め方（facts・ways と同じ形＝問い合わせと読み取りは askitems.ask、合わせ方は askitems.agreed）：2つのモデルの答えがそろい、その文の中の言葉のときだけ、その言葉。
そろわない・答えが無い・文の中に無い言葉なら空＝続きの電話でも今の用件の一文のまま（つながらない一文が残るだけで、違う用件を言うことはない側へ倒す）。
"""
from __future__ import annotations

PROMPT = (
    "電話で名乗ったあとに言う用件の一文の一覧です。文ごとに、その文の中で用件そのものを表す言葉を、文の中の文字のまま noun に答えてください。\n"
    "決まり：\n"
    "- 例：「ご予約の確認でお電話いたしました。」なら「ご予約の確認」。「本日は先日お送りした資料の件でお電話いたしました。」なら「先日お送りした資料の件」。\n"
    "- 「本日は」「でお電話いたしました」など、用件を表さない言葉は入れません。\n"
    "- 文の中に無い言葉・言い換えた言葉は書きません。用件を表す言葉が無ければ空にします。\n"
    "文（番号：文）：\n"
)
FIELD, DESC = "noun", "用件そのものを表す言葉（文の中の文字のまま）"
TOOL = ("purposes", "文ごとの、用件そのものを表す言葉")


def by_model(client, model: str, texts: list[str]) -> list[str | None]:
    """1回の問い合わせでまとめて答えさせる（取込は業務の版ごとに1回）。texts と同じ並びの答え（無ければ None）を返す（問い合わせは askitems.ask の1か所）。"""
    from askitems import ask

    body = "\n".join(f"{n}：{t}" for n, t in enumerate(texts, 1))
    items = ask(client, model, PROMPT + body, {"no": ("integer", "文の番号"), FIELD: ("string", DESC)}, tool=TOOL)
    got = {x.get("no"): x.get(FIELD) for x in items or [] if isinstance(x, dict)}
    return [got[n].strip() if isinstance(got.get(n), str) else None for n in range(1, len(texts) + 1)]


def decide(text: str, model_nouns: list[str | None]) -> str:
    """用件の名詞を1つに決める（取込のとき）：2つ以上のモデルの答えがそろい（askitems.agreed）、文の中の言葉のときだけその言葉。ほかは空。"""
    from askitems import agreed

    noun = agreed(model_nouns, "") or ""
    return noun if noun and noun in text else ""
