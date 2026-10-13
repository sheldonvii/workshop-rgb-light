#!/usr/bin/env python3
"""USB-only WLED-Controller-S2 30 -> 60 migration. See docs/prepare-wled.md."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

FLASH_SIZE = 0x400000
FS_OFFSET, FS_SIZE = 0x310000, 0xF0000
BLOCK_SIZE, DISK_VERSION = 4096, 0x00020000
ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dependency(name, version):
    require(importlib.metadata.version(name) == version,
            f"需要 {name}=={version}；安装命令见 docs/prepare-wled.md")


def partition(flash):
    """Validate the actual ESP partition table, including its MD5 and overlaps."""
    require(len(flash) == FLASH_SIZE, "仅支持已验证的 4 MiB Flash 布局")
    table = flash[0x8000:0x9000]
    entries = []
    for pos in range(0, len(table), 32):
        entry = table[pos:pos + 32]
        if entry[:2] == b"\xeb\xeb":
            require(hashlib.md5(table[:pos]).digest() == entry[16:], "分区表 MD5 不匹配")
            break
        require(entry[:2] == b"\xaa\x50", "分区表无效或缺少 MD5；拒绝写入")
        _, kind, subtype, offset, size, label, flags = struct.unpack("<HBBII16sI", entry)
        require(size > 0 and offset >= 0x9000 and offset + size <= FLASH_SIZE,
                "分区超出 Flash 边界")
        require(offset % BLOCK_SIZE == 0 and size % BLOCK_SIZE == 0, "分区未按擦除块对齐")
        require(not flags, "不支持带标记或加密的分区")
        entries.append((kind, subtype, offset, size, label.rstrip(b"\0")))
    else:
        raise ValueError("分区表缺少结束标记")
    ordered = sorted(entries, key=lambda e: e[2])
    require(all(a[2] + a[3] <= b[2] for a, b in zip(ordered, ordered[1:])), "分区重叠")
    filesystems = [e for e in entries if e[0] == 1 and e[1] in (0x82, 0x83)]
    require(len(filesystems) == 1, "需要唯一文件系统分区")
    fs = filesystems[0]
    require(fs[2:4] == (FS_OFFSET, FS_SIZE) and fs[4] in (b"spiffs", b"littlefs"),
            "文件系统布局与已验证控制器不同；拒绝套用偏移")
    return {"offset": fs[2], "size": fs[3], "label": fs[4].decode()}


def mount(image):
    from littlefs import LittleFS, UserContext
    require(len(image) == FS_SIZE, "LittleFS 镜像长度不正确")
    # Explicit mount prevents littlefs-python's constructor from auto-formatting
    # a corrupt/unsupported image. All edits stay in a private in-memory copy.
    fs = LittleFS(context=UserContext(buffer=bytearray(image)), mount=False,
                  block_size=BLOCK_SIZE, block_count=FS_SIZE // BLOCK_SIZE,
                  disk_version=DISK_VERSION)
    fs.mount()
    stat = fs.fs_stat()
    require((stat.disk_version, stat.block_size, stat.block_count) ==
            (DISK_VERSION, BLOCK_SIZE, FS_SIZE // BLOCK_SIZE), "LittleFS 格式与已验证版本不符")
    return fs


def files(fs):
    result = {}
    for directory, dirs, names in fs.walk("/"):
        result[directory.rstrip("/") + "/"] = None
        for name in names:
            path = directory.rstrip("/") + "/" + name
            with fs.open(path, "rb") as stream:
                result[path] = stream.read()
    return result


def config(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"cfg.json 含重复键 {key}")
            result[key] = value
        return result
    cfg = json.loads(data["/cfg.json"], object_pairs_hook=unique)
    led = cfg["hw"]["led"]
    require(len(led["ins"]) == 1, "仅支持一个 LED 输出")
    output = led["ins"][0]
    require(type(led["total"]) is int and type(output["len"]) is int and
            (led["total"], output["len"]) in ((30, 30), (60, 60)), "仅支持 30/30 或 60/60 的数量配置")
    require(output["start"] == 0 and output["pin"] == [38] and output["type"] == 22
            and output["order"] == 0 and output.get("skip", 0) == 0,
            "GPIO/类型/颜色顺序/起始位置不符合已验证配置")
    require(led["maxpwr"] == 850 and output["maxpwr"] == 850 and output["ledma"] == 55,
            "限流参数与已验证配置不同；不会自动覆盖")
    return cfg


def patch_image(image):
    """Return a verified image; preserve every other file byte for byte."""
    dependency("littlefs-python", "0.19.0")
    fs = mount(image)
    try:
        original = files(fs)
        before = config(original)
        if before["hw"]["led"]["total"] == 60:
            return image, False
        after = copy.deepcopy(before)
        after["hw"]["led"]["total"] = 60
        after["hw"]["led"]["ins"][0]["len"] = 60
        with fs.open("/cfg.json", "w", encoding="utf-8", newline="") as stream:
            json.dump(after, stream, separators=(",", ":"))
        fs.fs_mkconsistent()
    finally:
        fs.unmount()
    modified = bytes(fs.context.buffer)
    check = mount(modified)
    try:
        actual = files(check)
        require(config(actual) == after, "cfg.json 写入后不一致")
        actual["/cfg.json"] = original["/cfg.json"]
        require(actual == original, "检测到 cfg.json 以外的文件变化")
    finally:
        check.unmount()
    return modified, True


def esptool(port, action, *args, reboot=False):
    # Keep the loader running across commands; do not reset into WLED between
    # backup, write and readback. No automatic retry of a failed flash write.
    command = [sys.executable, "-m", "esptool", "--chip", "esp32s2", "--port", port,
               "--before", "no_reset", "--after", "hard_reset" if reboot else "no_reset_stub",
               action, *map(str, args)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", timeout=240)
    require(result.returncode == 0, f"esptool {action} 失败:\n{result.stdout[-4000:]}")
    return result.stdout


def boot_identity(port):
    output = esptool(port, "flash_id")
    require("ESP32-S2" in output and "Secure Download Mode" not in output,
            "设备不是普通下载模式的 ESP32-S2")
    require("Detected flash size: 4MB" in output, "设备 Flash 容量不是 4 MiB")
    match = re.search(r"MAC: ([0-9a-fA-F:]{17})", output)
    require(match is not None, "无法确认 ESP32-S2 MAC")
    return match.group(1).replace(":", "").lower()


def query(port):
    import serial
    with serial.Serial(port, 115200, timeout=0.2, write_timeout=1, exclusive=True) as stream:
        stream.rts = False
        stream.dtr = True
        stream.reset_input_buffer()
        stream.write(b'{"v":true}\n')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                info = json.loads(stream.readline())["info"]
                require(info["ver"] == "0.15.1", "固件不是已验证的 WLED 0.15.1")
                return {"count": info["leds"]["count"], "maxpwr": info["leds"]["maxpwr"],
                        "mac": info["mac"].replace(":", "").lower()}
            except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
                continue
    raise ValueError("WLED 未返回 USB 配置；检查端口、模式与串口占用")


def save(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(args):
    dependency("esptool", "4.9.0")
    dependency("littlefs-python", "0.19.0")
    dependency("pyserial", "3.5")
    import littlefs  # noqa: F401; fail before entering bootloader if native library is unavailable
    directory = args.backup_dir.resolve() / time.strftime("wled-60-%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    print(f"备份目录: {directory}", flush=True)
    metadata = {"status": "started", "port": args.port}
    def status(value):
        metadata["status"] = value
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    try:
        print("[1/5] 确认 ESP32-S2 和 4 MiB Flash；控制器需已进入 USB 下载模式", flush=True)
        metadata["mac"] = boot_identity(args.port)
        print("[2/5] 备份完整 Flash 并校验分区表", flush=True)
        original_path = directory / "flash-original.bin"
        esptool(args.port, "read_flash", "0", hex(FLASH_SIZE), original_path)
        flash = original_path.read_bytes()
        metadata["flash_sha256"] = digest(flash)
        metadata["partition"] = partition(flash)
        original = flash[FS_OFFSET:FS_OFFSET + FS_SIZE]
        save(directory / "filesystem-original.bin", original)
        metadata["filesystem_original_sha256"] = digest(original)
        print("[3/5] 只修改 cfg.json 两个数量字段，并逐文件核验", flush=True)
        prepared, changed = patch_image(original)
        if changed:
            prepared_path = directory / "filesystem-60.bin"
            save(prepared_path, prepared)
            metadata["filesystem_prepared_sha256"] = digest(prepared)
            status("prepared")
            # Persist recovery directions before any write; do not silently roll back.
            restore = (f"同一控制器 MAC {metadata['mac']} 进入 USB 下载模式后，核对原分区 SHA256\n"
                       f"{digest(original)}\n仅恢复 LittleFS（替换 PORT 为下载模式串口）:\n"
                       f"python -m esptool --chip esp32s2 --port PORT --before no_reset "
                       f"--after hard_reset write_flash {hex(FS_OFFSET)} \"{directory / 'filesystem-original.bin'}\"\n")
            (directory / "RESTORE.txt").write_text(restore, encoding="utf-8")
            require(sys.stdin.isatty(), "写入需要交互终端确认；镜像和备份已保留")
            answer = input(f"设备 {metadata['mac']}，30 -> 60，保留 850mA 限流。输入 60 写入: ")
            require(answer.strip() == "60", "已取消，未写入控制器")
            require(boot_identity(args.port) == metadata["mac"], "设备身份发生变化")
            # Read once more before writing so a cable/port change or intervening
            # configuration change cannot apply a stale backup to another device.
            current_path = directory / "flash-before-write.bin"
            esptool(args.port, "read_flash", "0", hex(FLASH_SIZE), current_path)
            require(current_path.read_bytes() == flash, "备份后 Flash 已变化；拒绝写入")
            status("writing")
            print("[4/5] 写回 LittleFS 并读取整片 Flash 比较", flush=True)
            esptool(args.port, "write_flash", hex(FS_OFFSET), prepared_path)
            readback_path = directory / "flash-readback.bin"
            esptool(args.port, "read_flash", "0", hex(FLASH_SIZE), readback_path)
            expected = flash[:FS_OFFSET] + prepared + flash[FS_OFFSET + FS_SIZE:]
            require(readback_path.read_bytes() == expected, "Flash 回读不一致；请保留备份并查看 RESTORE.txt")
            status("flash_verified")
        else:
            print("[4/5] 控制器已配置 60 颗，跳过写入", flush=True)
            status("already_60")
        print("[5/5] 重启并查询 WLED USB 配置", flush=True)
        esptool(args.port, "read_mac", reboot=True)
        last_error = None
        for attempt in range(12):
            time.sleep(1)
            try:
                info = query(args.app_port or args.port)
                require(info == {"count": 60, "maxpwr": 850, "mac": metadata["mac"]},
                        f"WLED 回读与目标不符: {info}")
                metadata["wled"] = info
                status("verified")
                print("PASS 控制器返回 60 颗、850mA；" + ("仅修改 LittleFS 的两个数量字段" if changed else "未写入 Flash"))
                return
            except (OSError, ValueError) as exc:
                last_error = exc
        raise ValueError(f"Flash 阶段已结束，WLED 回读尚未通过: {last_error}。"
                         "重插 USB 后使用 --check --port 应用串口复核；不要盲目重刷")
    except BaseException:
        metadata["failed_after"] = metadata["status"]
        status("incomplete")
        print(f"记录保留在 {directory}；失败后控制器可能仍在下载模式", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description="仅将 WLED-Controller-S2 内部配置从 30 颗改为 60 颗")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="备份、修改、写回并验证；先手动进入下载模式")
    mode.add_argument("--check", action="store_true", help="仅查询正常 WLED USB 串口的数量")
    mode.add_argument("--patch-image", type=Path, help="离线修改 LittleFS 镜像，不连接硬件")
    parser.add_argument("--output", type=Path, help="离线输出路径，必须为新文件")
    parser.add_argument("--port", help="明确指定 USB 串口，如 /dev/ttyACM0 或 COM3")
    parser.add_argument("--app-port", help="重启后应用串口路径，默认与 --port 相同")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "controller-backups")
    args = parser.parse_args()
    if args.patch_image:
        if not args.output:
            parser.error("--patch-image 必须提供 --output")
    elif not args.port:
        parser.error("--prepare / --check 必须明确指定 --port")
    try:
        if args.patch_image:
            require(not args.output.exists(), "输出文件已存在，拒绝覆盖")
            image, changed = patch_image(args.patch_image.read_bytes())
            save(args.output, image)
            print(f"PASS {'30 -> 60' if changed else '已是 60'}；SHA256 {digest(image)}")
        elif args.check:
            dependency("pyserial", "3.5")
            info = query(args.port)
            print(json.dumps(info))
            require(info["count"] == 60 and info["maxpwr"] == 850, "尚未达到 60 颗、850mA 配置")
            print("PASS WLED 返回 60 颗、850mA（配置数量，不是实物检测）")
        else:
            if os.name != "nt":
                os.umask(0o077)
            prepare(args)
        return 0
    except KeyboardInterrupt:
        print("已中断；若处于写入阶段，请按备份目录 RESTORE.txt 恢复", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
