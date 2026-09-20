"""One backend mood state and an independent USB Adalight output worker."""
from __future__ import annotations

import copy
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import uuid

PALETTE = {
    'happy': '#FFD000', 'surprise': '#FF780A', 'neutral': '#00D9A6',
    'sad': '#246BFF', 'angry': '#FF2010', 'fear': '#8000FF',
    'disgust': '#55DD00', 'contempt': '#FF269C',
}
log = logging.getLogger('barometer.light')


def rgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    if type(cfg.get('enabled')) is not bool or not isinstance(cfg.get('port'), str) or not cfg['port']:
        raise ValueError('灯带 enabled 必须为布尔值，port 必须为设备路径')
    for key, lo, hi in [('led_count', 1, 1000), ('transition_ms', 100, 3000),
                        ('fps', 1, 60), ('keepalive_ms', 100, 1000)]:
        if type(cfg.get(key)) is not int or not lo <= cfg[key] <= hi:
            raise ValueError(f'灯带 {key} 必须为 {lo}～{hi} 的整数')
    for key in ('brightness', 'standby_brightness'):
        value = cfg.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f'灯带 {key} 必须在 0～1 之间')
    color = cfg.get('standby_color', '')
    if not isinstance(color, str) or len(color) != 7 or not color.startswith('#'):
        raise ValueError('standby_color 必须为 #RRGGBB')
    rgb(color)
    return cfg


class MoodFilter:
    """Brief continuity OR recent majority; timestamps are monotonic seconds."""
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
        self.status = 'waiting'

    def tick(self, now):
        if self.last_sample is not None and now - self.last_sample > 1:
            self.candidate = None
            self.samples = 0
            self.recent = []
        self.recent = [(t, label) for t, label in self.recent if now - t <= 1.2]
        lost = self.invalid_at if self.invalid_at is not None else (
            now if self.last_sample is None else self.last_sample + 1)
        if now - lost >= .9:
            self.current = self.candidate = None
            self.samples = 0
            self.recent = []
            self.status = 'expired'

    def accept(self, primary, count, now):
        if self.last_sample is not None and now <= self.last_sample:
            return
        self.tick(now)
        self.last_sample = now
        reliable = count == 1 and primary and not primary.get('uncertain') and primary.get('label') in PALETTE
        if count != 1:
            self.recent = []
        else:
            self.recent.append((now, primary['label'] if reliable else None))
        if not reliable:
            self.candidate = None
            self.samples = 0
            if self.invalid_at is None:
                self.invalid_at = now
            self.status = 'holding' if self.current else 'waiting'
            self.tick(now)
            return
        self.invalid_at = None
        if self.current and self.current['label'] == primary['label']:
            self.current = copy.deepcopy(primary)
            self.candidate = None
            self.samples = 0
            self.status = 'stable'
            return
        if self.candidate != primary['label']:
            self.candidate = primary['label']
            self.candidate_at = now
            self.samples = 1
        else:
            self.samples += 1
        self.status = 'holding' if self.current else 'confirming'
        continuous = now - self.candidate_at >= .3 and self.samples >= 2
        votes = sum(label == primary['label'] for _, label in self.recent)
        repeated = votes >= 3 and votes / len(self.recent) >= .75
        if (continuous or repeated) and now - self.last_change >= .6:
            self.current = copy.deepcopy(primary)
            self.last_change = now
            self.candidate = None
            self.samples = 0
            self.recent = []
            self.status = 'stable'


def adalight_frame(color, count):
    size = count - 1
    hi, lo = size >> 8, size & 255
    return b'Ada' + bytes((hi, lo, hi ^ lo ^ 0x55)) + bytes(color) * count


