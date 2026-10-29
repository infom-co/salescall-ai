"""G-59 の外れが、流れの制御の動きを本当に変えるかを、会話の試しで確かめる（0円・判定もAIも呼ばない）。外れの形ごとに、正しい読み（正解の表）と
外れた読みで同じ会話を流し（AIの報告は、その言葉で考えられる報告を置く）、返す結果・流す録音・名乗りを聞いていない方の印を比べる。判定は1つだけ
（その判定が外れたとき）。数える道具（g59_tally の flow_ok）は流れの制御の決まりを写したもので、数えすぎ・数え漏れがありうる＝ここで確かめる。
✅ 2026-10-30 ユーザー：「流れの制御の動きが変わる外れの原因と理由が何かを、Lunaと考えないSonnetでしっかりと分析した方がいいんじゃないですか？」
⛔ 1回目は、電話のやり取りの字に事情の字（言っていない）を入れ、判定の返事が読めない形になって、どれも「確かめる」で同じに見えた ⇒ 読めない返事を
   作ったら止める（heard の assert）"""
import json, dataclasses
from flowctl import Flow, ABOUT, PHONE, SPEAKER, WHO
from instruction import Target
from g22_opening import BUSINESS
from meaning_judge import read
b = dataclasses.replace(BUSINESS, target=Target(name="岡田", reading="おかだ", role="営業部部長"))
K = {"同": "前と同じ方", "違": "前と違う方", "本": "ご本人", "受": "受付・取次ぎの方", "後": "代理の方（後任）", "代": "代理の方（どれかは分からない）",
     "別": "別の方（どなたかは分からない）", "不": "決められない", "無": "何もない", "待": "取り次ぎを待つ", "x": "言っていない", "在": "不在", "籍": "在籍していない"}

def facts(code):   # 例 "違本xx"＝(前と同じ方か, 誰か, 電話のやり取り＝x は何もない, 事情＝x は言っていない)
    return {SPEAKER: K[code[0]], WHO: K[code[1]], PHONE: K["無"] if code[2] == "x" else K[code[2]], ABOUT: K[code[3]]}

def heard(f, text, code):
    f.on_callee(); f.on_heard(text)
    if code:
        r = read(f.judge_context()[0], json.dumps(facts(code), ensure_ascii=False))
        assert r is not None, (text, code)   # 読めない返事を作っていたら止める（⛔ 1回目は電話のやり取りに事情の字を入れ、判定が届かなかった）
        f.on_check("judge", r)

def shown(r):
    return r.get("result"), [p.get("part", p.get("kind")) for p in r.get("play") or []], (r.get("say") or "")[:30]

def run(steps, successor=False):
    """steps：[(相手の言葉, 読みの字, AIの報告 (op, args) か None)]。最後の報告の返事と、流れの制御の状態を返す。"""
    f = Flow(b.questions, opening=b.replay_parts(), target=b.replay_parts(), play_recordings=True, judge_names=b.judge_names())
    f.use_checks("judge")
    if successor:
        heard(f, "岡田は先月退職いたしました。", "同受x籍")
        f.apply("request_callback", {"why": "departed"})
    out, ctx = [], None
    for text, code, op in steps:
        heard(f, text, code)
        ctx = {"asked": f.asked_target is not None, "before": f.judge_context()[2], "successor": successor}   # 最後の言葉の場面（g59_tally の行と同じ形）
        if op:
            out.append(shown(f.apply(*op)))
    return out, (f.phase, f.speaker_from, f.passed_on), ctx

T, H, R, S = (("report_party", {"party": "target"}), ("replay_opening", {"part": "handover"}), ("report_party", {"party": "reception"}),
              ("report_party", {"party": "successor"}))
YES = ("はい、そうです。", "同本xx", T)
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
    ("Luna：確かめへの「いいえ、私は山田と申します。」（前「お電話代わりました。担当の山田です。」）を前と違う方（AIは代わった方）",
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
    ("Luna：確かめへの「いいえ、私は山田と申します。」（前「お電話代わりました。担当の山田です。」）をご本人（AIは代わった方）",
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
