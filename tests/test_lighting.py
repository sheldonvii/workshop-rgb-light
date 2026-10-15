"""离线验证逐脸确认、群体灯态与模拟 USB，不连接真实灯带。"""

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
from lighting import (
    PALETTE,
    Lighting,
    MoodFilter,
    USBStrip,
    adalight_frame,
    load_config,
    rgb,
)


def primary(label="happy", uncertain=False):
    return {"label": label, "label_zh": label, "uncertain": uncertain}


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.f = MoodFilter()

    def put(self, label, t, uncertain=False, count=1):
        self.f.accept(primary(label, uncertain), count, t)

    def test_continuity_and_cooldown(self):
        self.put("happy", 0)
        self.f.tick(0.3)
        self.assertIsNone(self.f.current)
        self.put("happy", 0.35)
        self.assertEqual(self.f.current["label"], "happy")
        self.put("sad", 0.4)
        self.put("sad", 0.71)
        self.assertEqual(self.f.current["label"], "happy")
        self.put("sad", 0.96)
        self.assertEqual(self.f.current["label"], "sad")

    def test_majority_with_brief_uncertainty(self):
        for label, t, uncertain in [
            ("happy", 0, False),
            ("happy", 0.25, True),
            ("happy", 0.5, False),
            ("happy", 0.75, False),
        ]:
            self.put(label, t, uncertain)
        self.assertEqual(self.f.current["label"], "happy")

    def test_noise_never_confirms(self):
        for n in range(20):
            self.put("sad" if n % 2 else "happy", n * 0.25)
        self.assertIsNone(self.f.current)

    def test_invalid_grace_and_stall(self):
        for count, uncertain in [(0, False), (2, False), (1, True)]:
            self.f.reset()
            self.put("happy", 0)
            self.put("happy", 0.35)
            self.put("sad", 0.4, uncertain, count)
            self.f.tick(1.29)
            self.assertEqual(self.f.current["label"], "happy")
            self.f.tick(1.31)
            self.assertIsNone(self.f.current)
        self.f.reset()
        self.put("happy", 0)
        self.put("happy", 0.35)
        self.f.tick(2.26)
        self.assertIsNone(self.f.current)

    def test_duplicates_gap_and_multiple_clear_evidence(self):
        for t in [0, 0, -0.1]:
            self.put("happy", t)
        self.assertIsNone(self.f.current)
        self.put("happy", 1.1)
        self.assertIsNone(self.f.current)
        self.put("happy", 1.2, count=2)
        self.put("happy", 1.3)
        self.assertIsNone(self.f.current)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = load_config(ROOT / "configs/light.json")
        self.cfg.pop("current_alignment", None)
        self.light = Lighting(self.cfg, self.clock)
        self.light.session("start", "owner")

    def put(self, t, seq, label="happy", count=1, uncertain=False):
        self.clock.now = t
        ticket = self.light.ticket("owner", seq)
        return self.light.accept(
            ticket,
            result([label] * count, uncertain=tuple(range(count)) if uncertain else ()),
        )

    def test_session_conflict_and_observer_cannot_stop(self):
        self.assertFalse(self.light.session("start", "other"))
        self.light.session("stop", "other")
        self.assertEqual(self.light.snapshot()["owner"], "owner")
        self.assertIsNone(self.light.ticket("other", 1))

    def test_stop_drops_inflight_result(self):
        self.put(0, 1)
        self.clock.now = 0.35
        ticket = self.light.ticket("owner", 2)
        self.light.session("stop", "owner")
        self.light.accept(ticket, result(["happy"]))
        self.assertIsNone(self.light.snapshot()["current"])

    def test_pause_resume_epoch_and_stale_result(self):
        ticket = self.light.ticket("owner", 1)
        self.light.session("pause", "owner")
        self.light.session("resume", "owner")
        self.light.accept(ticket, result(["happy"]))
        self.assertIsNone(self.light.filter.last_sample)
        ticket = self.light.ticket("owner", 2)
        self.clock.now = 1.6
        self.light.accept(ticket, result(["happy"]))
        self.assertIsNone(self.light.filter.last_sample)

    def test_duplicate_and_old_sequence(self):
        self.put(0, 2)
        self.put(0.35, 2)
        self.put(0.5, 1)
        self.assertIsNone(self.light.snapshot()["current"])
        self.put(0.7, 3)
        self.assertEqual(self.light.snapshot()["current"]["label"], "happy")

    def test_lease_timeout_and_heartbeat(self):
        self.clock.now = 7
        self.light.session("start", "owner")
        self.clock.now = 9
        self.assertEqual(self.light.snapshot()["owner"], "owner")
        self.clock.now = 15.1
        self.assertIsNone(self.light.snapshot()["owner"])
        self.assertEqual(self.light.snapshot()["mode"], "standby")
        self.assertTrue(self.light.session("start", "other"))

    def test_trajectory_retarget_starts_at_displayed_color(self):
        initial = self.light.snapshot()["rgb"]
        self.put(0, 1)
        first = self.put(0.35, 2)
        self.assertEqual(first["rgb"], initial)
        self.clock.now = 0.55
        middle = self.light.snapshot()
        self.assertNotEqual(middle["rgb"], initial)
        self.assertNotEqual(middle["rgb"], middle["transition"]["to"])
        self.light.session("stop", "owner")
        stopped = self.light.snapshot()
        self.assertEqual(stopped["rgb"], middle["rgb"])
        self.clock.now = 0.96
        final = self.light.snapshot()
        self.assertEqual(final["rgb"], initial)
        self.assertEqual(final["power"], self.cfg["standby_brightness"])

    def test_held_color_then_standby(self):
        self.put(0, 1)
        self.put(0.35, 2)
        self.put(0.8, 3, uncertain=True)
        self.clock.now = 1.69
        self.assertEqual(self.light.snapshot()["mode"], "emotion")
        self.clock.now = 1.71
        self.assertEqual(self.light.snapshot()["mode"], "standby")

    def test_complete_sixty_pixel_packet(self):
        # 帧头独立给出预期，首尾异色可发现截断、重复或 RGB 顺序错误。
        pixels = (
            [(255, 0, 0)] + [(i, i + 1, i + 2) for i in range(1, 59)] + [(0, 0, 255)]
        )
        expected = b"Ada\x00\x3b\x6e" + bytes(
            channel for pixel in pixels for channel in pixel
        )
        frame = adalight_frame(pixels, 60)
        self.assertEqual(frame, expected)
        self.assertEqual(len(frame), 186)
        self.assertEqual(frame[6:9], b"\xff\x00\x00")
        self.assertEqual(frame[-3:], b"\x00\x00\xff")


