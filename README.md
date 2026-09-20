# 情绪晴雨表

在 SpacemiT K3 上用 USB 摄像头识别人脸表情，让网页灯带、背景和控件同步变色。画面在本机处理。当前提供屏幕灯效，尚未接入实体灯带。

默认使用 HSEmotion B0 八分类，设置中可以切换 B2 或官方七分类模型。多人入镜时暂停表情分类。识别结果描述可见表情，不代表真实心理状态。

## 安装与运行

需要 Bianbu riscv64、Python 3.14.4、系统 OpenCV 4.14 库和 USB 摄像头。环境约束见 [开发说明](docs/development.md)。在项目根目录执行：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
~/.local/bin/uv venv .venv --python /usr/bin/python3.14
~/.local/bin/uv pip install --python .venv/bin/python \
  --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple \
  -r requirements.txt
.venv/bin/python scripts/download_models.py
./run.sh
```

在 K3 浏览器打开 `http://127.0.0.1:8321/`，点击开始体验。设置里的模型切换需先停止体验。页面的停止按钮释放摄像头；终端 `Ctrl-C` 停止服务。

权重不进入 Git。下载脚本维护四个在用模型的来源和 SHA-256；已有文件校验不符时会报错，不覆盖。已有环境可直接运行 `./run.sh`。

## 验证

服务启动并使用 B0 后，关闭其他体验页面，依次运行：

```bash
.venv/bin/python scripts/smoke_vision.py
.venv/bin/python tests/test_api.py
node tests/test_light_controller.js
node tests/test_ui.js
node tests/test_browser_contract.js
node tests/test_browser_contract.js --lifecycle
node tests/test_camera.js
```

浏览器测试使用 Node.js 22+ 内置 WebSocket 和系统 Chromium，无 npm 依赖。测试自动生成的报告放在 `tests/output/`，不进入 Git。真实服务重启测试与持续测试见开发说明，不与上面的测试并行运行。

## 目录

| 路径 | 用途 |
| --- | --- |
| `server/`、`configs/` | HTTP 服务、推理和模型配置 |
| `web/` | 页面、灯效和共用测试帧 |
| `scripts/` | 模型下载校验、运行中服务冒烟 |
| `tests/` | 六个测试入口及共用浏览器工具 |
| `docs/development.md` | 当前行为、API、环境约束与开发验证 |
| `licenses/` | 第三方许可和来源 |

## 页面

下图使用明确标注的测试图片经 B0 真实推理，非现场摄像头采集。

![桌面情绪灯光](docs/images/desktop.png)

[移动端设置示例](docs/images/mobile-settings.png)。第三方来源见 [许可说明](licenses/README.md)。
