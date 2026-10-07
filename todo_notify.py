#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
往待办中心（todo.weshedait.com）建一条待办——以平台系统身份「业务部门自动化」发起、派给真人。

两种用法（影刀 / 命令行都行）:

  ① 影刀「Python 模块」调用（推荐）:
       import todo_notify
       r = todo_notify.send_todo("标题", "正文")                    # 派给默认收件人
       r = todo_notify.send_todo("标题", "正文", owner_name="张三")   # 派给别人（按中文名解析）
       r = todo_notify.send_todo("标题", "正文", urgent=True)        # 真推飞书加急，慎用
       # 成功返回 {"ok": True, "id": "<内部id>", "idempotencyKey": "..."}；失败抛 TodoError
       r = todo_notify.send_todo_safe("标题", "正文")               # 不抛异常版：失败回 {"ok": False, "error": "..."}

  ② 命令行 / 影刀「运行命令」:
       python3 todo_notify.py "标题" "正文" [--owner-name 张三] [--urgent] [--source-url URL] [--due 2026-10-01T18:00:00+08:00]
       python3 todo_notify.py --test           # 发一条测试待办（真单）
       python3 todo_notify.py --dry-run "标题" "正文"   # 只打印将发出的请求参数，不联网
       python3 todo_notify.py --whoami         # 自检：这把 Key 是谁、身份解析通不通（只读）
     stdout 恒输出一行 JSON（{"ok":true,"id":...} 或 {"ok":false,"error":...}），退出码 0 成功、1 发送失败、2 缺配置。

配置来源（按优先级，任选其一，都不写进本仓库）:
  1. 环境变量:
       TODO_CENTER_URL      MCP 端点（生产 https://todo.weshedait.com/api/mcp，dev 是 todo-dev）
       TODO_CENTER_KEY      API Key（tdw_ 开头）
       TODO_CENTER_ACTOR    请求头 X-On-Behalf-Of 的身份 ref（lk_ 开头；不是中文名、不是飞书 open_id）
       TODO_CENTER_SYSREF   【本版新增，显式发起身份】系统身份「业务部门自动化」的 ref（lk_ 开头）
       TODO_CENTER_OWNER    【本版新增，默认收件人】真人 ref（lk_ 开头）；不配则按 OWNER_NAME 中文名解析
  2. 配置文件 $TODO_CENTER_ENV_FILE（默认 ~/.config/todo-center.env；Windows 上就是 %USERPROFILE%\\.config\\todo-center.env），
     内容 `K=v` 或 `export K="v"` 两种写法都吃。建议权限 600。
  3. 影刀里也可以直接把配置字典传进函数: send_todo(..., config={"url":..., "key":..., "actor":..., "sysref":..., "owner":...})
     🔴 Key 值只在影刀的凭据/变量里配，不写进流程截图、日志、对话。

发起身份怎么落到「业务部门自动化」（本版与旧版的差别）:
  · 旧版：写操作不传 onBehalfOf，靠服务端回落到请求头 X-On-Behalf-Of。缺点是 ~/.config/todo-center.env 是多项目共用的，
    ACTOR 被别的项目改成真人 ref 后，发起人会静默换人（2026-09-21 实撞）。
  · 本版：配了 TODO_CENTER_SYSREF 就**显式传 onBehalfOf=SYSREF**，不再受 ACTOR 漂移影响（照 amazon-sellersprite-reviews.sh
    的 notify_human 做法）。没配 SYSREF 则退回旧行为（不传，靠请求头回落），完全向后兼容。
  · 服务端硬约束（实测 400）：系统身份只能当 onBehalfOf，不能当 owner / 旁听人。owner 必须是真人 ref。

