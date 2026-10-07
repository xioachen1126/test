#!/bin/bash
# 安装/卸载「每月 1 号 12:00(补跑 2 号)」本机定时任务（macOS launchd 用户级，无需 sudo）。
#   ./install_launchd.sh            安装并加载
#   ./install_launchd.sh --pilot    试跑模式：装一个一次性任务，用 --dry-run --force 跑一遍后自动卸载
#   ./install_launchd.sh --uninstall
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LABEL="com.local.boc-fx-monthly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UIDN="$(id -u)"
mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"

unload() { launchctl bootout "gui/$UIDN/$1" 2>/dev/null || true; }

case "${1:-}" in
  --uninstall) unload "$LABEL"; rm -f "$PLIST"; echo "已卸载 $LABEL"; exit 0;;
  --pilot)
    L="$LABEL.pilot"; P="$HOME/Library/LaunchAgents/$L.plist"
    cat > "$P" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$L</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/boc_fx_monthly.py</string><string>--dry-run</string><string>--force</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string><key>HOME</key><string>$HOME</string></dict>
  <key>StandardOutPath</key><string>$DIR/logs/pilot.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/pilot.log</string>
</dict></plist>
EOF
    : > "$DIR/logs/pilot.log"; unload "$L"
    launchctl bootstrap "gui/$UIDN" "$P"; launchctl kickstart -k "gui/$UIDN/$L"
    for i in $(seq 1 60); do sleep 2; grep -q -E "dry-run|ERROR|跳过" "$DIR/logs/pilot.log" && break; done
    cat "$DIR/logs/pilot.log"; unload "$L"; rm -f "$P"; echo "试跑任务已卸载"; exit 0;;
esac

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/boc_fx_monthly.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string><key>HOME</key><string>$HOME</string></dict>
  <key>StartCalendarInterval</key><array>
    <dict><key>Day</key><integer>1</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Day</key><integer>2</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$DIR/logs/run.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/run.log</string>
</dict></plist>
EOF
plutil -lint "$PLIST"; unload "$LABEL"; launchctl bootstrap "gui/$UIDN" "$PLIST"
launchctl print "gui/$UIDN/$LABEL" | grep -E "state|next|calendar|program" | head
echo "已安装 $LABEL：每月 1 号 12:00 运行，2 号 12:00 补跑（当月已有批次则自动跳过）"
