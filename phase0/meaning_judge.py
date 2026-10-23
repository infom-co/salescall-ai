"""相手の言葉の2つ目の判定（DESIGN.md 6-10 の (6)。G-53）：相手の言葉が何に当たるかを、会話のAIとは別の文字のAIが決める。
流れの制御（Flow.on_meaning）は、会話のAIの報告と比べて、食い違えばそのまま受け取らずに確かめる。耳の聞き違いを別の耳で確かめる
（6-10 の (5)）のと同じ形で、意味の取り違えを、別の判定で確かめる。1つの判定で、次の3つの場面を受け持つ（場面で選べるものが変わるだけ）：
- 質問への答え：質問の選択肢（選択肢の質問）／「答えた」（値を聞く質問）・どちらでも・わからない・答えたくない
- 言い直しへの返事（asked＝こちらが言い直した言葉）：選択肢の質問は、相手がどの意味を認めたか（「はい」でも、言い直した意味の選択肢）。
  値を聞く質問は「合っている」か「違う値を言われた」か
- 答えでない言葉（どの場面でも）：取り次ぎを待つように言われた・代わった人が話し始めた・決められない

⛔ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
   ⇒ 記録の通話で、会話のAIが意味を取り違えたまま、流れの制御が確かめずに受け取った答えが12回あった（「まだ決めてないです」⇒
   「どちらでも」5回・「どちらでも大丈夫です」⇒「カード」など5回・「いや、これからなんです」「家まだです」⇒「わからない」2回）。
   どれも聞き取りは正しく、別の耳では拾えない（私は「別の耳では直せません」と書いて、そのまま流していた）。
- 判定に渡すのは、質問の文・こちらが言い直した言葉（言い直しへの返事のとき）・選べるもの・相手の言葉だけ。
  会話のAIの選んだ答えは渡さない（引きずられないように＝独立した判定）。
- 選択肢は、**意味の言葉を名前にして**見せる（取込で決めた Question.meanings。無ければ選択肢の名前）。
  ⛔ 2026-10-18 G-53 の2回目：選択肢の名前（「はい」「いいえ」）で見せていたら、「念のためご予約の日時に変更がおありということでよろしいでしょうか？」
     への「違います」を「いいえ」（＝変更がおあり）と読んだ（2回）＝言い直しを認めていないのに、認めたと読む向き（誤った答えが渡る向き）。
     「違います」と、名前の「いいえ」を、言葉で結びつけた。
"""
from __future__ import annotations

import json

from flowctl import ANY, CHOICE_TYPES, CONFIRM_LABEL, HANDOVER, HOLD, UNDECIDED, label

SYSTEM_DESC = {ANY: "どれでもよいと、こちらに任された（「どちらでも」「どれでも」）",
               "unknown": "わからない・覚えていない・まだ決めていない（選ぶ前の状態）",
               "refused": "答えたくないと、答えを拒んだ"}
KIND_DESC = {HOLD: "ほかの人に電話を代わる・取り次ぐので、待つように言った（答えではない）",
             # ⛔ 2026-10-21 G-59（3.5 Flash Lite・前の言葉つき）：「お電話代わりました。山本です。」を3回中1回「ご本人」と読んだ ⇒ 違う名前は
             #    こちら、と書き足した（書き足したあとは3回とも正しい）。（「担当の者に代わります」のあとの「ご本人」は誤りではなかった＝✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）。
             #    「お待たせいたしました。」だけを取り次ぎと読む誤りは、説明を書き足しても直らなかったので外した＝繋がない側の誤り）
             # ⛔ 2026-10-19 G-57・G-59：「お電話代わりました。岡田です。」（お呼びした方の名前を名乗った）を、いまの判定は6回とも、こちらに読んだ
             # ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、
             #    『ご本人』になりますよね」⇒ お呼びした方に取り次いだあとなら、名乗らなくても答えが決まる（その前に相手が言ったこと＝BEFORE）
             # ⚠️ 同じ日のユーザー（2回目）：「取次後は必ずしも、本人とは限らない」⇒ ここは「代わった別の人が話し始めた」ときだけの話。取り次いだ方が
             #    また出て出られないと伝えたときは不在、どちらが出たか分からない言葉はご本人と決めない（決まりの正本＝flowctl.TARGET_MEANINGS）
             HANDOVER: "電話を代わった別の人が、話し始めた（「お電話代わりました」など。答えではない）。お呼びした方がいないので別の者に代わると"
                       "言ったあとに代わった方が出たとき・お呼びした方と違う名前を名乗ったときも、こちら。代わった方が誰かで答えが決まるとき"
                       "（お呼びした方の名前を名乗った・その前に取り次いだ方が、お呼びした方に取り次いだ）は、こちらではなく、その答えを選ぶ",
             UNDECIDED: "受け入れにも断りにも取れて、この言葉だけでは決められない（「大丈夫です」「結構です」だけなど）・どの答えにも寄っていない"
                        "（迷っている・どちらとも言っていない）・答えになっていない"}
CONFIRM_DESC = {"affirmed": "こちらが言い直した値で合っていると認めた", "corrected": "違う・直した（別の値を言った）"}

