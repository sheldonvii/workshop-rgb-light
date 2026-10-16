# Agent 接手指南

本文为自动化 Agent 与接手开发者说明项目结构、设备边界与提交规则。当前仓库在 SpacemiT K3 上运行人脸表情识别，并通过 USB 驱动 60 颗 WLED 灯带。

## 接手检查

1. 确认当前分支与工作树状态：

   ```bash
   pwd
   git branch --show-current
   git status --short --branch
   ```

2. 运行不占用硬件的自动化校验：

   ```bash
   ./run_test.sh --unit
   ./run_test.sh --no-hardware
   ```

   测试依赖 `.venv` 中的环境。若有失败项，先记录终端输出并核对报错文件，区分环境依赖问题与代码逻辑问题。

3. 根据任务查阅对应文档。基础安装与运行见 [README.md](README.md)，模块与调用关系见 [docs/code-guide.md](docs/code-guide.md)，接口协议见 [docs/development.md](docs/development.md)，控制器配置见 [docs/controller.md](docs/controller.md) 与 [docs/prepare-wled.md](docs/prepare-wled.md)。

## 职责划分

修改代码前按模块定位文件：HTTP 路由与会话管理在 `server/app.py`，模型注册与预处理在 `server/emotion_models.py`，人脸追踪与空间分区在 `server/group_mood.py`，灯光状态机与串口通信在 `server/lighting.py`，前端页面交互与灯态渲染在 `web/`。

单人与多人均统一走 `GroupMood` 和 `Lighting` 链路。新增功能时复用现有数据结构，前端仅负责同步和补间显示后端计算出的灯光状态，不在前端重复计算情绪或分区。

## 设备与硬件边界

自动化测试默认不打开摄像头与串口。需要连接实体硬件验证时，先确认板端没有其他服务占用设备，再启动 `./run.sh`。如需脱机检查灯珠接线与发光，执行：

```bash
./run_test.sh --pixels --seconds 15
```

控制器采用 USB CDC 串口以 Adalight 二进制协议通信，日常无需修改控制器参数。出厂 30 颗控制器的初始化仅按 [docs/prepare-wled.md](docs/prepare-wled.md) 执行。

## 分支与提交规则

开发在当前工作分支进行，改动聚焦于目标任务。模型权重、离线安装包、控制器固件备份及测试输出文件均已排除在 Git 之外，不要将临时产物提交到仓库。

未获明确授权时，Agent 不执行 commit、push、切分支或重置工作树。完成修改后提供改动文件清单、验证命令与结果；收到提交要求时，先整理提交信息并等待确认。
