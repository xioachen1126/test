#!/usr/bin/env python3
"""每月 1 号：抓中国银行外汇牌价 -> 写入飞书多维表 -> 回读核对。

设计要点（都来自探路实测，见 探路记录.md）：
  * 用应用身份(bot)写飞书：用户令牌刷新期只有约 7 天，月度任务必然过期。
  * lark-cli 经常偶发 EOF：每次调用重试；写入失败后先查"是否其实已写入"，防重复。
  * 防重复：当月已有批次就跳过（幂等）；月初 1 号没跑成，2 号补跑一次。
  * 校验在写入之前：币种必须 24 个齐全、折算价为正数、发布日期是当天、与上一批偏差过大即中止。
  * 价格口径：每 100 外币兑人民币；财务用 折算价÷100（飞书表公式列自动算）。

退出码：0 成功或按规则跳过；1 校验失败/写入失败/核对不一致（会弹系统通知）。
"""
import argparse
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
UA = "Mozilla/5.0"
CA = "/etc/ssl/cert.pem"  # macOS 自带 Python 缺根证书；用系统证书文件，保持校验开启
CTX = ssl.create_default_context(cafile=CA) if Path(CA).exists() else ssl.create_default_context()

# 中行页面币种中文名 -> ISO 代码（只列公司用到的 24 种）
NAME2CODE = {
    "澳大利亚元": "AUD", "巴西雷亚尔": "BRL", "加拿大元": "CAD", "瑞士法郎": "CHF",
    "丹麦克朗": "DKK", "欧元": "EUR", "英镑": "GBP", "港币": "HKD", "日元": "JPY",
    "韩国元": "KRW", "哈萨克斯坦坚戈": "KZT", "澳门元": "MOP", "林吉特": "MYR",
    "挪威克朗": "NOK", "新西兰元": "NZD", "菲律宾比索": "PHP", "巴基斯坦卢比": "PKR",
    "卢布": "RUB", "瑞典克朗": "SEK", "新加坡元": "SGD", "泰国铢": "THB",
    "新台币": "TWD", "美元": "USD", "南非兰特": "ZAR",
}
NAME_OVERRIDE = {"KRW": "韩元"}  # 飞书表历史上一直叫「韩元」
USED = set(NAME2CODE.values())
PRICE_FIELDS = ["midprice", "cashbuyprice", "forexbuyprice", "cashsellprice", "forexsellprice"]

ALLOWED_DAYS = {1, 2}      # 1 号正常跑；2 号只在 1 号没写成时补跑
WARN_PCT, ABORT_PCT = 10.0, 30.0


class Fail(Exception):
    pass


def log(msg, level="INFO"):
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} [{level}] {msg}", flush=True)


def notify(title, text):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(text, ensure_ascii=False)} '
                        f'with title {json.dumps(title, ensure_ascii=False)}'],
                       timeout=10, check=False)
    except Exception:  # noqa: BLE001
        pass


STATE = HERE / "state.json"   # 每月待办状态 {"2026-11": {"todo_id": "...", "status": "open|closed"}}，供 2 号补跑收尾


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(ym, **kv):
    try:
        st = load_state()
        st.setdefault(ym, {}).update(kv)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE)
    except Exception as e:  # noqa: BLE001
        log(f"写状态文件失败（忽略）：{e!r}", "WARN")