# 言い切っているか（✅ 2026-10-18 ユーザー：「『急ぎと言えば急ぎですが』『まあ、できれば早くとは思います』『さらっとは見たんですが』
#    『していないはずですが…』『5万円くらいかなとは思いますが』なんかは、『yes,but 曖昧』のような返答にしておけばいいんじゃないですか？」）。
#    寄っている答えは選び、言い切っていないことは別に答えさせる（それまでは「決められない」に入れていた＝寄っている向きが消えていた）。
#    流れの制御は、最初の答えなら言い直して確かめ、言い直しへの返事なら、その答えに「曖昧」の印を付けて受け取る（Flow._hedged）
FIRM = "firm"
# ⛔ 2026-10-18 の1回目：例に「〜くらい」「〜程度」を入れ、「〜と思うのですが」と「〜と思います」を分けなかった ⇒ 最初の答えの「30名くらいです」
#    「20名ほどおります」「来月以降になると思います」まで言い切っていないと読み、要らない確かめが約80個増えた（Sonnet 5.5・Haiku 5.5 の考える形。
#    「と思います」は言い切りの柔らかい形＝✅ 2026-10-17 ユーザー確定の言い切っていない答えの一覧（flowctl.UNCERTAIN）にも入れていない）
# 言い切っていない・言い切っている、の例（Claude・Gemini の決まり FIRM_RULE と、Jev の定義 JEV_FIRM の両方を、ここから作る＝1か所）
FIRM_NOT = "「たぶん」「〜はず」「〜かもしれない」「〜とは思うのですが」"
FIRM_YES = ("望みをはっきり言っている・「〜と思います」と柔らかく言い切っている・数がおよそ", "（「〜ほど」「〜くらい」）", "でも選ぶ答えがはっきりしている")
FIRM_RULE = ("言い切っていなくても（" + FIRM_NOT + "のように、含みを残す言い方）、どれかの答えに寄っていればその答えを選び、" + FIRM +
             " を false にします。言い切っていれば true です（" + "".join(FIRM_YES) + "、なら true）。")


class Reading(str):
    """判定の答え（中の名前のまま比べられる）に、お客様が言い切っているか（firm）を添えたもの。sure＝判定の確からしさ（返す判定だけ＝Jev）・
    by＝答えた判定の名前・tried＝その前に問い合わせて答えが無かった判定（JudgePanel が付ける）。"""
    sure: float | None = None
    by: str | None = None
    tried: tuple = ()

    def __new__(cls, cond: str, firm: bool = True, sure: float | None = None):
        r = super().__new__(cls, cond)
        r.firm, r.sure = bool(firm), sure
        return r


# 判定の決まり（どの会社の判定にも同じ文で渡す。Jev のように、決まり・場面・選べるものを別々に受け取る判定には、この部分を問いとして渡す）
RULES = (
    "電話でお客様に質問をして、お客様が話しました。お客様の言葉が、次の選べるもののどれに当たるかを、1つだけ選んでください。"
    "あわせて、お客様がその答えを言い切っているか（" + FIRM + "）も答えてください。\n"
    "決まり：\n"
    "- 言葉の意味で選びます。言葉の頭の「はい」「いいえ」だけで決めません（「いいえ、もう済ませてあります」は、済ませた側）。\n"
    "- 打ち消しを重ねた言い方は、意味で読みます（「していないわけではない」は、している側）。\n"
    "- 「〜ございませんでしょうか？」のような打ち消しの問いへの「はい」「いいえ」は、選べるものの名前（意味）で、どちらの意味かを決めます。\n"
    "- こちらが答えを言い直して確かめたあとの返事は、お客様が認めた意味で選びます（言い直した意味を「はい」と認めたら、その意味）。\n"
    # ⚠️ 例は、試験の言葉（G-14・G-52）と同じ言い方にしない（同じだと、試験で測った正しさが高く出る）
    # ⛔ 2026-10-18：言い切っているかを足した形で、Claude Sonnet 5.5（考える）が、打ち消しの言い直し（「〜ではないということで…」）への
    #    打ち消しの返事を、言い直した意味のまま読んだ（2回。受け取ると誤った値が渡る向き）⇒ 打ち消しの返事の決まりを足した。言い直しへの
    #    「違うだけ」「認める」の返事 552個で、危ない読み 2 ⇒ 0・正しい 539 ⇒ 551（⚠️ 同じ答えで決まりを作って測った＝高めに出うる）
    "- 言い直しを打ち消す返事（違う・そうではない、と言う）は、言い直した意味を打ち消した側を選びます（言い直したのが「〜ではない」の意味なら、"
    "「〜である」の側。値を言い直したときは「" + CONFIRM_LABEL["corrected"] + "」）。\n"
    "- " + FIRM_RULE + "\n"
    "- 迷ったら「" + UNDECIDED + "」を選びます。\n"
)
PROMPT = RULES + (
    "質問：「{question}」\n"
    "{before}"
    "{asked}"
    "選べるもの（名前：説明）：\n{choices}\n"
    "お客様の言葉：「{text}」\n"
)


