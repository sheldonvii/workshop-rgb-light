# 代码导读

本文面向第一次接手本 Demo 的开发者。它解释代码之间的调用关系和修改入口，不重复安装步骤、硬件接线、完整接口字段和控制器恢复流程。相关内容分别见 [README.md](../README.md)、[development.md](development.md)、[controller.md](controller.md) 和 [prepare-wled.md](prepare-wled.md)。

## 先看哪里

| 目的 | 先读的文件 | 需要理解的内容 |
| --- | --- | --- |
| 启动服务 | `run.sh`、`server/app.py` | 服务入口、模型异步加载、HTTP 路由 |
| 修改识别 | `server/emotion_models.py`、`configs/emotion.yaml` | 模型注册、预处理、标签和模型切换 |
| 修改多人逻辑 | `server/group_mood.py` | 人脸短期跟踪、情绪确认、群体汇总和灯带分区 |
| 修改灯光输出 | `server/lighting.py`、`configs/light.json` | 颜色、过渡、电流对齐、USB Adalight 帧 |
| 修改页面 | `web/app.js`、`web/light-controller.js`、`web/group-view.js` | 摄像头状态、推理提交、灯态同步和多人显示 |
| 验证改动 | `run_test.sh`、`tests/run_suite.py`、`tests/test_lighting.py`、`tests/test_ui.js` | 单元、接口、灯光协议和页面检查 |

## 一次推理如何变成灯光

```text
浏览器抓取 JPEG
  -> POST /api/infer
  -> server/app.py 解码并调用人脸检测
  -> emotion_models.py 逐脸裁剪并分类
  -> group_mood.py 关联短期轨迹并确认情绪
  -> lighting.py 生成 60 颗灯珠的逐像素颜色
  -> USBStrip 发送 Adalight 二进制帧
  -> web/light-controller.js 轮询同一份灯态
  -> web/group-view.js 和 app.js 绘制页面
```

`server/app.py` 只负责服务编排。它限制同一时间最多一个完整推理请求，检查会话编号与序号，并将识别结果交给灯光状态对象。这里适合修改路由、请求校验和模型切换流程，不适合直接写灯光算法。

`server/emotion_models.py` 负责模型注册和推理适配。人脸检测由 K3 的 Vision 引擎完成，HSEmotion 模型走 ONNX Runtime，官方模型走 Vision 原生接口。新增模型时需要同时检查模型文件信息、标签顺序、预处理和 `health` 返回内容；模型下载与校验入口在 `scripts/download_models.py`。

`server/group_mood.py` 负责群体状态计算与空间映射。`GroupMood` 为每张人脸维护短期追踪轨迹，过滤不确定结果，输出 `people` 状态、`composition` 情绪统计以及 `agreed` 一致性标志；`zone_pixels()` 根据人脸在画面的水平方位将情绪投影到灯带区间，并在交界处执行混色。单人与多人均统一复用该链路。

`server/lighting.py` 封装灯光状态机与硬件通信。`Lighting` 维护当前像素、过渡轨迹与控制器在线状态，`current_scale()` 依据电流标定参数动态缩放 RGB 强度，`USBStrip` 负责串口握手与 Adalight 二进制发帧。上层业务通过 `Lighting` 实例提交目标颜色与过渡，底层串口交互由模块内部独立线程驱动。

## 前端与后端的关系

`web/app.js` 管理页面交互生命周期与摄像头推流，控制请求提交节奏并丢弃过期响应。`web/light-controller.js` 负责校验 `/api/light` 快照的完整性，并沿后端提供的逐像素轨迹执行平滑补间；`web/group-view.js` 负责渲染多人识别框、情绪占比与分区指示。

后端返回的 `faces`、`group`、`pixels` 及 `transition` 是唯一状态源，前端仅做呈现与插值，不包含独立的情绪或分区决策逻辑。修改接口字段时，需同步核对 `server/app.py`、`web/light-controller.js` 的校验逻辑以及 `tests/test_ui.js` 中的相关断言。

## 常见修改入口

| 需求 | 修改位置 | 同步核对 |
| --- | --- | --- |
| 调整情绪色板 | `server/lighting.py` 的 `PALETTE` | 前端色板读取逻辑、API 测试 |
| 调整确认与切换平滑度 | `server/lighting.py` 的 `TIMING` | `docs/development.md`、灯光时序测试 |
| 调整多人空间分区算法 | `server/group_mood.py` 的 `zone_pixels()` | `tests/test_lighting.py` 分区断言、页面显示 |
| 调整电流自适应参数 | `configs/light.json` 的 `current_alignment` | 电流估算测试、控制器限流配置 |
| 引入新情绪模型 | `server/emotion_models.py` 与 `configs/` | 模型校验、`/api/health`、切换测试 |
| 调整前端视图状态 | `web/app.js` 或对应视图组件 | `tests/test_ui.js` 页面断言 |

修改灯光逻辑时，推荐先在 `tests/test_lighting.py` 中更新无硬件断言，再通过 `./run_test.sh --unit` 快速回归验证。

## 测试入口与修改建议

日常改动推荐先通过 `./run_test.sh --unit` 运行 Python 逻辑与状态机断言；涉及接口变更时，使用 `./run_test.sh --no-hardware` 跑通端到端测试帧验证。当需要排查实体 60 颗灯珠的接线、通道顺序与渐变时，执行 `./run_test.sh --pixels --seconds 15` 输出测试图案。

修改控制器配置需运行 `scripts/prepare_wled_60.py`，日常服务启动不会改写控制器固件。修改灯光通信前，先核对 `server/lighting.py` 中的 Adalight 帧封装与控制器协议文档。
