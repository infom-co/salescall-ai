"""G-59 の外れが、流れの制御の動きを本当に変えるかを、会話の試しで確かめる（0円・判定もAIも呼ばない）。外れの形ごとに、正しい読み（正解の表）と
外れた読みで同じ会話を流し（AIの報告は、その言葉で考えられる報告を置く）、返す結果・流す録音・名乗りを聞いていない方の印を比べる。判定は1つだけ
（その判定が外れたとき）。測りの数え（g59_tally）も、行の場面をここで組み（row_scene）、会話AIの報告ごとの動きの名前（act_name）で比べる＝流れの制御の
決まりを写さない（⛔ 2026-11-18 の見直し (13) の D1・D2 まで、数える道具 g59_tally.flow_ok は流れの制御の決まりを写した印で比べ、数え漏れ・数えすぎがあった）。
✅ 2026-10-30 ユーザー：「流れの制御の動きが変わる外れの原因と理由が何かを、Lunaと考えないSonnetでしっかりと分析した方がいいんじゃないですか？」
⛔ 1回目は、電話のやり取りの字に事情の字（言っていない）を入れ、判定の返事が読めない形になって、どれも「確かめる」で同じに見えた ⇒ 読めない返事を
   作ったら止める（heard の assert）"""
import json, dataclasses
from flowctl import ABOUT, PHONE, SPEAKER, WHO
from instruction import Target
from g22_opening import BUSINESS
from meaning_judge import read
# 測りの業務（岡田・おかだ・営業部部長）の唯一の定義（G-59 の測りの業務 g59_calling_judge.flows・電話を終える言葉の試しの表の流れ end_words.calling_flow もここを読む。
# 頼み rv140/AB の (2) の (b)。⛔ d8d03a78 まで、同じ定義を3か所に書いていた）
b = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長"))
# フルネームのある業務（詳しい呼ぶ一言＝target_full を流せる場面の行だけ。行の印 full）。呼ぶ一言・確かめる一言・ご事情を伺う一言の文は b と同じ
b_full = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長", full_name="岡田太郎", full_reading="おかだたろう"))
# かけた番号が携帯電話の番号の業務（行の印 mobile）の番号。携帯電話の番号かは本番の1か所（flowctl.is_mobile を業務設定の入口 instruction.Business.flow が読む）が決める
MOBILE = "+819012345678"


def business_of(r: dict):
    """測りの行 r の場面の業務（唯一の作り＝場面を組む row_scene・読み手の束の会話 g59_readers.conversation）：場面の業務の印（g59_calling_judge.MARKS）から。
    full＝フルネームのある業務（b_full）・mobile＝かけた番号が携帯電話の番号（業務設定の phone_e164）・personal＝個人のお客様（Target.kind。お名前と読みは同じ）。
    印から流れの制御に渡る真偽（Flow の dialed_mobile・personal）は、業務設定の入口（Business.flow）が本番と同じに決める＝ここでは業務だけを替える（2026-11-22 分身 AM＝
    担当 AK の棚卸しの F8・束3）"""
    bz = b_full if r.get("full") else b
    if r.get("mobile"):
        bz = dataclasses.replace(bz, phone_e164=MOBILE)
    if r.get("personal"):
        t = bz.target
        bz = dataclasses.replace(bz, target=Target(kind="個人", name=t.name, reading=t.reading, full_name=t.full_name, full_reading=t.full_reading))
    return bz
K = {"同": "前と同じ方", "違": "前と違う方", "本": "ご本人", "受": "受付・取次ぎの方", "後": "代理の方（後任）", "代": "代理の方（どれかは分からない）",
     "別": "別の方（どなたかは分からない）", "不": "決められない", "無": "何もない", "待": "取り次ぎを待つ", "x": "言っていない", "在": "不在", "籍": "在籍していない"}