# ⛔ 2026-10-18 取り下げた決まりの案：「言い切っていない返事（『〜ですが…』『できれば〜』『〜かな』『〜はず』『〜くらい』）は決められない」。
#    ユーザー：「『メールのほうが助かりますが、電話でも大丈夫です』なら、メールは優先でOK…決まらないではないですよね」＝望みが言えていれば答え。
#    案は逆の向きだった（測った「直った」は、作らせたモデルの誤った正解に合わせただけのものを含む。PHASE0 G-53 の3回目）。


def options(q, asked: str | None = None) -> list[tuple[str, str, str]]:
    """選べるもの（判定に見せる名前、説明、中の名前）。選択肢の質問と、値を聞く質問の最初の答えは Question.all_conditions の並び
    （宣言した選択肢は、意味の言葉を名前にする。無い・ほかの名前とかぶるときは選択肢の名前）。値を聞く質問の言い直しへの返事は
    「合っている」「違う値を言われた」（AIの報告 confirm_field と同じ名前）。最後に、答えでない言葉の3つ。"""
    if q.type in CHOICE_TYPES or asked is None:
        taken = {label(c) for c in q.all_conditions if c not in q.conditions} | set(KIND_DESC)
        head = []
        for c in q.all_conditions:
            if c in q.conditions:
                name = q.meanings.get(c)
                if not name or name in taken:
                    name = label(c)
                taken.add(name)
                head.append((name, "この意味の答え" if name != label(c) else f"「{c}」と答えた", c))
            else:
                head.append((label(c), SYSTEM_DESC[c], c))
    else:
        head = [(CONFIRM_LABEL[k], d, k) for k, d in CONFIRM_DESC.items()]
    return head + [(k, d, k) for k, d in KIND_DESC.items()]


BEFORE = "その前に相手が言ったこと"  # 目的の人を呼んでいる段だけ（Flow.judge_context。取り次いだ方の言葉で、代わった方が誰かが決まる）


def scene(q, text: str, asked: str | None = None, before: str | None = None) -> dict:
    """判定に渡す場面（質問の文・その前に相手が言ったこと・こちらが言い直して確かめた言葉・お客様の言葉）。prompt はこれを文にしたもの。"""
    return ({"質問": q.text} | ({BEFORE: before} if before else {}) | ({"こちらが言い直して確かめた言葉": asked} if asked else {})
            | {"お客様の言葉": text})


def prompt(q, text: str, asked: str | None = None, before: str | None = None) -> str:
    said = f"こちらが言い直して確かめた言葉：「{asked}」\n" if asked else ""
    return PROMPT.format(question=q.text, asked=said, text=text, before=f"{BEFORE}：「{before}」\n" if before else "",
                         choices="\n".join(f"- {n}：{d}" for n, d, _ in options(q, asked)))


def _config(q, asked: str | None):
    from google.genai import types

    names = [n for n, _, _ in options(q, asked)]
    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=names),
                                                              FIRM: types.Schema(type=types.Type.BOOLEAN)}, required=["choice", FIRM])
    # ⚠️ 考える量（thinking_budget）は指定しない：3.5 Flash Lite は指定すると断る（400）。3.8 Flash は0にしても約3.3秒（2026-10-17 に測った）
    return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)


def read(q, text: str | None, asked: str | None = None) -> Reading | None:
    """判定の返事から、中の名前（選択肢・any・unknown・refused／affirmed・corrected／HOLD・HANDOVER・UNDECIDED）と、言い切っているか。
    読めなければ None。言い切っているかが無い・読めない返事は、言い切っている（印を付けない＝ほかの確かめのとおり）。"""
    try:
        got = json.loads(text or "{}")
        name, firm, sure = got.get("choice"), got.get(FIRM), got.get("sure")
    except (ValueError, AttributeError):
        return None
    cond = next((c for n, _, c in options(q, asked) if n == name), None)
    return None if cond is None else Reading(cond, firm is not False, sure if isinstance(sure, (int, float)) else None)


def _schema(q, asked: str | None) -> dict:
    """選べるものの名前だけを返させる形（Gemini は _config・ほかの会社はこの形をそのまま渡す）。"""
    return {"type": "object", "properties": {"choice": {"type": "string", "enum": [n for n, _, _ in options(q, asked)]},
                                             FIRM: {"type": "boolean"}},
            "required": ["choice", FIRM], "additionalProperties": False}


