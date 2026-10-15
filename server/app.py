#!/usr/bin/env python3
"""情绪晴雨表 推理与页面服务（单板一体化，仅依赖本目录 .venv）。

数据链路：浏览器抓帧(JPEG) → POST /api/infer → 人脸检测(yolov5n-face)
→ 原图坐标裁剪 → 表情分类(HSEmotion B0/B2，或原版 ResNet50) → JSON。
检测使用 SpacemiT Vision 0.1.4 的 SpaceMITExecutionProvider。
HSEmotion 使用板上已有 ONNX Runtime 的 CPUExecutionProvider，来源与校验值见 server/emotion_models.py。

接口契约：
  GET  /api/health  → {ok, state: loading|ready|error, models, engine, message?}
  POST /api/infer   → 请求: 请求体 JPEG 帧；头 X-Run-Id / X-Seq（回显，页面用于丢弃过期结果）
                      响应: {ok:true, run_id, seq, frame:{w,h}, count, faces[], primary|null, timing{...}, server_ts}
                      错误: 409 busy / 503 not_ready / 400 bad_image/bad_sequence / 500 infer_failed，{ok:false, error:{code,message}}
  GET  /            → web/ 静态页面

并发：同一时刻最多一个完整推理在途；在途时新请求立即 409（页面端丢帧）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from emotion_models import REGISTRY, ZH, load_classifier, model_file_info, vision_infer
from lighting import PALETTE, TIMING, Lighting, load_config

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
FACE_CFG = ROOT / "configs" / "yolov5-face.yaml"
ENGINE_VERSION = "spacemit_vision 0.1.4 (SpaceMITExecutionProvider)"
FACE_INFO = {**model_file_info("yolov5n-face_cut.q.onnx"), "input": "[1,3,640,640]"}
MIN_FACE_SIZE = 8  # 原图像素，跳过过小裁剪。
MODEL_SWITCH_TIMEOUT = 6  # 秒，等待在途推理完成。

MAX_UPLOAD = 8 * 1024 * 1024
log = logging.getLogger("barometer")


class BadImageError(ValueError):
    """请求体解码失败，与分类器内部 ValueError 区分。"""


def decode_jpeg(jpeg):
    import cv2
    import numpy as np

    try:
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise BadImageError("无法解码的图像数据") from exc
    if image is None:
        raise BadImageError("无法解码的图像数据")
    return image


class ModelBundle:
    """管理检测器和统一分类器；状态为 loading、ready 或 error。"""

    def __init__(self, emotion_key="hse-b0") -> None:
        self.state = "loading"
        self.message = ""
        self.load_ms: dict[str, float] = {}
        self._det = None
        self._emo = None
        self._infer_lock = threading.Lock()
        self.emotion_key = emotion_key

    def load_async(self) -> None:
        threading.Thread(target=self._load, name="model-load", daemon=True).start()

    def _load(self):
        try:
            import cv2  # noqa: F401  先导入 OpenCV，满足 Vision 的动态库依赖。
            import numpy as np
            from spacemit_vision import VisionServiceNative

            started = time.perf_counter()
            self._det = VisionServiceNative.create(
                str(FACE_CFG), timing_print_to_stdout=False
            )
            self.load_ms["face"] = round((time.perf_counter() - started) * 1000, 1)
            # SDK 以状态返回失败，预热失败不能继续标记 ready。
            vision_infer(
                self._det, np.zeros((64, 64, 3), dtype=np.uint8), "人脸检测预热"
            )
            started = time.perf_counter()
            self._emo = load_classifier(self.emotion_key)
            self.load_ms["emotion"] = round((time.perf_counter() - started) * 1000, 1)
            self.state = "ready"
            log.info("模型加载及预热完成: %s", self.load_ms)
        except Exception as exc:
            self.state = "error"
            self.message = f"{type(exc).__name__}: {exc}"
            log.exception("模型加载失败")
            self.release()

    def health(self):
        model = self._emo.info if self._emo else {}
        return {
            "ok": self.state == "ready",
            "state": self.state,
            "message": self.message,
            "engine": f"{ENGINE_VERSION} face; {' / '.join(model.get('providers', []))} emotion",
            "models": {"face": FACE_INFO, "emotion": model},
            "load_ms": self.load_ms,
            "labels": model.get("labels", []),
            "emotion_key": self.emotion_key,
            "available_models": list(REGISTRY),
            "model_options": [
                {"key": key, "name": cfg["name"]} for key, cfg in REGISTRY.items()
            ],
            "labels_zh": ZH,
            "palette": PALETTE,
            "standby": {
                "color": LIGHT.cfg["standby_color"],
                "brightness": LIGHT.cfg["standby_brightness"],
            },
            "timing": {**TIMING, "transition_ms": LIGHT.cfg["transition_ms"]},
        }

    def switch(self, key):
        if key not in REGISTRY:
            return 400, {
                "ok": False,
                "error": {"code": "bad_model", "message": "未知模型"},
            }
        if self.state != "ready" or not self._infer_lock.acquire(
            timeout=MODEL_SWITCH_TIMEOUT
        ):
            return 409, {
                "ok": False,
                "error": {"code": "busy", "message": "请停止体验后再切换模型"},
            }
        try:
            if key != self.emotion_key:
                candidate = load_classifier(key)
                old = self._emo
                self._emo, self.emotion_key = candidate, key
                old.release()
            return 200, self.health()
        except Exception as exc:
            log.exception("模型切换失败")
            return 500, {
                "ok": False,
                "error": {"code": "model_failed", "message": str(exc)},
            }
        finally:
            self._infer_lock.release()

    def _classify_face(self, image, detection):
        h, w = image.shape[:2]
        # 检测框已还原到原图；各脸单独裁剪，单人和多人使用相同分类链路。
        x1, y1 = max(0, int(detection.x1)), max(0, int(detection.y1))
        x2, y2 = min(w, int(detection.x2)), min(h, int(detection.y2))
        if x2 - x1 < MIN_FACE_SIZE or y2 - y1 < MIN_FACE_SIZE:
            return None
        emotion = self._emo.predict(image[y1:y2, x1:x2])
        if emotion is not None:
            emotion["box"] = [
                round(float(getattr(detection, key)), 1)
                for key in ("x1", "y1", "x2", "y2")
            ]
        return emotion

    def infer(self, jpeg: bytes) -> dict:
        """完整串联。调用方需已持有 _infer_lock。"""
        t0 = time.perf_counter()
        img = decode_jpeg(jpeg)
        t_decode = time.perf_counter()

        dets = vision_infer(self._det, img, "人脸检测")
        t_detect = time.perf_counter()

        h, w = img.shape[:2]
        faces = []
        for d in dets:
            faces.append(
                {
                    "x1": round(float(d.x1), 1),
                    "y1": round(float(d.y1), 1),
                    "x2": round(float(d.x2), 1),
                    "y2": round(float(d.y2), 1),
                    "score": round(float(d.score), 4),
                }
            )

        for detection, face in zip(dets, faces):
            face["emotion"] = self._classify_face(img, detection)
        primary = faces[0]["emotion"] if len(faces) == 1 else None
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
            return 503, {
                "ok": False,
                "error": {
                    "code": "not_ready",
                    "message": f"模型未就绪: {self.state} {self.message}".strip(),
                },
            }
        if not self._infer_lock.acquire(blocking=False):
            return 409, {
                "ok": False,
                "error": {"code": "busy", "message": "上一帧仍在推理，本帧丢弃"},
            }
        try:
            data = self.infer(jpeg)
            data["ok"] = True
            return 200, data
        except BadImageError:
            return 400, {
                "ok": False,
                "error": {"code": "bad_image", "message": "无法解码的图像数据"},
            }
        except Exception as exc:
            log.exception("推理失败")
            return 500, {
                "ok": False,
                "error": {"code": "infer_failed", "message": str(exc)},
            }
        finally:
            self._infer_lock.release()

    def release(self) -> None:
        # 等待在途推理离开后才释放原生会话，避免底层访问已释放资源。
        with self._infer_lock:
            for svc in (self._det, self._emo):
                try:
                    if svc is not None:
                        svc.release()
                except Exception:
                    log.exception("模型释放失败")


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

    def log_message(
        self, fmt, *args
    ):  # 请求日志默认静默；模型与 USB 故障由各自日志记录。
        pass

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
            pass  # 停止相机时浏览器可主动取消请求。

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(
                404,
                {"ok": False, "error": {"code": "not_found", "message": "资源不存在"}},
            )
            return
        self.send_response(200)
        self.send_header(
            "Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html")
            return
        if path_only == "/api/health":
            self._send_json(200 if MODELS.state == "ready" else 503, MODELS.health())
            return
        if path_only == "/api/light":
            self._send_json(200, {"ok": True, "light": LIGHT.snapshot()})
            return
        # 静态资源：只允许 web/ 目录内
        rel = path_only.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if target.is_file() and target.is_relative_to(WEB_DIR.resolve()):
            self._send_file(target)
            return
        self._send_json(
            404, {"ok": False, "error": {"code": "not_found", "message": "资源不存在"}}
        )

    def do_POST(self) -> None:  # noqa: N802
        routes = {
            "/api/light/session": self._post_session,
            "/api/model": self._post_model,
            "/api/infer": self._post_infer,
        }
        route = routes.get(self.path)
        if route is None:
            self._send_json(
                404,
                {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}},
            )
            return
        route()

    def _post_session(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 512:
                raise ValueError("会话参数过长或为空")
            data = json.loads(self.rfile.read(n))
            accepted = LIGHT.session(data["action"], data["run_id"])
            self._send_json(
                200 if accepted else 409,
                {
                    "ok": accepted,
                    "light": LIGHT.snapshot(),
                    **(
                        {}
                        if accepted
                        else {
                            "error": {
                                "code": "light_busy",
                                "message": "另一页面正在体验，请先在该页面停止",
                            }
                        }
                    ),
                },
            )
        except (ValueError, KeyError, TypeError) as exc:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "bad_request", "message": str(exc)},
                },
            )

    def _post_model(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 256:
                raise ValueError("bad request")
            key = json.loads(self.rfile.read(n))["model"]
            if LIGHT.snapshot()["owner"]:
                self._send_json(
                    409,
                    {
                        "ok": False,
                        "error": {
                            "code": "light_busy",
                            "message": "请先停止正在进行的体验",
                        },
                    },
                )
                return
            status, payload = MODELS.switch(key)
            self._send_json(status, payload)
        except (ValueError, KeyError, TypeError):
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "bad_request", "message": "模型参数无效"},
                },
            )

    def _post_infer(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": {"code": "bad_image", "message": "请求体为空或超过 8MB"},
                },
            )
            return
        run_id = self.headers.get("X-Run-Id", "")
        raw_seq = self.headers.get("X-Seq")
        seq = None
        if raw_seq is not None:
            try:
                seq = int(raw_seq)
                if seq < 0:
                    raise ValueError("negative sequence")
            except ValueError:
                # 必须消费当前请求体，避免 HTTP 长连接将 JPEG 当成下一条请求。
                self.rfile.read(length)
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": {
                            "code": "bad_sequence",
                            "message": "X-Seq 必须为非负整数",
                        },
                    },
                )
                return
        ticket = LIGHT.ticket(run_id, seq) if seq is not None else None
        jpeg = self.rfile.read(length)
        status, payload = MODELS.infer_locked(jpeg)
        payload["mirror"] = self.headers.get("X-Mirror", "true") != "false"
        payload["light"] = (
            LIGHT.accept(ticket, payload) if status == 200 else LIGHT.snapshot()
        )
        # 运行标识和解析后的序号回显，缺失序号为 null，不建立灯光票据。
        payload["run_id"], payload["seq"] = run_id, seq
        payload["server_ts"] = round(time.time(), 3)
        self._send_json(status, payload)


def main() -> None:
    global LIGHT
    os.chdir(ROOT)  # Vision YAML 中路径相对项目根目录。
    ap = argparse.ArgumentParser(description="情绪晴雨表 Demo 服务")
    ap.add_argument(
        "--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，本机使用）"
    )
    ap.add_argument("--port", type=int, default=8321, help="监听端口（默认 8321）")
    ap.add_argument("--emotion-model", choices=list(REGISTRY), default="hse-b0")
    ap.add_argument("--light-config", default=str(ROOT / "configs/light.json"))
    args = ap.parse_args()
    MODELS.emotion_key = args.emotion_model

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
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
