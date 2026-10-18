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

from flowctl import (ANY_LABEL, ARG_LABEL, CALLBACK_LABEL, CONFIRM_LABEL, END_LABEL, GOT, PART_LABEL, PARTY_LABEL, SYSTEM_LABEL, Question,
                     choices_note)
from names import hira, quote  # noqa: F401（名前を「」でくくる唯一の場所。ここからも読める）

from phrases import AI_ANSWER, APOLOGY, ASK_BACK, GREETING, target_call, target_confirm, target_who  # noqa: F401（決まった言い方は phrases.py の1か所。試験からも、ここ経由で読める）
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
class Target:
    """目的の人（話すべき相手。DESIGN.md 4-3）。業務設定の「目的の人」と、発信対象データの部署・役職・名前（Intent.md 6-8 の3）から作る。
    ✅ 2026-10-13 ユーザー承認（CL-2 経由）：指定のしかたで4種類＝役職のみ／役職（部署）＋名前／名前のみ／その店・会社の人なら誰でも。
    「誰でも」の業務は Target を持たない（最初に出た人が目的の人＝呼ばずに最初の質問から）。
    呼び方（✅ 2026-10-16 ユーザー）：法人「（部署、役職）の（相手の名前）様はいらっしゃいますでしょうか？」・個人「（相手の名前）様のお電話で
    お間違えないでしょうか？」（phrases.target_call）。名前は読み（ひらがな。決まらなければ、まず言ってみる読み）で言う（G-44）"""
    kind: str = "法人"   # 発信対象の区分（法人／個人）
    name: str = ""       # 相手の名前の表記（役職のみなら空）
    reading: str = ""    # 名前の読み（ひらがな）。読みが決まらない名前は、まず言ってみる読み（確かめは業務の質問に reading_question を足す）
    role: str = ""       # 部署・役職（「営業部部長」「社長」）。名前のみなら空

    def __post_init__(self):
        assert self.kind in ("法人", "個人"), self.kind
        assert self.name or self.role, "目的の人には、名前か役職が要ります（どちらも無ければ「誰でも」＝Target を持たない）"
        assert not self.name or self.reading, f"目的の人「{self.name}」の読みが要ります（決まらなければ、まず言ってみる読み）"

    def _name(self) -> str:
        return quote(hira(self.reading)) if self.name else ""

    def who(self) -> str:
        return target_who(self.kind, self._name(), self.role)

    def call(self) -> str:
        return target_call(self.kind, self._name(), self.role)

    def confirm(self) -> str:
        return target_confirm(self._name(), self.role)


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
    # 留守番電話に残す伝言（Intent.md 9-4「伝言を残すか残さないかを設定で選ぶ。残す場合の文面も設定で持つ」。⏳ 残すかは 18-2 の14 で
    # ユーザーが決める）。空なら残さずに切る（安全側）
    voicemail_message: str | None = None
    # 「AIですか？」と聞かれたときの答え（Intent.md 9-4「答える文面は業務設定で持つ」）。空なら既定の文（phrases.AI_ANSWER）
    ai_answer: str | None = None
    # 目的の人（DESIGN.md 4-3）。None＝「誰でも」（最初に出た人が目的の人）。目的の人のある業務では、名乗りの終わりは目的の人を呼ぶ一言で、
    # 目的の人に繋がるまで最初の質問を聞かない（流れの制御 Flow の calling の段）
    target: Target | None = None
    # 自社名・担当者名の読み（ひらがな。空＝決まらない＝表記のまま渡す）。自社名は、設定に無ければ取込で names.decide が決める（Intent.md 6-8）。
    # ✅ 2026-10-16 ユーザー決定（案C）：担当者名の読みは、設定に必ず入れる（名乗りでは言い換えられない。個人向けでは名乗ることが法律の確認事項）。
    #    必須かどうかを確かめるのは設定の登録（CL-2）。ここは、担当者名を unread_names に入れた矛盾した設定を受け付けない。
    # 声に出す文には読みのほうを入れる（G-44：漢字のままだと読み違える。instruction.quote_in_text と同じ決まり）
    company_reading: str = ""
    caller_reading: str = ""
    # こちら側の名前で、読みが決まらないもの（台本の中の、名乗る担当者以外の人の名前など。表記）。指示文で、声に出さずに言い換える決まりを渡す（unread_rule）。
    # ⚠️ お客様側の名前（目的の人）は入れない：まず言ってみる読みで呼び、つながってから確かめる（flowctl.reading_question。✅ 2026-10-16 ユーザー）
    unread_names: list[str] = field(default_factory=list)
    # 台本の名前（names.decide の戻り値）。質問の文・目的・用件の一文・忙しいときの一言の中の名前を、作るときに1回だけ quote_in_text に通す
    # （⛔ 見直しで見つけた漏れ：読みのひらがなへの置き換えが名乗りにしか効かず、質問の文の名前は漢字のまま・読みがなは2回読まれた）
    names: list = field(default_factory=list)

    def __post_init__(self):
        # 担当者名は名乗りで必ず声に出すので、「声に出さずに言い換える名前」には入れられない（指示が矛盾する。案C＝読みは設定に必須）
        assert self.caller not in self.unread_names, f"担当者名「{self.caller}」は、読みを設定に入れてください（unread_names には入れられません）"
        if not self.names:
            return
        import dataclasses
        import unicodedata
        # 名乗りの社名の読みも、名前の一覧から取る（設定に読みが入っていれば、そちらを先に使う）
        # ⛔ 見直し（2026-10-16 の2回目）：前は名乗りだけが company_reading を見て、一覧の読みを見ていなかった
        # ＝一覧に読みがあっても、名乗りは漢字・質問の文はひらがな、と食い違いうる形だった
        # ✅ 担当者名は一覧から取らない：読みは設定に必ず入れる（2026-10-16 ユーザー決定＝案C。取込で決める決め方は使わない）
        got = {unicodedata.normalize("NFKC", n["surface"]): n.get("reading") or "" for n in self.names if isinstance(n, dict) and n.get("surface")}
        self.company_reading = self.company_reading or got.get(unicodedata.normalize("NFKC", self.company), "")
        self.questions = [q if q.reading_of else dataclasses.replace(q, text=quote_in_text(q.text, self.names)) for q in self.questions]
        self.purpose = quote_in_text(self.purpose, self.names) if self.purpose else self.purpose  # AIに渡す目的の中の名前も（AIが口にしうる）
        self.purpose_line = quote_in_text(self.purpose_line, self.names) if self.purpose_line else self.purpose_line
        self.busy_ask = quote_in_text(self.busy_ask, self.names) if self.busy_ask else self.busy_ask

    def _said(self) -> tuple[str, str]:
        return quote(self.company_reading or self.company), quote(self.caller_reading or self.caller)

    def opening(self) -> str:
        company, caller = self._said()
        return f"{GREETING}株式会社{company}の{caller}と申します。"

    def opening_parts(self) -> list[str]:
        """名乗りの録音を、文ごとに分けたもの（DESIGN.md 6-13 の案）。最後は最初の質問。
        目的の人のある業務では、最後は目的の人を呼ぶ一言（用件と最初の質問は、目的の人に繋がってから＝DESIGN.md 4-3 の ⚠️）。"""
        company, caller = self._said()
        parts = [GREETING, f"株式会社{company}の{caller}と申します。"]
        if self.target is not None:
            return parts + [self.target.call()]
        if self.purpose_line:
            parts.append(self.purpose_line)
        parts.append(self.questions[0].text)
        return parts

    def replay_parts(self) -> dict:
        """名乗りの部分（flowctl.Flow の opening。部分 → 文）：流し直す録音と、目的の人に繋がったときに流す名乗り・用件の両方に使う。
        文は opening_parts と同じもの（録音も同じ）。目的の人のある業務では、呼ぶ一言（target）と、代わった方を確かめる一言（target_confirm）も。"""
        company, caller = self._said()
        parts = {"greeting": GREETING, "intro": f"株式会社{company}の{caller}と申します。", "purpose": self.purpose_line or ""}
        if self.target is not None:
            parts.update(target=self.target.call(), target_confirm=self.target.confirm())
        return parts




