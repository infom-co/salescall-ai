"""AI自身の文字が、声より先に届くか（DESIGN.md 6-7 の英語の案：見つけたら相手への声を止められるか）。キーを使わない。

会話の試験の結果（convo の said_events：文字が届いた時刻 at_s と、その時点で受け取り済みの声の長さ audio_s）から数える。
文字のかたまりごとに、そのかたまりの「声の中の位置」を、前までの文字の読みの長さ ÷ ふつうの話す速さ（turn_check）で見込み、
届いた時点の受け取り済みの声の長さと比べる：
  先行（秒）＝ 見込みの位置 − 受け取り済みの声の長さ
  ⇒ 正なら、その文字の声が相手に流れる前に、文字が届いている（止められる）。負なら、声のほうが先に流れている。
⚠️ 見込みの位置は、文の間の息つぎを数えないので、実際より早めに出る（先行を小さめに見積もる＝止められる側に甘くならない）。
⚠️ 受け取り済みの声は、届いたそばから相手に流れるとみなす（電話の側で声をためる時間は、フェーズ1で測る）。
"""
from __future__ import annotations

import glob
import json
import statistics
import sys

from jtext import morae
from turn_check import NORMAL_MORAE_PER_S

VOICE = {"S2": "Sadachbia", "S4": "Sadachbia", "L4": "Leda"}


def leads(turn: dict, voice: str) -> list[float]:
    out, before = [], ""
    for e in turn.get("said_events") or []:
        if "audio_s" not in e:
            continue
        pos = morae(before) / NORMAL_MORAE_PER_S[voice]
        out.append(round(pos - e["audio_s"], 3))
        before += e.get("text", "")
    return out


def main(paths: list[str]) -> None:
    allv, first = [], []
    for f in paths:
        d = json.load(open(f, encoding="utf-8"))
        for r in d.get("rows", []) if isinstance(d, dict) else []:
            if not isinstance(r, dict):
                continue
            voice = VOICE.get(r.get("type"))
            for t in r.get("turns", []) or []:
                v = leads(t, voice) if voice else []
                allv += v
                first += v[:1]
    if not allv:
        print("said_events の記録が無い（記録を足す前の結果）")
        return
    q = statistics.quantiles(allv, n=20)
    print(f"文字のかたまり {len(allv)}：先行（秒）の 5%={q[0]:.2f} 中央={statistics.median(allv):.2f} 95%={q[-1]:.2f} "
          f"／ 先に届いた割合={sum(1 for x in allv if x > 0) / len(allv):.0%}")
    print(f"ターンの最初のかたまり {len(first)}：中央={statistics.median(first):.2f}")


if __name__ == "__main__":
    main(sys.argv[1:] or sorted(glob.glob("results/G-*.json")))
