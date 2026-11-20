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
    # ショートメール（R-9。2026-11-20 に読んだ＝docs/phone_probe/rakuten_link_screen.md）
    "message_tab": {"title": "メッセージ", "control_type": "Button"},                # 「メッセージ」のタブ
    "new_message": {"auto_id": "create_new_btn", "control_type": "Button"},         # 新しいメッセージ
    "to_box": {"auto_id_re": r"^mat-chip-list-input-\d+$", "control_type": "Edit"},  # 宛先の欄（Id の数は変わりうる）
    "to_chips": {"title": "Contact selection", "control_type": "List"},            # 入った宛先の一覧
    "sms_note": {"auto_id": "message_sms_learn_more_btn", "control_type": "Hyperlink"},   # ショートメールで送られる知らせ
    "body": {"title": "メッセージを入力", "control_type": "Text"},                    # 本文の欄（入力欄の形では出ない。押して貼り付ける）
    "send": {"auto_id": "message_send_btn", "control_type": "Button"},             # 送信
    "messages": {"auto_id": "chat-msg-container", "control_type": "Document"},     # 送ったメッセージが並ぶ所
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
        spec = dict(SCREEN[key])
        if "auto_id_re" in spec:   # Id の数が変わりうる部品は、形で探す
            pattern = re.compile(spec.pop("auto_id_re"))
            for c in win.descendants(control_type=spec.get("control_type")):
                if pattern.match(c.element_info.automation_id or ""):
                    return c
            return None
        p = win.child_window(**spec)
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
        """通話の窓が無い＝idle。窓があって、通話の時間が出ている＝in_call（つながった）。窓があって、時間が出ていない＝calling（呼び出し中）。
        R-6（2026-11-20・管理者の PC）：呼び出し中は通話の時間が出ず、お客様が出た瞬間に「00:00」が出て数え始める。"""
        try:
            call = self._win("call")
            if call is None:
                return "idle" if self._win("main") is not None else "unknown"
            timer = self.timer(call)
            return "in_call" if timer else "calling"
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

    # ── ショートメール（R-9。⏳ 送れたかの表示は、確かめで本物を読んでから決める）──
    def send_sms(self, number: str, text: str) -> None:
        """国内の形の番号へ、ショートメールを1通送る（宛先を入れる → 本文を貼り付ける → 送信）。送れなければ例外。
        送れたかの確かめ（画面の「送信済み」などの表示）は ⏳ R-9 で本物を読んでから足す。"""
        if not DOMESTIC.fullmatch((number or "").strip()):
            raise ValueError("番号は、国内の形の数字だけで渡してください")
        if not text.strip():
            raise ValueError("本文が空")
        main = self._win("main")
        if main is None:
            raise RuntimeError("Rakuten Link の窓が無い")
        if self._win("call") is not None:
            raise RuntimeError("通話中は送らない（⏳ R-9 で通話中に送れるかを確かめる）")
        for key in ("message_tab", "new_message"):
            part = self._part(main, key)
            if part is None:
                raise RuntimeError(f"{key} が見つからない")
            self._press(part)
            time.sleep(0.5)
        box = None
        for _ in range(20):
            box = self._part(main, "to_box")
            if box is not None:
                break
            time.sleep(0.1)
        if box is None:
            raise RuntimeError("宛先の欄が見つからない")
        self._paste(box, number)
        box.type_keys("{ENTER}")
        time.sleep(1.0)
        chips = self._part(main, "to_chips")
        if chips is None or not chips.children():
            raise RuntimeError("宛先が入らなかった")
        body = self._part(main, "body")
        if body is None:
            raise RuntimeError("本文の欄が見つからない")
        body.click_input()
        self._paste_focused(text)
        send = self._part(main, "send")
        if send is None:
            raise RuntimeError("送信のボタンが見つからない")
        self._press(send)

    @staticmethod
    def _paste_focused(text: str) -> None:
        """いまの入力の場所へ貼り付ける（本文の欄は入力欄の形で出ないので、押したあとに貼り付ける）。"""
        import win32clipboard as cb
        from pywinauto.keyboard import send_keys
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
            send_keys("^v")
            time.sleep(0.3)
        finally:
            cb.OpenClipboard()
            try:
                cb.EmptyClipboard()
                if old is not None:
                    cb.SetClipboardText(old, cb.CF_UNICODETEXT)
            finally:
                cb.CloseClipboard()
