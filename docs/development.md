# 架构设计与开发指南

本文档介绍情绪晴雨表的核心数据流、模型推理管线、HTTP API 接口规范与测试验证流程。

## 运行环境与依赖

Demo 运行于 SpacemiT K3 平台（Bianbu 4.0.3 riscv64，Python 3.14.4）。

视觉推理依赖 SpacemiT 专用加速库，分为系统层与 Python 运行层两部分：

| 依赖组件 | 安装路径与方式 | 验证方法 | 说明 |
| --- | --- | --- | --- |
| SpacemiT OpenCV 系统库 | `sudo apt install opencv-spacemit=5.0.0-1bb1`（安装至 `/opt/opencv-spacemit`） | `dpkg-query -W opencv-spacemit` 应输出 `5.0.0-1bb1` | 平台底层加速库，与通用 Ubuntu OpenCV 4.x 互不兼容 |
| Python cv2 绑定 | `uv pip install -r requirements.txt`，锁定 `opencv-python==5.0.0.93` | 虚拟环境中执行 `python -c "import cv2; print(cv2.__version__)"` 应输出 `5.0.0` | 包含 `cv2/config-3.14.py`，需从 SpacemiT 私有源索引安装 |
| Vision NPU 扩展 | `spacemit_vision==0.1.4` | `python -c "from spacemit_vision import extension_available; assert extension_available()"` | 调用 K3 NPU 执行硬件加速推理 |

服务启动时按顺序加载 `cv2` 与 `spacemit_vision`。脚本与服务入口会自动将工作目录重设为项目根目录，模型文件、标签及测试帧均使用相对路径寻址。板端浏览器运行测试时依赖系统中文矢量字体（如 `Noto Sans CJK SC`）。

## 数据链路与模型管线

系统采用前后端解耦架构，摄像头画面在本地完成检测、裁剪与分类：

```text
USB 摄像头采集帧 (720p JPEG)
  └──> YOLOv5-Face 人脸检测 (SpaceMITExecutionProvider)
        └──> 原图人脸区域高保真裁剪
              └──> 表情模型分类 (HSEmotion / 官方模型)
                    └──> 时序平滑与短期轨迹追踪
                          ├──> 空间分区渐变算法 ──> USB Adalight 实体灯带 (60 颗 WS2812B)
                          └──> 情绪快照与补间轨迹 ──> Web 前端实时可视化
```

### 1. 模型配置与阈值判定

| 任务 | 模型文件 | 引擎与提供程序 | 输入与预处理规范 |
| --- | --- | --- | --- |
| 人脸检测 | `yolov5n-face_cut.q.onnx` | Vision 0.1.4, SpaceMITExecutionProvider | 720p 图像输入，输出人脸包围盒坐标 |
| 默认表情识别 | HSEmotion B0 (8分类) | ONNX Runtime, CPUExecutionProvider (4线程) | 224×224 RGB，ImageNet 均值方差归一化，NCHW float32 |
| 对照表情识别 | HSEmotion B2 (8分类) | ONNX Runtime, CPUExecutionProvider (4线程) | 260×260 RGB，同上预处理，高精度对照 |
| 官方对照模型 | ResNet50 (7分类) | Vision 0.1.4, SpaceMITExecutionProvider | 原生 Vision 引擎，标签顺序见 `configs/emotion.txt` |

HSEmotion 模型输出标签顺序为 angry、contempt、disgust、fear、happy、neutral、sad、surprise。输出通过 Softmax 计算分类概率。当最高概率低于 0.45，或前两名分差小于 0.12 时，分类结果标记为 uncertain（不确定），交由后续时序滤波器平滑。

### 2. 多人追踪与空间几何关联

系统检测画面中的所有人脸，并通过几何关联算法实现短期跟踪：
* **几何匹配**：基于归一化人脸中心距离与包围盒面积比计算相似度，为有效人脸分配短期 `track_id`，容忍检测抖动与小幅度位移。
* **时序滤波**：每张人脸独立维护滤波实例。单一情绪持续稳定至少 300 毫秒（且不少于 2 帧有效结果），或在 1.2 秒滑动窗口内占比达到 75% 时判定为已确认。
* **防频闪与离场宽限**：同一人脸的状态切换间隔至少 600 毫秒；人脸短暂遮挡或偏头时保留旧状态 900 毫秒，超时后退出群体占比。

## 灯效策略与交互规则

后端负责实时维护群体情绪状态与 60 颗灯珠的颜色输出，前端仅同步状态并执行视觉补间：

| 交互维度 | 控制机制 | 说明 |
| --- | --- | --- |
| 帧提交节奏 | 约 350 毫秒/次 | 保证单帧在途，忙时主动跳帧以防请求堆积 |
| 颜色平滑过渡 | 400 毫秒过渡时间 | 后端生成最多 30 FPS 的 Adalight 渐变帧，网页按同一轨迹补间 |
| 协同增亮策略 | 单人 220mA / 2-3人 450mA / 4人及以上 700mA | 全员稳定且情绪一致时触发提亮；存在未确认人脸时不增档 |
| 空间分区模式 | 左右横向投影分区 | 多人情绪不同时，按画面坐标由左至右划分等长灯区，交界处以 4 颗灯珠混色 |
| 历史记录去重 | 按情绪人数构成去重 | 仅记录保持稳定的状态区间，过渡动画不记入历史 |

