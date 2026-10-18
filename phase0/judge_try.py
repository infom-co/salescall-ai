"""Google 以外の判定（meaning_judge.OTHER_JUDGES）を、1つの命令で試す（DESIGN.md 1-3 の (2)。設定は docs/JUDGE_SETUP.md）。
✅ 2026-10-18 ユーザー：「ジェミニよりほぼ確実に精度がいいものを抜粋しないと時間の無駄」「最新モデルで評価しないとダメだよ」
- 確かめ：キーが入っているか・接続先に届くか・1個だけ判定できるか（GPT-6 Luna の名前が違えば、使えるモデルの名前を出す）
- 測る：いまの判定（3.5 Flash Lite）を測った G-53 の答えと同じ答えで、使える判定ぜんぶに聞く（言い直しへの返事を先に・決め手の15個を先頭に）
使い方：
    python judge_try.py --check   （確かめだけ）
    python judge_try.py           （確かめてから測る。結果は results/G-53-others_*.json）"""
from __future__ import annotations

import subprocess
import sys
import time

from common import HERE, base_parser, describe_error, has_secret, load_secret, require_python, say
from meaning_judge import OTHER_JUDGES, judge_other

# いまの判定を、いまの問い合わせの文で測った答え（2回目。C の組は判定し直していないので使わない）
BASE_RESULT = "results/G-53_20261018_105658.json"


def check(name: str, env) -> bool:
    import httpx

    from g53_meaning import g48_questions

    j = OTHER_JUDGES[name]
    if not has_secret(j["key"], env):
        say(f"⛔ {name}：キー {j['key']} が入っていません（docs/JUDGE_SETUP.md の (2)）")
        return False
    try:
        httpx.get(f"https://{j['host']}/", timeout=15)
    except Exception as exc:
        say(f"⛔ {name}：{j['host']} に届きません（docs/JUDGE_SETUP.md の (1)）：{describe_error(exc).get('message', '')[:120]}")
        return False
    q = next(q for q in g48_questions() if q.id == "G17")  # ご連絡はお電話とメールのどちらがよろしいでしょうか？
    key = load_secret(j["key"], env)
    with httpx.Client(timeout=60) as http:
        t = time.monotonic()
        try:
            got = judge_other(http, name, q, "メールのほうが助かりますが…", "念のためご連絡はメールでよろしいでしょうか？", key=key)
        except httpx.HTTPStatusError as exc:
            say(f"⛔ {name}：{exc.response.status_code} {exc.response.text[:300]}")
            if j["maker"] == "openai" and exc.response.status_code in (400, 404):
                ids = [m["id"] for m in http.get(f"https://{j['host']}/v1/models", headers={"authorization": f"Bearer {key}"}).json().get("data", [])]
                say("使えるモデル（gpt-6 を含む名前）：", sorted(i for i in ids if "gpt-6" in i))
            return False
        say(f"✅ {name}（{j['model']}）：{time.monotonic() - t:.2f}秒で「{got}」（正解は「決められない」）")
    return True


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--check", action="store_true", help="確かめだけ")
    args = p.parse_args()
    ok = [n for n in OTHER_JUDGES if check(n, args.env)]
    if args.check or not ok:
        return
    cmd = [sys.executable, "g53_meaning.py", "--reuse", BASE_RESULT, "--sets", "DABE", "--tag=-others"]
    for n in ok:
        cmd += ["--judge", n]
    say("測ります：", " ".join(cmd[1:]))
    subprocess.run(cmd, cwd=HERE, check=True)


if __name__ == "__main__":
    main()