协议细节（握手/会话/SSE 解析/错误分层）照抄自 connect-to-todo-center skill 的 references/client.ts 与 playbook.md。
HTTP 优先走 curl 子进程：本机（Mac）TLS 链有中间证书，Python 自带 CA bundle 不认（README「坑位一」），
urllib/requests 会报 CERTIFICATE_VERIFY_FAILED，不要"优化"回去。只有在找不到 curl 可执行文件时（部分 Windows 环境）
才退回 urllib。Python 3.7+ 可跑（影刀内置 Python 版本偏旧，别用海象、新式类型标注）。
"""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

ALLOWED_URLS = {
    "https://todo.weshedait.com/api/mcp",
    "https://todo-dev.weshedait.com/api/mcp",
}
OWNER_NAME = "王维"  # 默认收件人（未配 TODO_CENTER_OWNER、也没传 owner 时按此中文名解析）
CLIENT_NAME = "boc-fx-monthly"  # MCP 握手时的客户端自报名，仅用于服务端日志
REQUEST_TIMEOUT_S = 15
IDEMPOTENCY_PREFIX = "boc-fx-monthly"  # 幂等键前缀；不同业务脚本可改，避免跨脚本撞键


class TodoError(Exception):
    pass


# ---------------------------------------------------------------- 配置

def _safe(msg, cfg):
    """错误文本里绝不拼密钥：剥掉 key/actor/sysref 的值。"""
    if not cfg or not msg:
        return msg
    out = str(msg)
    for k in ("key", "actor", "sysref"):
        v = cfg.get(k)
        if v:
            out = out.replace(v, "***")
    return out


def _parse_env_file(path):
    """兼容 `K=v` 与 `export K="v"`（该文件多项目共用，两种写法都出现过）。"""
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k:
                out[k] = v
    return out


_ENV_KEYS = {
    "url": "TODO_CENTER_URL",
    "key": "TODO_CENTER_KEY",
    "actor": "TODO_CENTER_ACTOR",
    "sysref": "TODO_CENTER_SYSREF",
    "owner": "TODO_CENTER_OWNER",
}


def load_config(override=None):
    """
    合并三层：显式传入的 override > 环境变量 > env 文件。
    url/key/actor 三件齐全且 URL 命中白名单才返回 dict，否则 None（调用方据此降级）。
    sysref / owner 可选。
    """
    merged = {}
    env_file = os.environ.get(
        "TODO_CENTER_ENV_FILE", os.path.join(os.path.expanduser("~"), ".config", "todo-center.env")
    )
    if os.path.isfile(env_file):
        try:
            file_vals = _parse_env_file(env_file)
        except Exception as e:
            print("todo_notify: 读配置文件失败：%s" % e, file=sys.stderr)
            file_vals = {}
        for k, env_name in _ENV_KEYS.items():
            if file_vals.get(env_name):
                merged[k] = file_vals[env_name].strip()
    for k, env_name in _ENV_KEYS.items():
        v = os.environ.get(env_name, "").strip()
        if v:
            merged[k] = v
    if override:
        for k in _ENV_KEYS:
            v = override.get(k)
            if v:
                merged[k] = str(v).strip()

    if not (merged.get("url") and merged.get("key") and merged.get("actor")):
        return None
    if merged["url"] not in ALLOWED_URLS:
        print("todo_notify: TODO_CENTER_URL 不在白名单，拒绝调用", file=sys.stderr)
        return None
    for k in ("actor", "sysref", "owner"):
        v = merged.get(k)
        if v and (not v.startswith("lk_") or any(ord(c) > 0x7E or ord(c) < 0x20 for c in v)):
            print("todo_notify: %s 必须是 lk_ 开头的 ASCII 身份 ref（填中文名会被编码成乱码后 403）" % _ENV_KEYS[k],
                  file=sys.stderr)
            return None
    return merged


# ---------------------------------------------------------------- HTTP

def _headers(cfg, session_id):
    h = {
        "X-API-Key": cfg["key"],
        "X-On-Behalf-Of": cfg["actor"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _post_curl(cfg, payload, session_id):
    cmd = ["curl", "-sS", "-i", "--max-time", str(REQUEST_TIMEOUT_S), "-X", "POST"]
    for k, v in _headers(cfg, session_id).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    cmd += ["-d", json.dumps(payload, ensure_ascii=False), cfg["url"]]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=REQUEST_TIMEOUT_S + 5)
    except FileNotFoundError:
        raise  # 交给上层退回 urllib
    except Exception as e:
        raise TodoError("待办中心连不上（含超时）：%s" % _safe(str(e), cfg))
    if out.returncode != 0:
        raise TodoError("待办中心连不上：curl exit=%s %s"
                        % (out.returncode, _safe(out.stderr.decode("utf-8", "replace").strip(), cfg)))
    raw = out.stdout.decode("utf-8", "replace")
    if not raw.strip():
        raise TodoError("待办中心无响应")
    blocks = raw.split("\r\n\r\n") if "\r\n\r\n" in raw else raw.split("\n\n")
    # 100-continue 会多一组头：取最后一组以 HTTP/ 开头的块当真正的响应头
    hdr_idx = 0
    for i, b in enumerate(blocks):
        if b.lstrip().startswith("HTTP/"):
            hdr_idx = i
    header_block = blocks[hdr_idx]
    body = "\n\n".join(blocks[hdr_idx + 1:]).strip()
    header_lines = header_block.replace("\r\n", "\n").splitlines()
    status = 0
    if header_lines:
        parts = header_lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    sid = None
    for line in header_lines:
        if line.lower().startswith("mcp-session-id:"):
            sid = line.split(":", 1)[1].strip()
    return status, sid, body


def _post_urllib(cfg, payload, session_id):
    import urllib.request
    import urllib.error
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=data, method="POST", headers=_headers(cfg, session_id))

    class _NoRedirect(urllib.request.HTTPRedirectHandler):  # 防带 Key 的请求被 3xx 带走
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=REQUEST_TIMEOUT_S)
        status, hdrs, body = resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, hdrs, body = e.code, e.headers, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise TodoError("待办中心连不上（含超时）：%s" % _safe(str(e), cfg))
    return status, hdrs.get("Mcp-Session-Id"), body


def _post(cfg, payload, session_id=None):
    try:
        return _post_curl(cfg, payload, session_id)
    except FileNotFoundError:
        return _post_urllib(cfg, payload, session_id)


def _parse_body(text):
    """SSE 形（data: 行内是 JSON，取最后一条）或普通 JSON；解析不动回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    try:
        if t.startswith("{"):
            return json.loads(t)
        data_lines = [l for l in t.splitlines() if l.startswith("data:")]
        if not data_lines:
            return None
        return json.loads(data_lines[-1][len("data:"):].strip())
    except Exception:
        return None


