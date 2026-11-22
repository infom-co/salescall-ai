"""おわびの録音の PC の上の置き場（持ち主 CL-3。docs/PHONE.md 2-5）。

通話の途中でサーバーとのつながりが切れても、お客様を無言で待たせずに、おわびの録音を流して切るため（計画書 10-3・8-2 の
`play_apology_and_hang_up`）、声の型ごとの2つの録音（共通の一言 `line_trouble_ask`・`line_trouble_bye`）を、通話の前に PC に持っておく。

- しまう（`put(声の型, 部分, 指紋, 音)`）：サーバーから届いた録音（WAV）を、届いた指紋（sha256）と照らしてからしまう。合わなければしまわない（壊れた録音・
  取り違えた録音を流さない）。声の型の名前は英数字と「_」「-」だけ（置き場の外へ書かない）。書くのは一時のファイルに書いてから置き換える
  （途中で止まっても、半分の録音を残さない）。
- 読む（`recordings`）：声の型 → （ひとつ目・ふたつ目）の録音を、装置の形（`rate`）で返す。声の形を変えるのは `audio.resample` の1か所。
  どちらかが無い・読めない（16bit・1ch でない）ときは None（電話の部品は、録音が無ければ黙って待たせずにすぐ切る）。
- サーバーから配るやり取りの形（どの録音を持つべきかの一覧・録音の中身を送る形）は CL-2 の持ち物（`link/protocol.py`）。案は CL-3 の連絡帳
  4-1c の 74。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import wave
from pathlib import Path

from salescall.agent.phones import audio

PARTS = ("line_trouble_ask", "line_trouble_bye")   # ひとつ目・ふたつ目（名前の正本は CL-1 の phrases.RECORDED・core/recordings.COMMON_PARTS）
_VOICE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Rejected(ValueError):
    """しまわなかった（指紋が合わない・声の型の名前や部分が分からない・WAV として読めない）。"""


def default_root() -> Path:
    """置き場（設定 SALESCALL_APOLOGY_DIR。無ければ利用者のフォルダの .salescall/apology）。"""
    return Path(os.environ.get("SALESCALL_APOLOGY_DIR") or Path.home() / ".salescall" / "apology")


def _pcm_of(data: bytes) -> tuple[bytes, int]:
    """WAV（16bit・1ch）から PCM と速さを取り出す。それ以外は Rejected。"""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise Rejected("おわびの録音は 16bit・1ch の WAV だけです")
            return w.readframes(w.getnframes()), w.getframerate()
    except (wave.Error, EOFError) as e:
        raise Rejected(f"WAV として読めません：{e}") from None


class ApologyCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()

    def _path(self, voice: str, part: str) -> Path:
        if not _VOICE.fullmatch(voice or ""):
            raise Rejected(f"声の型の名前が分かりません：{voice!r}")
        if part not in PARTS:
            raise Rejected(f"おわびの録音の部分が分かりません：{part!r}")
        return self.root / voice / f"{part}.wav"

    def put(self, voice: str, part: str, sha256: str, audio: bytes) -> None:
        """届いた録音をしまう（指紋が合い、WAV として読めるときだけ。前の録音は置き換える）。引数の並びは PC の部品の土台の置き場の形
        （CL-2 の `agent.main.ApologyStore.put(voice, part, sha256, audio)`＝4-479）。合わなければ Rejected（ValueError）。"""
        path = self._path(voice, part)
        if hashlib.sha256(audio).hexdigest() != (sha256 or "").lower():
            raise Rejected("届いた録音の指紋が合いません（しまいません）")
        _pcm_of(audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(audio)
        os.replace(tmp, path)

    def held(self) -> dict[str, dict[str, str]]:
        """持っている録音の指紋（声の型 → {部分 → sha256}）。サーバーへ「何を持っているか」を知らせるため（合図の apologies。
        CL-2 の `agent.main.ApologyStore.held` の形＝4-479）。名前の決まりに合わないフォルダ・部分は数えない。"""
        out: dict[str, dict[str, str]] = {}
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not _VOICE.fullmatch(d.name):
                continue
            got = {part: hashlib.sha256((d / f"{part}.wav").read_bytes()).hexdigest() for part in PARTS if (d / f"{part}.wav").exists()}
            if got:
                out[d.name] = got
        return out

    def recordings(self, rate: int):   # noqa: ANN201  （rakuten_link.Recordings の形）
        """電話の部品に渡す口：声の型 → （ひとつ目・ふたつ目）の録音（装置の形 rate）。どちらかが無い・読めなければ None。"""
        def get(voice: str | None) -> tuple[bytes, bytes] | None:
            if not voice:
                return None
            try:
                got = []
                for part in PARTS:
                    p = self._path(voice, part)
                    if not p.exists():
                        return None
                    pcm, src = _pcm_of(p.read_bytes())
                    got.append(audio.resample(pcm, src, rate))
                return got[0], got[1]
            except Rejected:
                return None
        return get
