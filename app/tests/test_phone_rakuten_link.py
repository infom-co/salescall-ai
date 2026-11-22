"""Rakuten Link の電話の部品の流れの試験（持ち主 CL-3。agent/phones/rakuten_link.py。docs/PHONE.md 2章）。

本物の画面・音の装置の代わりに、にせの画面（状態を持つ）とにせの音（作った音を流し込む）で確かめる。時計もにせ（待つと進む）。
⏳ 本物の画面の部品の名前・音の装置は、管理者の PC での確かめ（R-2〜R-6）のあとで足す。データベースは使わない。
"""

from __future__ import annotations

import unittest

from salescall.agent.phones import audio, rakuten_link
from salescall.link.phone import CallNotPlaced
from tests.test_phone_tones import silence, tone

RATE = 16000   # にせの音の装置の形（本物は 48kHz の見込み）


class Clock:
    def __init__(self):
        self.t = 0.0
        self.on_sleep = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for f in list(self.on_sleep):
            f(self.t)


class FakeScreen:
    def __init__(self, linked=True, goes_to="calling", fails=False):
        self._linked, self.goes_to, self.fails = linked, goes_to, fails
        self.now = "idle"
        self.dialed = []
        self.hung = 0

    def linked(self):
        return self._linked

    def state(self):
        return self.now

    def dial(self, number):
        if self.fails:
            raise RuntimeError("押せない")
        self.dialed.append(number)
        self.now = self.goes_to

    def hang_up(self):
        self.hung += 1
        self.now = "idle"

    seen = "090-0000-0000"   # 架空の着信の番号（画面の文字のまま）
    answered = 0

    def incoming_number(self):
        return self.seen

    def answer(self):
        self.answered += 1
        self.now = "in_call"

    sms_error = None

    def send_sms(self, number, text):
        if self.sms_error:
            raise self.sms_error
        self.sent = (number, text)


class FakeSound:
    rate = RATE

    def __init__(self):
        self.played = []
        self.stopped = 0
        self.frame = None

    def listen(self, on_frame):
        self.frame = on_frame

    def feed(self, pcm):
        for f in audio.Framer(RATE).push(pcm):
            self.frame(f)

    def play(self, pcm, done=None):
        self.played.append(("play", len(pcm)))
        self.done = getattr(self, "done", []) + [done]

    def play_wait(self, pcm):
        self.played.append(("wait", len(pcm)))

    def stop(self):
        self.stopped += 1
        return 0.48   # 流していた声が流れた秒（架空）


def phone(screen=None, recordings=lambda v: (b"\x01\x00" * 160, b"\x02\x00" * 160)):
    clock = Clock()
    p = rakuten_link.RakutenLink(screen or FakeScreen(), FakeSound(), recordings, clock=clock.now, sleep=clock.sleep, watch_every=None)
    return p, clock


class Placing(unittest.TestCase):
    def test_a_call_is_placed_only_when_linked_and_the_screen_starts_calling(self):
        p, _ = phone()
        p.start_call("+81300000001", "声A", national="0300000001")
        self.assertEqual(p.screen.dialed, ["0300000001"])   # 番号の欄へは、サーバーが添えた国内の形のまま（部品の中で形を変えない）
        self.assertTrue(p.in_call())

    def test_not_linked_or_unknown_link_is_not_placed(self):
        for linked in (False, None):
            with self.subTest(linked=linked):
                p, _ = phone(FakeScreen(linked=linked))
                with self.assertRaises(CallNotPlaced):
                    p.start_call("+81300000001", national="0300000001")
                self.assertEqual(p.screen.dialed, [])

    def test_a_screen_that_never_starts_calling_is_not_placed(self):
        p, clock = phone(FakeScreen(goes_to="idle"))
        with self.assertRaises(CallNotPlaced):
            p.start_call("+81300000001", national="0300000001")
        self.assertGreaterEqual(clock.t, rakuten_link.DIAL_WAIT)

    def test_a_button_that_cannot_be_pressed_is_not_placed(self):
        p, _ = phone(FakeScreen(fails=True))
        with self.assertRaises(CallNotPlaced):
            p.start_call("+81300000001", national="0300000001")

    def test_a_line_in_a_call_does_not_place_another(self):
        s = FakeScreen()
        s.now = "in_call"
        p, _ = phone(s)
        with self.assertRaises(CallNotPlaced):
            p.start_call("+81300000001", national="0300000001")                                     # 1回線で同時に1本（計画書 8-3）
        self.assertEqual(s.dialed, [])

    def test_an_unknown_screen_counts_as_in_a_call(self):
        s = FakeScreen()
        s.now = "unknown"
        p, _ = phone(s)
        self.assertTrue(p.in_call())                                         # 分からなければ通話中＝かけない側


