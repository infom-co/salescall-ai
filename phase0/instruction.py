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

from flowctl import Question

ASK_BACK = "聞き取りにくかったので、もう一度、おっしゃっていただいてよろしいでしょうか。"
RECHECK = "念のため確認させてください。"
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

    def opening(self) -> str:
        return f"お忙しいところ恐れ入ります。株式会社{quote(self.company)}の{quote(self.caller)}と申します。"


def quote(name: str) -> str:
    """名前を「」でくくる唯一の場所。"""
    return f"「{name}」"


def system_rules() -> str:
    """システム共通の会話の決まり（6-3 の材料2・3）。業務ごとに書かない。"""
    return (
        "会話の決まり：\n"
        "- 質問は、プログラムから渡されたものだけを、1つずつ聞きます。言い回しは自然にしてかまいませんが、意味を変えてはいけません。\n"
        "- 相手が答えたら、必ず record_answer を呼びます。返事に次にすることが書いてあるので、それに従います。\n"
        "- 相手が「わからない」と答えたら condition_id を unknown に、答えたくないと言ったら refused にして record_answer を呼びます。"
        "答えたくないと言われた質問は、二度と聞きません。\n"
        "- 日付・時刻・数・金額・名前・電話番号・メール・住所は、返事に「言い直して」とあれば、書いてある値のとおりに言い直して、相手の返事を待ちます。"
        "相手が返事をしてから confirm_field で報告します（相手の返事を聞く前に報告してはいけません）。\n"
        "- 時刻は、午前か午後かを必ずはっきりさせます。相手が言わなかったら「午前と午後、どちらでしょうか」と聞きます。自分で決めてはいけません。\n"
        f"- 聞き取れない・曖昧なときは report_unclear を呼びます。返事が「聞き返す」なら「{ASK_BACK}」と聞き返します。\n"
        f"- 返事に「念のための確認」とあれば、「{RECHECK}」と言ってから、その質問をもう1回だけ聞きます。\n"
        "- 相手が急いでいる・切ろうとしているときは、request_end を callee_hurry で呼びます。通話そのものを断られたら callee_refused_call で呼びます。\n"
        "- 返事に「締めの言葉」とあれば、お礼を言って終えます。\n"
    )


def build(business: Business, voice_type: str, resume=None) -> tuple[str, str]:
    """(声の名前, 指示文) を返す。

    resume：張り替え・つなぎ直しのとき、流れの制御（flowctl.Flow）を渡す。名乗りの代わりに「いまの位置」を書く（DESIGN.md 6-6）。
    いまの位置は流れの制御が持っているので、AIが前のセッションを覚えていなくても続きから話せる。
    """
    voice, style_text = voice_style(voice_type)
    first = business.questions[0]
    parts = [
        f"あなたは株式会社{quote(business.company)}の{quote(business.caller)}として、電話をかけています。日本語で、丁寧に短く話します。",
        f"目的：{business.purpose}",
    ]
    if resume is None:
        parts.append(f"相手が電話に出たら、まず「{business.opening()}」と名乗り、続けて最初の質問 {first.id}「{first.text}」を聞きます。")
    else:
        parts.append(position_text(business, resume))
    parts.append(system_rules())
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
        if it.state in ("confirmed", "accepted"):
            done.append(f"{q.id}「{q.text}」⇒ {it.value or it.condition}（済み）")
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