class RunTodo:
    """一次运行 = 一条待办（待办中心「来源待办」，发起人恒为「业务部门自动化」，收件人为默认负责人）。
    开始：建单（回跳按钮指向飞书外汇表）；成功：回复结果并 todo_source_close 自动结单；
    失败：回复原因并**保持打开**（需要人介入）。待办中心任何失败都只记日志，绝不影响主流程退出码。"""
    DEFAULT_SOURCE = "https://todo.weshedait.com/"

    def __init__(self, ym, enabled, trial=False, source_url=None):
        self.ym, self.enabled, self.trial = ym, enabled, trial
        self.source_url = source_url or self.DEFAULT_SOURCE
        self.id = None
        stamp = datetime.now(TZ).strftime("%Y%m%d%H%M%S")
        self.key = f"boc-fx-monthly-trial-{stamp}" if trial else f"boc-fx-monthly-{ym}-run"

    def _tn(self):
        sys.path.insert(0, str(HERE))
        import todo_notify
        return todo_notify

    def _call(self, tool, args):
        tn = self._tn()
        cfg = tn.load_config()
        if not cfg:
            raise RuntimeError("缺少待办中心配置(~/.config/todo-center.env)")
        if cfg.get("sysref"):
            args["onBehalfOf"] = cfg["sysref"]
        return tn._call_tool(cfg, tool, args)

    def start(self):
        if not self.enabled or self.id:
            return
        try:
            tag = "【试运行】" if self.trial else ""
            title = f"{tag}【外汇月度任务】{self.ym} 取中行牌价写飞书表"
            text = ("定时任务已启动：抓取中国银行外汇牌价 → 校验 24 个币种 → 写入飞书多维表 → 回读逐字段核对。\n"
                    "全部核对通过后会在本待办回复结果并自动结单；失败则保持打开并写明原因，需要人介入。\n"
                    + ("（本条为试运行：只取数校验，不写入飞书表。）" if self.trial else ""))
            r = self._tn().send_todo(title, text, source_url=self.source_url,
                                     idempotency_key=self.key, append_host_time=False)
            self.id = r.get("id")
            log(f"待办中心：已建单 {self.id or '(同键已存在)'}")
            if self.id and not self.trial:
                save_state(self.ym, todo_id=self.id, status="open")
        except Exception as e:  # noqa: BLE001
            log(f"待办中心建单失败（忽略，不影响任务）：{str(e)[:160]}", "WARN")

    def reply(self, text, key=None):
        if not (self.enabled and self.id):
            return
        try:
            a = {"id": self.id, "text": text[:1500]}
            if key:
                a["idempotencyKey"] = key
            self._call("todo_reply", a)
        except Exception as e:  # noqa: BLE001
            log(f"待办中心回复失败（忽略）：{str(e)[:160]}", "WARN")

    def succeed(self, text):
        """回复结果 + 自动结单（来源平台自关，跳过确认卡）。"""
        if not (self.enabled and self.id):
            return
        self.reply(text, key=f"{self.key}-ok")
        try:
            self._call("todo_source_close", {"id": self.id, "reason": "回读核对通过，自动结单",
                                             "idempotencyKey": f"{self.key}-close"})
            log("待办中心：已自动结单")
            if not self.trial:
                save_state(self.ym, status="closed")
        except Exception as e:  # noqa: BLE001
            log(f"待办中心自动结单失败（待办保持打开，请人工确认）：{str(e)[:160]}", "WARN")

    def fail(self, reason):
        if not self.enabled:
            return
        self.start()
        self.reply(f"❌ 失败：{reason[:600]}\n\n需要人介入：按案例 SOP 第 5 节故障表排查；"
                   f"日志 {HERE}/logs/run.log，状态 {HERE}/last_run.json。本待办保持打开。\n"
                   f"2 号 12:00 会自动补跑一次（当月已有批次则跳过）；补跑成功会在此回复并自动结单。")


# ---------- 取数 ----------
def http_get(url, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise Fail(f"抓取失败 {url}: {last}")


def parse_boc(html):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) == 8 and c[0] and c[0] != "&nbsp;":
            rows.append(c)
    return rows


def fetch_boc():
    first = http_get(BOC_URL + "index.html")
    m = re.search(r"createPage\((\d+)", first)
    pages = int(m.group(1)) if m else 1
    rows = parse_boc(first)
    for p in range(1, pages):
        rows += parse_boc(http_get(f"{BOC_URL}index_{p}.html"))
        time.sleep(0.5)
    if not rows:
        raise Fail("没有解析到任何牌价，页面结构可能改版")
    return rows


def num(s):
    s = (s or "").strip()
    return float(s) if s else None


def build_records(rows, today):
    """页面列序：币种 现汇买 现钞买 现汇卖 现钞卖 折算价 发布日期 发布时间"""
    by_code = {}
    for name, fb, cb, fs, cs, mid, ts, _ in rows:
        code = NAME2CODE.get(name)
        if code:
            by_code[code] = (name, fb, cb, fs, cs, mid, ts)
    missing = sorted(USED - set(by_code))
    if missing:
        raise Fail(f"官网缺少应有的币种: {missing}（官网可能下架了币种，需人工确认）")
    recs, stamps = [], set()
    for code in sorted(USED):
        name, fb, cb, fs, cs, mid, ts = by_code[code]
        rec = {"银行编码": ["BOC"], "code": code, "name": NAME_OVERRIDE.get(code, name),
               "更新时间": ts.replace("/", "-"),
               "midprice": num(mid), "cashbuyprice": num(cb), "forexbuyprice": num(fb),
               "cashsellprice": num(cs), "forexsellprice": num(fs)}
        if not rec["midprice"] or rec["midprice"] <= 0:
            raise Fail(f"{code} 折算价无效: {mid!r}")
        recs.append({k: v for k, v in rec.items() if v is not None})
        stamps.add(ts[:10])
    if len(stamps) != 1:
        raise Fail(f"各币种发布日期不一致: {sorted(stamps)}")
    page_day = datetime.strptime(stamps.pop(), "%Y/%m/%d").date()
    if (today - page_day).days not in (0, 1):
        raise Fail(f"官网牌价日期 {page_day} 与今天 {today} 相差过大，疑似页面未更新")
    return recs


