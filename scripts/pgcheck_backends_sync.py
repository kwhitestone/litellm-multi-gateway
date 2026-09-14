#!/usr/bin/env python3
"""对真实 PG 跑 backends 同步协程的活体自测（dev compose 库，非 PROD）。

协议（用户指定）：测前快照 id=1 的 content+updated_at，测后恢复，最后 diff 证明无损。
不依赖真实时钟等待：直接 await run_once()，验证「一轮同步」的效果，
周期协程本身由单测覆盖（sync_loop 只是 run_once + sleep 的循环）。

热重载用 stub 替身：本脚本在容器里以独立进程跑，没有 litellm 的 llm_router 全局
（那是 server.py 进程里的对象）。这里验证的是「同步逻辑 + DB 读写 + 文件物化」，
以及 set_model_list 确实被调到——真正的 router 热重载在单测里用 AsyncMock 断言，
容器内活体验证按约定不在本次范围。

运行：docker exec litellm-multi-gateway-litellm-1 python3 /tmp/pgcheck.py
"""
import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2  # noqa: E402

DSN = os.environ["DATABASE_URL"]
TABLE = '"manage".gateway_backends'
OK, FAIL = "✓", "✗"
results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(f"  {OK if cond else FAIL} {label}{(' -- ' + extra) if extra else ''}", flush=True)


def q(sql, args=None, fetch=True):
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(sql, args or ())
        row = cur.fetchone() if fetch else None
        c.commit()
        return row


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest() if s is not None else None


print("=" * 68)
print("backends 同步 · 真实 PG 自测")
print("=" * 68)

# ---- 0. 快照 ----
snap_content, snap_updated = q(f"SELECT content, updated_at FROM {TABLE} WHERE id=1")
print(f"\n[快照] {len(snap_content)} 字节, updated_at={snap_updated}, sha={sha(snap_content)[:16]}")

work = Path(tempfile.mkdtemp(prefix="pgcheck-"))
backends_file = work / "backends.yaml"
config_file = work / "config.yaml"

from manage import backends_store, backends_sync  # noqa: E402

# 把 store 指向临时文件，别动容器里正在用的 /app/backends.yaml
backends_store.BAKED_BACKENDS = backends_file
backends_store.BAKED_CONFIG = config_file

reload_calls = []


async def fake_reload():
    """替身：记录热重载被调用。真 router 在 server.py 进程里，这里够不着。"""
    reload_calls.append(1)
    return 42


backends_sync._reload_with_lock = fake_reload