class WorkerTests(unittest.TestCase):
    def test_disconnect_reconnect_latest_color_and_keepalive(self):
        cfg = load_config(ROOT / "configs/light.json")
        cfg["keepalive_ms"] = 100
        cfg["brightness"] = 0.25  # 固定夹具，与部署亮度配置独立。
        cfg.pop("current_alignment", None)
        sent = []
        connects = []
        failed = threading.Event()

        class FakeUSB:
            def __init__(self, cfg):
                pass

            def connect(self):
                connects.append(time.monotonic())
                return {"version": "fake", "led_count": cfg["led_count"]}

            def send(self, color):
                if not failed.is_set():
                    failed.set()
                    raise OSError("simulated unplug")
                sent.append((time.monotonic(), color))

            def close(self):
                pass

        light = Lighting(cfg, transport_factory=FakeUSB)
        light.start()
        try:
            self.assertTrue(failed.wait(1))
            light.session("start", "test")
            deadline = time.monotonic() + 3.3
            seq = 0
            while time.monotonic() < deadline and len(sent) < 3:
                seq += 1
                ticket = light.ticket("test", seq)
                light.accept(ticket, result(["neutral"]))
                time.sleep(0.16)
            self.assertGreaterEqual(len(connects), 2)
            self.assertGreaterEqual(len(sent), 3)
            self.assertEqual(light.snapshot()["device"]["status"], "connected")
            self.assertEqual(sent[-1][1], ((0, 54, 42),) * cfg["led_count"])
            self.assertEqual(sent[-1][1], sent[-2][1])
        finally:
            light.close()
        self.assertFalse(light.thread.is_alive())


class SerialHangupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PTY 断开语义")
    def test_real_terminal_hangup_releases_fd_and_worker_reconnects(self):
        import os
        import pty
        import termios

        cfg = load_config(ROOT / "configs/light.json")
        cfg["keepalive_ms"] = 100
        transports = []
        recovered = threading.Event()

        class PseudoUSB(USBStrip):
            def connect(self):
                self.master, self.fd = pty.openpty()
                self.original = termios.tcgetattr(self.fd)
                self.saved_fd = self.fd
                transports.append(self)
                return {"version": "pty-test", "led_count": cfg["led_count"]}

            def send(self, color):
                if self is transports[0] and self.master is not None:
                    os.close(self.master)
                    self.master = None
                super().send(color)
                recovered.set()

            def close(self):
                super().close()
                if getattr(self, "master", None) is not None:
                    os.close(self.master)
                    self.master = None

        light = Lighting(cfg, transport_factory=PseudoUSB)
        light.start()
        try:
            deadline = time.monotonic() + 1
            while (
                time.monotonic() < deadline
                and light.snapshot()["device"]["status"] != "disconnected"
            ):
                time.sleep(0.01)
            self.assertEqual(light.snapshot()["device"]["status"], "disconnected")
            self.assertTrue(light.thread.is_alive())
            first = transports[0]
            self.assertIsNone(first.fd)
            with self.assertRaises(OSError):
                os.fstat(first.saved_fd)
            first.close()  # 重复收尾不得再访问已关闭描述符。
            self.assertTrue(recovered.wait(4))
            self.assertGreaterEqual(len(transports), 2)
            self.assertEqual(light.snapshot()["device"]["status"], "connected")
        finally:
            light.close()
        self.assertFalse(light.thread.is_alive())


