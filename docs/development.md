# 开发说明

本文描述当前实现。源码、配置和可运行测试是维护对象；测试输出、模型权重和本机环境不提交。

## 环境与路径

运行平台为 SpacemiT K3、Bianbu 4.0.3 riscv64，Python 3.14.4。Python 依赖和下游安装索引固定在 `requirements.txt`。模型 URL、版本和 SHA-256 统一维护在 `server/emotion_models.py`。

`spacemit_vision` 锁定 0.1.4。OpenCV 分两层安装：

| 依赖 | 安装方式 | 核对 |
| --- | --- | --- |
| SpacemiT OpenCV 系统库 | `sudo apt install opencv-spacemit=5.0.0-1bb1`，安装到 `/opt/opencv-spacemit` | `dpkg-query -W opencv-spacemit` 应显示 5.0.0-1bb1 |
| Python cv2 包 | `uv pip install -r requirements.txt`，锁定 `opencv-python==5.0.0.93` | 在项目虚拟环境执行 `import cv2`，版本为 5.0.0 |

两者都需要，安装 `requirements.txt` 不会替代系统库安装。不要用通用 OpenCV 4.x 库或将旧库改名来满足 Vision 0.1.4 的依赖。服务先导入 `cv2` 再导入 Vision；完整命令与导入检查见 [安装与运行](../README.md#安装与运行)。

上述系统包来自 Bianbu 4 的 SpacemiT 软件源；当前板端候选版本为 5.0.0-1bb1。`apt-cache policy opencv-spacemit` 可检查来源和可用版本；若只看到 4.14，先核对系统版本及 Bianbu 4 软件源，不在此项目中自动切换系统源。

Python 3.14 对应的 OpenCV wheel 必须包含 `cv2/config-3.14.py`。安装只使用 SpacemiT 下游索引，避免混源解析成需要编译的源码包。首次重建需要网络。先用 uv 创建 `.venv`，再安装依赖；缓存位置可沿用 uv 默认值。项目内 `.uv-cache` 仅是可选缓存，不是启动前提，也不随仓库分发。

项目可以放在其他目录。`run.sh` 和服务入口会将工作目录设为项目根目录，YAML 中的模型、标签和测试图均为相对路径。浏览器测试从自身位置推导根目录。浏览器需要中文字体，如系统 `Noto Sans CJK SC`。

## 数据链路与模型

摄像头提交 JPEG → YOLOv5-Face 检测 → 每张人脸原图裁剪 → 逐人分类与确认 → 汇总占比 → 分区渐变输出。

| 模块 | 当前行为 |
| --- | --- |
| 人脸检测 | `yolov5n-face_cut.q.onnx`，Vision 0.1.4，SpaceMITExecutionProvider |
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
| 换色间隔 | 同一张脸的已确认情绪切换至少间隔 600 毫秒；人数、分区、暂停和停止不受此限制 |
| 过渡 | 后端生成 400 毫秒渐变，USB 最多 30 帧/秒，网页显示同一轨迹 |
| 不可靠结果 | 每张脸独立保留旧色 900 毫秒，失效者退出占比；全部失效后回待机 |
| 亮度 | 单一确认情绪按估算电流对齐：单人 220mA；2～3 人全员一致 450mA；4 人及以上一致 700mA；不同表情仍为 60% |

单帧不能确认；重复、乱序输入不计票。某张脸不再出现或超过 1 秒的采样间断会清除其候选证据。完全没有新结果时，从最后采样后的 1 秒开始计入 900 毫秒宽限；后端独立线程定时检查。正常停止、切到后台、摄像头结束会释放会话，清除轨迹与情绪证据并渐变回待机。若编码回调挂起时相机恰好断开，仍沿用已有行为，停止重开或刷新页面恢复；本轮不新增自动恢复。

多人一致增亮要求每个人当前可见且稳定，同色但有人待确认不增亮。不同表情仅按画面左右分区并在边界渐变，不保留模式选择。沿线颜色占比和背景按确认人数汇总，各人按钮和人脸框可打开对应模型分数。未确认者单独提示，不计入颜色占比。历史按确认的情绪人数构成去重，记录保持区间，不记录过渡动画帧；相同构成交换位置不增加历史。

300 毫秒是确认门槛，端到端响应还受采样和推理耗时影响。`server/group_mood.py` 管理短期轨迹、逐人滤波和群体状态，`server/lighting.py` 管理会话、逐像素渐变和 USB 输出。网页只同步状态。串口权限、接线和配置见 [控制器说明](controller.md)。

## 接口

| 接口 | 请求与返回 |
| --- | --- |
| `GET /api/health` | 服务状态、执行引擎、模型选项、模型专属标签顺序、中文标签、统一色板、确认时序及加载时间 |
| `POST /api/model` | JSON `{ "model": "hse-b0" }`；可选 `hse-b0`、`hse-b2`、`official` |
| `POST /api/infer` | 请求体为 JPEG，最多 8 MiB；`X-Run-Id` 回显；`X-Seq` 解析一次并回显整数或 null；`X-Mirror: false` 使用非镜像灯区顺序 |
| `GET /api/light` | 后端当前情绪、group 人员与占比、pixels 与逐像素渐变轨迹、会话归属和设备状态 |
| `POST /api/light/session` | `action` 为 start/stop/pause/resume，`run_id` 为会话标识；冲突返回 409 |
| `GET /` | 静态页面，仅允许访问 `web/` 内的文件 |

推理返回 `ok`、`run_id`、`seq`、`frame`、`count`、`faces`、`primary`、`timing`、`server_ts`。坐标对应上传原图。镜像仅影响预览和框的显示，模型输入不镜像。服务只允许一个推理请求在途，其他请求返回 409。未就绪为 503，非法图片为 400，推理失败为 500。前端按运行标识和序号丢弃旧结果。推理响应增加 `light` 快照。未登记体验会话的推理请求仅返回识别结果，不控制实体灯带。后端仅接受当前会话、当前代次、递增序号且处理未超过 1.5 秒的结果；停止或暂停前已在途的结果不能重新点亮。

切换模型要求停止页面体验；切换会先加载并 warmup 新模型，成功后释放旧模型。重启服务默认回到 B0，或用 `--emotion-model` 指定启动模型。

会话 start 可重复调用，用作每两秒心跳；八秒无续租释放控制权。页面隐藏时 pause，恢复时 resume；只读页面刷新不会接管或停止其他会话。网页每 100 毫秒读取快照，在本地按后端轨迹补间，不重新计算情绪或叠加 400 毫秒延迟。减少动效偏好只影响网页，实体灯带仍平滑过渡。

USB 使用 Python 标准库，不增加 pyserial 依赖。串口查询使用 JSON，颜色用 Adalight，稳定后每秒补发。独立线程连接、限时写入、重连，不持有推理锁；连接前核对数量、实时接管状态。服务关闭后 WLED 按固件超时恢复原灯效。


### 多人字段与时效

下表是 HTTP JSON 的使用说明，维护者也应在更改字段时同步修改边界校验和完整夹具。`MoodFilter` 每人复用；单人同样走 `GroupMood`，生产代码不补假人脸或默认 1×1 图像。

| 字段 | 含义与单位 |
| --- | --- |
| `faces[].emotion` / `primary` | 每脸本帧分类或 null；仅一脸时 primary 指向该脸分类，多脸和无人为 null |
| `faces[].track_id` | 仅在被当前有效会话接纳的帧上添加，几何短期编号，不是身份；观察者或缺序号推理不保证有该字段 |
| `group.people[]` | 当前保留轨迹，含 id、原图像素 box、归一化横坐标 position、visible、raw、current 和逐脸 status |
| `people[].raw` / `current` | 最近一次本帧原始分类 / 经时序确认的情绪；raw 可不确定或为 null，current 过期后为 null |
| `group.visible_count` | 最近一次接纳检测中可见的人数，非即时相机在线检测 |
| `group.confirmed_count` / `composition[]` | 仍有 current 的人数及各颜色 label、label_zh、count；短暂离开者在宽限内仍可计入 |
| `group.agreed` / `key` | 所有轨迹可见、稳定且同色才为 true；key 按类别人数构成去重，不包含位置 |
| `sample_ms` / `revision` | 服务器单调时钟采样毫秒 / 输出目标变化编号；sample_ms 不是新推理发生的时间，跨进程用 instance 区分 |
| `pixels` / `power` | 过渡中的未缩放 RGB 数组 / RGB 缩放系数；实际 USB 通道为 round(channel × power) |
| `transition` | from/to 与逐像素数组、from_power/to_power、elapsed_ms/duration_ms；前端沿此轨迹补间 |
| `current_target_ma` | 同色 220/450/700mA 整套估算目标，混色和待机为 null；不是传感器读数 |
| `device.status` / `last_write_ms` | 控制器通信状态 / 最近写成功的单调时钟毫秒；均不能证明灯带接线正确或实际发光 |

推理停更但后端仍可访问时，逐脸 current 会按时过期、灯回待机，但 visible_count、raw 和可见轨迹保留最近检测样本。页面不是实时人数监视器；本轮不增加轨迹失效清理或后端失联显示恢复框架。

请求示例先登记会话，再用相同 run_id 提交递增序号。

```bash
curl -s http://127.0.0.1:8321/api/light/session -H 'Content-Type: application/json' \
  -d '{"action":"start","run_id":"demo-dev"}'
curl -s http://127.0.0.1:8321/api/infer -H 'X-Run-Id: demo-dev' -H 'X-Seq: 1' \
  -H 'X-Mirror: true' --data-binary @web/test-frames/multi.jpg
curl -s http://127.0.0.1:8321/api/light/session -H 'Content-Type: application/json' \
  -d '{"action":"stop","run_id":"demo-dev"}'
```

缺少 `X-Seq` 时推理仍执行，响应 seq=null，不创建灯光票据。空串、非整数、负数返回 400 bad_sequence；合法但重复或倒序的帧可返回识别结果，灯光忽略它。错误码还包括 400 bad_image/bad_request/bad_model、409 busy/light_busy、503 not_ready、500 infer_failed/model_failed。只有图像解码错误属于 bad_image，模型内部 ValueError 会记日志并返回 infer_failed；页面按固定错误码判断控制权，不读取中文提示参与逻辑。

### 文件职责

| 文件 | 维护范围 |
| --- | --- |
| `server/app.py` | HTTP 边界、图像解码、逐脸检测裁剪分类、结果组装和模型生命周期 |
| `server/emotion_models.py` | 模型来源、校验、标签顺序、不确定参数与统一分类器接口 |
| `server/group_mood.py` | 几何关联、逐脸滤波实例、群体组成和空间分区 |
| `server/lighting.py` | 色板、时序、会话票据、同色电流策略、像素过渡和 USB 线程 |
| `web/app.js` | 相机与推理调度、会话协调、响应边界校验、模型设置与页面状态 |
| `web/light-controller.js` | 灯态边界校验、快照顺序、通信队列、轮询和心跳；不访问页面状态 |
| `web/group-view.js` / `web/weather.js` | 人员选择、群体背景与曲线 / 抽屉外观、分数、灯光绘制和时序呈现 |
| `scripts/prepare_wled_60.py` | 独立首次准备，保留所在目录和原有刷写保护；不进入日常启动 |

同色估算目标和混色 60% 策略本轮保持原状，120mA 预留已计入估算。异色情绪按组人数分配电流留作后续开发，不承诺等电流时肉眼等亮。

## 测试与复现

统一入口为 `./run_test.sh`。测试前在其他网页停止体验，避免摄像头、模型切换和灯光控制权冲突。完整检查需要当前 Python 环境、四个模型文件、Node.js 22+、系统 Chromium、真实 USB 摄像头及控制器；无 npm 依赖，不自动下载模型或安装软件，不需要 sudo。

```bash
./run_test.sh                     # 全套自动检查，含摄像头和 USB 控制器
./run_test.sh --unit              # 离线灯光规则检查，不访问硬件或启动服务
./run_test.sh --no-hardware       # 明确跳过摄像头和控制器检查
./run_test.sh --soak 1800         # 全套检查后增加 30 分钟摄像头压力测试
```

`--no-hardware --soak 15` 可短程验证图片推理压力，不代表通过长期验收，也不验证摄像头。`--no-hardware` 跳过硬件要求，不能关闭已有服务的实体输出；若控制器在线，图片推理测试仍可能改变灯色。需要完全关闭输出时，按控制器文档设置 `enabled=false` 并重启服务。

调度器按依赖顺序串行运行。服务不存在时从当前项目临时启动，结束后只停止自己启动的服务；已有服务必须是当前项目的唯一进程，测试后恢复原模型并释放测试会话。若其他网页正在体验，会报错并要求先停止，不抢占会话。不额外加载第二套 NPU 引擎。Ctrl-C 会保留已完成报告并清理本轮子进程。

默认地址为 `http://127.0.0.1:8321`，`BAROMETER_URL` 可指定板端 localhost 的其他端口。`CHROMIUM` 指定浏览器可执行文件，默认 `/usr/lib/chromium/chromium`；`TEST_PYTHON` 指定 Python，可用于不同虚拟环境。完整检查应在 K3 本机执行，浏览器依赖板端设备和 `/proc`。

| 测试文件 | 职责与用例 |
| --- | --- |
| `tests/run_suite.py` | 依赖、服务、超时和报告；模型接口、3/6 脸真实拼图推理；独立生成合成 UI 快照；显式手动像素图案 |
| `tests/test_lighting.py` | 逐脸滤波、会话、轨迹、同色电流与混色；真实 connect 配合 PTY、60 颗完整协议帧、模拟断开重连、模型错误分类 |
| `tests/test_ui.js` | CDP 共用支持及 visual/group/shared/contracts/lifecycle/camera/soak 命名用例，各自输出报告和截图；生命周期用例含绘制异常与通信错误分离检查 |

运行结果写入 `tests/output/latest/`。`summary.txt` 汇总 PASS、FAIL、SKIP，`report.json` 包含各环节耗时、错误、排查提示和日志路径；对应阶段日志保留断言与堆栈，截图和子报告按职责存放。每次运行只清理这一生成目录，拒绝并发执行；合成页面夹具在模型接口测试之前独立生成，不因模型测试失败被跳过，也不复用上次 fixture。退出码 0 表示本轮已执行检查通过，1 表示检查失败，2 表示入口参数或环境错误，130 表示中断。SKIP 只表示未执行，不代表通过。需要保留证据时，下一次运行前复制该目录。

默认检查 USB 控制器通信、配置灯珠数，并通过 API 推理发送灯光状态；三线灯带没有发光反馈，软件通过不代表实际灯珠颜色或接线已验证。需要目视检查寻址时，先停止 Demo，再执行 `./run_test.sh --pixels --seconds 15`，最后重新 `./run.sh`。电流档位的纯逻辑验证已进入单元测试；控制器电流回读属于估算值，不能替代实测电流和现场观感。

`?frames=test` 显式启用隔离图片源，页面常驻提示测试图片，请求仍执行真实推理。共用图片保留在 `web/test-frames/`，不能当中间产物删除。3/6 人测试使用同一人像的重复拼图，混合情绪 fixture 为合成 UI 输入，两者都不构成真人群体准确率评测。
## 故障处理

模型缺失或校验失败时运行 `scripts/download_models.py --verify-only` 查看具体文件。加载失败先检查 `/api/health` 和服务终端输出，再核对 Python、Vision 与系统 OpenCV 版本。摄像头失败检查 USB 连接、设备占用和浏览器权限；外部 HTTP 地址通常不满足摄像头安全上下文，默认使用板端 localhost。

出现 `tcm buffer acquire failed` 时，用 `spacemit-tcm-smi` 检查占用及进程归属。不要直接全局清理，其他应用可能共用 TCM。统一测试通过同一个服务验证检测和分类，不额外加载第二套 NPU 引擎。

## 开发格式检查

格式工具只在开发机使用，不加入运行依赖。Python 使用 Ruff 0.14.0 的 `check --select F,I` 和 `format`；自有 JS、HTML、CSS 使用 Prettier 3.6.2，跳过第三方 lucide.min.js。源码以 UTF-8、LF 保存；注释以简体中文解释协议、单位、锁和时序约束。
