"""AIの声が、プログラムの渡した言い方から外れたかを見分ける唯一の場所（DESIGN.md 6-7）と、外れたあとの言い直しの文。

- off_script：渡した言い方（言い直し・聞き返し・締めなど、返事に「言い方」があるターン）と、AIが話し始めた言葉（AI自身の文字。声の
  すぐあとに届く）を、頭の受けの言葉・お詫びを除いた読みで比べる。話し始めの CHECK_KANA 字（言い終わった単語まで）のうち、渡した言い方と
  合う字が MIN_MATCH に届かなければ外れた（言い回しが少し違うだけ・漢数字の書き方の違いは外れにしない）。
  ⛔ G-57 の3回目：言い直し「ありがとうございます。夕方の5時頃でよろしいでしょうか？」のはずが、「承知いたしました。では、これらを踏まえて、
     実際のプレゼン資料の構成や…」と、用件と関係の無い話を12秒した。
- SpeechGate：AIの声を電話へ流してよい所（決まるまでは、最初の文の切れ目で待つ）。止めたときに相手に何が聞こえていたかも、ここで決める。
- restate：外れたあとに、新しいつなぎに言わせる文（「失礼いたしました。改めまして、」＋渡した言い方。人が言い間違えて、少し間を置いて
  言い直すのと同じ形＝✅ 2026-10-19 ユーザー「違和感のない形にしないと、AIや機械だと勘繰られますよ」。「改めまして」は 2026-10-14 に
  ユーザーが決めた、お詫びのあとの言い直しの頭＝phrases.LEAD_IN）。
  ⛔ 2026-10-19 G-57：お詫びを録音で流し、AIには「改めまして、…」だけを言わせる形では、6本中1本でAIが「失礼いたしました。」を自分でも
     言った（お詫びが2回。指示文の決まり「相手に不快な思いをさせたかもしれないときは、話し始めに『失礼いたしました。』」に従った）
     ⇒ お詫びもAIの言う文に入れる（2回になりようがない）。止めてから声が出るまでは約0.8〜1.1秒（人が言い直す前の間と同じくらい）。
"""
from __future__ import annotations

import re
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


def verdict(expected: str, spoken: str) -> str | None:
    """AIが話し始めた言葉（spoken。返事を送ったあとの AI自身の文字）が、渡した言い方（expected）どおりか："ok"＝言い方どおり・"off"＝外れた・
    None＝まだ決めない（受けの言葉を除いて CHECK_KANA 字に届いていない・受けの言葉を言っている途中）。"""
    s, e = _core(_settled(spoken)), _core(expected)
    if len(s) < CHECK_KANA or not e:
        return None
    if any(lead.startswith(s) for lead in _LEAD_READINGS):
        return None  # 受けの言葉を言っている途中（「承知いたしま」まで届いた）。言い終わるまで決めない
    head = s[:CHECK_KANA]
    matched = sum(b.size for b in SequenceMatcher(None, head, e[:CHECK_KANA + 3], autojunk=False).get_matching_blocks())
    return "off" if matched / CHECK_KANA < MIN_MATCH else "ok"


def off_script(expected: str, spoken: str) -> bool:
    return verdict(expected, spoken) == "off"


PAUSE_S = 0.15     # 文の切れ目とみなす、声の中の無音（受けの言葉の文のあと。電話へ流すのを、決まるまでここで待つ）
NONE_S = 0.15      # これより短い声しか流れていなければ、相手には何も聞こえていないとみなす
# 文の切れ目で待つ長さの上限（決まらなければ、そのあとは流す）。G-57 の言い方を渡したターン239で、待つ必要があったのは9・最長1.99秒
MAX_HOLD_S = 2.0


def first_pause(pcm: bytes, rate: int) -> float | None:
    """声の中の、最初の文の切れ目（PAUSE_S 以上の無音の始まり。秒）。まだ無ければ None。"""
    from barge import voice_runs, voiced
    runs = voice_runs(voiced(pcm, rate), 0.05)
    return next((a[1] for a, b in zip(runs, runs[1:]) if b[0] - a[1] >= PAUSE_S), None)


