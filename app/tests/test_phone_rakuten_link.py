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

    def play(self, pcm):
        self.played.append(("play", len(pcm)))

    def play_wait(self, pcm):
        self.played.append(("wait", len(pcm)))

    def stop(self):
        self.stopped += 1


def phone(screen=None, recordings=lambda v: (b"\x01\x00" * 160, b"\x02\x00" * 160)):
    clock = Clock()
    p = rakuten_link.RakutenLink(screen or FakeScreen(), FakeSound(), recordings, clock=clock.now, sleep=clock.sleep, watch_every=None)
    return p, clock


class Placing(unittest.TestCase):
    def test_a_call_is_placed_only_when_linked_and_the_screen_starts_calling(self):
        p, _ = phone()
        p.start_call("+81300000001", "声A")
        self.assertEqual(p.screen.dialed, ["+81300000001"])
        self.assertTrue(p.in_call())

    def test_not_linked_or_unknown_link_is_not_placed(self):
        for linked in (False, None):
            with self.subTest(linked=linked):
                p, _ = phone(FakeScreen(linked=linked))
                with self.assertRaises(CallNotPlaced):
                    p.start_call("+81300000001")
                self.assertEqual(p.screen.dialed, [])

    def test_a_screen_that_never_starts_calling_is_not_placed(self):
        p, clock = phone(FakeScreen(goes_to="idle"))
        with self.assertRaises(CallNotPlaced):
            p.start_call("+81300000001")
        self.assertGreaterEqual(clock.t, rakuten_link.DIAL_WAIT)

    def test_a_button_that_cannot_be_pressed_is_not_placed(self):
        p, _ = phone(FakeScreen(fails=True))
        with self.assertRaises(CallNotPlaced):
            p.start_call("+81300000001")

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
        p.start_call("+81300000001")
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
        self.assertTrue(p.test_ring("+81300000009"))
        self.assertEqual(p.screen.hung, 1)                                   # 相手が出る前に切る

    def test_busy_heard_also_means_the_line_can_call(self):
        p, clock = phone()
        clock.on_sleep.append(lambda t: p.sound.feed(tone(400, 0.1) if int(t * 10) % 10 < 5 else silence(0.1)))
        self.assertTrue(p.test_ring("+81300000009"))

    def test_not_placed_is_false_and_silence_is_unknown(self):
        p, _ = phone(FakeScreen(goes_to="idle"))
        self.assertFalse(p.test_ring("+81300000009"))
        p, clock = phone()
        clock.on_sleep.append(lambda t: p.sound.feed(silence(0.1)))
        self.assertIsNone(p.test_ring("+81300000009"))                      # 見分けられなければ決めつけない


class Numbers(unittest.TestCase):
    def test_only_the_domestic_digits_are_typed_and_nothing_is_reshaped_here(self):
        from salescall.agent.phones.screen_uia import DOMESTIC
        self.assertTrue(DOMESTIC.fullmatch("0300000001") and DOMESTIC.fullmatch("09000000001"))
        for bad in ("+81300000001", "03-0000-0001", "300000001", ""):
            self.assertIsNone(DOMESTIC.fullmatch(bad), bad)                    # 形を変えずに断る（形を変えるのは core/phone の1か所）


class Apology(unittest.TestCase):
    def test_ask_wait_for_the_reply_then_bye_then_hang_up(self):
        p, clock = phone()
        p.start_call("+81300000001", "声A")
        p.screen.now = "in_call"
        clock.on_sleep.append(lambda t: p.sound.feed(tone(300, 0.05, amp=6000) if t < 1.5 else silence(0.05)))
        p.play_apology_and_hang_up()
        self.assertEqual([k for k, _ in p.sound.played], ["wait", "wait"])   # ひとつ目・ふたつ目
        self.assertEqual(p.screen.hung, 1)
        self.assertLess(clock.t, rakuten_link.REPLY_LISTEN)                 # 返事が終われば待たずに進む

    def test_without_the_recordings_it_hangs_up_at_once(self):
        p, _ = phone(recordings=lambda v: None)
        p.start_call("+81300000001")
        p.play_apology_and_hang_up()
        self.assertEqual(p.sound.played, [])
        self.assertEqual(p.screen.hung, 1)

    def test_not_in_a_call_does_nothing(self):
        p, _ = phone()
        p.play_apology_and_hang_up()
        self.assertEqual(p.screen.hung, 0)


if __name__ == "__main__":
    unittest.main()