# ---------- 飞书 ----------
class Lark:
    def __init__(self, cfg):
        self.cli = cfg.get("lark_cli", "lark-cli")
        self.base = cfg["base_token"]
        self.table = cfg["table_id"]
        self.identity = cfg.get("identity", "bot")
        self.profile = cfg.get("profile")  # lark-cli 命名配置档（如「RPA 通知机器人」）；不填则用默认档

    def run(self, args, cwd=None, retries=4):
        pre = ["--profile", self.profile] if self.profile else []
        cmd = [self.cli, *pre, "base", *args, "--base-token", self.base, "--table-id", self.table,
               "--as", self.identity]
        last = ""
        for i in range(retries):
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=180)
            out = p.stdout.strip()
            try:
                j = json.loads(out[out.find("{"):]) if "{" in out else {}
            except json.JSONDecodeError:
                j = {}
            if p.returncode == 0 and (j.get("ok", True) is not False):
                return out, j
            last = (out or p.stderr)[-400:]
            err = (j.get("error") or {})
            if err.get("type") not in (None, "network"):  # 非网络错误(权限/参数)重试没用
                break
            time.sleep(4 * (i + 1))
        raise Fail(f"lark-cli 调用失败 {args[0]}: {last}")

    def export(self, fields):
        """导出全表指定字段，返回记录列表。"""
        with tempfile.TemporaryDirectory() as d:
            a = ["+record-list", "--format", "ndjson", "--output", "./x.ndjson"]
            for f in fields:
                a += ["--field-id", f]
            out, j = self.run(a, cwd=d)
            f = Path(d) / "x.ndjson"
            rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
            if j.get("has_more"):
                raise Fail("导出被截断(has_more)，表超过 2000 行，需要改成按月过滤导出")
            return rows

    def create(self, recs):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8",
                                         dir=str(HERE)) as f:
            json.dump({"create_records": recs}, f, ensure_ascii=False)
            name = Path(f.name).name
        try:
            out, j = self.run(["+record-batch-create", "--json", f"@{name}"], cwd=str(HERE), retries=1)
            return (j.get("data") or {}).get("record_id_list", [])
        finally:
            (HERE / name).unlink(missing_ok=True)


