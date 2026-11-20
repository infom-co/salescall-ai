"""Rakuten Link デスクトップ版（2.1.0）の画面を操作する本物の口（`rakuten_link.Screen` の形。docs/PHONE.md 2-1。持ち主 CL-3）。

画面の部品の見分け方は `SCREEN` の1か所だけ（2026-11-20 に管理者の PC で読んだもの＝docs/phone_probe/rakuten_link_screen.md）。
Rakuten Link の版が上がって見分けられなくなったら、この表だけを直す。見分けられないときは、かけない側（`dial` は例外・
`state` は "unknown"＝通話中とみなす）。
**ほかの部品を読まない**（確かめの道具 probe_dial.py と一緒に、このファイルだけで管理者の PC で動く）。pywinauto は呼ぶときに読む。
"""

from __future__ import annotations

import re
import time
from typing import Any

SCREEN: dict[str, dict[str, str]] = {
    "main": {"title": "Rakuten Link", "class_name": "Chrome_WidgetWin_1"},            # いつもの窓
    "call": {"title": "Rakuten Link Call", "class_name": "Chrome_WidgetWin_1"},       # 通話のあいだだけ出る窓
    "calls_tab": {"auto_id": "calls_tab_btn", "control_type": "Button"},             # 「通話」のタブ
    "dialpad": {"auto_id": "dialpad_btn", "control_type": "Button"},                 # ダイヤルパッドを開く
    "number": {"auto_id": "digits-only", "control_type": "Edit"},                    # 番号を入れる欄
    "dial": {"auto_id": "dialpad_call_btn", "control_type": "Button"},               # 発信
    "call_ui": {"auto_id": "call-ui", "control_type": "Document"},                   # 通話の窓の中身
    "hang_up": {"title": "通話を終了", "control_type": "Button"},                     # 通話を終了
}
TIMER = re.compile(r"^\d{1,2}:\d{2}$")      # 通話の時間の表示（例「00:02」）
# ⏳ スマホとの連携が切れたときの表示（スマホの Wi-Fi を切って読む＝2-6）。分かるまでは、いつもの窓があれば連携しているとみなす
LINK_LOST = re.compile(r"接続されていません|接続が切れ|オフライン|スマートフォンと接続")


DOMESTIC = re.compile(r"0\d{9,10}")   # 入れてよい番号の形（国内の数字だけ）。形を変えるのはサーバーの番号の正規化の1か所（core/phone.py の national）


