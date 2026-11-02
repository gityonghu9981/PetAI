#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫"裸模块名 import 指向别的目录"——CHANGELOG §8 那类静默失败的可复跑判据。

起因（2026-11-01）：`app/brain/pipeline.py` 写的是 `import status` / `import audit`，
而这两个模块的真身在 `app/core/` 下。从 `app/brain/` 根本解析不到，`ImportError` 被紧挨着的
`except Exception: status_light = None` 吞掉 ⇒ "答完不回自由态" 和 "逐轮审计从没写过"
两件事同时静默发生，日志里一个字都没有。

判定口径（宁窄勿宽，噪声多的检查没人肯跑）：
- 只盯**裸模块名**：`import NAME` / `from NAME import …`，NAME 不带点号。
- NAME 是标准库（`sys.stdlib_module_names`）→ 放过。
- NAME 是 root 下的**包目录**（`app/core/`、`app/brain/` 这种）→ 放过。
  本仓一律用 `from core import config` 这种包限定写法，`cwd=app` 下确实能用，不是问题。
- NAME 命中某个 `.py` 文件，但那个文件**不在本文件自己的目录里** → SUSPECT。
  同时打印它外面是否裹着 `except`：裹着 = 失败会静默，最危险的一种。

退出码：0 = 干净；1 = 有命中；2 = 用法/环境错误。
用法：
  .venv\\Scripts\\python.exe -X utf8 tools\\bare_import_scan.py --all
  .venv\\Scripts\\python.exe -X utf8 tools\\bare_import_scan.py --root app
"""
from __future__ import annotations

import argparse
import os
import re
import sys

STDLIB = set(getattr(sys, "stdlib_module_names", ()))
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "build", "dist", ".tmp"}
BARE_RE = re.compile(r"^[ \t]*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")


def collect(root):
    """modules: 裸名 -> 该 .py 所在目录集合；packages: 裸名 -> 包目录的父目录集合。"""
    modules, packages = {}, {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py") and f != "__init__.py":
                modules.setdefault(f[:-3], set()).add(dirpath)
        for d in dirnames:
            if os.path.exists(os.path.join(dirpath, d, "__init__.py")):
                packages.setdefault(d, set()).add(dirpath)
    return modules, packages


def in_except(lines, idx, window=6):
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    for j in range(idx - 1, max(-1, idx - window), -1):
        s = lines[j]
        if not s.strip():
            continue
        ind = len(s) - len(s.lstrip())
        if ind < indent and s.lstrip().startswith("except"):
            return True
    return False


def scan(root):
    if not os.path.isdir(root):
        print("目录不存在：%s" % root)
        return None
    modules, packages = collect(root)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            try:
                lines = open(path, encoding="utf-8").read().splitlines()
            except Exception as e:
                print("读不动 %s：%s" % (path, e))
                continue
            for i, line in enumerate(lines):
                m = BARE_RE.match(line)
                if not m:
                    continue
                name = m.group(1)
                if name in STDLIB or name in packages:
                    continue
                where = modules.get(name)
                if not where or dirpath in where:
                    continue              # 同目录的兄弟文件，裸 import 能用
                hits.append((path.replace("\\", "/"), i + 1, line.strip(),
                             in_except(lines, i),
                             sorted(os.path.relpath(w, root).replace("\\", "/") or "."
                                    for w in where)))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="app", help="要扫的包根（默认 app）")
    ap.add_argument("--all", action="store_true", help="app/ 和 desktop/ 都扫")
    args = ap.parse_args()
    roots = ["app", "desktop"] if args.all else [args.root]
    total = 0
    for r in roots:
        hits = scan(r)
        if hits is None:
            return 2
        print("--- %s：命中 %d 处 ---" % (r, len(hits)))
        for path, ln, text, swallowed, where in hits:
            total += 1
            tag = "  ⚠ 外面裹着 except ⇒ 会静默变成 None" if swallowed else ""
            print("  %s:%d  %s   真身在 %s%s"
                  % (path, ln, text, " / ".join(where), tag))
    if total == 0:
        print("结果：OK（没有指向别的目录的裸模块名 import）")
        return 0
    print("结果：FAIL（%d 处）——改成 `from <包> import <模块>`（见 CHANGELOG §8），"
          "或确认可导入后在这行注明为什么不会 ImportError" % total)
    return 1


if __name__ == "__main__":
    sys.exit(main())