class SpeechGate:
    """渡した言い方のあるターンで、AIの声を電話へ流してよい所（唯一の場所。電話接続側が使う。試作では、止めたときに相手に何が聞こえていたかを
    convo がこれで数える）。✅ 2026-10-19 ユーザー：「明らかに違う内容を喋ろうとしているのが事前に分かるのであれば、止めてもOKだよ」。
    - AI自身の文字は、たいてい最初の声と同時に届き（G-57 の言い方を渡したターン239で、中央0.0秒）、声は流すより速く届く（決められた時点で、
      受け取り済みの声は中央1.79秒・流れていたのは中央0.27秒）⇒ 流す前に決められる。
    - 決まるまでは、最初の文の切れ目（受けの言葉の文のあと）までだけ流して待つ。言い方どおりと決まったら続きを流し、外れたら、それ以上流さない。
      待つのは239ターン中9（0.5秒より短いのが5・最長2.0秒。文の切れ目なので、ふつうの間に聞こえる）。待つのは MAX_HOLD_S まで。
    - ⛔ 2026-10-19 G-57 の本番の形で見つけた穴：AI自身の文字が1つも届かないターンがある（言い方を渡して声が出た333ターン中11＝3.3%。声は
      言い方どおり）。文字が無いまま切れ目で待つと、続きが流れない ⇒ 切れ目までに文字が1つも届いていなければ待たない（見張れないので、これまで
      どおり流す）。文字は届いたが決まらないときだけ、MAX_HOLD_S まで待つ。
    expected_of：いまの渡した言い方を返す関数（返事を送るたびに変わる。None なら見張らない）。force：試験だけ（最初の文字で外れにする）。"""

    def __init__(self, expected_of, force: bool = False):
        self.expected_of, self.force = expected_of, force
        self.reset(0)

    def reset(self, audio_from: int) -> None:
        """返事を送った（新しい言い方になる）。audio_from＝そこまでに受け取った声のバイト数（このあとの声だけを数える）。"""
        self.text, self.state, self.audio_from, self.first_audio_at, self.first_text_at = "", None, audio_from, None, None

    def on_audio(self, at_s: float) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = at_s

    def on_text(self, text: str, at_s: float | None = None) -> str | None:
        """AI自身の文字が届いた（at_s＝届いた時刻）。いまの決まり（None・"ok"・"off"）を返す。一度決まったら変えない。"""
        self.text += text
        if self.first_text_at is None:
            self.first_text_at = at_s
        if self.state is None:
            expected = self.expected_of()
            if expected:
                self.state = "off" if self.force else verdict(expected, self.text)
        return self.state

    def heard(self, at_s: float, pcm: bytes, rate: int) -> dict:
        """外れたと決まって止めた時点（at_s）で、相手に聞こえていたもの。kind："none"＝まだ何も／"opener"＝受けの言葉の文だけ／
        "content"＝外れた中身まで（最初の文が受けの言葉でないとき＝待つ所より前に中身があった）。seconds＝流れた声の秒。"""
        pcm = pcm[self.audio_from:]
        total = len(pcm) / 2 / rate
        elapsed = max(0.0, at_s - self.first_audio_at) if self.first_audio_at is not None else 0.0
        pause = first_pause(pcm, rate)
        # 切れ目で待ったか：切れ目に着くまでに文字が届いていたときだけ（届かなければ待たずに流した）。待つのは MAX_HOLD_S まで
        held = (pause is not None and self.first_text_at is not None and self.first_audio_at is not None
                and self.first_text_at - self.first_audio_at <= pause)
        if held:
            played = pause if elapsed <= pause + MAX_HOLD_S else elapsed - MAX_HOLD_S
        else:
            played = elapsed
        played = min(played, total)
        first = re.split(r"(?<=[。？！?!])", self.text, maxsplit=1)[0]
        opener_only = not _core(first) and (pause is None or played <= pause + 0.05)  # 流れたのは、受けの言葉の文のうちだけ
        kind = "none" if played < NONE_S else ("opener" if opener_only else "content")
        return {"kind": kind, "seconds": round(played, 2)}


def _strip_leads(expected: str) -> str:
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
    return text


def restate(expected: str, heard: str = "content") -> str:
    """外れたあとの言い直しの文。相手に聞こえていたもの（SpeechGate.heard の kind）で変える：
    "none"＝何も聞こえていない ⇒ 渡した言い方のまま（相手には、少し間があいただけ）／"opener"＝受けの言葉の文だけ ⇒「それでは、」＋中身
    （✅ 2026-10-14 ユーザーの決めた、受けたあとの言い直しの頭）／"content"＝外れた中身まで ⇒「失礼いたしました。改めまして、」＋中身。"""
    if heard == "none":
        return expected.strip()
    lead = LEAD_IN["言い直しを受けて"][0] if heard == "opener" else APOLOGY[0] + LEAD_IN["お詫びのあとの言い直し"][0]
    return f"{lead}、{_strip_leads(expected)}"
