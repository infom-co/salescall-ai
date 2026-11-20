"""Rakuten Link デスクトップ版（2.1.0）の画面を操作する本物の口（`rakuten_link.Screen` の形。docs/PHONE.md 2-1。持ち主 CL-3）。

画面の部品の見分け方は `SCREEN` の1か所だけ（2026-11-20 に管理者の PC で読んだもの＝docs/phone_probe/rakuten_link_screen.md）。
Rakuten Link の版が上がって見分けられなくなったら、この表だけを直す。見分けられないときは、かけない側（`dial` は例外・
`state` は "unknown"＝通話中とみなす）。
**ほかの部品を読まない**（確かめの道具 probe_dial.py と一緒に、このファイルだけで管理者の PC で動く）。pywinauto は呼ぶときに読む。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
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
    "new_message": {"auto_id": "create_new_btn", "control_type": "Button"},         # 新しいメッセージ（＋）
    "new_message_item": {"auto_id": "new_message_btn", "control_type": "Button"},   # ＋のポップアップの「メッセージ」（「グループ」は new_group_message_btn）
    "to_box": {"auto_id_re": r"^mat-chip-list-input-\d+$", "control_type": "Edit"},  # 宛先の欄（Id の数は変わりうる）
    "sms_note": {"auto_id": "message_sms_learn_more_btn", "control_type": "Hyperlink"},   # ショートメールで送られる知らせ
    "body": {"title": "メッセージを入力", "control_type": "Text"},                    # 本文の欄（入力欄の形では出ない。押して貼り付ける）
    "send": {"auto_id": "message_send_btn", "control_type": "Button"},             # 送信
    "messages": {"auto_id": "chat-msg-container", "control_type": "Document"},     # 送ったメッセージが並ぶ所
}
TIMER = re.compile(r"^\d{1,2}:\d{2}$")      # 通話の時間の表示（例「00:02」）
# ⏳ スマホとの連携が切れたときの表示（スマホの Wi-Fi を切って読む＝2-6）。分かるまでは、いつもの窓があれば連携しているとみなす
LINK_LOST = re.compile(r"接続されていません|接続が切れ|オフライン|スマートフォンと接続")


DOMESTIC = re.compile(r"0\d{9,10}")   # 入れてよい番号の形（国内の数字だけ）。形を変えるのはサーバーの番号の正規化の1か所（core/phone.py の national）


class NotRegistered(Exception):
    """ショートメールの宛先が、Rakuten Link の連絡先に登録されていない（送っていない。知らせはチャットワークへ回し、登録を頼む）。"""


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
    def _clipboard_paste(text: str, press: Callable[[], None], landed: Callable[[], bool], wait: float = 3.0) -> bool:
        """クリップボードに text を入れて貼り付け（press）、入ったこと（landed）を確かめてから、もとのクリップボードに戻す（貼り付けの1か所）。
        ⛔ 2026-11-20：貼り付けの合図のあと 0.3秒で戻していたら、Rakuten Link がそのあとにクリップボードを読み、もとの中身
        （管理者がコピーしていた文）がショートメールの本文として送られた。⇒ 入ったことを確かめるまで戻さない。入らなければ偽。"""
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
        ok = False
        try:
            press()
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if landed():
                    ok = True
                    break
                time.sleep(0.1)
        finally:
            cb.OpenClipboard()
            try:
                cb.EmptyClipboard()
                if old is not None:
                    cb.SetClipboardText(old, cb.CF_UNICODETEXT)
            finally:
                cb.CloseClipboard()
        return ok

    def _paste(self, field: Any, text: str) -> None:
        """欄へ貼り付ける（欄の中身が text になったのを確かめてから、クリップボードを戻す）。"""
        def press() -> None:
            field.set_focus()
            field.type_keys("^a^v", with_spaces=False)
        self._clipboard_paste(text, press, lambda: self._value(field) == text)

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
        # 「＋」（新しいメッセージ）を押すと「メッセージ」「グループ」のメニューが出るので、メニューの中の「メッセージ」を選ぶ
        # （部品の名前 new_message_btn で選ぶ＝左のタブと取り違えない。2026-11-20 管理者が教えてくれ、ポップアップを読んで確かめた）
        # ポップアップの項目は、そのまま押す形（Invoke）では反応しなかった（2026-11-20）。マウスで押す → そのまま押す → Enter の順に試し、
        # 宛先の欄が出たかで確かめる
        choice = None
        for _ in range(20):
            choice = self._part(main, "new_message_item")
            if choice is not None:
                break
            time.sleep(0.1)
        if choice is None:
            raise RuntimeError("＋のポップアップの「メッセージ」が見つからない")
        box = None
        for press in (lambda: choice.click_input(), lambda: choice.invoke(), lambda: (choice.set_focus(), choice.type_keys("{ENTER}"))):
            try:
                press()
            except Exception:
                continue
            for _ in range(20):
                box = self._part(main, "to_box")
                if box is not None:
                    break
                time.sleep(0.1)
            if box is not None:
                break
        if box is None:
            raise RuntimeError("宛先の欄が見つからない")
        self._paste(box, number)
        # 番号を入れると、連絡先に登録された番号なら、その下に候補（auto_option_<数>）が出る。それを押すと宛先になる
        # （2026-11-20 管理者が教えてくれ、候補を読んで確かめた）。登録されていない番号は「連絡先に登録」を押さないと宛先にならないが、
        # プログラムはスマホの連絡先を書き換えない＝送らずに NotRegistered（知らせの送り先は、契約者が前もって連絡先に登録しておく）。
        # 候補がちょうど1つのときだけ押す（2つ以上なら、違う方へ送らないよう送らない）
        options: list[Any] = []
        for _ in range(30):
            options = self._all_with_id(main, r"^auto_option_\d+$")
            if options:
                time.sleep(0.3)   # 候補が出そろうのを少し待つ
                options = self._all_with_id(main, r"^auto_option_\d+$")
                break
            time.sleep(0.1)
        if not options:
            raise NotRegistered("この番号は Rakuten Link の連絡先に登録されていない（候補が出なかった）")
        if len(options) > 1:
            raise RuntimeError(f"候補が{len(options)}つ出た（違う方へ送らないよう、送らない）")
        try:
            options[0].click_input()
        except Exception:
            options[0].invoke()
        for _ in range(30):
            if self._has_recipient(main):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("候補を押しても、宛先が入らなかった")
        body = self._part(main, "body")
        if body is None:
            raise RuntimeError("本文の欄が見つからない")
        send = self._part(main, "send")
        if send is None:
            raise RuntimeError("送信のボタンが見つからない")
        composer = send.parent().parent()   # 本文の欄と送信のボタンが入っているまとまり（送ったメッセージの一覧とは別）

        def typed() -> bool:   # 本文の欄に、決めた文がそのまま入ったか（ほかの文が入っていないか）
            try:
                return any((t.window_text() or "").strip() == text.strip() for t in composer.descendants(control_type="Text"))
            except Exception:
                return False

        body.click_input()
        from pywinauto.keyboard import send_keys
        if not self._clipboard_paste(text, lambda: send_keys("^v"), typed):
            body.click_input()
            send_keys("^a{DELETE}")   # 違う文が入っていれば消す（送らない）
            raise RuntimeError("本文の欄に、決めた文が入ったことを確かめられなかった（送らずに止めた）")
        self._press(send)

    @staticmethod
    def _all_with_id(win: Any, pattern: str) -> list[Any]:
        rx = re.compile(pattern)
        return [c for c in win.descendants() if rx.match(c.element_info.automation_id or "")]

    @staticmethod
    def _has_recipient(main: Any) -> bool:
        """宛先が入ったか：「Contact selection」（宛先が無いときは Group、入ると List）の中に、宛先の項目があるか。"""
        for c in main.descendants(title="Contact selection"):
            if any(k.element_info.control_type == "ListItem" for k in c.children()):
                return True
        return False
