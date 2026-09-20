"""Download the current four models and verify fixed SHA-256 digests."""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
REV = '520a051c64cd191521e5934655314e769a319684'
HSE = f'https://raw.githubusercontent.com/sb-ai-lab/EmotiEffLib/{REV}/models/affectnet_emotions/onnx'
ZOO = 'https://archive.spacemit.com/spacemit-ai/model_zoo'
MODELS = {
    'yolov5n-face_cut.q.onnx': (f'{ZOO}/vision/yolov5-face/yolov5n-face_cut.q.onnx', 'acd919436d3b8965a656e952cce7d384774d8225df1055b7c6a983074883d4f9'),
    'emotion_resnet50_final.q.onnx': (f'{ZOO}/vision/resnet/emotion_resnet50_final.q.onnx', '92ece9d7bb1c35e434ebe6d5b72ec91256e178faa9c8d36f0f261d22c885b81d'),
    'enet_b0_8_best_afew.onnx': (f'{HSE}/enet_b0_8_best_afew.onnx', '7aa2ea31c1311f4f8aa9d3fdb085d418dd4e7a48c4b9ed41df8c044f91d0213f'),
    'enet_b2_8.onnx': (f'{HSE}/enet_b2_8.onnx', '180a9d4845b59393de4511598a0d1d34b705034691ea32959ce5009db7cf52b7'),
}

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true', help='Check installed files without downloading')
    args = parser.parse_args()
    folder = ROOT / 'models'
    if not args.verify_only:
        folder.mkdir(exist_ok=True)
    for name, (url, expected) in MODELS.items():
        target = folder / name
        if target.exists():
            if digest(target) != expected:
                raise RuntimeError(f'{name}: checksum mismatch; existing file was not replaced')
        elif args.verify_only:
            raise FileNotFoundError(target)
        else:
            temporary = target.with_suffix('.onnx.part')
            try:
                with urlopen(url, timeout=120) as response, temporary.open('wb') as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if digest(temporary) != expected:
                    raise RuntimeError(f'{name}: downloaded checksum mismatch')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        print(name, 'verified')

if __name__ == '__main__':
    main()