## HTTP API 接口规范

| 路径 | 方法 | 请求体 | 核心响应字段 | 说明 |
| --- | --- | --- | --- | --- |
| `/api/health` | GET | 无 | `status`, `engine`, `models`, `palette` | 查询服务健康度、推理引擎、模型列表与当前色板 |
| `/api/model` | POST | `{"model": "hse-b0"}` | `ok`, `model` | 切换情绪模型（可选 `hse-b0` / `hse-b2` / `official`），需停止体验后调用 |
| `/api/infer` | POST | JPEG 二进制 (≤8MB) | `faces`, `primary`, `group`, `light`, `timing` | 提交图像推理，支持通过 `X-Run-Id` 和递增 `X-Seq` 关联体验会话 |
| `/api/light` | GET | 无 | `group`, `pixels`, `transition`, `device` | 轮询当前灯态、逐像素色彩数组、过渡轨迹与控制器状态 |
| `/api/light/session` | POST | `{"action": "start", "run_id": "xxx"}` | `ok`, `session` | 会话控制（`start` / `stop` / `pause` / `resume`），心跳租期 8 秒 |
| `/` | GET | 无 | HTML | 静态交互界面，仅允许读取 `web/` 目录静态资源 |

### 推理调用示例

通过命令行可快速模拟前端与服务的标准会话流程：

```bash
# 1. 注册体验会话并获取控制权
curl -s http://127.0.0.1:8321/api/light/session -H 'Content-Type: application/json' \
  -d '{"action":"start","run_id":"demo-dev"}'

# 2. 携带会话 ID 与递增序号提交推理
curl -s http://127.0.0.1:8321/api/infer -H 'X-Run-Id: demo-dev' -H 'X-Seq: 1' \
  -H 'X-Mirror: true' --data-binary @web/test-frames/multi.jpg

# 3. 停止体验并释放控制权
curl -s http://127.0.0.1:8321/api/light/session -H 'Content-Type: application/json' \
  -d '{"action":"stop","run_id":"demo-dev"}'
```

### 多人数据快照字段说明

| 字段路径 | 数据类型与单位 | 说明 |
| --- | --- | --- |
| `faces[].emotion` | 字典或 null | 当前帧原始识别结果，包含英文标签、中文名及置信度 |
| `faces[].track_id` | 整数 | 几何跟踪短期编号，由当前活跃会话维护 |
| `group.people[]` | 数组 | 活跃跟踪列表，包含归一化位置 `position`、可见状态 `visible` 及已确认状态 `current` |
| `group.composition[]`| 数组 | 群体情绪汇总，包含各类情绪的统计人数及对应色标 |
| `group.agreed` | 布尔值 | 当且仅当所有人员可见、稳定且情绪一致时为 true |
| `pixels` | 数组 (`[r, g, b]`) | 当前时刻 60 颗灯珠的实时未缩放 RGB 色彩数组 |
| `power` | 浮点数 (0~1) | 当前电流对齐对应的输出缩放系数，实际发帧数值为 `round(channel * power)` |
| `transition` | 字典 | 包含过渡起点、终点、逐像素数组与经过时间 `elapsed_ms`，用于前端补间 |

## 测试与质量验证

测试套件统一通过 `./run_test.sh` 调度，按需提供不同梯度的验证模式：

```bash
./run_test.sh                     # 全套自动化验收（覆盖硬件串口、模型接口及无头浏览器 UI）
./run_test.sh --unit              # 离线运行单元测试（校验状态机、电流计算与时序规则）
./run_test.sh --no-hardware       # 绕过摄像头与物理控制器，使用静态测试帧运行完整流程
./run_test.sh --pixels --seconds 15 # 独立点亮实体灯带，验证寻址与通道顺序
./run_test.sh --soak 1800         # 运行 30 分钟摄像头持续采集与推理压力测试
```

测试产物统一输出至 `tests/output/latest/`，包括汇总报告 `summary.txt`、机器可读报告 `report.json`、断言日志及失败截图。

可通过环境变量调整测试目标：`BAROMETER_URL` 指定服务地址（默认 `http://127.0.0.1:8321`），`CHROMIUM` 指定浏览器路径（默认 `/usr/lib/chromium/chromium`），`TEST_PYTHON` 指定 Python 解释器。

## 故障排查

* **模型加载或校验失败**：执行 `python scripts/download_models.py --verify-only` 查看具体文件的 SHA-256 校验状态。若报错缺少符号，优先检查系统的 `opencv-spacemit` 版本是否为 `5.0.0-1bb1`。
* **摄像头无法打开**：确认设备节点权限及占用情况。浏览器需在安全上下文（如 `http://127.0.0.1:8321`）中运行方可调取摄像头。
* **NPU 内存申请失败 (`tcm buffer acquire failed`)**：执行 `spacemit-tcm-smi` 检查板端显存占用，定位并终止异常占用 TCM 的遗留进程。

## 代码与开发规范

* **格式化检查**：Python 代码使用 Ruff（规则 `check --select F,I` 和 `format`），前端代码（HTML/CSS/JS）使用 Prettier 3.6.2，排除第三方库 `lucide.min.js`。
* **编码规范**：所有源文件与配置文件均采用 UTF-8 编码、LF 换行符保存。
