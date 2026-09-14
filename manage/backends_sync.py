"""
manage/backends_sync.py — backends.yaml 的 DB <-> 文件 实时双向同步 + 路由热重载。

背景：真相源是 PG（manage.gateway_backends 单行），但所有消费者（LiteLLM 路由表、
vision_hook、管理页 routes.py）读的都是容器内的 /app/backends.yaml。原来只有 server.py
启动时 load_on_boot() 物化一次，于是「改了 DB 不重启不生效」「手改容器内文件下次重启被
覆盖」。本模块加一个周期协程把两边持续对齐，变更后热重载路由表。

同步方向由 decide() 纯函数决定（无 I/O、无时钟依赖，单测只注入状态测分支）：
    (DbState, FileState, LastSeen) -> Action

为什么用「updated_at 跳变 + 内容 hash」两个条件而不是单看时间戳：
  - 只看 updated_at：自己物化完文件后 DB 时间戳没变，若误判成「文件变了」就会回写 DB，
    回写推高 updated_at，下一轮所有实例又拉一遍 —— 自激死循环。
  - 只看 hash：分得出「不一致」，分不出「是谁改的」，定不了方向。
两个一起用才能既定方向又不自激：物化/回写后 file_hash == db_hash，直接落 NOOP 收敛。

冲突语义：DB 是会合点，后写者赢。两边都变时以 DB 为准（PULL），运维手改但未入库的
本地内容会被覆盖 —— 这是刻意的，避免多实例各执一词。但不静默丢弃：PULL_OVERWRITE
会把被覆盖的内容留档（overwrite_warning + 落盘 .overwritten 备份），管理页横幅明示。
重启语义不变：load_on_boot 那套原样保留，没动。

失败语义（三种都不影响正在服务的路由）：
  - DB 内容非法（gen-config 跑不过）：拒绝应用，不碰文件，红字横幅。
  - 本地文件非法：拒绝回写 DB，红字横幅，路由继续跑旧配置。
  - 热重载失败：保留旧 router 继续服务，红字横幅。

热重载选型见 hot_reload() 注释（用 Router.set_model_list，不用 ProxyConfig.load_config）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# 同步周期（秒）。管理页保存走快路径立即生效，这里兜底别的实例 / 直改 DB / 手改文件。
SYNC_INTERVAL_SECONDS = float(os.environ.get("BACKENDS_SYNC_INTERVAL", "10") or 10)


def content_hash(content: Optional[str]) -> Optional[str]:
    """内容指纹。None（文件不存在 / DB 无行）保持 None，不要退化成空串的 hash——
    「没有内容」和「内容是空」在方向判定里是两回事。"""
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DbState:
    """DB 侧快照。reachable=False 表示这轮没连上，与「连上了但表里没行」区分开。"""
    reachable: bool
    content: Optional[str] = None
    updated_at: Optional[Any] = None   # datetime 或 isoformat str，只做相等比较

    @property
    def hash(self) -> Optional[str]:
        return content_hash(self.content)

    @property
    def has_row(self) -> bool:
        return self.reachable and self.content is not None


@dataclass(frozen=True)
class FileState:
    """容器内 /app/backends.yaml 快照。"""
    exists: bool
    content: Optional[str] = None

    @property
    def hash(self) -> Optional[str]:
        return content_hash(self.content)


@dataclass
class LastSeen:
    """本实例上次「达成一致」时记下的坐标，用来判断这轮是哪边动了。

    只在同步成功（物化完 / 回写完 / 确认一致）后更新；校验失败时**不更新**，
    否则下一轮就认不出那份非法内容还没处理、会误判成已收敛。
    """
    updated_at: Optional[Any] = None
    content_hash: Optional[str] = None

    def mark(self, updated_at: Optional[Any], chash: Optional[str]) -> None:
        self.updated_at = updated_at
        self.content_hash = chash


class Action(Enum):
    """decide() 的输出。执行副作用的代码按这个分派，分派见 run_once()。"""
    NOOP = "noop"                          # 两边一致，不动
    PULL = "pull"                          # DB -> 文件 + regen + 热重载
    PULL_OVERWRITE = "pull_overwrite"      # 同 PULL，但要额外告警：本地手改被覆盖
    PUSH = "push"                          # 文件 -> DB（先 validate，失败不写）
    SEED = "seed"                          # DB 可达但没行：当前文件作初始值入库
    SKIP_DB_DOWN = "skip_db_down"          # DB 不可达，这轮什么都不做


# 方向判定表：(db_changed, file_changed) -> Action
# 写成表而不是 if-else 链，四种组合一眼看全，单测也能逐格覆盖。
_DIRECTION: dict[tuple[bool, bool], Action] = {
    # 双端同变：DB 是会合点后写者赢，但本地手改要显式告警，不静默丢
    (True, True): Action.PULL_OVERWRITE,
    # 只有 DB 变：管理页 / 别的实例 / 直改 DB
    (True, False): Action.PULL,
    # 只有文件变：有人手改了容器内文件，校验通过就回写 DB
    (False, True): Action.PUSH,
    # 两边相对 last_seen 都没变但 hash 不等：本实例刚启动（last_seen 为空）
    # 或 last_seen 丢了，无从判断谁新 —— 按真相源 DB 兜底
    (False, False): Action.PULL,
}


def decide(db: DbState, file: FileState, last_seen: LastSeen) -> Action:
    """纯函数：据两侧快照 + 上次坐标决定这轮做什么。无 I/O、无副作用、不读时钟。

    先排除「没得比」的情况（DB 不可达 / DB 没行 / 文件不存在），
    两边都有内容时才进方向判定表。
    """
    if not db.reachable:
        return Action.SKIP_DB_DOWN
    if not db.has_row:
        # DB 连上但表里没行 = 首次部署。有文件就拿它当初始值，没文件则无事可做。
        return Action.SEED if file.exists else Action.NOOP
    if not file.exists:
        return Action.PULL          # 文件被删 / 还没物化过，按 DB 铺一份

    if db.hash == file.hash:
        return Action.NOOP          # 已一致：物化/回写后的稳定态，也是防循环的终点

    if last_seen.content_hash is None:
        # 冷启动：本进程还没同步过，没有「上一轮坐标」可比，任何差异都无从归因。
        # 此时文件是镜像烤进去的版本而非运维手改，按真相源 DB 铺下来即可。
        # 必须在方向判定表之前拦掉：否则 last_seen 全空会让 file_changed 恒为 True，
        # 冷启动被误判成「双端同变」，每次重启都报一次「本地改动被覆盖」的假告警。
        return Action.PULL

    db_changed = db.updated_at != last_seen.updated_at
    file_changed = file.hash != last_seen.content_hash
    return _DIRECTION[(db_changed, file_changed)]


@dataclass
class SyncStatus:
    """同步状态，供管理页横幅渲染。

    错误分三类是因为影响面不同：pull_error = DB 里的配置没法应用（别人推了坏配置），
    push_error = 本地文件没法入库（手改写错了），reload_error = 配置已落盘但路由没换上。
    """
    last_action: Optional[str] = None
    last_ok_at: Optional[str] = None          # 最近一次成功应用的时间戳（= 生效时间）
    pull_error: Optional[str] = None
    push_error: Optional[str] = None
    reload_error: Optional[str] = None
    overwrite_warning: Optional[str] = None   # 本地手改被 DB 覆盖（不静默丢弃）
    db_down: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_action": self.last_action,
            "last_ok_at": self.last_ok_at,
            "pull_error": self.pull_error,
            "push_error": self.push_error,
            "reload_error": self.reload_error,
            "overwrite_warning": self.overwrite_warning,
            "db_down": self.db_down,
            "healthy": not (self.pull_error or self.push_error or self.reload_error),
        }

    def clear_errors(self) -> None:
        self.pull_error = None
        self.push_error = None
        self.reload_error = None


# 进程级单例（管理页读它渲染横幅）
STATUS = SyncStatus()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 热重载
# ---------------------------------------------------------------------------

def hot_reload() -> int:
    """把 regen 后的 config.yaml 的 model_list 热加载进正在服务的 Router。

    选型：Router.set_model_list（router.py:8156），不用 ProxyConfig.load_config。
    依据（读的是容器实际运行的 litellm 1.82 源码副本 litellm-src-ref/）：

    1) load_config 在 1.82 里**没有任何运行时调用点**。三处调用
       （proxy_server.py:1024 / :1039 / :7453）全在启动路径上，:7453 位于
       initialize()（定义在 :7349）这个 CLI/启动辅助函数里，不是 /model/new。
       即「官方运行时路径」这个前提在 1.82 不成立。
    2) load_config 的 global 块（proxy_server.py:4620+）会重绑 master_key、
       litellm_master_key_hash、user_custom_auth、use_background_health_checks 等一票
       全局；并重跑 initialize_guardrails(:4752) 和 initialize_callbacks_on_proxy(:4792)。
       我们用 config.yaml 注册了 hooks.vision_hook.VisionPreRequestHook 和 headroom
       guardrail，每 10s 重跑一次有重复注册风险（回调被调 N 次），
       且 callback_utils 的去重语义无法从现有源码副本证实 —— 不拿生产正确性赌它。
    3) set_model_list 只重置模型路由相关状态，不碰 auth/callback/guardrail 全局：
       model_list / model_id_to_deployment_index_map / model_name_to_deployment_indices /
       team_* / quality_routers / complexity_routers / auto_routers + 两个 cache 失效。
       upstream 在 router.py:8164-8165 的注释明确写了这是为热重载设计的：
       "Reset per-strategy router registries so hot-reload doesn't leave stale routers
        pointing at the old model_list."
       它是同步函数，中途无 await 点，换表是原子的。

    并发：litellm 自己的 add_deployment 后台 job（proxy_server.py:8848-8853，60s 一次）
    会 read-modify-write 同一个 llm_router 全局。proxy_server.py:2191 的
    MODEL_RECONCILE_LOCK 注释点名了这个竞态（两个并发 reconcile 各按自己的快照，
    旧快照那个会把新加的 deployment 挤掉）。所以我们也走同一把锁。
    本函数是同步的，锁要在 async 侧拿 —— 见 _reload_with_lock()。

    返回加载的 model 数量；失败抛异常（调用方转红字横幅，旧 router 继续服务）。
    """
    import yaml

    from . import backends_store

    cfg_path = backends_store.BAKED_CONFIG
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    model_list = cfg.get("model_list") or []
    if not model_list:
        raise RuntimeError(f"{cfg_path} 里没有 model_list，拒绝用空表替换正在服务的路由")

    from litellm.proxy import proxy_server as ps

    router = getattr(ps, "llm_router", None)
    if router is None:
        # 还没初始化完（启动早期）——不算错误，等下一轮或启动流程自己加载
        raise RuntimeError("llm_router 尚未初始化，跳过热重载")

    router.set_model_list(model_list)
    # proxy_server 另有 llm_model_list 全局供 /v1/models 等读取，一并对齐，
    # 否则路由换了但模型列表接口还报旧的。
    ps.llm_model_list = router.get_model_list()
    return len(model_list)


async def _reload_with_lock() -> int:
    """在 MODEL_RECONCILE_LOCK 下做热重载（见 hot_reload() 的并发说明）。

    拿不到锁对象（litellm 版本不同/未导出）时退化为直接调用：
    宁可承担低概率竞态，也不要因为找不到锁就完全不热重载。
    """
    lock = None
    try:
        from litellm.proxy import proxy_server as ps
        lock = getattr(ps, "MODEL_RECONCILE_LOCK", None)
    except Exception:
        lock = None

    if lock is None:
        return hot_reload()
    async with lock:
        return hot_reload()


# ---------------------------------------------------------------------------
# 一轮同步
# ---------------------------------------------------------------------------

def read_file_state(path: Path) -> FileState:
    """读本地文件快照。读不出来当作不存在（让 decide 走 PULL 兜底）。"""
    try:
        if not path.exists():
            return FileState(exists=False)
        return FileState(exists=True, content=path.read_text(encoding="utf-8"))
    except Exception:
        return FileState(exists=False)


def _backup_overwritten(path: Path, content: Optional[str]) -> Optional[str]:
    """把即将被 DB 覆盖的本地内容留一份，别让运维的手改凭空消失。
    返回备份路径；失败返回 None（备份失败不能挡住同步本身）。"""
    if not content:
        return None
    try:
        dest = path.with_suffix(path.suffix + ".overwritten")
        dest.write_text(content, encoding="utf-8")
        return str(dest)
    except Exception:
        return None


async def run_once(last_seen: LastSeen, status: SyncStatus = STATUS) -> Action:
    """跑一轮同步：读两侧快照 -> decide -> 执行副作用。返回实际执行的 Action。

    所有副作用集中在这里，decide() 保持纯函数。异常不外抛（后台协程不能被单轮
    失败打死），转成 status 上的错误字段给管理页横幅。
    """
    from . import backends_store

    path = backends_store.BAKED_BACKENDS
    db_raw = await asyncio.to_thread(backends_store.fetch_raw)
    db = DbState(**db_raw) if isinstance(db_raw, dict) else DbState(reachable=False)
    file = read_file_state(path)

    action = decide(db, file, last_seen)
    status.db_down = action is Action.SKIP_DB_DOWN

    if action in (Action.NOOP, Action.SKIP_DB_DOWN):
        if action is Action.NOOP:
            # 已一致：把坐标对齐，之后 DB 再变才认得出来
            last_seen.mark(db.updated_at, file.hash)
        return action

    if action is Action.SEED:
        try:
            await asyncio.to_thread(backends_store.seed_from_file, file.content)
            fresh = await asyncio.to_thread(backends_store.fetch_raw)
            last_seen.mark(fresh.get("updated_at"), file.hash)
            status.last_action = action.value
            status.last_ok_at = _now_iso()
            status.push_error = None
        except Exception as exc:
            status.push_error = f"初始化入库失败: {exc}"
        return action

    if action in (Action.PULL, Action.PULL_OVERWRITE):
        # DB 内容先校验再落盘：别人推了坏配置时不碰文件、不动正在服务的路由
        try:
            await asyncio.to_thread(backends_store.validate, db.content)
        except Exception as exc:
            status.pull_error = f"数据库里的配置校验不通过，已拒绝应用（路由仍在跑旧配置）: {exc}"
            return action

        backup = None
        if action is Action.PULL_OVERWRITE:
            backup = _backup_overwritten(path, file.content)

        try:
            await asyncio.to_thread(path.write_text, db.content, "utf-8")
            await asyncio.to_thread(backends_store.regen_config_strict)
        except Exception as exc:
            status.pull_error = f"物化/生成 config.yaml 失败: {exc}"
            return action

        status.pull_error = None
        try:
            n = await _reload_with_lock()
            status.reload_error = None
            status.last_action = action.value
            status.last_ok_at = _now_iso()
            last_seen.mark(db.updated_at, db.hash)
            print(f"[backends_sync] 已应用 DB 配置并热重载 {n} 个模型", flush=True)
        except Exception as exc:
            # 配置已落盘但路由没换上：旧 router 继续服务，红字横幅
            status.reload_error = f"热重载失败（路由仍在跑旧配置，重启可生效）: {exc}"
            last_seen.mark(db.updated_at, db.hash)

        if action is Action.PULL_OVERWRITE:
            # 不静默丢弃：明确告诉运维本地手改被覆盖了，备份在哪
            status.overwrite_warning = (
                "检测到本地文件与数据库同时被修改，已按「数据库为准」覆盖本地改动"
                + (f"；被覆盖的内容已备份到 {backup}" if backup
                   else "；备份失败，本地改动未能留档")
            )
            print(f"[backends_sync] {status.overwrite_warning}", flush=True)
        return action

    if action is Action.PUSH:
        # 本地手改回写 DB：校验不过就拒绝，路由继续跑旧配置
        try:
            await asyncio.to_thread(backends_store.save, file.content)
        except Exception as exc:
            status.push_error = f"本地文件校验不通过，已拒绝写入数据库: {exc}"
            return action
        fresh = await asyncio.to_thread(backends_store.fetch_raw)
        last_seen.mark(fresh.get("updated_at"), file.hash)
        status.push_error = None
        status.last_action = action.value
        status.last_ok_at = _now_iso()
        print("[backends_sync] 本地文件改动已回写数据库", flush=True)
        return action

    return action


async def apply_now(content: str, status: SyncStatus = STATUS) -> str:
    """管理页保存的快路径：内容已入库，这里立刻物化 + regen + 热重载本实例。

    不等 10s 轮询是为了「点保存 -> 立即生效」的手感；其余实例照常由各自协程收敛
    （DB 的 updated_at 已经跳变，它们下一轮就是 PULL）。

    这里**不更新 last_seen**：协程有自己的 LastSeen 实例，它下一轮读到
    file_hash == db_hash 会直接 NOOP 并对齐坐标，不会误判成「文件被手改」。
    返回生效时间戳；热重载失败抛异常（调用方转成 reload_error 提示）。
    """
    from . import backends_store

    path = backends_store.BAKED_BACKENDS
    await asyncio.to_thread(path.write_text, content, "utf-8")
    await asyncio.to_thread(backends_store.regen_config_strict)
    try:
        n = await _reload_with_lock()
    except Exception as exc:
        status.reload_error = f"热重载失败（路由仍在跑旧配置，重启可生效）: {exc}"
        raise
    status.clear_errors()
    status.last_action = "save"
    status.last_ok_at = _now_iso()
    print(f"[backends_sync] 管理页保存已即时生效，热重载 {n} 个模型", flush=True)
    return status.last_ok_at


async def sync_loop(interval: float = SYNC_INTERVAL_SECONDS) -> None:
    """后台协程：周期跑 run_once。被 cancel 时干净退出（server.py 关闭钩子用）。"""
    last_seen = LastSeen()
    print(f"[backends_sync] 同步协程已启动（周期 {interval}s）", flush=True)
    while True:
        try:
            await run_once(last_seen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # 单轮异常不能打死协程
            print(f"[backends_sync] 本轮同步异常: {exc!r}", flush=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            print("[backends_sync] 同步协程已停止", flush=True)
            raise