def facts(code):   # 例 "違本xx"＝(前と同じ方か, 誰か, 電話のやり取り＝x は何もない, 事情＝x は言っていない)
    return {SPEAKER: K[code[0]], WHO: K[code[1]], PHONE: K["無"] if code[2] == "x" else K[code[2]], ABOUT: K[code[3]]}

def heard(f, text, code, asks=None):
    """相手の言葉 text を聞き、判定の読み code（読みの字か、項目ごとの答え＝判定の返事の形の dict）を本番と同じ入口（meaning_judge.read → Flow.on_check）で渡す。
    asks＝同時に出る尋ねているかの問い合わせの読み（使う判定ごとの受け持ち＝flowctl.ASKS_CHECKS。None＝渡さない＝まだ届かない）。会話AIの答えの報告は、この読みを
    待ってから決める（Flow._answer_judged。まとめ役 CL-1 の頼み rv178/X の5回目）。"""
    f.on_callee(); f.on_heard(text)
    if code:
        r = read(f.judge_context()[0], json.dumps(facts(code) if isinstance(code, str) else code, ensure_ascii=False))
        assert r is not None, (text, code)   # 読めない返事を作っていたら止める（⛔ 1回目は電話のやり取りに事情の字を入れ、判定が届かなかった）
        f.on_check("judge", r)
    if asks is not None:
        from flowctl import ASKS_CHECKS, JUDGMENTS
        said = read(f.asks_context()[0], json.dumps({"choice": asks}, ensure_ascii=False))
        assert said is not None, (text, asks)
        for a, j in zip(ASKS_CHECKS, JUDGMENTS):
            if j in f.checks_used:
                f.on_check(a, said)

def shown(r):
    return r.get("result"), [p.get("part", p.get("kind")) for p in r.get("play") or []], (r.get("say") or "")[:30]

def scene(steps, successor=False, checks=("judge",), business=None, sees_target=None, first=None, first_party="person"):
    """お呼びする段の場面を、本番の流れの制御（Flow）に組む唯一の所（会話の試し run・G-68 の測り g68_returned が通る）。steps：[(相手の言葉, 読みの字,
    AIの報告 (op, args) か None)]。checks＝流れの制御が待つ確かめの名前（Flow.use_checks）。business＝業務（既定は b・フルネームのある行は b_full）。
    手順に4つ目を書けば、その言葉の尋ねているかの読み（heard の asks）。
    sees_target＝お呼びする方の名前と読みを判定に見せるか（測りの --hide-names＝instruction.Business.judge_names の1か所）。
    first＝電話に出た方の第一声（本番の始まり＝繋がってから第一声を聞き、会話AIの「人」の報告のあとに名乗りと呼ぶ一言を流す Flow.on_connect の道で組む）。
    None は第一声を待たない古い形（名乗りの終わりが呼ぶ一言＝測りの表の行の場面）。2つの形で、判定に見せる質問とその前の言葉が同じこと（第一声は判定に渡らない）を
    試験 CallingBeforeTest が確かめる。first_party＝第一声への会話AIの報告（既定は「人」。第一声でお呼びする方ご本人が名乗った電話＝行の印 named_first は「目的の人」＝
    流れの制御は呼ぶ一言ではなく確かめる一言を流す＝Flow._first_words_named_target）。
    流れの制御は業務設定の入口（instruction.Business.flow＝本番の1か所）で作る（かけた番号が携帯電話の番号か・個人のお客様かも、そこで読む）。⛔ 2026-11-22 分身 AM まで、
    ここで Flow を直接作り、かけた番号も個人のお客様も渡さなかった＝測りの場面は会社の電話の業務だけ（担当 AK の棚卸しの F8。印の無い行の判定に送る文と動きは同じ）。
    返す：(流れの制御, 報告の返事の並び, 最後の言葉の場面)。"""
    bz = business or b
    f = bz.flow(recorded=True, play_recordings=True, sees_target=sees_target)
    f.use_checks(*checks)
    if first is not None:
        f.on_connect(); f.on_callee(); f.on_heard(first)
        f.apply("report_party", {"party": first_party}); f.on_activity(False); f.on_first_words_end()
    if successor:
        heard(f, "岡田は先月退職いたしました。", "同受x籍")
        f.apply("request_callback", {"why": "departed"})
    out, ctx = [], None
    for text, code, op, *asks in steps:
        heard(f, text, code, *asks)
        ctx = {"asked": f.asked_target is not None, "before": f.judge_context()[2], "successor": successor}   # 最後の言葉の場面（g59_tally の行と同じ形）
        if op:
            out.append(shown(f.apply(*op)))
    return f, out, ctx