MAX_VARIANTS = 6  # 1つの文から作る、読みの候補ごとの録音の数の上限（読みが決まらない名前が2つ以上ある文でも、増えすぎないように）


def reading_variants(text: str, names: list) -> list[tuple[dict, str]]:
    """録音にする文（名乗り・用件・質問）の中の、読みが決まらない名前を、読みの候補ごとの文にする。依頼主ごとの録音で、人が聞いて
    正しい読みの録音を選ぶ（✅ 2026-10-16 ユーザー：「こちら側の担当者名は、依頼者ごとの録音時に録音しておくといいでしょう」）。
    選ばれた録音の読みが、その名前の読みになる（録音にした文は、録音のとおりに読まれるので、言い換えの決まり＝unread_rule は効かない。G-47）。
    戻り値：[(その文で読む読み {表記: 読み}, 文)]。読みが決まらない名前が無ければ [({}, text)]。まず言ってみる読みを先に並べる。"""
    from names import hira

    out = [({}, text)]
    for n in names:
        if not (isinstance(n, dict) and n.get("surface") and not n.get("reading")):
            continue
        mark = f"「{n['surface']}」"
        if mark not in text:
            continue
        cands = list(dict.fromkeys(hira(r) for r in [n.get("likely"), *(n.get("candidates") or [])] if r))
        if not cands:
            continue
        out = [({**m, n["surface"]: c}, t.replace(mark, f"「{c}」")) for m, t in out for c in cands]
    return out[:MAX_VARIANTS]


_OPEN, _CLOSE = "「『", "」』"


