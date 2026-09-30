#!/bin/bash
# KnowledgeFlow v0.4 · macOS Apple Silicon 构建脚本
#
#   clean → 取内嵌 Python runtime → 装依赖 → 组装 .app → 签名 → 打 DMG
#
# 产物：dist/KnowledgeFlow-macOS-arm64.dmg
#
# 设计取舍（说明写在代码里，免得下次有人"优化"掉）：
#
# 1. **不重写后端**。.app 里的 backend/ 就是仓库里那份代码，原样拷进去。
#    打包层只做「内嵌解释器 + 环境变量映射 + 启动」，业务一行不动。
# 2. **不内嵌 ffmpeg**。实测这个项目**不调用 ffmpeg CLI** —— 音视频解码走 PyAV
#    （它的 wheel 自带 ffmpeg 库）。所以「系统没装 ffmpeg」不该拦人。
# 3. **可选能力单独装**：ASR/OCR 的依赖装失败只降级、不中断构建
#    （定稿第十九节）。用 --minimal 可以只装核心，做个更小的包。
# 4. **ad-hoc 签名**（codesign -s -）。没有 Developer ID 就不要假装有：
#    首次运行会看到「无法验证开发者」，README 里写清怎么放行，
#    **不要求用户关闭系统安全功能**（定稿第十六节）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$REPO_ROOT/packaging/macos"
BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"
APP_NAME="KnowledgeFlow"
APP="$BUILD_DIR/$APP_NAME.app"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/backend/pyproject.toml" | head -1)"
VERSION="${VERSION:-0.4.0}"

# python-build-standalone：可重定位的独立 Python，适合塞进 .app
PBS_TAG="20260929"
PBS_PYTHON="3.13.15"
PBS_ASSET="cpython-${PBS_PYTHON}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
# 下载缓存放在仓库外：这样 --clean 不会把 24MB 的 runtime 反复重下
CACHE_DIR="${HOME}/Library/Caches/KnowledgeFlow-build"

WITH_MEDIA=1
MEDIA_STATUS="未包含（构建中断）"
DO_DMG=1

die() { printf '\n[失败] %s\n' "$1" >&2; exit 1; }
step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal)    WITH_MEDIA=0 ;;
    --no-dmg)     DO_DMG=0 ;;
    --clean)      rm -rf "$BUILD_DIR"; printf '[已清理] %s\n' "$BUILD_DIR" ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "未知参数：$1（可用：--minimal --no-dmg --clean）" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
step "0/8 环境检查"
# --------------------------------------------------------------------------- #
[ "$(uname -s)" = "Darwin" ] || die "这个脚本只能在 macOS 上跑"
[ "$(uname -m)" = "arm64" ] || die "v0.4 只支持 Apple Silicon（当前架构：$(uname -m)）"
for tool in curl tar rsync sips iconutil hdiutil codesign plutil; do
  command -v "$tool" >/dev/null 2>&1 || die "缺少系统工具：$tool"
done
printf '  架构 %s · macOS %s · 版本 %s\n' "$(uname -m)" "$(sw_vers -productVersion)" "$VERSION"

# --------------------------------------------------------------------------- #
step "1/8 清理构建目录"
# --------------------------------------------------------------------------- #
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# --------------------------------------------------------------------------- #
step "2/8 准备内嵌 Python runtime"
# --------------------------------------------------------------------------- #
mkdir -p "$CACHE_DIR"
if [ ! -f "$CACHE_DIR/$PBS_ASSET" ]; then
  printf '  下载 %s\n' "$PBS_ASSET"
  curl -fL --retry 3 -o "$CACHE_DIR/$PBS_ASSET.part" "$PBS_URL"
  mv "$CACHE_DIR/$PBS_ASSET.part" "$CACHE_DIR/$PBS_ASSET"
else
  printf '  命中缓存 %s\n' "$CACHE_DIR/$PBS_ASSET"
fi
TMP_EXTRACT="$BUILD_DIR/.extract"
mkdir -p "$TMP_EXTRACT"
tar -xzf "$CACHE_DIR/$PBS_ASSET" -C "$TMP_EXTRACT"
mv "$TMP_EXTRACT/python" "$BUILD_DIR/runtime"
rmdir "$TMP_EXTRACT"
[ -x "$BUILD_DIR/runtime/bin/python3" ] || die "runtime 里没有 bin/python3，下载可能不完整"
"$BUILD_DIR/runtime/bin/python3" -V | sed 's/^/  /'