class NoScene(Exception):
    """測りの行の場面を、本番の流れの制御に組めない（行の前の言葉の正しい読みが表に無い・組んだ場面が行の場面と違う）。"""


class Unreadable(Exception):
    """判定の読み（記録の項目ごとの答え）が、今の問いでは本番の入口（meaning_judge.read）で読めない（今の選べるものに無い答え＝古い文で取った記録）。
    本番なら読みとして届かない＝流れの制御の動きは比べられない。"""


def row_scene(r: dict, sees_target=None, first=None):
    """測りの行 r（g59_tally の行＝text・before・asked・successor と場面の業務の印＝g59_calling_judge.MARKS）の場面を本番の流れの制御に組み、行の言葉を聞いた
    ところ（判定を渡す前）の流れの制御を返す（唯一の作り。判定に見せる質問 g59_calling_judge.judge_questions もここを通る）。前の言葉は、正解の表の前の
    言葉の読み（g59_calling_judge.steps_before）で流す。業務は印の業務（business_of）。第一声でご本人が名乗った電話の行（印 named_first）は、その第一声から
    （会話AIの報告は目的の人＝確かめる一言から始まる）。組んだ場面が行の場面（確かめる一言のあとか・判定に渡す前の言葉）と違えば NoScene（黙って違う場面で
    数えない）。sees_target・first は scene と同じ（測りの --hide-names・電話に出た方の第一声から始める本番の始まり。印 named_first の行は、その印の第一声）。"""
    import g59_calling_judge as g
    try:
        steps = g.steps_before(r["before"])
    except KeyError as e:
        raise NoScene(f"前の言葉の正しい読みが表（BEFORE_READ）に無い：{e}") from None
    named = r.get("named_first")
    f, _, _ = scene(steps, successor=r["successor"], business=business_of(r), sees_target=sees_target, first=named or first,
                    first_party="target" if named else "person")
    f.on_callee(); f.on_heard(r["text"])
    built = (f.asked_target is not None, f.judge_context()[2] or None)
    if built != (bool(r["asked"]), r["before"] or None):
        raise NoScene(f"組んだ場面（確かめたあとか・前の言葉）が {built}")
    return f


