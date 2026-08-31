#!/usr/bin/env python3
"""secrets-cli - provider 密钥加密存储的管理 CLI。

用法（在仓库根或容器内均可；连的是 DATABASE_URL 指向的库）：
  python3 secrets-cli.py list                 # 列名字/更新时间（不显示值）
  python3 secrets-cli.py set CLAUDE_CODE_KEY_3   # getpass 输入值，不进 shell history
  python3 secrets-cli.py delete CLAUDE_CODE_KEY_3
  python3 secrets-cli.py verify CLAUDE_CODE_KEY_3 # 解密回显前后各 4 位（确认能解开）
  python3 secrets-cli.py export > backup.json     # 明文导出（换派生密钥前备份）
  python3 secrets-cli.py import backup.json       # 导入（set 语义，upsert）

环境变量要求与容器一致：DATABASE_URL + GATEWAY_MASTER_KEY（或
SECRETS_MASTER_KEY_FILE）。本地跑可以从 .env.merged source。

注意：set 之后该 key 的明文应从云端配置 / PaaS 环境变量里删掉，否则加密
存储只是多一份副本，起不到收敛可见面的作用。
"""
from __future__ import annotations

import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manage import secrets_store  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    cmd = argv[0]

    if cmd == "list":
        rows = secrets_store.list_all()
        if not rows:
            print("（空：没有存任何加密密钥）")
            return 0
        print(f"{'名称':<28} 更新时间")
        for name, _enc, ts in rows:
            print(f"{name:<28} {ts}")
        print(f"\n共 {len(rows)} 个。值不回显；verify NAME 可确认可解密。")
        return 0

    if cmd == "set":
        if len(argv) != 2:
            print("用法: set NAME", file=sys.stderr)
            return 2
        value = getpass.getpass(f"输入 {argv[1]} 的值（输入不回显）: ")
        value2 = getpass.getpass("再输一遍确认: ")
        if value != value2:
            print("✗ 两次输入不一致", file=sys.stderr)
            return 1
        secrets_store.set_secret(argv[1], value)
        print(f"✓ {argv[1]} 已加密入库。记得把明文从云端配置/环境变量里删掉。")
        return 0

    if cmd == "delete":
        if len(argv) != 2:
            print("用法: delete NAME", file=sys.stderr)
            return 2
        n = secrets_store.delete_secret(argv[1])
        print(f"✓ 删除 {n} 条" if n else "- 本来就没有这条")
        return 0

    if cmd == "verify":
        if len(argv) != 2:
            print("用法: verify NAME", file=sys.stderr)
            return 2
        v = secrets_store.get_decrypted(argv[1])
        if v is None:
            print(f"✗ {argv[1]} 不存在", file=sys.stderr)
            return 1
        shown = v if len(v) <= 8 else v[:4] + "…" + v[-4:]
        print(f"✓ 可解密，长度 {len(v)}，前后 4 位: {shown}")
        return 0

    if cmd == "export":
        rows = secrets_store.list_all()
        plain = {name: secrets_store.decrypt(enc) for name, enc, _ts in rows}
        json.dump(plain, sys.stdout, ensure_ascii=False, indent=2)
        print(file=sys.stdout)
        print(f"（已导出 {len(plain)} 条明文到 stdout，注意保管）", file=sys.stderr)
        return 0

    if cmd == "import":
        if len(argv) != 2:
            print("用法: import FILE", file=sys.stderr)
            return 2
        with open(argv[1], encoding="utf-8") as f:
            plain = json.load(f)
        for name, value in plain.items():
            secrets_store.set_secret(name, value)
        print(f"✓ 导入 {len(plain)} 条")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
