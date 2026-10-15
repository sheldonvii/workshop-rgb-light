"""统一分类器接口；模型来源、校验值与中文标签在此维护。"""

import hashlib
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ["angry", "contempt", "disgust", "fear", "happy", "neutral", "sad", "surprise"]
ZH = dict(zip(LABELS, ["生气", "轻蔑", "厌恶", "害怕", "开心", "平静", "难过", "惊讶"]))
# 官方分类下标必须使用其标签文件顺序，不能使用 HSE 的八类顺序。
OFFICIAL_LABELS = (ROOT / "configs/emotion.txt").read_text().split()
REV = "520a051c64cd191521e5934655314e769a319684"
HSE = f"https://raw.githubusercontent.com/sb-ai-lab/EmotiEffLib/{REV}/models/affectnet_emotions/onnx"
ZOO = "https://archive.spacemit.com/spacemit-ai/model_zoo"
MODEL_FILES = {
    "yolov5n-face_cut.q.onnx": (
        f"{ZOO}/vision/yolov5-face/yolov5n-face_cut.q.onnx",
        "acd919436d3b8965a656e952cce7d384774d8225df1055b7c6a983074883d4f9",
    ),
    "emotion_resnet50_final.q.onnx": (
        f"{ZOO}/vision/resnet/emotion_resnet50_final.q.onnx",
        "92ece9d7bb1c35e434ebe6d5b72ec91256e178faa9c8d36f0f261d22c885b81d",
    ),
    "enet_b0_8_best_afew.onnx": (
        f"{HSE}/enet_b0_8_best_afew.onnx",
        "7aa2ea31c1311f4f8aa9d3fdb085d418dd4e7a48c4b9ed41df8c044f91d0213f",
    ),
    "enet_b2_8.onnx": (
        f"{HSE}/enet_b2_8.onnx",
        "180a9d4845b59393de4511598a0d1d34b705034691ea32959ce5009db7cf52b7",
    ),
}
REGISTRY = {
    "hse-b0": {"file": "enet_b0_8_best_afew.onnx", "name": "HSEmotion B0", "size": 224},
    "hse-b2": {"file": "enet_b2_8.onnx", "name": "HSEmotion B2", "size": 260},
    "official": {
        "file": "emotion_resnet50_final.q.onnx",
        "name": "SpacemiT ResNet50",
        "size": 224,
    },
}
# 当前 Demo 的 CPU 并行和不确定判定参数，非准确率校准结论。
CPU_THREADS = 4
MIN_SCORE = 0.45
MIN_MARGIN = 0.12


def model_file_info(filename):
    source, sha256 = MODEL_FILES[filename]
    return {"name": filename, "source": source, "sha256": sha256}


def vision_infer(service, image, operation):
    """SDK 用状态返回失败；转成异常后由服务入口统一记录日志。"""
    from spacemit_vision import VisionServiceStatus

    status, results = service.infer_image(image)
    if status != VisionServiceStatus.OK:
        raise RuntimeError(f"{operation}失败: {status}")
    return results


class EmotionModel:
    def __init__(self, key="hse-b0"):
        import numpy as np
        import onnxruntime as ort

        cfg = REGISTRY[key]
        path = ROOT / "models" / cfg["file"]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = CPU_THREADS
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.key, self.size = key, cfg["size"]
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        self.input_name = self.session.get_inputs()[0].name
        self.info = {
            **model_file_info(cfg["file"]),
            **cfg,
            "key": key,
            "labels": LABELS,
            "providers": self.session.get_providers(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "score_type": "softmax",
            "preprocessing": "BGR to RGB, bilinear resize, ImageNet mean/std, NCHW float32",
        }

    def predict(self, bgr):
        import cv2
        import numpy as np

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        x = cv2.resize(rgb, (self.size, self.size)).astype(np.float32) / 255.0
        x = ((x - self.mean) / self.std).transpose(2, 0, 1)[None]
        logits = self.session.run(None, {self.input_name: x})[0][0].astype(np.float64)
        if logits.shape != (8,) or not np.isfinite(logits).all():
            raise RuntimeError("表情模型输出维度或数值无效")
        prob = np.exp(logits - logits.max())
        prob /= prob.sum()
        top = int(prob.argmax())
        margin = float(np.sort(prob)[-1] - np.sort(prob)[-2])
        return {
            "label": LABELS[top],
            "label_zh": ZH[LABELS[top]],
            "mood": LABELS[top],
            "score": round(float(prob[top]), 6),
            "score_type": "softmax",
            "raw_logit": round(float(logits[top]), 6),
            "margin": round(margin, 6),
            "uncertain": float(prob[top]) < MIN_SCORE or margin < MIN_MARGIN,
            "distribution": [
                {
                    "label": label,
                    "label_zh": ZH[label],
                    "score": round(float(prob[i]), 6),
                }
                for i, label in enumerate(LABELS)
            ],
            "model": self.key,
        }

    def release(self):
        self.session = None


class OfficialEmotionModel:
    """保留官方 logit 分数和七类顺序，提供与 HSE 相同的调用接口。"""

    def __init__(self):
        from spacemit_vision import VisionServiceNative

        self.session = VisionServiceNative.create(
            str(ROOT / "configs/emotion.yaml"), timing_print_to_stdout=False
        )
        self.info = {
            **model_file_info(REGISTRY["official"]["file"]),
            "key": "official",
            "labels": OFFICIAL_LABELS,
            "score_type": "logit",
            "providers": ["SpaceMITExecutionProvider"],
            "input": "[1,3,224,224]",
        }

    def predict(self, bgr):
        results = vision_infer(self.session, bgr, "表情分类")
        if not results:
            return None
        index = int(results[0].label)
        if not 0 <= index < len(OFFICIAL_LABELS):
            raise RuntimeError(f"官方表情模型类别下标越界: {index}")
        score = float(results[0].score)
        if not math.isfinite(score):
            raise RuntimeError("官方表情模型分数无效")
        label = OFFICIAL_LABELS[index]
        return {
            "label": label,
            "label_zh": ZH[label],
            "mood": label,
            "score": round(score, 4),
            "score_type": "logit",
            "model": "official",
            "uncertain": False,
        }

    def release(self):
        self.session.release()


def load_classifier(key):
    """预热失败释放候选模型；调用方仍持有原模型，切换失败可继续使用。"""
    import cv2  # noqa: F401  先加载 OpenCV，满足 Vision 的动态库依赖。
    import numpy as np

    classifier = OfficialEmotionModel() if key == "official" else EmotionModel(key)
    try:
        classifier.predict(np.zeros((64, 64, 3), dtype=np.uint8))
    except Exception:
        classifier.release()
        raise
    return classifier
