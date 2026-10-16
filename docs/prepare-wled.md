# 通过 USB 将 WLED 控制器配置为 60 颗

`scripts/prepare_wled_60.py` 用于首次准备控制器。它沿用 esptool 与 littlefs-python 工具链，只把控制器 `/cfg.json` 中的 `hw.led.total`、`hw.led.ins[0].len` 从 30 改为 60。项目的 `configs/light.json` 固定为 60，脚本不读取或修改它，也不接入 `run.sh`。

## 适用范围

适用于本 Demo 已验证的 WLED-Controller-S2、ESP32-S2、4 MiB Flash、WLED 0.15.1。原配置为单路输出、GPIO38、类型 22、颜色顺序 0、850mA 限流、55mA/颗。脚本保留这些参数；遇到不同配置会退出，不能用于任意 WLED 控制板。

控制器通过 USB 数据线连接 K3，整个过程不连接 WLED Wi-Fi、不使用 HTTP。应用固件、分区表、NVS、其他配置文件和预设保持原样。原预设的 30 颗分段终点也保留；Demo 的 Adalight 实时帧输出 60 个像素。

## 首次操作

1. 停止 Demo 服务和串口终端，按 [控制器说明](controller.md#连接与权限) 完成 `dialout` 权限。使用普通用户运行脚本；进入下载模式前后都要能读写对应串口。
2. 在项目目录创建独立维护环境。K3 使用系统 Python 3.14，复用系统基础库；littlefs-python 在 RISC-V 上需要本机 C 编译器及 Python 头文件。缺少时由系统管理员安装 `build-essential`、`python3-dev` 后继续。

   ```bash
   ~/.local/bin/uv venv .venv-controller --python /usr/bin/python3.14 --system-site-packages
   .venv-controller/bin/python -m pip install \
     --index-url https://pypi.org/simple \
     esptool==4.9.0 pyserial==3.5 littlefs-python==0.19.0 bitstring==3.1.9
   ```

   使用系统 pip 可以复用 Bianbu 已安装的 cryptography、PyYAML 等本机库。此处联网只用于下载主机工具，与连接控制器 Wi-Fi 无关。Windows 也可安装相同依赖，用 `python` 执行脚本，并将串口换成实际 `COM` 编号。
3. 控制器正常启动时先查询。下面的固定路径来自当前设备，更换设备后以 `ls -l /dev/serial/by-id/` 为准。

   ```bash
   .venv-controller/bin/python scripts/prepare_wled_60.py --check \
     --port /dev/serial/by-id/usb-WEMOS.CC_LOLIN-S2-MINI_0-if00
   ```

   返回 `PASS` 表示已经是 60 颗、850mA，无需执行迁移。返回 30 颗时继续下一步；端口繁忙、权限错误或没有回应需要先解决，不能据此判断数量。
4. 让控制器进入 USB 下载模式。通常按住 BOOT/IO0 键重新接通 USB，再松开按键；具体按键操作按板卡标识确认。此时设备会重新枚举，执行 `ls -l /dev/serial/by-id/ /dev/ttyACM*` 确认下载模式串口。下例假定它是 `/dev/ttyACM0`，不要直接套用原应用串口名称。

   ```bash
   .venv-controller/bin/python scripts/prepare_wled_60.py --prepare \
     --port /dev/ttyACM0 \
     --app-port /dev/serial/by-id/usb-WEMOS.CC_LOLIN-S2-MINI_0-if00
   ```

   脚本备份、校验并生成镜像后，会显示设备 MAC 和 30 → 60 的改动。输入 `60` 才写回。保留 USB 连接，直到显示 `PASS`；写回后脚本读取整片 Flash，比对仅文件系统分区发生预期变化，再重启并查询 WLED 的数量、限流和 MAC。

成功后正常启动 `./run.sh`。数量查询来自控制器配置，不检测实际灯珠数量；首次使用还应观察整条灯带是否亮起。

## 备份与失败恢复

备份在 `controller-backups/wled-60-时间戳/`，包含原始完整 Flash、原始 LittleFS、待写入镜像、写入前快照、写入后快照、SHA-256 和阶段记录。文件可能包含设备网络配置，目录已排除 Git。请保留同一控制器的备份，不能把其他设备的整片 Flash 写进去。

脚本从备份中的分区表校验实际布局及 MD5，仅支持偏移 `0x310000`、长度 `0xF0000` 的既有布局。LittleFS 磁盘版本固定为 `0x00020000`；遇到损坏或不兼容镜像时拒绝处理，不自动格式化。

| 结果 | 处理 |
| --- | --- |
| 备份或镜像校验失败 | 尚未写入；核对工具、控制器与原配置。退出下载模式后可恢复正常启动 |
| 写入或 Flash 回读失败 | 查看备份目录 `RESTORE.txt`，确认同一设备 MAC 和原镜像 SHA-256；按该文件仅恢复原 LittleFS 分区 |
| Flash 已校验，但 WLED 回读失败 | 可能仍在下载模式，或应用串口名称已变化。重新插拔 USB，使用实际应用端口执行 `--check`，通过前不算完成 |

`metadata.json` 记录已完成阶段。取消或失败返回非零退出码，不会自动重试刷写或恢复。恢复到原始 30 颗配置后，Demo 的 60 颗校验会拒绝输出，需要排查并重新完成迁移。

## 离线验证

准备脚本的 Flash 迁移不包含在自动化测试流中，首次使用新控制器需手动执行 `--prepare`。

对已有 LittleFS 备份可单独生成镜像，输出必须为新文件：

```bash
.venv-controller/bin/python scripts/prepare_wled_60.py \
  --patch-image controller-backups/某次备份/filesystem-original.bin \
  --output controller-backups/某次备份/filesystem-60-offline.bin
```

历史 30 颗备份的离线处理已复现出与此前成功镜像相同的 SHA-256。可通过该离线方式验证镜像生成的完整性。
