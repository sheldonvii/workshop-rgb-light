"""Deterministic control tests and bounded worker recovery, without LEDs."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server'))
from lighting import Lighting, MoodFilter, USBStrip, adalight_frame, load_config


def primary(label='happy', uncertain=False):
    return {'label': label, 'label_zh': label, 'uncertain': uncertain}


class Clock:
    now = 0.
    def __call__(self):
        return self.now


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.f = MoodFilter()

    def put(self, label, t, uncertain=False, count=1):
        self.f.accept(primary(label, uncertain), count, t)

    def test_continuity_and_cooldown(self):
        self.put('happy', 0)
        self.f.tick(.3)
        self.assertIsNone(self.f.current)
        self.put('happy', .35)
        self.assertEqual(self.f.current['label'], 'happy')
        self.put('sad', .4)
        self.put('sad', .71)
        self.assertEqual(self.f.current['label'], 'happy')
        self.put('sad', .96)
        self.assertEqual(self.f.current['label'], 'sad')

    def test_majority_with_brief_uncertainty(self):
        for label, t, uncertain in [('happy', 0, False), ('happy', .25, True), ('happy', .5, False), ('happy', .75, False)]:
            self.put(label, t, uncertain)
        self.assertEqual(self.f.current['label'], 'happy')

    def test_noise_never_confirms(self):
        for n in range(20):
            self.put('sad' if n % 2 else 'happy', n * .25)
        self.assertIsNone(self.f.current)

    def test_invalid_grace_and_stall(self):
        for count, uncertain in [(0, False), (2, False), (1, True)]:
            self.f.reset()
            self.put('happy', 0)
            self.put('happy', .35)
            self.put('sad', .4, uncertain, count)
            self.f.tick(1.29)
            self.assertEqual(self.f.current['label'], 'happy')
            self.f.tick(1.31)
            self.assertIsNone(self.f.current)
        self.f.reset()
        self.put('happy', 0)
        self.put('happy', .35)
        self.f.tick(2.26)
        self.assertIsNone(self.f.current)

    def test_duplicates_gap_and_multiple_clear_evidence(self):
        for t in [0, 0, -.1]:
            self.put('happy', t)
        self.assertIsNone(self.f.current)
        self.put('happy', 1.1)
        self.assertIsNone(self.f.current)
        self.put('happy', 1.2, count=2)
        self.put('happy', 1.3)
        self.assertIsNone(self.f.current)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = load_config(ROOT / 'configs/light.json')
        self.light = Lighting(self.cfg, self.clock)
        self.light.session('start', 'owner')

    def put(self, t, seq, label='happy', count=1, uncertain=False):
        self.clock.now = t
        ticket = self.light.ticket('owner', seq)
        return self.light.accept(ticket, {'primary': primary(label, uncertain), 'count': count})

    def test_session_conflict_and_observer_cannot_stop(self):
        self.assertFalse(self.light.session('start', 'other'))
        self.light.session('stop', 'other')
        self.assertEqual(self.light.snapshot()['owner'], 'owner')
        self.assertIsNone(self.light.ticket('other', 1))

    def test_stop_drops_inflight_result(self):
        self.put(0, 1)
        self.clock.now = .35
        ticket = self.light.ticket('owner', 2)
        self.light.session('stop', 'owner')
        self.light.accept(ticket, {'primary': primary(), 'count': 1})
        self.assertIsNone(self.light.snapshot()['current'])

    def test_pause_resume_epoch_and_stale_result(self):
        ticket = self.light.ticket('owner', 1)
        self.light.session('pause', 'owner')
        self.light.session('resume', 'owner')
        self.light.accept(ticket, {'primary': primary(), 'count': 1})
        self.assertIsNone(self.light.filter.last_sample)
        ticket = self.light.ticket('owner', 2)
        self.clock.now = 1.6
        self.light.accept(ticket, {'primary': primary(), 'count': 1})
        self.assertIsNone(self.light.filter.last_sample)

    def test_duplicate_and_old_sequence(self):
        self.put(0, 2)
        self.put(.35, 2)
        self.put(.5, 1)
        self.assertIsNone(self.light.snapshot()['current'])
        self.put(.7, 3)
        self.assertEqual(self.light.snapshot()['current']['label'], 'happy')

    def test_lease_timeout_and_heartbeat(self):
        self.clock.now = 7
        self.light.session('start', 'owner')
        self.clock.now = 9
        self.assertEqual(self.light.snapshot()['owner'], 'owner')
        self.clock.now = 15.1
        self.assertIsNone(self.light.snapshot()['owner'])
        self.assertEqual(self.light.snapshot()['mode'], 'standby')
        self.assertTrue(self.light.session('start', 'other'))

    def test_trajectory_retarget_starts_at_displayed_color(self):
        initial = self.light.snapshot()['rgb']
        self.put(0, 1)
        first = self.put(.35, 2)
        self.assertEqual(first['rgb'], initial)
        self.clock.now = .55
        middle = self.light.snapshot()
        self.assertNotEqual(middle['rgb'], initial)
        self.assertNotEqual(middle['rgb'], middle['transition']['to'])
        self.light.session('stop', 'owner')
        stopped = self.light.snapshot()
        self.assertEqual(stopped['rgb'], middle['rgb'])
        self.clock.now = .96
        final = self.light.snapshot()
        self.assertEqual(final['rgb'], initial)
        self.assertEqual(final['power'], self.cfg['standby_brightness'])

    def test_held_color_then_standby(self):
        self.put(0, 1)
        self.put(.35, 2)
        self.put(.8, 3, uncertain=True)
        self.clock.now = 1.69
        self.assertEqual(self.light.snapshot()['mode'], 'emotion')
        self.clock.now = 1.71
        self.assertEqual(self.light.snapshot()['mode'], 'standby')

    def test_packet(self):
        frame = adalight_frame((1, 2, 3), 30)
        self.assertEqual(frame[:6], b'Ada\x00\x1d\x48')
        self.assertEqual(frame[6:], bytes([1, 2, 3]) * 30)


class WorkerTests(unittest.TestCase):
    def test_disconnect_reconnect_latest_color_and_keepalive(self):
        cfg = load_config(ROOT / 'configs/light.json')
        cfg['keepalive_ms'] = 100
        cfg['brightness'] = 0.25  # Fixed fixture, independent of deployment brightness.
        sent = []
        connects = []
        failed = threading.Event()
        class FakeUSB:
            def __init__(self, cfg):
                pass
            def connect(self):
                connects.append(time.monotonic())
                return {'version': 'fake', 'led_count': 30}
            def send(self, color):
                if not failed.is_set():
                    failed.set()
                    raise OSError('simulated unplug')
                sent.append((time.monotonic(), color))
            def close(self):
                pass
        light = Lighting(cfg, transport_factory=FakeUSB)
        light.start()
        try:
            self.assertTrue(failed.wait(1))
            light.session('start', 'test')
            deadline = time.monotonic() + 3.3
            seq = 0
            while time.monotonic() < deadline and len(sent) < 3:
                seq += 1
                ticket = light.ticket('test', seq)
                light.accept(ticket, {'primary': primary('neutral'), 'count': 1})
                time.sleep(.16)
            self.assertGreaterEqual(len(connects), 2)
            self.assertGreaterEqual(len(sent), 3)
            self.assertEqual(light.snapshot()['device']['status'], 'connected')
            self.assertEqual(sent[-1][1], (0, 54, 42))
            self.assertEqual(sent[-1][1], sent[-2][1])
        finally:
            light.close()
        self.assertFalse(light.thread.is_alive())


class SerialHangupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PTY hangup semantics')
    def test_real_terminal_hangup_releases_fd_and_worker_reconnects(self):
        import os
        import pty
        import termios
        cfg = load_config(ROOT / 'configs/light.json')
        cfg['keepalive_ms'] = 100
        transports = []
        recovered = threading.Event()

        class PseudoUSB(USBStrip):
            def connect(self):
                self.master, self.fd = pty.openpty()
                self.original = termios.tcgetattr(self.fd)
                self.saved_fd = self.fd
                transports.append(self)
                return {'version': 'pty-test', 'led_count': cfg['led_count']}

            def send(self, color):
                if self is transports[0] and self.master is not None:
                    os.close(self.master)
                    self.master = None
                super().send(color)
                recovered.set()

            def close(self):
                super().close()
                if getattr(self, 'master', None) is not None:
                    os.close(self.master)
                    self.master = None

        light = Lighting(cfg, transport_factory=PseudoUSB)
        light.start()
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and light.snapshot()['device']['status'] != 'disconnected':
                time.sleep(.01)
            self.assertEqual(light.snapshot()['device']['status'], 'disconnected')
            self.assertTrue(light.thread.is_alive())
            first = transports[0]
            self.assertIsNone(first.fd)
            with self.assertRaises(OSError):
                os.fstat(first.saved_fd)
            first.close()  # Repeated cleanup must be harmless.
            self.assertTrue(recovered.wait(4))
            self.assertGreaterEqual(len(transports), 2)
            self.assertEqual(light.snapshot()['device']['status'], 'connected')
        finally:
            light.close()
        self.assertFalse(light.thread.is_alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
