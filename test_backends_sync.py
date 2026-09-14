#!/usr/bin/env python3
"""backends_sync 的同步方向判定 + 副作用分派自测（不连真实 PG，不起协程）。

覆盖需求里点名的 6 个场景：
  无变化 / DB 变 / 文件变(校验过) / 文件变(校验败) / 循环防护 / 后写者赢

decide() 是纯函数，测它只需注入 DbState/FileState/LastSeen —— 不依赖真实时钟、
不 sleep、不连库。run_once 的副作用分派用 stub 的 backends_store 验证。

运行：python3 test_backends_sync.py
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manage.backends_sync import (  # noqa: E402
    Action,
    DbState,
    FileState,
    LastSeen,
    SyncStatus,
    content_hash,
    decide,
    read_file_state,
    run_once,
)

A = "backends:\n  ark: {}\n"      # 内容 A
B = "backends:\n  zai: {}\n"      # 内容 B（与 A 不同）
HA, HB = content_hash(A), content_hash(B)
T1, T2 = "2026-09-13T14:49:49", "2026-09-13T15:00:00"


class DecideTest(unittest.TestCase):
    """方向判定：纯函数，逐分支。"""

    def test_no_change_when_both_sides_equal(self):
        """无变化：两边 hash 相同 -> NOOP（这也是防循环的终点态）。"""
        db = DbState(reachable=True, content=A, updated_at=T1)
        self.assertIs(decide(db, FileState(True, A), LastSeen(T1, HA)), Action.NOOP)

    def test_db_changed_pulls(self):
        """DB 变（管理页/别的实例/直改 DB）：updated_at 跳变、文件仍是上轮内容 -> PULL。"""
        db = DbState(reachable=True, content=B, updated_at=T2)
        self.assertIs(decide(db, FileState(True, A), LastSeen(T1, HA)), Action.PULL)

    def test_file_changed_pushes(self):
        """文件变：updated_at 没动、文件 hash 偏离上轮坐标 -> PUSH（回写 DB）。"""
        db = DbState(reachable=True, content=A, updated_at=T1)
        self.assertIs(decide(db, FileState(True, B), LastSeen(T1, HA)), Action.PUSH)

    def test_both_changed_db_wins(self):
        """后写者赢：两边都相对 last_seen 变了 -> PULL_OVERWRITE（DB 为准 + 告警）。"""
        db = DbState(reachable=True, content=B, updated_at=T2)
        act = decide(db, FileState(True, "backends:\n  local: {}\n"), LastSeen(T1, HA))
        self.assertIs(act, Action.PULL_OVERWRITE)

    def test_loop_guard_after_materialize(self):
        """循环防护：物化后 file==db，即使 last_seen 还停在旧坐标也必须 NOOP。

        这是自激死循环的关键闸门——若这里返回 PUSH，回写会推高 updated_at，
        下一轮所有实例再拉一遍，无限循环。
        """
        db = DbState(reachable=True, content=B, updated_at=T2)
        self.assertIs(decide(db, FileState(True, B), LastSeen(T1, HA)), Action.NOOP)

    def test_loop_guard_after_push(self):
        """循环防护（回写方向）：PUSH 完成后两边一致 -> NOOP，不会再被当成 DB 变。"""
        db = DbState(reachable=True, content=B, updated_at=T2)
        self.assertIs(decide(db, FileState(True, B), LastSeen(T2, HB)), Action.NOOP)

    def test_db_unreachable_skips(self):
        """DB 不可达：什么都不做（不能拿本地文件当真相去覆盖别人）。"""
        self.assertIs(decide(DbState(False), FileState(True, A), LastSeen()),
                      Action.SKIP_DB_DOWN)

    def test_seed_when_table_empty(self):
        """DB 可达但没行 = 首次部署：拿本地文件作初始值。"""
        db = DbState(reachable=True, content=None, updated_at=None)
        self.assertIs(decide(db, FileState(True, A), LastSeen()), Action.SEED)
        # 连文件都没有就无事可做
        self.assertIs(decide(db, FileState(False), LastSeen()), Action.NOOP)

    def test_missing_file_pulls(self):
        """文件不存在（被删/没物化过）：按 DB 铺一份。"""
        db = DbState(reachable=True, content=A, updated_at=T1)
        self.assertIs(decide(db, FileState(False), LastSeen(T1, HA)), Action.PULL)

    def test_cold_start_prefers_db(self):
        """冷启动：last_seen 全空，两边不一致且判不出谁新 -> 按真相源 DB 兜底。"""
        db = DbState(reachable=True, content=A, updated_at=None)
        self.assertIs(decide(db, FileState(True, B), LastSeen()), Action.PULL)

    def test_cold_start_never_false_alarms_overwrite(self):
        """回归：冷启动不能被判成「双端同变」。

        last_seen 全空时 file_changed 恒为 True，若不在方向表之前拦掉冷启动，
        每次重启都会报一次「本地改动被覆盖」的假告警、还白存一份 .overwritten 备份。
        """
        db = DbState(reachable=True, content=A, updated_at=T1)
        for file_content in (B, "backends:\n  whatever: {}\n"):
            with self.subTest(file=file_content[:20]):
                act = decide(db, FileState(True, file_content), LastSeen())
                self.assertIs(act, Action.PULL)
                self.assertIsNot(act, Action.PULL_OVERWRITE)

    def test_hash_distinguishes_none_from_empty(self):
        """None（没有内容）和 ""（内容为空）不能混为一谈。"""
        self.assertIsNone(content_hash(None))
        self.assertIsNotNone(content_hash(""))


class _Store:
    """backends_store 的最小替身：记录调用，可注入校验失败。"""

    def __init__(self, tmp: Path, db_row, validate_exc=None, save_exc=None):
        self.BAKED_BACKENDS = tmp / "backends.yaml"
        self.BAKED_CONFIG = tmp / "config.yaml"
        self.db_row = db_row
        self.validate_exc = validate_exc
        self.save_exc = save_exc
        self.saved = None
        self.seeded = None
        self.regen_called = 0

    def fetch_raw(self):
        return self.db_row

    def validate(self, content):
        if self.validate_exc:
            raise self.validate_exc
        return content

    def save(self, content):
        if self.save_exc:
            raise self.save_exc
        self.saved = content
        self.db_row = {"reachable": True, "content": content, "updated_at": T2}

    def seed_from_file(self, content):
        self.seeded = content
        self.db_row = {"reachable": True, "content": content, "updated_at": T2}

    def regen_config_strict(self):
        self.regen_called += 1


class RunOnceTest(unittest.TestCase):
    """副作用分派：每个 Action 落到正确的操作，失败不影响正在服务的路由。"""

    def _run(self, store, last_seen, reload_exc=None):
        status = SyncStatus()
        reload_mock = mock.AsyncMock(
            side_effect=reload_exc if reload_exc else None, return_value=3)
        with mock.patch.dict(sys.modules, {}), \
             mock.patch("manage.backends_sync._reload_with_lock", reload_mock):
            import manage.backends_sync as bs
            with mock.patch.object(bs, "backends_store", store, create=True), \
                 mock.patch("builtins.__import__", side_effect=__import__):
                # run_once 内部 `from . import backends_store`，用 sys.modules 顶替
                sys.modules["manage.backends_store"] = store
                try:
                    act = asyncio.run(run_once(last_seen, status))
                finally:
                    sys.modules.pop("manage.backends_store", None)
        return act, status, reload_mock

    def test_pull_writes_file_regens_and_reloads(self):
        """DB 变 -> 落盘 + regen + 热重载，生效时间戳被记录。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(A, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": B, "updated_at": T2})
            act, status, reload_mock = self._run(store, LastSeen(T1, content_hash(A)))
            self.assertIs(act, Action.PULL)
            self.assertEqual((tmp / "backends.yaml").read_text(encoding="utf-8"), B)
            self.assertEqual(store.regen_called, 1)
            reload_mock.assert_awaited_once()
            self.assertIsNotNone(status.last_ok_at)
            self.assertIsNone(status.pull_error)

    def test_pull_rejects_invalid_db_content(self):
        """DB 里是坏配置：不碰文件、不热重载，错误进横幅。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(A, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": "bad", "updated_at": T2},
                           validate_exc=ValueError("mapping 指向不存在的模型"))
            act, status, reload_mock = self._run(store, LastSeen(T1, content_hash(A)))
            self.assertIs(act, Action.PULL)
            # 文件没被动，路由没被重载
            self.assertEqual((tmp / "backends.yaml").read_text(encoding="utf-8"), A)
            reload_mock.assert_not_awaited()
            self.assertIn("拒绝应用", status.pull_error)

    def test_push_writes_valid_local_edit_to_db(self):
        """文件变且校验过 -> 回写 DB。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(B, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": A, "updated_at": T1})
            act, status, _ = self._run(store, LastSeen(T1, content_hash(A)))
            self.assertIs(act, Action.PUSH)
            self.assertEqual(store.saved, B)
            self.assertIsNone(status.push_error)

    def test_push_rejects_invalid_local_edit(self):
        """文件变但校验败 -> 不入库，错误进横幅，路由继续跑旧配置。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text("bad", encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": A, "updated_at": T1},
                           save_exc=ValueError("YAML 语法错误"))
            act, status, _ = self._run(store, LastSeen(T1, content_hash(A)))
            self.assertIs(act, Action.PUSH)
            self.assertIsNone(store.saved)
            self.assertIn("拒绝写入", status.push_error)

    def test_overwrite_warns_loudly_and_backs_up(self):
        """双端同变：DB 赢，但本地改动要留档 + 横幅告警（不静默丢弃）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            local = "backends:\n  local_edit: {}\n"
            (tmp / "backends.yaml").write_text(local, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": B, "updated_at": T2})
            act, status, _ = self._run(store, LastSeen(T1, content_hash(A)))
            self.assertIs(act, Action.PULL_OVERWRITE)
            self.assertEqual((tmp / "backends.yaml").read_text(encoding="utf-8"), B)
            # 被覆盖的内容有备份，横幅有告警
            self.assertEqual(
                (tmp / "backends.yaml.overwritten").read_text(encoding="utf-8"), local)
            self.assertIn("已按「数据库为准」覆盖本地改动", status.overwrite_warning)

    def test_reload_failure_keeps_serving_old_router(self):
        """热重载失败：配置已落盘，但要明示路由仍是旧的（不假装成功）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(A, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": B, "updated_at": T2})
            act, status, _ = self._run(store, LastSeen(T1, content_hash(A)),
                                       reload_exc=RuntimeError("router 未初始化"))
            self.assertIs(act, Action.PULL)
            self.assertIn("热重载失败", status.reload_error)
            self.assertFalse(status.as_dict()["healthy"])

    def test_db_down_marks_status_and_does_nothing(self):
        """DB 不可达：状态标记，不做任何写操作。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(A, encoding="utf-8")
            store = _Store(tmp, {"reachable": False, "content": None, "updated_at": None})
            act, status, reload_mock = self._run(store, LastSeen())
            self.assertIs(act, Action.SKIP_DB_DOWN)
            self.assertTrue(status.db_down)
            self.assertIsNone(store.saved)
            reload_mock.assert_not_awaited()

    def test_seed_puts_local_file_into_empty_table(self):
        """首次部署：表里没行 -> 本地文件入库。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "backends.yaml").write_text(A, encoding="utf-8")
            store = _Store(tmp, {"reachable": True, "content": None, "updated_at": None})
            act, status, _ = self._run(store, LastSeen())
            self.assertIs(act, Action.SEED)
            self.assertEqual(store.seeded, A)


class FileStateTest(unittest.TestCase):
    def test_missing_file_reports_not_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(read_file_state(Path(td) / "nope.yaml").exists)

    def test_reads_existing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "backends.yaml"
            p.write_text(A, encoding="utf-8")
            st = read_file_state(p)
            self.assertTrue(st.exists)
            self.assertEqual(st.hash, HA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