# ---------------------------------------------------------------- MCP 会话

_session = {"id": None, "cfg_key": None}


def _handshake(cfg):
    _session["id"] = None
    status, sid, body_text = _post(cfg, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": CLIENT_NAME, "version": "2"}},
    })
    body = _parse_body(body_text)
    if status >= 400 or (body and body.get("error")):
        msg = ((body or {}).get("error") or {}).get("message", "")
        raise TodoError("待办中心握手失败（HTTP %s）%s" % (status, ("：" + _safe(msg, cfg)) if msg else ""))
    if not body or "result" not in body:
        raise TodoError("待办中心握手响应无法解析（HTTP %s）" % status)
    _session["id"] = sid  # 生产端点可能不下发会话 ID：拿到就带，拿不到照发
    _session["cfg_key"] = cfg["key"]
    try:
        _post(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid)
    except Exception:
        pass  # 202 空响应，不致命


def _is_session_invalid(status, body):
    """真会话失效判据。401/403/429 不算。"""
    if status in (401, 403, 429):
        return False
    msg = (((body or {}).get("error") or {}).get("message") or "").lower()
    return "session" in msg and any(k in msg for k in ("invalid", "expired", "not found", "missing"))


def _call_tool(cfg, name, args):
    if _session["cfg_key"] != cfg["key"]:  # 首次或换了 Key 才握手；生产可能不下发会话 ID，不能拿 id 当判据
        _handshake(cfg)
    retried_session = False
    retried_429 = False
    while True:
        status, sid, body_text = _post(
            cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
            session_id=_session["id"],
        )
        if sid:
            _session["id"] = sid
        body = _parse_body(body_text)

        if _is_session_invalid(status, body) and not retried_session:
            retried_session = True
            _handshake(cfg)
            continue
        if status == 429 and not retried_429:
            retried_429 = True
            time.sleep(1)
            continue
        if status >= 400 or (body and body.get("error")):
            msg = ((body or {}).get("error") or {}).get("message", "HTTP %s" % status)
            raise TodoError("待办中心 %s 失败（HTTP %s）：%s" % (name, status, _safe(msg, cfg)))

        result = (body or {}).get("result") or {}
        content = result.get("content") or []
        text = (content[0].get("text", "") or "") if content else ""
        if result.get("isError"):
            raise TodoError("待办中心 %s 失败：%s" % (name, _safe(text, cfg)))
        if not result or not text.strip():
            raise TodoError("待办中心 %s 返回了空的或无法解析的响应（HTTP %s）" % (name, status))
        try:
            return json.loads(text)
        except Exception:
            return {"_text": text}


_person_cache = {}


def resolve_person(cfg, name):
    """中文名 → 身份 ref。重名 conflict 是永久失败（服务端拒写），查无此人可稍后重试。"""
    if name in _person_cache:
        return _person_cache[name]
    r = _call_tool(cfg, "todo_resolve_person", {"name": name}) or {}
    if r.get("conflict"):
        raise TodoError("待办中心「%s」重名（conflict），服务端拒写，需人工消重" % name)
    matches = r.get("matches") or []
    ref = matches[0].get("ref") if matches else None
    if not ref:
        raise TodoError("待办中心解析不出「%s」的身份引用" % name)
    _person_cache[name] = ref
    return ref