class Listening(unittest.TestCase):
    def test_audio_goes_to_the_server_in_20ms_frames_and_tones_and_states_become_events(self):
        p, _ = phone()
        sent, events = [], []
        p.on_audio(sent.append)
        p.on_event(events.append)
        p.start_call("+81300000001", national="0300000001")
        p.sound.feed(b"".join(tone(400, 1.0) + silence(2.0) for _ in range(3)))
        self.assertIn({"kind": "ringback"}, events)
        self.assertTrue(sent and all(len(f) == audio.TO_SERVER_RATE * 20 // 1000 * 2 for f in sent))
        p.poll_state()                                                        # 画面の状態は、声とは別の流れで読む
        p.screen.now = "in_call"
        p.poll_state()
        p.screen.now = "idle"
        p.poll_state()
        self.assertEqual([e["kind"] for e in events][-2:], ["answered", "ended"])

    def test_server_voice_is_converted_once_to_the_device_rate(self):
        p, _ = phone()
        p.play(b"\x00\x00" * rakuten_link.FROM_SERVER_RATE)                 # 1秒
        self.assertEqual(p.sound.played, [("play", RATE * 2)])


class TestRing(unittest.TestCase):
    def test_ringback_heard_means_the_line_can_call(self):
        p, clock = phone()
        clock.on_sleep.append(lambda t: p.sound.feed(tone(400, 0.1) if int(t * 10) % 30 < 10 else silence(0.1)))
        self.assertTrue(p.test_ring("+81300000009", national="0300000009"))
        self.assertEqual(p.screen.hung, 1)                                   # 相手が出る前に切る

    def test_busy_heard_also_means_the_line_can_call(self):
        p, clock = phone()
        clock.on_sleep.append(lambda t: p.sound.feed(tone(400, 0.1) if int(t * 10) % 10 < 5 else silence(0.1)))
        self.assertTrue(p.test_ring("+81300000009", national="0300000009"))

    def test_a_call_that_ends_without_any_sound_cannot_call_but_a_voice_is_not_judged(self):
        """計画書 8-2：呼び出しの音もお知らせの声も無いまま終わった＝発信できない。何かの声が聞こえて終わったら、お知らせの声かも
        しれないので決めつけない（お知らせの声の聞き分けは見本を録ってから）。"""
        for sound, want in ((silence(0.1), False), (tone(300, 0.1, amp=6000), None)):
            with self.subTest(want=want):
                p, clock = phone()
                def step(t, p=p, sound=sound):
                    p.sound.feed(sound)
                    if t > 3:
                        p.screen.now = "idle"
                clock.on_sleep.append(step)
                self.assertIs(p.test_ring("+81300000009", national="0300000009"), want)

    def test_not_placed_is_false_and_silence_is_unknown(self):
        p, _ = phone(FakeScreen(goes_to="idle"))
        self.assertFalse(p.test_ring("+81300000009", national="0300000009"))
        p, clock = phone()
        clock.on_sleep.append(lambda t: p.sound.feed(silence(0.1)))
        self.assertIsNone(p.test_ring("+81300000009", national="0300000009"))                      # 見分けられなければ決めつけない


class ShortMessage(unittest.TestCase):
    def test_a_message_is_sent_only_when_linked_and_not_in_a_call(self):
        """ショートメール（CL-2 へのお願い 62）：通話の途中は送らない（withheld）・連携していない／連絡先に無い＝送っていない（not_placed）。"""
        from salescall.agent.phones.screen_uia import NotRegistered
        from salescall.link.phone import CallWithheld
        p, _ = phone()
        p.send_sms("+81300000001", "架空の知らせ", national="0300000001")
        self.assertEqual(p.screen.sent, ("0300000001", "架空の知らせ"))
        self.assertTrue(p.usable())
        busy = FakeScreen()
        busy.now = "in_call"
        p, _ = phone(busy)
        with self.assertRaises(CallWithheld):
            p.send_sms("+81300000001", "架空の知らせ", national="0300000001")
        for screen in (FakeScreen(linked=False), FakeScreen(linked=None)):
            p, _ = phone(screen)
            self.assertFalse(p.usable())
            with self.assertRaises(CallNotPlaced):
                p.send_sms("+81300000001", "架空の知らせ", national="0300000001")
        unknown = FakeScreen()
        unknown.sms_error = NotRegistered("宛先がスマホの連絡先にありません")
        p, _ = phone(unknown)
        with self.assertRaises(CallNotPlaced):
            p.send_sms("+81300000001", "架空の知らせ", national="0300000001")
        broken = FakeScreen()
        broken.sms_error = RuntimeError("押したあとの確かめが合わない")
        p, _ = phone(broken)
        with self.assertRaises(RuntimeError):                                  # 送れたか分からない（サーバーは次の送り方へ）
            p.send_sms("+81300000001", "架空の知らせ", national="0300000001")


class PlayedAndStopped(unittest.TestCase):
    def test_the_phone_says_when_a_voice_has_finished_and_when_it_was_stopped(self):
        """声を流し終えたら played（黙ったときの見張りの数え始め＝CL-1 の 4-227 の (1)）、サーバーが「やめて」と言ったら、順番待ちを捨てて
        stopped（流れた秒つき。まだ流れ始めていない声は 0 秒＝CL-1 の 4-226 の (2)）。決めるのはサーバー（PC は言われたとおりにして知らせるだけ）。"""
        p, _ = phone()
        events = []
        p.on_event(events.append)
        p.play(b"\x00\x00" * 2400, ref="r1")
        p.play(b"\x00\x00" * 2400, ref="r2")
        p.play(b"\x00\x00" * 2400, ref="r3")
        p.sound.done[0]()                                                     # 1つ目を流し終えた
        self.assertEqual(events, [{"kind": "played", "ref": "r1"}])
        p.stop_playing()
        self.assertEqual(events[1:], [{"kind": "stopped", "ref": "r2", "played_s": 0.48}, {"kind": "stopped", "ref": "r3", "played_s": 0.0}])
        p.play(b"\x00\x00" * 2400)                                           # 印の無い声（録音など）は知らせない
        self.assertIsNone(p.sound.done[-1])


class VacQueue(unittest.TestCase):
    def test_the_device_queue_calls_done_after_the_last_bytes_and_stop_reports_the_seconds(self):
        """仮想の音声装置の順番待ち（装置そのものは使わない＝流す口 _speak を直に呼ぶ）：ひと続きを流し終えた時に done を1回・
        途中で stop したら、そのひと続きが流れた秒を返す。"""
        import threading
        from collections import deque
        from salescall.agent.phones import sound_vac
        v = object.__new__(sound_vac.VacSound)
        v._queue, v._lock, v._drained = deque(), threading.Lock(), threading.Event()
        calls = []
        frame = sound_vac.FRAME
        v.play(b"\x01\x00" * (frame + frame // 2), lambda: calls.append("a"))   # 1.5 フレーム分
        v.play(b"\x02\x00" * frame, lambda: calls.append("b"))
        out = bytearray(frame * 2)
        v._speak(out, frame, None, None)
        self.assertEqual(calls, [])                                           # まだ途中
        v._speak(out, frame, None, None)
        self.assertEqual(calls, ["a"])                                        # 1つ目を流し終えた（2つ目は途中）
        played = v.stop()
        self.assertAlmostEqual(played, (frame // 2) / sound_vac.RATE)         # 2つ目は半フレーム分流れていた
        self.assertEqual(calls, ["a"])                                        # 止めた声の done は呼ばない（止めた知らせは別に出す）


class Usable(unittest.TestCase):
    def test_usable_answers_from_a_fresh_reading_without_reading_the_screen_each_second(self):
        """土台は1秒ごとに usable を聞く（CL-2 の 4-459）。覚えた答えが LINKED_FRESH 秒より新しければ画面を読まず、古ければ読み直す。"""
        screen = FakeScreen()
        p, clock = phone(screen)
        reads = []
        real = screen.linked
        screen.linked = lambda: (reads.append(1), real())[1]
        self.assertTrue(p.usable())
        self.assertTrue(p.usable())
        self.assertEqual(len(reads), 1)                                      # 2回目は覚えた答え
        clock.sleep(rakuten_link.LINKED_FRESH + 0.1)
        screen._linked = False
        self.assertFalse(p.usable())                                         # 古くなったら読み直す（連携が切れたのが分かる）
        self.assertEqual(len(reads), 2)


class Numbers(unittest.TestCase):
    def test_only_the_domestic_digits_are_typed_and_nothing_is_reshaped_here(self):
        from salescall.agent.phones.screen_uia import DOMESTIC
        self.assertTrue(DOMESTIC.fullmatch("0300000001") and DOMESTIC.fullmatch("09000000001"))
        for bad in ("+81300000001", "03-0000-0001", "300000001", ""):
            self.assertIsNone(DOMESTIC.fullmatch(bad), bad)                    # 形を変えずに断る（形を変えるのは core/phone の1か所）


class Apology(unittest.TestCase):
    def test_ask_wait_for_the_reply_then_bye_then_hang_up(self):
        p, clock = phone()
        p.start_call("+81300000001", "声A", national="0300000001")
        p.screen.now = "in_call"
        clock.on_sleep.append(lambda t: p.sound.feed(tone(300, 0.05, amp=6000) if t < 1.5 else silence(0.05)))
        p.play_apology_and_hang_up()
        self.assertEqual([k for k, _ in p.sound.played], ["wait", "wait"])   # ひとつ目・ふたつ目
        self.assertEqual(p.screen.hung, 1)
        self.assertLess(clock.t, rakuten_link.REPLY_LISTEN)                 # 返事が終われば待たずに進む

    def test_without_the_recordings_it_hangs_up_at_once(self):
        p, _ = phone(recordings=lambda v: None)
        p.start_call("+81300000001", national="0300000001")
        p.play_apology_and_hang_up()
        self.assertEqual(p.sound.played, [])
        self.assertEqual(p.screen.hung, 1)

    def test_not_in_a_call_does_nothing(self):
        p, _ = phone()
        p.play_apology_and_hang_up()
        self.assertEqual(p.screen.hung, 0)


if __name__ == "__main__":
    unittest.main()


class IncomingCall(unittest.TestCase):
    """折り返しの着信（計画書 8-7 ✅ 2026-11-23 管理者「イ」・docs/PHONE.md 2-8）：鳴ったら番号の文字のまま知らせ、鳴っているあいだは発信せず、
    サーバーが「出る」と言ったときだけ出る。出ないまま鳴り終わったら、それも知らせる。"""

    def setup(self):
        screen = FakeScreen()
        p, events, clock = None, [], Clock()
        p = rakuten_link.RakutenLink(screen, FakeSound(), lambda v: None, clock=clock.now, sleep=clock.sleep, watch_every=None)
        p.on_event(events.append)
        return p, screen, events

    def test_a_ringing_call_is_told_with_the_number_as_seen_and_answered_only_when_told(self):
        p, screen, events = self.setup()
        screen.now = "ringing_in"
        p.poll_state()
        self.assertEqual(events, [{"kind": "incoming", "phone_seen": "090-0000-0000"}])   # 形をそろえるのはサーバー
        self.assertTrue(p.in_call())                                              # 鳴っているあいだは発信しない
        with self.assertRaises(CallNotPlaced):
            p.start_call("+819000000001", national="09000000001")
        self.assertEqual(screen.answered, 0)                                      # 言われるまで出ない
        p.answer_incoming()
        self.assertEqual(screen.answered, 1)
        p.poll_state()
        self.assertEqual(events[-1], {"kind": "answered"})

    def test_a_call_that_stopped_ringing_is_told_and_cannot_be_answered(self):
        p, screen, events = self.setup()
        screen.now = "ringing_in"
        p.poll_state()
        screen.now = "idle"
        p.poll_state()
        self.assertEqual(events[-1], {"kind": "incoming_ended"})
        with self.assertRaises(CallNotPlaced):
            p.answer_incoming()
        self.assertEqual(screen.answered, 0)

    def test_the_real_screen_does_not_answer_until_its_parts_are_read(self):
        from salescall.agent.phones import screen_uia
        self.assertEqual(screen_uia.UiaScreen.incoming_number(object.__new__(screen_uia.UiaScreen)), "")
        with self.assertRaises(NotImplementedError):
            screen_uia.UiaScreen.answer(object.__new__(screen_uia.UiaScreen))


class SameShapeAsThePhoneOfTheLink(unittest.TestCase):
    """電話の部品は、PC の部品の土台が呼ぶ形（CL-2 の `link/phone.Phone`）とそろっている（ずれると、土台が呼んだときに落ちる。2026-11-21：知らせの部品で
    見守り役の呼び方とずれていたのを見つけた＝同じことを電話の部品でも起こさないための見張り）。名前で渡す引数（* のあと）は、名前がそろっていること。"""

    def test_every_operation_of_the_phone_is_there_with_the_same_named_arguments(self):
        import inspect
        from salescall.link.phone import Phone
        ops = [n for n, f in vars(Phone).items() if callable(f) and not n.startswith("_")]
        self.assertIn("play", ops)
        for name in ops:
            with self.subTest(op=name):
                self.assertTrue(hasattr(rakuten_link.RakutenLink, name))
                want = inspect.signature(getattr(Phone, name)).parameters.values()
                have = inspect.signature(getattr(rakuten_link.RakutenLink, name)).parameters
                for p in want:
                    if p.kind is p.KEYWORD_ONLY:
                        self.assertIn(p.name, have)                                  # 名前で渡す引数は同じ名前で受ける
                pos = lambda ps: [p for p in ps if p.kind is p.POSITIONAL_OR_KEYWORD]   # noqa: E731
                self.assertGreaterEqual(len(pos(have.values())), len(pos(want)))         # 呼ぶ側の位置の引数を、すべて受ける
                self.assertLessEqual(len([p for p in pos(have.values()) if p.default is p.empty]),
                                     len([p for p in pos(want) if p.default is p.empty]))   # 呼ぶ側が渡さないものを求めない
