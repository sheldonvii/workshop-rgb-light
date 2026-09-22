#!/usr/bin/env python3
"""情绪晴雨表 推理与页面服务（单板一体化，仅依赖本目录 .venv）。

数据链路：浏览器抓帧(JPEG) → POST /api/infer → 人脸检测(yolov5n-face)
→ 原图坐标裁剪 → 表情分类(HSEmotion B0/B2，或原版 ResNet50) → JSON。
检测使用 SpacemiT Vision 0.1.2 的 SpaceMITExecutionProvider。
HSEmotion 使用板上已有 ONNX Runtime 的 CPUExecutionProvider，来源与校验值见 scripts/download_models.py。

接口契约：
  GET  /api/health  → {ok, state: loading|ready|error, models, engine, message?}
  POST /api/infer   → 请求: 请求体 JPEG 帧；头 X-Run-Id / X-Seq（回显，页面用于丢弃过期结果）
                      响应: {ok:true, run_id, seq, frame:{w,h}, count, faces[], primary|null, timing{...}, server_ts}
                      错误: 409 busy / 503 not_ready / 400 bad_image / 500 infer_failed，{ok:false, error:{code,message}}
  GET  /            → web/ 静态页面

并发：同一时刻最多一个完整推理在途；在途时新请求立即 409（页面端丢帧）。
"""
from __future__ import annotations

import argparse
import os
import io
import json
import logging
import signal
import threading
import time
import traceback
from emotion_models import EmotionModel, REGISTRY, LABELS
from lighting import Lighting, load_config
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
FACE_CFG = ROOT / "configs" / "yolov5-face.yaml"
EMO_CFG = ROOT / "configs" / "emotion.yaml"

ENGINE_VERSION = "spacemit_vision 0.1.2 (SpaceMITExecutionProvider)"
MODEL_INFO = {
    "face": {
        "name": "yolov5n-face_cut.q.onnx",
        "source": "https://archive.spacemit.com/spacemit-ai/model_zoo/vision/yolov5-face/yolov5n-face_cut.q.onnx",
        "md5": "095598f3a279b5f004cbe0b44fab660b",
        "input": "[1,3,640,640]",
    },
    "emotion": {
        "name": "emotion_resnet50_final.q.onnx",
        "source": "https://archive.spacemit.com/spacemit-ai/model_zoo/vision/resnet/emotion_resnet50_final.q.onnx",
        "md5": "982cb7e6d38842750887560a8d7f363a",
        "input": "[1,3,224,224]",
    },
}

# label → (中文文案, 氛围键)。标签顺序与 configs/emotion.txt 一致（官方标签文件）。
EMOTIONS = {
    "neutral":  {"zh": "平静", "mood": "neutral"},
    "happy":    {"zh": "开心", "mood": "happy"},
    "sad":      {"zh": "难过", "mood": "sad"},
    "angry":    {"zh": "生气", "mood": "angry"},
    "fear":     {"zh": "害怕", "mood": "fear"},
    "disgust":  {"zh": "厌恶", "mood": "disgust"},
    "surprise": {"zh": "惊讶", "mood": "surprise"},
}
EMOTION_LABELS = list(EMOTIONS.keys())  # neutral happy sad angry fear disgust surprise

MAX_UPLOAD = 8 * 1024 * 1024
log = logging.getLogger("barometer")