def whoami(cfg):
    return _call_tool(cfg, "todo_whoami", {})


# ---------------------------------------------------------------- 建单

def _normalize_idempotency_key(k):
    s = (k or "").strip()
    if not s:
        raise TodoError("幂等键不能为空")
    if len(s) <= 128 and all(0x20 <= ord(c) <= 0x7E for c in s):
        return s
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def build_create_args(cfg, title, text, owner_ref, source_url=None, due_at=None, idempotency_key=None):
    """
    组 todo_create 的 arguments。幂等键缺省 = 完整请求体的 hash，与请求体严格一一对应
    （同体同键 → 重试不重复建单；异体异键 → 新事件建新单）。
    🔴 别改成只 hash 标题再拼时间桶：同键不同体会被服务端 409 拒执行（2026-09-18 实撞）。
    """
    args = {"text": text, "title": title, "owner": owner_ref}
    if source_url:
        args["sourceUrl"] = source_url  # 带上才是「来源待办」，程序以后才能 todo_source_close
    if due_at:
        args["dueAt"] = due_at
    if cfg.get("sysref"):
        # 显式发起身份 = 系统身份「业务部门自动化」。不配则不传，靠请求头 X-On-Behalf-Of 回落（旧行为）。
        args["onBehalfOf"] = cfg["sysref"]
    if idempotency_key:
        args["idempotencyKey"] = _normalize_idempotency_key(idempotency_key)
    else:
        digest = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        args["idempotencyKey"] = _normalize_idempotency_key("%s-%s" % (IDEMPOTENCY_PREFIX, digest))
    return args


def _pick_owner(cfg, owner_ref, owner_name):
    if owner_ref:
        if not owner_ref.startswith("lk_"):
            raise TodoError("owner_ref 必须是 lk_ 开头的身份 ref，不是中文名")
        return owner_ref
    if owner_name:
        return resolve_person(cfg, owner_name)
    if cfg.get("owner"):
        return cfg["owner"]
    return resolve_person(cfg, OWNER_NAME)


def send_todo(title, text, owner_ref=None, owner_name=None, urgent=False,
              source_url=None, due_at=None, idempotency_key=None, config=None, append_host_time=True):
    """
    建一条待办。成功返回 {"ok": True, "id": 内部id, "idempotencyKey": ...}，失败抛 TodoError。
      owner_ref   收件人 ref（lk_）；优先级最高
      owner_name  收件人中文名，内部解析成 ref
      urgent      同时标加急（真推飞书加急，慎用）
      source_url  回跳链接（https、weshedait.com 域下、≤512 字），带上程序以后才能自关
      due_at      截止时间，ISO 8601 带时区，如 2026-10-01T18:00:00+08:00
      idempotency_key  自定义幂等键（≤128 可见 ASCII，否则自动 sha256 归一）；缺省按请求体 hash。
                       🔴 缺省键 + 正文自带秒级时间 = 每次触发都是新单；要「同一件事只发一条」就传稳定键
                       （如 bizauto-<业务主键>-<状态>-<日期>），撞 409 会按 {"ok": True, "duplicate": True} 返回
      config      直接传配置 dict（影刀里从凭据/变量取值时用）
      append_host_time  正文末尾追加「主机 / 时间」两行（默认开）
    """
    cfg = load_config(config)
    if not cfg:
        raise TodoError("缺少待办中心配置：需要 TODO_CENTER_URL / TODO_CENTER_KEY / TODO_CENTER_ACTOR"
                        "（环境变量、~/.config/todo-center.env 或 config 参数）")
    if append_host_time:
        try:
            host = socket.gethostname() or "unknown"
        except Exception:
            host = "unknown"
        text = "%s\n\n主机: %s\n时间: %s" % (text, host, time.strftime("%Y-%m-%d %H:%M:%S"))

    owner = _pick_owner(cfg, owner_ref, owner_name)
    args = build_create_args(cfg, title, text, owner, source_url, due_at, idempotency_key)
    try:
        result = _call_tool(cfg, "todo_create", args)
    except TodoError as e:
        # 自定义稳定键 + 正文变了（比如带时间戳）→ 服务端 409「同键不同体，本次未执行」。
        # 对「一件事只发一条」的用法这就是"已发过"，按成功返回并打 duplicate 标记，别让 RPA 流程报错。
        if idempotency_key and "409" in str(e):
            return {"ok": True, "id": None, "duplicate": True, "idempotencyKey": args["idempotencyKey"]}
        raise
    thread_id = (result or {}).get("id")
    if not thread_id:
        raise TodoError("待办中心 todo_create 返回里没有 id")
    out = {"ok": True, "id": thread_id, "idempotencyKey": args["idempotencyKey"]}
    if urgent:
        try:
            _call_tool(cfg, "todo_urgent", {"id": thread_id})
            out["urgent"] = True
        except Exception as e:
            out["urgent"] = False
            out["urgentError"] = _safe(str(e), cfg)
            print("todo_notify: 标加急失败（待办已建成，忽略）：%s" % out["urgentError"], file=sys.stderr)
    return out


