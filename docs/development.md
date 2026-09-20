# 开发说明

本文描述当前实现。源码、配置和可运行测试是维护对象；测试输出、模型权重和本机环境不提交。

## 环境与路径

运行平台为 SpacemiT K3、Bianbu 4.0.3 riscv64，Python 3.14.4。Python 依赖和下游安装索引固定在 `requirements.txt`。模型 URL、版本和 SHA-256 统一维护在 `scripts/download_models.py`。

`spacemit_vision` 锁定 0.1.2。该引擎依赖 `/opt/opencv-spacemit` 的 OpenCV 4.14 系统库；0.1.3/0.1.4 需要 OpenCV 5.0 系统库，本环境不能直接升级。Python wheel `opencv-python==5.0.0.93` 与引擎链接的系统 OpenCV 是两套依赖，不可混淆。服务先导入 `cv2` 再导入 Vision 引擎。

Python 3.14 对应的 OpenCV wheel 必须包含 `cv2/config-3.14.py`。安装只使用 SpacemiT 下游索引，避免混源解析成需要编译的源码包。首次重建需要网络，`.venv` 与 `.uv-cache` 不随仓库分发。

项目可以放在其他目录。`run.sh` 和服务入口会将工作目录设为项目根目录，YAML 中的模型、标签和测试图均为相对路径。浏览器测试从自身位置推导根目录。浏览器需要中文字体，如系统 `Noto Sans CJK SC`。

## 数据链路与模型

摄像头提交 JPEG → YOLOv5-Face 检测 → 单人脸原图裁剪 → 表情分类 → 原始分数展示 → 稳定灯效输出。

| 模块 | 当前行为 |
| --- | --- |
| 人脸检测 | `yolov5n-face_cut.q.onnx`，Vision 0.1.2，SpaceMITExecutionProvider |
| 默认表情 | HSEmotion B0，RGB、双线性缩放 224×224、ImageNet mean/std、NCHW float32，CPUExecutionProvider 四线程 |
| B2 对照 | 同一预处理规则，输入尺寸 260×260，CPUExecutionProvider |
| 官方对照 | ResNet50 七分类，Vision 引擎，SpaceMITExecutionProvider |

B0/B2 标签顺序为 angry、contempt、disgust、fear、happy、neutral、sad、surprise。输出经 softmax 转成分数；最高分低于 0.45，或前两名差值低于 0.12 时标为不确定。softmax 分数不是经过校准的真实心理概率。官方模型标签顺序见 `configs/emotion.txt`，原始分数为 logit，不能当百分比。

只在检测到一张脸时进行分类。无人或多人时 `primary=null`；多人画面显示所有红色人脸框并提示调整位置。没有主角选择、人脸身份跟踪或多人情绪融合。

## 灯效与页面

页面采用相机与灯带并排的布局，右上设置抽屉放置模型选项、原始分数、灯效规则和识别说明。明暗主题均支持整页情绪配色；贴纸、按钮、灯带和背景由同一稳定结果驱动。窄屏纵向布局，尊重减少动效偏好。

| 规则 | 当前值 |
| --- | --- |
| 提交节奏 | 约 350 毫秒；同一时间最多一个推理请求，忙时跳帧 |
| 连续确认 | 同一可靠类别连续至少 300 毫秒，至少两个结果 |
| 重复确认 | 或最近 1.2 秒内同一可靠类别出现至少三次，占全部单人结果至少 75% |
| 换色间隔 | 两次颜色输出至少相隔 600 毫秒，两条确认通路均遵守 |
| 过渡 | 背景、灯带及情绪控件用 450 毫秒渐变 |
| 不可靠结果 | 不确定、无人或多人持续 900 毫秒后清除旧灯色 |

单帧不能确认；重复、乱序输入不计票。无人、多人以及超过 1 秒的采样间断会清除候选证据。完全没有新结果时，从最后采样后的 1 秒开始计入 900 毫秒宽限；前端定时检查存在调度间隔。停止、切到后台、摄像头结束或结果失效会清除状态。

如果短暂多人后恢复单人且类别与保留灯色相同，可恢复该灯色；不同类别仍需确认。历史记录表示灯色保持区间，包含短暂保持旧色的时间，不等于逐帧情绪事实。侧栏始终区分原始判断与稳定灯效。

300 毫秒是确认门槛，端到端响应还受采样和推理耗时影响。当前没有 GPIO、串口或实体灯带驱动。接硬件时应使用稳定输出事件，不能直接把浏览器每个动画帧转成灯带指令。