class ModelBundle:
    """Detector and selected emotion classifier. state: loading -> ready | error."""

    def __init__(self, emotion_key='hse-b0') -> None:
        self.state = "loading"
        self.message = ""
        self.load_ms: dict[str, float] = {}
        self._det = None
        self._emo = None
        self._infer_lock = threading.Lock()
        self._loaded_lock = threading.Event()
        self.emotion_key = emotion_key
        self.emotion_info = {}

    def load_async(self) -> None:
        threading.Thread(target=self._load, name="model-load", daemon=True).start()

    def _load(self) -> None:
        try:
            import cv2  # noqa: F401  先于引擎导入（libvision 依赖其库解析环境）
            from spacemit_vision import VisionServiceNative

            for key, cfg in (("face", FACE_CFG), ("emotion", EMO_CFG)):
                t0 = time.perf_counter()
                svc = (EmotionModel(self.emotion_key) if key == 'emotion' and self.emotion_key != 'official'
                       else VisionServiceNative.create(str(cfg), timing_print_to_stdout=False))
                self.load_ms[key] = round((time.perf_counter() - t0) * 1000, 1)
                if key == "face":
                    self._det = svc
                else:
                    self._emo = svc
                log.info("model %s loaded in %.0f ms", key, self.load_ms[key])
            # 各跑一次 warmup（首张 NPU 推理有编译开销）
            import numpy as np

            dummy = np.zeros((64, 64, 3), dtype=np.uint8)
            self._det.infer_image(dummy)
            if self.emotion_key == 'official':
                self._emo.infer_image(dummy)
            else:
                self._emo.predict(dummy)
            self.emotion_info = self._emotion_info()
            self.state = "ready"
        except Exception as exc:  # 模型加载失败：health 必须如实上报
            self.state = "error"
            self.message = f"{type(exc).__name__}: {exc}"
            log.error("model load failed:\n%s", traceback.format_exc())
        finally:
            self._loaded_lock.set()

    def health(self) -> dict:
        return {
            "ok": self.state == "ready",
            "state": self.state,
            "message": self.message,
            "engine": ENGINE_VERSION if self.emotion_key == 'official' else f'{ENGINE_VERSION} face; ONNX Runtime CPUExecutionProvider emotion',
            "models": {**MODEL_INFO, 'emotion': self.emotion_info},
            "load_ms": self.load_ms,
            "labels": EMOTION_LABELS if self.emotion_key == 'official' else LABELS,
            "emotion_key": self.emotion_key,
            "available_models": ['hse-b0', 'hse-b2', 'official'],
        }

    def _emotion_info(self):
        if self.emotion_key == 'official':
            return {**MODEL_INFO['emotion'], 'key': 'official', 'score_type': 'logit', 'labels': EMOTION_LABELS}
        return self._emo.info

    def switch(self, key):
        if key not in (*REGISTRY, 'official'):
            return 400, {'ok': False, 'error': {'code': 'bad_model', 'message': '未知模型'}}
        if self.state != 'ready' or not self._infer_lock.acquire(timeout=6):
            return 409, {'ok': False, 'error': {'code': 'busy', 'message': '请停止体验后再切换模型'}}
        try:
            if key != self.emotion_key:
                import cv2
                import numpy as np
                if key == 'official':
                    from spacemit_vision import VisionServiceNative
                    candidate = VisionServiceNative.create(str(EMO_CFG), timing_print_to_stdout=False)
                    candidate.infer_image(np.zeros((64,64,3), dtype=np.uint8))
                else:
                    candidate = EmotionModel(key)
                    candidate.predict(np.zeros((64,64,3), dtype=np.uint8))
                old = self._emo
                self._emo, self.emotion_key = candidate, key
                self.emotion_info = self._emotion_info()
                old.release()
            return 200, self.health()
        except Exception as exc:
            log.exception('model switch failed')
            return 500, {'ok': False, 'error': {'code': 'model_failed', 'message': str(exc)}}
        finally:
            self._infer_lock.release()

    def infer(self, jpeg: bytes) -> dict:
        """完整串联。调用方需已持有 _infer_lock。"""
        import cv2
        import numpy as np
        from spacemit_vision import VisionServiceStatus

        t0 = time.perf_counter()
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("bad_image")
        t_decode = time.perf_counter()

        status, dets = self._det.infer_image(img)
        if status != VisionServiceStatus.OK:
            raise RuntimeError(f"face detect failed: {status}")
        t_detect = time.perf_counter()

        h, w = img.shape[:2]
        faces = []
        for d in dets:
            faces.append({
                "x1": round(float(d.x1), 1), "y1": round(float(d.y1), 1),
                "x2": round(float(d.x2), 1), "y2": round(float(d.y2), 1),
                "score": round(float(d.score), 4),
            })

        for d, face in zip(dets, faces):
            primary = None
            # Classify each original-coordinate crop; retain the single-face API.
            x1, y1 = max(0, int(d.x1)), max(0, int(d.y1))
            x2, y2 = min(w, int(d.x2)), min(h, int(d.y2))
            if x2 - x1 >= 8 and y2 - y1 >= 8:
                crop = img[y1:y2, x1:x2]
                if self.emotion_key != 'official':
                    primary = self._emo.predict(crop)
                    primary['box'] = [round(float(d.x1),1), round(float(d.y1),1), round(float(d.x2),1), round(float(d.y2),1)]
                    res = None
                    status2 = VisionServiceStatus.OK
                else:
                    status2, res = self._emo.infer_image(crop)
                if status2 != VisionServiceStatus.OK:
                    raise RuntimeError(f"emotion classify failed: {status2}")
                if res:
                    r = res[0]
                    idx = int(r.label)
                    label = EMOTION_LABELS[idx] if 0 <= idx < len(EMOTION_LABELS) else str(idx)
                    primary = {
                        "label": label,
                        "label_zh": EMOTIONS.get(label, {}).get("zh", label),
                        "mood": EMOTIONS.get(label, {}).get("mood", "neutral"),
                        "score": round(float(r.score), 4),  # 模型原始分数（logit，非概率，可>1）
                        "score_type": "logit", "model": "official", "uncertain": False,
                        "box": [round(float(d.x1), 1), round(float(d.y1), 1),
                                round(float(d.x2), 1), round(float(d.y2), 1)],
                    }
            face['emotion'] = primary
        primary = faces[0]['emotion'] if len(faces) == 1 else None
        t_emo = time.perf_counter()

        return {
            "frame": {"w": w, "h": h},
            "count": len(faces),
            "faces": faces,
            "primary": primary,
            "timing": {
                "decode_ms": round((t_decode - t0) * 1000, 1),
                "detect_ms": round((t_detect - t_decode) * 1000, 1),
                "emotion_ms": round((t_emo - t_detect) * 1000, 1),
                "total_ms": round((t_emo - t0) * 1000, 1),
            },
        }

    def infer_locked(self, jpeg: bytes) -> tuple[int, dict]:
        """带在途锁的入口。返回 (http_status, payload)。"""
        if self.state != "ready":
            return 503, {"ok": False, "error": {"code": "not_ready",
                         "message": f"模型未就绪: {self.state} {self.message}".strip()}}
        if not self._infer_lock.acquire(blocking=False):
            return 409, {"ok": False, "error": {"code": "busy", "message": "上一帧仍在推理，本帧丢弃"}}
        try:
            data = self.infer(jpeg)
            data["ok"] = True
            return 200, data
        except ValueError:
            return 400, {"ok": False, "error": {"code": "bad_image", "message": "无法解码的图像数据"}}
        except Exception as exc:
            log.error("infer failed:\n%s", traceback.format_exc())
            return 500, {"ok": False, "error": {"code": "infer_failed", "message": str(exc)}}
        finally:
            self._infer_lock.release()

    def release(self) -> None:
        # Native sessions must outlive any request currently executing them.
        with self._infer_lock:
            for svc in (self._det, self._emo):
                try:
                    if svc is not None:
                        svc.release()
                except Exception:
                    log.exception('model release failed')