# --------------------------------------------------------------------------- #
step "3/8 安装核心依赖"
# --------------------------------------------------------------------------- #
RUNTIME_PY="$BUILD_DIR/runtime/bin/python3"
# 构建期的网络是唯一的单点故障：明确给重试与超时（默认 15 秒很脆）。
#
# 国内常见情况：索引 pypi.org 能通，但**包文件主机 files.pythonhosted.org 被墙**
# （表现为 SSL: UNEXPECTED_EOF，然后 pip 报 "No matching distribution found"，
#  看起来像包不存在，其实是下载不了）。这时用镜像：
#
#   KF_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ./scripts/build_macos_arm64.sh
PIP_FLAGS=(--disable-pip-version-check --no-warn-script-location -q
           --timeout 60 --retries 8)
if [ -n "${KF_PIP_INDEX_URL:-}" ]; then
  PIP_FLAGS+=(--index-url "$KF_PIP_INDEX_URL")
  printf '  pip 源：%s\n' "$KF_PIP_INDEX_URL"
fi
"$RUNTIME_PY" -m pip install "${PIP_FLAGS[@]}" -r "$PKG_DIR/requirements-app-core.txt"

# --------------------------------------------------------------------------- #
step "4/8 安装可选能力（ASR / OCR）"
# --------------------------------------------------------------------------- #
if [ "$WITH_MEDIA" = "1" ]; then
  # 定稿第十九节：装不上只降级、不中断构建 —— 用户仍能用链接/文本/手动粘贴。
  #
  # **但降级必须如实记账**：早先版本这里只看命令行开关，
  # 结果可选依赖装失败时 BUILD-INFO 仍写「已包含 ASR+OCR」，
  # 而 .app 里根本没有那两个模块 —— 交付物在说谎。现在用变量记录真实结果。
  if "$RUNTIME_PY" -m pip install "${PIP_FLAGS[@]}" \
       -r "$PKG_DIR/requirements-app-media.txt"; then
    MEDIA_STATUS="已包含：ASR(faster-whisper) + OCR(pytesseract)"
    printf '  已装：本地语音转写（faster-whisper）+ OCR（pytesseract）\n'
    printf '  注：OCR 的实际引擎是系统里的 tesseract，未安装时自动停用\n'
  else
    MEDIA_STATUS="未包含（可选依赖安装失败）—— 应用照常可用，ASR/OCR 显示未启用"
    printf '  \033[33m[警告] 可选能力未装成功 —— 应用照常可用，ASR/OCR 会显示「未启用」\033[0m\n'
    printf '  \033[33m        重跑一次构建通常就好了（构建期网络是唯一的单点故障）\033[0m\n'
  fi
else
  MEDIA_STATUS="未包含（--minimal）"
  printf '  --minimal：跳过可选能力\n'
fi

# 复核：说「已包含」就必须真的能 import，不能只信 pip 的返回码
if [ "$WITH_MEDIA" = "1" ]; then
  for module in faster_whisper av pytesseract PIL; do
    if ! "$RUNTIME_PY" -c "import $module" >/dev/null 2>&1; then
      MEDIA_STATUS="未包含（复核失败：$module 导不进来）—— ASR/OCR 显示未启用"
      printf '  \033[33m[警告] 复核发现 %s 缺失，按「未包含」记账\033[0m\n' "$module"
      break
    fi
  done
fi

# 清掉测试与缓存，只留运行时需要的东西（体积与攻击面都小一圈）
"$RUNTIME_PY" - <<'PY'
import pathlib, shutil, sys
site = pathlib.Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
removed = 0
for pattern in ("**/tests", "**/test", "**/__pycache__"):
    for path in site.glob(pattern):
        if path.is_dir() and "site-packages" in str(path):
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
print(f"  清掉 {removed} 个测试/缓存目录")
PY

# --------------------------------------------------------------------------- #
step "5/8 组装 $APP_NAME.app"
# --------------------------------------------------------------------------- #
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

rsync -a "$BUILD_DIR/runtime/" "$APP/Contents/Resources/runtime/"
rsync -a "$PKG_DIR/launcher/" "$APP/Contents/Resources/launcher/"

# 后端原样拷进去（排除测试/数据/凭据）
mkdir -p "$APP/Contents/Resources/backend"
rsync -a \
  --exclude 'tests/' --exclude '.pytest_tmp/' --exclude '__pycache__/' \
  --exclude '*.py[co]' --exclude '.pytest_cache/' --exclude 'data/' \
  --exclude '.env' --exclude '*.db' --exclude '*.sqlite3' \
  "$REPO_ROOT/backend/" "$APP/Contents/Resources/backend/"

