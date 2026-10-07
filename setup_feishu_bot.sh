#!/bin/bash
# 把「RPA 通知机器人」配成 lark-cli 的一个命名配置档（与现有默认应用并存，不覆盖、不切换）。
#
#   ./setup_feishu_bot.sh              交互式：问 App ID / App Secret(不回显) -> 写凭据文件 -> 建配置档（每台电脑只需一次）
#   ./setup_feishu_bot.sh --bootstrap  非交互：配置档已存在就退出 0；不存在则读凭据文件自动创建（月度脚本启动时调用，自愈）
#   ./setup_feishu_bot.sh --check      只检查：凭据文件、配置档、bot 身份状态（不改任何东西）
#
# 凭据文件：~/.config/feishu-rpa-bot.env（权限 600，不入任何仓库），内容形如
#   export FEISHU_RPA_APP_ID=cli_xxx
#   export FEISHU_RPA_APP_SECRET=xxx
#   export FEISHU_RPA_PROFILE=rpa-notify-bot      # 可省，默认 rpa-notify-bot
# 密钥只经 stdin 传给 lark-cli，不出现在进程列表、日志或命令行参数里。
set -euo pipefail

ENV_FILE="${FEISHU_BOT_ENV_FILE:-${HOME}/.config/feishu-rpa-bot.env}"
LARK="${LARK_CLI:-$(command -v lark-cli || echo "${HOME}/.local/bin/lark-cli")}"
MODE="${1:-}"

[ -x "${LARK}" ] || { echo "找不到 lark-cli（可用 LARK_CLI=绝对路径 指定）" >&2; exit 4; }

load_env() {            # 校验权限后读入变量；失败返回 1
  [ -f "${ENV_FILE}" ] || return 1
  local perm; perm="$(stat -f %Lp "${ENV_FILE}" 2>/dev/null || stat -c %a "${ENV_FILE}")"
  if [ "${perm}" != "600" ]; then echo "凭据文件权限是 ${perm}，应为 600：chmod 600 ${ENV_FILE}" >&2; return 2; fi
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  APP_ID="${FEISHU_RPA_APP_ID:-}"; APP_SECRET="${FEISHU_RPA_APP_SECRET:-}"; PROFILE="${FEISHU_RPA_PROFILE:-rpa-notify-bot}"
  [ -n "${APP_ID}" ] && [ -n "${APP_SECRET}" ] || { echo "凭据文件里缺 FEISHU_RPA_APP_ID / FEISHU_RPA_APP_SECRET" >&2; return 3; }
}

profile_json() { "${LARK}" profile list 2>/dev/null | python3 -c 'import sys,json; t=sys.stdin.read(); print(json.dumps(json.loads(t[t.find("["):])))' ; }
profile_exists() { profile_json | python3 -c 'import sys,json; p=sys.argv[1]; print(any(x.get("name")==p for x in json.load(sys.stdin)))' "$1"; }
active_profile() { profile_json | python3 -c 'import sys,json; print(next((x["name"] for x in json.load(sys.stdin) if x.get("active")), ""))'; }

create_profile() {      # 追加命名配置档；若 lark-cli 顺手切换了默认档，切回去
  local before after
  before="$(active_profile)"
  printf '%s' "${APP_SECRET}" | "${LARK}" config init --name "${PROFILE}" --app-id "${APP_ID}" --app-secret-stdin >/dev/null
  after="$(active_profile)"
  if [ -n "${before}" ] && [ "${before}" != "${after}" ]; then "${LARK}" profile use "${before}" >/dev/null; echo "已把默认配置档恢复为 ${before}"; fi
  echo "已创建配置档 ${PROFILE}（默认配置档仍是 ${before:-<无>}）"
}

case "${MODE}" in
  --check)
    if load_env; then echo "凭据文件: OK (${ENV_FILE})"; else echo "凭据文件: 缺失或不可用 (${ENV_FILE})"; PROFILE="${FEISHU_RPA_PROFILE:-rpa-notify-bot}"; fi
    echo "配置档 ${PROFILE} 存在: $(profile_exists "${PROFILE}")"; echo "默认配置档: $(active_profile)"
    "${LARK}" --profile "${PROFILE}" auth status 2>/dev/null | python3 -c 'import sys,json; t=sys.stdin.read(); j=json.loads(t[t.find("{"):]); print("bot 身份:", j.get("identities",{}).get("bot",{}).get("status"))' 2>/dev/null || echo "bot 身份: 无法读取"
    ;;
  --bootstrap)
    PROFILE="${FEISHU_RPA_PROFILE:-rpa-notify-bot}"
    if load_env 2>/dev/null; then :; fi
    if [ "$(profile_exists "${PROFILE}")" = "True" ]; then exit 0; fi
    load_env || { echo "本机没有配置档 ${PROFILE}，且凭据文件不可用：请先运行 $(dirname "$0")/setup_feishu_bot.sh" >&2; exit 3; }
    create_profile
    ;;
  "")
    mkdir -p "$(dirname "${ENV_FILE}")"
    read -r -p "App ID（cli_ 开头）: " APP_ID </dev/tty
    [ -n "${APP_ID}" ] || { echo "App ID 不能为空" >&2; exit 1; }
    read -r -s -p "App Secret（输入不回显）: " APP_SECRET </dev/tty; echo
    read -r -p "配置档名 [rpa-notify-bot]: " PROFILE </dev/tty; PROFILE="${PROFILE:-rpa-notify-bot}"
    [ -n "${APP_SECRET}" ] || { echo "密钥不能为空" >&2; exit 1; }
    ( umask 077
      { echo "# 飞书「RPA 通知机器人」凭据（lark-cli 命名配置档用）。🔴 含密钥，权限 600，绝不提交进任何仓库"
        echo "export FEISHU_RPA_APP_ID=${APP_ID}"
        echo "export FEISHU_RPA_APP_SECRET=${APP_SECRET}"
        echo "export FEISHU_RPA_PROFILE=${PROFILE}"; } > "${ENV_FILE}" )
    chmod 600 "${ENV_FILE}"; echo "已写入 ${ENV_FILE}（权限 600）"
    if [ "$(profile_exists "${PROFILE}")" = "True" ]; then echo "配置档 ${PROFILE} 已存在，不重复创建（如需换密钥：lark-cli profile remove ${PROFILE} 后重跑）"; else create_profile; fi
    "$0" --check
    ;;
  *) echo "用法: $0 [--bootstrap|--check]" >&2; exit 1;;
esac