def act_name(f, rep: dict | None) -> str:
    """流れの制御の返事 rep と、そのあとの流れの制御の覚えから、お呼びする段の動きの名前（唯一の作り＝正しい読みの動きと判定の読みの動きのどちらも通る。
    g53_meaning.flow_name と同じ考え）。返事＝お客様に聞こえること（流す録音・相づち・言い方・次の質問）と結果。会話AIへの知らせ（message）は、差し戻し
    （rejected）のときだけ（差し戻しの種類を運ぶ）。覚え＝あとの発話の動きを変えるもの：段・名乗りを聞いていない方か（passed_on）・電話口の方の区切り
    （speaker_from）・確かめたあとか・締めた理由・繋がらなかった理由（not_reached）・記録の事実（facts。書き留め FACT_NOTES は除く）と、まだお呼び
    している段（締めていない）なら、いまの方をご本人と読んだ判定・不在と読んだ判定（Flow._target_readings）・前の方が不在を伝えて代わった方か（Flow._handed）
    ＝お呼びする段の報告を決める所（_calling_reported・_away）だけが読む覚え（繋いだあと・締めたあとは読まない＝比べない。⛔ 比べると、急いでいる方への
    伺いで繋いだあとの、使われない「ご本人」の覚えの違いを外れと数えた）。報告の無いターンで動かない（None）は「動かない」。"""
    reply = {k: v for k, v in (rep or {}).items() if k != "message" or (rep or {}).get("result") == "rejected"}
    keep = lambda e: {k: v for k, v in e.items() if k not in FACT_NOTES} if isinstance(e, dict) else e
    state = {"phase": f.phase, "passed_on": f.passed_on, "speaker_from": f.speaker_from, "asked": f.asked_target is not None,
             "end_reason": f.end_reason, "not_reached": f.not_reached, "facts": {k: [keep(e) for e in v] if isinstance(v, list) else v for k, v in f.facts.items()}}
    if f.phase == "calling" and f.end_reason is None:
        state |= {"target_readings": [sorted(x) if isinstance(x, set) else x for x in f._target_readings()], "handed": sorted(f._handed())}
    return json.dumps({"reply": reply if rep is not None else "動かない", "state": state}, ensure_ascii=False, sort_keys=True, default=str)


_ACTS: dict = {}
# 記録の事実（Flow.facts）のうち、動きではなく「いつ・何を聞いて・誰が・どう読んだか」の書き留め（通話後の確かめの材料）＝動きの名前に入れない
FACT_NOTES = ("turn", "said", "by", "read", "turn_reads", "judging")


def calling_acts(r: dict, got: dict, asks: str | None = None) -> tuple:
    """行 r の場面で、判定の読み got（目的のお客様の判定の項目ごとの答え）を本番と同じ入口（meaning_judge.read → Flow.on_check("judge")）で渡し、
    会話AIの報告（REPORTS）ごとの流れの制御の動きの名前（act_name）の並び。同時に出る「尋ねているか」の問い合わせ（convo の asks＝Flow._asking_now：
    質問なら先に答える・Flow._who_asked：どの方かの答え）の読み asks も同じ入口（meaning_judge.read → Flow.on_check("asks")）で渡す：None なら正しい読み
    （g59_calling_judge.asks_truths。どちらでもよい言葉は、そのどれも）＝目的のお客様の判定の外れを比べる形。尋ねているかの判定の読みを渡すと、その読み1つ（測りの数えの
    「尋ねているか」の問い合わせ＝g59_tally.off_reports が、got に正解の表の読みを渡して比べる。⛔ 4bc94b4d まで、尋ねているかはいつも正しい読みを渡し、尋ねているかの外れは
    値だけで数えていた＝まとめ役 CL-1 の頼み rv178/X の4回目 (1)）。返す：尋ねているかの読みごとの、報告ごとの名前の並び。
    同じ行・同じ読みは1回だけ流す（同じ行＝場面を組む row_scene が読む欄の全部。場面の業務の印＝g59_calling_judge.row_marks も＝⛔ 分身 S（rv176/S）まで鍵に印 full が無く、印の無い行の動きを
    印のある行にも使っていた＝試験 RereadRowsTest の test_4）。組めない場面は NoScene・今の問いで読めない読み（目的のお客様の判定・尋ねているかのどちらも）は Unreadable。"""
    import g59_calling_judge as g
    k = (r["text"], r["before"] or None, bool(r["asked"]), r["successor"], *g.row_marks(r), json.dumps(got, ensure_ascii=False, sort_keys=True), asks)
    if k not in _ACTS:
        out = []
        for a in (asks,) if asks is not None else g.asks_truths(r):   # 行で決まる正しい読み（代わった方の行は前の言葉で決まる）
            acts = []
            for _, op in REPORTS:
                f = row_scene(r)
                q = f.judge_context()[0]
                res = read(q, json.dumps(got, ensure_ascii=False))
                if res is None:
                    raise Unreadable(f"今の問いで読めない判定の読み：{got}")
                f.on_check("judge", res)
                # 尋ねているかの質問も本番と同じ1か所（Flow.asks_context＝_calling_asked。⛔ a13eb002 まで、ここで呼ぶ一言・確かめる一言の質問を組み直していた）
                said = read(f.asks_context()[0], json.dumps({"choice": a}, ensure_ascii=False))
                if said is None:
                    raise Unreadable(f"今の問いで読めない尋ねているかの読み：{a}")
                f.on_check("asks", said)
                acts.append(act_name(f, f.apply(*op) if op else f.judged_turn(voiced=False)))
            out.append(tuple(acts))
        _ACTS[k] = tuple(out)
    return _ACTS[k]


