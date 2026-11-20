"""確かめの道具：プログラムから Rakuten Link で発信し、通話の状態を読み、切れるか（docs/PHONE.md 3章の R-5・R-6）。持ち主 CL-3。

管理者の PC で、screen_uia.py と同じフォルダに置いて動かす（ほかの部品は読まない）：
    py probe_dial.py

- かける相手は、ご家族の携帯だけ（管理者の携帯は発信の元なので受けられない）。番号はその場で打ち、ファイルにも結果にも残さない
  （結果には下4けただけ）。
- 回ごとに「ご家族に出てもらうか」を選ぶ。出ない回で、呼び出し中の画面の変わり方を、出る回で、つながったあとの変わり方を記録する。
- 結果は phone_dial_result.json（番号・お名前は入らない）。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent
RESULT = OUT / "phone_dial_result.json"
WATCH = 20.0   # 1回の発信で、画面を見ている長さ（秒）


def main() -> None:
    sys.path.insert(0, str(OUT))
    repo_src = OUT.parents[1] / "src"   # リポジトリの中（app/tools/phone_probe）なら、本物の部品を読む
    if (repo_src / "salescall" / "agent" / "phones" / "screen_uia.py").exists():
        sys.path.insert(0, str(repo_src / "salescall" / "agent" / "phones"))
    try:
        import screen_uia
    except ImportError:
        sys.exit("screen_uia.py が同じフォルダにありません。お送りした screen_uia.py を、このフォルダに置いてください。")
    try:
        screen = screen_uia.UiaScreen()
    except ImportError:
        sys.exit("pywinauto が入っていません：py -m pip install pywinauto==0.6.9")
    number = input("ご家族の携帯の番号を、0から始まる数字だけで打って Enter（ハイフンは入れない。ファイルには残しません）\n> ").strip()
    if not screen_uia.DOMESTIC.fullmatch(number):
        sys.exit("番号は 0 から始まる数字だけ（10けたか11けた）で打ってください。もう一度動かしてください。")
    try:
        n = max(1, min(10, int(input("何回かけますか？（1〜10。まずは 3 がおすすめ）\n> ").strip() or "3")))
    except ValueError:
        n = 3
    print("スマホとの連携：", {True: "している", False: "していない", None: "分からない"}[screen.linked()])
    rounds = []
    for i in range(1, n + 1):
        answer = input(f"\n【{i}回目】この回、ご家族に電話に出てもらいますか？（y＝出てもらう／n＝出ないで鳴らしたまま）\n> ").strip().lower().startswith("y")
        input("用意ができたら Enter を押してください。プログラムが発信します。")
        r: dict = {"round": i, "family_answers": answer, "states": []}
        t0 = time.monotonic()
        try:
            screen.dial(number)
            r["dialed"] = True
            r["pressed_by"] = screen.last_press
            print("  発信できました（押し方：", {"invoke": "そのまま押す", "click": "マウスで押す", "enter": "Enter キー"}.get(screen.last_press, "?"), "）")
        except Exception as e:
            r["dialed"] = False
            r["error"] = f"{type(e).__name__}: {e}"[:200]
            print("発信できませんでした：", r["error"])
            rounds.append(r)
            continue
        last = None
        while time.monotonic() - t0 < WATCH:
            now = (screen.state(), screen.timer())
            if now != last:
                r["states"].append({"at_s": round(time.monotonic() - t0, 1), "state": now[0], "timer": now[1]})
                print(f"  {time.monotonic() - t0:5.1f}秒：状態={now[0]} 通話の時間={now[1]}")
                last = now
            time.sleep(0.2)
        try:
            screen.hang_up()
            for _ in range(25):
                if screen.state() == "idle":
                    break
                time.sleep(0.2)
            r["hung_up"] = screen.state() == "idle"
        except Exception as e:
            r["hung_up"] = False
            r["hang_up_error"] = f"{type(e).__name__}: {e}"[:200]
        print("  切れたか：", "はい" if r.get("hung_up") else "いいえ（Rakuten Link で手で切ってください）")
        heard = input("  ご家族の電話は鳴りましたか？（y／n／s＝分からない）\n> ").strip().lower()[:1]
        r["family_phone_rang"] = heard
        rounds.append(r)
        time.sleep(3)
    result = {"at": datetime.now().isoformat(timespec="seconds"), "to_last4": number[-4:], "rounds": rounds,
              "summary": {"dialed": sum(1 for r in rounds if r.get("dialed")), "hung_up": sum(1 for r in rounds if r.get("hung_up")),
                          "rang": sum(1 for r in rounds if r.get("family_phone_rang") == "y"), "of": len(rounds)}}
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果を {RESULT} に書きました。このファイル（phone_dial_result.json）を送ってください。")


if __name__ == "__main__":
    main()
