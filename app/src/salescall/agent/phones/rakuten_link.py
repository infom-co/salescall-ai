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
LINKED_FRESH = 3.0         # usable が覚えた「スマホと連携しているか」を使ってよい長さ（秒。土台は1秒ごとに聞く＝画面を毎回は読まない）


class Screen(Protocol):
    """Rakuten Link の画面を操作する口（本物は UI Automation。⏳ 部品の名前は R-5・R-6 で決める）。"""
    def linked(self) -> bool | None: ...          # スマホと連携しているか（分からなければ None）
    def state(self) -> str: ...                   # "idle"・"calling"・"in_call"・"unknown"
    def dial(self, phone_e164: str) -> None: ...   # 番号を入れて発信を押す（押せなければ例外）
    def hang_up(self) -> None: ...
    def send_sms(self, number: str, text: str) -> None: ...   # 国内の形の番号へ1通（宛先が連絡先に無ければ LookupError）


class Sound(Protocol):
    """音の装置（本物は Virtual Audio Cable の Line 1 から録り、Line 2 へ流す。⏳ R-2〜R-4）。"""
    rate: int
    def listen(self, on_frame: Callable[[bytes], None]) -> None: ...   # 装置の形の 20ms ごとの声を渡し続ける
    def play(self, pcm: bytes, done: Callable[[], None] | None = None) -> None: ...   # 装置の形の声を流す（待たずに返す）。流し終えたら done を呼ぶ
    def play_wait(self, pcm: bytes) -> None: ...                        # 流し終えるまで待つ
    def stop(self) -> float: ...                                        # 流している声と順番待ちをやめる。答え＝流していた声が流れた秒（無ければ 0）


Recordings = Callable[[str | None], tuple[bytes, bytes] | None]   # 声の型 → （ひとつ目・ふたつ目）の録音（装置の形）


