# 开发说明

本文描述当前实现。源码、配置和可运行测试是维护对象；测试输出、模型权重和本机环境不提交。

## 环境与路径

运行平台为 SpacemiT K3、Bianbu 4.0.3 riscv64，Python 3.14.4。Python 依赖和下游安装索引固定在 `requirements.txt`。模型 URL、版本和 SHA-256 统一维护在 `scripts/download_models.py`。

`spacemit_vision` 锁定 0.1.2。该引擎依赖 `/opt/opencv-spacemit` 的 OpenCV 4.14 系统库；0.1.3/0.1.4 需要 OpenCV 5.0 系统库，本环境不能直接升级。Python wheel `opencv-python==5.0.0.93` 与引擎链接的系统 OpenCV 是两套依赖，不可混淆。服务先导入 `cv2` 再导入 Vision 引擎。

Python 3.14 对应的 OpenCV wheel 必须包含 `cv2/config-3.14.py`。安装只使用 SpacemiT 下游索引，避免混源解析成需要编译的源码包。首次重建需要网络，`.venv` 与 `.uv-cache` 不随仓库分发。

项目可以放在其他目录。`run.sh` 和服务入口会将工作目录设为项目根目录，YAML 中的模型、标签和测试图均为相对路径。浏览器测试从自身位置推导根目录。浏览器需要中文字体，如系统 `Noto Sans CJK SC`。

## 数据链路与模型

摄像头提交 JPEG → YOLOv5-Face 检测 → 每张人脸原图裁剪 → 逐人分类与确认 → 汇总占比 → 分区渐变输出。

| 模块 | 当前行为 |
| --- | --- |
| 人脸检测 | `yolov5n-face_cut.q.onnx`，Vision 0.1.2，SpaceMITExecutionProvider |
| 默认表情 | HSEmotion B0，RGB、双线性缩放 224×224、ImageNet mean/std、NCHW float32，CPUExecutionProvider 四线程 |
| B2 对照 | 同一预处理规则，输入尺寸 260×260，CPUExecutionProvider |
| 官方对照 | ResNet50 七分类，Vision 引擎，SpaceMITExecutionProvider |

B0/B2 标签顺序为 angry、contempt、disgust、fear、happy、neutral、sad、surprise。输出经 softmax 转成分数；最高分低于 0.45，或前两名差值低于 0.12 时标为不确定。softmax 分数不是经过校准的真实心理概率。官方模型标签顺序见 `configs/emotion.txt`，原始分数为 logit，不能当百分比。

所有检测到的人脸依次分类，不设三人上限。每个 `faces[]` 包含 `emotion`；`primary` 保留单人兼容格式，无人或多人时仍为 null。有效控制会话为脸分配短期 `track_id`，依据归一化中心距离和面积匹配，容忍检测列表重排与小幅移动。编号不代表身份，遮挡、交叉和快速移动可能重建或误配编号，不用于身份识别。

## 灯效与页面

页面采用相机与灯带并排的布局，右上设置抽屉放置模型选项、原始分数、灯效规则和识别说明。明暗主题均支持整页情绪配色；贴纸、按钮、灯带和背景由同一稳定结果驱动。窄屏纵向布局，尊重减少动效偏好。

| 规则 | 当前值 |
| --- | --- |
| 提交节奏 | 约 350 毫秒；同一时间最多一个推理请求，忙时跳帧 |
| 连续确认 | 同一可靠类别连续至少 300 毫秒，至少两个结果 |
| 重复确认 | 或每张脸最近 1.2 秒内同一可靠类别出现至少三次，占该脸全部结果至少 75% |
| 换色间隔 | 两次颜色输出至少相隔 600 毫秒，两条确认通路均遵守 |
| 过渡 | 后端生成 400 毫秒渐变，USB 最多 30 帧/秒，网页显示同一轨迹 |
| 不可靠结果 | 每张脸独立保留旧色 900 毫秒，失效者退出占比；全部失效后回待机 |
| 亮度 | 单人或不同表情 60%；2～3 人全员一致 78%；4 人及以上一致 95% |

单帧不能确认；重复、乱序输入不计票。某张脸不再出现或超过 1 秒的采样间断会清除其候选证据。完全没有新结果时，从最后采样后的 1 秒开始计入 900 毫秒宽限；后端独立线程定时检查。停止、切到后台、摄像头结束后清除全部轨迹和情绪证据并渐变回待机。

多人一致增亮要求每个人当前可见且稳定，同色但有人待确认不增亮。不同表情可选择按画面左右分区，或整灯按情绪轮换。整灯模式每种情绪停留人数 × 1.2 秒、最多 2.4 秒，含 400 毫秒渐变；轮换不重置逐人确认，也不改变网页占比背景。沿线颜色占比和背景按确认人数汇总，各人按钮和人脸框可打开对应模型分数。未确认者单独提示，不计入颜色占比。历史按确认的情绪人数构成去重，记录保持区间，不记录过渡动画帧；相同构成交换位置不增加历史。