try:
    # ---- 1. PULL：DB 变 -> 文件被物化 + 热重载被调 ----
    print("\n[1] PULL：改 DB 内容，验证文件物化 + 热重载")
    backends_file.write_text(snap_content, encoding="utf-8")
    last_seen = backends_sync.LastSeen()
    asyncio.run(backends_sync.run_once(last_seen))   # 先收敛到一致态
    reload_calls.clear()

    # 合法改动：加一条注释（gen-config 能过）
    mutated = snap_content.rstrip() + "\n# pgcheck 临时改动\n"
    q(f"UPDATE {TABLE} SET content=%s, updated_at=NOW() WHERE id=1", (mutated,), fetch=False)

    act = asyncio.run(backends_sync.run_once(last_seen))
    on_disk = backends_file.read_text(encoding="utf-8")
    check("动作为 PULL", act is backends_sync.Action.PULL, act.value)
    check("文件已物化为 DB 内容", sha(on_disk) == sha(mutated))
    check("热重载被调用", len(reload_calls) == 1, f"{len(reload_calls)} 次")
    check("config.yaml 已重新生成", config_file.exists())
    check("无 pull_error", backends_sync.STATUS.pull_error is None,
          str(backends_sync.STATUS.pull_error))
    check("记录了生效时间", bool(backends_sync.STATUS.last_ok_at),
          str(backends_sync.STATUS.last_ok_at))

    # ---- 2. 循环防护：紧接着再跑一轮必须 NOOP ----
    print("\n[2] 循环防护：物化后再跑一轮")
    reload_calls.clear()
    act2 = asyncio.run(backends_sync.run_once(last_seen))
    check("动作为 NOOP（不回写、不自激）", act2 is backends_sync.Action.NOOP, act2.value)
    check("未再次热重载", len(reload_calls) == 0, f"{len(reload_calls)} 次")

    # ---- 3. PUSH：手改文件（合法）-> 回写 DB ----
    print("\n[3] PUSH：手改容器内文件（合法配置），验证回写 DB")
    hand_edit = mutated.rstrip() + "\n# pgcheck 手改回写\n"
    backends_file.write_text(hand_edit, encoding="utf-8")
    reload_calls.clear()
    act3 = asyncio.run(backends_sync.run_once(last_seen))
    db_now, _ = q(f"SELECT content, updated_at FROM {TABLE} WHERE id=1")
    check("动作为 PUSH", act3 is backends_sync.Action.PUSH, act3.value)
    check("DB 内容已更新为手改内容", sha(db_now) == sha(hand_edit))
    check("无 push_error", backends_sync.STATUS.push_error is None,
          str(backends_sync.STATUS.push_error))

    # ---- 4. PUSH 被拒：手改文件非法 -> 不入库 + 横幅 ----
    print("\n[4] 非法文件：验证拒绝回写 + 横幅可见")
    before, _ = q(f"SELECT content, updated_at FROM {TABLE} WHERE id=1")
    asyncio.run(backends_sync.run_once(last_seen))     # 先收敛
    backends_file.write_text("backends:\n  broken: {mapping: [[[\n", encoding="utf-8")
    act4 = asyncio.run(backends_sync.run_once(last_seen))
    after, _ = q(f"SELECT content, updated_at FROM {TABLE} WHERE id=1")
    check("动作为 PUSH（尝试回写）", act4 is backends_sync.Action.PUSH, act4.value)
    check("DB 未被非法内容污染", sha(after) == sha(before))
    check("push_error 已设置（横幅可见）", bool(backends_sync.STATUS.push_error))
    check("状态 healthy=False", backends_sync.STATUS.as_dict()["healthy"] is False)
    print(f"      横幅内容: {str(backends_sync.STATUS.push_error)[:100]}")

    # ---- 5. PULL 被拒：DB 里是坏配置 -> 不碰文件、不热重载 ----
    print("\n[5] DB 里是坏配置：验证拒绝应用 + 不动正在服务的路由")
    backends_file.write_text(snap_content, encoding="utf-8")
    last_seen2 = backends_sync.LastSeen()
    asyncio.run(backends_sync.run_once(last_seen2))
    good_on_disk = backends_file.read_text(encoding="utf-8")
    q(f"UPDATE {TABLE} SET content=%s, updated_at=NOW() WHERE id=1",
      ("backends:\n  x: {litellm_model: [[[bad\n",), fetch=False)
    reload_calls.clear()
    backends_sync.STATUS.pull_error = None
    asyncio.run(backends_sync.run_once(last_seen2))
    check("文件未被坏配置覆盖", sha(backends_file.read_text(encoding="utf-8")) == sha(good_on_disk))
    check("未热重载（路由继续跑旧配置）", len(reload_calls) == 0, f"{len(reload_calls)} 次")
    check("pull_error 已设置（横幅可见）", bool(backends_sync.STATUS.pull_error))
    print(f"      横幅内容: {str(backends_sync.STATUS.pull_error)[:100]}")

finally:
    # ---- 恢复 ----
    print("\n[恢复] 写回快照内容与原 updated_at")
    q(f"UPDATE {TABLE} SET content=%s, updated_at=%s WHERE id=1",
      (snap_content, snap_updated), fetch=False)

final_content, final_updated = q(f"SELECT content, updated_at FROM {TABLE} WHERE id=1")
print("=" * 68)
print(f"  content sha  : 前 {sha(snap_content)[:16]} / 后 {sha(final_content)[:16]}")
print(f"  updated_at   : 前 {snap_updated} / 后 {final_updated}")
restored = sha(final_content) == sha(snap_content) and final_updated == snap_updated
check("恢复无损（content + updated_at 完全一致）", restored)

print("=" * 68)
print(f"结果：{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
