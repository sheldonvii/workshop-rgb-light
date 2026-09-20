"""Author-compatible HSEmotion ONNX inference with an explicit model registry."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parent.parent
LABELS = ['angry', 'contempt', 'disgust', 'fear', 'happy', 'neutral', 'sad', 'surprise']
ZH = dict(zip(LABELS, ['生气', '轻蔑', '厌恶', '害怕', '开心', '平静', '难过', '惊讶']))
REGISTRY = {
    'hse-b0': {'file': 'enet_b0_8_best_afew.onnx', 'name': 'HSEmotion B0', 'size': 224},
    'hse-b2': {'file': 'enet_b2_8.onnx', 'name': 'HSEmotion B2', 'size': 260},
}

class EmotionModel:
    def __init__(self, key='hse-b0'):
        import onnxruntime as ort
        import numpy as np
        cfg = REGISTRY[key]
        path = ROOT / 'models' / cfg['file']
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=['CPUExecutionProvider'])
        self.key, self.size = key, cfg['size']
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        self.input_name = self.session.get_inputs()[0].name
        self.info = {**cfg, 'key': key, 'labels': LABELS,
                     'providers': self.session.get_providers(),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'source': 'https://github.com/sb-ai-lab/EmotiEffLib/tree/520a051c64cd191521e5934655314e769a319684/models/affectnet_emotions/onnx',
                     'score_type': 'softmax', 'preprocessing': 'BGR to RGB, bilinear resize, ImageNet mean/std, NCHW float32'}

    def predict(self, bgr):
        import cv2
        import numpy as np
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        x = cv2.resize(rgb, (self.size, self.size)).astype(np.float32) / 255.0
        x = ((x - self.mean) / self.std).transpose(2, 0, 1)[None]
        logits = self.session.run(None, {self.input_name: x})[0][0].astype(np.float64)
        if logits.shape != (8,) or not np.isfinite(logits).all():
            raise RuntimeError('Invalid emotion output')
        prob = np.exp(logits - logits.max())
        prob /= prob.sum()
        top = int(prob.argmax())
        margin = float(np.sort(prob)[-1] - np.sort(prob)[-2])
        return {'label': LABELS[top], 'label_zh': ZH[LABELS[top]], 'mood': LABELS[top],
                'score': round(float(prob[top]), 6), 'score_type': 'softmax',
                'raw_logit': round(float(logits[top]), 6), 'margin': round(margin, 6),
                'uncertain': float(prob[top]) < 0.45 or margin < 0.12,
                'distribution': [{'label': label, 'label_zh': ZH[label],
                                  'score': round(float(prob[i]), 6)} for i, label in enumerate(LABELS)],
                'model': self.key}

    def release(self):
        self.session = None
