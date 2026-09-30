#!/bin/bash
# 把 KnowledgeFlow 后端装成 **开机自启** 的 LaunchAgent（macOS）。
#
# 为什么需要它：Chrome 扩展要「点一下就完事」，前提是本地服务一直活着。
# 每次手动跑 serve.py 的话，扩展用起来就跟没装一样。
#
# 用法：
#   ./scripts/install_autostart.sh --print       # 只打印将要写入的 plist（不写、不装）
#   ./scripts/install_autostart.sh --install     # 写入并立即启动
#   ./scripts/install_autostart.sh --status      # 看它是不是活着
#   ./scripts/install_autostart.sh --uninstall   # 停掉并移除
#
# 安全边界（和 serve.py 一致）：只绑 127.0.0.1，不对外暴露；
# 服务本身没有鉴权，暴露到局域网等于把「写你 Obsidian 库、用你 LLM 额度」
# 的接口开放出去 —— 所以这里**刻意不传 --expose**。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND_ROOT/.." && pwd)"

LABEL="cn.knowledgeflow.serve"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/KnowledgeFlow"
PORT="8000"

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
SERVE="$BACKEND_ROOT/scripts/serve.py"

die() { printf '[失败] %s\n' "$1" >&2; exit 1; }

# 用哪个 python：优先仓库自己的 venv（依赖都装在那儿）。
# 没有 venv 时退到系统 python3 —— 这样 `--print` / `--status` 在**全新 clone**
# 上也能用（CI 里就是这个状态），只有 `--install` 才强制要求 venv。
pick_python() {
  if [ -x "$VENV_PYTHON" ]; then printf '%s' "$VENV_PYTHON"; return; fi
  command -v python3 || printf '%s' "$VENV_PYTHON"
}

require_venv() {
  [ -x "$VENV_PYTHON" ] || die "找不到虚拟环境里的 python：${VENV_PYTHON}（先在仓库根建 .venv 并装依赖）"
  [ -f "$SERVE" ] || die "找不到 serve.py：${SERVE}"
}

# 注意：变量一律写成 ``${VAR}``。``$VAR（`` 这种「变量名后面紧跟中文」的写法
# 在 ``set -u`` 下会把中文字节当成变量名的一部分（实测报 ``VENV_PYTHON\xef:
# unbound variable``），而且只在那个分支真被执行时才炸 —— 本地有 .venv 时
# 永远走不到，等到干净 clone 才发现。测试里有一条静态守卫钉死这个写法。
PYTHON="$(pick_python)"

plist_content() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SERVE}</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BACKEND_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/serve.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/serve.err</string>
</dict>
</plist>
PLIST
}

do_print() { plist_content; }

do_install() {
  require_venv
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  plist_content > "$PLIST"
  printf '[已写入] %s\n' "$PLIST"

  # 先 bootout 再 bootstrap：重复安装时不这么做会报 "service already loaded"。
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    # 实测：在某些「非登录会话」的上下文里（ssh / 自动化工具起的 shell）
    # launchctl 一律回 `Bootstrap failed: 5: Input/output error`，
    # 连一个只跑 /bin/sleep 的最小 plist 也一样 —— 那是**环境问题，不是 plist 有问题**。
    # 这种时候别假装成功：plist 已经落盘，下次登录会自动生效，
    # 想立刻生效就在**自己的终端**里再跑一次（那里有完整的 launchd 会话）。
    printf '[未立即启动] launchctl bootstrap 被当前环境拒绝（error 5）\n'
    printf '  plist 已经写好，**下次登录后会自动运行**；想现在就生效，\n'
    printf '  请在**你自己的终端**里执行：\n'
    printf '    %s --install\n' "${BASH_SOURCE[0]}"
    printf '  （或者注销再登录一次）\n'
    return 0
  fi

  printf '  日志：%s/serve.log\n' "$LOG_DIR"
  sleep 2
  do_status || true
}

do_uninstall() {
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  printf '[已移除] %s\n' "$PLIST"
}

do_status() {
  if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    printf '[运行中] %s\n' "$LABEL"
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      printf '  http://127.0.0.1:%s/api/health → ok\n' "$PORT"
    else
      printf '  进程在，但接口没响应 —— 看 %s/serve.err\n' "$LOG_DIR"
      return 1
    fi
  else
    printf '[未运行] %s（用 --install 装）\n' "$LABEL"
    return 1
  fi
}

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  --print)     do_print ;;
  --install)   do_install ;;
  --uninstall) do_uninstall ;;
  --status)    do_status ;;
  --port)      PORT="${2:?--port 需要一个端口号}"; shift 2; exec "${BASH_SOURCE[0]}" "$@" ;;
  --help|-h|"") usage ;;
  *)           usage; exit 2 ;;
esac
