"""G-17：発音の審査（judge.py）が使えるかを、ユーザーが聞いて確かめた録音で確かめる。

- 問題あり（ユーザーが聞いて確かめた5本）：見つけられるか
- 問題なし（ユーザーが5点・4.5点を付けた5本）：無い問題を「ある」と言わないか
審査する AI の答えはぶれうるので、1本につき --repeat 回ずつ審査する。
"""
from __future__ import annotations

import asyncio
import json

from common import HERE, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say
from g17_misspeak import SCRIPTS
from judge import judge

R = HERE / "results"
CASES = [
    # (ラベル, 録音, 台本, ユーザーが聞いて確かめた問題)
    ("問題あり1", "corpus/misread_omo-isogashii_Sadachbia.wav", "A", "「お忙しいところ」を「おも忙しいところ」"),
    ("問題あり2", "G-03_Leda_warm_business_x2.wav", "A", "「株式会社サンプルの担当」の「サンプル」が抜けた"),
    ("問題あり3", "g17_audio/20261012_194722_033_Sadachbia_tone_x2_short_A.wav", "A", "出だしが「おまちばしところ」／「失礼いたし」で途切れた"),
    ("問題あり4", "g17_audio/20261012_194722_024_Leda_tone_x2_short_B.wav", "B", "同じ文を2回言った／「ありがとうございました」が途中で切れた"),
    ("問題あり5（軽い）", "g17_audio/20261012_194722_039_Sadachbia_tone_x2_long_B.wav", "B", "「アオバ」がオとアの間（オに8割がた近い）。少し噛んだくらい"),
    ("問題なし1（S2・5点）", "G-03_Sadachbia_warm_business_x2.wav", "A", ""),
    ("問題なし2（S4・5点）", "G-03_Sadachbia_smile_x2.wav", "A", ""),
    ("問題なし3（L2・4.5点）", "G-03_Leda_humble_x2.wav", "A", ""),
    ("問題なし4（L4・5点）", "G-03_Leda_humble_business_x2.wav", "A", ""),
    ("問題なし5（L4・5点）", "G-03_Leda_humble_business_x2_2.wav", "A", ""),
]


async def run(args) -> dict:
    if args.dry_run:
        missing = [c[1] for c in CASES if not (R / c[1]).exists()]
        return {"dry_run": True, "missing_audio": missing}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for label, wav, sid, known in CASES:
            pcm, rate = read_wav_pcm16_mono(R / wav)
            try:
                res = await judge(client, pcm, rate, SCRIPTS[sid])
                row = {"rep": rep, "case": label, "wav": wav, "known": known, **res}
            except Exception as exc:
                row = {"rep": rep, "case": label, "wav": wav, "known": known, "error": describe_error(exc)}
            rows.append(row)
            say(f"[{rep}] {label}：{json.dumps(row.get('problems'), ensure_ascii=False)[:300]} {row.get('error', '')}")
            await asyncio.sleep(args.pause)
    return {"model": "gemini-3.8-live", "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0)
    args = p.parse_args()
    save_result("G-17-judge-check", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