def send_todo_safe(*a, **kw):
    """不抛异常版，给影刀流程判断用：失败回 {"ok": False, "error": "..."}。"""
    try:
        return send_todo(*a, **kw)
    except TodoError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": _safe(str(e), load_config(kw.get("config")))}


# ---------------------------------------------------------------- CLI

def _mask_refs(obj):
    """CLI 输出里把 lk_ 身份 ref 打码（只留末 4 位），与「actor 值不进日志」的纪律一致。"""
    if isinstance(obj, dict):
        return dict((k, _mask_refs(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_mask_refs(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("lk_") and len(obj) > 8:
        return "lk_…" + obj[-4:]
    return obj


def _emit(obj, code):
    print(json.dumps(_mask_refs(obj), ensure_ascii=False))
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description="以「业务部门自动化」身份往待办中心建一条待办")
    ap.add_argument("title", nargs="?", default="业务自动化通知")
    ap.add_argument("body", nargs="?", default="（无正文）")
    ap.add_argument("--owner-name", help="收件人中文名（缺省用配置里的 TODO_CENTER_OWNER 或 %s）" % OWNER_NAME)
    ap.add_argument("--owner-ref", help="收件人身份 ref（lk_ 开头），优先于 --owner-name")
    ap.add_argument("--source-url", help="回跳链接（带上程序以后才能自关）")
    ap.add_argument("--due", help="截止时间 ISO 8601，如 2026-10-01T18:00:00+08:00")
    ap.add_argument("--idempotency-key", help="自定义幂等键")
    ap.add_argument("--urgent", action="store_true", help="同时标加急（真会推飞书提醒）")
    ap.add_argument("--test", action="store_true", help="发一条测试待办（真单）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发出的 todo_create 参数，不联网")
    ap.add_argument("--whoami", action="store_true", help="自检：Key 是谁、身份能否解析（只读，不建单）")
    args = ap.parse_args()

    if args.test:
        title = "业务部门自动化 · 通道测试"
        body = "这是一条测试待办，用于验证自动化脚本 → 待办中心链路是否打通。收到即表示配置正确。"
    else:
        title, body = args.title, args.body

    cfg = load_config()
    if not cfg:
        _emit({"ok": False, "error": "缺少待办中心配置。请设置 TODO_CENTER_URL/KEY/ACTOR，"
                                     "或用 scripts/setup_todo.sh 写入 ~/.config/todo-center.env"}, 2)

    try:
        if args.whoami:
            me = whoami(cfg)
            info = {"ok": True, "whoami": me, "sysrefConfigured": bool(cfg.get("sysref")),
                    "ownerConfigured": bool(cfg.get("owner"))}
            if cfg.get("sysref"):
                info["sysrefResolvesTo"] = _call_tool(cfg, "todo_resolve_person", {"name": "业务部门自动化"})
            _emit(info, 0)
        if args.dry_run:
            owner = args.owner_ref or cfg.get("owner") or "<待解析:%s>" % (args.owner_name or OWNER_NAME)
            preview = build_create_args(cfg, title, body, owner, args.source_url, args.due, args.idempotency_key)
            if preview.get("onBehalfOf"):
                preview["onBehalfOf"] = "<SYSREF 已配置，显式传>"
            _emit({"ok": True, "dryRun": True, "todo_create": preview, "urgent": args.urgent}, 0)
        r = send_todo(title, body, owner_ref=args.owner_ref, owner_name=args.owner_name, urgent=args.urgent,
                      source_url=args.source_url, due_at=args.due, idempotency_key=args.idempotency_key)
        _emit(r, 0)
    except TodoError as e:
        _emit({"ok": False, "error": str(e)}, 1)
    except Exception as e:
        _emit({"ok": False, "error": _safe(str(e), cfg)}, 1)


if __name__ == "__main__":
    main()
