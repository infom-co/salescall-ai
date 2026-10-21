"""PC の電話の部品（電話接続の担当。まだ振っていない）の形（DESIGN.md 5-5）。持ち主 CL-2（形だけ）。

PC の部品が、回線（スマホ1台）ごとに1つ作る（設定の `モジュール:名前` から。回線の ID を受け取る）。サーバーの発信の出口
（core/dial_exit.py）が「かける」と決めたときだけ、PC の部品がサーバーの頼みを受けて `start_call` を呼ぶ（I-1）。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class CallNotPlaced(Exception):
    """電話の部品が、電話をかけていないと確かに言える失敗（例：アプリが番号を受け付けなかった）。回線の異常として数える。"""


class CallWithheld(CallNotPlaced):
    """かけなかった（PC の上で緊急停止が押されている）。回線の異常ではないので、数えない。"""


class Phone(Protocol):
    def in_call(self) -> bool:
        """スマホが、いま通話の途中か（呼び出し中を含む）。分からなければ True（かけない側）。"""
        ...

    def start_call(self, phone_e164: str) -> None: ...

    def hang_up(self) -> None: ...

    def play_apology_and_hang_up(self) -> None:
        """サーバーとのつながりが通話の途中で切れたとき：あらかじめ持っている、おわびの録音を流して切る（お客様を無言で放置しない）。"""
        ...

    def on_audio(self, send: Callable[[bytes], None]) -> None:
        """お客様の声をサーバーへ送る口を受け取る（形は会話＝CL-1 と電話接続の担当が決める。どの流れから呼んでもよい）。"""
        ...

    def on_event(self, send: Callable[[dict[str, Any]], None]) -> None:
        """電話の出来事（呼び出し・応答・切れた など。中身は会話＝CL-1 と電話接続の担当が決める）をサーバーへ送る口を受け取る
        （どの流れから呼んでもよい）。"""
        ...

    def play(self, audio: bytes) -> None:
        """サーバーから届いた声（AI の声・録音）を、通話へ流す（待たずに返す＝PC の部品のやり取りを止めない）。"""
        ...
