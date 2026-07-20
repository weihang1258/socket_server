#!/usr/bin/env python3
"""从 CHANGELOG.md 提取当前版本的 release notes，生成 socket_server/release_notes.py。

打包前调用：python3 packaging/generate_release_notes.py
输出：socket_server/release_notes.py（.gitignore，不入库）

handlers.py datatype 19 会读本模块，不再调用 GitHub API。
"""
import os
import re
import sys

ROOTDIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHANGELOG = os.path.join(ROOTDIR, "CHANGELOG.md")
OUTPUT = os.path.join(ROOTDIR, "socket_server", "release_notes.py")


def _get_version():
    """从 version.py 读 VERSION"""
    version_file = os.path.join(ROOTDIR, "socket_server", "version.py")
    with open(version_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("VERSION ="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def extract_notes(changelog_path, version):
    """从 CHANGELOG.md 提取 [version] 节的内容，返回 markdown 字符串"""
    with open(changelog_path) as f:
        lines = f.readlines()

    # 匹配 ## [version] 或 ## version 等写法
    pattern = re.compile(rf'^##\s*\[?{re.escape(version)}\]?')
    in_section = False
    notes = []
    for line in lines:
        if not in_section:
            if pattern.match(line):
                in_section = True
                # 跳过标题行本身
                continue
        else:
            # 遇到下一个 ## 节就停
            if line.startswith("## "):
                break
            notes.append(line)

    return "".join(notes).strip()


def main():
    version = _get_version()
    if not version:
        print("ERROR: 无法从 version.py 读取 VERSION", file=sys.stderr)
        sys.exit(1)

    notes = extract_notes(CHANGELOG, version)
    if not notes:
        print(f"WARNING: CHANGELOG.md 中未找到 [{version}] 节，release_notes 为空", file=sys.stderr)

    content = f'"""当前版本 {version} 的 release notes（打包时从 CHANGELOG.md 注入，不联网查询）。"""\n'
    content += f'VERSION = "{version}"\n'
    content += f'RELEASE_NOTES = """\n{notes}\n"""\n'

    with open(OUTPUT, "w") as f:
        f.write(content)
    print(f"已生成 {OUTPUT} (version={version}, notes_len={len(notes)})")


if __name__ == "__main__":
    main()
