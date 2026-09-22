"""Deterministic control tests and bounded worker recovery, without LEDs."""
import copy
import json
import tempfile
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server'))
from lighting import Lighting, MoodFilter, USBStrip, PALETTE, rgb, adalight_frame, load_config


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
        self.cfg.pop('current_alignment', None)
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
        cfg.pop('current_alignment', None)
        sent = []
        connects = []
        failed = threading.Event()
        class FakeUSB:
            def __init__(self, cfg):
                pass
            def connect(self):
                connects.append(time.monotonic())
                return {'version': 'fake', 'led_count': cfg['led_count']}
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
            self.assertEqual(sent[-1][1], ((0, 54, 42),) * cfg['led_count'])
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




def result(labels, reverse=False, uncertain=(), mirror=True):
    faces=[dict(x1=i*200+20,y1=30,x2=i*200+150,y2=180,
                emotion=dict(label=label,label_zh=label,uncertain=i in uncertain))
           for i,label in enumerate(labels)]
    return dict(count=len(faces),faces=list(reversed(faces)) if reverse else faces,
                frame=dict(w=1600,h=900),mirror=mirror)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        cfg=load_config(Path(__file__).resolve().parents[1]/'configs/light.json')
        cfg['group_mode']='spatial'
        cfg.pop('current_alignment', None)  # Also retain the percentage-mode contract.
        cfg.update(brightness=.1, mixed_brightness=.6, group_brightness=[.35,.6])
        self.light=Lighting(cfg,clock=lambda:self.now)
        self.light.session('start','group-test')
        self.seq=0

    def put(self,now,labels,**kw):
        self.now=now;self.seq+=1
        payload=result(labels,**kw)
        state=self.light.accept(self.light.ticket('group-test',self.seq),payload)
        return state,payload

    def test_all_agree_tiers_and_no_three_person_cap(self):
        for n,power in [(1,.1),(2,.35),(3,.35),(4,.6),(6,.6)]:
            self.light.session('stop','group-test');self.light.session('start','group-test')
            start=self.now+.1
            self.put(start,['happy']*n)
            state,_=self.put(start+.35,['happy']*n)
            self.assertTrue(state['group']['agreed'])
            self.assertEqual(len(state['group']['people']),n)
            self.assertEqual(state['transition']['to_power'],power)

    def test_mixed_proportions_spatial_pixels_and_mirror(self):
        labels=['happy','happy','sad']
        self.put(0,labels)
        state,_=self.put(.35,labels)
        self.assertEqual(state['current']['label'],'mixed')
        self.assertEqual({c['label']:c['count'] for c in state['group']['composition']},{'happy':2,'sad':1})
        pixels=state['transition']['to_pixels']
        self.assertEqual(pixels[0],rgb(PALETTE['sad']))
        self.assertEqual(pixels[-1],rgb(PALETTE['happy']))
        self.assertTrue(any(p not in [rgb(PALETTE['sad']),rgb(PALETTE['happy'])] for p in pixels))
        self.assertEqual(state['transition']['to_power'],.6)
        state,_=self.put(.7,labels,mirror=False)
        self.assertEqual(state['transition']['to_pixels'][0],rgb(PALETTE['happy']))

    def test_detector_order_and_motion_keep_ids(self):
        _,first=self.put(0,['happy','sad'])
        state,second=self.put(.35,['happy','sad'],reverse=True)
        self.assertEqual([p['track_id'] for p in first['faces']],[p['track_id'] for p in reversed(second['faces'])])
        moving=result(['happy','sad'])
        for face in moving['faces']:
            face['x1']+=20;face['x2']+=20
        self.now=.7
        self.light.accept(self.light.ticket('group-test',3),moving)
        self.assertEqual([p['track_id'] for p in first['faces']],[p['track_id'] for p in moving['faces']])

    def test_pending_prevents_boost_and_uncertain_expires_independently(self):
        self.put(0,['happy','happy'],uncertain=(1,))
        state,_=self.put(.35,['happy','happy'],uncertain=(1,))
        self.assertFalse(state['group']['agreed'])
        self.assertEqual(state['transition']['to_power'],.1)
        self.put(.7,['happy','sad'])
        self.put(1.05,['happy','sad'])
        state,_=self.put(1.4,['happy','sad'],uncertain=(1,))
        self.assertEqual(state['current']['label'],'mixed')
        state,_=self.put(2.31,['happy','sad'],uncertain=(1,))
        self.assertEqual(state['group']['confirmed_count'],1)
        self.assertEqual(state['transition']['to_power'],.1)
        self.assertFalse(state['group']['agreed'])

    def test_leave_grace_return_single_stop_and_epoch(self):
        self.put(0,['happy','sad']);self.put(.35,['happy','sad'])
        state,_=self.put(.7,['happy'])
        self.assertEqual(state['group']['confirmed_count'],2)
        self.now=1.59
        self.assertEqual(self.light.snapshot()['group']['confirmed_count'],2)
        state,_=self.put(1.61,['happy'])
        self.assertEqual(len(state['group']['people']),1)
        self.assertEqual(state['current']['label'],'happy')
        ticket=self.light.ticket('group-test',99)
        self.light.session('stop','group-test')
        state=self.light.accept(ticket,result(['sad']*4))
        self.assertEqual(state['group']['people'],[])
        self.assertEqual(state['mode'],'standby')

    def test_no_faces_and_stall_return_standby(self):
        self.put(0,['happy','sad']);self.put(.35,['happy','sad'])
        self.put(.7,[])
        self.now=1.61
        self.assertEqual(self.light.snapshot()['mode'],'standby')
        self.put(2,['happy','sad']);self.put(2.35,['happy','sad'])
        self.now=4.26
        self.assertEqual(self.light.snapshot()['mode'],'standby')

    def test_pixel_packet_keeps_distinct_colors(self):
        packet=adalight_frame([(255,0,0),(0,0,255)],2)
        self.assertEqual(packet,b'Ada\x00\x01\x54\xff\x00\x00\x00\x00\xff')
        with self.assertRaises(ValueError):adalight_frame([(1,2,3)],2)

    def test_cycle_keeps_composition_and_obeys_owner(self):
        self.assertFalse(self.light.layout('cycle','observer'))
        self.assertTrue(self.light.layout('cycle','group-test'))
        self.put(0,['happy','happy','sad']);state,_=self.put(.35,['happy','happy','sad'])
        key=state['group']['key']
        self.assertEqual(state['output_label'],'happy')
        for t in [.7,1.05,1.4,1.75,2.1,2.45,2.8]:
            state,_=self.put(t,['happy','happy','sad'])
        self.assertEqual(state['output_label'],'sad')
        self.assertEqual(state['group']['key'],key)
        self.assertEqual(state['transition']['to_pixels'],[rgb(PALETTE['sad'])]*self.light.cfg['led_count'])
        self.assertEqual(state['transition']['to_power'],.6)



class CurrentAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / 'configs/light.json')
        self.cfg.update(led_count=60, mixed_brightness=.6, standby_brightness=.06,
                        current_alignment=dict(targets_ma=[220,450,700],led_ma=55,
                                               controller_bri=128,reserve_ma=120,limit_ma=850))
        self.now = 0.
        self.light = Lighting(self.cfg, clock=lambda: self.now)
        self.light.session('start', 'test')
        self.seq = 0

    def put(self, labels, uncertain=()):
        self.now += .35
        self.seq += 1
        return self.light.accept(self.light.ticket('test', self.seq), result(labels, uncertain=uncertain))

    def test_palette_tiers_and_integer_output(self):
        for label in PALETTE:
            for count, target in [(1,220), (2,450), (3,450), (4,700), (6,700)]:
                with self.subTest(label=label, count=count):
                    self.light.session('stop', 'test')
                    self.light.session('start', 'test')
                    self.put([label]*count)
                    state = self.put([label]*count)
                    self.now += .5
                    state = self.light.snapshot()
                    self.assertEqual(state['current_target_ma'], target)
                    self.assertTrue(state['group']['agreed'])
                    channels = sum(round(c*state['power']) for p in state['pixels'] for c in p)
                    estimated = 120 + channels*55*128//(765*255)
                    self.assertLessEqual(abs(estimated-target), 4)
                    self.assertLess(estimated, 790)
                    self.assertTrue(0 <= state['power'] <= 1)

    def test_mixed_pending_and_standby(self):
        self.put(['happy','sad']); state=self.put(['happy','sad'])
        self.assertIsNone(state['current_target_ma'])
        self.assertEqual(state['transition']['to_power'], .6)
        self.put(['happy','sad'], uncertain=(1,))
        for _ in range(3):
            state=self.put(['happy','sad'], uncertain=(1,))
        self.assertEqual(state['current_target_ma'],220)
        self.assertFalse(state['group']['agreed'])
        self.light.session('stop','test')
        self.now += .5
        state=self.light.snapshot()
        self.assertIsNone(state['current_target_ma'])
        self.assertEqual(state['power'], .06)

    def test_legacy_config_and_invalid_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'light.json'
            for bad in [dict(targets_ma=[220,450,850]),dict(targets_ma=[450,220,700]),
                        dict(led_ma=0),dict(controller_bri=0),dict(targets_ma=[220,float('nan'),700])]:
                cfg=copy.deepcopy(self.cfg);cfg['current_alignment'].update(bad)
                path.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):load_config(path)
            cfg=copy.deepcopy(self.cfg);cfg.pop('current_alignment')
            path.write_text(json.dumps(cfg))
            self.assertNotIn('current_alignment',load_config(path))



if __name__ == '__main__': unittest.main(verbosity=2)
