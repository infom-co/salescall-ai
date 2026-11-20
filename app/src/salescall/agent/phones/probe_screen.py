"""確かめの道具：Rakuten Link の画面の部品の名前を読む（docs/PHONE.md 2-1 の `SCREEN` の材料・R-5・R-6 の前の準備）。持ち主 CL-3。

**読むだけ**（押さない・入力しない）。管理者の PC で、このファイルだけを動かす：
    py -m pip install pywinauto==0.6.9
    py probe_screen.py            ← いつもの画面（電話をかけていないとき）
    py probe_screen.py calling    ← ご家族の携帯へ電話をかけて、呼び出し中・通話中のときに
    py probe_screen.py dialpad    ← 通話の画面で「ダイヤルパッド」（テンキーの絵のボタン）を押して、番号を入れる画面を出したときに（番号は入れない）

結果は同じフォルダの rakuten_link_screen_<名前>.txt。画面に出ている番号・名前（通話の履歴など）が写らないよう、
4けた以上の数字は下4けただけにし、通話の履歴の行は中を写さず、文字の長い表示は先頭だけにする。ブラウザの窓は読まない。それでも、送る前に中を見て、個人の情報が無いかを確かめる。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
TITLE = re.compile(r"Rakuten\s*Link", re.I)
DIGITS = re.compile(r"\d[\d\-\s]{3,}\d")


def mask(text: str) -> str:
    """番号は下4けただけ・長い文字は先頭だけ（個人の情報を写さない）。"""
    text = DIGITS.sub(lambda m: "…" + re.sub(r"\D", "", m.group())[-4:], text or "")
    return text if len(text) <= 24 else text[:24] + "…"


HISTORY = re.compile(r"発信\(|着信\(|不在着信|通話時間")   # 通話の履歴・連絡先の行（名前が入るので、中を写さない）


def walk(el, depth: int, lines: list[str], limit: int = 12) -> None:
    info = el.element_info
    if info.control_type in ("Button", "ListItem") and HISTORY.search(info.name or ""):
        lines.append("  " * depth + "[" + str(info.control_type) + "]（通話の履歴の行。名前・番号は写さない）")
        return
    lines.append("  " * depth + f"[{info.control_type}] name={mask(info.name)!r} id={info.automation_id!r} class={info.class_name!r}"
                 + (" (押せる)" if info.control_type in ("Button", "MenuItem", "ListItem", "TabItem") else ""))
    if depth >= limit:
        return
    for child in el.children():
        walk(child, depth + 1, lines, limit)


def main() -> None:
    try:
        from pywinauto import Desktop
    except ImportError:
        sys.exit("pywinauto が入っていません。次を打ってから、もう一度動かしてください：\n  py -m pip install pywinauto==0.6.9")
    label = (sys.argv[1] if len(sys.argv) > 1 else "idle").strip() or "idle"
    # Rakuten Link の窓だけ（ブラウザの「Rakuten Link」のページの窓は読まない＝ほかのタブの名前を写さない）
    wins = [w for w in Desktop(backend="uia").windows()
            if TITLE.match(w.window_text() or "") and w.element_info.class_name == "Chrome_WidgetWin_1" and "|" not in (w.window_text() or "")
            and not any(c.element_info.class_name == "BrowserRootView" for c in w.children())]
    if not wins:
        sys.exit("Rakuten Link の窓が見つかりません。Rakuten Link を開いて（最小化しないで）から、もう一度動かしてください。")
    lines = [f"窓の数：{len(wins)}"]
    for w in wins:
        lines.append(f"\n=== 窓：{mask(w.window_text())} ===")
        walk(w, 0, lines)
    path = OUT / f"rakuten_link_screen_{re.sub(r'[^a-z0-9_]', '', label.lower()) or 'idle'}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{path} に書きました（部品 {len(lines)} 行）。中を見て、個人の情報が無いことを確かめてから送ってください。")


if __name__ == "__main__":
    main()