class RakutenLink:
    def __init__(self, screen: Screen, sound: Sound, recordings: Recordings, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, watch_every: float | None = 0.2) -> None:
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
        self._pending: list[str] = []           # 流している・順番待ちの声の印（流した順）
        self._lock_play = threading.Lock()
        self._linked, self._linked_at = False, None   # スマホとの連携（usable が返す覚え。poll_state のついでに読み直す）
        sound.listen(self._on_frame)
        # 画面の状態（呼び出し中・つながった・切れた）は、声の流れとは別の流れで watch_every 秒ごとに読む（画面を読むのは遅いので、
        # 20ms ごとの声の流れの中で読むと声が詰まる）。試験は None にして、poll_state を自分で呼ぶ
        if watch_every is not None:
            threading.Thread(target=self._watch, args=(watch_every,), daemon=True, name="rakuten-link-state").start()

    # ── Phone の形 ──────────────────────────────────────────
    def in_call(self) -> bool:
        try:
            return self.screen.state() != "idle"   # "unknown" も通話中とみなす（かけない側）
        except Exception:
            return True

    def start_call(self, phone_e164: str, voice_type: str | None = None, *, national: str) -> None:
        """かける。番号の欄へは、サーバーが添えた国内の形 national をそのまま入れる（部品の中で番号の形を変えない＝見張り Q1。
        CL-2 の 4-459・やり取りの版 8）。phone_e164 は記録と照らし合わせのためだけ。"""
        with self._lock:
            self._voice = voice_type
            self._place(national)

    def hang_up(self) -> None:
        with self._lock_play:
            self._pending = []   # 切った電話の声の印は残さない（次の電話に持ち越さない）
        self.sound.stop()
        self.screen.hang_up()

    def test_ring(self, phone_e164: str, *, national: str) -> bool | None:
        """テスト番号へかけ、この回線から電話網へ発信できたかを答える（計画書 8-2・8-8。答えの意味は `link.phone.Phone.test_ring`）。
        - True：呼び出しの音・話し中の音が聞こえた。
        - False：画面が「かけている」にならない（アプリが発信できない）、または、音も声も何も聞こえないまま電話が終わった。
        - None：何かの声は聞こえたが音ではなかったまま終わった（電源オフ・圏外のお知らせの声かもしれない＝それは「発信できた」なので、
          聞き分けられないうちは「発信できない」と決めつけない）・期限まで見分けられなかった。
        ⏳ お知らせの声（電源オフ・圏外）を聞き分けて True にするのは、見本を録ってから（R-6）。"""
        with self._lock:
            self._cadence = tones.Cadence(self.sound.rate)
            try:
                self._place(national)
            except CallNotPlaced:
                return False
            started = self.clock()
            end = started + ANSWER_BY - 3
            try:
                while self.clock() < end:
                    if self._tone in ("ringback", "busy"):
                        return True
                    if self._state() == "idle":                          # 音が出ないまま電話が終わった
                        return None if any(t >= started for t in self._heard) else False
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

    def usable(self) -> bool:
        """この回線が、いま発信を受けられるか（スマホと連携している。分からなければ False＝かけない側）。PC の部品の土台が、合図の
        回線の一覧に入れるかをこれで決める（CL-2 の 4-459 の (2)）。土台は1秒ごとに呼ぶので、画面を毎回は読まない：状態を読む流れ
        （poll_state）のついでに覚えた答えが LINKED_FRESH 秒より新しければ、それを返す。古い・まだ無いときだけ、ここで読む。"""
        if self._linked_at is not None and self.clock() - self._linked_at <= LINKED_FRESH:
            return self._linked
        return self._read_linked()

    def _read_linked(self) -> bool:
        try:
            ok = self.screen.linked() is True
        except Exception:
            ok = False
        self._linked, self._linked_at = ok, self.clock()
        return ok

    def send_sms(self, phone_e164: str, text: str, *, national: str) -> None:
        """ショートメールを1通送る（CL-2 へのお願い 62 の (1)）。送れたと確かに言えるときだけ返る：
        - 通話の途中（または分からない）＝CallWithheld（送ろうとしない。画面の操作で通話を乱さない）
        - スマホと連携していない・宛先がスマホの連絡先に無い・番号の形が違う＝CallNotPlaced（送っていない）
        - それ以外の失敗（画面の部品が見つからない・押したあとの確かめが合わない など）＝そのまま例外（送れたか分からない＝サーバーは
          送れなかったとして次の送り方へ。同じ文が2通届くことはありうるが、知らせが届かないよりよい）
        連絡先には登録しない（スマホの連絡先を書き換えない）。"""
        from salescall.link.phone import CallWithheld
        with self._lock:
            if self.in_call():
                raise CallWithheld("通話の途中なので、ショートメールは送りません")
            if not self._read_linked():   # 送る直前は、覚えた答えでなく画面を読む
                raise CallNotPlaced("Rakuten Link がスマホと連携していない（または分からない）")
            try:
                self.screen.send_sms(national, text)   # 宛先の欄へは国内の形をそのまま（start_call と同じ）
            except LookupError as e:   # 宛先が連絡先に無い（screen_uia.NotRegistered）・部品が見つからない
                raise CallNotPlaced(str(e) or "宛先がスマホの連絡先にありません") from None
            except ValueError as e:    # 番号の形が違う（国内の形でない＝送っていない）
                raise CallNotPlaced(str(e)) from None

    def on_audio(self, send: Callable[[bytes], None]) -> None:
        self._send_audio = send

    def on_event(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send_event = send

    def play(self, pcm: bytes, ref: str | None = None) -> None:
        """サーバーから届いた声のひと続きを流す（待たずに返す）。ref（頼みの印）があれば、通話へ流し終えたときに出来事
        {"kind": "played", "ref": ref} を返す（黙ったときの見張りの数え始めは、声が終わってから＝CL-1 の 4-227 の (1)・PHONE.md 2-2）。"""
        data = audio.resample(pcm, FROM_SERVER_RATE, self.sound.rate)
        if ref is None:
            self.sound.play(data)
            return
        with self._lock_play:
            self._pending.append(ref)
        def done() -> None:
            with self._lock_play:
                if ref in self._pending:
                    self._pending.remove(ref)
            self._send_event({"kind": "played", "ref": ref})
        self.sound.play(data, done)

    def stop_playing(self) -> None:
        """流すのをやめる（サーバーが決める＝外れた言葉を止める見張り・お客様が話し始めた。CL-1 の 4-226 の (2)・PHONE.md 2-2）。順番待ちの声は捨て、
        流している途中の声も止める。止めた声ごとに {"kind": "stopped", "ref": 印, "played_s": 流れた秒} を返す（お客様にどこまで聞こえたかの記録。
        まだ流れ始めていない声は 0 秒）。"""
        with self._lock_play:
            dropped, self._pending = list(self._pending), []
        played = self.sound.stop()
        for n, ref in enumerate(dropped):
            self._send_event({"kind": "stopped", "ref": ref, "played_s": round(played, 2) if n == 0 else 0.0})

    # ── 中の流れ ─────────────────────────────────────────────
    _tone: str | None = None

    def _place(self, phone_e164: str) -> None:
        """発信する。スマホと連携していない・画面が「かけている」にならない＝CallNotPlaced（お客様の電話は鳴っていない）。"""
        if self.screen.linked() is not True:
            raise CallNotPlaced("Rakuten Link がスマホと連携していない（または分からない）")
        if self.in_call():                                               # 1回線で同時に1本（計画書 8-3）。話している電話を保留にしない
            raise CallNotPlaced("この回線は通話の途中（または分からない）")
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
        for frame in self._framer.push(audio.resample(pcm, self.sound.rate, audio.TO_SERVER_RATE)):
            self._send_audio(frame)

    def _state(self) -> str:
        try:
            return self.screen.state()
        except Exception:
            return "unknown"

    def poll_state(self) -> None:
        """画面の状態を1回読み、移ったら出来事を知らせる（つながった answered・切れた ended）。"""
        state = self._state()
        if state != self._last_state:
            if state == "in_call":
                self._send_event({"kind": "answered"})
            elif state == "idle" and self._last_state in ("calling", "in_call"):
                self._send_event({"kind": "ended"})
            self._last_state = state

    def _watch(self, every: float) -> None:
        while True:
            self.poll_state()
            if self._linked_at is None or self.clock() - self._linked_at > LINKED_FRESH / 2:   # 覚えが古くなる前に読み直す
                self._read_linked()
            time.sleep(every)

    def _wait_reply(self, until: float) -> None:
        """お客様が話し始めて、話し終える（QUIET_END 途切れる）まで待つ。話さなければ until まで。"""
        started = self.clock()
        while self.clock() < until:
            recent = [t for t in self._heard if t >= started]
            if recent and self.clock() - recent[-1] >= QUIET_END:
                return
            self.sleep(0.05)


_MADE: dict[str, Any] = {}


def make(line_id: Any) -> RakutenLink:
    """PC の部品が回線ごとに呼ぶ入口（設定 SALESCALL_PHONE＝"salescall.agent.phones.rakuten_link:make"）。本物の画面の口と音の装置で作る。
    ⏳ PC 1台で Rakuten Link を2つ動かせるかは R-8 で確かめる。それまでは1台の PC に回線は1本だけ（2本目は断る＝かけない側）。
    おわびの録音は、PC の上の置き場（apology_cache の1か所）から読む。⏳ サーバーから配る形ができるまで置き場は空（録音が無ければ、黙って
    待たせずにすぐ切る＝play_apology_and_hang_up。配る形の案は CL-3 の連絡帳 4-1c の 74）。"""
    if _MADE and str(line_id) not in _MADE:
        raise RuntimeError("この PC の Rakuten Link は1つだけ（回線は1本まで。2本目は R-8 で確かめてから）")
    if str(line_id) not in _MADE:
        from salescall.agent.phones.screen_uia import UiaScreen
        from salescall.agent.phones.sound_vac import VacSound
        from salescall.agent.phones.apology_cache import ApologyCache
        sound = VacSound()
        _MADE[str(line_id)] = RakutenLink(UiaScreen(), sound, ApologyCache().recordings(sound.rate))
    return _MADE[str(line_id)]
