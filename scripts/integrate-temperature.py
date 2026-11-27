#!/usr/bin/env python3
"""把 autocore 的温度脚本接到 LuCI 系统概览页（参考 Kahen/PonWrt-CI-UBI）。

目标：HG5585F-CT 概览页能显示实时温度；没有读数时显示"不可用"，绝不显示虚假的 0°C；
温度随概览页轮询刷新。CI 在源码接入阶段与最终镜像阶段各校验一次整条链路。

改 5 处：
  1. package/emortal/autocore/Makefile     —— 让 airoha% 也安装 tempinfo 到 /sbin/
  2. luci-base 的 rpcd/ucode/luci          —— getTempInfo 读 /sbin/tempinfo（原生 LuCI 已实现则保留）
  3. luci-mod-status 的 10_system.js       —— 概览页始终显示温度行，无读数显示 Unavailable
  4. luci-mod-status 的 ACL                —— 只读放行 luci.getTempInfo
  5. luci-base 的 zh_Hans/base.po          —— Temperature/Unavailable 中文词条

上游文件结构一旦变化就 fail，避免静默产出缺链路的固件。
"""
import json
import pathlib
import re
import sys

CI_DIR = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = pathlib.Path("package/emortal/autocore/Makefile")
RPC = pathlib.Path("feeds/luci/modules/luci-base/root/usr/share/rpcd/ucode/luci")
PAGE = pathlib.Path("feeds/luci/modules/luci-mod-status/htdocs/luci-static/resources/view/status/include/10_system.js")
ACL = pathlib.Path("feeds/luci/modules/luci-mod-status/root/usr/share/rpcd/acl.d/luci-mod-status-index.json")
ZH_HANS = pathlib.Path("feeds/luci/modules/luci-base/po/zh_Hans/base.po")
MARKER = "// PonWrt-CI temperature integration."
# 无读数哨兵：tempinfo 脚本在无传感器时输出 "No temperature info"，RPC 已等价转为空串。
NO_READING_SENTINELS = ("No temperature info", "0°C", "0 C")


def replace_once(text, old, new, description):
    if text.count(old) != 1:
        raise ValueError(f"上游 {description} 已变化，需人工复核温度接入（匹配数={text.count(old)}）")
    return text.replace(old, new, 1)


