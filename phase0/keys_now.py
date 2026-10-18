"""いま、どのキーがそのモデルで使えるかを、1本ずつ短い問い合わせを1回だけ送って確かめる（キーの値は出さない。名前だけ）。
⛔ 2026-10-18：前の日の上限の記録を混ぜて読み、「ほとんどのキーで使い切った」と誤って測定を翌日へ回した（実際は11本中2本だけ）。
   キーの状態は記録から推し量らず、これで確かめる。
使い方：python keys_now.py [モデルのID（既定 gemini-3.5-flash-lite）]"""
from __future__ import annotations

import sys

from common import KeyRing, base_parser, describe_error, is_daily, is_denied, is_quota, make_client, require_python, say


def check(model: str) -> dict[str, list[str]]:
    ring = KeyRing(base_parser("").parse_args([]), state_path=False)
    ring.state_path = None  # 確かめるだけ（その日の状態の記録は書き換えない）
    out: dict[str, list[str]] = {"使える": [], "1日の上限": [], "1分の上限": [], "止められている": [], "ほか": []}
    for k in ring.keys:
        try:
            client = make_client(k)  # ⚠️ 問い合わせの間は手放さない（手放すと「client has been closed」になる）
            client.models.generate_content(model=model, contents="「はい」とだけ答えてください。")
            out["使える"].append(k.name)
        except Exception as exc:
            kind = "止められている" if is_denied(exc) else "1日の上限" if is_daily(exc) else "1分の上限" if is_quota(exc) else "ほか"
            out[kind].append(k.name if kind != "ほか" else f"{k.name}：{describe_error(exc).get('message', '')[:80]}")
    return out


if __name__ == "__main__":
    require_python()
    m = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.5-flash-lite"
    for kind, names in check(m).items():
        say(m, kind, len(names), names)
    # ⛔ 2026-10-18 夜：11本とも「使える」と出たのに、流すと44個で全部が1日の上限に当たった（1回の問い合わせが通っただけ）
    say("⚠️「使える」は、短い問い合わせが1回通っただけです。1日の残りの量は分かりません（量が要る測定は、少し流して様子を見る）。")