def ensure_profile(cfg):
    """配置档自愈：本机没有指定的 lark-cli 配置档时，用凭据文件(~/.config/feishu-rpa-bot.env)自动创建。"""
    if not cfg.get("profile"):
        return
    sh = HERE / "setup_feishu_bot.sh"
    env = {**__import__("os").environ, "LARK_CLI": cfg.get("lark_cli", "lark-cli"),
           "FEISHU_RPA_PROFILE": cfg["profile"]}
    p = subprocess.run([str(sh), "--bootstrap"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env)
    if p.returncode != 0:
        raise Fail(f"飞书配置档 {cfg['profile']} 不可用：{(p.stderr or p.stdout).strip()[:300]}")
    if p.stdout.strip():
        log(p.stdout.strip())


def probe_write(lark):
    """写一行带标记的测试记录并立刻删除，验证写权限；成功后核对表行数不变。"""
    before = len(lark.export(["code"]))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8", dir=str(HERE)) as f:
        json.dump({"create_records": [{"code": "__TEST__", "name": "权限探测-请忽略"}]}, f, ensure_ascii=False)
        name = Path(f.name).name
    try:
        _, j = lark.run(["+record-batch-create", "--json", f"@{name}"], cwd=str(HERE), retries=3)
    finally:
        (HERE / name).unlink(missing_ok=True)
    ids = (j.get("data") or {}).get("record_id_list", [])
    if not ids:
        raise Fail("探测写入没有返回 record_id")
    for rid in ids:
        lark.run(["+record-delete", "--record-id", rid, "--yes"])
    after = len(lark.export(["code"]))
    if after != before:
        raise Fail(f"探测后表行数 {before}->{after}，测试行可能没删干净，请人工检查 code=__TEST__ 的行")
    log(f"写权限探测通过：写入并删除 1 行测试记录，表行数保持 {before}")


def month_of(ts):
    return ts[:7] if ts else ""


def rows_in_month(rows, ym):
    return [r for r in rows if month_of(r.get("更新时间")) == ym]


def compare_prev(rows, recs):
    """与上一批（最近一个有数据的批次）比折算价，返回(警告列表, 中止列表)。"""
    dated = [r for r in rows if r.get("更新时间") and r.get("code") and r.get("midprice") and r["code"] != "RMB"]
    if not dated:
        return [], []
    last_ts = max(r["更新时间"] for r in dated)
    prev = {r["code"]: r["midprice"] for r in dated if r["更新时间"][:10] == last_ts[:10]}
    warns, aborts = [], []
    for r in recs:
        p = prev.get(r["code"])
        if not p:
            continue
        pct = abs(r["midprice"] - p) / p * 100
        if pct > ABORT_PCT:
            aborts.append(f"{r['code']} {p}->{r['midprice']} ({pct:.1f}%)")
        elif pct > WARN_PCT:
            warns.append(f"{r['code']} {p}->{r['midprice']} ({pct:.1f}%)")
    return warns, aborts


def verify(lark, recs, ym):
    rows = lark.export(["更新时间", "code", "银行编码"] + PRICE_FIELDS)
    got = {r["code"]: r for r in rows_in_month(rows, ym)}
    if len(got) != len(recs):
        raise Fail(f"回读核对失败：当月行数 {len(got)} != 预期 {len(recs)}")
    bad = []
    for r in recs:
        g = got.get(r["code"])
        if not g:
            bad.append(f"{r['code']} 缺失")
            continue
        for k in PRICE_FIELDS:
            a, b = g.get(k), r.get(k)
            if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9):
                bad.append(f"{r['code']}.{k}: 表={a} 预期={b}")
    if bad:
        raise Fail("回读核对不一致: " + "; ".join(bad[:8]))
    dup = len(rows_in_month(rows, ym)) - len(got)
    if dup:
        raise Fail(f"回读发现当月重复行 {dup} 条，需人工处理")


