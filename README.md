# 情绪晴雨表

在 SpacemiT K3 上用 USB 摄像头识别人脸表情，让网页灯带、背景和控件同步变色。画面在本机处理。通过 USB Adalight 二进制协议控制 WLED 实体灯带，网页同步显示后端灯光状态；控制器不需要连接 Wi-Fi。

默认使用 HSEmotion B0 八分类，设置中可以切换 B2 或官方七分类模型。多人入镜时逐人确认表情，一致时共同增亮，不同时按画面左右分区渐变。网页保留弯曲灯线，用沿线渐变和背景呈现颜色占比。识别结果描述可见表情，不代表真实心理状态。

## 硬件初始化

使用 USB 摄像头、WLED-Controller-S2（WLED 0.15.1）和 1 米 60 颗 RGB 灯珠。摄像头与控制器通过支持数据传输的 USB 接口或扩展坞连接 K3，灯带接控制器。首次启动 Demo 前完成下面两项。

### 1. 配置串口权限

在 K3 终端执行，其他账户需将 `bianbu` 替换为实际用户名：

```bash
ls -l /dev/serial/by-id/
sudo usermod -aG dialout bianbu
```

退出并重新登录，执行 `id` 确认包含 `dialout`。日常运行无需 sudo；核对 [configs/light.json](configs/light.json) 的 `port` 与实际设备路径一致。

### 2. 将控制器灯珠数量初始化为 60

控制器出厂可能只配置了 30 颗，此时 60 颗灯带可能只有前半段亮起。先检查控制器内的数量设置，再排查灯带；项目中的 `led_count=60` 不会自动改写控制器。

首次使用按 [60 颗控制器准备步骤](docs/prepare-wled.md#首次操作) 安装独立维护环境，再执行其中的 `--check` 查询。已是 60 颗则无需写入；仍为 30 颗时，按同一文档进入 USB 下载模式，执行 `--prepare`，将总数量和单路长度都改为 60。全过程通过 USB 完成，无需连接控制器 Wi-Fi。

准备后保持控制器正常启动、Demo 服务停止，用维护环境再次检查：

```bash
.venv-controller/bin/python scripts/prepare_wled_60.py --check \
  --port /dev/serial/by-id/usb-WEMOS.CC_LOLIN-S2-MINI_0-if00
```

端口替换为实际路径。返回 `PASS` 表示控制器已配置 60 颗、850mA；启动 Demo 后再观察整条灯带是否亮起。接线、灯带类型、GPIO、颜色顺序和排障见 [控制器说明](docs/controller.md)。仅体验网页时，可将 `enabled` 设为 `false`，跳过控制器初始化。

## 安装与运行

以下命令在 K3 的项目根目录执行，适用于已安装 Python 3.14.4 和 uv 的 Bianbu 4.0.3 riscv64。板端 uv 默认位于 `~/.local/bin/uv`；若安装在其他位置，替换下面的命令路径。

### 1. 创建虚拟环境

```bash
~/.local/bin/uv venv .venv --python /usr/bin/python3.14
source .venv/bin/activate
```

uv 使用默认缓存即可，无需先设置缓存目录。

### 2. 安装 OpenCV 系统库

Vision 0.1.4 需要 SpacemiT 的 OpenCV 5.0 系统库。首次部署在 K3 执行：

```bash
sudo apt update
sudo apt install opencv-spacemit=5.0.0-1bb1
dpkg-query -W opencv-spacemit
```

最后一条应显示 `5.0.0-1bb1`。此系统包安装到 `/opt/opencv-spacemit`；不能用 Ubuntu 通用 OpenCV 4.x 包代替。若软件源找不到该版本，先核对 Bianbu 4 软件源，详见 [环境说明](docs/development.md#环境与路径)。

### 3. 安装 Python 依赖并检查 OpenCV 与 Vision

```bash
~/.local/bin/uv pip install --python .venv/bin/python \
  --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple \
  -r requirements.txt
.venv/bin/python - <<'PY'
import cv2
from spacemit_vision import extension_available, extension_import_error

print("OpenCV:", cv2.__version__)
if not extension_available():
    raise RuntimeError(extension_import_error())
print("Vision 扩展可用")
PY
```

`requirements.txt` 安装 `opencv-python==5.0.0.93` 和 `spacemit_vision==0.1.4` 等依赖。Python 的 `cv2` 包与上一步的 OpenCV 系统库都需要安装；检查应输出 `OpenCV: 5.0.0` 和 `Vision 扩展可用`。

### 4. 下载模型并启动

确认已完成上方硬件初始化，再执行：

```bash
.venv/bin/python scripts/download_models.py
./run.sh
```

在 K3 浏览器打开 `http://127.0.0.1:8321/`，点击开始体验。设置里的模型切换需先停止体验。页面的停止按钮释放摄像头；终端 `Ctrl-C` 停止服务。已有环境和已初始化的控制器，日常直接执行 `./run.sh`。

权重不进入 Git。[server/emotion_models.py](server/emotion_models.py) 维护四个在用模型的来源和 SHA-256；下载脚本共用该信息，已有文件校验不符时会报错，不覆盖。

## 一键检查

在其他网页停止体验后执行，测试会切换模型并短暂改变灯色；需要 Node.js 22+、系统 Chromium、摄像头和已连接的控制器。

```bash
./run_test.sh
```

检查覆盖灯光规则、模型文件、接口、页面交互、真实摄像头和 USB 控制器。没有服务时临时启动并在结束后停止；已有服务则保留，并恢复原模型。失败时终端会指出环节、日志路径和排查建议。

最近一次的汇总为 `tests/output/latest/summary.txt`，机器可读报告为 `report.json`，详细日志与截图在同目录。每次运行覆盖上一轮输出，返回码非零表示失败。仅检查代码用 `./run_test.sh --unit`，无摄像头和控制器时用 `./run_test.sh --no-hardware`；被跳过的项目不会算作通过。长时间验证与灯珠寻址说明见 [开发验证](docs/development.md#测试与复现)。

## 目录

| 路径 | 用途 |
| --- | --- |
| `server/`、`configs/` | HTTP 服务、推理和模型配置 |
| `web/` | 页面、后端灯光状态显示和共用测试帧 |
| `scripts/` | 模型下载校验、控制器首次准备 |
| `run_test.sh`、`tests/` | 统一检查入口、测试与最近一次报告 |
| `docs/controller.md` | 控制器接线、权限、参数与排障 |
| `docs/development.md` | 当前行为、API、环境约束与开发验证 |
| `licenses/` | 第三方许可和来源 |

## 页面

下图使用明确标注的测试图片经 B0 真实推理，非现场摄像头采集。

![桌面情绪灯光](docs/images/desktop.png)

[移动端设置示例](docs/images/mobile-settings.png)。第三方来源见 [许可说明](licenses/README.md)。
