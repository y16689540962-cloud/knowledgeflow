"""路径解析：.app 内部资源 与 用户数据目录，**两者严格分离**。

定稿第七节要求：用户数据绝不能写进 ``Contents/Resources/`` —— 因为
.app 会被替换、被移动到回收站、被代码签名校验；把数据库写进去迟早出事
（签名失效、系统更新时被清、用户拖到别处就丢数据）。

所以：

    程序（只读、随 .app 走）  Contents/Resources/
    数据（可写、独立）        ~/Library/Application Support/KnowledgeFlow/

``data_home()`` 走 ``Path.home()``，因此把 ``HOME`` 指到别处就能模拟一台
全新 Mac —— 端到端测试就是靠这一点做的（见 ``tests/test_macos_packaging.py``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: 用户数据目录名（macOS 惯例：~/Library/Application Support/<产品名>）
DATA_HOME_DIRNAME: Final[str] = "KnowledgeFlow"

#: 支持的最低 macOS 版本，与 Info.plist 的 LSMinimumSystemVersion 保持一致。
#: 12.0 是 arm64 机器上仍能跑 python-build-standalone 的保守下限。
MIN_MACOS_VERSION: Final[tuple[int, int]] = (12, 0)


@dataclass(frozen=True)
class AppPaths:
    """一次解析出来的所有路径。"""

    #: .app 包内的 ``Contents/``
    contents: Path
    #: ``Contents/Resources/``（只读）
    resources: Path
    #: ``Contents/Resources/runtime/``（内嵌 Python）
    runtime: Path
    #: ``Contents/Resources/backend/``（原样的后端代码）
    backend: Path

    #: ``~/Library/Application Support/KnowledgeFlow/``
    home: Path
    config: Path
    data: Path
    logs: Path
    cache: Path
    run: Path

    @property
    def runtime_python(self) -> Path:
        return self.runtime / "bin" / "python3"

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.data / "knowledgeflow.db"

    @property
    def media_dir(self) -> Path:
        return self.data / "media"

    @property
    def app_log(self) -> Path:
        return self.logs / "app.log"

    @property
    def pid_file(self) -> Path:
        return self.run / "service.json"


def data_home(home: Path | None = None) -> Path:
    """用户数据根目录。``home`` 可注入 —— 测试用它模拟干净机器。"""
    base = home if home is not None else Path.home()
    return base / "Library" / "Application Support" / DATA_HOME_DIRNAME


def find_app_paths(launcher_file: Path | None = None, *, home: Path | None = None) -> AppPaths:
    """从本文件位置向上找到 ``Contents/``，再拼出全部路径。

    找不到 ``Contents/``（例如从源码目录直接跑启动器做开发）时，
    退化成「仓库布局」：``Resources`` 指向仓库根、``runtime`` 指向当前解释器
    所在的目录。这样开发期也能跑同一条代码路径。
    """
    here = (launcher_file or Path(__file__)).resolve()
    contents = _find_contents(here)

    if contents is not None:
        resources = contents / "Resources"
        runtime = resources / "runtime"
        backend = resources / "backend"
    else:
        repo_root = _find_repo_root(here) or here.parent
        resources = repo_root
        # 开发模式下「内嵌 runtime」就是当前解释器所在的 venv
        runtime = Path(os.environ.get("KF_DEV_RUNTIME", str(Path.home() / ".knowledgeflow-dev")))
        backend = repo_root / "backend"

    user_home = data_home(home)
    return AppPaths(
        contents=contents or resources,
        resources=resources,
        runtime=runtime,
        backend=backend,
        home=user_home,
        config=user_home / "config",
        data=user_home / "data",
        logs=user_home / "logs",
        cache=user_home / "cache",
        run=user_home / "runtime",
    )


def _find_contents(start: Path) -> Path | None:
    """向上最多 6 层找 ``Contents``（``.../X.app/Contents/Resources/...``）。"""
    for parent in list(start.parents)[:6]:
        if parent.name == "Contents" and (parent / "Info.plist").is_file():
            return parent
    return None


def _find_repo_root(start: Path) -> Path | None:
    for parent in list(start.parents)[:8]:
        if (parent / "backend" / "scripts" / "serve.py").is_file():
            return parent
    return None


#: 需要预创建的子目录（第七节列的五个）。
SUBDIRS: Final[tuple[str, ...]] = ("config", "data", "logs", "cache", "runtime")


def ensure_layout(paths: AppPaths) -> None:
    """创建用户数据目录。**只创建，不删除任何东西。**"""
    for name in SUBDIRS:
        (paths.home / name).mkdir(parents=True, exist_ok=True)


def is_macos_apple_silicon() -> bool:
    """v0.4 只支持 Apple Silicon（这是**打包目标**的约束，不是运行时代码的约束）。"""
    import platform

    return platform.system() == "Darwin" and platform.machine() == "arm64"


def macos_version() -> tuple[int, int] | None:
    """取 macOS 主次版本号；非 macOS 返回 ``None``。"""
    import platform

    if platform.system() != "Darwin":
        return None
    try:
        release = platform.mac_ver()[0]  # 形如 "15.7.7"
        major, minor = release.split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None