class UiaScreen:
    """Windows の UI Automation（pywinauto の uia）で Rakuten Link を操作する。"""

    def __init__(self) -> None:
        from pywinauto import Desktop
        self._desktop = Desktop(backend="uia")
        self.last_press: str | None = None   # 最後の発信で、どの押し方で通話の窓が出たか（確かめの記録）

    # ── 窓 ──
    def _win(self, key: str) -> Any | None:
        w = self._desktop.window(**SCREEN[key])
        return w if w.exists(timeout=0) else None

    def _part(self, win: Any, key: str) -> Any | None:
        p = win.child_window(**SCREEN[key])
        return p if p.exists(timeout=0) else None

    @staticmethod
    def _value(field: Any) -> str:
        """欄に入っている文字（見比べるだけ。形は変えない）。"""
        try:
            return (field.get_value() or "").strip()
        except Exception:
            return (field.window_text() or "").strip()

    @staticmethod
    def _paste(field: Any, text: str) -> None:
        """クリップボードを使って貼り付ける。もとのクリップボードの文字は、貼り付けたあとに戻す。"""
        import win32clipboard as cb
        cb.OpenClipboard()
        try:
            try:
                old = cb.GetClipboardData(cb.CF_UNICODETEXT)
            except Exception:
                old = None
            cb.EmptyClipboard()
            cb.SetClipboardText(text, cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()
        try:
            field.set_focus()
            field.type_keys("^a^v", with_spaces=False)
            time.sleep(0.2)
        finally:
            cb.OpenClipboard()
            try:
                cb.EmptyClipboard()
                if old is not None:
                    cb.SetClipboardText(old, cb.CF_UNICODETEXT)
            finally:
                cb.CloseClipboard()

    @staticmethod
    def _press(part: Any) -> None:
        """押す。マウスを動かさない押し方（Invoke）を先に試し、だめならクリック。"""
        try:
            part.invoke()
        except Exception:
            part.click_input()

    # ── rakuten_link.Screen の形 ──
    def linked(self) -> bool | None:
        main = self._win("main")
        if main is None:
            return False
        try:
            texts = [t.window_text() for t in main.descendants(control_type="Text")[:400]]
        except Exception:
            return None
        return not any(LINK_LOST.search(t or "") for t in texts)

    def state(self) -> str:
        """通話の窓が無い＝idle。窓があって、通話の時間が 00:00 より進んでいる＝in_call。窓があってそれ以外＝calling。
        ⏳ 呼び出し中に通話の時間が進むか（つながってから数えるか）は R-6 で確かめて、ここを直す。"""
        try:
            call = self._win("call")
            if call is None:
                return "idle" if self._win("main") is not None else "unknown"
            timer = self.timer(call)
            return "in_call" if timer and timer not in ("00:00", "0:00") else "calling"
        except Exception:
            return "unknown"

    def timer(self, call: Any | None = None) -> str | None:
        call = call or self._win("call")
        if call is None:
            return None
        ui = self._part(call, "call_ui") or call
        for t in ui.descendants(control_type="Text"):
            text = (t.window_text() or "").strip()
            if TIMER.match(text):
                return text
        return None

    def dial(self, phone: str) -> None:
        """番号（国内の形の数字だけ。サーバーの正規化が作ったもの）を入れて発信を押す。押せなければ例外（rakuten_link が
        CallNotPlaced にする）。番号の形はここでは変えない（違う形なら入れずに断る）。"""
        number = (phone or "").strip()
        if not DOMESTIC.fullmatch(number):
            raise ValueError("番号は、国内の形の数字だけで渡してください（形を変えるのはサーバーの1か所）")
        main = self._win("main")
        if main is None:
            raise RuntimeError("Rakuten Link の窓が無い")
        if self._win("call") is not None:
            raise RuntimeError("もう通話の窓が出ている")
        field = self._part(main, "number")
        if field is None:
            tab = self._part(main, "calls_tab")
            if tab is not None:
                self._press(tab)
            pad = self._part(main, "dialpad")
            if pad is None:
                raise RuntimeError("ダイヤルパッドのボタンが見つからない")
            self._press(pad)
            for _ in range(20):
                field = self._part(main, "number")
                if field is not None:
                    break
                time.sleep(0.1)
        if field is None:
            raise RuntimeError("番号を入れる欄が見つからない")
        # 番号は貼り付けで入れる（管理者が確かめた：貼り付けると発信のボタンがピンク＝使える状態になる。欄へ値を直に書き込むと、
        # 画面には出ても Rakuten Link の中では入っていない扱いで、ボタンが使えなかった＝2026-11-20）。入らなければキーボードで打つ
        self._paste(field, number)
        if self._value(field) != number:
            field.set_focus()
            field.type_keys("^a{BACKSPACE}", with_spaces=False)
            field.type_keys(number, with_spaces=False, pause=0.03)
        if self._value(field) != number:
            raise RuntimeError("番号を入れられなかった")
        button = self._part(main, "dial")
        if button is None:
            raise RuntimeError("発信のボタンが見つからない")
        for _ in range(20):   # 使える状態（ピンク）になるのを待つ
            try:
                if button.is_enabled():
                    break
            except Exception:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("番号を入れても、発信のボタンが使える状態にならなかった")
        # 押す：マウスを動かさない押し方 → マウスで押す → 番号の欄で Enter の順に試し、通話の窓が出たら終わり（どれで出たかを残す）
        tries = (("invoke", lambda: button.invoke()), ("click", lambda: button.click_input()),
                 ("enter", lambda: (field.set_focus(), field.type_keys("{ENTER}"))))
        self.last_press = None
        for name, press in tries:
            try:
                press()
            except Exception:
                continue
            for _ in range(30):
                if self._win("call") is not None:
                    self.last_press = name
                    return
                time.sleep(0.1)
        raise RuntimeError("発信を押しても、通話の窓が出なかった")

    def hang_up(self) -> None:
        call = self._win("call")
        if call is None:
            return
        button = self._part(call, "hang_up")
        if button is None:
            raise RuntimeError("「通話を終了」が見つからない")
        self._press(button)
