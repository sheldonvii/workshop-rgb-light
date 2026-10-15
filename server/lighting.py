"""后端统一维护群体灯态，独立线程通过 USB 输出。"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

from group_mood import GroupMood, zone_pixels

PALETTE = {
    "happy": "#FFD000",
    "surprise": "#FF780A",
    "neutral": "#00D9A6",
    "sad": "#246BFF",
    "angry": "#FF2010",
    "fear": "#8000FF",
    "disgust": "#55DD00",
    "contempt": "#FF269C",
}
# 单调时钟秒；交互策略详见 docs/development.md 的“灯效与页面”。
# 每张脸独立确认；不可靠结果计入投票分母，轨迹缺席清空投票窗口。
TIMING = {
    "sample_gap_seconds": 1.0,
    "vote_window_seconds": 1.2,
    "expire_grace_seconds": 0.9,
    "confirm_seconds": 0.3,
    "confirm_samples": 2,
    "vote_samples": 3,
    "vote_ratio": 0.75,
    "change_interval_seconds": 0.6,
    "lease_seconds": 8,
    "heartbeat_ms": 2000,
    "ticket_seconds": 1.5,
}
USB_WRITE_TIMEOUT = 0.2
USB_QUERY_TIMEOUT = 2
USB_RETRY_SECONDS = 2


log = logging.getLogger("barometer.light")


def rgb(hex_color):
    return [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]


@lru_cache(maxsize=128)
def current_scale(pixels, target_ma, calibration):
    """按整数 RGB 量化估算整套电流；预留已计入，不再从目标额外扣除。"""

    led_ma, controller_bri, reserve_ma = calibration

    def estimate(scale):
        channels = sum(round(c * scale) for pixel in pixels for c in pixel)
        return reserve_ma + channels * led_ma * controller_bri // (765 * 255)

    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        if estimate(mid) < target_ma:
            lo = mid
        else:
            hi = mid
    return min((lo, hi), key=lambda s: (abs(estimate(s) - target_ma), s))


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        type(cfg.get("enabled")) is not bool
        or not isinstance(cfg.get("port"), str)
        or not cfg["port"]
    ):
        raise ValueError("灯带 enabled 必须为布尔值，port 必须为设备路径")
    for key, lo, hi in [
        ("led_count", 1, 1000),
        ("transition_ms", 100, 3000),
        ("fps", 1, 60),
        ("keepalive_ms", 100, 1000),
    ]:
        if type(cfg.get(key)) is not int or not lo <= cfg[key] <= hi:
            raise ValueError(f"灯带 {key} 必须为 {lo}～{hi} 的整数")
    cfg.setdefault("mixed_brightness", cfg.get("brightness"))
    for key in ("brightness", "mixed_brightness", "standby_brightness"):
        value = cfg.get(key)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"灯带 {key} 必须在 0～1 之间")
    color = cfg.get("standby_color", "")
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        raise ValueError("standby_color 必须为 #RRGGBB")
    rgb(color)
    levels = cfg.get("group_brightness", [0.35, 0.60])
    if (
        not isinstance(levels, list)
        or len(levels) != 2
        or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
            for v in levels
        )
    ):
        raise ValueError("group_brightness 必须为两个 0～1 的亮度值")
    cfg["group_brightness"] = levels
    alignment = cfg.get("current_alignment")
    if alignment is not None:
        if not isinstance(alignment, dict):
            raise ValueError("current_alignment 必须为对象")
        for key, lo, hi in [
            ("led_ma", 1, 254),
            ("controller_bri", 1, 255),
            ("reserve_ma", 0, 500),
            ("limit_ma", 1, 10000),
        ]:
            if type(alignment.get(key)) is not int or not lo <= alignment[key] <= hi:
                raise ValueError(f"current_alignment.{key} 无效")
        targets = alignment.get("targets_ma")
        if (
            not isinstance(targets, list)
            or len(targets) != 3
            or any(
                type(t) is not int
                or not alignment["reserve_ma"]
                < t
                < alignment["limit_ma"] - cfg["led_count"]
                for t in targets
            )
            or not targets[0] < targets[1] < targets[2]
        ):
            raise ValueError("targets_ma 必须为三个递增且低于限流预算的整数")
    return cfg


class MoodFilter:
    """每张脸复用连续确认与近期多数票；时间戳为单调时钟秒。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.current = None
        self.candidate = None
        self.candidate_at = 0
        self.samples = 0
        self.recent = []
        self.last_change = -math.inf
        self.last_sample = None
        self.invalid_at = None
        self.status = "waiting"

    def tick(self, now):
        if (
            self.last_sample is not None
            and now - self.last_sample > TIMING["sample_gap_seconds"]
        ):
            self.candidate = None
            self.samples = 0
            self.recent = []
        self.recent = [
            (t, label)
            for t, label in self.recent
            if now - t <= TIMING["vote_window_seconds"]
        ]
        lost = (
            self.invalid_at
            if self.invalid_at is not None
            else (
                now
                if self.last_sample is None
                else self.last_sample + TIMING["sample_gap_seconds"]
            )
        )
        if now - lost >= TIMING["expire_grace_seconds"]:
            self.current = self.candidate = None
            self.samples = 0
            self.recent = []
            self.status = "expired"

    def accept(self, primary, count, now):
        if self.last_sample is not None and now <= self.last_sample:
            return
        self.tick(now)
        self.last_sample = now
        reliable = (
            count == 1
            and primary
            and not primary.get("uncertain")
            and primary.get("label") in PALETTE
        )
        if count != 1:
            self.recent = []
        else:
            self.recent.append((now, primary["label"] if reliable else None))
        if not reliable:
            self.candidate = None
            self.samples = 0
            if self.invalid_at is None:
                self.invalid_at = now
            self.status = "holding" if self.current else "waiting"
            self.tick(now)
            return
        self.invalid_at = None
        if self.current and self.current["label"] == primary["label"]:
            self.current = copy.deepcopy(primary)
            self.candidate = None
            self.samples = 0
            self.status = "stable"
            return
        if self.candidate != primary["label"]:
            self.candidate = primary["label"]
            self.candidate_at = now
            self.samples = 1
        else:
            self.samples += 1
        self.status = "holding" if self.current else "confirming"
        continuous = (
            now - self.candidate_at >= TIMING["confirm_seconds"]
            and self.samples >= TIMING["confirm_samples"]
        )
        votes = sum(label == primary["label"] for _, label in self.recent)
        repeated = (
            votes >= TIMING["vote_samples"]
            and votes / len(self.recent) >= TIMING["vote_ratio"]
        )
        if (continuous or repeated) and now - self.last_change >= TIMING[
            "change_interval_seconds"
        ]:
            self.current = copy.deepcopy(primary)
            self.last_change = now
            self.candidate = None
            self.samples = 0
            self.recent = []
            self.status = "stable"


