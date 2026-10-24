"""サーバーと PC の部品のつなぎ口の決まり（DESIGN.md 5-5。**やり取りの形はこの1か所**）。持ち主 CL-2。

- つなぐのは PC から（外向きの1本だけ。WebSocket。暗号化して相手を確かめるのはサーバーの入口＝https）。
- 文字のやり取りは JSON 1つずつ（`"t"` が種類）。通話の声と控えの中身は、バイトのひと続き（先頭1バイトの種類＋16バイトの番号＋中身）。
- **版**（`VERSION`）：つないだときに PC が渡す。サーバーと合わなければ、サーバーは断って「更新してください」と返す（その PC からは
  かけない）。やり取りの形を変えたら番号を上げる。

PC → サーバー：
  hello {v, token, updater, lines, drive, key, emergency}  つないだとき（通行証・つないでいる回線・控えのハードディスクと鍵があるか・
                                                   緊急停止の印 {reason, at, event}。at＝押した時刻＝1回の押しの印）
  beat {lines, drive, key, emergency}              動いている合図（BEAT_EVERY 秒ごと。印が変わったら、すぐ）
  reply {id, ok, value?, error?}                   頼まれたことの答え（error：かけていないと確かに言える＝not_placed（かけようとして、
                                                   かけられなかった。だれのせいかはサーバーが証拠で決める）・withheld（かけようとしなかった：
                                                   PC の緊急停止・答えの期限を過ぎた）／failed＝かけたか分からない。形は link/phone.py）
  phone_event {line, event}                        電話の出来事（中身は会話＝CL-1 と電話接続の担当が決める。サーバーは通話を進める部品へ渡す）
  backup_ack {id, n}                               控えの中身を n 個目まで書いた（サーバーは、書けた分だけ先へ送る＝下の「送る速さ」）
  backup_done {id, ok, error?, ask?, size?, sha256?, removed?} 控えを書いた・片付けた結果（sha256・size は暗号化したファイルのもの。
                                                   ask＝PC の前の作業）。書けなければ、終わりを待たずにすぐ返す（サーバーは送るのをやめる）
  emergency {action: release, reason, event}       PC で「解いてほしい」（event＝PC が知っている停止の番号）
サーバー → PC：
  welcome {tenant, pc, lines}                      通った（lines＝その契約者の、使っている回線だけ）
  refuse {why, relogin, update}                    断った（relogin＝ログインし直す・update＝更新が要る）。サーバーは切る
  request {id, op: in_call|start_call|hang_up|apologize, line, phone?, voice?}   apologize＝おわびの録音を流して切る（サーバーの側の
                                                   誤りで通話を続けられないとき。お客様を無言で放置しない）。voice＝start_call の通話で話す
                                                   声の型（門が決めたもの。おわびの録音も、この声の型で流す）
  backup_begin {id, folder, file_name, sha256}     控えを書く（中身は CHUNK ずつのバイト。sha256＝暗号化する前の中身）
  backup_end {id}
  prune {id, folders}                              古い控えを消す（folders＝フォルダごとに、種類・残す日数・消さないもの）
  emergency_state {event, open, at?}               PC の緊急停止の記録の状態（open=true＝記録した：at の押しに番号を書く／
                                                   open=false＝その番号の停止が解かれた：印の番号が同じなら PC は印を消す）

**送る速さ（控えの中身。判断はこの1か所＝`window`）**：控えと通話の声は同じ1本を通るので、控えを詰めて送ると、声がその後ろで待つ
（AI の声が遅れる）。そこで、PC が書けた印（backup_ack）を待ちながら、まだ書けていない分を決まった数（窓）までしか送らない。その PC で
通話の声・頼みが `CALL_QUIET` 秒以内にあれば、窓は1つ（声の前に並ぶのは、控えの1回分＝CHUNK だけ）。PC の側も、受け取ったものを
控えごとの順番待ちに置くだけにし、書くのは別に動かす（受け取りの繰り返しを、ディスクと暗号化で止めない＝声を止めない）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID

VERSION = 4                  # やり取りの形の版（変えたら上げる。2＝控えの書けた印と送る速さ。3＝番号を受け付けなかった number_rejected。
                             # 4＝number_rejected をやめ not_placed の1つに（だれのせいかは証拠で決める＝0129）・期限切れは withheld）
BEAT_EVERY = 15              # 動いている合図の間隔（秒）
REPLY_WAIT = 20              # 頼んだことの答えを待つ長さ（秒。過ぎたら「分からない」＝かけない側）
ANSWER_BY = REPLY_WAIT - 5   # PC が答える期限（受け取ってから。行き帰りの時間をあけて、サーバーが待つのをやめる前に届ける）。過ぎて
                             # 「かけた」が終わったら、PC は自分でおわびして切り、「分からない」と答える（サーバーと PC で食い違わない）
PING_EVERY = 3.0             # つながりを確かめる間隔と、答えを待つ長さ（秒。サーバーと PC の両方。通話の途中で切れたら、これだけで気づく）
CHUNK = 1 << 15              # 控えの中身を送る1回の大きさ（32 KiB。遅い回線でも、声の前に並ぶのは一瞬）
WINDOW = 32                  # 書けた印を待たずに送ってよい数（通話の無いとき。1 MiB）
CALL_QUIET = 5.0             # 通話の声・頼みがこの秒数無ければ「通話の無いとき」
PROGRESS_WAIT = 60.0         # 控えの中身を送っているあいだ、書けた印をこれだけ待っても来なければ、書けなかったとする（秒）
MAX_FRAME = (1 << 20) + 64   # 1回のやり取りの大きさの上限（声の1回分・控えの1回分に足りる大きさ）。縮める（compression）は使わない
                             # （声も暗号化した控えも縮まず、縮めた形が上限を少し超えて、つなぎが切れた）

AUDIO, BACKUP = 1, 2         # バイトのひと続きの種類

PC_TO_SERVER = frozenset({"hello", "beat", "reply", "phone_event", "backup_ack", "backup_done", "emergency"})
SERVER_TO_PC = frozenset({"welcome", "refuse", "request", "backup_begin", "backup_end", "prune", "emergency_state"})
OPS = frozenset({"in_call", "start_call", "hang_up", "apologize"})


class RemoteError(Exception):
    """PC の部品が、頼まれたことをできなかった（中身は、鍵・お客様の情報を含まない短い説明）。ask＝直すのが PC の前の作業なら、
    その PC の持ち主へ知らせる文（ハードディスクをつなぐ・鍵を入れる）。無ければプログラムの誤り（修正依頼）。"""

    def __init__(self, message: str, ask: str | None = None) -> None:
        super().__init__(message)
        self.ask = ask


class Interrupted(BaseException):
    """サーバーを止めるので、PC の部品への頼み（控えを書く）を取りやめた。**失敗ではない**：記録も修正依頼もせず、次に動いたときに
    やり直す（`except Exception` で受けて失敗として記録されないよう、BaseException にする。仕事の借用は返す）。"""


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


def window(calling: bool) -> int:
    """書けた印を待たずに送ってよい、控えの中身の数（上の「送る速さ」。判断はこの1か所）。"""
    return 1 if calling else WINDOW


def new_id() -> UUID:
    return uuid.uuid4()


def uuid_of(value: Any) -> UUID | None:
    """番号を読む（読めなければ None＝そのやり取りを捨てる。受けた側を落とさない）。"""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def event_of(value: Any) -> int | None:
    """緊急停止の記録の番号を読む（整数だけ。真偽は番号ではない）。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def uuids(values: Any) -> list[UUID]:
    """回線の一覧など（読めないものは捨てる＝名乗った回線はサーバーがデータベースで確かめ直す）。"""
    out = []
    for v in values if isinstance(values, list) else []:
        try:
            out.append(UUID(str(v)))
        except ValueError:
            continue
    return out