def quote_in_text(text: str, names: list) -> str:
    """台本などの文の中の名前を、「」でくくる（名前の位置にだけ付け、ほかの文字は1文字も変えない）。
    names＝名前の表記（文字）か、names.decide の名前（{"surface", "reading"}）。**読みが決まっている名前は、くくった中身を読み（ひらがな）に
    置き換える**（「大阪の「にっぽんばし」店」）。読みが決まらない名前（reading が空）は、表記のままくくる。もとからかっこの中にある名前も
    中身を読みにし、名前のすぐあとの読みがな（「（しょうじ）」）は外す。声に出す文（指示文・質問・録音の文）は、すべてここを通す（Business）。
    ✅ G-44（2026-10-16）：読み違えやすい名前12を、録音と同じ形で読ませた。漢字のまま「」でくくる形は24本中8本しか正しく読まず（「枚方」⇒
       まいかた・「中島」⇒なかじま（正しくは なかしま）・「東海林」⇒とうかいりん など）、ひらがなで渡す形は23本中21本（読めなかったと
       見えたのは「山崎」＝やまさき の2本だけ ⇒ ⛔ 2026-10-16 訂正：1本ずつ書き起こし直すと、2本とも「やまさき」と言えていた。まとめて
       書き起こしたため、隣の音声に引きずられていた＝23本中23本）。読みを指示に添える形は20本、文に「（読み）」を添える形は13本で、
       「とうかいりんしょうじ」と2回言った回もあった。カタカナで渡す形は「アオバ物流」を2本とも「おおば」と読んだ。
    - 長い名前から先に当てる（「川崎市中原区」と「中原区」が重なれば、長いほうを1回だけくくる）
    - 同じ名前が何回出ても、全部くくる
    - もともと「」『』の中にある所は、くくらない（二重にしない）
    - 全角と半角の違いは、そろえて当てる（文の書き方は変えない）
    - 別の言葉の一部には付けない：名前の頭と終わりが、どちらも単語の切れ目（jtext.word_edges）にあるときだけ当てる
      （「上野原」は1つの単語なので、中の「上野」には付けない。「株式会社ＡＢＣホールディングス営業部」は
       「株式／会社／ＡＢＣ／ホールディングス／営業／部」と分かれるので、「ＡＢＣホールディングス」に付けられる）
    ⚠️ 限界：単語に分ける処理が、1つの名前を2つに分けることがある（「神田川」＝「神田／川」）。名前の一覧に「神田」だけがあると、
       「「神田」川」になる。名前の一覧に長いほうの名前があれば、長いほうが先に当たる（取込の文字のモデルは「兵庫県三田市」のように
       まとまりで出す。G-42）。
    """
    import re
    import unicodedata

    from jtext import word_edges
    from names import hira

    readings = {}
    for n in names:
        if isinstance(n, dict) and n.get("surface"):
            readings[unicodedata.normalize("NFKC", n["surface"])] = n.get("reading") or ""
        elif isinstance(n, str) and n:
            readings.setdefault(unicodedata.normalize("NFKC", n), "")
    # もとから「」『』でくくってある名前（「大和ハウス工業」様）も、読みが決まっていれば中身を読みにする（かっこはそのまま）
    text = re.sub(r"([「『])([^「」『』]+)([」』])",
                  lambda m: m.group(1) + (readings.get(unicodedata.normalize("NFKC", m.group(2))) or m.group(2)) + m.group(3), text)
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
    for name in sorted(readings, key=len, reverse=True):
        start = 0
        while (i := normed.find(name, start)) != -1:
            j = i + len(name)
            start = i + 1
            if any(taken[i:j]) or i not in edges or j not in edges:
                continue
            spans.append((i, j, readings[name]))
            for k in range(i, j):
                taken[k] = True
    out, pos = [], 0
    for i, j, r in sorted(spans):
        out.append(text[pos:i] + quote(r or text[i:j]))
        pos = j
        # 名前のすぐあとの読みがな（「東海林（しょうじ）」の「（しょうじ）」）が読みと同じなら外す（そのまま読ませると2回言う）
        if r and (m := re.match(r"\s*[（(]\s*([ぁ-ゖァ-ヶー]+)\s*[）)]", text[j:])) and hira(m.group(1)) == r:
            pos = j + m.end()
    out.append(text[pos:])
    return "".join(out)