def adalight_frame(color, count):
    pixels = (
        [color] * count
        if len(color) == 3 and isinstance(color[0], (int, float))
        else color
    )
    if len(pixels) != count or not 1 <= count <= 1000:
        raise ValueError("像素数量与配置不符")
    size = count - 1
    hi, lo = size >> 8, size & 255
    return b"Ada" + bytes((hi, lo, hi ^ lo ^ 0x55)) + b"".join(bytes(p) for p in pixels)


class USBStrip:
    """Linux USB CDC；JSON 仅查询设备配置，灯色帧使用 Adalight 二进制。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.fd = None
        self.original = None

    def _write(self, data):
        import select

        end = time.monotonic() + USB_WRITE_TIMEOUT
        while data:
            if time.monotonic() >= end:
                raise TimeoutError("USB 写入超时")
            try:
                sent = os.write(self.fd, data)
                if sent == 0:
                    raise OSError("USB 已断开")
                data = data[sent:]
            except BlockingIOError:
                select.select([], [self.fd], [], 0.01)

    def connect(self):
        import array
        import fcntl
        import select
        import termios

        self.fd = os.open(self.cfg["port"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.original = termios.tcgetattr(self.fd)
            attr = termios.tcgetattr(self.fd)
            attr[0] = attr[1] = attr[3] = 0
            attr[2] = (
                (
                    attr[2]
                    & ~(
                        termios.PARENB
                        | termios.CSTOPB
                        | termios.CSIZE
                        | termios.CRTSCTS
                    )
                )
                | termios.CS8
                | termios.CREAD
                | termios.CLOCAL
            )
            attr[4] = attr[5] = termios.B115200
            attr[6][termios.VMIN] = attr[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attr)
            fcntl.ioctl(
                self.fd, termios.TIOCMBIS, array.array("i", [termios.TIOCM_DTR])
            )
            termios.tcflush(self.fd, termios.TCIFLUSH)
            self._write(b'{"v":true}\n')
            buffer = bytearray()
            end = time.monotonic() + USB_QUERY_TIMEOUT
            while time.monotonic() < end:
                ready, _, _ = select.select([self.fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    raise OSError("USB 已断开")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    try:
                        response = json.loads(line)
                        info = response["info"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    if info["leds"]["count"] != self.cfg["led_count"]:
                        raise ValueError(
                            f"灯珠数量不一致：WLED={info['leds']['count']}，配置={self.cfg['led_count']}"
                        )
                    if info.get("live") or response["state"].get("lor", 0):
                        raise ValueError("WLED 正被其他实时源控制或已禁止实时接管")
                    alignment = self.cfg.get("current_alignment")
                    if alignment and (
                        response["state"]["bri"] != alignment["controller_bri"]
                        or info["leds"]["maxpwr"] != alignment["limit_ma"]
                    ):
                        raise ValueError(
                            "WLED 总亮度或限流与电流对齐配置不一致，请核对控制器设置"
                        )
                    self._write(adalight_frame((0, 0, 0), self.cfg["led_count"]))
                    return {"version": info["ver"], "led_count": info["leds"]["count"]}
            raise TimeoutError("WLED 未回应 USB 查询；请检查固件和串口占用")
        except Exception as exc:
            self.close()
            # 板端 termios.error 不继承 OSError，转换后供重连逻辑统一处理。
            if isinstance(exc, termios.error):
                raise OSError(*exc.args) from exc
            raise

    def send(self, color):
        self._write(adalight_frame(color, self.cfg["led_count"]))

    def close(self):
        if self.fd is not None:
            import termios

            fd, original = self.fd, self.original
            self.fd = self.original = None
            try:
                if original is not None:
                    termios.tcsetattr(fd, termios.TCSANOW, original)
            except (OSError, termios.error):
                pass  # 设备已拔出时无法恢复串口设置，仍须关闭文件描述符。
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


class Lighting:
    def __init__(self, cfg, clock=time.monotonic, transport_factory=USBStrip):
        self.cfg, self.clock, self.transport_factory = cfg, clock, transport_factory
        # snapshot 会在持锁操作中再次进入；RLock 保证状态和过渡来自同一时刻。
        self.lock = threading.RLock()
        self.filter = GroupMood(MoodFilter, TIMING["expire_grace_seconds"])
        self.instance = uuid.uuid4().hex
        self.owner = None
        self.epoch = 0
        self.lease_until = 0
        self.paused = False
        self.last_seq = -1
        self.reason = "idle"
        self.color = rgb(cfg["standby_color"])
        self.power = cfg["standby_brightness"]
        self.from_color, self.target_color = self.color[:], self.color[:]
        self.pixels = [self.color[:] for _ in range(cfg["led_count"])]
        self.from_pixels = copy.deepcopy(self.pixels)
        self.target_pixels = copy.deepcopy(self.pixels)
        self.from_power = self.target_power = self.power
        self.transition_at = clock()
        self.revision = 0
        self.output_label = None
        self.current_target_ma = None
        self.device = {
            "status": "connecting" if cfg["enabled"] else "disabled",
            "message": "",
            "port": cfg["port"],
        }
        self.stop_event = threading.Event()
        self.thread = None

    def _advance(self, now):
        if self.owner and now >= self.lease_until:
            self.owner = None
            self.epoch += 1
            self.filter.reset()
            self.reason = "session_expired"
        self.filter.tick(now)
        current = self.filter.current
        target_pixels = zone_pixels(
            self.filter.people,
            {k: rgb(v) for k, v in PALETTE.items()},
            self.cfg["led_count"],
            rgb(self.cfg["standby_color"]),
        )
        self.output_label = current["label"] if current else None
        power, self.current_target_ma = self._target_power(current, target_pixels)
        self._interpolate(now)
        if target_pixels != self.target_pixels or power != self.target_power:
            self.from_color, self.from_power = self.color[:], self.power
            self.from_pixels, self.target_pixels = (
                copy.deepcopy(self.pixels),
                target_pixels,
            )
            self.target_color, self.target_power = target_pixels[0], power
            self.transition_at = now
            self.revision += 1

    def _brightness_tier(self):
        count = len(self.filter.people)
        if not self.filter.agreed or count < 2:
            return 0
        return 1 if count < 4 else 2

    def _target_power(self, current, target_pixels):
        """返回 RGB 缩放与整套估算目标；混色保留 60%，不分组分配电流。"""
        if current is None:
            return self.cfg["standby_brightness"], None
        if len(self.filter.composition) > 1:
            return self.cfg["mixed_brightness"], None
        tier = self._brightness_tier()
        alignment = self.cfg.get("current_alignment")
        if not alignment:
            power = (
                self.cfg["brightness"]
                if tier == 0
                else self.cfg["group_brightness"][tier - 1]
            )
            return power, None
        target_ma = alignment["targets_ma"][tier]
        calibration = tuple(
            alignment[key] for key in ("led_ma", "controller_bri", "reserve_ma")
        )
        power = current_scale(
            tuple(tuple(pixel) for pixel in target_pixels), target_ma, calibration
        )
        return power, target_ma

    def _interpolate(self, now):
        t = min(
            1, max(0, (now - self.transition_at) * 1000 / self.cfg["transition_ms"])
        )
        ease = t * t * (3 - 2 * t)
        self.color = [
            a + (b - a) * ease for a, b in zip(self.from_color, self.target_color)
        ]
        self.pixels = [
            [a + (b - a) * ease for a, b in zip(start, end)]
            for start, end in zip(self.from_pixels, self.target_pixels)
        ]
        self.power = self.from_power + (self.target_power - self.from_power) * ease

    def session(self, action, run_id):
        if (
            not isinstance(run_id, str)
            or not 1 <= len(run_id) <= 128
            or action not in ("start", "stop", "pause", "resume")
        ):
            raise ValueError("体验会话参数无效")
        with self.lock:
            now = self.clock()
            self._advance(now)
            if action in ("start", "resume"):
                if self.owner and self.owner != run_id:
                    return False
                if self.owner != run_id or self.paused:
                    self.epoch += 1
                    self.filter.reset()
                    self.last_seq = -1
                    self.reason = "waiting"
                self.owner, self.paused = run_id, False
                self.lease_until = now + TIMING["lease_seconds"]
            elif self.owner == run_id:
                self.epoch += 1
                self.filter.reset()
                self.paused = action == "pause"
                self.reason = "paused" if self.paused else "idle"
                if not self.paused:
                    self.owner = None
            self._advance(now)
            return True

    def ticket(self, run_id, seq):
        """票据绑定控制者、会话代次与帧号，防止停止后的迟到结果点灯。"""
        with self.lock:
            self._advance(self.clock())
            if (
                not self.owner
                or self.owner != run_id
                or self.paused
                or seq <= self.last_seq
            ):
                return None
            return (run_id, self.epoch, seq, self.clock())

    def accept(self, ticket, result):
        with self.lock:
            now = self.clock()
            self._advance(now)
            if ticket is not None:
                owner, epoch, seq, started = ticket
                if (
                    owner == self.owner
                    and epoch == self.epoch
                    and not self.paused
                    and seq > self.last_seq
                    and now - started <= TIMING["ticket_seconds"]
                ):
                    self.last_seq = seq
                    self.filter.accept(result, now)
                    self.reason = (
                        "multiple"
                        if result["count"] > 1
                        else "no_face"
                        if result["count"] == 0
                        else "uncertain"
                        if not result.get("primary")
                        or result["primary"].get("uncertain")
                        else "recognizing"
                    )
            return self.snapshot()

    def snapshot(self):
        """深拷贝隔离 HTTP 读者；时效字段含义见开发文档。"""
        with self.lock:
            now = self.clock()
            self._advance(now)
            return copy.deepcopy(
                {
                    "instance": self.instance,
                    "revision": self.revision,
                    "sample_ms": now * 1000,
                    "owner": self.owner,
                    "paused": self.paused,
                    "current": self.filter.current,
                    "status": self.filter.status,
                    "group": self.filter.snapshot(),
                    "output_label": self.output_label,
                    "pixels": [[round(c) for c in p] for p in self.pixels],
                    "mode": "emotion" if self.filter.current else "standby",
                    "reason": self.reason,
                    "rgb": [round(c) for c in self.color],
                    "power": round(self.power, 4),
                    "current_target_ma": self.current_target_ma,
                    "transition": {
                        "from": self.from_color,
                        "to": self.target_color,
                        "from_pixels": self.from_pixels,
                        "to_pixels": self.target_pixels,
                        "from_power": self.from_power,
                        "to_power": self.target_power,
                        "elapsed_ms": max(0, (now - self.transition_at) * 1000),
                        "duration_ms": self.cfg["transition_ms"],
                    },
                    "device": self.device,
                }
            )

    def start(self):
        self.thread = threading.Thread(
            target=self._worker, name="usb-light", daemon=True
        )
        self.thread.start()

    def _mark_connected(self, device_info):
        with self.lock:
            self.device.update(status="connected", message="", **device_info)
        log.info("USB WLED 已连接: %s", device_info)

    def _mark_written(self, pixels):
        # 写成功仅证明控制器通信，不代表灯带供电、接线或实际发光。
        with self.lock:
            self.device.update(
                sent_rgb=list(pixels[0]),
                sent_pixels=pixels,
                last_write_ms=self.clock() * 1000,
            )

    def _mark_disconnected(self, error):
        message = (
            "串口无权限：将运行用户加入 dialout 并重新登录"
            if isinstance(error, PermissionError)
            else str(error)
        )
        with self.lock:
            changed = self.device["message"] != message
            self.device.update(status="disconnected", message=message)
        if changed:
            log.warning("USB 灯光不可用: %s", message)

    def _worker(self):
        transport = None
        retry_at = 0
        last_sent = -math.inf
        previous = None
        try:
            while not self.stop_event.is_set():
                start = self.clock()
                state = self.snapshot()
                if self.cfg["enabled"] and start >= retry_at:
                    try:
                        if transport is None:
                            transport = self.transport_factory(self.cfg)
                            info = transport.connect()
                            self._mark_connected(info)
                            previous = None
                            state = self.snapshot()
                        color = tuple(
                            tuple(round(c * state["power"]) for c in p)
                            for p in state["pixels"]
                        )
                        if (
                            color != previous
                            or start - last_sent >= self.cfg["keepalive_ms"] / 1000
                        ):
                            transport.send(color)
                            previous, last_sent = color, start
                            self._mark_written(color)
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        ImportError,
                    ) as exc:
                        if transport:
                            transport.close()
                        transport = None
                        retry_at = self.clock() + USB_RETRY_SECONDS
                        self._mark_disconnected(exc)
                self.stop_event.wait(
                    max(0.001, 1 / self.cfg["fps"] - (self.clock() - start))
                )
        finally:
            if transport:
                transport.close()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)
