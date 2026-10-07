#!/usr/bin/env python3
"""写入链路的故障注入测试（离线，不碰飞书/官网/待办中心）。
用内存里的假表模拟网络抖动，验证：不重复写、不漏写、该失败时失败且不乱写。
运行：python3 test_resilience.py     （全部通过退出码 0）
"""
import json
import sys
import tempfile
from pathlib import Path

import boc_fx_monthly as m

m.time.sleep = lambda *_a, **_k: None           # 不真等待
m.notify = lambda *a, **k: None                  # 不弹系统通知
m.ensure_profile = lambda cfg: None              # 不碰 lark-cli 配置档

CODES = sorted(m.USED)
PAGE = [[n, "100", "100", "101", "101", "100.5", "2026/11/01", "12:00:00"] for n in m.NAME2CODE]
OCT = [{"更新时间": "2026-10-01 22:56:15", "code": c, "midprice": 100.5} for c in CODES]  # 上一批，价格相同 => 不触发偏差保护


class FakeTodo:                                  # 待办中心：只记录调用
    enabled = True
    id = None
    key = "boc-fx-monthly-2026-11-run"
    def __init__(self, *a, **k): self.calls = []
    def start(self): self.calls.append("start")
    def succeed(self, t): self.calls.append("succeed")
    def fail(self, r): self.calls.append("fail")
    def reply(self, t, key=None): self.calls.append("reply_warn")


class FakeLark:
    """scenario 决定 create/export 的行为；self.rows 是"服务端真实状态"。"""
    scenario = None
    preload = 0                                     # 预置多少行"当月已存在"的数据
    def __init__(self, cfg):
        self.identity = "bot"
        type(self).rows = [dict(r) for r in OCT]
        for c in CODES[:type(self).preload]:
            type(self).rows.append({"更新时间": "2026-11-01 12:00:05", "code": c, "midprice": 100.5, "银行编码": ["BOC"]})
        type(self).create_calls = 0
        type(self).export_calls = 0

    def export(self, fields):
        type(self).export_calls += 1
        sc = type(self).scenario
        if sc == "export_dies_after_create_error" and type(self).create_calls >= 1 and type(self).export_calls > 2:
            raise m.Fail("lark-cli 调用失败 +record-list: EOF")
        return [dict(r) for r in type(self).rows]

    def _commit(self, recs, n=None):
        for r in (recs if n is None else recs[:n]):
            type(self).rows.append({**{k: v for k, v in r.items() if k != "银行编码"}, "银行编码": ["BOC"]})

    def create(self, recs):
        cls = type(self); cls.create_calls += 1; sc = cls.scenario; k = cls.create_calls
        if sc == "clean":
            self._commit(recs); return ["r"] * len(recs)
        if sc == "eof_after_server_processed":          # 最危险：服务端已写入，客户端却收到 EOF
            self._commit(recs); raise m.Fail("lark-cli 调用失败: Post ...: EOF")
        if sc == "eof_before_server_twice":             # 前两次请求没到服务端，第三次成功
            if k < 3: raise m.Fail("lark-cli 调用失败: EOF")
            self._commit(recs); return ["r"] * len(recs)
        if sc == "always_eof":
            raise m.Fail("lark-cli 调用失败: EOF")
        if sc == "partial_then_eof":                    # 只写进了 10 行就断了
            self._commit(recs, 10); raise m.Fail("lark-cli 调用失败: EOF")
        if sc == "no_permission":
            raise m.Fail("lark-cli 调用失败 +record-batch-create: code 91403 you don't have permission")
        if sc == "export_dies_after_create_error":
            self._commit(recs); raise m.Fail("lark-cli 调用失败: EOF")
        raise AssertionError(sc)


def run(scenario, preload=0, state=None):
    stp = Path(tempfile.mkdtemp()) / "state.json"
    if state is not None:
        stp.write_text(json.dumps(state))
    m.STATE = stp
    FakeLark.preload = preload
    cfgp = Path(tempfile.mkdtemp()) / "c.json"
    cfgp.write_text(json.dumps({"base_token": "x", "table_id": "y"}))
    FakeLark.scenario = scenario
    todos = []
    m.Lark = FakeLark
    m.fetch_boc = lambda: PAGE
    m.RunTodo = lambda *a, **k: (todos.append(FakeTodo()) or todos[-1])
    sys.argv = ["x", "--today", "2026-11-01", "--config", str(cfgp)]
    rc = m.main()
    nov = [r for r in FakeLark.rows if r["更新时间"].startswith("2026-11")]
    result = json.loads((m.HERE / "last_run.json").read_text())["result"]
    return rc, result, len(nov), len({r["code"] for r in nov}), FakeLark.create_calls, todos[0].calls


def check(name, got, want):
    rc, result, rows, uniq, creates, todo = got
    ok = (rc, result, rows, uniq) == want[:4] and (len(want) < 5 or creates == want[4])
    print(f"{'✓' if ok else '✗'} {name}\n    退出码={rc} 结果={result} 当月行数={rows}(去重{uniq}) create调用={creates} 待办={todo}")
    return ok


if __name__ == "__main__":
    bak = (m.HERE / "last_run.json").read_text() if (m.HERE / "last_run.json").exists() else None
    res = [
        check("A 正常写入", run("clean"), (0, "ok", 24, 24, 1)),
        check("B 服务端已写入但客户端收到 EOF（最危险）→ 不得重复写", run("eof_after_server_processed"), (0, "ok", 24, 24, 1)),
        check("C 前两次请求没到服务端、第三次成功 → 重试补上，不漏写", run("eof_before_server_twice"), (0, "ok", 24, 24, 3)),
        check("D 三次全失败 → 失败收场，不乱写", run("always_eof"), (1, "failed", 0, 0, 3)),
        check("E 只写进 10 行就断了 → 不得补写成重复，必须报错交人", run("partial_then_eof"), (1, "failed", 10, 10, 1)),
        check("F 没有权限(91403) → 不重试、直接失败", run("no_permission"), (1, "failed", 0, 0, 1)),
        check("G 写入时 EOF 且随后查询也连续失败 → 失败收场（数据其实已写入，待办保持打开）",
              run("export_dies_after_create_error"), (1, "failed", 24, 24, 1)),
        # ---- 2 号补跑收尾（修复"写入成功但回读失败 → 1 号失败待办一直开着"的缺口）----
        check("H 当月批次齐全 + 上次待办还开着 → 回复并自动结单，不再写入",
              run("clean", preload=24, state={"2026-11": {"todo_id": "T1", "status": "open"}}), (0, "skipped_exists", 24, 24, 0)),
        check("I 当月批次齐全 + 待办早已结单 → 静默跳过，不打扰",
              run("clean", preload=24, state={"2026-11": {"todo_id": "T1", "status": "closed"}}), (0, "skipped_exists", 24, 24, 0)),
        check("J 当月只有 10 行 + 待办还开着 → 只回复警告、保持打开、不补写",
              run("clean", preload=10, state={"2026-11": {"todo_id": "T1", "status": "open"}}), (0, "skipped_exists", 10, 10, 0)),
        check("K 当月批次齐全但本机没有待办记录 → 静默跳过",
              run("clean", preload=24, state=None), (0, "skipped_exists", 24, 24, 0)),
    ]
    if bak is not None:
        (m.HERE / "last_run.json").write_text(bak)
    print(f"\n{sum(res)}/{len(res)} 通过")
    sys.exit(0 if all(res) else 1)
