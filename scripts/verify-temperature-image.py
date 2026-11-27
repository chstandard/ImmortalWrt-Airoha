#!/usr/bin/env python3
"""逐个检查最终 sysupgrade(.itb/.bin) 镜像里的整条温度链路 + 关键软件包。

参考 Kahen/PonWrt-CI-UBI：从 FIT/tar 镜像里取出真实 squashfs，检查：
  - /sbin/tempinfo 存在且可执行（autocore 已为 airoha 安装）
  - luci.getTempInfo 读 /sbin/tempinfo（RPC 接入）
  - 概览页 10_system.js 有温度调用与"Unavailable"兜底
  - ACL 只读放行 getTempInfo
  - 包数据库里有 autocore / luci-app-autoreboot / luci-theme-footstrap

任一项缺失即非零退出，阻止发布缺链路的固件。
"""
import json
import pathlib
import struct
import subprocess
import sys
import tarfile
import tempfile

TEMP_PATHS = [
    "sbin/tempinfo",
    "usr/share/rpcd/ucode/luci",
    "usr/share/rpcd/acl.d/luci-mod-status-index.json",
    "www/luci-static/resources/view/status/include/10_system.js",
]
REQUIRED_PACKAGES = ["autocore", "luci-app-autoreboot", "luci-theme-footstrap"]


def fail(msg):
    print("::error::" + msg, file=sys.stderr)
    sys.exit(1)


def fdtget(image, node, prop, kind="x"):
    return subprocess.check_output(
        ["fdtget", "-t", kind, str(image), node, prop], text=True
    ).strip()


def rootfs_from_fit(image):
    nodes = subprocess.check_output(
        ["fdtget", "-l", str(image), "/images"], text=True
    ).splitlines()
    candidates = []
    for name in nodes:
        node = "/images/" + name
        if fdtget(image, node, "type", "s") == "filesystem":
            candidates.append(node)
    if len(candidates) != 1:
        raise ValueError(f"期望 FIT 内只有一个 rootfs，实际 {candidates}")
    node = candidates[0]
    props = subprocess.check_output(["fdtget", "-p", str(image), node], text=True).splitlines()
    size = int(fdtget(image, node, "data-size"), 16)
    if "data-position" in props:
        offset = int(fdtget(image, node, "data-position"), 16)
    elif "data-offset" in props:
        with image.open("rb") as stream:
            _, total_size = struct.unpack(">II", stream.read(8))
        offset = (total_size + 3) // 4 * 4 + int(fdtget(image, node, "data-offset"), 16)
    else:
        raise ValueError("FIT rootfs 不是 external data，无法定位")
    if size <= 0 or offset < 40 or offset + size > image.stat().st_size:
        raise ValueError("FIT rootfs 偏移/大小越界")
    with image.open("rb") as stream:
        stream.seek(offset)
        return stream.read(size)


def rootfs_from_tar(image):
    with tarfile.open(image, "r:*") as archive:
        members = [x for x in archive.getmembers() if x.isfile() and x.name.endswith("/root")]
        if len(members) != 1:
            raise ValueError("sysupgrade tar 内期望只有一个 root 文件")
        with archive.extractfile(members[0]) as stream:
            return stream.read()


def parse_packages(text):
    packages, name = {}, None
    for line in text.splitlines():
        if line.startswith("P:"):
            name = line[2:]
        elif line.startswith("Package: "):
            name = line[9:]
        elif name and line.startswith("V:"):
            packages[name] = line[2:]
        elif name and line.startswith("Version: "):
            packages[name] = line[9:]
    return packages


def validate_temperature(directory):
    errors = []
    tempinfo = directory / "sbin/tempinfo"
    if not tempinfo.is_file():
        errors.append("缺 /sbin/tempinfo（autocore 未为 airoha 安装）")
    elif not tempinfo.stat().st_mode & 0o111:
        errors.append("/sbin/tempinfo 不可执行")

    rpc_path = directory / "usr/share/rpcd/ucode/luci"
    page_path = directory / "www/luci-static/resources/view/status/include/10_system.js"
    acl_path = directory / "usr/share/rpcd/acl.d/luci-mod-status-index.json"
    contents = {}
    for name, path in [("rpc", rpc_path), ("page", page_path), ("acl", acl_path)]:
        if not path.is_file():
            errors.append(f"缺温度链路文件：{path}")
        else:
            contents[name] = path.read_bytes()

    if "rpc" in contents:
        rpc = contents["rpc"]
        if b"getTempInfo" not in rpc or b"/sbin/tempinfo" not in rpc:
            errors.append("luci.getTempInfo 未接到 /sbin/tempinfo")
    if "page" in contents:
        page = contents["page"]
        if b"getTempInfo" not in page or b"Temperature" not in page:
            errors.append("概览页缺温度调用/字段")
        if b"Unavailable" not in page:
            errors.append("概览页缺无读数兜底（Unavailable），可能显示假 0°C")
    if "acl" in contents:
        try:
            acl = json.loads(contents["acl"])
            permitted = acl["luci-mod-status-index"]["read"]["ubus"]["luci"]
            if "getTempInfo" not in permitted:
                errors.append("ACL 未放行 luci.getTempInfo（概览页会 403）")
        except (ValueError, KeyError, TypeError):
            errors.append("温度 ACL 不是合法 JSON")
    return errors


def verify(image):
    image = pathlib.Path(image)
    with image.open("rb") as stream:
        magic = stream.read(4)
    rootfs = rootfs_from_fit(image) if magic == b"\xd0\x0d\xfe\xed" else rootfs_from_tar(image)
    if rootfs[:4] != b"hsqs":
        raise ValueError("镜像 rootfs 不是 squashfs")

    errors = []
    with tempfile.TemporaryDirectory() as temporary:
        base = pathlib.Path(temporary)
        squash = base / "rootfs.squashfs"
        squash.write_bytes(rootfs)

        # 抽取温度链路 4 个文件并校验
        extraction = base / "temperature"
        result = subprocess.run(
            ["unsquashfs", "-no-progress", "-no-xattrs", "-processors", "1",
             "-d", str(extraction), str(squash), *TEMP_PATHS],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode:
            errors.append("无法解出温度链路文件：" + result.stderr.strip())
        else:
            errors += validate_temperature(extraction)

        # 从包数据库校验关键包
        installed = {}
        for db in ["lib/apk/db/installed", "usr/lib/apk/db/installed",
                   "usr/lib/opkg/status", "var/lib/opkg/status"]:
            result = subprocess.run(
                ["unsquashfs", "-cat", str(squash), db],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode == 0 and result.stdout:
                installed = parse_packages(result.stdout)
                break
        if not installed:
            errors.append("找不到 apk/opkg 包数据库")
        else:
            for pkg in REQUIRED_PACKAGES:
                if pkg not in installed:
                    errors.append(f"缺关键包：{pkg}")
    return errors


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: verify-temperature-image.py IMAGE [IMAGE ...]")
    any_error = False
    for arg in sys.argv[1:]:
        try:
            errs = verify(arg)
        except (ValueError, subprocess.CalledProcessError, OSError) as exc:
            errs = [f"{arg}: {exc}"]
        if errs:
            any_error = True
            print(f"::group::温度链路校验失败：{pathlib.Path(arg).name}")
            for e in errs:
                print("  - " + e, file=sys.stderr)
            print("::endgroup::")
        else:
            print(f"OK {pathlib.Path(arg).name}：温度链路 + 关键包齐全")
    if any_error:
        sys.exit(1)
