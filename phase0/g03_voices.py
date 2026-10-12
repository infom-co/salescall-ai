"""G-3：声（Sadachbia・Leda）が選べて、日本語で自然に話すか。

やること：同じ日本語の台本を、声ごとに読ませて WAV に保存する。
⇒ 保存した WAV を社内の3人以上で聞き、業務に使えるかを判断して PHASE0_VERIFICATION.md に記録する。
※ 保存した WAV は、G-6 などで「相手の声」の代わりにも使える。
"""
from __future__ import annotations

import asyncio
import time

from common import HERE, VOICES, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from live_util import build_config, collect_turn, send_text

INSTRUCTION = (
    "あなたは電話で話す担当者です。これから渡す台本を、そのまま、丁寧で自然な日本語の話し方で読み上げてください。"
    "台本にない言葉を足さないでください。"
)
# 話し方（DESIGN.md 6-3 の材料3「システム共通の会話の決まり」に入る候補）。
# 2026-10-12 ユーザーの評価：「声が暗く、話すスピードが遅すぎ、留守番電話のような話し方」⇒「暗くならず、2倍速で自然に」
STYLES = {
    "baseline": "",
    "bright_fast": (
        "話し方：明るく、はきはきと、感じのよい声で話してください。"
        "留守番電話の案内やナレーションのような、ゆっくりした読み上げ方にしないでください。"
        "ふだんの読み上げのおよそ2倍の速さで、営業電話の担当者が実際に話すような、テンポのよい自然な会話の速さで話してください。"
        "語尾を下げて暗く終わらないでください。"
    ),
    # ユーザーの経験：「2倍速」と指示すると、実際は 1.3倍前後になる（2026-10-12）。目標は実際の速さで 1.3倍前後。
    "bright_x2": (
        "話し方：明るく、はきはきと、感じのよい声で話してください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    # 2026-10-12 ユーザーの評価：暗さは取れた。ただ「明るく」「はきはき」は、営業には不向きな、強引で怪しいニュアンスになる。
    # ⇒ 声の調子の言い方を替えて比べる。速さは実測で 1.15〜1.2倍くらいでもよい。
    "positive_x2": (
        "話し方：前向きで、落ち着いた、感じのよい声で話してください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    "sincere_x2": (
        "話し方：誠実で、やわらかく、前向きな声で話してください。押しつけがましい話し方や、売り込むような話し方はしないでください。"
        "話す速さは2倍速にしてください。留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    "business_x2": (
        "話し方：取引先に電話をかける会社員のように、ていねいで、前向きで、自然な声で話してください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    # 2026-10-12 ユーザーの評価（5点満点）：Leda は positive・business とも4点くらい。positive は少し暗く、聞き返しが少し怖い（威圧的）。
    # business は抑揚に違和感。2つの良いところが合わされば5点。Sadachbia は3.5点くらいで、少し威圧感がある。
    # ⇒ 「前向き＋取引先に電話する会社員」を土台に、暗さ・威圧感・抑揚の違和感を消す言い方を足して比べる。
    "warm_business_x2": (
        "話し方：取引先に電話をかける会社員のように、ていねいで、前向きで、感じのよい声で話してください。"
        "声は暗くせず、やわらかく話してください。抑揚は、ふだんの日本語の会話と同じ、自然な抑揚にしてください。"
        "聞き返すときや確認するときは、相手を問い詰める感じにならないよう、申し訳なさそうに、やさしく言ってください。"
        "威圧的な話し方はしないでください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    "humble_x2": (
        "話し方：お客様にお願いをする気持ちで、謙虚で、やわらかく、前向きな声で話してください。"
        "声は暗くせず、語尾はやさしく終えてください。抑揚は、ふだんの日本語の会話と同じ、自然な抑揚にしてください。"
        "聞き返すときは、申し訳なさそうに言ってください。威圧的な話し方はしないでください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
    "smile_x2": (
        "話し方：感じのよい受付の担当者のように、にこやかで、やわらかく、前向きな声で話してください。"
        "抑揚は、ふだんの日本語の会話と同じ、自然な抑揚にしてください。"
        "聞き返すときは、申し訳なさそうに、やさしく言ってください。威圧的な話し方はしないでください。話す速さは2倍速にしてください。"
        "留守番電話の案内のような、ゆっくりした話し方にしないでください。言葉は1文字も変えないでください。"
    ),
}
# 前回（話し方の指示なし）の実測：読み終えるまで Sadachbia 21.24秒・Leda 26.27秒（G-03_20261012_174309.json）
BASELINE_SECONDS = {"Sadachbia": 21.24, "Leda": 26.27}
SCRIPT = (
    "お忙しいところ恐れ入ります。株式会社サンプルの担当でございます。"
    "本日は、ご予約の日時の確認でお電話いたしました。"
    "15日の午後2時でお間違いないでしょうか。"
    "聞き取りにくかったので、もう一度、おっしゃっていただいてよろしいでしょうか。"
    "念のため確認させてください。ありがとうございました。失礼いたします。"
)


async def one(client, args, voice: str) -> dict:
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION + STYLES[args.style])
    t0 = time.monotonic()
    async with client.aio.live.connect(model=args.model, config=cfg) as session:
        t1 = time.monotonic()
        await send_text(session, f"台本：{SCRIPT}")
        turn = await collect_turn(session, started_at=t1, timeout_s=120)
    suffix = "" if args.style == "baseline" else f"_{args.style}"
    wav = HERE / "results" / f"G-03_{voice}{suffix}.wav"
    n = 2
    while wav.exists():  # 前の見本を上書きしない（ユーザーが評価した見本を残すため）
        wav = HERE / "results" / f"G-03_{voice}{suffix}_{n}.wav"
        n += 1
    if turn.audio:
        write_wav(wav, bytes(turn.audio), turn.audio_rate or 24000)
    s = turn.summary()
    sec = s["audio_seconds"]
    return {"voice": voice, "style": args.style, "connected_after_s": round(t1 - t0, 3), "wav": str(wav) if turn.audio else None,
            "chars_per_second": round(len(SCRIPT) / sec, 2) if sec else None,
            # 2.0 なら2倍の速さ。声がほとんど届かなかった回（G-3 で 0.77秒）は、速さとしては数えない
            "speed_vs_baseline": round(BASELINE_SECONDS[voice] / sec, 2) if sec and sec >= BASELINE_SECONDS[voice] / 3 else None,
            # 文字起こしが届かない回がある（G-7）。届かなければ「確かめられない」（None）にする
            "matches_script": (s["output_transcript"].replace(" ", "") == SCRIPT) if s["output_transcript"] else None, **s}


async def run(args) -> dict:
    if args.dry_run:
        for v in VOICES:
            build_config(voice=v, system_instruction=INSTRUCTION)
        return {"dry_run": True, "config_ok": True, "voices": list(VOICES)}
    client = make_client(pick_key(args))
    results = []
    for v in VOICES:
        try:
            results.append({"ok": True, **await one(client, args, v)})
        except Exception as exc:
            results.append({"ok": False, "voice": v, "error": describe_error(exc)})
        say(f"{v}：", "成功" if results[-1]["ok"] else "失敗")
    return {"model": args.model, "script": SCRIPT, "results": results}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--style", choices=sorted(STYLES), default="baseline", help="話し方の指示")
    args = p.parse_args()
    save_result("G-03", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