## 接口

| 接口 | 请求与返回 |
| --- | --- |
| `GET /api/health` | 服务状态、执行引擎、当前模型、可用模型、标签及加载时间 |
| `POST /api/model` | JSON `{ "model": "hse-b0" }`；可选 `hse-b0`、`hse-b2`、`official` |
| `POST /api/infer` | 请求体为 JPEG，最多 8 MiB；`X-Run-Id` 和 `X-Seq` 原样回显 |
| `GET /` | 静态页面，仅允许访问 `web/` 内的文件 |

推理返回 `ok`、`run_id`、`seq`、`frame`、`count`、`faces`、`primary`、`timing`、`server_ts`。坐标对应上传原图。镜像仅影响预览和框的显示，模型输入不镜像。服务只允许一个推理请求在途，其他请求返回 409。未就绪为 503，非法图片为 400，推理失败为 500。前端按运行标识和序号丢弃旧结果。

切换模型要求停止页面体验；切换会先加载并 warmup 新模型，成功后释放旧模型。重启服务默认回到 B0，或用 `--emotion-model` 指定启动模型。

## 测试与复现

测试前关闭其他体验页面，确保 B0 已就绪。测试按顺序运行，避免相互切换模型、争用摄像头或使服务进入 busy。

| 入口 | 职责 |
| --- | --- |
| `tests/test_light_controller.js` | 连续／重复确认、噪声、换色间隔、过期、无人、重置 |
| `tests/test_api.py` | 无人／单人／多人、八类分数、原图坐标、错误请求和路径限制 |
| `tests/test_ui.js` | 四种视口、侧栏焦点、明暗配色、低动效、真实测试帧及合成 UI 状态 |
| `tests/test_browser_contract.js` | 三模型切换、人数状态、镜像、不确定、停止和权限拒绝 |
| `tests/test_camera.js` | 真实 USB 摄像头至少 1280×720、画面与推理更新、停止释放 |
| `tests/test_soak.js` | 摄像头持续运行并发单人分类压力、RSS、视频帧／序号推进、资源释放 |

浏览器工具 `tests/browser.js` 使用 Node 22+ 自带 WebSocket；默认系统 Chromium 为 `/usr/lib/chromium/chromium`，可通过 `CHROMIUM` 指定。测试使用临时浏览器目录，结束时清理。`--use-fake-ui-for-media-stream` 仅自动允许权限，摄像头测试仍读取真实设备。

`BAROMETER_URL` 可指定测试服务地址。持续测试和重启测试需要本机 `/proc`，会按当前项目入口及端口定位唯一服务，拒绝匹配不到或匹配多个进程的情况。

```bash
# 模拟权限等待、迟到帧和请求失败；不停止真实服务
node tests/test_browser_contract.js --lifecycle

# 手动恢复验证：会停止并重新启动当前项目服务，结束后保持服务运行
node tests/test_browser_contract.js --restart

# 默认 30 分钟，包含摄像头及真实单人分类压力
node tests/test_soak.js

# 仅验证持续测试脚本的短程执行，不代表通过 30 分钟验收
SOAK_SECONDS=15 node tests/test_soak.js

# 无摄像头时用测试图片验证真实推理及压力循环，不验证摄像头硬件
SOAK_SECONDS=15 node tests/test_soak.js --frames
```

重启测试启动的服务日志写入 `tests/output/runtime/server.log`。其他报告按测试职责写入 `tests/output/` 子目录。输出不进入 Git；仅保留 `docs/images/` 中当前页面的展示图。通过与否以本次命令退出码及本次报告为准，不沿用历史测试结论。

`?frames=test` 显式启用隔离测试源，页面常驻提示测试图片；请求仍执行真实推理。三张共用输入位于 `web/test-frames/`。合成 UI fixture 仅验证颜色与布局，不证明模型准确率。项目没有带真人表情标注的准确率评测集。

## 故障处理

模型缺失或校验失败时运行 `scripts/download_models.py --verify-only` 查看具体文件。加载失败先检查 `/api/health` 和服务终端输出，再核对 Python、Vision 与系统 OpenCV 版本。摄像头失败检查 USB 连接、设备占用和浏览器权限；外部 HTTP 地址通常不满足摄像头安全上下文，默认使用板端 localhost。

出现 `tcm buffer acquire failed` 时，用 `spacemit-tcm-smi` 检查占用及进程归属。不要直接全局清理，其他应用可能共用 TCM。冒烟脚本通过运行中服务验证检测和分类，不额外加载第二套 NPU 引擎。