def integrate(source):
    edits = {}

    # 1) autocore：把 airoha% 加进 tempinfo 的安装机型过滤。
    makefile = (source / MAKEFILE).read_text(encoding="utf-8")
    rule = re.search(r"\$\(filter ([^,]+), \$\(TARGETID\)\)", makefile)
    if not rule or "./files/tempinfo $(1)/sbin/" not in makefile:
        raise ValueError("上游 autocore tempinfo 安装规则已变，无法确认 airoha 覆盖")
    if "airoha%" not in rule[1].split():
        makefile = makefile[:rule.start(1)] + rule[1] + " airoha%" + makefile[rule.end(1):]
        makefile = re.sub(r"^(PKG_RELEASE:=\S+)$", r"\1.1", makefile, count=1, flags=re.MULTILINE)
    edits[MAKEFILE] = makefile

    # 2) RPC：用健壮版 getTempInfo 替换上游原生实现。
    #    上游原生版无读数时返回 "No temperature info" 英文串 / '?'，不满足"显示不可用、
    #    不显示假 0°C"，因此统一换成读 /sbin/tempinfo 且把无读数映射为空串的版本。
    fragment = (CI_DIR / "config/temperature-rpc.uc").read_text(encoding="utf-8").rstrip("\n")
    rpc = (source / RPC).read_text(encoding="utf-8")
    if MARKER in rpc:
        print("  RPC：已接入，跳过")
    else:
        # 整块替换原生 getTempInfo 方法（一个制表符缩进的 `getTempInfo: {...},`）
        native = re.search(r"\n\tgetTempInfo\s*:\s*\{.*?\n\t\},(?=\n)", rpc, re.S)
        if native:
            if "/sbin/tempinfo" not in native.group(0):
                raise ValueError("现有 luci.getTempInfo 不读 /sbin/tempinfo，拒绝盲改")
            rpc = rpc[:native.start()] + "\n" + fragment + rpc[native.end():]
            print("  RPC：已用健壮版替换原生 getTempInfo（无读数→空串→不可用）")
        elif "const methods = {\n" in rpc:
            rpc = replace_once(rpc, "const methods = {\n",
                               "const methods = {\n" + (CI_DIR / "config/temperature-rpc.uc").read_text(encoding="utf-8"),
                               "LuCI RPC methods")
            print("  RPC：注入 getTempInfo 片段")
        else:
            raise ValueError("luci RPC 里既无 getTempInfo 也无 const methods 定义")
    edits[RPC] = rpc

    # 3) 概览页：温度行始终显示，无读数显示 Unavailable（避免隐藏行 / 假 0°C）。
    page = (source / PAGE).read_text(encoding="utf-8")
    if MARKER not in page:
        if "getTempInfo" in page:
            if not (re.search(r"method:\s*['\"]getTempInfo['\"]", page)
                    and "callTempInfo()" in page
                    and "tempinfo.tempinfo" in page):
                raise ValueError("概览页温度调用形态未知，拒绝盲改")
            native_row = ("\t\tif (tempinfo.tempinfo) {\n"
                          "\t\t\tfields.splice(6, 0, _('Temperature'));\n"
                          "\t\t\tfields.splice(7, 0, tempinfo.tempinfo);\n"
                          "\t\t}\n")
            patched_row = ("\t\t" + MARKER + "\n"
                           "\t\tfields.splice(6, 0, _('Temperature'), "
                           "tempinfo.tempinfo || _('Unavailable'));\n")
            page = replace_once(page, native_row, patched_row, "原生概览页温度行")
            print("  PAGE：温度行改为始终显示，无读数显示 Unavailable")
        else:
            declaration = ("\n" + MARKER + "\n"
                           "var callGetTempInfo = rpc.declare({\n"
                           "    object: 'luci',\n"
                           "    method: 'getTempInfo',\n"
                           "    expect: { tempinfo: '' }\n"
                           "});\n\n")
            page = replace_once(page, "return baseclass.extend({", declaration + "return baseclass.extend({", "status include")
            page = replace_once(page, "uci.load('system')",
                                "uci.load('system'),\n\t\t\tL.resolveDefault(callGetTempInfo(), '')", "status load")
            page = replace_once(page, "_('Kernel Version'),   boardinfo.kernel,",
                                "_('Kernel Version'),   boardinfo.kernel,\n"
                                "\t\t\t_('Temperature'),      data[5] || _('Unavailable'),",
                                "status fields")
            print("  PAGE：注入温度 RPC 声明 / 加载 / 字段")
    else:
        print("  PAGE：已接入，跳过")
    edits[PAGE] = page

    # 4) ACL：只读放行 luci.getTempInfo。
    acl = json.loads((source / ACL).read_text(encoding="utf-8"))
    methods = acl["luci-mod-status-index"]["read"]["ubus"]["luci"]
    if "getTempInfo" not in methods:
        methods.append("getTempInfo")
        edits[ACL] = json.dumps(acl, indent="\t") + "\n"
        print("  ACL：加入 luci.getTempInfo 只读权限")
    else:
        edits[ACL] = (source / ACL).read_text(encoding="utf-8")
        print("  ACL：已放行，跳过")

    # 5) 中文词条：补 Temperature / Unavailable，避免显示英文或空串。
    translations = (source / ZH_HANS).read_text(encoding="utf-8")
    additions = ""
    for label, translated in [("Temperature", "温度"), ("Unavailable", "不可用")]:
        if not re.search(r'^msgid "' + re.escape(label) + r'"$', translations, re.MULTILINE):
            additions += f'\n\nmsgid "{label}"\nmsgstr "{translated}"\n'
        else:
            print(f"  PO：{label} 词条已存在")
    if additions:
        translations = translations.rstrip() + additions
        edits[ZH_HANS] = translations
        print("  PO：补齐 Temperature / Unavailable 中文词条")

    for relative, content in edits.items():
        (source / relative).write_text(content, encoding="utf-8")
    print("温度链路源码接入完成（autocore / RPC / 概览页 / ACL / 中文词条）。")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: integrate-temperature.py PONWRT_SOURCE")
    try:
        integrate(pathlib.Path(sys.argv[1]).resolve())
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"::error::温度接入失败：{exc}", file=sys.stderr)
        sys.exit(1)