def judge(client, model: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """相手の言葉 text が、質問 q（asked＝こちらが言い直した言葉・before＝その前に相手が言ったこと）のどれに当たるか。読めなければ None。"""
    r = client.models.generate_content(model=model, contents=prompt(q, text, asked, before), config=_config(q, asked))
    return read(q, r.text, asked)


async def ajudge(client, model: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """judge と同じ（通話の中で、会話のAIの報告を待つ間に走らせる形）。"""
    r = await client.aio.models.generate_content(model=model, contents=prompt(q, text, asked, before), config=_config(q, asked))
    return read(q, r.text, asked)


JUDGE_MODELS = ("3.5 Flash Lite", "3.6 Flash", "3.7 Flash", "3.8 Flash")   # 判定に使える文字のモデル（表示名。g23_names.find_models でIDを引く）

# ── Google 以外の判定（DESIGN.md 1-3 の (2)・選んだ根拠は docs/JUDGE_SETUP.md）。同じ問い合わせの文・同じ選べるもの・同じ読み方（read）で、
#    送り方だけが会社ごとに違う。
#    ✅ 2026-10-18 ユーザー：「ジェミニより精度が悪いんだったら意味がないでしょ。ジェミニよりほぼ確実に精度がいいものを抜粋しないと時間の無駄」
#       「最新モデルで評価しないとダメだよ」⇒ 各社の最新の、速くて安いモデルのうち、公開の数字で 3.5 Flash Lite を上回るものだけ
#    ⚠️ キーの名前は ANTHROPIC_API_KEY にしない（この作業の場所そのものが読む名前と同じになる）
OTHER_JUDGES = {
    "Claude Haiku 5.5": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com"},
    # 考える段を入れた形（2026-10-18：考えない形は「違います」を、言い直した意味を認めたと読む向きに 47 回誤った）
    "Claude Haiku 5.5（考える）": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com",
                                 "think": "low"},
    # Sonnet 5.5（2026-10-18 ユーザー：「Sonnet5.5でも試した方がいい」）：名前を指定した道具の呼び出しは断る（400）ので、返事の形を決めて
    # JSON で返させる。考えない形は {"type": "between_tools"}（道具が無いので考える段が無い）
    "Claude Sonnet 5.5": {"maker": "anthropic", "model": "claude-sonnet-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com",
                          "form": "json", "effort": "low", "thinking": {"type": "between_tools"}},
    "Claude Sonnet 5.5（考える）": {"maker": "anthropic", "model": "claude-sonnet-5-5", "key": "ANTHROPIC_JUDGE_KEY",
                                  "host": "api.anthropic.com", "form": "json", "effort": "low", "thinking": {"type": "adaptive"}},
    "GPT-6 Luna": {"maker": "openai", "model": "gpt-6-luna", "key": "OPENAI_JUDGE_KEY", "host": "api.openai.com"},
    # 同じ GPT-6 Luna を、設定済みの OpenRouter のキーで（2026-10-23。OpenAI のキーが作業環境に無いため）。送り方は OpenAI と同じ形
    # （OpenRouter の Responses の窓口）。⚠️ 文の行き先は OpenRouter（米国）を通って OpenAI（米国）。考える量は「なし」（中身の判定の
    # 3文で、low は 3.3〜4.4秒・minimal は 3.0〜3.8秒＝2秒の待ちに入らない。none は 1.1〜1.6秒）。⛔ G-63（`G-63-luna_20261023_094345.json`）：
    # 機械の確かめと合わせても危ない文を通した（「当日の担当者につきましては、担当の者よりお話しさせていただきます。」）＝中身の判定に使わない。
    # 新しいアカウントは1分に20回まで（答えが返ったのは168回中40回）
    "GPT-6 Luna（OpenRouter・考えない）": {"maker": "openai", "model": "openai/gpt-6-luna", "key": "OPENROUTER_JUDGE_KEY",
                                       "host": "openrouter.ai", "path": "/api/v1/responses", "effort": "none"},
    # Jev（TypeSafe の、選ぶ・分けるだけの専用モデル。2026-10-18 ユーザー：「判断のみなら、Jevが安くて優秀だと聞きましたが、使えないんですか？」）。
    # 直接の窓口は順番待ちなので OpenRouter を通す。最新の版を名前で決めて測る（「最新」の別名だと、測ったあとに中身が替わる）
    # 線（2026-10-18 G-53 の「曖昧の印と Jev」。線は正解の付いた答えの半分で決め、残りの半分で数えた）：
    # sure_min＝答えにする確からしさ（Jev が受け持って誤った数が 0.95 から0。下は答えにしない＝もう1つの判定で決める）・
    # firm_min＝言い切っているとする確からしさ（0.5 では要らない「曖昧」の印が53個、0.2 では5個）
    # ground_min＝中身の判定（grounding）で読みにする確からしさ（2026-10-23 G-63：Jev が外したのは5回とも 0.48 以下・正しく通したのは 0.51 以上
    # ＝線 0.8 で危ない文 0・Jev が決めるのは 76%。⏳ 中身の判定に Jev を入れるか（GROUNDERS）は、ユーザーのご判断を待つ）
    "Jev 1.14": {"maker": "typesafe", "model": "typesafe/jev-1.14", "key": "OPENROUTER_JUDGE_KEY", "host": "openrouter.ai",
                 "sure_min": 0.95, "firm_min": 0.2, "ground_min": 0.8},
}
# 2つ目の判定（convo.check_handler の judge2。DESIGN.md 1-3 の (2)）：✅ 2026-10-18 ユーザー：「クレジットの使い残しはもったいないので、当面は、
#    Sonnetだけを使いましょう。Jev検証時を除く」（Max の月の API の分は繰り越さない）⇒ Jev との振り分け（jev_try.py）は検証のときだけ
JUDGE2 = "Claude Sonnet 5.5（考える）"
OTHER_PATH = {"openai": "/v1/responses", "typesafe": "/api/v1/systemone"}  # 部品を使わず httpx で送る会社の窓口


def other_path(name: str) -> str:
    """httpx で送る判定の窓口（同じ会社の形を、別の接続先から使うときは、その判定の path）。"""
    j = OTHER_JUDGES[name]
    return j.get("path") or OTHER_PATH[j["maker"]]
OPENAI_EFFORT = "low"  # 考える量（速さの公開の数字は low で約1.5秒。高くすると十数秒〜）


class OtherJudgeError(RuntimeError):
    """Google 以外の判定の失敗（status＝返事の番号・retry＝待つように言われた秒）。上限（429）・混雑（5xx）は呼ぶ側がやり直す。"""

    def __init__(self, status: int | None, message: str, retry: float | None = None):
        super().__init__(f"{status} {message}")
        self.status, self.retry = status, retry


def other_request(name: str, q, text: str, asked: str | None, before: str | None = None) -> dict:
    """Google 以外の判定への問い合わせの中身（会社ごとの形。問い合わせの文と選べるものは Gemini と同じ）。"""
    j, p = OTHER_JUDGES[name], prompt(q, text, asked, before)
    if j["maker"] == "anthropic":
        # ⚠️ Claude Haiku 5.5 は temperature などを送ると断る（1以外は400＝2026-10-18 に当たった）。名前を指定した道具の呼び出しは、
        #    考える段を飛ばして道具の呼び出しから返す（いちばん速い形）。strict＝選べるもの以外の名前を返させない
        body = {"model": j["model"], "max_tokens": 256, "messages": [{"role": "user", "content": p}],
                "tools": [{"name": "answer", "description": "お客様の言葉に当たるものの名前", "input_schema": _schema(q, asked),
                           "strict": True}],
                "tool_choice": {"type": "tool", "name": "answer"}}
        if j.get("think"):  # 名前を指定した呼び出しは考える段を飛ばすので、道具は任せ、考える量を決め、道具で答えるように言う
            body |= {"max_tokens": 4096, "tool_choice": {"type": "auto"}, "output_config": {"effort": j["think"]},
                     "system": "選んだものは、必ず道具 answer で返してください。"}
        if j.get("form") == "json":  # 道具を使わず、返事の形（選べるものの名前だけ）を決めて返させる
            # ⚠️ 断られたときに別のモデルへ回す仕組み（fallbacks）は入れない：測っているモデルの答えに、別のモデルの答えが混ざるため
            #    （断られたら読めない返事＝None として数える）
            body = {"model": j["model"], "max_tokens": 4096, "messages": [{"role": "user", "content": p}], "thinking": j["thinking"],
                    "output_config": {"effort": j["effort"], "format": {"type": "json_schema", "schema": _schema(q, asked)}}}
        return body
    if j["maker"] == "typesafe":
        return jev_request(j["model"], q, text, asked, before)
    return {"model": j["model"], "input": p, "reasoning": {"effort": j.get("effort", OPENAI_EFFORT)}, "max_output_tokens": 2000,
            "text": {"format": {"type": "json_schema", "name": "answer", "schema": _schema(q, asked), "strict": True}}}


# ── Jev（判断だけの専用モデル）への問い方。⛔ 2026-10-18 の1回目：チャットのAI向けの長い決まりの文（RULES）を、そのまま問い（instructions）に
#    渡し、選択肢の定義を「この意味の答え」としか書かず、答えの確からしさを捨てていた（ユーザー：「Jevの使い方は理解できていますか？」）。
#    公式の型（OpenRouter の Jev の手引き）：問いは短く1つ・選択肢の意味は定義（criteria）に書く・確からしさで線を決める（線は正解の付いた答えで）。
#    決まりの中身は RULES・FIRM_RULE と同じ（言い直しへの返事は、認めたら言い直した意味・打ち消したら打ち消した側・寄っていればその答え）
JEV_ASK = "お客様の言葉は、質問へのどの答えに当たりますか？"
JEV_ASK_REPLY = ("お客様の言葉は、こちらが言い直して確かめた言葉への返事として、どの答えに当たりますか？"
                 "（言い直した意味を認めたら、その意味。言い直しを打ち消したら、打ち消した側）")
JEV_FIRM = {"true": "答えを言い切っている（" + FIRM_YES[0] + FIRM_YES[2] + "）",
            "false": "答えに寄っているが、" + FIRM_NOT + "のように含みを残している"}


def jev_criteria(q, asked: str | None = None) -> dict:
    """Jev に渡す選択肢の定義（名前は options と同じ。答えの意味の選択肢は、意味で読むことを定義に書く）。"""
    out = {}
    for n, d, c in options(q, asked):
        if d == "この意味の答え" or d.startswith("「"):
            d = (f"お客様の答えの意味が「{n}」（打ち消しを重ねた言い方・打ち消しの問いへの「はい」「いいえ」も、言葉の頭ではなく意味で読む。"
                 "言い切っていなくても、この意味に寄っていればこれ）")
        out[n] = d
    return out


def jev_request(model: str, q, text: str, asked: str | None = None, before: str | None = None) -> dict:
    """Jev への問い合わせ：どの答えか（choice・確からしさ付き）と、言い切っているか（noul）。"""
    return {"model": model, "state": scene(q, text, asked, before),
            "questions": {"choice": {"type": "choice", "instructions": JEV_ASK_REPLY if asked else JEV_ASK, "criteria": jev_criteria(q, asked)},
                          FIRM: {"type": "noul", "instructions": "お客様は、その答えを言い切っていますか？", "criteria": JEV_FIRM}}}


def other_text(name: str, data: dict) -> str | None:
    """Google 以外の判定の返事から、read に渡す文字（{"choice": 名前}）。"""
    if OTHER_JUDGES[name]["maker"] == "anthropic":
        if OTHER_JUDGES[name].get("form") == "json":
            return next((c.get("text") for c in data.get("content") or [] if c.get("type") == "text"), None)
        got = next((c.get("input") for c in data.get("content") or [] if c.get("type") == "tool_use"), None)
        return json.dumps(got, ensure_ascii=False) if got else None
    if OTHER_JUDGES[name]["maker"] == "typesafe":  # 言い切っているかは確からしさ（0〜1）で返る：線（firm_min）から上なら言い切っている
        # ⛔ 2026-10-19 の見直し：ここは半分（0.5）で切っていた（測って合った線は0.2）。答えの確からしさ（sure）も捨てていた
        answers = data.get("answers") or {}
        choice = answers.get("choice") or {}
        got, firm = choice.get("choice"), (answers.get(FIRM) or {}).get("noul")
        return json.dumps({"choice": got, FIRM: firm is None or firm >= OTHER_JUDGES[name]["firm_min"], "sure": choice.get("confidence")},
                          ensure_ascii=False) if got else None
    return next((c.get("text") for o in data.get("output") or [] for c in o.get("content") or [] if c.get("type") == "output_text"), None)


def other_client(name: str, key: str, aio: bool = False):
    """Google 以外の判定へ送る道具（Anthropic は公式の部品・OpenAI は httpx）。やり直しは呼ぶ側の1か所でするので、部品では繰り返さない。"""
    if OTHER_JUDGES[name]["maker"] == "anthropic":
        import anthropic

        return (anthropic.AsyncAnthropic if aio else anthropic.Anthropic)(api_key=key, max_retries=0, timeout=60)
    import httpx

    return (httpx.AsyncClient if aio else httpx.Client)(base_url=f"https://{OTHER_JUDGES[name]['host']}", timeout=60,
                                                        headers={"authorization": f"Bearer {key}"})


def _other_error(exc: BaseException) -> OtherJudgeError | None:
    """部品ごとの失敗を、1つの形（OtherJudgeError）にそろえる。ほかの失敗は None（そのまま上げる）。"""
    import anthropic
    import httpx

    if isinstance(exc, anthropic.APIStatusError):
        r = exc.response
        return OtherJudgeError(exc.status_code, str(exc.message)[:300], float(r.headers.get("retry-after") or 0) or None)
    if isinstance(exc, httpx.HTTPStatusError):
        r = exc.response
        return OtherJudgeError(r.status_code, r.text[:300], float(r.headers.get("retry-after") or 0) or None)
    return None


def judge_other(client, name: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """judge と同じ（Google 以外の判定。client は other_client で作る）。失敗は OtherJudgeError。"""
    body = other_request(name, q, text, asked, before)
    try:
        if OTHER_JUDGES[name]["maker"] == "anthropic":
            data = client.messages.create(**body).to_dict()
        else:
            r = client.post(other_path(name), json=body)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        err = _other_error(exc)
        if err is None:
            raise
        raise err from exc
    return read(q, other_text(name, data), asked)


async def ajudge_other(client, name: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """judge_other と同じ（通話の中の形。client は other_client(…, aio=True) で作る）。"""
    body = other_request(name, q, text, asked, before)
    try:
        if OTHER_JUDGES[name]["maker"] == "anthropic":
            data = (await client.messages.create(**body)).to_dict()
        else:
            r = await client.post(other_path(name), json=body)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        err = _other_error(exc)
        if err is None:
            raise
        raise err from exc
    return read(q, other_text(name, data), asked)


def add_judge_args(p) -> None:
    """通話の試験が判定を選ぶ引数（どの試験も、ここから足す＝選び方を試験ごとに書かない）。--judge＝その場の判定（既定 3.5 Flash Lite）・
    --judge2＝同時に出すもう1つの判定（既定 JUDGE2）。止まった判定の代わり（STANDIN）は、判定を使えば入る（--no-standin で外す）。"""
    p.add_argument("--judge", nargs="?", const="3.5 Flash Lite", choices=JUDGE_MODELS, help="意味の判定を使う（DESIGN.md 6-10 の (6)）")
    p.add_argument("--judge2", nargs="?", const=JUDGE2, choices=tuple(OTHER_JUDGES) + JUDGE_MODELS,
                   help=f"もう1つの判定を同時に出す（DESIGN.md 6-10 の (6)。既定 {JUDGE2}）")
    p.add_argument("--no-standin", action="store_true", help=f"止まった判定の代わり（{STANDIN}）を使わない（比べるときだけ）")
    p.add_argument("--judge-down", action="append", choices=tuple(OTHER_JUDGES) + JUDGE_MODELS,
                   help="試験だけ：この判定が止まっているものとして流す（代わりが受け持つかを確かめる。いくつでも）")
    # 中身の判定（grounding）の顔ぶれ（既定 grounding.GROUNDERS）。「なし」＝中身を確かめられない通話（受け皿の道を通話で確かめる）
    p.add_argument("--grounder", action="append", choices=tuple(OTHER_JUDGES) + JUDGE_MODELS + (NO_GROUNDER,),
                   help="試験だけ：AIの自分の言葉の中身の判定の顔ぶれ（同時に出して早いほう。既定 grounding.GROUNDERS。「なし」で判定を使わない）")


# ── 止まった判定の代わり（✅ 2026-10-19 ユーザー：「どれかがダウンしていたら、Jevも使ったらいいんじゃない」）。
#    2つの判定は会社を分けて同時に出す（✅ 2026-10-19 ユーザー：「同じ会社のモデルだけだと、通信の問題が影響するでしょ」。G-61：同じ会社の
#    2本は遅れが一緒に出て、ほとんど縮まなかった）。1社が止まると、残りの1社だけ＝その会社の遅れがそのまま出て、確かめも1つ減る
#    ⇒ 3つ目の会社の判定（Jev）が、止まった判定の受け持ちに入る。ふだんは使わない（✅ 2026-10-18 ユーザー：「当面は、Sonnetだけを使いましょう」）
STANDIN = "Jev 1.14"
NO_GROUNDER = "なし"
DOWN_AFTER_S = 10.0  # 1回の判定をこの秒で打ち切り、その判定は止まっているとみなす（報告を待たせるのは2秒まで。G-61：Sonnet 5.5 の最長 9.4秒）
DOWN_S = 60.0        # 止まっているとみなした判定に、問い合わせずに代わりへ回す秒（毎回の発話で、止まった判定の答えを待たない）


def maker(name: str) -> str:
    """判定の会社（Google の判定は JUDGE_MODELS）。"""
    return OTHER_JUDGES[name]["maker"] if name in OTHER_JUDGES else "google"


def outage(exc: BaseException) -> bool:
    """判定が止まっている側の失敗か（時間切れ・つながらない・上限・キーが止められた・混雑）。問い合わせの中身が断られた（400 など）は、
    その発話だけ代わりに回し、止まっているとはみなさない。"""
    for v in (getattr(exc, "status", None), getattr(exc, "code", None)):
        if isinstance(v, int) and 400 <= v < 500 and v not in (401, 403, 408, 429):
            return False
    return True


class NoJudgment(RuntimeError):
    """受け持ちの判定も代わりも、答えを返さなかった（tried＝問い合わせた判定ごとの、答えが無かった理由）。"""

    def __init__(self, tried: list[dict]):
        super().__init__("判定が届かなかった：" + "・".join(f"{t['model']}（{t.get('error') or '確からしさが線より下'}）" for t in tried))
        self.tried = tried


class JudgePanel:
    """通話の中の意味の判定の受け持ち（唯一の場所。DESIGN.md 6-10 の (6)）：受け持ち（judge・judge2）ごとに、決めた判定へ問い合わせる。
    受け持ちの判定が答えなかった（止まっている・上限・時間切れ・読めない返事）ときは、代わり（STANDIN）に問い合わせる。
    - 代わりが入るのは1つの受け持ちだけ（2つの受け持ちが同じ判定になると、独立した2つの確かめにならない）。入った受け持ちの判定が答えたら返す
    - 代わりは、ほかの受け持ちと違う会社だけ（同じ会社だと、止まるのも遅れるのも一緒）
    - 止まっている（outage）とみなした判定は DOWN_S 秒、問い合わせずに代わりへ
    - 確からしさを返す判定（Jev）は、線（OTHER_JUDGES の sure_min）より下なら答えにしない＝その受け持ちは届かなかった（もう1つの判定で
      決め、通話後の検証へ。✅ 2026-10-18 ユーザーの案「このスコア以上ならJevで間違いなく正確というライン」の線）。受け持ちに置いたときも同じ
    どれを使って答えたかは、答え（Reading）の by・tried に残す。"""

    def __init__(self, judges: dict, standin: tuple | None = None, down=(), clock=None):
        import time

        self.judges = judges  # 受け持ち → (判定の名前, async (質問, 言葉, こちらが言い直した言葉) -> Reading)
        self.standin = standin if standin and maker(standin[0]) not in {maker(n) for n, _ in judges.values()} else None
        self.forced_down = set(down)   # 試験だけ（--judge-down）
        self.down_until: dict[str, float] = {}
        self.holder: str | None = None  # 代わりが入っている受け持ち
        self.clock = clock or time.monotonic

    def slot(self, kind: str):
        """受け持ち kind の判定（convo.check_handler に渡す形。panel＝この受け持ちの仕組み）。"""
        async def run(q, text, asked=None, before=None):
            return await self.ask(kind, q, text, asked, before)
        run.panel = self
        return run

    def reset(self) -> None:
        """止まっているとみなした印と、代わりが入っている受け持ちを消す（判定の読みを言葉ごとに測る試験だけ：1つの言葉の時間切れで、
        次の言葉からの問い合わせを飛ばさない。⛔ 2026-10-22 G-59：3.5 Flash Lite の時間切れ1回のあと、残りの言葉を全部飛ばして数えた）。"""
        self.down_until.clear()
        self.holder = None

    async def _one(self, name: str, fn, q, text: str, asked: str | None, before: str | None):
        import asyncio

        if name in self.forced_down:
            raise OtherJudgeError(503, "試験：止まっているものとして扱う")
        return await asyncio.wait_for(fn(q, text, asked, before=before), DOWN_AFTER_S)

    async def _try(self, name: str, fn, q, text: str, asked: str | None, before: str | None, tried: list[dict]) -> Reading | None:
        from common import redact

        t0 = self.clock()
        try:
            got = await self._one(name, fn, q, text, asked, before)
        except Exception as exc:
            if outage(exc):
                self.down_until[name] = self.clock() + DOWN_S
            tried.append({"model": name, "error": redact(repr(exc))[:160], "s": round(self.clock() - t0, 3)})
            return None
        if got is None:
            tried.append({"model": name, "error": "読めない返事", "s": round(self.clock() - t0, 3)})
            return None
        line = OTHER_JUDGES.get(name, {}).get("sure_min")  # 確からしさを返す判定（Jev）は、線から上だけを答えにする
        if line is not None and (got.sure is None or got.sure < line):
            tried.append({"model": name, "unsure": str(got), "sure": got.sure, "s": round(self.clock() - t0, 3)})
            return None
        return got

    async def ask(self, kind: str, q, text: str, asked: str | None = None, before: str | None = None) -> Reading:
        tried: list[dict] = []
        name, fn = self.judges[kind]
        if self.down_until.get(name, 0.0) > self.clock():
            tried.append({"model": name, "error": "止まっている（前の問い合わせ）"})
        elif (got := await self._try(name, fn, q, text, asked, before, tried)) is not None:
            if self.holder == kind:
                self.holder = None
            return self._mark(got, name, tried)
        if self.standin is None or self.holder not in (None, kind):
            raise NoJudgment(tried)
        self.holder = kind
        sname, sfn = self.standin
        got = await self._try(sname, sfn, q, text, asked, before, tried)
        if got is None:
            raise NoJudgment(tried)
        return self._mark(got, sname, tried)

    @staticmethod
    def _mark(got: Reading, name: str, tried: list[dict]) -> Reading:
        got.by, got.tried = name, tuple(tried)
        return got


def make_judges(args) -> dict:
    """add_judge_args で選んだ判定を、run_call にそのまま渡せる形で（{"judge": …, "judge2": …, "grounder": …}。選ばれていなければ None）。
    2つは1つの JudgePanel の受け持ち（止まった判定の代わりを、2つで分け合う）。"""
    from common import say

    names = {k: getattr(args, k, None) for k in ("judge", "judge2")}
    used = {k: n for k, n in names.items() if n}
    if not used:
        return {k: None for k in names} | {"grounder": None}
    standin = None if getattr(args, "no_standin", False) else STANDIN
    if standin and maker(standin) in {maker(n) for n in used.values()}:
        say(f"止まった判定の代わり（{standin}）は、ほかの判定と同じ会社なので使いません")
        standin = None
    panel = JudgePanel({k: (n, make_judge(args, n)) for k, n in used.items()},
                       standin=(standin, make_judge(args, standin)) if standin else None, down=getattr(args, "judge_down", None) or ())
    # AIが自分の言葉で言う文の中身の判定（grounding）も、判定を使う通話では一緒に作る（顔ぶれは grounding.GROUNDERS＝危ない文を通さないと
    # 測ったものだけ。意味の判定の顔ぶれとは別）。⛔ 2026-10-22 G-63：言葉で見る確かめは、通すべき世間話・お礼を12文止め、作り話を2文通した
    from grounding import GROUNDERS, make_grounder
    chosen = tuple(getattr(args, "grounder", None) or GROUNDERS)
    grounder = None if NO_GROUNDER in chosen else make_grounder(args, chosen)
    return {k: panel.slot(k) if k in used else None for k in names} | {"grounder": grounder}


def make_judge(args, model_name: str):
    """通話の中で使う形：async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。キーは会話と同じく、上限・止められたキーを飛ばす
    （common.KeyRing.acall）。どの試験も、これを run_call(judge=…) に渡す（作り方を試験ごとに書かない）。"""
    from common import KeyRing, load_secret
    from g23_names import find_models

    if model_name in OTHER_JUDGES:
        client = other_client(model_name, load_secret(OTHER_JUDGES[model_name]["key"], args.env), aio=True)

        async def other(q, text, asked=None, before=None):
            return await ajudge_other(client, model_name, q, text, asked, before)
        return other
    ring = KeyRing(args)
    model = find_models(ring.client())[0][model_name]

    async def run(q, text, asked=None, before=None):
        # どのキーも上限で休み中なら、待たずに失敗にする（通話の中では待っても間に合わない＝JudgePanel が代わりへ回す）
        got, _ = await ring.acall(model, lambda client: ajudge(client, model, q, text, asked, before), max_wait_s=0.0)
        return got
    return run