# 相手が考えている（「えー」「その」など）ときの決まり（1か所）。G-16：考えている相手に質問を聞き直した誤りから足した。
# ⛔ 2026-10-14 G-30〜G-32：前の「『ごゆっくりどうぞ』とだけ言って待つ」は、相手が「えーとですね、」のあと 0.7〜1秒あけると、
#    相手の続きの言葉にかぶった。「何も言わずに待つ」にしても、AIは黙らず「お待ちしております」や相手の言葉の真似を言った。
# ✅ 2026-10-14 ユーザー承認：短い「はい。」の相づちだけにする（少しかぶっても失礼になりにくい）。さらに間があいたときの一言
#    （phrases.SILENCE_PROMPT）は、電話接続側が流す（AIは時間を数えられない）
# ⚠️ 2026-10-14 ユーザー：S2・S4 の「えーとですね」のあとの「はい」が、物欲しそう・期待しているように聞こえる（L4 は問題なし）。
#    相づち風の「はい」に ⇒ 2つの形を G-36 で比べた：A＝言い方を足す（下）／B＝AIは黙って報告し、相づちはプログラムが録音で流す。
# ✅ 2026-10-14 ユーザー（G-36 を聞いて）：「これまでより、良くなりました」「形 A が良い」⇒ A にした（B の決まりと操作は外した）
THINKING_RULE = ("相手が考えている（「えー」「えーと」「その」など、まだ答え終わっていない）ときは、「はい。」とだけ、軽い相づちとして、"
                 "落ち着いた声で、語尾を上げずに短く言って、相手が話し終えるまで待ちます（相手の答えを期待するような言い方にしません）。"
                 "何も報告しません。相手の言葉を真似しません。質問を聞き直してはいけません。")


