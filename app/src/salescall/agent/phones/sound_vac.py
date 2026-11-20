"""音の装置の本物の口（`rakuten_link.Sound` の形。docs/PHONE.md 2-2。持ち主 CL-3）。

Virtual Audio Cable の2本の線を使う（2026-11-20 に管理者の PC で確かめた R-2〜R-4）：
- Line 1：Rakuten Link のスピーカーの出口 ＝ ここから録る（お客様の声）。
- Line 2：ここへ流す ＝ Rakuten Link のマイクの入口（AI の声・録音）。
装置の形は 48kHz・16bit・1ch。20ms ごとに録った声を渡し、流す声は順番待ちに積んで、装置の流れの中で少しずつ流す
（`play` は待たずに返す＝PC の部品のやり取りを止めない）。sounddevice は使うときに読む（試験の機械には要らない）。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

RATE = 48000
FRAME = RATE // 50            # 20ms
CUSTOMER_LINE = "Line 1"      # 録る（お客様の声）
AI_LINE = "Line 2"            # 流す（AI の声）


def _device(sd: Any, name: str, kind: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
        if name.lower() in d["name"].lower() and ch > 0:
            return i
    raise RuntimeError(f"音の装置「{name}」（{kind}）が見つからない（Virtual Audio Cable の線が2本あるかを確かめる）")


class VacSound:
    rate = RATE

    def __init__(self) -> None:
        import sounddevice as sd
        self._sd = sd
        self._queue: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()
        self._on_frame: Callable[[bytes], None] | None = None
        self._in = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                     device=_device(sd, CUSTOMER_LINE, "input"), callback=self._heard)
        self._out = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                       device=_device(sd, AI_LINE, "output"), callback=self._speak)
        self._in.start()
        self._out.start()

    # ── 装置の流れ（sounddevice が呼ぶ。止めない・待たない）──
    def _heard(self, data: Any, frames: int, time_info: Any, status: Any) -> None:
        sink = self._on_frame
        if sink is not None:
            try:
                sink(bytes(data))
            except Exception:
                pass   # 渡し先の誤りで、音の流れを止めない

    def _speak(self, out: Any, frames: int, time_info: Any, status: Any) -> None:
        need = frames * 2
        chunk = bytearray()
        with self._lock:
            while self._queue and len(chunk) < need:
                head = self._queue.popleft()
                take = need - len(chunk)
                chunk += head[:take]
                if len(head) > take:
                    self._queue.appendleft(head[take:])
            if not self._queue:
                self._drained.set()
        out[:] = bytes(chunk) + b"\x00" * (need - len(chunk))

    # ── rakuten_link.Sound の形 ──
    def listen(self, on_frame: Callable[[bytes], None]) -> None:
        self._on_frame = on_frame

    def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            self._queue.append(pcm)
            self._drained.clear()

    def play_wait(self, pcm: bytes, limit: float = 30.0) -> None:
        self.play(pcm)
        self._drained.wait(limit)

    def stop(self) -> None:
        with self._lock:
            self._queue.clear()
            self._drained.set()

    def close(self) -> None:
        for s in (self._in, self._out):
            try:
                s.stop()
                s.close()
            except Exception:
                pass
