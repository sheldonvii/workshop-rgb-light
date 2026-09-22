# 情绪晴雨表

在 SpacemiT K3 上用 USB 摄像头识别人脸表情，让网页灯带、背景和控件同步变色。画面在本机处理。通过 USB Adalight 二进制协议控制 WLED 实体灯带，网页同步显示后端灯光状态；控制器不需要连接 Wi-Fi。

默认使用 HSEmotion B0 八分类，设置中可以切换 B2 或官方七分类模型。多人入镜时逐人确认表情，一致时共同增亮，不同时可选择整灯轮换或分区渐变。网页保留弯曲灯线，用沿线渐变和背景呈现颜色占比。识别结果描述可见表情，不代表真实心理状态。

## 准备控制器

已验证 WLED-Controller-S2（WLED 0.15.1）与 60 颗 RGB 灯珠。用 USB 数据线将控制器连接 K3，先在 WLED 中确认灯带类型、GPIO、颜色顺序和灯珠数量。控制过程无需连接 Wi-Fi，供电需与灯带匹配。

首次部署时，在 K3 终端为实际运行用户配置串口权限：

```bash
ls -l /dev/serial/by-id/
sudo usermod -aG dialout bianbu  # 如使用其他账户，替换为实际用户名
```

退出并重新登录，执行 `id` 确认包含 `dialout`；日常运行无需 sudo。修改 [configs/light.json](configs/light.json) 的 `port` 和 `led_count`，与实际设备一致。仅体验网页时，可将 `enabled` 设为 `false`。

接线、WLED 设置、完整参数、迁移与排障见 [控制器说明](docs/controller.md)。

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

## 一键检查

在其他网页停止体验后执行，测试会切换模型并短暂改变灯色；需要 Node.js 22+、系统 Chromium、摄像头和已连接的控制器。

```bash
./run_test.sh
```

检查覆盖灯光规则、模型文件、接口、页面交互、真实摄像头和 USB 控制器。没有服务时临时启动并在结束后停止；已有服务则保留，并恢复原模型和灯光模式。失败时终端会指出环节、日志路径和排查建议。

最近一次的汇总为 `tests/output/latest/summary.txt`，机器可读报告为 `report.json`，详细日志与截图在同目录。每次运行覆盖上一轮输出，返回码非零表示失败。仅检查代码用 `./run_test.sh --unit`，无摄像头和控制器时用 `./run_test.sh --no-hardware`；被跳过的项目不会算作通过。长时间验证与灯珠寻址说明见 [开发验证](docs/development.md#测试与复现)。
## 目录

| 路径 | 用途 |
| --- | --- |
| `server/`、`configs/` | HTTP 服务、推理和模型配置 |
| `web/` | 页面、后端灯光状态显示和共用测试帧 |
| `scripts/` | 模型下载与校验 |
| `run_test.sh`、`tests/` | 统一检查入口、测试与最近一次报告 |
| `docs/controller.md` | 控制器接线、权限、参数与排障 |
| `docs/development.md` | 当前行为、API、环境约束与开发验证 |
| `licenses/` | 第三方许可和来源 |

## 页面

下图使用明确标注的测试图片经 B0 真实推理，非现场摄像头采集。

![桌面情绪灯光](docs/images/desktop.png)

[移动端设置示例](docs/images/mobile-settings.png)。第三方来源见 [许可说明](licenses/README.md)。