MODELS = ModelBundle()
LIGHT = None

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "EmotionBarometer/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 降噪音，只留 4xx/5xx 由 access 自行判断
        pass

    # ---------- helpers ----------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The browser may abort a frame when stopping the camera.

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "资源不存在"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routes ----------
    def do_GET(self) -> None:  # noqa: N802
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html")
            return
        if path_only == "/api/health":
            self._send_json(200 if MODELS.state == "ready" else 503, MODELS.health())
            return
        if path_only == '/api/light':
            self._send_json(200, {'ok': True, 'light': LIGHT.snapshot()})
            return
        # 静态资源：只允许 web/ 目录内
        rel = path_only.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if target.is_file() and target.is_relative_to(WEB_DIR.resolve()):
            self._send_file(target)
            return
        self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "资源不存在"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == '/api/light/layout':
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 512:
                    raise ValueError('灯光模式参数过长或为空')
                data = json.loads(self.rfile.read(n))
                accepted = LIGHT.layout(data['mode'], data.get('run_id'))
                self._send_json(200 if accepted else 409, {'ok': accepted, 'light': LIGHT.snapshot(),
                    **({} if accepted else {'error': {'code': 'light_busy', 'message': '另一页面正在体验，请在该页面切换灯光模式'}})})
            except (ValueError, KeyError, TypeError) as exc:
                self._send_json(400, {'ok': False, 'error': {'code': 'bad_request', 'message': str(exc)}})
            return
        if self.path == '/api/light/session':
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 512:
                    raise ValueError('会话参数过长或为空')
                data = json.loads(self.rfile.read(n))
                accepted = LIGHT.session(data['action'], data['run_id'])
                self._send_json(200 if accepted else 409, {'ok': accepted, 'light': LIGHT.snapshot(),
                    **({} if accepted else {'error': {'code': 'light_busy', 'message': '另一页面正在体验，请先在该页面停止'}})})
            except (ValueError, KeyError, TypeError) as exc:
                self._send_json(400, {'ok': False, 'error': {'code': 'bad_request', 'message': str(exc)}})
            return
        if self.path == '/api/model':
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 256:
                    raise ValueError('bad request')
                key = json.loads(self.rfile.read(n))['model']
                if LIGHT.snapshot()['owner']:
                    self._send_json(409, {'ok': False, 'error': {'code': 'light_busy', 'message': '请先停止正在进行的体验'}})
                    return
                status, payload = MODELS.switch(key)
                self._send_json(status, payload)
            except (ValueError, KeyError, TypeError):
                self._send_json(400, {'ok': False, 'error': {'code': 'bad_request', 'message': '模型参数无效'}})
            return
        if self.path != "/api/infer":
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._send_json(400, {"ok": False, "error": {"code": "bad_image", "message": "请求体为空或超过 8MB"}})
            return
        run_id = self.headers.get('X-Run-Id', '')
        try:
            seq = int(self.headers.get('X-Seq', '0'))
        except ValueError:
            seq = 0
        ticket = LIGHT.ticket(run_id, seq)
        jpeg = self.rfile.read(length)
        status, payload = MODELS.infer_locked(jpeg)
        payload['mirror'] = self.headers.get('X-Mirror', 'true') != 'false'
        payload['light'] = LIGHT.accept(ticket, payload) if status == 200 else LIGHT.snapshot()
        # 运行标识与帧序号原样回显，页面端据此丢弃过期/跨运行结果
        payload["run_id"] = self.headers.get("X-Run-Id", "")
        try:
            payload["seq"] = int(self.headers.get("X-Seq", "0"))
        except ValueError:
            payload["seq"] = 0
        payload["server_ts"] = round(time.time(), 3)
        self._send_json(status, payload)


def main() -> None:
    global LIGHT
    os.chdir(ROOT)  # Vision YAML paths are relative to the project root.
    ap = argparse.ArgumentParser(description="情绪晴雨表 Demo 服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，本机使用）")
    ap.add_argument("--port", type=int, default=8321, help="监听端口（默认 8321）")
    ap.add_argument('--emotion-model', choices=['hse-b0', 'hse-b2', 'official'], default='hse-b0')
    ap.add_argument('--light-config', default=str(ROOT / 'configs/light.json'))
    args = ap.parse_args()
    MODELS.emotion_key = args.emotion_model

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    LIGHT = Lighting(load_config(args.light_config))
    LIGHT.start()
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    MODELS.load_async()
    log.info("serving on http://%s:%d  (web dir: %s)", args.host, args.port, WEB_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LIGHT.close()
        httpd.server_close()
        MODELS.release()
        log.info("stopped, models released")


if __name__ == "__main__":
    main()