def system_rules(play_recordings: bool = False) -> str:
    """システム共通の会話の決まり（6-3 の材料2・3）。業務ごとに書かない。play_recordings：次の質問と締めを、電話接続側が録音で流す形（G-37）。"""
    return (
        "会話の決まり：\n" +
        ("- 返事に「プログラムが録音で流しました」とあれば、その言葉は相手にもう聞こえています。同じことを言わずに、何も言わないで、"
         "相手が話すのを待ちます。\n" if play_recordings else "") +
        # ✅ 2026-10-15 ユーザー：「（文章の）多くが、文語や強制的な冷たい言い回しが多いので気を付けて。言い方ひとつで、お客様の機嫌を損ねたり、
        #    機嫌を良くでき、契約にも影響する」（DESIGN.md 6-11）
        "- お客様には、命令の形（「〜してください」）や硬い言い方を使わず、「〜いただいてもよろしいでしょうか」「〜いただけますでしょうか」"
        "のように、柔らかく、同意をいただく言い方で話します。\n"
        # ⚠️ 2026-10-15 ユーザー（G-37 の2回目）：「『かしこまりました。3人で大丈夫でしょうか』が早回しのように聞こえる」（読点を外してから、
        #    言い直しの速さが S2 で約1割速くなった）・「『ありがとうございます。』が暗く怖い」
        "- 言い直しは、急がずに、落ち着いた速さで、はっきり言います。受けの言葉（「ありがとうございます。」「かしこまりました。」など）は、"
        "前向きに、心を込めて言います。\n"
        # ⛔ 2026-10-15：ここに「明るく」を入れていた。✅ 2026-10-12 ユーザー：「『明るく』や『ハキハキ』だと営業には不向きな強引そうな詐欺師の
        #    ような怪しいニュアンスになっているので、『前向き』とか別の表現を試してください」（g03_voices にも書いてある）。⇒ 外した。
        #    この決まりは test_offline.WordingTest が見張る
        "- 質問は、プログラムから渡されたものだけを、1つずつ聞きます。言い回しは自然にしてかまいませんが、意味を変えてはいけません。"
        "プログラムの返事に書かれていない質問や話題を、自分から出してはいけません（締めの前に「ほかに何かございますか」と聞くことも含みます）。\n"
        # ✅ 2026-10-14 ユーザー承認：答えが質問に合うかの判断は、プログラムの1か所（flowctl.form_problem）にまとめる。⛔ G-35：AIが「いつでも」を
        #    曖昧として報告し、「聞き取りにくかったので…」と聞き返した（相手ははっきり話している）
        "- 相手が答えたら、必ず record_answer を呼びます。聞き取れた答えは、いまの質問に合わないように思えても（ほかの質問の答え・"
        "「いつでも」など）、聞いたとおりの値で報告します。合うかどうかはプログラムが決めて、返事で知らせます（自分で聞き直してはいけません）。"
        "返事に次にすることが書いてあるので、それに従います。\n"
        f"- 相手が「わからない」と答えたら{ARG_LABEL['condition_id']}を「{SYSTEM_LABEL['unknown']}」に、答えたくないと言ったら"
        f"「{SYSTEM_LABEL['refused']}」にして record_answer を呼びます。"
        "答えたくないと言われた質問は、二度と聞きません。\n"
        # ✅ 2026-10-17 ユーザー：選択肢の質問の「どちらでも大丈夫です」は答え（「いつでも」と同じ）。⛔ G-14：合う選択肢が無く、AIは
        #    「わからない」「カード」「答えたくない」に分けた
        # ⛔ G-14 の続き：この決まりを足した1回目、「まだ決めてないです」を 6本中5本「どちらでも」にした ⇒ 任せた・まだ決めていない、を分けて書く
        f"- 選択肢から選ぶ質問で、相手が、どれを選んでもよいとこちらに任せたときだけ（「どちらでも大丈夫です」「どれでもいいです」など）、"
        f"{ARG_LABEL['condition_id']}を「{ANY_LABEL}」にして record_answer を呼びます。まだ決めていない・分からないとき（「まだ決めてないです」"
        f"「ちょっと分からないです」）は「{SYSTEM_LABEL['unknown']}」です。\n"
        "- 日付・時刻・数・金額・名前・電話番号・メール・住所は、返事に「言い直して」とあれば、返事に書いてある確認の言い方のとおりに言って、"
        "相手の返事を待ちます。相手が返事をしてから confirm_field で報告します（相手の返事を聞く前に報告してはいけません）。\n"
        # ✅ 2026-10-15 ユーザー：午前・午後の確かめは、AIからプログラムへ移す（営業時間から決める。flowctl.fill_ampm）。⛔ G-37：AIが「32 です」を
        #    時刻と思い込み、自分で午前・午後を聞いて、相手の言っていない「3時の午後」を報告した
        "- 時刻に午前・午後が無くても、聞いたとおりの値で報告します。午前か午後かは、プログラムが決めるか、聞き方を返事で知らせます"
        "（自分で聞いたり、自分で決めたりしてはいけません）。\n"
        "- 相手の答えが聞き取れない（何と言ったか分からない）ときだけ report_unclear を呼びます。返事が「聞き返す」のときは、返事に書いてある言い方のとおりに聞き返して、"
        "相手の答えを待ちます。この言い方だけを使い、言葉を足したり、言い換えたりしません。返事が「-にします」なら、聞き返さずに、返事に書いてある次の質問へ進みます。\n"
        f"- {THINKING_RULE}\n"
        # ✅ 2026-10-14 ユーザー決定：値を直されたとき、こちらの聞き違いか相手の言い間違いかは、プログラムが相手の言葉から決める
        #    （⛔ G-36：AIに選ばせると、12本中12本「相手が言い直した」を選んだ）⇒ AIには、どちらでも「違う値を言われた」で報告させる
        "- 言い直した値に相手が返事をしたら、record_answer ではなく confirm_field で報告します。合っていれば返事の種類「合っている」。"
        f"値を直されたら（こちらの聞き違いでも、相手の言い間違いでも）「{CONFIRM_LABEL['corrected']}」にして、直したあとの値を"
        f"「{ARG_LABEL['correction']}」に全部入れます（相手が一部だけ直したときは、直していない部分も足します。"
        "例：「15日の午後2時」に「いや、16日です」と言われたら「16日の午後2時」）。値を直されたら、自分で言い直さずに、すぐ報告します"
        "（言い直しの言い方は、プログラムの返事のとおりにします）。\n"
        # ⛔ 2026-10-14 G-36 の3回目：AIが、プログラムの言い方の前に、自分で「失礼いたしました。」を足した（録音の相づちと重なった）
        "- プログラムの返事に言い方が書いてあるときは、その言い方から話し始めます。前に「失礼いたしました」「ありがとうございます」"
        "「かしこまりました」などを足しません（相づち・お詫びは、プログラムが録音で流します）。\n"
        "- 返事に「念のための確認」とあれば、返事に書いてある言い方のとおりに、その質問をもう1回だけ聞きます（言い方を足したり変えたりしません）。\n"
        # かけ直しの申し出（DESIGN.md 6-2）：「かけ直して」と言われたときだけ。忙しいとだけ言われたときは request_end（引き止めるかは業務設定）
        "- 相手が、かけ直してほしいと言ったとき（話すべき方がいない「出かけております」、「携帯のほうにかけてもらえますか」、"
        f"「あとでかけてもらえますか」など）は、request_callback を呼びます（{ARG_LABEL['why']}は「{CALLBACK_LABEL['absent']}」"
        f"「{CALLBACK_LABEL['other_number']}」「{CALLBACK_LABEL['later']}」のどれか）。プログラムが、次に聞くこと（戻られる時刻など）を返事で渡します。"
        "かけ直すことは約束しません。\n"
        f"- 相手が急いでいる・切ろうとしているときは、request_end を{ARG_LABEL['reason']}「{END_LABEL['callee_hurry']}」で呼びます。"
        f"通話そのものを断られたら（「営業はお断りしています」「もうかけないで」など）「{END_LABEL['callee_refused_call']}」で呼びます。"
        # 怒り・苦情（Intent.md 9-4）：弁明や約束をせず、謝意を伝えて終える。人へ知らせる印は流れの制御が残す（Flow._note）
        f"相手が怒っている・苦情を言っているときは、言い訳・説明・約束をせずに、すぐ「{END_LABEL['callee_complaint']}」で呼びます"
        "（締めの言い方は、プログラムが返事で渡します）。\n"
        # 相手の種別（DESIGN.md 4-3・Intent.md 9-4）：人ではない相手には話さない。伝言を残すかは業務設定（流れの制御が返事で決める）
        "- 電話に出たのが人ではないとき（留守番電話の案内「ただいま電話に出ることができません」「発信音のあとに」・自動音声の案内"
        "「〜の方は1番を押してください」・ファクスの音）は、何も言わずに report_party で知らせます（伝言を残すかは、プログラムが返事で決めます）。"
        f"こちらがかけた先ではないと言われたとき（「うちは〇〇ではありません」「番号をお間違えでは」）も、report_party を「{PARTY_LABEL['wrong_party']}」"
        "で呼びます。\n"
        # ⛔ 2026-10-17 の見直し：前は「お礼を言って終えます」と書いていた＝AIが締めの文を自分で作り、「お気軽にお問い合わせください」
        #    「お気をつけてお越しくださいませ」（命令の形）を足した（G-45）。締めの言い方は流れの制御の1か所（flowctl.Flow._close）が決める
        "- 締めは、返事に書いてある締めの言い方のとおりに言って終えます。言葉を足しません。録音で流したとあれば、何も言いません。\n"
        # ⛔ 2026-10-17 の見直し：雑音で「はい、転送しました」と聞き違えたAIが、自分で「少々お待ちくださいませ」と言った（G-14。命令の形。
        #    ✅ 2026-10-15 ユーザー：「『少々お待ちください。』は、電話での接客では強制的で不向き」）。間のつなぎはプログラムの仕事（DESIGN.md 6-15）
        "- 相手にお待ちいただく言葉（「少々お待ちください」など）は、自分では言いません。間があいたときのつなぎの言葉は、プログラムが録音で流します。\n"
        # ⛔ G-47：相手の質問（「当日はどなたが担当されるんですか？」）が、10本中10本聞き流された（Intent.md 9-4「範囲外の質問」）
        "- 相手から質問されたら、聞き流さずに、目的と業務のことで分かる範囲だけを短く答えます。分からないことは作らず、"
        "「申し訳ございません、私の方では分かりかねます」のように答えます。\n"
        "- 「またお電話します」「改めてご連絡します」など、かけ直すことや連絡することを約束してはいけません（かけ直すかは、プログラムが決めます）。\n"
        f"- 相手が聞き取れなかったとき（「もう一度」「え？」「はい？」など）や、相手に不快な思いをさせたかもしれないとき（話がかぶった・待たせた・相手が困っている）は、"
        f"話し始めに「{APOLOGY[0]}」か「{APOLOGY[1]}」を付けてから話します。\n"
    )