def result(labels, reverse=False, uncertain=(), mirror=True):
    faces = [
        dict(
            x1=i * 200 + 20,
            y1=30,
            x2=i * 200 + 150,
            y2=180,
            emotion=dict(label=label, label_zh=label, uncertain=i in uncertain),
        )
        for i, label in enumerate(labels)
    ]
    return dict(
        count=len(faces),
        faces=list(reversed(faces)) if reverse else faces,
        frame=dict(w=1600, h=900),
        mirror=mirror,
    )


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        cfg = load_config(Path(__file__).resolve().parents[1] / "configs/light.json")
        cfg.pop("current_alignment", None)  # 备用百分比路径也保留覆盖。
        cfg.update(brightness=0.1, mixed_brightness=0.6, group_brightness=[0.35, 0.6])
        self.light = Lighting(cfg, clock=lambda: self.now)
        self.light.session("start", "group-test")
        self.seq = 0

    def put(self, now, labels, **kw):
        self.now = now
        self.seq += 1
        payload = result(labels, **kw)
        state = self.light.accept(self.light.ticket("group-test", self.seq), payload)
        return state, payload

    def test_all_agree_tiers_and_no_three_person_cap(self):
        for n, power in [(1, 0.1), (2, 0.35), (3, 0.35), (4, 0.6), (6, 0.6)]:
            self.light.session("stop", "group-test")
            self.light.session("start", "group-test")
            start = self.now + 0.1
            self.put(start, ["happy"] * n)
            state, _ = self.put(start + 0.35, ["happy"] * n)
            self.assertTrue(state["group"]["agreed"])
            self.assertEqual(len(state["group"]["people"]), n)
            self.assertEqual(state["transition"]["to_power"], power)

    def test_mixed_proportions_spatial_pixels_and_mirror(self):
        labels = ["happy", "happy", "sad"]
        self.put(0, labels)
        state, _ = self.put(0.35, labels)
        self.assertEqual(state["current"]["label"], "mixed")
        self.assertEqual(
            {c["label"]: c["count"] for c in state["group"]["composition"]},
            {"happy": 2, "sad": 1},
        )
        pixels = state["transition"]["to_pixels"]
        self.assertEqual(pixels[0], rgb(PALETTE["sad"]))
        self.assertEqual(pixels[-1], rgb(PALETTE["happy"]))
        self.assertTrue(
            any(p not in [rgb(PALETTE["sad"]), rgb(PALETTE["happy"])] for p in pixels)
        )
        self.assertEqual(state["transition"]["to_power"], 0.6)
        state, _ = self.put(0.7, labels, mirror=False)
        self.assertEqual(state["transition"]["to_pixels"][0], rgb(PALETTE["happy"]))

    def test_detector_order_and_motion_keep_ids(self):
        _, first = self.put(0, ["happy", "sad"])
        state, second = self.put(0.35, ["happy", "sad"], reverse=True)
        self.assertEqual(
            [p["track_id"] for p in first["faces"]],
            [p["track_id"] for p in reversed(second["faces"])],
        )
        moving = result(["happy", "sad"])
        for face in moving["faces"]:
            face["x1"] += 20
            face["x2"] += 20
        self.now = 0.7
        self.light.accept(self.light.ticket("group-test", 3), moving)
        self.assertEqual(
            [p["track_id"] for p in first["faces"]],
            [p["track_id"] for p in moving["faces"]],
        )

    def test_pending_prevents_boost_and_uncertain_expires_independently(self):
        self.put(0, ["happy", "happy"], uncertain=(1,))
        state, _ = self.put(0.35, ["happy", "happy"], uncertain=(1,))
        self.assertFalse(state["group"]["agreed"])
        self.assertEqual(state["transition"]["to_power"], 0.1)
        self.put(0.7, ["happy", "sad"])
        self.put(1.05, ["happy", "sad"])
        state, _ = self.put(1.4, ["happy", "sad"], uncertain=(1,))
        self.assertEqual(state["current"]["label"], "mixed")
        state, _ = self.put(2.31, ["happy", "sad"], uncertain=(1,))
        self.assertEqual(state["group"]["confirmed_count"], 1)
        self.assertEqual(state["transition"]["to_power"], 0.1)
        self.assertFalse(state["group"]["agreed"])

    def test_leave_grace_return_single_stop_and_epoch(self):
        self.put(0, ["happy", "sad"])
        self.put(0.35, ["happy", "sad"])
        state, _ = self.put(0.7, ["happy"])
        self.assertEqual(state["group"]["confirmed_count"], 2)
        self.now = 1.59
        self.assertEqual(self.light.snapshot()["group"]["confirmed_count"], 2)
        state, _ = self.put(1.61, ["happy"])
        self.assertEqual(len(state["group"]["people"]), 1)
        self.assertEqual(state["current"]["label"], "happy")
        ticket = self.light.ticket("group-test", 99)
        self.light.session("stop", "group-test")
        state = self.light.accept(ticket, result(["sad"] * 4))
        self.assertEqual(state["group"]["people"], [])
        self.assertEqual(state["mode"], "standby")

    def test_no_faces_and_stall_return_standby(self):
        self.put(0, ["happy", "sad"])
        self.put(0.35, ["happy", "sad"])
        self.put(0.7, [])
        self.now = 1.61
        self.assertEqual(self.light.snapshot()["mode"], "standby")
        self.put(2, ["happy", "sad"])
        self.put(2.35, ["happy", "sad"])
        self.now = 4.26
        self.assertEqual(self.light.snapshot()["mode"], "standby")

    def test_pixel_packet_keeps_distinct_colors(self):
        packet = adalight_frame([(255, 0, 0), (0, 0, 255)], 2)
        self.assertEqual(packet, b"Ada\x00\x01\x54\xff\x00\x00\x00\x00\xff")
        with self.assertRaises(ValueError):
            adalight_frame([(1, 2, 3)], 2)


class CurrentAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "configs/light.json")
        self.cfg.update(
            led_count=60,
            mixed_brightness=0.6,
            standby_brightness=0.06,
            current_alignment=dict(
                targets_ma=[220, 450, 700],
                led_ma=55,
                controller_bri=128,
                reserve_ma=120,
                limit_ma=850,
            ),
        )
        self.now = 0.0
        self.light = Lighting(self.cfg, clock=lambda: self.now)
        self.light.session("start", "test")
        self.seq = 0

    def put(self, labels, uncertain=()):
        self.now += 0.35
        self.seq += 1
        return self.light.accept(
            self.light.ticket("test", self.seq), result(labels, uncertain=uncertain)
        )

    def test_palette_tiers_and_integer_output(self):
        for label in PALETTE:
            for count, target in [(1, 220), (2, 450), (3, 450), (4, 700), (6, 700)]:
                with self.subTest(label=label, count=count):
                    self.light.session("stop", "test")
                    self.light.session("start", "test")
                    self.put([label] * count)
                    state = self.put([label] * count)
                    self.now += 0.5
                    state = self.light.snapshot()
                    self.assertEqual(state["current_target_ma"], target)
                    self.assertTrue(state["group"]["agreed"])
                    channels = sum(
                        round(c * state["power"]) for p in state["pixels"] for c in p
                    )
                    estimated = 120 + channels * 55 * 128 // (765 * 255)
                    self.assertLessEqual(abs(estimated - target), 4)
                    self.assertLess(estimated, 790)
                    self.assertTrue(0 <= state["power"] <= 1)

    def test_mixed_pending_and_standby(self):
        self.put(["happy", "sad"])
        state = self.put(["happy", "sad"])
        self.assertIsNone(state["current_target_ma"])
        self.assertEqual(state["transition"]["to_power"], 0.6)
        self.put(["happy", "sad"], uncertain=(1,))
        for _ in range(3):
            state = self.put(["happy", "sad"], uncertain=(1,))
        self.assertEqual(state["current_target_ma"], 220)
        self.assertFalse(state["group"]["agreed"])
        self.light.session("stop", "test")
        self.now += 0.5
        state = self.light.snapshot()
        self.assertIsNone(state["current_target_ma"])
        self.assertEqual(state["power"], 0.06)

    def test_percentage_config_and_invalid_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "light.json"
            for bad in [
                dict(targets_ma=[220, 450, 850]),
                dict(targets_ma=[450, 220, 700]),
                dict(led_ma=0),
                dict(controller_bri=0),
                dict(targets_ma=[220, float("nan"), 700]),
            ]:
                cfg = copy.deepcopy(self.cfg)
                cfg["current_alignment"].update(bad)
                path.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):
                    load_config(path)
            cfg = copy.deepcopy(self.cfg)
            cfg.pop("current_alignment")
            path.write_text(json.dumps(cfg))
            self.assertNotIn("current_alignment", load_config(path))