def run(steps, successor=False):
    """steps：[(相手の言葉, 読みの字, AIの報告 (op, args) か None)]。最後の報告の返事と、流れの制御の状態を返す。"""
    f, out, ctx = scene(steps, successor)
    return out, (f.phase, f.speaker_from, f.passed_on), ctx

T, H, R, S = (("report_party", {"party": "target"}), ("replay_opening", {"part": "handover"}), ("report_party", {"party": "reception"}),
              ("report_party", {"party": "successor"}))
# 会話AIがお呼びする段で出しうる報告の全部（g59_tally が報告ごとに流す）：相手の報告（人の相手＝flowctl.PERSON_PARTIES と、目的の人の代わりにお話しうる方
# STANDIN_PARTIES）・代わった方（名乗りから伺い直す）・お呼びした方で答える（尋ねられたことに、お呼びしている方のことで答える＝replay_opening の target。
# 尋ねているかの読みで動きが変わる答えの報告＝どの方かと読めば、お伝えすることが無ければかけ直さない締め・Flow._asked_target。帳面 0-3。まとめ役 CL-1 の頼み rv178/X の4回目 (1)。
# ⛔ 4bc94b4d まで無く、尋ねているかの外れが動きに出なかった）・報告なし（会話AIが声だけで操作を呼ばなかった＝Flow.judged_turn）
A = ("replay_opening", {"part": "target"})
REPORTS = (("ご本人", T), ("受付の方", R), ("代理の方", ("report_party", {"party": "substitute"})), ("代わった方", H), ("後任の方", S),
           ("お呼びした方で答える", A), ("報告なし", None))
