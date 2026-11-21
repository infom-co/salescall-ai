"""業務で流す録音の音の置き場（表の作り 0275。持ち主 CL-2。設計書 3-8。CL-1 の連絡帳 4-222）。

✅ 2026-11-21 日本時間1時台 管理者「ア　データベースの中に置く」。音を置く・読む・指紋を確かめる・権限を確かめる・誰がいつ聞いたかを記録する、はこの1か所。
使うのは3者で、どれもここを呼ぶ：録音を作る道具（CL-1）が置く（`put`）・承認と審査の画面（CL-3）が聞く（`listen`。名前の録音の審査は管理者のシステムの取引で
`review_listen`）・通話の声を PC へ運ぶ所が流す（`read`）。

- 音は WAV のまま置く（加工は一切しない＝計画書 9-8）。参照は「audio:指紋」（録音の記録の audio_ref。`REF_PREFIX`）。同じ範囲の同じ音は1つ（使い回す）。
- 音は指紋で探す（指紋が音の名前）。指紋が中身と同じことは、置くときにデータベースが守り（食い違う音は置けない）、置いた音は書き換えられない。そのため、録音の記録の
  指紋で見つかった音は、承認した音そのもの。見つからなければ聞かせない・流さない（`AudioMissing`。置き場ができる前の試験の録音など）。
  ⛔ 2026-11-21 の壊し試し：読むたびに中身から指紋を作り直す確かめと、参照の形の確かめは、上の守りと同じ判断の写し（外しても結果が変わらない）だったので、持たない。
- 読める範囲はデータベースが守る（録音の記録と同じ：その契約者・依頼者の取引から。システムの録音はどの契約者からも読める・置くのは管理者のシステムの取引だけ）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from salescall.core.access import Refused, Tx

REF_PREFIX = "audio:"
MAX_BYTES = 10 * 1024 * 1024   # データベースの守りと同じ上限（0275）


class AudioRefused(Refused):
    """置けない音（理由は日本語）。"""


class AudioMissing(Refused):
    """読めない音：置き場に無い・指紋が違う（聞かせない・流さない）。"""


@dataclass(frozen=True)
class Stored:
    """置いた音の参照（録音の記録の audio_ref・audio_sha256 にそのまま入れる）。"""
    audio_ref: str
    audio_sha256: str


def _wav(data: bytes) -> bool:
    return len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def put(tx: Tx, data: bytes) -> Stored:
    """音を置く（1か所）。置く範囲は取引の範囲（システムの取引＝システムの録音・契約者の取引＝その契約者・依頼者の取引＝その依頼者）。同じ範囲に同じ音があれば
    それを使う。WAV でない・大きすぎる音は AudioRefused。"""
    if not isinstance(data, (bytes, bytearray)) or not _wav(bytes(data)):
        raise AudioRefused("録音の音は WAV のファイルです")
    if len(data) > MAX_BYTES:
        raise AudioRefused(f"録音の音が大きすぎます（{MAX_BYTES // (1024 * 1024)}MB まで）")
    sha = hashlib.sha256(data).hexdigest()
    tx.execute("INSERT INTO salescall.recording_audio (audio_sha256, audio) VALUES (%s, %s)"
               " ON CONFLICT (tenant_id, client_id, audio_sha256) DO NOTHING", (sha, bytes(data)))
    return Stored(REF_PREFIX + sha, sha)


def read(tx: Tx, audio_sha256: str) -> bytes:
    """録音の記録の指紋の音を読む（1か所。流す所・聞く所はここだけを呼ぶ）。読める範囲に無ければ AudioMissing。"""
    row = tx.one("SELECT audio FROM salescall.recording_audio WHERE audio_sha256 = %s LIMIT 1", (audio_sha256,))
    if row is None:
        raise AudioMissing("録音の音が置き場にありません")
    return bytes(row["audio"])


def listen(tx: Tx, set_id: UUID, part: str) -> bytes:
    """承認・審査の画面で、録音の組の1つの部分を聞く（聞いた記録を残す。だれが＝取引の方）。見えない組・部分は AudioMissing（読める範囲は録音の記録の守り）。"""
    row = tx.one("SELECT audio_sha256 FROM salescall.recordings WHERE set_id = %s AND part = %s", (set_id, part))
    if row is None:
        raise AudioMissing("その録音はありません")
    data = read(tx, row["audio_sha256"])
    tx.execute("INSERT INTO salescall.recording_listens (set_id, part) VALUES (%s, %s)", (set_id, part))
    return data


def review_listen(tx: Tx, set_id: UUID, part: str) -> bytes:
    """名前の録音の審査で、管理者が音を聞く（管理者のシステムの取引で。CL-3 の 68。聞いた記録を残す）。聞けるのは、審査を待っている組
    （`recordings.name_reviews` の一覧に出る組）の部分だけ。音の置き場は契約者・依頼者の範囲の守りなので、データベースの1か所
    salescall.review_listen が、その組の範囲の音を読む（表の作り 0277）。審査を待っていない組・置き場に無い音は AudioMissing。
    管理者でない・システムの取引でないときは Rejected（name_review_by_admin＝審査の守りと同じ）。"""
    row = tx.one("SELECT salescall.review_listen(%s, %s) AS audio", (set_id, part))
    if row["audio"] is None:
        raise AudioMissing("審査を待っている名前の録音に、その音はありません")
    return bytes(row["audio"])
