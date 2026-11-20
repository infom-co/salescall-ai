"""Rakuten Link デスクトップ版の電話の部品（docs/PHONE.md 2章。持ち主 CL-3）。形は `salescall.link.phone.Phone`。

この部品は「流れ」だけを持つ。画面を押す所（`Screen`）と音の装置（`Sound`）は差し替えられる形にし、本物は管理者の PC で
確かめてから入れる（⏳ 画面の部品の名前＝R-5・R-6、音の装置＝R-2〜R-4）。試験は、にせの画面とにせの音で流れを確かめる。

- 判断を持たない：かけてよいか・番号のせいか回線のせいかは、サーバーの1か所が決める。分からないときは「かけない側」
  （`in_call` は真・`start_call` は CallNotPlaced・`test_ring` は None）。
- 声の形を変えるのは `audio.resample` の1か所。電話の音を聞き分けるのは `tones.Cadence` の1か所。
- ⏳ **かける番号の形**：Rakuten Link には国内の形の数字（0 から始まる）を入れる。形を変えてよいのはサーバーの番号の正規化の1か所
  （core/phone.py の national＝機械の見張り Q1）なので、サーバーから国内の形も渡してもらう（CL-2 へお願い 60）。届くまで、国際の形（頭に「＋」）で
  渡された番号は画面の口が断る（CallNotPlaced＝かけない側）。
- ⏳ 出来事の名前（ringing・busy・answered・ended）と、サーバーから届く声の形（`FROM_SERVER_RATE`）は CL-1 と決める
  （CL-3 の連絡帳 4-2 の7）。おわびの録音の届け方は CL-2 と決める（お願い 58 の (2)(b)）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from salescall.agent.phones import audio, tones
from salescall.link.phone import CallNotPlaced
from salescall.link.protocol import ANSWER_BY, APOLOGY_WAIT

FROM_SERVER_RATE = 24000   # ⏳ CL-1 と決める（会話AIの出力の形のまま送ってもらう案）
DIAL_WAIT = 8.0            # 番号を入れてから、画面が「かけている」になるまで待つ長さ（秒）
REPLY_LISTEN = 6.0         # おわびのひとつ目のあと、お客様の返事を待つ長さ（秒）
QUIET_END = 0.8            # お客様の声がこれだけ途切れたら、返事が終わったとみなす（秒）
VOICE_RMS = 300.0          # これより大きい切れ目を「声がある」とみなす


class Screen(Protocol):
    """Rakuten Link の画面を操作する口（本物は UI Automation。⏳ 部品の名前は R-5・R-6 で決める）。"""
    def linked(self) -> bool | None: ...          # スマホと連携しているか（分からなければ None）
    def state(self) -> str: ...                   # "idle"・"calling"・"in_call"・"unknown"
    def dial(self, phone_e164: str) -> None: ...   # 番号を入れて発信を押す（押せなければ例外）
    def hang_up(self) -> None: ...


class Sound(Protocol):
    """音の装置（本物は Virtual Audio Cable の Line 1 から録り、Line 2 へ流す。⏳ R-2〜R-4）。"""
    rate: int
    def listen(self, on_frame: Callable[[bytes], None]) -> None: ...   # 装置の形の 20ms ごとの声を渡し続ける
    def play(self, pcm: bytes) -> None: ...                             # 装置の形の声を流す（待たずに返す）
    def play_wait(self, pcm: bytes) -> None: ...                        # 流し終えるまで待つ
    def stop(self) -> None: ...                                         # 流している声をやめる


Recordings = Callable[[str | None], tuple[bytes, bytes] | None]   # 声の型 → （ひとつ目・ふたつ目）の録音（装置の形）


class RakutenLink:
    def __init__(self, screen: Screen, sound: Sound, recordings: Recordings, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.screen, self.sound, self.recordings = screen, sound, recordings
        self.clock, self.sleep = clock, sleep
        self._send_audio: Callable[[bytes], None] = lambda b: None
        self._send_event: Callable[[dict[str, Any]], None] = lambda e: None
        self._lock = threading.Lock()
        self._voice: str | None = None
        self._framer = audio.Framer()
        self._cadence = tones.Cadence(sound.rate)
        self._heard: list[float] = []           # 声のあった切れ目の時刻（おわびの返事を待つため）
        self._last_state = "idle"
        sound.listen(self._on_frame)

    # ── Phone の形 ──────────────────────────────────────────
    def in_call(self) -> bool:
        try:
            return self.screen.state() != "idle"   # "unknown" も通話中とみなす（かけない側）
        except Exception:
            return True

    def start_call(self, phone_e164: str, voice_type: str | None = None) -> None:
        with self._lock:
            self._voice = voice_type
            self._place(phone_e164)

    def hang_up(self) -> None:
        self.sound.stop()
        self.screen.hang_up()

    def test_ring(self, phone_e164: str) -> bool | None:
        """テスト番号へかけ、呼び出しの音・話し中の音が聞こえたら真（回線から発信できた）。画面が「かけている」にならない＝偽。
        期限のうちに見分けられなければ None。⏳ 電話網のお知らせの声（電源オフ・圏外）の見分けは R-6 で見本を録ってから。"""
        with self._lock:
            self._cadence = tones.Cadence(self.sound.rate)
            try:
                self._place(phone_e164)
            except CallNotPlaced:
                return False
            end = self.clock() + ANSWER_BY - 3
            try:
                while self.clock() < end:
                    if self._tone in ("ringback", "busy"):
                        return True
                    self.sleep(0.1)
                return None
            finally:
                self.hang_up()

    def play_apology_and_hang_up(self) -> None:
        """おわびのひとつ目 → お客様の返事を待つ → ふたつ目 → 切る（APOLOGY_WAIT のうちに）。録音が無ければ、すぐ切る
        （黙って待たせない）。通話中でなければ何もしない。"""
        if not self.in_call():
            return
        deadline = self.clock() + APOLOGY_WAIT - 5
        rec = None
        try:
            rec = self.recordings(self._voice)
        except Exception:
            rec = None
        try:
            if rec:
                ask, bye = rec
                self.sound.stop()
                self.sound.play_wait(ask)
                self._wait_reply(min(deadline, self.clock() + REPLY_LISTEN))
                if self.clock() < deadline:
                    self.sound.play_wait(bye)
        finally:
            self.hang_up()

    def on_audio(self, send: Callable[[bytes], None]) -> None:
        self._send_audio = send

    def on_event(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send_event = send

    def play(self, pcm: bytes) -> None:
        self.sound.play(audio.resample(pcm, FROM_SERVER_RATE, self.sound.rate))

    # ── 中の流れ ─────────────────────────────────────────────
    _tone: str | None = None

    def _place(self, phone_e164: str) -> None:
        """発信する。スマホと連携していない・画面が「かけている」にならない＝CallNotPlaced（お客様の電話は鳴っていない）。"""
        if self.screen.linked() is not True:
            raise CallNotPlaced("Rakuten Link がスマホと連携していない（または分からない）")
        self._tone = None
        try:
            self.screen.dial(phone_e164)
        except Exception as e:
            raise CallNotPlaced(f"発信の操作ができなかった：{type(e).__name__}") from None
        end = self.clock() + DIAL_WAIT
        while self.clock() < end:
            if self.screen.state() in ("calling", "in_call"):
                return
            self.sleep(0.1)
        raise CallNotPlaced("発信を押しても、かけている状態にならなかった")

    def _on_frame(self, pcm: bytes) -> None:
        """装置から届いた 20ms ごとの声：サーバーへ送る形にして送り、電話の音と状態の移りを見る。"""
        if audio.rms_of(pcm) > VOICE_RMS:
            self._heard.append(self.clock())
            del self._heard[:-200]
        tone = self._cadence.push(pcm)
        if tone and tone != self._tone:
            self._tone = tone
            self._send_event({"kind": tone})          # ⏳ 名前は CL-1 と決める（ringback・busy）
        try:
            state = self.screen.state()
        except Exception:
            state = "unknown"
        if state != self._last_state:
            if state == "in_call":
                self._send_event({"kind": "answered"})
            elif state == "idle" and self._last_state in ("calling", "in_call"):
                self._send_event({"kind": "ended"})
            self._last_state = state
        for frame in self._framer.push(audio.resample(pcm, self.sound.rate, audio.TO_SERVER_RATE)):
            self._send_audio(frame)

    def _wait_reply(self, until: float) -> None:
        """お客様が話し始めて、話し終える（QUIET_END 途切れる）まで待つ。話さなければ until まで。"""
        started = self.clock()
        while self.clock() < until:
            recent = [t for t in self._heard if t >= started]
            if recent and self.clock() - recent[-1] >= QUIET_END:
                return
            self.sleep(0.05)