def build(business: Business, voice_type: str, resume=None, opening_mode: str = "ai", play_recordings: bool = False) -> tuple[str, str]:
    """(声の名前, 指示文) を返す。

    resume：張り替え・つなぎ直しのとき、流れの制御（flowctl.Flow）を渡す。名乗りの代わりに「いまの位置」を書く（DESIGN.md 6-6）。
    opening_mode："ai"＝AIが名乗る／"recorded"＝名乗りと最初の質問は録音で流す（DESIGN.md 6-13 の案）。
    いまの位置は流れの制御が持っているので、AIが前のセッションを覚えていなくても続きから話せる。
    """
    voice, style_text = voice_style(voice_type)
    first = business.questions[0]
    parts = [
        "あなたは株式会社{}の{}として、電話をかけています。日本語で、丁寧に短く話します。".format(*business._said()),
        f"目的：{business.purpose}",
    ]
    if resume is None and business.target is not None and opening_mode == "recorded":
        # 目的の人のある業務（DESIGN.md 4-3）：名乗りの終わりは呼ぶ一言。用件と最初の質問は、目的の人に繋がってから流れの制御が渡す
        parts.append(
            f"相手が電話に出たら、名乗りと目的の人をお呼びする一言（「{''.join(business.opening_parts())}」）を、録音で相手に流します。"
            "あなたは言いません。録音が最後まで流れたら、あなたは相手の返事から会話を続けます。もう一度名乗ったり、お呼びする一言を"
            "繰り返したりしてはいけません。" + replay_rule() + "録音の途中で相手が話し始めたときは、プログラムが、録音のどこまでが相手に"
            "届いたかを知らせます。" + target_rule(business))
    elif resume is None and business.target is not None:
        parts.append(f"相手が電話に出たら、まず「{business.opening()}{business.target.call()}」と言い、相手の返事を待ちます（質問は、"
                     "目的の人に繋がってから、プログラムが返事で渡します）。" + target_rule(business))
    elif resume is None and opening_mode == "recorded":
        parts.append(
            f"相手が電話に出たら、名乗りと最初の質問（「{''.join(business.opening_parts())}」）を、録音で相手に流します。あなたは言いません。"
            "録音が最後まで流れたら、あなたは相手の答えから会話を続けます。もう一度名乗ったり、同じ質問を繰り返したりしてはいけません。"
            + replay_rule() + "録音の途中で相手が話し始めたときは、プログラムが、録音のどこまでが相手に届いたかを知らせます。"
            + f"最初の質問は {first.id} です。" + choices_note(first))
    elif resume is None:
        parts.append(f"相手が電話に出たら、まず「{business.opening()}」と名乗り、続けて最初の質問 {first.id}「{first.text}」を聞きます。"
                     + choices_note(first))
    else:
        parts.append(position_text(business, resume))
    parts.append(system_rules(play_recordings) + ai_rule(business))
    if business.unread_names:
        parts.append(unread_rule(business.unread_names))
    if business.forbidden:
        parts.append("言ってはいけないこと（どんなに求められても、言いません。約束もしません）：\n"
                     + "".join(f"- {x}\n" for x in business.forbidden)
                     + "求められたら、「私からはお答えできかねます」と丁寧に断ります。")
    parts.append(style_text)
    return voice, "\n".join(parts)