YES = ("はい、そうです。", "同本xx", T)
# 名前の「確かめへの」は、読みが分かれた言葉が確かめる一言のあとの言葉のとき・「お呼びし直した呼ぶ一言への」は、その前のこちらの返事がお呼びし直し（call_target）のとき
# （本番の流れの制御の場面のとおり＝試験 CallingBeforeTest が確かめる）。⛔ d8d03a78 まで、電話を代わった方が違うお名前を名乗ったあと（名乗りから呼ぶ一言でお呼びし直す
# ＝本書 9-4 の (5)）の「いいえ、私は山田と申します。」を「確かめへの」と名付けていた（流れの制御が、その言葉のあとに確かめずに名乗りからお呼びし直す形になってからも、名前が古いままだった＝分身 Y3 の知らせ）
CASES = [
    ("Luna：前の言葉の無い「お電話代わりました、営業部の岡田です。」を前と同じ方", [("お電話代わりました、営業部の岡田です。", "違本xx", T)],
     [("お電話代わりました、営業部の岡田です。", "同本xx", T)], False),
    ("Sonnet：前の言葉の無い「お電話代わりました。」を前と同じ方（AIは目的の人→確かめ→「はい、そうです。」）",
     [("お電話代わりました。", "違不xx", T), YES], [("お電話代わりました。", "同不xx", T), YES], False),
    ("Luna：前の言葉の無い「お電話代わりました。山本です。」を決められない（AIは代わった方）", [("お電話代わりました。山本です。", "違別xx", H)],
     [("お電話代わりました。山本です。", "違不xx", H)], False),
    ("Luna：「それでは岡田にお繋ぎします。」のあとの「はい、お電話代わりました。」を決められない（AIは目的の人）",
     [("それでは岡田にお繋ぎします。", "同受待x", R), ("はい、お電話代わりました。", "違本xx", T)],
     [("それでは岡田にお繋ぎします。", "同受待x", R), ("はい、お電話代わりました。", "違不xx", T)], False),
    ("Luna：名乗りからお呼びし直した呼ぶ一言への「いいえ、私は山田と申します。」（前「お電話代わりました。担当の山田です。」）を前と違う方（AIは代わった方）",
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "同別xx", H)],
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "違別xx", H)], False),
    ("Luna：確かめへの「いいえ、山田です。岡田に代わりますので少々お待ちください。」を不在（AIは受付の方→次に「お電話代わりました。岡田です。」）",
     [("はい。", "同不xx", T), ("いいえ、山田です。岡田に代わりますので少々お待ちください。", "同受待x", R), ("お電話代わりました。岡田です。", "違本xx", T)],
     [("はい。", "同不xx", T), ("いいえ、山田です。岡田に代わりますので少々お待ちください。", "同受待在", R), ("お電話代わりました。岡田です。", "違本xx", T)], False),
    ("Luna：後任の方の場面の「お電話代わりました。山本です。」（前「後任は田中になります。…」）を決められない（AIは代わった方）",
     [("後任は田中になります。少々お待ちください。", "同受待x", R), ("お電話代わりました。山本です。", "違別xx", H)],
     [("後任は田中になります。少々お待ちください。", "同受待x", R), ("お電話代わりました。山本です。", "違不xx", H)], True),
    ("Sonnet：確かめへの「いえ、岡田は退職しまして、私が後任の田中です。」の事情を言っていない（AIは後任の方）",
     [("はい。", "同不xx", T), ("いえ、岡田は退職しまして、私が後任の田中です。", "同後x籍", S)],
     [("はい。", "同不xx", T), ("いえ、岡田は退職しまして、私が後任の田中です。", "同後xx", S)], False),
    ("Luna：名乗りからお呼びし直した呼ぶ一言への「いいえ、私は山田と申します。」（前「お電話代わりました。担当の山田です。」）をご本人（AIは代わった方）",
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "同別xx", H)],
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "違本xx", H)], False),
    ("Luna：同じ言葉をご本人（AIも読み違えて目的の人）",
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "同別xx", T)],
     [("お電話代わりました。担当の山田です。", "違別xx", T), ("いいえ、私は山田と申します。", "違本xx", T)], False),
    ("Sonnet：「どちらの岡田でしょうか？」を決められない（AIは受付の方）", [("どちらの岡田でしょうか？", "同受xx", R)], [("どちらの岡田でしょうか？", "同不xx", R)], False),
    ("Sonnet：同じ言葉を決められない（AIは読み違えて目的の人）", [("どちらの岡田でしょうか？", "同受xx", T)], [("どちらの岡田でしょうか？", "同不xx", T)], False),
]


def changes(right, wrong, succ) -> bool:
    """正しい読みと外れた読みで、流れの制御の動き（返す結果・流す録音・名乗りを聞いていない方の印）が変わるか。"""
    a, sa, _ = run(right, succ)
    w, sw, _ = run(wrong, succ)
    return (a, sa[2]) != (w, sw[2])


def main() -> None:
    for name, right, wrong, succ in CASES:
        a, sa, _ = run(right, succ)
        w, sw, _ = run(wrong, succ)
        print(("同じ" if (a, sa[2]) == (w, sw[2]) else "変わる"), "｜", name)
        if (a, sa[2]) != (w, sw[2]):
            print("    正しい読み：", a, "｜名乗りを聞いていない方か：", sa[2])
            print("    外れた読み：", w, "｜名乗りを聞いていない方か：", sw[2])


if __name__ == "__main__":
    main()
