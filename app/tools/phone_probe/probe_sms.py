"""確かめの道具：Rakuten Link でショートメールを1通送れるか、送れたかを画面から読めるか（docs/PHONE.md 3章の R-9）。持ち主 CL-3。

管理者の PC で、screen_uia.py・probe_screen.py と同じフォルダに置いて動かす：
    py probe_sms.py

- 送る相手は、ご家族の携帯だけ。番号はその場で打ち、ファイルにも結果にも残さない（結果には下4けただけ）。
- 送る文は決まった文 TEXT だけ。
- 送ったあと、送ったメッセージのまわりの画面の部品を読む（決まった画面の言葉と部品の名前だけを写す＝probe_screen と同じ決まり。
  送った決まった文と「送信済み」などの表示の候補は写す）。結果は phone_sms_result.json と rakuten_link_screen_sms_sent.txt。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent
TEXT = "SalesCall 試験の送信です（返信不要）"
STATUS_WORDS = {"送信済み", "送信中", "送信失敗", "未送信", "再送信", "再送", "既読", "送信できませんでした", "SMS", "エラー", "失敗"}


def main() -> None:
    sys.path.insert(0, str(OUT))
    repo_src = OUT.parents[1] / "src"   # リポジトリの中（app/tools/phone_probe）なら、本物の部品を読む
    if (repo_src / "salescall" / "agent" / "phones" / "screen_uia.py").exists():
        sys.path.insert(0, str(repo_src / "salescall" / "agent" / "phones"))
    try:
        import probe_screen
        import screen_uia
    except ImportError:
        sys.exit("screen_uia.py と probe_screen.py を、このフォルダに置いてください。")
    probe_screen.UI_WORDS |= STATUS_WORDS | {TEXT}
    screen = screen_uia.UiaScreen()
    number = input("ご家族の携帯の番号を、0から始まる数字だけで打って Enter（ファイルには残しません）\n> ").strip()
    if not screen_uia.DOMESTIC.fullmatch(number):
        sys.exit("番号は 0 から始まる数字だけ（10けたか11けた）で打ってください。")
    input(f"これから「{TEXT}」を1通送ります。Rakuten Link を開いたまま（最小化しない）にして、Enter を押してください。"
          "送り終わるまで、マウスとキーボードにさわらないでください。")
    result: dict = {"at": datetime.now().isoformat(timespec="seconds"), "to_last4": number[-4:], "text": TEXT}
    t0 = time.monotonic()
    try:
        screen.send_sms(number, TEXT)
        result["pressed_send"] = True
    except Exception as e:
        result["pressed_send"] = False
        result["error"] = f"{type(e).__name__}: {e}"[:200]
        print("送れませんでした：", result["error"])
    result["seconds"] = round(time.monotonic() - t0, 1)
    time.sleep(3)
    lines: list[str] = []
    try:
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(**screen_uia.SCREEN["main"])
        part = win.child_window(**screen_uia.SCREEN["messages"])
        probe_screen.walk(part.wrapper_object() if part.exists(timeout=0) else win.wrapper_object(), 0, lines)
    except Exception as e:
        lines.append(f"読めませんでした：{type(e).__name__}")
    (OUT / "rakuten_link_screen_sms_sent.txt").write_text("\n".join(lines), encoding="utf-8")
    result["family_received"] = input("\nご家族の携帯に、ショートメールが届きましたか？（y／n／s＝分からない）\n> ").strip().lower()[:1]
    (OUT / "phone_sms_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n結果を phone_sms_result.json と rakuten_link_screen_sms_sent.txt に書きました。2つとも、中を見てから送ってください。")


if __name__ == "__main__":
    main()