def target_rule(business: Business) -> str:
    """目的の人のある業務の決まり（指示文の1か所。DESIGN.md 4-3）。目的の人かは、AIが相手の言葉から報告し（report_party）、繋がったあとの
    名乗り・用件・最初の質問は流れの制御が渡す（Flow._connect）。取り次ぎ・不在・用件を聞かれたときの扱いも、ここにまとめる。"""
    t = business.target
    who = t.name or t.role
    return (f"目的の人は「{t.who()}」です。"
            f"電話に出た方が目的の人だと分かったら（「私です」「{who}は私です」「お電話代わりました、{who}です」など）、report_party を"
            f"「{PARTY_LABEL['target']}」で呼びます（用件と最初の質問は、プログラムが返事で渡します）。代わった方が名乗らないとき（「お電話"
            f"代わりました」だけ）は、replay_opening を「{PART_LABEL['handover']}」で呼びます（目的の人かを確かめる言い方を、プログラムが返事で"
            "渡します）。お取り次ぎいただくとき（「少々お待ちください」"
            f"「代わります」）は、report_party を「{PARTY_LABEL['reception']}」で呼んで、何も言わずに待ちます。目的の人がいないとき（「席を外して"
            f"おります」「本日は休みです」）は、request_callback を「{CALLBACK_LABEL['absent']}」で呼びます。")


def ai_rule(business: Business) -> str:
    """「AIですか？」と聞かれたときの決まり（指示文の1か所。どのセッションにも入る）。Intent.md 原則8：冒頭でAIだとは名乗らない
    （2026-10-12 ユーザー確定）が、人間だとは偽らない。⛔ 2026-10-18 の見直しで見つけた漏れ：前は指示文にこの決まりが無く、聞かれたときに
    AIが何と答えるかは決めていなかった（9-4 の「答える文面は業務設定で持つ」に届いていない）"""
    return (f"- 相手に「エーアイですか？」「機械ですか？」「人間ですか？」などと聞かれたら、人間だとは答えません。「{business.ai_answer or AI_ANSWER}」"
            "とだけ答えて、いまの質問を続けます（言い直しの返事を待っていたなら、その言い直しをもう一度言います）。")


def replay_rule() -> str:
    """名乗りを録音で流す通話で、聞き返されたときの決まり（指示文の1か所。最初のセッションと、張り替えのあとの「いまの位置」の両方で使う）。
    ✅ 2026-10-13 ユーザー：名乗り直しも録音で流す（「間が大丈夫ならOK」）。⛔ G-22・G-29：AIが自分で名乗り直すと、社名が正しく
    聞こえない（24本中、2通りの耳の両方で正しく聞こえたのは3本）⇒ AIは部分を選ぶだけ（flowctl.Flow._op_replay_opening）。
    何を流すか（お詫びの録音・質問の録音の有無・まだ届いていない部分）は場面で変わるので、ここには書かず、操作の返事が伝える。
    ⛔ 2026-10-17 の見直しで見つけた漏れ：前は最初のセッションの文にだけ書いていた＝張り替えのあとのAIは、この決まりを知らなかった。"""
    return ("名乗りや用件を聞き返されたら、自分では言わずに replay_opening を呼びます（プログラムが名乗りの録音を流します。そのあとに"
            f"することは、返事に書いてあります）。誰からの電話かを聞かれた（「どちら様ですか」など）＝「{PART_LABEL['intro']}」、用件を聞かれた"
            f"（「何のご用件ですか」など）＝「{PART_LABEL['purpose']}」、聞き取れなかった様子（「もう一度お願いします」「はい？」「え？」など）＝"
            f"「{PART_LABEL['all']}」、相手が別の人に代わった（「お電話代わりました」）＝「{PART_LABEL['handover']}」。")