# 配置模板也带上：出问题时对照用
rsync -a "$PKG_DIR/requirements-app-core.txt" "$PKG_DIR/requirements-app-media.txt" \
  "$APP/Contents/Resources/packaging/"

# Info.plist（替换版本号）
sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$VERSION/" \
  "$PKG_DIR/Info.plist" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null || die "Info.plist 不合法"

# 图标：1024 母版 → iconset → icns（sips / iconutil 都是系统自带）
ICONSET="$BUILD_DIR/AppIcon.iconset"
mkdir -p "$ICONSET"
MASTER="$PKG_DIR/assets/AppIcon-1024.png"
[ -f "$MASTER" ] || die "缺少图标母版：$MASTER"
for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
            "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" \
            "512 icon_256x256@2x" "512 icon_512x512" "1024 icon_512x512@2x"; do
  set -- $spec
  sips -z "$1" "$1" "$MASTER" --out "$ICONSET/$2.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# 可执行入口：把 python 换上去，信号能直达启动器（exec）
cat > "$APP/Contents/MacOS/$APP_NAME" <<'LAUNCHER'
#!/bin/bash
# .app 的入口。exec 换进程 —— 这样 Dock 右键「退出」发的 SIGTERM
# 会直接到达 Python 启动器，它据此把后端子进程收干净（不留孤儿）。
# 路径一律用 ${VAR} 花括号形式：$VAR 后面紧跟中文时 bash 会把中文字节
# 当成变量名的一部分（本项目踩过一次，有测试钉住这个写法）。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$(cd "${HERE}/../Resources" && pwd)"
exec "${RES}/runtime/bin/python3" "${RES}/launcher/run.py" "$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# 启动器入口脚本
cat > "$APP/Contents/Resources/launcher/run.py" <<'ENTRY'
""".app 的真正入口：把 launcher 目录加进 sys.path 后交给 kf_app.main。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kf_app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
ENTRY

# --------------------------------------------------------------------------- #
step "6/8 签名（ad-hoc）"
# --------------------------------------------------------------------------- #
codesign --force --deep --sign - "$APP" 2>&1 | sed 's/^/  /' || true
if codesign --verify --deep --strict "$APP" 2>/dev/null; then
  printf '  ✓ 签名校验通过（ad-hoc）\n'
else
  printf '  \033[33m[警告] 签名校验未通过 —— 应用通常仍可运行，但首次打开提示可能更强\033[0m\n'
fi

# --------------------------------------------------------------------------- #
step "7/8 冒烟测试（用打包好的 runtime 真的跑一次）"
# --------------------------------------------------------------------------- #
printf '  用 .app 内的解释器与后端做环境自检：\n'
if "$APP/Contents/MacOS/$APP_NAME" --check; then
  printf '  ✓ 自检通过\n'
else
  die "冒烟测试失败：打包出来的 .app 起不来"
fi

# --------------------------------------------------------------------------- #
step "8/8 生成 DMG"
# --------------------------------------------------------------------------- #
DMG="$DIST_DIR/${APP_NAME}-macOS-arm64.dmg"
if [ "$DO_DMG" = "1" ]; then
  STAGE="$BUILD_DIR/dmg"
  mkdir -p "$STAGE"
  rsync -a "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"   # 拖拽安装的标准摆法
  hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"
  printf '  ✓ %s（%s）\n' "$DMG" "$(du -h "$DMG" | cut -f1)"
else
  printf '  --no-dmg：跳过\n'
fi

# 构建信息：交付时一眼能看清「这个包是什么、怎么来的」
cat > "$DIST_DIR/BUILD-INFO.txt" <<INFO
KnowledgeFlow $VERSION · macOS Apple Silicon
构建时间：$(date '+%Y-%m-%d %H:%M:%S')
构建机：$(uname -m) / macOS $(sw_vers -productVersion)
内嵌 Python：$("$APP/Contents/Resources/runtime/bin/python3" -V 2>&1)
可选能力：$MEDIA_STATUS
签名：ad-hoc（codesign -s -），未做 Developer ID 与公证
.app 体积：$(du -sh "$APP" | cut -f1)
DMG：$([ "$DO_DMG" = "1" ] && du -h "$DMG" | cut -f1 || echo "已跳过")
数据目录：~/Library/Application Support/KnowledgeFlow/
INFO

printf '\n\033[1m构建完成\033[0m\n'
[ "$DO_DMG" = "1" ] && printf '  安装包：%s\n' "$DMG"
printf '  .app  ：%s\n' "$APP"
printf '  构建信息：%s\n' "$DIST_DIR/BUILD-INFO.txt"