def reconcile_existing(todo, ym, have):
    """本月已有批次而跳过时：若本月的运行待办还开着（例如 1 号写入成功但回读/通知时网络断了），
    核对批次是否齐全，齐全则回复并自动结单，不齐则回复警告并保持打开。没有待办记录则静默。"""
    st = load_state().get(ym) or {}
    if not (todo.enabled and st.get("todo_id") and st.get("status") == "open"):
        return
    todo.id = st["todo_id"]
    codes = [r.get("code") for r in have]
    missing = sorted(USED - set(codes))
    dup = len(codes) - len(set(codes))
    no_mid = sum(1 for r in have if not r.get("midprice"))
    if not missing and not dup and not no_mid and set(codes) == USED:
        log("补跑收尾：当月批次齐全，收掉上一次遗留的待办")
        todo.succeed(f"✅ 补跑核对：上一次运行报失败，但飞书表里 {ym} 批次已存在且完整——24 个币种齐全、无重复行、折算价均有值。"
                     f"（注意：本次只核对完整性，未与官网逐值比对；如需逐值核对请人工抽查。）已自动结单。")
    else:
        log(f"补跑收尾：当月批次不完整（缺 {missing}、重复 {dup}、无折算价 {no_mid}），保持待办打开", "WARN")
        todo.reply(f"⚠️ 补跑核对：{ym} 批次存在但不完整——缺币种 {missing}、重复行 {dup}、无折算价 {no_mid}。"
                   f"本待办保持打开，请人工核对（不会自动补写，避免重复）。", key=f"{todo.key}-reconcile-warn")


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.local.json"))
    ap.add_argument("--dry-run", action="store_true", help="只取数+校验+检查飞书，不写入")
    ap.add_argument("--force", action="store_true", help="忽略日期保护与当月已有批次的跳过规则")
    ap.add_argument("--probe-write", action="store_true", help="验证 bot 写权限：写一行测试记录并立刻删除，然后退出")
    ap.add_argument("--today", help="仅测试用：假装今天是 YYYY-MM-DD")
    ap.add_argument("--simulate-fail", action="store_true", help="仅配合 --todo-trial：流程中途故意失败，演练失败分支(待办回复原因并保持打开)")
    ap.add_argument("--todo-trial", action="store_true", help="试运行待办生命周期：建一条【试运行】待办→跑流程→自动结单；配合 --dry-run --force，不写飞书表")
    args = ap.parse_args()

    now = datetime.now(TZ)
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else now.date()
    ym = f"{today:%Y-%m}"
    status = {"time": now.isoformat(timespec="seconds"), "dry_run": args.dry_run}
    # 待办生命周期：真实运行默认开；干跑/探测/测试日期默认关；--todo-trial 显式开（试运行）
    todo = RunTodo(ym, enabled=(not (args.probe_write or args.today) and (not args.dry_run or args.todo_trial)),
                   trial=args.todo_trial)
    try:
        if today.day not in ALLOWED_DAYS and not args.force and not args.probe_write:
            log(f"今天是 {today.day} 号，不在运行日 {sorted(ALLOWED_DAYS)}，跳过")
            status["result"] = "skipped_not_run_day"
            return 0
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if cfg.get("source_url"):
            todo.source_url = cfg["source_url"]
        ensure_profile(cfg)
        lark = Lark(cfg)
        if args.probe_write:
            probe_write(lark)
            status["result"] = "probe_ok"
            return 0

        existing = lark.export(["更新时间", "code", "midprice"])
        have = rows_in_month(existing, ym)
        log(f"飞书连通 OK（身份 {lark.identity}），表内 {len(existing)} 行，本月已有 {len(have)} 行")
        if have and not args.force:
            log(f"{ym} 已有批次，按幂等规则跳过")
            reconcile_existing(todo, ym, have)
            status["result"] = "skipped_exists"
            return 0

        todo.start()  # 确认本月确需运行后立即建单（回跳按钮指向飞书外汇表）
        recs = build_records(fetch_boc(), today)
        log(f"官网取到并通过校验：{len(recs)} 个币种，发布时间 {recs[0]['更新时间']}")
        if args.simulate_fail and args.todo_trial:
            raise Fail("【演练】模拟失败：用于验证失败分支（待办应写明原因并保持打开）")
        warns, aborts = compare_prev(existing, recs)
        for w in warns:
            log(f"与上一批偏差 >{WARN_PCT:.0f}%：{w}", "WARN")
        if aborts:
            raise Fail(f"与上一批偏差 >{ABORT_PCT:.0f}%，疑似数据异常，中止：" + "; ".join(aborts))
        if today.day != 1:
            log(f"注意：今天是 {today.day} 号（补跑），写入的更新时间是真实发布时间而非 1 号", "WARN")
            if not args.dry_run:
                notify("外汇月度任务", f"{today.day} 号补跑：1 号没写成，已用当天牌价补写，请确认口径")

        if args.dry_run:
            log(f"[dry-run] 将写入 {len(recs)} 行，示例：{json.dumps(recs[0], ensure_ascii=False)}")
            status["result"] = "dry_run_ok"
            todo.succeed(f"🧪 试运行通过：官网 {len(recs)} 个币种取数并通过全部校验，飞书连通正常（dry-run，未写入飞书表）。")
            return 0

        for attempt in range(1, 4):
            try:
                ids = lark.create(recs)
                log(f"写入接口返回 {len(ids)} 个 record_id")
                break
            except Fail as e:
                if "91403" in str(e) or "permission" in str(e).lower():
                    raise Fail("bot 没有这张表的写权限（91403）：请把应用加为表格协作者(可编辑)，见 SOP「首次授权」") from e
                log(f"第 {attempt} 轮写入报错（{str(e)[:160]}）；先查是否其实已写入，防止重复", "WARN")
                time.sleep(3)
                if rows_in_month(lark.export(["更新时间", "code"]), ym):
                    log("发现当月批次已存在，转入核对")
                    break
                if attempt == 3:
                    raise
                time.sleep(10 * attempt)
        verify(lark, recs, ym)
        log(f"回读核对通过：{len(recs)} 行与官网数据一致，无重复")
        status["result"] = "ok"
        status["rows"] = len(recs)
        todo.succeed(f"✅ 完成：{len(recs)} 个币种已写入飞书表「一键获取汇率（接口）」，回读逐字段核对一致、无重复行；"
                     f"官网发布时间 {recs[0]['更新时间']}。")
        return 0
    except Fail as e:
        log(str(e), "ERROR")
        status["result"] = "failed"
        status["error"] = str(e)[:300]
        notify("外汇月度任务失败", str(e)[:120])
        todo.fail(str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"未预期的错误: {e!r}", "ERROR")
        status["result"] = "crashed"
        status["error"] = repr(e)[:300]
        notify("外汇月度任务异常", repr(e)[:120])
        todo.fail(repr(e))
        return 1
    finally:
        (HERE / "last_run.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