def unread_rule(names: list[str]) -> str:
    """こちら側の、読みが決まらない名前の扱い（指示文の1か所）。✅ 2026-10-16 ユーザー：「なかじまとなかしまなどは人が見ても判断できません。
    それは会話の中で失礼のないように語るか、場合によっては聞き出す」。⇒ こちら側の名前は、相手に聞いても分からないので、声に出さずに言い換える。
    お客様側の名前（目的の人）は、ここには入れない（まず言ってみる読みで呼び、つながってから確かめる＝flowctl.reading_question）。
    ⛔ 見直しで直した：前は「お客様側の名前は確かめるまで使わない」とも書いていた＝承認いただいた形（言ってみる読みで呼ぶ）と食い違っていた。"""
    shown = "".join(quote(n) for n in names)
    return (f"こちら側の人の名前のうち、{shown}は読み方が決まっていません。この名前は声に出さずに、「担当の者」のように言い換えて話します。"
            "読み方を自分で決めて言ってはいけません。")


def position_text(business: Business, flow) -> str:
    """張り替えのときに渡す「いまの位置」（DESIGN.md 6-6）。名乗りはもう済んでいるので、繰り返させない。"""
    done = []
    for q in business.questions:
        it = flow.items[q.id]
        if it.state in GOT:
            # 答えは依頼主へ渡すものと同じ（flow.answer_of）。⛔ 2026-10-17 の見直しで見つけた漏れ：選択肢の質問でも相手の言葉（「大丈夫です」）や、
            #    中の名前（英字の any）をそのまま見せていた
            done.append(f"{q.id}「{q.text}」⇒ {flow.answer_of(q.id)}（済み）")
        elif it.state == "dont_know":
            # ⛔ 直す前は「取れた」と同じ行で、選択肢の中の名前（英字の unknown）をそのままAIに見せていた
            done.append(f"{q.id}「{q.text}」⇒ わからないと言われた（聞き直さない）")
        elif it.state == "refused":
            done.append(f"{q.id}「{q.text}」⇒ 答えたくないと言われた（二度と聞かない）")
    lines = ["これは通話の続きです。回線の都合で、つなぎ直しました。名乗りはもう済んでいます。もう一度名乗ってはいけません。"
             + (replay_rule() if flow.opening else "")]
    if flow.phase == "calling":
        # 目的の人に繋がる前（DESIGN.md 4-3）：いま求めていることは流れの制御の1か所（Flow._now）から
        lines.append(flow._now() + target_rule(business))
    if done:
        lines.append("ここまでに聞けたこと：" + "／".join(done))
    if flow.awaiting_confirm:
        # 言い直しの言い方は流れの制御が持っている（awaiting_say）。⛔ 2026-10-17 の見直しで見つけた漏れ：値そのものを渡していたので、
        #    選択肢の確かめ（「念のため…ということでよろしいでしょうか？」）では、相手の言葉（「大丈夫です」）を言い直す形になっていた
        lines.append(f"いまは {flow.awaiting_confirm} を「{flow.awaiting_say}」と確かめて、相手の返事を待っているところです。"
                     + flow._confirm_how(flow.awaiting_confirm))
    elif flow.current:
        # ⛔ 2026-10-18 の見直しで見つけた漏れ：業務の質問の一覧から探していた＝プログラムが足した質問（かけ直しの C1 など）が
        #    いまの質問のときに、張り替えで落ちる形だった ⇒ 流れの制御が持つ質問から取る
        q = flow.questions[flow.current]
        lines.append(f"次に聞くのは {q.id}「{q.text}」です。相手が話したら、それに答えてから、この質問を聞きます。" + choices_note(q))
    return "\n".join(lines)


def barge_in_note(business: Business, done: list[str], rest: list[str]) -> str:
    """名乗りの録音の途中で相手が話し始めて止めたとき、AIへ知らせる文（DESIGN.md 6-13 の案。文面はここだけ）。"""
    heard = "".join(done) if done else "（まだ何も届いていません）"
    # 名乗り・用件を伝え直すときは、自分で言わずに replay_opening（録音。頭の「失礼いたしました。」もプログラムが流す）。⛔ G-29：AIが自分で
    # 名乗り直した社名は、6本中、2通りの耳の両方で正しく聞こえたのは0本
    return (f"［プログラムからのお知らせ］名乗りの録音は「{heard}」まで相手に届いたところで、相手が話し始めたので止めました。"
            f"まだ相手に伝えていないこと：「{''.join(rest)}」。"
            "相手の話に答えるのに名乗りや用件が要るとき（「どちら様？」「何でしょう？」など）は、自分では言わずに replay_opening を呼びます"
            # まだ届いていない名乗り・用件は、流れの制御が足して流す（Flow.on_opening_cut。⛔ G-50：AIは「名乗り」だけを選び、用件が届かなかった）
            f"（誰かを聞かれた＝「{PART_LABEL['intro']}」、用件を聞かれた＝「{PART_LABEL['purpose']}」、聞き取れなかった様子＝「{PART_LABEL['all']}」。"
            "まだ届いていない名乗り・用件は、プログラムが一緒に流します）。"
            f"相手がもう質問に答えているときは、そのまま答えを報告します。相手に届いたことは、繰り返さなくてかまいません。"
            f"相手の話にかぶせてしまったので、自分で話すときは、話し始めに「{APOLOGY[0]}」を添えます（replay_opening のときは、プログラムが流します）。")

