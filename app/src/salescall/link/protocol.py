"""サーバーと PC の部品のつなぎ口の決まり（DESIGN.md 5-5。**やり取りの形はこの1か所**）。持ち主 CL-2。

- つなぐのは PC から（外向きの1本だけ。WebSocket。暗号化して相手を確かめるのはサーバーの入口＝https）。
- 文字のやり取りは JSON 1つずつ（`"t"` が種類）。通話の声と控えの中身は、バイトのひと続き（先頭1バイトの種類＋16バイトの番号＋中身）。
- **版**（`VERSION`）：つないだときに PC が渡す。サーバーと合わなければ、サーバーは断って「更新してください」と返す（その PC からは
  かけない）。やり取りの形を変えたら番号を上げる。

PC → サーバー：
  hello {v, token, lines, drive, key, emergency}   つないだとき（通行証・つないでいる回線・控えのハードディスクと鍵があるか・緊急停止の印）
  beat {lines, drive, key, emergency}              動いている合図（BEAT_EVERY 秒ごと）
  reply {id, ok, value?, error?}                   頼まれたことの答え（error：not_placed＝かけていないと確かに言える・busy・failed）
  phone_event {line, call, event, data}            電話の出来事（電話接続の担当が決める。サーバーは通話を進める部品へ渡す）
  backup_done {id, ok, error?, ask?, size?, sha256?} 控えを書いた結果（sha256・size は暗号化したファイルのもの。ask＝PC の前の作業）
  emergency {action: stop|release, reason, event?} PC の上で押した緊急停止（event＝サーバーが記録した停止の番号。解くとき）
サーバー → PC：
  welcome {tenant, pc, lines}                      通った（lines＝その契約者の、使っている回線だけ）
  refuse {why, relogin, update}                    断った（relogin＝ログインし直す・update＝更新が要る）。サーバーは切る
  request {id, op: in_call|start_call|hang_up, line, call?, phone?}
  backup_begin {id, folder, file_name, size, sha256}  控えを書く（中身は CHUNK ずつのバイト。sha256＝暗号化する前の中身）
  backup_end {id}
  prune {id, folders}                              古い控えを消す（folders＝フォルダごとに、種類・残す日数・消さないもの）
  emergency_state {event, open}                    PC の緊急停止の記録の状態（open=false＝解かれた：PC は印を消す）
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta, timezone
from typing import Any
from uuid import UUID

VERSION = 1                  # やり取りの形の版（変えたら上げる）
BEAT_EVERY = 15              # 動いている合図の間隔（秒）
REPLY_WAIT = 20              # 頼んだことの答えを待つ長さ（秒。過ぎたら「分からない」＝かけない側）
CHUNK = 1 << 20              # 控えの中身を送る1回の大きさ（1 MiB）
MAX_FRAME = CHUNK + 64       # 1回のやり取りの大きさの上限（声・控えの1回分に足りる大きさ）。縮める（compression）は使わない
                             # （声も暗号化した控えも縮まず、縮めた形が上限を少し超えて、つなぎが切れた）

AUDIO, BACKUP = 1, 2         # バイトのひと続きの種類
JST = timezone(timedelta(hours=9))   # 控えのファイルの名前の日時（サーバーの置き場の時間帯に関係なく日本時間。日本に夏時間は無い）

PC_TO_SERVER = frozenset({"hello", "beat", "reply", "phone_event", "backup_done", "emergency"})
SERVER_TO_PC = frozenset({"welcome", "refuse", "request", "backup_begin", "backup_end", "prune", "emergency_state"})
OPS = frozenset({"in_call", "start_call", "hang_up"})


class RemoteError(Exception):
    """PC の部品が、頼まれたことをできなかった（中身は、鍵・お客様の情報を含まない短い説明）。ask＝直すのが PC の前の作業なら、
    その PC の持ち主へ知らせる文（ハードディスクをつなぐ・鍵を入れる）。無ければプログラムの誤り（修正依頼）。"""

    def __init__(self, message: str, ask: str | None = None) -> None:
        super().__init__(message)
        self.ask = ask


class BadMessage(Exception):
    """決まりに合わないやり取り（受けた側は、そのやり取りを捨てて記録する。中身は書かない）。"""


def dumps(kind: str, **fields: Any) -> str:
    if kind not in PC_TO_SERVER | SERVER_TO_PC:
        raise BadMessage(f"知らない種類：{kind}")
    return json.dumps({"t": kind, **fields}, ensure_ascii=False, default=str)


def loads(text: str | bytes, allowed: frozenset[str]) -> dict[str, Any]:
    """文字のやり取りを読む（allowed＝受けてよい種類。向きが逆の種類・知らない種類は BadMessage）。"""
    try:
        got = json.loads(text)
    except (ValueError, TypeError):
        raise BadMessage("JSON ではありません") from None
    if not isinstance(got, dict) or got.get("t") not in allowed:
        raise BadMessage(f"受けられない種類：{got.get('t') if isinstance(got, dict) else type(got).__name__}")
    return got


def frame(kind: int, ident: UUID, payload: bytes) -> bytes:
    """バイトのひと続き（声・控えの中身）：種類1バイト＋番号16バイト＋中身。"""
    return bytes([kind]) + ident.bytes + payload


def unframe(data: bytes) -> tuple[int, UUID, bytes]:
    if len(data) < 17 or data[0] not in (AUDIO, BACKUP):
        raise BadMessage("バイトのひと続きの形が違います")
    return data[0], UUID(bytes=bytes(data[1:17])), bytes(data[17:])


def new_id() -> UUID:
    return uuid.uuid4()


def uuids(values: Any) -> list[UUID]:
    """回線の一覧など（読めないものは捨てる＝名乗った回線はサーバーがデータベースで確かめ直す）。"""
    out = []
    for v in values if isinstance(values, list) else []:
        try:
            out.append(UUID(str(v)))
        except ValueError:
            continue
    return out
