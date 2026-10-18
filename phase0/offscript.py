"""AIの声が、プログラムの渡した言い方から外れたかを見分ける唯一の場所（DESIGN.md 6-7）と、外れたあとの言い直しの文。

- off_script：渡した言い方（言い直し・聞き返し・締めなど、返事に「言い方」があるターン）と、AIが話し始めた言葉（AI自身の文字。声の
  すぐあとに届く）を、頭の受けの言葉・お詫びを除いた読みで比べる。話し始めの CHECK_KANA 字（言い終わった単語まで）のうち、渡した言い方と
  合う字が MIN_MATCH に届かなければ外れた（言い回しが少し違うだけ・漢数字の書き方の違いは外れにしない）。
  ⛔ G-57 の3回目：言い直し「ありがとうございます。夕方の5時頃でよろしいでしょうか？」のはずが、「承知いたしました。では、これらを踏まえて、
     実際のプレゼン資料の構成や…」と、用件と関係の無い話を12秒した。
- restate：外れたあとに、新しいつなぎに言わせる文（「失礼いたしました。改めまして、」＋渡した言い方。人が言い間違えて、少し間を置いて
  言い直すのと同じ形＝✅ 2026-10-19 ユーザー「違和感のない形にしないと、AIや機械だと勘繰られますよ」。「改めまして」は 2026-10-14 に
  ユーザーが決めた、お詫びのあとの言い直しの頭＝phrases.LEAD_IN）。
  ⛔ 2026-10-19 G-57：お詫びを録音で流し、AIには「改めまして、…」だけを言わせる形では、6本中1本でAIが「失礼いたしました。」を自分でも
     言った（お詫びが2回。指示文の決まり「相手に不快な思いをさせたかもしれないときは、話し始めに『失礼いたしました。』」に従った）
     ⇒ お詫びもAIの言う文に入れる（2回になりようがない）。止めてから声が出るまでは約0.8〜1.1秒（人が言い直す前の間と同じくらい）。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from jtext import compare_reading
from phrases import ACK, APOLOGY, LEAD_IN

CHECK_KANA = 8     # 話し始めの、受けの言葉を除いた何字（読み）で決めるか（早く決めるほど、相手に聞こえる外れた言葉が短い）
# そのうち、渡した言い方と合う字の割合がこれ未満なら外れた。2026-10-19 に G-57 の言い方を渡したターン174で測った：本当に外れた2つは
# 0.0（声の無い英語の文字）と 0.125（「これらを踏まえて…」）、言い方どおり・言い回しが少し違うだけ（「3人で」⇒「3名様で」）は 0.5 以上 ⇒ 間の 0.3
MIN_MATCH = 0.3
# 話し始めに置いてよい言葉（受けの言葉・相づち・お詫び）。渡した言い方の頭と違っても、外れにしない
_LEADS = sorted({w.strip("、。") for ws in LEAD_IN.values() for w in ws} | {w.strip("、。") for w in ACK.values()}
                | {w.strip("、。") for w in APOLOGY} | {"かしこまりました", "承知いたしました", "失礼いたしました"}, key=len, reverse=True)


def _reading(text: str) -> str:
    from flowctl import _kanji_to_digits  # 漢数字の書き方の違い（「十五日」「15日」）を外れにしない（値の書き方をそろえる1か所）
    return compare_reading(_kanji_to_digits(text or ""))


_LEAD_READINGS = None


def _core(text: str) -> str:
    """頭の受けの言葉・お詫びを（いくつ続いても）除いた読み。"""
    global _LEAD_READINGS
    if _LEAD_READINGS is None:
        _LEAD_READINGS = [r for r in (_reading(w) for w in _LEADS) if r]
    r = _reading(text)
    cut = True
    while cut:
        cut = False
        for lead in _LEAD_READINGS:
            if r.startswith(lead):
                r, cut = r[len(lead):], True
    return r


def _settled(text: str) -> str:
    """届いた途中の文字のうち、言い終わった単語まで（最後の単語は、まだ途中かもしれない）。⛔ 2026-10-19：途中の漢字（「3人で大」）は
    読みが変わり（「大」＝おお）、言い方どおりのターンでも、合う字が半分に下がった（G-57 の言い方を渡したターン173のうち多数）。"""
    from jtext import words
    t = text.rstrip()
    if not t or t[-1] in "。、？！?!,.":
        return t
    ws = list(words(t))
    return "".join(w.surface for w in ws[:-1]) if len(ws) > 1 else ""


def off_script(expected: str, spoken: str) -> bool:
    """AIが話し始めた言葉（spoken。そのターンの、返事を送ったあとの AI自身の文字）が、渡した言い方（expected）から外れたか。
    受けの言葉を除いて CHECK_KANA 字に届くまでは決めない（False）。"""
    s, e = _core(_settled(spoken)), _core(expected)
    if len(s) < CHECK_KANA or not e:
        return False
    if any(lead.startswith(s) for lead in _LEAD_READINGS):
        return False  # 受けの言葉を言っている途中（「承知いたしま」まで届いた）。言い終わるまで決めない
    head = s[:CHECK_KANA]
    matched = sum(b.size for b in SequenceMatcher(None, head, e[:CHECK_KANA + 3], autojunk=False).get_matching_blocks())
    return matched / CHECK_KANA < MIN_MATCH


def restate(expected: str) -> str:
    """外れたあとの言い直しの文：頭の受けの言葉を除いた渡した言い方に、「失礼いたしました。改めまして、」を付ける。"""
    text = expected.strip()
    cut = True
    while cut:
        cut = False
        for lead in _LEADS:
            for sep in ("。", "、", ""):
                if text.startswith(lead + sep) and len(text) > len(lead + sep):
                    text, cut = text[len(lead + sep):].lstrip("、。 "), True
                    break
            if cut:
                break
    return f"{APOLOGY[0]}{LEAD_IN['お詫びのあとの言い直し'][0]}、{text}"