class USBStrip:
    """Linux USB CDC. JSON is only used for read-only device identification."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.fd = None
        self.original = None

    def _write(self, data):
        import select
        end = time.monotonic() + .2
        while data:
            if time.monotonic() >= end:
                raise TimeoutError('USB 写入超时')
            try:
                sent = os.write(self.fd, data)
                if sent == 0:
                    raise OSError('USB 已断开')
                data = data[sent:]
            except BlockingIOError:
                select.select([], [self.fd], [], .01)

    def connect(self):
        import array
        import fcntl
        import select
        import termios
        self.fd = os.open(self.cfg['port'], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.original = termios.tcgetattr(self.fd)
            attr = termios.tcgetattr(self.fd)
            attr[0] = attr[1] = attr[3] = 0
            attr[2] = (attr[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.CRTSCTS)) | termios.CS8 | termios.CREAD | termios.CLOCAL
            attr[4] = attr[5] = termios.B115200
            attr[6][termios.VMIN] = attr[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attr)
            fcntl.ioctl(self.fd, termios.TIOCMBIS, array.array('i', [termios.TIOCM_DTR]))
            termios.tcflush(self.fd, termios.TCIFLUSH)
            self._write(b'{"v":true}\n')
            buffer = bytearray()
            end = time.monotonic() + 2
            while time.monotonic() < end:
                ready, _, _ = select.select([self.fd], [], [], .1)
                if not ready:
                    continue
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    raise OSError('USB 已断开')
                buffer.extend(chunk)
                while b'\n' in buffer:
                    line, _, buffer = buffer.partition(b'\n')
                    try:
                        info = json.loads(line)['info']
                    except (ValueError, KeyError, TypeError):
                        continue
                    if info['leds']['count'] != self.cfg['led_count']:
                        raise ValueError(f"灯珠数量不一致：WLED={info['leds']['count']}，配置={self.cfg['led_count']}")
                    if info.get('live') or json.loads(line)['state'].get('lor', 0):
                        raise ValueError('WLED 正被其他实时源控制或已禁止实时接管')
                    self._write(adalight_frame((0, 0, 0), self.cfg['led_count']))
                    return {'version': info['ver'], 'led_count': info['leds']['count']}
            raise TimeoutError('WLED 未回应 USB 查询；请检查固件和串口占用')
        except Exception as exc:
            self.close()
            # termios.error is not an OSError on the board's Python.
            if isinstance(exc, termios.error):
                raise OSError(*exc.args) from exc
            raise

    def send(self, color):
        self._write(adalight_frame(color, self.cfg['led_count']))

    def close(self):
        if self.fd is not None:
            import termios
            fd, original = self.fd, self.original
            self.fd = self.original = None
            try:
                if original is not None:
                    termios.tcsetattr(fd, termios.TCSANOW, original)
            except (OSError, termios.error):
                pass  # Unplugged devices cannot restore terminal settings.
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


class Lighting:
    def __init__(self, cfg, clock=time.monotonic, transport_factory=USBStrip):
        self.cfg, self.clock, self.transport_factory = cfg, clock, transport_factory
        self.lock = threading.RLock()
        self.filter = MoodFilter()
        self.instance = uuid.uuid4().hex
        self.owner = None
        self.epoch = 0
        self.lease_until = 0
        self.paused = False
        self.last_seq = -1
        self.reason = 'idle'
        self.color = rgb(cfg['standby_color'])
        self.power = cfg['standby_brightness']
        self.from_color, self.target_color = self.color[:], self.color[:]
        self.from_power = self.target_power = self.power
        self.transition_at = clock()
        self.revision = 0
        self.device = {'status': 'connecting' if cfg['enabled'] else 'disabled', 'message': '', 'port': cfg['port']}
        self.stop_event = threading.Event()
        self.thread = None

    def _advance(self, now):
        if self.owner and now >= self.lease_until:
            self.owner = None
            self.epoch += 1
            self.filter.reset()
            self.reason = 'session_expired'
        self.filter.tick(now)
        p = self.filter.current
        target = rgb(PALETTE[p['label']]) if p else rgb(self.cfg['standby_color'])
        power = self.cfg['brightness'] if p else self.cfg['standby_brightness']
        self._interpolate(now)
        if target != self.target_color or power != self.target_power:
            self.from_color, self.from_power = self.color[:], self.power
            self.target_color, self.target_power = target, power
            self.transition_at = now
            self.revision += 1

    def _interpolate(self, now):
        t = min(1, max(0, (now - self.transition_at) * 1000 / self.cfg['transition_ms']))
        ease = t * t * (3 - 2 * t)
        self.color = [a + (b - a) * ease for a, b in zip(self.from_color, self.target_color)]
        self.power = self.from_power + (self.target_power - self.from_power) * ease

    def session(self, action, run_id):
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128 or action not in ('start', 'stop', 'pause', 'resume'):
            raise ValueError('体验会话参数无效')
        with self.lock:
            now = self.clock()
            self._advance(now)
            if action in ('start', 'resume'):
                if self.owner and self.owner != run_id:
                    return False
                if self.owner != run_id or self.paused:
                    self.epoch += 1
                    self.filter.reset()
                    self.last_seq = -1
                    self.reason = 'waiting'
                self.owner, self.paused = run_id, False
                self.lease_until = now + 8
            elif self.owner == run_id:
                self.epoch += 1
                self.filter.reset()
                self.paused = action == 'pause'
                self.reason = 'paused' if self.paused else 'idle'
                if not self.paused:
                    self.owner = None
            self._advance(now)
            return True

    def ticket(self, run_id, seq):
        with self.lock:
            self._advance(self.clock())
            if not self.owner or self.owner != run_id or self.paused or seq <= self.last_seq:
                return None
            return (run_id, self.epoch, seq, self.clock())

    def accept(self, ticket, result):
        with self.lock:
            now = self.clock()
            self._advance(now)
            if ticket is not None:
                owner, epoch, seq, started = ticket
                if owner == self.owner and epoch == self.epoch and not self.paused and seq > self.last_seq and now - started <= 1.5:
                    self.last_seq = seq
                    self.filter.accept(result.get('primary'), result['count'], now)
                    self.reason = 'multiple' if result['count'] > 1 else 'no_face' if result['count'] == 0 else 'uncertain' if not result.get('primary') or result['primary'].get('uncertain') else 'recognizing'
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            now = self.clock()
            self._advance(now)
            return copy.deepcopy({
                'instance': self.instance, 'revision': self.revision,
                'sample_ms': now * 1000,
                'owner': self.owner, 'paused': self.paused,
                'current': self.filter.current, 'status': self.filter.status,
                'mode': 'emotion' if self.filter.current else 'standby', 'reason': self.reason,
                'rgb': [round(c) for c in self.color], 'power': round(self.power, 4),
                'transition': {'from': self.from_color, 'to': self.target_color,
                    'from_power': self.from_power, 'to_power': self.target_power,
                    'elapsed_ms': max(0, (now - self.transition_at) * 1000), 'duration_ms': self.cfg['transition_ms']},
                'device': self.device,
            })

    def start(self):
        self.thread = threading.Thread(target=self._worker, name='usb-light', daemon=True)
        self.thread.start()

    def _worker(self):
        transport = None
        retry_at = 0
        last_sent = -math.inf
        previous = None
        try:
            while not self.stop_event.is_set():
                start = self.clock()
                state = self.snapshot()
                if self.cfg['enabled'] and start >= retry_at:
                    try:
                        if transport is None:
                            transport = self.transport_factory(self.cfg)
                            info = transport.connect()
                            with self.lock:
                                self.device.update(status='connected', message='', **info)
                            log.info('USB WLED connected: %s', info)
                            previous = None
                            state = self.snapshot()
                        color = tuple(round(c * state['power']) for c in state['rgb'])
                        if color != previous or start - last_sent >= self.cfg['keepalive_ms'] / 1000:
                            transport.send(color)
                            previous, last_sent = color, start
                            with self.lock:
                                self.device.update(sent_rgb=list(color), last_write_ms=self.clock() * 1000)
                    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
                        if transport:
                            transport.close()
                        transport = None
                        retry_at = self.clock() + 2
                        message = ('串口无权限：将运行用户加入 dialout 并重新登录' if isinstance(exc, PermissionError) else str(exc))
                        with self.lock:
                            changed = self.device['message'] != message
                            self.device.update(status='disconnected', message=message)
                        if changed:
                            log.warning('USB light unavailable: %s', message)
                self.stop_event.wait(max(.001, 1 / self.cfg['fps'] - (self.clock() - start)))
        finally:
            if transport:
                transport.close()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)
