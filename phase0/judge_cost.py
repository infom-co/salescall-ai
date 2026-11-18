"""有料の判定（Anthropic）の料金を、新しく送らずに記録から数える（PHASE0 G-59 の 2026-10-29）。

送った回数：results/ の記録の、判定ごとの1件の読み（「判定の名前」と「判定の名前＋秒」の対）を、同じ読み（同じ言葉・同じ判定・同じ秒）は
1回に数える（--reuse で前の回の読みを写した記録を、2回に数えないため）。会話の試験は判定1回ずつの記録が無いので数えられない
（⇒ これからは meaning_judge.SPEND が、どの結果にも料金を入れる）
⛔ 2026-10-28 ユーザー：「ここまでで3万円以上使用しているということですよ。無駄遣いや、必要ないのに使っているシーンはありませんか？」
⛔ 2026-11-18 日本時間15時台（見直し (13) の持ち場 D の D13）：送り方の比べ（10-25 の G-53race・10-24 の G-63G67each の記録で、送り始めの時刻と
   組の顔ぶれを変えたときの正しさ・秒・Sonnet の数）を外した。組の顔ぶれ（Jev を先頭に考えない Sonnet）と先に出すもの（並びの最初）を手で書いていた
   ＝本番は顔ぶれが members_for（答えの段は d1-ultra・Jev・考える Sonnet）・先に出すものが leads_of（d1-ultra と Jev）。2つの記録には d1-ultra の
   読みが無く、本番の組では数えられない（記録の無い判定の読みを推し量らない）。本番の組で決める数えは、お呼びする段が g59_tally.cascade（顔ぶれ・
   先に出すもの・決め方を本番の1か所から作り、記録の無い判定で決まりうる行は数えない）・答えの段が G-53 の組の測り（本番の JudgePanel で送る＝
   g53_meaning.run_group）。10-29 の比べの数は PHASE0_VERIFICATION.md G-59 の 10-29 の段に残っている
"""
from __future__ import annotations

import collections
import glob
import json
import os

from common import HERE

PAID = ("Claude Sonnet 5.5（考える）", "Claude Sonnet 5.5", "Claude Haiku 5.5（考える）", "Claude Haiku 5.5")   # 記録の欄の名前（送った回数を数える有料の判定）


def paid_calls() -> dict:
    """日ごと・判定ごとの、本当に送った回数（同じ読みは1回）。"""
    seen: dict = {}

    def walk(x, day):
        if isinstance(x, dict):
            for n in PAID:
                if n + "秒" in x:
                    seen.setdefault((n, x.get("qid"), x.get("text"), x.get("asked"), x.get(n + "秒"), str(x.get(n))), day)
            for v in x.values():
                walk(v, day)
        elif isinstance(x, list):
            for v in x:
                walk(v, day)
    for f in sorted(glob.glob(str(HERE / "results" / "**" / "*.json"), recursive=True)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        walk(d, (d.get("saved_at") or "")[:10] if isinstance(d, dict) else "")
    out = collections.defaultdict(collections.Counter)
    for k, day in seen.items():
        out[day][k[0]] += 1
    return {day: dict(c) for day, c in sorted(out.items())}


def main() -> None:
    out = {"送った回数（同じ読みは1回）": paid_calls()}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    os.chdir(HERE)
    main()
