"""下载当前四个模型，并验证固定 SHA-256 校验值。"""

import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
# 只导入元数据，不加载推理引擎。
import sys

sys.path.insert(0, str(ROOT / "server"))
from emotion_models import MODEL_FILES


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only", action="store_true", help="仅验证已有文件，不下载"
    )
    args = parser.parse_args()
    folder = ROOT / "models"
    if not args.verify_only:
        folder.mkdir(exist_ok=True)
    for name, (url, expected) in MODEL_FILES.items():
        target = folder / name
        if target.exists():
            if digest(target) != expected:
                raise RuntimeError(
                    f"{name}: checksum mismatch; existing file was not replaced"
                )
        elif args.verify_only:
            raise FileNotFoundError(target)
        else:
            temporary = target.with_suffix(".onnx.part")
            try:
                with (
                    urlopen(url, timeout=120) as response,
                    temporary.open("wb") as output,
                ):
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if digest(temporary) != expected:
                    raise RuntimeError(f"{name}: downloaded checksum mismatch")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        print(name, "verified")


if __name__ == "__main__":
    main()