300 毫秒是确认门槛，端到端响应还受采样和推理耗时影响。`server/group_mood.py` 管理短期轨迹、逐人滤波和群体状态，`server/lighting.py` 管理会话、逐像素渐变和 USB 输出。网页只同步状态。串口权限、接线和配置见 [控制器说明](controller.md)。

## 接口

| 接口 | 请求与返回 |
| --- | --- |
| `GET /api/health` | 服务状态、执行引擎、当前模型、可用模型、标签及加载时间 |
| `POST /api/model` | JSON `{ "model": "hse-b0" }`；可选 `hse-b0`、`hse-b2`、`official` |
| `POST /api/infer` | 请求体为 JPEG，最多 8 MiB；`X-Run-Id` 和 `X-Seq` 原样回显；`X-Mirror: false` 使用非镜像灯区顺序 |
| `GET /api/light` | 后端当前情绪、group 人员与占比、pixels 与逐像素渐变轨迹、会话归属和设备状态 |
| `POST /api/light/layout` | `{ "mode": "cycle" 或 "spatial", "run_id": "当前会话" }`；无体验时可切换，有体验时仅控制者可切换；不改写配置文件 |
| `POST /api/light/session` | `action` 为 start/stop/pause/resume，`run_id` 为会话标识；冲突返回 409 |
| `GET /` | 静态页面，仅允许访问 `web/` 内的文件 |

推理返回 `ok`、`run_id`、`seq`、`frame`、`count`、`faces`、`primary`、`timing`、`server_ts`。坐标对应上传原图。镜像仅影响预览和框的显示，模型输入不镜像。服务只允许一个推理请求在途，其他请求返回 409。未就绪为 503，非法图片为 400，推理失败为 500。前端按运行标识和序号丢弃旧结果。推理响应增加 `light` 快照。未登记体验会话的推理请求仅返回识别结果，不控制实体灯带。后端仅接受当前会话、当前代次、递增序号且处理未超过 1.5 秒的结果；停止或暂停前已在途的结果不能重新点亮。

切换模型要求停止页面体验；切换会先加载并 warmup 新模型，成功后释放旧模型。重启服务默认回到 B0，或用 `--emotion-model` 指定启动模型。

会话 start 可重复调用，用作每两秒心跳；八秒无续租释放控制权。页面隐藏时 pause，恢复时 resume；只读页面刷新不会接管或停止其他会话。网页每 100 毫秒读取快照，在本地按后端轨迹补间，不重新计算情绪或叠加 400 毫秒延迟。减少动效偏好只影响网页，实体灯带仍平滑过渡。

USB 使用 Python 标准库，不增加 pyserial 依赖。串口查询使用 JSON，颜色用 Adalight，稳定后每秒补发。独立线程连接、限时写入、重连，不持有推理锁；连接前核对数量、实时接管状态。服务关闭后 WLED 按固件超时恢复原灯效。

## 测试与复现

测试前关闭其他体验页面，确保 B0 已就绪。测试按顺序运行，避免相互切换模型、争用摄像头或使服务进入 busy。

基础验证命令如下。`test_lighting_browser.js` 要求实体灯带已连接，`test_camera.js` 要求真实 USB 摄像头；其他测试可在关闭实体输出时运行。浏览器测试使用 Node.js 22+ 和系统 Chromium，无 npm 依赖。

```bash
.venv/bin/python scripts/smoke_vision.py
.venv/bin/python tests/test_api.py
.venv/bin/python tests/test_lighting.py
.venv/bin/python tests/test_group_mood.py
.venv/bin/python tests/test_multi_face.py
node tests/test_multi_face_browser.js
node tests/test_lighting_browser.js
node tests/test_ui.js
node tests/test_browser_contract.js
node tests/test_browser_contract.js --lifecycle
node tests/test_camera.js
```

| 入口 | 职责 |
| --- | --- |
| `tests/test_lighting.py` | 连续／重复确认、噪声、宽限、会话、迟到结果、渐变连续性、串口断开与重连 |
| `tests/test_lighting_browser.js` | 已连接实体控制器时验证双页面同步、控制权冲突、多人逐脸结果和停止释放 |
| `tests/test_group_mood.py` | 多人人数档位、混合占比、镜像、轨迹匹配、逐人宽限及像素协议 |
| `tests/test_multi_face.py` | 同一测试人像组成 3／6 人图，真实推理与增亮；生成明确标注的混合情绪界面 fixture |
| `tests/test_multi_face_browser.js` | 真实三人页面、灯光模式切换、混合配色与个人分数；先运行上一项生成 fixture |
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