class ModelBoundaryTests(unittest.TestCase):
    def test_sdk_failure_status_is_not_silently_accepted(self):
        from emotion_models import vision_infer

        service = Mock()
        service.infer_image.return_value = ("failed", [])
        sdk = SimpleNamespace(VisionServiceStatus=SimpleNamespace(OK="ok"))
        with patch.dict(sys.modules, {"spacemit_vision": sdk}):
            with self.assertRaisesRegex(RuntimeError, "预热失败"):
                vision_infer(service, None, "预热")

    def test_internal_value_error_is_logged_as_server_failure(self):
        from app import ModelBundle

        bundle = ModelBundle()
        bundle.state = "ready"
        bundle.infer = Mock(side_effect=ValueError("内部故障"))
        with self.assertLogs("barometer", level="ERROR"):
            status, payload = bundle.infer_locked(b"image")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "infer_failed")

    def test_bad_image_keeps_client_error(self):
        from app import BadImageError, ModelBundle

        bundle = ModelBundle()
        bundle.state = "ready"
        bundle.infer = Mock(side_effect=BadImageError())
        status, payload = bundle.infer_locked(b"bad")
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_image"))

    def test_decoder_exception_is_a_bad_image(self):
        from app import BadImageError, decode_jpeg

        class DecodeFailure(Exception):
            pass

        cv2 = SimpleNamespace(
            error=DecodeFailure,
            IMREAD_COLOR=1,
            imdecode=Mock(side_effect=DecodeFailure("解码失败")),
        )
        with patch.dict(sys.modules, {"cv2": cv2, "numpy": Mock()}):
            with self.assertRaises(BadImageError):
                decode_jpeg(b"broken image")

    def test_official_out_of_range_label_is_rejected(self):
        from emotion_models import OfficialEmotionModel

        model = OfficialEmotionModel.__new__(OfficialEmotionModel)
        model.session = object()
        for label in (-1, 7, 99):
            with (
                self.subTest(label=label),
                patch(
                    "emotion_models.vision_infer",
                    return_value=[SimpleNamespace(label=label, score=1)],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "越界"):
                    model.predict(None)

    def test_failed_classifier_warmup_releases_candidate(self):
        # 只用内存对象验证生命周期，不加载模型或访问 NPU。
        from emotion_models import load_classifier

        candidate = Mock()
        candidate.predict.side_effect = RuntimeError("预热失败")
        modules = {"cv2": Mock(), "numpy": Mock()}
        with (
            patch.dict(sys.modules, modules),
            patch("emotion_models.EmotionModel", return_value=candidate),
        ):
            with self.assertRaisesRegex(RuntimeError, "预热失败"):
                load_classifier("hse-b0")
        candidate.release.assert_called_once()


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "串口配置测试需要 Linux PTY")
    def test_real_connect_validates_controller_settings(self):
        import fcntl
        import os
        import pty
        import select
        import termios

        original_ioctl = fcntl.ioctl

        def ioctl(fd, request, *args):
            # PTY 无 USB DTR 引脚，其余打开、独占、查询、解析均走生产 connect。
            if request == termios.TIOCMBIS:
                return 0
            return original_ioctl(fd, request, *args)

        scenarios = [
            (60, False, 0, 128, 850, None),
            (30, False, 0, 128, 850, "灯珠数量不一致"),
            (60, True, 0, 128, 850, "其他实时源"),
            (60, False, 1, 128, 850, "其他实时源"),
            (60, False, 0, 127, 850, "总亮度或限流"),
            (60, False, 0, 128, 900, "总亮度或限流"),
        ]
        for count, live, lor, brightness, limit, error in scenarios:
            with self.subTest(
                count=count, live=live, lor=lor, brightness=brightness, limit=limit
            ):
                master, slave = pty.openpty()
                cfg = {
                    **load_config(ROOT / "configs/light.json"),
                    "port": os.ttyname(slave),
                }
                strip = USBStrip(cfg)
                failures = []

                def respond():
                    try:
                        self.assertTrue(select.select([master], [], [], 2)[0])
                        self.assertEqual(os.read(master, 4096), b'{"v":true}\n')
                        reply = {
                            "info": {
                                "leds": {"count": count, "maxpwr": limit},
                                "live": live,
                                "ver": "pty",
                            },
                            "state": {"lor": lor, "bri": brightness},
                        }
                        os.write(master, json.dumps(reply).encode() + b"\n")
                    except Exception as exc:
                        failures.append(exc)

                responder = threading.Thread(target=respond)
                responder.start()
                try:
                    with patch("fcntl.ioctl", side_effect=ioctl):
                        if error:
                            with self.assertRaisesRegex(ValueError, error):
                                strip.connect()
                            self.assertIsNone(strip.fd)
                        else:
                            self.assertEqual(
                                strip.connect(), {"version": "pty", "led_count": 60}
                            )
                            self.assertTrue(select.select([master], [], [], 1)[0])
                            self.assertEqual(
                                os.read(master, 4096), b"Ada\x00\x3b\x6e" + bytes(180)
                            )
                finally:
                    strip.close()
                    responder.join(timeout=3)
                    os.close(slave)
                    os.close(master)
                self.assertFalse(responder.is_alive())
                self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
