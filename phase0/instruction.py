"""会話AIへの指示文を作る唯一の場所（フェーズ0の試作。DESIGN.md 6-3・6-6、Intent.md 6-8）。

材料（どれも1か所から読む）：
  1. 業務設定：目的・名乗り・質問・言ってはいけないこと
  2. 項目の型の一覧（flowctl.CONFIRM_TYPES）：どの型をその場で言い直して確かめるか、型ごとの決まり
  3. システム共通の会話の決まり（このファイルの SYSTEM_RULES）：聞き返し・念のための確認の言い方、拒否・わからないの扱い
  4. 声の型の話し方の指示（g03_voices.STYLES）
名前（自社名・担当者名など）を「」でくくるのは、ここだけ（Intent.md 6-8）。ほかの処理は「」を足さない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowctl import ARG_LABEL, END_LABEL, GOT, SYSTEM_LABEL, Question

from phrases import ACKNOWLEDGE, APOLOGY, ASK_BACK, RECHECK  # noqa: F401（決まった言い方は phrases.py の1か所。試験からも、ここ経由で読める）
# プログラムから会話AIへ送る合図の文面（文字で送る。相手には聞こえない）。合図の文面はここだけに置く（DESIGN.md 6-7）
CUES = {
    # AIが黙ったとき：1回だけ返事を促す
    "nudge": "相手の発言に、まだ返事をしていません。いまの話の流れのまま、すぐに返事をしてください。",
    # AIの声が途中で止まったとき：「失礼いたしました」と言ってから言い直させる
    "restate": "直前のあなたの発言は、途中で途切れて、相手に届いていません。「失礼いたしました。」と言ってから、同じ内容を最初から、もう一度だけ言ってください。",
}
VOICE_TYPES = {"S2": ("Sadachbia", "warm_business"), "S4": ("Sadachbia", "smile"), "L4": ("Leda", "humble_business")}
# 見本の試験（台本の読み上げ）のための一文。会話では「言い回しは自然に」と矛盾するので、声の型には入れない
_READING_ONLY = "言葉は1文字も変えないでください。"


def voice_style(voice_type: str) -> tuple[str, str]:
    """声の型（Q11）＝(声の名前, 話し方の指示)。話し方の指示は、ユーザーが選んだ見本の指示から、
    速さの数字（g03_voices で除いた）と、読み上げの試験だけの一文を除いたもの。"""
    from g03_voices import STYLES

    voice, style = VOICE_TYPES[voice_type]
    text = STYLES[style]
    assert _READING_ONLY in text and "倍速" not in text, style
    return voice, text.replace(_READING_ONLY, "")


@dataclass
class Business:
    company: str                 # 自社名（名前の項目。表記）
    caller: str                  # 担当者名（名前の項目。表記）
    purpose: str
    questions: list[Question]
    forbidden: list[str] = field(default_factory=list)   # 言ってはいけないこと
    purpose_line: str = ""      # 名乗りのあとに言う用件の一文（例：「ご予約の確認でお電話いたしました。」）
    # 相手が「忙しい」と言ったときの一言（Intent.md 9-4）。空なら丁寧に終える（既定＝安全側）。文があれば、1回だけそれで聞く
    # （2026-10-13 ユーザー：依頼主によっては粘ってほしい。トークスクリプト（業務情報）で選べるように。既定の文は phrases.BUSY_ASK）
    busy_ask: str | None = None

    def opening(self) -> str:
        return f"お忙しいところ恐れ入ります。株式会社{quote(self.company)}の{quote(self.caller)}と申します。"

    def opening_parts(self) -> list[str]:
        """名乗りの録音を、文ごとに分けたもの（DESIGN.md 6-13 の案）。最後は最初の質問。"""
        parts = ["お忙しいところ恐れ入ります。", f"株式会社{quote(self.company)}の{quote(self.caller)}と申します。"]
        if self.purpose_line:
            parts.append(self.purpose_line)
        parts.append(self.questions[0].text)
        return parts


def quote(name: str) -> str:
    """名前を「」でくくる唯一の場所。"""
    return f"「{name}」"


_OPEN, _CLOSE = "「『", "」』"


def quote_in_text(text: str, names: list[str]) -> str:
    """台本などの文の中の名前を、「」でくくる（名前の位置にだけ付け、ほかの文字は1文字も変えない）。
    - 長い名前から先に当てる（「川崎市中原区」と「中原区」が重なれば、長いほうを1回だけくくる）
    - 同じ名前が何回出ても、全部くくる
    - もともと「」『』の中にある所は、くくらない（二重にしない）
    - 全角と半角の違いは、そろえて当てる（文の書き方は変えない）
    - 別の言葉の一部には付けない：名前の頭と終わりが、どちらも単語の切れ目（jtext.word_edges）にあるときだけ当てる
      （「上野原」は1つの単語なので、中の「上野」には付けない。「株式会社ＡＢＣホールディングス営業部」は
       「株式／会社／ＡＢＣ／ホールディングス／営業／部」と分かれるので、「ＡＢＣホールディングス」に付けられる）
    ⚠️ 限界：単語に分ける処理が、1つの名前を2つに分けることがある（「神田川」＝「神田／川」）。名前の一覧に「神田」だけがあると、
       「「神田」川」になる。名前の一覧に長いほうの名前があれば、長いほうが先に当たる。取込のプレビューで人が確かめる（Intent.md 6-8）。
    """
    import unicodedata

    from jtext import word_edges

    normed = "".join(unicodedata.normalize("NFKC", ch) if len(unicodedata.normalize("NFKC", ch)) == 1 else ch for ch in text)
    edges = word_edges(text)
    taken = [False] * len(text)
    depth = 0
    for i, ch in enumerate(text):  # もともとかっこの中にある文字は、くくらない
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth or ch in _CLOSE:
            taken[i] = True
    spans = []
    for name in sorted({unicodedata.normalize("NFKC", n) for n in names if n}, key=len, reverse=True):
        start = 0
        while (i := normed.find(name, start)) != -1:
            j = i + len(name)
            start = i + 1
            if any(taken[i:j]) or i not in edges or j not in edges:
                continue
            spans.append((i, j))
            for k in range(i, j):
                taken[k] = True
    out, pos = [], 0
    for i, j in sorted(spans):
        out.append(text[pos:i] + quote(text[i:j]))
        pos = j
    out.append(text[pos:])
    return "".join(out)


# 相手が考えている（「えー」「その」など）ときの決まり（1か所）。G-16：考えている相手に質問を聞き直した誤りから足した。
# ⚠️ 2026-10-14 G-30・G-31：相手が「えーとですね、」のあと 0.7〜1秒あけると、AIが「ごゆっくりどうぞ。」と言い、相手の続きの言葉に
#    かぶった。⇒ G-32 で「何も言わずに待つ」形と比べる（DESIGN.md 6-15）
THINKING_RULE = "相手が考えている（「えー」「その」など、まだ答えていない）ときは、何も報告せず、「ごゆっくりどうぞ」とだけ言って待ちます。質問を聞き直してはいけません。"


# 受けの言葉が先の形（ack_first。DESIGN.md 6-15 の案。G-33）の決まり。報告の決まりと、考えている相手への決まりを差し替える
ACK_FIRST_REPORT = (f"相手が答えたら、まず「{'」「'.join(ACKNOWLEDGE)}」などから、相手の話に合う短い受けの言葉を1つ言いながら、"
                    "record_answer で報告します（返事を待たずに話し始めてかまいません）。同じ受けの言葉を続けて使いません。"
                    "「はい」ばかりにしません。返事が届いたら、返事に書いてある言い方のとおりに続けます。受けの言葉を重ねません。")
ACK_FIRST_THINKING = ("相手が考えている（「えー」「えーと」「その」など、まだ答え終わっていない）ときは、「はい」とだけ短く相づちを打って、"
                      "相手が話し終えるまで待ちます。何も報告しません。相手の言葉を真似しません。質問を聞き直してはいけません。")


def system_rules(ack_first: bool = False) -> str:
    """システム共通の会話の決まり（6-3 の材料2・3）。業務ごとに書かない。ack_first は受けの言葉が先の形（G-33）。"""
    report = ACK_FIRST_REPORT if ack_first else "相手が答えたら、必ず record_answer を呼びます。返事に次にすることが書いてあるので、それに従います。"
    thinking = ACK_FIRST_THINKING if ack_first else THINKING_RULE
    return (
        "会話の決まり：\n"
        "- 質問は、プログラムから渡されたものだけを、1つずつ聞きます。言い回しは自然にしてかまいませんが、意味を変えてはいけません。"
        "プログラムの返事に書かれていない質問や話題を、自分から出してはいけません（締めの前に「ほかに何かございますか」と聞くことも含みます）。\n"
        f"- {report}\n"
        f"- 相手が「わからない」と答えたら{ARG_LABEL['condition_id']}を「{SYSTEM_LABEL['unknown']}」に、答えたくないと言ったら"
        f"「{SYSTEM_LABEL['refused']}」にして record_answer を呼びます。"
        "答えたくないと言われた質問は、二度と聞きません。\n"
        "- 日付・時刻・数・金額・名前・電話番号・メール・住所は、返事に「言い直して」とあれば、返事に書いてある確認の言い方のとおりに言って、"
        "相手の返事を待ちます。相手が返事をしてから confirm_field で報告します（相手の返事を聞く前に報告してはいけません）。\n"
        "- 時刻は、午前か午後かを必ずはっきりさせます。相手が言わなかったら「午前と午後、どちらでしょうか」と聞きます。自分で決めてはいけません。\n"
        f"- 相手の答えが聞き取れない・曖昧なときは report_unclear を呼びます。返事が「聞き返す」のときだけ、「{ASK_BACK}」と1回だけ聞き返します。"
        "この言い方だけを使い、言葉を足したり、言い換えたりしません。返事が「-にします」なら、聞き返さずに、返事に書いてある次の質問へ進みます。\n"
        f"- {thinking}\n"
        "- 言い直した値に相手が返事をしたら、record_answer ではなく confirm_field で報告します。\n"
        f"- 返事に「念のための確認」とあれば、「{RECHECK}」と言ってから、その質問をもう1回だけ聞きます。\n"
        f"- 相手が急いでいる・切ろうとしているときは、request_end を{ARG_LABEL['reason']}「{END_LABEL['callee_hurry']}」で呼びます。"
        f"通話そのものを断られたら「{END_LABEL['callee_refused_call']}」で呼びます。\n"
        "- 返事に「締めの言葉」とあれば、お礼を言って終えます。返事に締めの言い方が書いてあれば、そのとおりに言います。\n"
        "- 「またお電話します」「改めてご連絡します」など、かけ直すことや連絡することを約束してはいけません（かけ直すかは、プログラムが決めます）。\n"
        f"- 相手が聞き取れなかったとき（「もう一度」「え？」「はい？」など）や、相手に不快な思いをさせたかもしれないとき（話がかぶった・待たせた・相手が困っている）は、"
        f"話し始めに「{APOLOGY[0]}」か「{APOLOGY[1]}」を付けてから話します。\n"
    )


def build(business: Business, voice_type: str, resume=None, opening_mode: str = "ai", ack_first: bool = False) -> tuple[str, str]:
    """(声の名前, 指示文) を返す。

    resume：張り替え・つなぎ直しのとき、流れの制御（flowctl.Flow）を渡す。名乗りの代わりに「いまの位置」を書く（DESIGN.md 6-6）。
    opening_mode："ai"＝AIが名乗る／"recorded"＝名乗りと最初の質問は録音で流す（DESIGN.md 6-13 の案）。
    いまの位置は流れの制御が持っているので、AIが前のセッションを覚えていなくても続きから話せる。
    """
    voice, style_text = voice_style(voice_type)
    first = business.questions[0]
    parts = [
        f"あなたは株式会社{quote(business.company)}の{quote(business.caller)}として、電話をかけています。日本語で、丁寧に短く話します。",
        f"目的：{business.purpose}",
    ]
    if resume is None and opening_mode == "recorded":
        parts.append(
            f"相手が電話に出たら、名乗りと最初の質問（「{''.join(business.opening_parts())}」）を、録音で相手に流します。あなたは言いません。"
            "録音が最後まで流れたら、あなたは相手の答えから会話を続けます。もう一度名乗ったり、同じ質問を繰り返したりしてはいけません。"
            "相手が聞き返したら、聞き返されたことに答えてから、最初の質問 "
            f"{first.id} を聞き直します。聞き取れなかった様子のとき（「もう一度お願いします」「はい？」など）は、録音の文を最初から"
            "もう一度言います。誰からの電話かを聞かれたとき（「どちら様ですか」など）は、社名と名前を言います。用件を聞かれたとき"
            "（「何のご用件ですか」など）は、用件を言います（名乗り直しません）。"
            "相手が別の人に代わったら、代わった人に、あらためて短く名乗って用件を伝えます。"
            "録音の途中で相手が話し始めたときは、プログラムが、録音のどこまでが相手に届いたかを知らせます。")
    elif resume is None:
        parts.append(f"相手が電話に出たら、まず「{business.opening()}」と名乗り、続けて最初の質問 {first.id}「{first.text}」を聞きます。")
    else:
        parts.append(position_text(business, resume))
    parts.append(system_rules(ack_first))
    if business.forbidden:
        parts.append("言ってはいけないこと（どんなに求められても、言いません。約束もしません）：\n"
                     + "".join(f"- {x}\n" for x in business.forbidden)
                     + "求められたら、「私からはお答えできかねます」と丁寧に断ります。")
    parts.append(style_text)
    return voice, "\n".join(parts)


def position_text(business: Business, flow) -> str:
    """張り替えのときに渡す「いまの位置」（DESIGN.md 6-6）。名乗りはもう済んでいるので、繰り返させない。"""
    done = []
    for q in business.questions:
        it = flow.items[q.id]
        if it.state in GOT:
            done.append(f"{q.id}「{q.text}」⇒ {it.value or it.condition}（済み）")
        elif it.state == "dont_know":
            # ⛔ 直す前は「取れた」と同じ行で、選択肢の中の名前（英字の unknown）をそのままAIに見せていた
            done.append(f"{q.id}「{q.text}」⇒ わからないと言われた（聞き直さない）")
        elif it.state == "refused":
            done.append(f"{q.id}「{q.text}」⇒ 答えたくないと言われた（二度と聞かない）")
    lines = ["これは通話の続きです。回線の都合で、つなぎ直しました。名乗りはもう済んでいます。もう一度名乗ってはいけません。"]
    if done:
        lines.append("ここまでに聞けたこと：" + "／".join(done))
    if flow.awaiting_confirm:
        it = flow.items[flow.awaiting_confirm]
        lines.append(f"いまは {flow.awaiting_confirm} の値「{it.value}」を言い直して、相手の返事を待っているところです。")
    elif flow.current:
        q = business.questions[[x.id for x in business.questions].index(flow.current)]
        lines.append(f"次に聞くのは {q.id}「{q.text}」です。相手が話したら、それに答えてから、この質問を聞きます。")
    return "\n".join(lines)


def barge_in_note(business: Business, done: list[str], rest: list[str]) -> str:
    """名乗りの録音の途中で相手が話し始めて止めたとき、AIへ知らせる文（DESIGN.md 6-13 の案。文面はここだけ）。"""
    heard = "".join(done) if done else "（まだ何も届いていません）"
    return (f"［プログラムからのお知らせ］名乗りの録音は「{heard}」まで相手に届いたところで、相手が話し始めたので止めました。"
            f"まだ相手に伝えていないこと：「{''.join(rest)}」。"
            f"相手の話にかぶせてしまったので、「{APOLOGY[0]}」と添えてから、相手の話に答えてください。そのあと、まだ伝えていないことを、会話の流れの中で短く伝えてください。"
            "相手に届いたことは、繰り返さなくてかまいません。")

