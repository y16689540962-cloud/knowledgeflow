"""v1.0 Windows 便携版打包层测试（对位 ``test_macos_packaging.py`` 的 10 项验收）。

分两层，与 macOS 版一致：

**逻辑层**（任何平台都能跑）——路径解析、配置读写与 ACL、环境变量映射、
Vault 可写性、LLM 错误翻译、依赖清单与构建脚本的约束。
这一层是**纯函数**，所以能在 CI（Linux x86）上跑，不需要 Windows。

**端到端层**（真的起一次后端）——用临时 ``LOCALAPPDATA`` 模拟一台干净机器，
把启动器当**子进程**跑起来，验证：服务真的起来、Web UI 能访问、
SQLite 落在用户数据目录（不是安装目录里）、重复启动不会产生第二个实例、
``--stop`` 之后**不留孤儿进程**、再启动能复用同一个数据库。

三条纪律：

1. **测试绝不碰真实家目录 / 真实 %LOCALAPPDATA%**：``USERPROFILE`` 与
   ``LOCALAPPDATA`` 每个用例都指向临时目录。（踩过一次：macOS 的端到端用例
   在 Windows 上跑会把 ``~/Library/Application Support/KnowledgeFlow/``
   真的建到用户家目录里 —— 因为 ``Path.home()`` 在 Windows 上不认 ``HOME``。）
2. **能测行为就别扫文本**：例如「不许暴露 LAN」用假 Popen 抓真实 argv，
   而不是在源码里搜 ``0.0.0.0`` —— 后者会被自己的注释命中（本项目栽过好几次）。
   构建脚本那几条只能扫文本（PowerShell 没法直接执行），所以**先剥注释再匹配**。
3. **两个平台的 ``kf_app`` 是重名包**，同进程内 import 会互相串味 ——
   下面用 ``_load_windows_kf_app()`` 换进换出，见那里的说明。
"""

from __future__ import annotations

import ast
import codecs
import importlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
WIN_PKG = REPO_ROOT / "packaging" / "windows"
LAUNCHER_DIR = WIN_PKG / "launcher"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_windows_x64.ps1"
ACCEPT_SCRIPT = REPO_ROOT / "scripts" / "accept_windows_x64.py"

pytestmark = pytest.mark.skipif(
    not LAUNCHER_DIR.is_dir(),
    reason="这个仓库里没有 Windows 打包层（packaging/windows/launcher）",
)


# --------------------------------------------------------------------------- #
# 加载：两个平台各有一个叫 kf_app 的顶层包
# --------------------------------------------------------------------------- #
def _load_windows_kf_app() -> dict[str, object]:
    """把 Windows 启动器加载进来，**且不污染同名的 macOS 包**。

    ``packaging/macos/launcher/kf_app`` 与 ``packaging/windows/launcher/kf_app``
    是**两个同名的顶层包**。同一个 pytest 进程里先 import 谁，
    ``sys.modules["kf_app"]`` 就是谁 —— 后 import 的那个会静默拿到对方的模块
    （``from kf_app import paths`` 直接命中缓存，**不会报错**）。
    这个坑的症状是断言以完全看不懂的方式失败：例如在 Windows 上断言
    ``~/Library/Application Support``。

    所以这里**换进换出**：临时把 ``kf_app*`` 从 ``sys.modules`` 里摘掉，
    加载 Windows 那份，再把原来的状态原样放回去。两边各拿自己那份，
    而且**与 pytest 的收集顺序无关**（靠字母序凑巧正确不算正确）。

    安全性：两个平台的 ``kf_app`` 子模块都只在**模块级**互相 import
    （没有函数体内的 ``from kf_app import ...``），所以模块对象一旦拿到手，
    内部引用就不会再回头看 ``sys.modules``。
    """
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "kf_app" or name.startswith("kf_app.")
    }
    for name in saved:
        del sys.modules[name]

    sys.path.insert(0, str(LAUNCHER_DIR))
    try:
        modules: dict[str, object] = {}
        for name in ("paths", "settings", "service", "wizard", "control", "main"):
            modules[name] = importlib.import_module(f"kf_app.{name}")
    finally:
        sys.path.remove(str(LAUNCHER_DIR))
        for name in [n for n in sys.modules if n == "kf_app" or n.startswith("kf_app.")]:
            del sys.modules[name]
        sys.modules.update(saved)
    return modules


_MODULES = _load_windows_kf_app()
kf_paths = _MODULES["paths"]
kf_settings = _MODULES["settings"]
kf_service = _MODULES["service"]
kf_wizard = _MODULES["wizard"]
kf_control = _MODULES["control"]
kf_main = _MODULES["main"]


# --------------------------------------------------------------------------- #
# 环境隔离
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把「家目录」「%LOCALAPPDATA%」「开发模式 runtime」都指到临时目录。

    ``USERPROFILE`` 与 ``LOCALAPPDATA`` 必须一起设：Windows 上 ``Path.home()``
    **不认 ``HOME``**，只认 ``USERPROFILE``；而 ``data_home()`` 优先读
    ``LOCALAPPDATA``。少设任何一个，用例就会往真实用户目录里写东西。
    """
    root = tmp_path_factory.mktemp("kf-win-sandbox")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "AppData" / "Local"))
    monkeypatch.setenv("KF_DEV_RUNTIME", str(root / "dev-runtime"))
    monkeypatch.delenv("KF_SETUP_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("KF_VAULT_PATH", raising=False)
    monkeypatch.delenv("KF_LLM_API_KEY", raising=False)
    return root


def make_paths(home: Path) -> "kf_paths.AppPaths":
    """仓库布局下的路径集合（``runtime`` 指向 ``KF_DEV_RUNTIME``）。"""
    return kf_paths.find_app_paths(home=home)


def make_app_paths(*, root: Path, runtime: Path, home: Path) -> "kf_paths.AppPaths":
    """直接拼一个 ``AppPaths``，用来测「属性本身」而不是「怎么找出来的」。"""
    return kf_paths.AppPaths(
        root=root,
        runtime=runtime,
        backend=root / "backend",
        home=home,
        config=home / "config",
        data=home / "data",
        logs=home / "logs",
        cache=home / "cache",
        run=home / "runtime",
    )


def strip_hash_comments(source: str) -> str:
    """剥掉 ``#`` 整行注释 —— 否则断言会被自己写的说明命中。"""
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def strip_ps_comments(source: str) -> str:
    """剥掉 PowerShell 的块注释（``<# ... #>``）与 ``#`` 整行注释。

    构建脚本的说明写得比代码多，不剥的话「脚本里没有 arm64」这种断言
    会被注释里的 ``build_macos_arm64.sh`` 直接证伪。
    """
    without_block = re.sub(r"<#.*?#>", "", source, flags=re.DOTALL)
    return strip_hash_comments(without_block)


def read_build_script() -> str:
    """读构建脚本正文。

    用 ``utf-8-sig``：这个文件**必须带 BOM**（见 ``test_build_script_is_utf8_with_bom``），
    按纯 UTF-8 读会在开头留一个 U+FEFF —— 它粘进正则或 ``startswith`` 断言里
    迟早出事，而且是那种「看起来只差一个看不见的字符」的错。
    """
    return BUILD_SCRIPT.read_text(encoding="utf-8-sig")


def _smoke_test_body() -> str:
    """只取构建脚本「7/8 冒烟测试」那一段（已剥注释）。

    整份脚本扫文本太容易误命中：``$Stage`` 之类的变量名到处都是。截到
    「8/8 打 ZIP」为止，断言就只落在冒烟测试那几步上。
    """
    body = strip_ps_comments(read_build_script())
    start = body.index("7/8 冒烟测试")
    end = body.index("8/8 打 ZIP")
    assert start < end, "构建脚本的步骤编号乱了"
    return body[start:end]


def _decode_console(raw: bytes) -> str:
    """解 Windows 命令行工具的输出。

    中文 Windows 的控制台码页是 GBK，``icacls`` 按 GBK 写；而某些环境
    （``chcp 65001``）是 UTF-8。先用 UTF-8 严格解，失败再退 GBK —— 两个
    都用 ``errors="replace"`` 兜底。**不能只按 UTF-8 解**：那样中文用户名
    会变成乱码，断言「ACL 只剩当前用户」就永远匹配不上。
    """
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _icacls(path: Path) -> str:
    """读回一个文件的 DACL（已解码）。"""
    raw = subprocess.run(
        ["icacls", str(path)], capture_output=True, timeout=30, check=False
    ).stdout
    return _decode_console(raw)


def _ace_lines(dump: str) -> list[str]:
    """从 ``icacls`` 输出里挑出 ACE 行（形如 ``... file DOMAIN\\user:(F)``）。

    以 ``:(`` 为判据 —— 权限标记（``(F)`` / ``(M)`` / ``(RX)``）每个 ACE 都有，
    而首行文件名、末行「已成功处理」都没有。**不按组名匹配**：
    组名是本地化的，钉死它等于把测试绑在某种语言的 Windows 上。
    """
    return [line for line in dump.splitlines() if ":(" in line]


# --------------------------------------------------------------------------- #
# ① 逻辑层：平台与架构
# --------------------------------------------------------------------------- #
def test_platform_is_windows_x64() -> None:
    """非 Windows 上跳过 —— 这是**打包目标**的约束，不是运行时的约束。"""
    if os.name != "nt":
        pytest.skip("不是 Windows，跳过架构断言")
    import platform

    assert platform.machine().lower() in {"amd64", "x86_64"}, "v1.0 的安装包只面向 x64"
    assert kf_paths.is_windows_x64() is True


def test_windows_version_meets_the_declared_minimum() -> None:
    if os.name != "nt":
        pytest.skip("不是 Windows")
    version = kf_paths.windows_version()
    assert version is not None
    assert (version[0], version[1]) >= kf_paths.MIN_WINDOWS_VERSION


def test_windows_version_is_none_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 上必须老实返回 ``None``，不能瞎猜一个版本号出来。"""
    monkeypatch.setattr(sys, "platform", "linux")
    assert kf_paths.windows_version() is None


# --------------------------------------------------------------------------- #
# ① 逻辑层：路径
# --------------------------------------------------------------------------- #
def _make_portable(tmp_path: Path, *, name: str = "KnowledgeFlow") -> tuple[Path, Path]:
    """搭一个**合成的便携包目录**：``runtime/python.exe`` + ``backend/scripts/serve.py``。"""
    portable = tmp_path / name
    (portable / "runtime").mkdir(parents=True)
    (portable / "runtime" / "python.exe").write_bytes(b"")
    (portable / "backend" / "scripts").mkdir(parents=True)
    (portable / "backend" / "scripts" / "serve.py").write_text("", encoding="utf-8")
    launcher_file = portable / "launcher" / "kf_app" / "paths.py"
    launcher_file.parent.mkdir(parents=True)
    launcher_file.write_text("", encoding="utf-8")
    return portable, launcher_file


def test_user_data_never_lives_inside_the_program_dir(tmp_path: Path) -> None:
    """用户数据与程序本体**必须**分离。

    便携包会被解压到 U 盘、被挪走、被整个删掉重下。数据库写在里面的话，
    用户「重新解压一份」就等于丢数据；而且放在 ``Program Files`` 里，
    普通用户根本没有写权限。
    """
    portable, launcher_file = _make_portable(tmp_path)
    home = tmp_path / "fake-home"
    paths = kf_paths.find_app_paths(launcher_file, home=home)

    assert paths.root == portable.resolve(), "应当把便携包根目录识别出来"
    assert paths.runtime == portable.resolve() / "runtime"
    assert paths.backend == portable.resolve() / "backend"

    portable_resolved = portable.resolve()
    for directory in (paths.config, paths.data, paths.logs, paths.cache, paths.run):
        resolved = directory.resolve()
        assert not resolved.is_relative_to(portable_resolved), (
            f"用户数据落在安装目录里了：{directory}"
        )
        assert resolved.is_relative_to(home.resolve()), f"用户数据没落在数据目录里：{directory}"


def test_data_home_follows_LOCALAPPDATA(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``%LOCALAPPDATA%`` 才是 Windows 的正统答案 —— 它可能被组策略指到 D 盘。

    自己拼 ``Path.home()/AppData/Local`` 是猜。显式传 ``home`` 时（测试用）
    才走第一条分支。
    """
    assert kf_paths.data_home(tmp_path) == tmp_path / "AppData" / "Local" / "KnowledgeFlow"

    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("LOCALAPPDATA", str(custom))
    assert kf_paths.data_home() == custom / "KnowledgeFlow"


def test_data_home_falls_back_when_LOCALAPPDATA_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``LOCALAPPDATA`` 被清掉（某些精简环境）时不能崩，退回家目录。"""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert kf_paths.data_home() == Path.home() / "AppData" / "Local" / "KnowledgeFlow"


def test_ensure_layout_creates_only_the_five_subdirs(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    created = sorted(p.name for p in paths.home.iterdir() if p.is_dir())
    assert created == sorted(kf_paths.SUBDIRS)
    assert set(created) == {"config", "data", "logs", "cache", "runtime"}


def test_runtime_python_prefers_direct_and_accepts_venv_layout(tmp_path: Path) -> None:
    """两种布局都要认，否则开发时为了跑一次 ``--check`` 得手工摆目录结构。

    * ``runtime/python.exe`` —— 打包产物（python-build-standalone 解包后就是这样）
    * ``runtime/Scripts/python.exe`` —— 开发模式下 ``KF_DEV_RUNTIME`` 指向 venv
    """
    root = tmp_path / "portable"
    runtime = root / "runtime"
    (runtime / "Scripts").mkdir(parents=True)
    (runtime / "Scripts" / "python.exe").write_bytes(b"")

    paths = make_app_paths(root=root, runtime=runtime, home=tmp_path / "home")
    assert paths.runtime_python == runtime / "Scripts" / "python.exe"

    # 打包产物那一份优先
    (runtime / "python.exe").write_bytes(b"")
    assert paths.runtime_python == runtime / "python.exe"


def test_frozen_launcher_finds_its_install_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PyInstaller 冻结后 ``__file__`` 指向临时解包目录（``sys._MEIPASS``）。

    若启动器拿 ``__file__`` 往上找安装目录，会**静默退化成「开发模式」**——
    症状是「打包好的 exe 找不到自己的 backend」。所以冻结时必须用
    ``sys.executable``。
    """
    portable, _ = _make_portable(tmp_path)
    exe = portable / "KnowledgeFlow.exe"
    exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    paths = kf_paths.find_app_paths(home=tmp_path / "fake-home")
    assert paths.root == portable.resolve(), "冻结后没有从 exe 的位置找到安装目录"
    assert paths.backend == portable.resolve() / "backend"


def test_dev_mode_falls_back_to_the_repo_layout() -> None:
    """找不到安装目录（直接从源码目录跑）时退化成仓库布局，不崩。"""
    paths = kf_paths.find_app_paths(home=Path(os.environ["LOCALAPPDATA"]) / "x")
    assert paths.backend == BACKEND_ROOT, "开发模式下 backend 应当指向仓库里的那份"


# --------------------------------------------------------------------------- #
# ① 逻辑层：配置
# --------------------------------------------------------------------------- #
def test_settings_roundtrip(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    original = kf_settings.Settings(
        obsidian_vault_path=str(tmp_path / "vault"),
        llm_provider="deepseek",
        llm_api_key="sk-not-a-real-key",
        llm_model="deepseek-flash",
    )
    kf_settings.save(paths, original)

    assert paths.settings_file.is_file()
    loaded = kf_settings.load(paths)
    assert loaded is not None
    assert loaded.llm_api_key == original.llm_api_key
    assert loaded.obsidian_vault_path == original.obsidian_vault_path
    assert loaded.created_at, "保存时应自动写上 created_at"


def _require_windows_acl() -> str:
    """ACL 相关用例的前置守卫：Windows + icacls + 取得到当前用户。"""
    if os.name != "nt":
        pytest.skip("ACL 是 Windows 的概念")
    if shutil.which("icacls") is None:
        pytest.skip("这台机器上没有 icacls")
    principal = kf_settings.current_user_principal()
    if not principal:
        pytest.skip("取不到当前用户主体名（USERNAME 为空）")
    return principal


def test_save_tightens_the_settings_acl(tmp_path: Path) -> None:
    """Windows 上的 ``0600`` 等价物：API Key 只有本人能读。

    ``os.chmod(0o600)`` 在 Windows 上**只切「只读」位**，碰不到 DACL ——
    「只有我能读这个 API Key」这个性质根本没被保护。这里的等价物是
    ``icacls /inheritance:r /grant:r <user>:(F)``。

    **断言结果不认实现**，而且刻意避开「ACE 恰好剩几条」这种脆断言：
    实测同一台机器上，不同父目录下 ``icacls`` 留下的显式 ACE 数量并不一样
    （用户目录里可能还留着 ``SYSTEM`` / ``Administrators`` / ``OWNER RIGHTS``
    —— 那些本来就能读任何文件，不是泄漏）。要守的三条性质是：

    1. 当前用户拿到**显式**完全控制
    2. 不再有 ``(I)`` 标记的继承 ACE（父目录给的广域权限全没了）
    3. 广域组（``Users`` / ``Everyone`` = 本机任何用户）不再有任何 ACE

    「继承被真正断掉」这件事由下一条用例用一个**自己搭的**继承场景来证明 ——
    不能靠「这台机器默认 ACL 长什么样」，那既不可控也会让断言变成空话。
    """
    principal = _require_windows_acl()

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    kf_settings.save(paths, kf_settings.Settings(llm_api_key="sk-not-a-real-key"))

    dump = _icacls(paths.settings_file)
    aces = _ace_lines(dump)

    assert any(principal in line and ":(F)" in line for line in aces), (
        f"当前用户 {principal} 没有拿到显式完全控制：\n{dump}"
    )
    assert not any("(I)" in line for line in aces), (
        f"settings.json 仍带着继承来的 ACE：\n{dump}"
    )
    broad = [line for line in aces if re.search(r"(^|[\\ ])Users:\(|Everyone", line)]
    assert broad == [], f"settings.json 仍对本机任意用户开放：{broad}\n{dump}"


def test_restrict_to_current_user_drops_inherited_access(tmp_path: Path) -> None:
    """``/inheritance:r`` 真的把继承来的广域权限清掉了 —— 用一个**自建的**场景验证。

    先给一个目录加一条可继承的 ``Everyone:(OI)(CI)(RX)``，确认新建的文件
    确实继承了它（带 ``(I)`` 标记），再收紧，确认继承 ACE 全部消失。

    为什么要自己搭：默认 ACL 随机器/父目录而变。实测在受限环境里，
    工作目录下的新文件**根本没有继承 ACE**（DACL 是被保护过的），
    那时「收紧之后没有 ``(I)``」就是一句空话 —— 用例会绿，但什么都没测到。

    用 ``*S-1-1-0``（Everyone 的 SID）而不是名字：组名是本地化的，
    SID 不是。
    """
    principal = _require_windows_acl()

    shared = tmp_path / "shared"
    shared.mkdir()
    granted = subprocess.run(
        ["icacls", str(shared), "/grant", "*S-1-1-0:(OI)(CI)(RX)"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if granted.returncode != 0:
        pytest.skip(f"这台机器不允许改目录 ACL（退出码 {granted.returncode}），搭不出继承场景")

    target = shared / "settings.json"
    target.write_text("{}", encoding="utf-8")

    before = _ace_lines(_icacls(target))
    inherited = [line for line in before if "(I)" in line]
    if not inherited:
        pytest.skip("新建文件没有继承父目录的 ACE，无法验证 /inheritance:r")

    assert kf_settings.restrict_to_current_user(target) is True

    dump = _icacls(target)
    after = _ace_lines(dump)
    assert not any("(I)" in line for line in after), (
        f"继承来的 ACE 没有被断掉：\n{dump}"
    )
    assert len(after) < len(before), f"收紧之后 ACE 反而没有变少：\n{dump}"
    assert any(principal in line and ":(F)" in line for line in after), (
        f"收紧之后当前用户没有完全控制：\n{dump}"
    )


def test_restrict_to_current_user_reports_failure_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """取不到用户名时必须**返回 False**，不能抛 —— 配置存不下去比权限宽松更糟。"""
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USERDOMAIN", raising=False)
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    assert kf_settings.restrict_to_current_user(target) is False


def test_broken_settings_file_does_not_crash(tmp_path: Path) -> None:
    """配置损坏 → 当作「没配过」，而不是抛 traceback 给用户。"""
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    paths.settings_file.write_text("{ 这不是 json", encoding="utf-8")
    assert kf_settings.load(paths) is None


def test_missing_fields_are_reported_in_plain_language() -> None:
    gaps = kf_settings.Settings().missing()
    assert any("Vault" in g for g in gaps)
    assert any("API Key" in g for g in gaps)
    assert not any(g.islower() and "_" in g for g in gaps), "不该把字段名直接甩给用户"


def test_env_uses_absolute_posix_paths_for_database_and_media(tmp_path: Path) -> None:
    """**这是打包最容易翻车的地方**，Windows 上还多一层：斜杠方向。

    后端默认 ``DATABASE_URL=sqlite:///./data/...`` 依赖 cwd；双击启动的进程
    cwd 是安装目录（甚至 ``C:\\Windows\\System32``），相对路径会落到别处。
    另外 SQLAlchemy 的 SQLite URL 里**反斜杠是转义字符**，
    ``sqlite:///C:\\Users\\...`` 在不同版本上解析结果不一致 ——
    ``sqlite:///C:/Users/...`` 才是稳定写法。
    """
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(paths, kf_settings.Settings())

    assert env["DATABASE_URL"].startswith("sqlite:///"), env["DATABASE_URL"]
    db_path = env["DATABASE_URL"].replace("sqlite:///", "")
    assert "\\" not in env["DATABASE_URL"], f"SQLite URL 里不该有反斜杠：{env['DATABASE_URL']}"
    assert Path(db_path).is_absolute(), f"数据库路径必须是绝对路径：{db_path}"
    assert db_path == paths.database_file.as_posix()
    assert "sqlite:///./" not in env["DATABASE_URL"], "不能留相对路径（cwd 一变就写错地方）"

    assert Path(env["DOWNLOAD_DIR"]).is_absolute()
    assert str(paths.data) in env["DOWNLOAD_DIR"]


def test_env_pins_the_utf8_code_page(tmp_path: Path) -> None:
    """中文 Windows 的控制台默认码页是 GBK。

    不钉住的话后端一 ``print`` 中文就 ``UnicodeEncodeError`` ——
    而且是**子进程崩**、日志里只留一行 traceback，很难查。
    """
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(paths, kf_settings.Settings())
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_env_strips_inherited_python_variables(tmp_path: Path, monkeypatch) -> None:
    """后端跑的是包里那份 runtime，环境却是从用户 shell 继承来的 —— 两者不该混。

    用户 shell 里设了 ``PYTHONPATH``，它就会插进**内嵌 runtime** 的 ``sys.path``，
    而且路径上任何叫 ``sitecustomize.py`` 的文件都会被自动执行。这不是假想问题：
    本项目的构建机上就带着一个 ``PYTHONPATH``（指向 IDE 的垫片），
    它一度让构建工具 venv 的 ``ensurepip`` 退出 1，而报错完全指不到 ``PYTHONPATH``。
    ``PYTHONHOME`` 更狠：它会把内嵌 runtime 的 ``prefix`` 整个改掉，连标准库都找不到。
    用户级 site-packages 同理 —— 用户 profile 里恰好装过的包会遮蔽包里那份。
    """
    leaky = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE")
    for name in leaky:
        monkeypatch.setenv(name, "/somewhere/leaky")

    env = kf_settings.build_env(make_paths(tmp_path), kf_settings.Settings())

    for name in leaky:
        assert name not in env, f"{name} 不该被后端继承（会污染内嵌解释器）"
    assert env["PYTHONNOUSERSITE"] == "1", "还要挡住用户级 site-packages"


def test_env_maps_provider_specific_keys(tmp_path: Path) -> None:
    """后端是 ``Literal["openai","deepseek"]`` + 两套 Key 名，映射不能错位。"""
    paths = make_paths(tmp_path)

    deepseek = kf_settings.build_env(
        paths, kf_settings.Settings(llm_provider="deepseek", llm_api_key="k1", llm_model="m")
    )
    assert deepseek["LLM_PROVIDER"] == "deepseek"
    assert deepseek["DEEPSEEK_API_KEY"] == "k1"
    assert deepseek["DEEPSEEK_BASE_URL"].startswith("https://api.deepseek.com")

    openai = kf_settings.build_env(
        paths,
        kf_settings.Settings(
            llm_provider="openai", llm_api_key="k2", llm_model="m", llm_base_url="http://x/v1"
        ),
    )
    assert openai["OPENAI_API_KEY"] == "k2"
    assert openai["OPENAI_BASE_URL"] == "http://x/v1"


def test_env_falls_back_to_openai_for_unknown_providers(tmp_path: Path) -> None:
    """第三方兼容端点的做法是「openai + 自定义 Base URL」，不能因为
    填了个不认识的名字就让后端在 pydantic 校验上炸掉。"""
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(
        paths, kf_settings.Settings(llm_provider="some-proxy", llm_api_key="k", llm_model="m")
    )
    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "k"


def test_env_keeps_model_cache_inside_app_data(tmp_path: Path) -> None:
    """Whisper 模型别落到 ``~/.cache`` —— 卸载时要能一起清掉。"""
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(paths, kf_settings.Settings())
    assert env["HF_HOME"] == str(paths.cache / "huggingface")


# --------------------------------------------------------------------------- #
# ① 逻辑层：向导的判定
# --------------------------------------------------------------------------- #
def test_probe_vault_accepts_writable_and_leaves_nothing_behind(tmp_path: Path) -> None:
    vault = tmp_path / "MyVault"
    vault.mkdir()
    ok, message = kf_settings.probe_vault(str(vault))
    assert ok, message
    assert list(vault.iterdir()) == [], "测试文件必须删掉"


def test_probe_vault_rejects_missing_path(tmp_path: Path) -> None:
    ok, message = kf_settings.probe_vault(str(tmp_path / "nope"))
    assert not ok
    assert "不存在" in message


def test_probe_vault_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    ok, message = kf_settings.probe_vault(str(target))
    assert not ok
    assert "文件" in message


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "API Key"), (404, "Base URL"), (429, "限流")],
)
def test_probe_llm_translates_http_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    """失败要给出「检查什么」，不是把 HTTP 状态码原样丢出来。"""

    def fake_urlopen(request: object, timeout: float = 0) -> object:  # noqa: ARG001
        raise urllib.error.HTTPError(
            "https://api.example.com/v1/chat/completions", status, "err", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, message = kf_settings.probe_llm("https://api.example.com/v1", "k", "m")
    assert not ok
    assert expected in message, message


def test_probe_llm_reports_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float = 0) -> object:  # noqa: ARG001
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, message = kf_settings.probe_llm("https://nope.invalid/v1", "k", "m")
    assert not ok
    assert "连不上" in message


def test_optional_capabilities_never_block_startup(tmp_path: Path) -> None:
    """ASR / OCR 缺了**不能**成为启动的硬阻塞。

    构造「内置 runtime 与后端都在」的情形，确认 ``blocking_problems`` 为空 ——
    也就是 tesseract / whisper 的缺失都不算阻塞项。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    paths.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_python.write_text("x", encoding="utf-8")
    assert paths.backend.is_dir(), "仓库布局下 backend 应存在"

    assert kf_wizard.blocking_problems(paths) == []

    checks = {name: ok for name, ok, _ in kf_wizard.check_environment(paths)}
    assert any("OCR" in name for name in checks), "环境检查应列出 OCR 状态"
    assert checks.get("音视频解码") is True, "本项目用 PyAV，不该要求系统装 ffmpeg"
    # 汇总文字里不该出现异常类型
    summary = kf_wizard.environment_summary(paths)
    assert "Traceback" not in summary and "ModuleNotFound" not in summary


# --------------------------------------------------------------------------- #
# ① 逻辑层：输出编码（GBK 控制台装不下 ✓）
# --------------------------------------------------------------------------- #
def test_console_marks_fall_back_when_the_encoding_cannot_hold_them() -> None:
    """标记必须按**输出编码**挑，不能无脑用 ``✓``。

    ``✓``（U+2713）不在 GBK 码表里，而中文 Windows 的 stdout 就是 GBK。
    实测的故障：``KnowledgeFlow.exe --check`` 退出码 1、stdout **一个字节都没有**，
    用户唯一看到的是一行 ``'gbk' codec can't encode character '\\u2713'``。
    """
    assert kf_wizard.console_marks("utf-8") == ("✓", "✗")
    assert kf_wizard.console_marks("gbk") == ("[OK]", "[--]")
    assert kf_wizard.console_marks("cp1252") == ("[OK]", "[--]")
    # 编码名写错时按「装不下」处理，而不是抛 LookupError 把诊断命令打挂
    assert kf_wizard.console_marks("no-such-codec") == ("[OK]", "[--]")


def test_environment_summary_is_printable_on_a_gbk_console(tmp_path: Path) -> None:
    """``--check`` 的汇总文字必须在 GBK 上**能编码**（不抛 UnicodeEncodeError）。"""

    class _GbkStdout:
        encoding = "gbk"
        errors = "replace"

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    paths.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_python.write_text("x", encoding="utf-8")

    original = sys.stdout
    sys.stdout = _GbkStdout()  # type: ignore[assignment]
    try:
        summary = kf_wizard.environment_summary(paths)
    finally:
        sys.stdout = original

    summary.encode("gbk")  # 这一句就是回归断言：以前会抛 UnicodeEncodeError
    assert "内置运行环境" in summary, "中文必须原样保住 —— GBK 装得下"
    assert "✓" not in summary and "✗" not in summary, "装不下的标记应降级成 ASCII"


def test_environment_summary_keeps_the_pretty_marks_on_utf8(tmp_path: Path) -> None:
    """UTF-8 输出（日志文件、pip 管道、Linux CI）上仍然是好看的 ``✓``。"""
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)

    original = sys.stdout

    class _Utf8Stdout:
        encoding = "utf-8"
        errors = "strict"

    sys.stdout = _Utf8Stdout()  # type: ignore[assignment]
    try:
        summary = kf_wizard.environment_summary(paths)
    finally:
        sys.stdout = original
    assert "✓" in summary or "✗" in summary, summary


def test_launcher_survives_a_gbk_console(tmp_path: Path) -> None:
    """在 GBK 流上 ``print`` 不许抛异常 —— 这正是 ``--check`` 死掉的地方。

    顺带钉住「中文不能跟着一起丢」：降级只该发生在装不下的字符上。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)

    raw = io.BytesIO()
    gbk_stream = io.TextIOWrapper(raw, encoding="gbk", newline="\n")
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = gbk_stream  # type: ignore[assignment]
    sys.stderr = gbk_stream  # type: ignore[assignment]
    kf_main._attach_std_streams(paths)
    tee = sys.stdout
    try:
        print("✓ 内置运行环境 —— 已就绪")  # 修之前：UnicodeEncodeError: 'gbk' codec
        tee.flush()  # type: ignore[attr-defined]
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        tee.close()  # type: ignore[attr-defined]

    written = raw.getvalue().decode("gbk")
    assert "内置运行环境" in written, "中文必须原样写出去"
    assert "✓" not in written, "装不下的字符要降级，而不是把整条输出丢掉"


def test_launcher_always_writes_a_console_log(tmp_path: Path) -> None:
    """无论有没有控制台，启动器都要留一份 ``console.log``。

    双击启动时用户**看不到任何输出**，这份日志是唯一的线索；而从 cmd /
    PowerShell 启动时，PyInstaller 会把 std 句柄按 ANSI 码页重开 —— 那种流既不是
    ``None``（所以旧的「只有 None 才接管」判断会放它过去），写出去的东西也可能
    到不了用户眼前。所以只能**无条件**记一份。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)

    console = io.StringIO()
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = console  # type: ignore[assignment]
    sys.stderr = console  # type: ignore[assignment]
    kf_main._attach_std_streams(paths)
    tee = sys.stdout
    try:
        print("marker-中文-✓")
        tee.flush()  # type: ignore[attr-defined]
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        tee.close()  # type: ignore[attr-defined]

    assert "marker-中文-✓" in console.getvalue(), "原控制台该看到的还得看到"
    logged = (paths.logs / "console.log").read_text(encoding="utf-8")
    assert "marker-中文-✓" in logged, "console.log 必须无条件写"


def test_launcher_takes_over_the_streams_when_there_is_no_console(tmp_path: Path) -> None:
    """无控制台（双击）时 ``sys.stdout`` 是 ``None``，``print`` 会抛 AttributeError。

    而 ``print`` 大多出现在**错误处理路径**上（"启动失败，日志在…"），
    于是真正的故障被一个 AttributeError 盖住。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)

    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = None  # type: ignore[assignment]
    sys.stderr = None  # type: ignore[assignment]
    kf_main._attach_std_streams(paths)
    stream = sys.stdout
    try:
        print("启动失败，日志在 …")
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        stream.close()  # type: ignore[attr-defined]

    assert sys.stdout is not None and sys.stderr is not None
    logged = (paths.logs / "console.log").read_text(encoding="utf-8")
    assert "启动失败，日志在" in logged


def test_check_environment_reports_windows_version_and_arch(tmp_path: Path) -> None:
    """环境检查必须真的问一次系统，而不是写死一行「满足要求」。"""
    paths = make_paths(tmp_path)
    names = [name for name, _, _ in kf_wizard.check_environment(paths)]
    if os.name == "nt":
        assert any(name.startswith("Windows ") for name in names), names
    assert any("64 位" in name for name in names), names
    assert any("内置运行环境" == name for name in names), names
    assert any("后端程序" == name for name in names), names


def test_missing_runtime_is_blocking(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    problems = kf_wizard.blocking_problems(paths)  # runtime 故意不创建
    assert any("运行环境" in p for p in problems), problems


def test_wizard_noninteractive_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无人值守路径（测试与批量装机走这条）必须完全不弹窗。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("KF_SETUP_NONINTERACTIVE", "1")
    monkeypatch.setenv("KF_VAULT_PATH", str(vault))
    monkeypatch.setenv("KF_LLM_API_KEY", "sk-not-a-real-key")
    monkeypatch.setenv("KF_LLM_MODEL", "deepseek-flash")

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    settings = kf_wizard.run_wizard(paths)
    assert settings is not None
    assert settings.obsidian_vault_path == str(vault)
    assert settings.llm_api_key == "sk-not-a-real-key"


def test_wizard_returns_none_when_the_environment_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KF_SETUP_NONINTERACTIVE", "1")
    monkeypatch.delenv("KF_VAULT_PATH", raising=False)
    monkeypatch.delenv("KF_LLM_API_KEY", raising=False)
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    assert kf_wizard.run_wizard(paths) is None


# --------------------------------------------------------------------------- #
# ① 逻辑层：启动行为（测行为，不扫文本）
# --------------------------------------------------------------------------- #
class _FakeProcess:
    def __init__(self, argv: list[str], kwargs: dict) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def terminate(self) -> None:  # pragma: no cover - 由 terminate 调用
        pass


def test_service_start_never_exposes_lan(tmp_path: Path) -> None:
    """只监听 ``127.0.0.1``，**绝不允许** ``0.0.0.0`` / ``--expose``。

    用注入的假进程工厂抓真实 argv —— 比在源码里搜字符串可靠：源码里
    「我们不用 0.0.0.0」这句注释会把文本断言骗过去。

    **刻意不 patch ``subprocess.Popen``**：那是全局打补丁，会把同一进程里
    所有用它的人一起换掉。
    """
    captured: dict[str, list[str]] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        captured["argv"] = list(argv)
        return _FakeProcess(list(argv), dict(kwargs))

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    kf_service.start(paths, {"PATH": "C:\\Windows"}, 8123, log_path=paths.app_log, spawn=fake_spawn)

    argv = captured["argv"]
    joined = " ".join(argv)
    assert "--expose" not in argv, "打包层不许把服务暴露到局域网"
    assert "--host" not in argv, "打包层不许改监听地址（默认就是回环）"
    assert "0.0.0.0" not in joined
    # 用 Path 拆而不是 ``endswith("scripts/serve.py")``：后者在 Windows 上
    # 会拿到 ``scripts\\serve.py``，断言的是分隔符而不是「复用了同一个入口」。
    assert Path(argv[0]).name.startswith("python"), f"应当用内嵌解释器启动：{argv[0]}"
    assert Path(argv[1]).parts[-2:] == ("scripts", "serve.py"), (
        f"应当复用现有后端入口：{argv[1]}"
    )


def test_service_start_suppresses_the_console_window(tmp_path: Path) -> None:
    """``CREATE_NO_WINDOW``：不给后端弹一个黑色控制台窗口。

    这不是观感问题 —— 用户关掉那个窗口会**连带杀掉后端**，
    他会以为「程序自己退了」。``CREATE_NEW_PROCESS_GROUP`` 让
    ``taskkill /T`` 能把整组收掉。
    """
    captured: dict[str, dict] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        captured["kwargs"] = dict(kwargs)
        return _FakeProcess(list(argv), dict(kwargs))

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    kf_service.start(paths, {}, 8123, log_path=paths.app_log, spawn=fake_spawn)

    kwargs = captured["kwargs"]
    flags = kwargs["creationflags"]
    assert flags & kf_service.CREATE_NO_WINDOW, "必须掐掉子进程的控制台窗口"
    assert flags & kf_service.CREATE_NEW_PROCESS_GROUP, "必须自成一组，好整组收掉"
    assert kwargs["stdin"] == subprocess.DEVNULL, "子进程不该抢 stdin"
    assert kwargs["cwd"] == str(paths.backend)


def test_service_start_writes_state_and_uses_absolute_paths(tmp_path: Path) -> None:
    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        return _FakeProcess(list(argv), dict(kwargs))

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    handle = kf_service.start(paths, {}, 8123, log_path=paths.app_log, spawn=fake_spawn)

    assert handle.owned is True
    assert Path(handle.process.argv[0]).is_absolute()
    assert Path(handle.process.argv[1]).is_absolute()
    state = kf_service.read_state(paths)
    assert state is not None and state["port"] == 8123
    kf_service.clear_state(paths)
    assert kf_service.read_state(paths) is None


def test_health_rejects_a_different_service_on_the_same_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口上可能有**别的**程序也在跑 HTTP —— 不能当成自己的服务复用。"""

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"hello": "world"}'

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response())
    assert kf_service.health(12345) is None


def test_health_probe_retries_before_concluding_nothing_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次探活失败**不能**就断定「服务没在运行」。

    机器忙时（杀毒软件正在扫刚解压的目录、同时在跑测试套件）单次 1.5 秒探活
    可能超时，于是启动器会把自己的服务误判成「端口被别的程序占用」而拒绝启动。
    """
    calls = {"n": 0}
    payload = {"version": "0.4.0", "llm_provider": "deepseek"}

    def flaky(port: int, *, timeout: float = 0) -> dict | None:  # noqa: ARG001
        calls["n"] += 1
        return payload if calls["n"] >= 2 else None

    monkeypatch.setattr(kf_service, "health", flaky)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)

    assert kf_service.health_with_retry(12345) == payload
    assert calls["n"] == 2, "应当重试一次后才拿到结果"


def test_health_probe_gives_up_after_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def always_none(port: int, *, timeout: float = 0) -> None:  # noqa: ARG001
        calls["n"] += 1
        return None

    monkeypatch.setattr(kf_service, "health", always_none)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)
    assert kf_service.health_with_retry(12345) is None
    assert calls["n"] == kf_service.PROBE_ATTEMPTS


def test_stop_does_not_claim_success_without_stopping_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**修过的真 bug**：状态文件不在时，旧实现直接返回「已停止」。

    现在的契约：没停掉任何东西就返回 ``False``，让调用方如实报告。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    monkeypatch.setattr(kf_service, "_listener_pid", lambda port: None)
    assert kf_service.stop_by_state(paths, 1) is False


def test_pid_alive_never_kills_the_process_it_probes() -> None:
    """**Windows 上最容易踩的一个坑**。

    ``os.kill(pid, 0)`` 在 POSIX 上是「发 0 号信号」（探活），但在 Windows 上
    CPython 会把它落到 ``TerminateProcess`` —— 拿它探活等于**把目标进程杀掉**，
    然后报告「进程不在了」。这个错误极其隐蔽：调用方看到的结果永远自洽
    （问了就说没了），只有被探的那个进程莫名其妙消失时才暴露。

    所以这里连着探 5 次，既断言结果稳定为 ``True``，也断言**目标进程还活着**。
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        results = [kf_service.pid_alive(proc.pid) for _ in range(5)]
        assert results == [True] * 5, f"探活结果不稳定：{results}"
        assert proc.poll() is None, "探活把目标进程杀掉了 —— 这就是 os.kill(pid, 0) 的坑"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            proc.kill()
            proc.wait(timeout=5)


def test_pid_alive_is_false_for_a_process_that_already_exited() -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    proc.wait(timeout=30)
    assert kf_service.pid_alive(proc.pid) is False


def test_pid_alive_rejects_nonsense_pids() -> None:
    assert kf_service.pid_alive(0) is False
    assert kf_service.pid_alive(-1) is False


def test_cli_parses_the_documented_flags() -> None:
    args = kf_main.parse_args(
        ["--port", "9000", "--no-browser", "--no-window", "--status", "--stop", "--check"]
    )
    assert args.port == 9000
    assert args.no_browser is True
    assert args.no_window is True
    assert args.status is True
    assert args.stop is True
    assert args.check is True

    defaults = kf_main.parse_args([])
    assert defaults.port is None, "端口默认交给 settings.json，不该写死在 CLI 里"


# --------------------------------------------------------------------------- #
# ① 逻辑层：依赖纯净性（启动器只用标准库）
# --------------------------------------------------------------------------- #
def test_launcher_imports_only_stdlib_and_itself() -> None:
    """启动器**零第三方依赖**：GUI 用的是 Python 自带的 tkinter。

    用 ``ast`` 取真实 import 语句，而不是在源码里搜字符串 ——
    注释里出现的包名会把文本断言骗过去。
    """
    allowed = set(sys.stdlib_module_names)
    offenders: dict[str, list[str]] = {}

    files = [LAUNCHER_DIR / "run.py"] + [
        LAUNCHER_DIR / "kf_app" / f"{name}.py"
        for name in ("paths", "settings", "service", "wizard", "control", "main")
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in allowed:
                        bad.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入
                    continue
                root = (node.module or "").split(".")[0]
                if root and root != "kf_app" and root not in allowed:
                    bad.add(node.module or "")
        if bad:
            offenders[path.name] = sorted(bad)

    assert offenders == {}, f"启动器引入了第三方依赖：{offenders}"


def test_control_window_is_implemented_with_tkinter() -> None:
    """控制窗口是 Windows 上用户**唯一看得见、点得着**的东西，不能悄悄消失。

    不引入托盘图标依赖（pystray 之类）—— 那会带来一个只在 Windows 上
    才有意义的第三方包，与「零额外依赖」的取向冲突。
    """
    source = (LAUNCHER_DIR / "kf_app" / "control.py").read_text(encoding="utf-8")
    assert "tkinter" in source
    assert kf_control.POLL_INTERVAL_MS > 0
    assert callable(kf_control.run)
    assert kf_control.APP_TITLE == "KnowledgeFlow"


# --------------------------------------------------------------------------- #
# ① 逻辑层：产物约束（只能扫文本 —— PowerShell 脚本没法直接执行）
# --------------------------------------------------------------------------- #
def test_build_script_is_utf8_with_bom() -> None:
    """**这个脚本必须以 UTF-8 *带 BOM* 保存。**

    Windows PowerShell 5.1 读 ``.ps1`` 时，文件没有 BOM 就按系统 ANSI 码页解
    （中文机器上是 GBK）。本脚本里全是中文注释，UTF-8 字节被当成 GBK 之后，
    某些多字节序列会把后面的引号「吃掉」—— 于是 PowerShell 报的是一堆
    莫名其妙的语法错误（实测：``表达式或语句中包含意外的标记"stage'``、
    ``此语言版本中不支持"from"关键字``），**而真正的原因（编码）一个字都不提**。

    这个坑实测踩过：脚本本身完全正确，换个 BOM 就能跑。
    """
    raw = BUILD_SCRIPT.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8), (
        "build_windows_x64.ps1 必须以 UTF-8 带 BOM 保存；"
        "否则 PowerShell 5.1 会按 ANSI 码页解读中文注释并报出一堆无关的语法错误"
    )
    text = raw.decode("utf-8-sig")
    assert "环境检查" in text, "带 BOM 之后正文应当仍能正常解码"
    # 不能只有 BOM 却内容损坏
    assert "Windows" in text


def test_version_info_file_is_written_without_a_bom() -> None:
    """``version_info.txt`` 不能带 BOM —— PyInstaller 把它当 Python 表达式 eval。

    开头的 U+FEFF 会直接变成语法错误。而 PowerShell 5.1 的
    ``Set-Content -Encoding UTF8`` **恰恰会加 BOM**（那是它的历史行为，
    跟「UTF-8」这个名字给人的预期不一致），所以这里必须绕开 cmdlet。
    """
    source = strip_ps_comments(read_build_script())
    assert "UTF8Encoding($false)" in source, (
        "version_info.txt 必须用 UTF8Encoding($false) 写（不带 BOM）"
    )
    assert not re.search(r"Set-Content\s+-Encoding\s+UTF8\s+\$VersionFile", source), (
        "Set-Content -Encoding UTF8 会加上 BOM，PyInstaller eval 时会语法错误"
    )


def test_build_script_only_targets_x64() -> None:
    source = strip_ps_comments(read_build_script())
    assert "AMD64" in source, "构建脚本应当拒绝非 x64 架构"
    assert "windows-x64" in source, "产物名里应当写明架构"
    assert "arm64" not in source.lower(), "v1.0 的 Windows 包不含 arm64"


def test_build_script_pins_the_embedded_python() -> None:
    """内嵌 runtime 的 tag 与 Python 版本必须与 macOS 版**逐字对齐**。

    「同一个项目在 Mac 上 3.13.15、在 Windows 上 3.13.9」是没必要的差异，
    以后排查「只在某个平台上复现」的问题时，多一个变量就多一层怀疑。
    所以这里**从两个脚本里各取一次**再比对，而不是把版本号抄进断言
    （抄进断言的话，两边一起升级时这条用例就变成摆设了）。
    """
    win = read_build_script()
    mac = (REPO_ROOT / "scripts" / "build_macos_arm64.sh").read_text(encoding="utf-8")

    def pick(source: str, pattern: str) -> str:
        match = re.search(pattern, source)
        assert match is not None, f"从脚本里取不到 {pattern!r}"
        return match.group(1)

    win_tag = pick(win, r"\$PbsTag\s*=\s*'([^']+)'")
    win_python = pick(win, r"\$PbsPython\s*=\s*'([^']+)'")
    mac_tag = pick(mac, r'PBS_TAG="([^"]+)"')
    mac_python = pick(mac, r'PBS_PYTHON="([^"]+)"')

    assert win_tag == mac_tag, f"两个平台的 python-build-standalone tag 不一致：{win_tag} vs {mac_tag}"
    assert win_python == mac_python, f"两个平台的 Python 版本不一致：{win_python} vs {mac_python}"

    # 平台专属的资产名（Windows 必须是 msvc x64；macOS 是 apple-darwin arm64）
    assert "python-build-standalone" in win
    assert "x86_64-pc-windows-msvc" in win
    assert "aarch64-apple-darwin" in mac


def test_build_script_freezes_a_windowless_onefile_exe() -> None:
    """入口用 PyInstaller 冻一个无控制台的单文件 exe。

    为什么不是「让用户双击 .cmd」：.cmd 会留下一个黑色控制台窗口，
    关掉那个窗口会**连带杀掉后端** —— 用户会以为「程序自己退了」。
    """
    source = strip_ps_comments(read_build_script())
    for flag in ("--onefile", "--noconsole", "--icon", "--version-file"):
        assert flag in source, f"构建脚本缺少 PyInstaller 参数 {flag}"
    # 构建工具装在独立 venv 里，不能污染内嵌 runtime
    assert "pyinstaller" in source.lower()
    assert ".tools" in source


def test_generated_cmd_entry_uses_the_script_directory() -> None:
    """入口模板必须用 ``%~dp0`` 而不是 ``%CD%`` —— 用户可能从任意目录调用它。

    先剥注释：脚本里恰好有一句「**必须用 %~dp0 而不是 %CD%**」的说明，
    不剥的话断言会被自己写的注释证伪。
    """
    source = strip_ps_comments(read_build_script())
    assert "%~dp0" in source
    assert "%CD%" not in source


def test_build_script_survives_windows_powershell_51() -> None:
    """``$IsWindows`` 是 PowerShell 6+ 才有的自动变量，5.1 上**不存在**。

    脚本开头有 ``Set-StrictMode -Version Latest``，而严格模式下读一个未定义的
    变量是**终止性错误**（实测 5.1 抛「检索不到变量"$IsWindows"」）——
    直接写 ``-not $IsWindows`` 会让脚本在「0/8 环境检查」当场死掉，一行活都干不了。
    正确写法是先按 ``$PSVersionTable.PSVersion.Major`` 短路。
    """
    source = strip_ps_comments(read_build_script())
    assert "Set-StrictMode -Version Latest" in source

    offenders = [
        line.strip()
        for line in source.splitlines()
        if "$IsWindows" in line and "PSVersionTable.PSVersion.Major" not in line
    ]
    assert offenders == [], (
        f"读 $IsWindows 之前必须先用版本号短路（PowerShell 5.1 上会抛错）：{offenders}"
    )


def test_build_script_pip_helper_does_not_swallow_pip_output() -> None:
    """``Invoke-Pip`` 的返回值必须是**一个整数**，否则每次安装都被判成失败。

    pip 的 stdout 一旦被 ``$code = Invoke-Pip ...`` 一起捕获，``$code`` 就成了
    数组 ``@(几十行输出..., 退出码)``；而 PowerShell 里「数组 -ne 0」是**过滤**
    语义（返回所有不等于 0 的元素），非空数组恒为真 —— 于是 ``if ($code -ne 0)``
    永远成立，构建死在「核心依赖安装失败」。实测复现过。

    所以 ``Invoke-Pip`` 要把输出交给 ``Invoke-Native``（它用 ``Out-Host`` /
    ``Out-Null`` 把输出从成功流里摘出去），自己只 ``return`` 一个退出码。
    """
    source = strip_ps_comments(read_build_script())
    assert re.search(r"return Invoke-Native -Program \$Python", source), (
        "Invoke-Pip 必须通过 Invoke-Native 跑 pip（输出不进成功流），只返回退出码"
    )
    assert re.search(r"\|\s*Out-Host", source), "输出应当去 Out-Host，不能留在成功流里"
    assert re.search(r"\|\s*Out-Null", source), "需要静默的地方应当去 Out-Null"


def test_build_script_routes_native_commands_through_the_helper() -> None:
    """原生程序必须走 ``Invoke-Native``，不能直接 ``&`` 调用。

    PowerShell 5.1 在 ``$ErrorActionPreference = 'Stop'`` 下，只要原生程序往
    **stderr** 写一行就抛 ``NativeCommandError`` 当场终止脚本 —— 哪怕它退出码是 0。
    而这类输出是家常便饭：curl 的下载进度条、pip 的弃用警告、robocopy 的统计。
    实测就是 curl 的进度条把「2/8 准备内嵌 Python runtime」那一步打断的，
    报出来还是个 ``RemoteException``，完全指不到「EAP 太严」这个真因。

    ``Invoke-Native`` 临时把 EAP 放回 ``Continue``，改用退出码判断成败。
    """
    source = strip_ps_comments(read_build_script())
    pattern = re.compile(
        r"^\s*&\s*(\$?\w[\w.]*\.exe|\$RuntimePy|\$ToolsPy|\$exePath|\$CmdPath)\b",
        re.MULTILINE,
    )
    offenders = [match.group(0).strip() for match in pattern.finditer(source)]
    assert offenders == [], (
        f"这些原生调用没走 Invoke-Native（会被 stderr 输出打断）：{offenders}"
    )


def test_build_script_cleans_the_previous_runtime() -> None:
    """``Move-Item`` 遇到**已存在**的目标目录时，会把源目录移**进去**。

    所以不清掉上一轮的 ``build\\runtime``，第二次不带 ``-Clean`` 的构建就会得到
    ``runtime\\python\\python.exe``，然后在「找不到 python.exe」那一步失败 ——
    而报错信息完全指不到真正的原因。
    """
    source = strip_ps_comments(read_build_script())
    assert re.search(r"if \(Test-Path \$Runtime\) \{ Remove-Tree \$Runtime \}", source), (
        "构建前必须清掉上一轮的 runtime 目录"
    )


def test_build_script_never_calls_remove_item() -> None:
    """删目录一律走 ``Remove-Tree``（.NET API），不用 ``Remove-Item``。

    Windows PowerShell 5.1 的 ``Remove-Item -Recurse`` 有两个会**留下半棵残树**的
    毛病，而残树比慢更危险 —— 它会污染下一轮构建：

    * 路径超过 260 字符直接报「路径太长」并停下。而这里要删的恰恰是最深的两个：
      ``build\\runtime``（python-build-standalone 解出来的 site-packages）与
      ``build\\pywork``（PyInstaller 的工作目录）。
    * 遇到只读文件抛 ``UnauthorizedAccessException`` 也停下 —— ``Remove-Item -Force``
      会替你摘掉只读属性，``Directory.Delete`` 不会，所以 ``Remove-Tree`` 里要显式摘。

    给 .NET 的 API 传 ``\\\\?\\`` 前缀可以绕开 MAX_PATH。这两个理由写在脚本的函数注释里。
    """
    source = strip_ps_comments(read_build_script())
    assert "Remove-Item" not in source, (
        "删目录请用 Remove-Tree：Remove-Item 在长路径/只读文件上会半途而废，留下残树"
    )
    assert "Remove-Tree" in source


def test_build_script_delete_helper_is_long_path_aware() -> None:
    """``Remove-Tree`` 必须给 .NET 传 ``\\\\?\\`` 前缀，否则绕不开 MAX_PATH。"""
    source = strip_ps_comments(read_build_script())
    assert r"'\\?\'" in source, r"删除辅助函数需要 \\?\ 前缀来支持长路径"
    assert "[System.IO.Directory]::Delete" in source
    assert "[System.IO.File]::Delete" in source
    assert "ReadOnly" in source, (
        "只读属性要显式摘掉：Directory.Delete 不像 Remove-Item -Force 那样替你处理"
    )


def test_build_script_tries_plain_delete_before_clearing_readonly() -> None:
    """先直接删，只在被只读文件挡住时才去摘属性。

    反过来写（无条件先把整棵树扫一遍）看着更「稳」，其实是把上万个文件多遍历一次 ——
    而 ``build\\runtime`` 有一万多个文件，那一趟的开销比删除本身还大，
    偏偏绝大多数文件根本不是只读的。

    断言要看**目录分支**内部：文件分支里本来就会先摘属性再删（单个文件没有
    「重试整棵树」这回事），拿全文找第一次出现的位置会被那一段先命中。
    """
    source = strip_ps_comments(read_build_script())
    body = source[source.index("function Remove-Tree") :]
    body = body[: body.index("Write-Step '0/8")]

    marker = "catch [System.UnauthorizedAccessException]"
    assert marker in body, "要专门接住「只读文件」这一种失败"

    attempted = body[: body.index(marker)]
    recovered = body[body.index(marker) :]
    assert "[System.IO.Directory]::Delete($full, $true)" in attempted, (
        "try 块里应当先直接删一次"
    )
    assert "Clear-ReadOnly -Path $full" in recovered, "只在被只读文件挡住后才摘属性"
    assert "[System.IO.Directory]::Delete($full, $true)" in recovered, "摘完属性要重删一次"


def test_build_script_checks_tools_by_exe_name() -> None:
    """工具检查必须写 ``curl.exe``，不能写 ``curl``。

    在 Windows PowerShell 5.1 里 ``curl`` 是个**别名**，指向 ``Invoke-WebRequest`` ——
    于是 ``Get-Command curl`` 永远成功，「缺工具就早退」这个检查成了摆设，
    真到下载那一步才炸（而那时已经跑了好几分钟）。查 ``curl.exe`` 才是真的查文件。
    """
    source = strip_ps_comments(read_build_script())
    match = re.search(r"foreach \(\$tool in @\(([^)]*)\)\)", source)
    assert match, "找不到工具检查那一段"
    checked = match.group(1)
    for tool in ("tar.exe", "curl.exe", "robocopy.exe"):
        assert f"'{tool}'" in checked, f"工具检查里应当查 {tool}"
    assert "'curl'" not in checked, "不能查裸 curl：那是 PowerShell 的别名，永远存在"


def test_build_script_clears_inherited_python_env() -> None:
    """构建前必须清掉继承来的 ``PYTHONPATH`` 等变量。

    脚本跑的是**自带解释器**的构建，但子进程会原样继承父进程的环境。
    ``PYTHONPATH`` 一旦漏进去，构建机上路径里的 ``sitecustomize.py`` 会被
    **自动执行**，而且 PyInstaller 的分析可能把不相干的模块打进交付的 exe。

    这不是假想问题：本项目的构建机上就带着一个 ``PYTHONPATH``（指向 IDE 的垫片），
    它让 ``ensurepip`` 在创建构建工具 venv 时退出 1 —— 报错只说
    「ensurepip 返回非零」，一个字都没提 ``PYTHONPATH``。
    """
    source = strip_ps_comments(read_build_script())
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        assert f"'{name}'" in source, f"应当清掉继承来的 {name}"
    assert "SetEnvironmentVariable" in source, "清环境变量要用 .NET API（纯内存操作）"
    assert "PYTHONNOUSERSITE" in source, (
        "还要挡住用户级 site-packages：它会遮蔽包里那份依赖"
    )


def test_build_script_tools_venv_verifies_pip() -> None:
    """构建工具 venv 的判据必须是「pip 能用」，不是「python.exe 在」。

    venv 是分步建的：目录与 ``Scripts\\python.exe`` 先落盘，``ensurepip`` 在后面。
    ensurepip 失败时目录**已经存在** —— 只看 python.exe 会把半成品当成品，
    下次构建跳过创建，然后在 ``$ToolsPy -m pip install`` 那里报一个
    跟真实原因（venv 没建成）毫不相干的错。
    """
    source = strip_ps_comments(read_build_script())
    assert re.search(r"\$ToolsPy\s+-Arguments\s+@\('-m',\s*'pip',\s*'--version'\)", source), (
        "创建 venv 后要探测一次 pip 才算数"
    )
    assert re.search(r"if \(-not \$toolsOk\) \{", source), "venv 不可用时要重建，不能直接复用"


def test_build_script_smoke_test_isolates_the_user_data_dir() -> None:
    """冒烟测试必须把 ``%LOCALAPPDATA%`` 指到临时目录，并在结束时恢复。

    否则每构建一次，构建机的真实 ``%LOCALAPPDATA%\\KnowledgeFlow`` 就被写一遍 ——
    而这个目录里会有 ``settings.json``（内含 API Key）。
    """
    source = strip_ps_comments(read_build_script())
    assert "LOCALAPPDATA" in source
    assert "smoke-home" in source
    assert "finally" in source, "临时 LOCALAPPDATA 必须在 finally 里恢复"


def _cmd_entry_region() -> str:
    """只取构建脚本里生成 ``KnowledgeFlow.cmd`` 的那一小段（已剥注释）。

    从 ``$CmdPath`` 一直到 ``Set-Content`` **那一行结束** —— 必须含这一行：
    ``-join "`r`n"`` 就写在上面，只取到行首会把要断言的东西切掉。
    """
    body = strip_ps_comments(read_build_script())
    start = body.index("$CmdPath")
    set_content = body.index("Set-Content", start)
    end = body.index("\n", set_content)
    assert start < set_content < end, "构建脚本里 .cmd 的生成段落结构变了"
    return body[start:end]


def test_build_script_writes_the_cmd_with_crlf() -> None:
    """生成的 ``KnowledgeFlow.cmd`` 必须是 **CRLF** 行尾，而且要显式拼。

    cmd.exe **不认 LF-only 的批处理**：它把注释行按错误的边界切开，把 ``rem``
    后面的片段当命令去执行。实测（同一份内容，只换行尾）：

    * LF   → 用户每次跑 .cmd 都多看到三行「'-status' 不是内部或外部命令」这类噪音
    * CRLF → 干净

    而**不能靠 here-string 的行尾** —— 它跟着 ``.ps1`` 自己的行尾走，仓库里这份
    ``.ps1`` 是 LF（git 的 autocrlf 归一化过），从干净克隆构建出来必然又是 LF。
    所以必须「行数组 + ``-join "`r`n"``」。
    """
    region = _cmd_entry_region()
    assert "`r`n" in region, "生成的 .cmd 必须显式拼 CRLF 行尾"
    assert "'@echo off'" in region, "应当用「行数组 + -join」生成 .cmd，别用 here-string"


def test_generated_cmd_lines_are_pure_ascii() -> None:
    """生成的 ``.cmd`` 每一行都必须是纯 ASCII。

    批处理是**按字节**解析的：GBK 汉字的次字节可能正好落在 cmd 的特殊字符上
    （``|`` 0x7C、``&`` 0x26、``<`` 0x3C、``>`` 0x3E、``^`` 0x5E），那时行会被
    从中间切开 —— 而且这跟控制台码页无关。这一次的中文注释恰好没踩上
    （改成 CRLF 就干净了），但那是运气；注释写成 ASCII 就没有这个运气成分。

    剥掉注释再查：中文说明**故意**放在 ``#`` 注释里，那里不会进 .cmd。
    """
    region = _cmd_entry_region()
    offenders = sorted({ch for ch in region if not ch.isascii()})
    assert not offenders, f".cmd 的生成代码里出现非 ASCII 字符：{offenders}"


def test_acceptance_script_imports_only_stdlib() -> None:
    """``scripts/accept_windows_x64.py`` 只能用标准库。

    它要在**用户自己的** Python 上跑（不是内嵌 runtime、也不是本仓库的 venv）——
    一旦引入第三方依赖，想验包就得先装东西，验收门槛比用包还高。
    用 ``ast`` 取真实 import，别在源码里搜字符串（注释里的包名会骗过文本断言）。
    """
    allowed = set(sys.stdlib_module_names)
    tree = ast.parse(ACCEPT_SCRIPT.read_text(encoding="utf-8"))
    bad: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in allowed:
                    bad.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                continue
            root = (node.module or "").split(".")[0]
            if root and root not in allowed:
                bad.add(node.module or "")
    assert bad == set(), f"验收脚本引入了第三方依赖：{sorted(bad)}"


def test_acceptance_script_checks_the_cmd_line_endings() -> None:
    """验收脚本必须真的查 ``.cmd`` 的行尾与字符集。

    这正是它抓到的那个缺陷（LF 行尾 → cmd.exe 把注释当命令执行）。
    断言用源码里的真实特征，而不是随便搜个词：既要数裸 LF，也要数非 ASCII 字节。
    """
    source = ACCEPT_SCRIPT.read_text(encoding="utf-8")
    assert "bare_lf" in source and "non_ascii" in source, "验收脚本没查 .cmd 的行尾/字符集"
    assert "不是内部或外部命令" in source, "验收脚本没断言 cmd.exe 的解析噪音"


def test_build_script_verifies_the_generated_cmd_at_build_time() -> None:
    """构建脚本自己也要体检它生成的 ``.cmd``（错了当场失败，别等用户双击）。

    验收脚本是**手动**跑的，而这个缺陷是构建脚本自己引入的 —— 所以第 7 步必须
    当场查一遍行尾与字符集。
    """
    smoke = _smoke_test_body()
    assert "bareLf" in smoke and "nonAscii" in smoke, "冒烟测试没查 .cmd 的行尾/字符集"
    assert "体检通过" in smoke, "冒烟测试应当把 .cmd 的体检结果打出来"


def test_build_script_smoke_test_looks_like_a_user_machine() -> None:
    """冒烟测试必须清掉构建机上的 ``PYTHONIOENCODING`` / ``PYTHONUTF8``。

    构建机上常设这两个变量（本脚本第 0 步就设了 ``PYTHONUTF8``），而它们会让
    内嵌解释器绕过 GBK 输出问题 —— 于是「中文 Windows 上 ``--check`` 抛
    ``UnicodeEncodeError``」这个真实缺陷在构建机上**永远测不出来**。
    实测正是这样漏过去的：exe 在用户的 GBK 控制台上退出码 1、stdout 一个字节
    都没有，而这里的冒烟测试却「通过」了。
    """
    smoke = _smoke_test_body()
    assert "$env:PYTHONIOENCODING = $null" in smoke
    assert "$env:PYTHONUTF8 = $null" in smoke
    assert "finally" in smoke, "清掉的环境变量必须在 finally 里恢复"


def test_build_script_smoke_test_captures_and_asserts_the_exe_output() -> None:
    """exe 自检必须**捕获并断言输出**，不能只看退出码。

    只看退出码时，「找不到自己的安装目录」和「编码炸了」都表现为退出码 1，
    而 stdout 一个字节都没有 —— 报错时给不出任何线索。还要断言 ``console.log``
    真的产出了：双击启动时用户看不到任何输出，那份日志是唯一的线索。
    """
    smoke = _smoke_test_body()
    assert re.search(r"\$exePath[^\n]*-Capture", smoke), "exe 自检必须捕获输出"
    assert "console.log" in smoke, "必须断言 console.log 真的产出"
    assert re.search(r"console\.log[\s\S]{0,600}-notmatch", smoke), (
        "console.log 的内容也要断言，不能只判断文件存在"
    )


def test_build_script_reports_media_status_honestly() -> None:
    """可选能力装失败时 BUILD-INFO 必须如实写「未包含」。

    早先 macOS 版这里只看命令行开关，结果可选依赖装失败时 BUILD-INFO 仍写
    「已包含 ASR+OCR」，而包里根本没有那两个模块 —— **交付物在说谎**。
    """
    source = strip_ps_comments(read_build_script())
    assert "MediaStatus" in source
    assert "复核" in source, "说「已包含」就必须真的 import 一次复核"


def test_app_requirements_exclude_developer_tools() -> None:
    """装进用户机器的东西里不该有测试框架和浏览器驱动（playwright 135MB）。

    先剥注释 —— 依赖文件里正好写了「去掉的：playwright / pytest / coverage」，
    不剥的话断言会被这句说明命中。
    """
    core = strip_hash_comments(
        (WIN_PKG / "requirements-app-core.txt").read_text(encoding="utf-8")
    )
    for banned in ("playwright", "pytest", "coverage"):
        assert banned not in core, f"打包依赖里不该有 {banned}"


def test_av_is_pinned_below_19() -> None:
    """PyAV 19 删了 ``metadata_errors``，faster-whisper 会直接 ``TypeError``。

    这个坑之前踩过（av 19.0.0 必炸、18.1.0 正常），打包依赖里必须钉住。
    """
    media = (WIN_PKG / "requirements-app-media.txt").read_text(encoding="utf-8")
    assert re.search(r"^av>=11,<19\s*$", media, re.MULTILINE), "av 必须 <19"


def test_windows_and_macos_requirements_stay_in_sync() -> None:
    """两个平台的**后端依赖必须逐行一致**：后端依赖本来就不分平台，
    只有 wheel 不同。分开维护是为了各自可发布，不是为了让它们分叉。"""
    macos_pkg = REPO_ROOT / "packaging" / "macos"

    def packages(path: Path) -> list[str]:
        return sorted(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    for name in ("requirements-app-core.txt", "requirements-app-media.txt"):
        assert packages(WIN_PKG / name) == packages(macos_pkg / name), (
            f"{name} 在两个平台上不一致"
        )


# --------------------------------------------------------------------------- #
# ② 端到端层：真的起一次后端
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_status(url: str, *, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _wait_status(url: str, *, want: int, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_status(url) == want:
            return True
        time.sleep(0.4)
    return False


def _launcher_env(fake_home: Path, vault: Path) -> dict[str, str]:
    """构造一台「干净的 Windows」的运行环境。"""
    env = dict(os.environ)
    env.update(
        {
            # 三个一起设：Windows 上 Path.home() 只认 USERPROFILE，
            # 而数据目录优先读 LOCALAPPDATA。少任何一个都会写到真实用户目录。
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
            "LOCALAPPDATA": str(fake_home / "AppData" / "Local"),
            "KF_SETUP_NONINTERACTIVE": "1",
            "KF_VAULT_PATH": str(vault),
            # 假 Key：非交互向导只校验 Vault，不发起真实模型调用
            "KF_LLM_API_KEY": "sk-not-a-real-key-for-tests",
            "KF_LLM_PROVIDER": "deepseek",
            "KF_LLM_MODEL": "deepseek-flash",
            # 「内嵌 runtime」在测试里就是当前解释器所在的 venv。
            # **不能用 Path(sys.executable).resolve()** —— venv 的
            # Scripts/python.exe 在某些布局下会被解析成基础解释器，
            # 而基础解释器里没有 fastapi。
            "KF_DEV_RUNTIME": str(Path(sys.prefix)),
        }
    )
    for stale in ("OBSIDIAN_VAULT_PATH", "DATABASE_URL", "DOWNLOAD_DIR"):
        env.pop(stale, None)
    # **清掉 stdio 编码相关的变量**，让启动器面对和真实用户一样的环境。
    #
    # 构建机/CI 上常设 ``PYTHONIOENCODING=utf-8``（``PYTHONUTF8=1`` 也会把 stdio
    # 变成 UTF-8），而**恰恰是这两个变量的缺席**才暴露缺陷：中文 Windows 上
    # ``--check`` 的 stdout 是个管道，编码是 GBK，而 ``✓``（U+2713）不在 GBK 码表里。
    # 留着它们，``test_check_never_prints_a_traceback`` 就永远测不出
    # ``UnicodeEncodeError``（构建脚本第 7 步踩过同一个坑，那里也做了同样的清理）。
    for var in ("PYTHONIOENCODING", "PYTHONUTF8"):
        env.pop(var, None)
    return env


def _spawn_launcher(env: dict[str, str], *args: str) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, "-m", "kf_app.main", *args],
        cwd=str(LAUNCHER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _app_home(fake_home: Path) -> Path:
    return fake_home / "AppData" / "Local" / "KnowledgeFlow"


def _diagnostics(env: dict[str, str], fake_home: Path) -> str:
    """失败时把能看的都打出来 —— 不然只有一个 assert False 什么都查不出。"""
    parts = []
    for name in ("launcher.log", "app.log", "console.log"):
        path = _app_home(fake_home) / "logs" / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")[-3000:]
            parts.append(f"--- {name} ---\n{text}")
    return "\n".join(parts) or "(没有任何日志 —— 启动器可能根本没跑起来)"


def _snapshot_repo_data() -> dict[str, int]:
    """记录仓库 ``backend/data/`` 里的文件与大小，用来断言「没有新增」。

    为什么不直接断言「文件不存在」：这个目录可能有更早测试留下的空库，
    那样断言会因为无关的历史文件而失败。
    """
    data_dir = BACKEND_ROOT / "data"
    if not data_dir.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in data_dir.iterdir() if p.is_file()}


def _terminate(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
        proc.kill()
        proc.wait(timeout=10)


def _force_cleanup(env: dict[str, str], port: int) -> None:
    """收干净：先走 ``--stop``（应用自己的退出路径），再兜底 taskkill 监听者。

    Windows 没有 SIGTERM：``Popen.terminate()`` 就是 ``TerminateProcess``，
    启动器拿不到任何「该收尾了」的信号，``finally`` 不会执行 ——
    直接杀启动器**一定会留下孤儿后端**。所以清理必须自己来。
    """
    try:
        subprocess.run(
            [sys.executable, "-m", "kf_app.main", "--port", str(port), "--stop"],
            cwd=str(LAUNCHER_DIR),
            env=env,
            capture_output=True,
            timeout=90,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass
    pid = kf_service._listener_pid(port)
    if pid:
        kf_service.taskkill(pid, force=True)


@pytest.fixture
def clean_machine(tmp_path: Path) -> tuple[Path, Path]:
    """一台「干净的 Windows」：空的用户目录 + 一个空 Vault。"""
    home = tmp_path / "fake-home"
    vault = tmp_path / "ObsidianVault"
    home.mkdir()
    vault.mkdir()
    return home, vault


#: Windows 启动器的**进程管理**是 Windows 专属：``taskkill /T`` / ``netstat -ano``
#: / ``CREATE_NO_WINDOW`` 在 POSIX 上要么不存在、要么语义不同。下面这条端到端用例
#: 测的正是那部分，所以只在 Windows 上跑（macOS 侧的对等场景见
#: ``test_macos_packaging.py`` 的 posix_only 用例）。
windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows 便携版的进程管理依赖 taskkill / netstat / CREATE_NO_WINDOW",
)


@windows_only
def test_end_to_end_start_serve_status_stop_and_reuse(
    clean_machine: tuple[Path, Path],
) -> None:
    """定稿验收一起验：

    启动器（真实子进程）→ 向导（非交互）→ 起后端 → Web UI 可访问 →
    SQLite 落在临时用户数据目录 → 重复启动复用同一实例 → ``--status`` 如实报告 →
    ``--stop`` 之后**不留孤儿进程**、启动器自己也退出 → 再启动复用同一个数据库。
    """
    home, vault = clean_machine
    port = _free_port()
    env = _launcher_env(home, vault)
    base = f"http://127.0.0.1:{port}"

    # 前置守卫：这个端口必须是干净的。否则启动器会「复用已有服务」直接退出，
    # 下面的断言会以一个完全看不懂的方式失败（被上一轮的孤儿进程坑过）。
    assert _http_status(f"{base}/api/health") is None, (
        f"端口 {port} 上已经有服务在跑了 —— 先清掉残留进程再跑测试"
    )

    repo_data_before = _snapshot_repo_data()
    proc = _spawn_launcher(env, "--port", str(port), "--no-browser", "--no-window")
    try:
        assert _wait_status(f"{base}/api/health", want=200), (
            f"后端没能起来\n{_diagnostics(env, home)}"
        )

        # ① Web UI 可访问
        assert _http_status(f"{base}/") == 200, "Web 界面打不开"

        # ② 配置与数据库落在临时用户数据目录
        app_home = _app_home(home)
        assert (app_home / "config" / "settings.json").is_file(), "配置没写进用户数据目录"
        assert (app_home / "data" / "knowledgeflow.db").is_file(), "SQLite 没建在用户数据目录"

        # ③ 数据没有跑到仓库里去。
        # 不能断言「仓库里没有 knowledgeflow.db」—— 仓库里可能早就有一个
        # 更早的测试留下的空库。要断言的是**本次运行没有新增**文件。
        assert _snapshot_repo_data() == repo_data_before, (
            "本次运行往仓库的 backend/data/ 里写了文件 —— 用户数据位置错了"
        )

        # ④ 单实例：再起一次应当复用，而不是撞端口
        # 启动器往**管道**写中文时按本机码页（中文 Windows 上是 GBK）编码，
        # 所以一律用 `_decode_console` 解，不能只按 UTF-8 解。
        second = _spawn_launcher(env, "--port", str(port), "--no-browser", "--no-window")
        out, _ = second.communicate(timeout=90)
        text = _decode_console(out)
        assert second.returncode == 0, text
        assert "已经在运行" in text, text
        assert _http_status(f"{base}/api/health") == 200, "第二次启动把服务搞挂了"

        # ⑤ 状态查询
        status = _spawn_launcher(env, "--port", str(port), "--status")
        status_out, _ = status.communicate(timeout=90)
        status_text = _decode_console(status_out)
        assert status.returncode == 0, status_text
        assert "运行中" in status_text, status_text

        # ⑥ 退出：Windows 上没有 SIGTERM，正式路径是 --stop
        stop = _spawn_launcher(env, "--port", str(port), "--stop")
        stop_out, _ = stop.communicate(timeout=90)
        stop_text = _decode_console(stop_out)
        assert stop.returncode == 0, stop_text
        assert "已停止" in stop_text, stop_text

        # ⑦ 后端没了之后，启动器自己应当退出（它一直在轮询子进程）
        assert proc.wait(timeout=60) == 0, (
            f"后端停掉之后启动器没有自己退出\n{_diagnostics(env, home)}"
        )
    finally:
        _force_cleanup(env, port)
        _terminate(proc)

    # ⑧ 收干净：端口应当被释放（不留孤儿进程）
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _http_status(f"{base}/api/health") is not None:
        time.sleep(0.4)
    assert _http_status(f"{base}/api/health") is None, (
        f"退出后后端仍在跑 —— 留下孤儿进程了\n{_diagnostics(env, home)}"
    )

    # ⑨ 再次启动能恢复，并复用同一个数据库
    again = _spawn_launcher(env, "--port", str(port), "--no-browser", "--no-window")
    try:
        assert _wait_status(f"{base}/api/health", want=200), (
            f"重启失败\n{_diagnostics(env, home)}"
        )
        assert (_app_home(home) / "data" / "knowledgeflow.db").is_file(), "重启后数据库不见了"
    finally:
        _force_cleanup(env, port)
        _terminate(again)


def test_stop_command_reports_when_nothing_runs(clean_machine: tuple[Path, Path]) -> None:
    """没停掉任何东西就要说「没有在运行」，不能谎报「已停止」。"""
    home, vault = clean_machine
    env = _launcher_env(home, vault)
    port = _free_port()
    proc = _spawn_launcher(env, "--port", str(port), "--stop")
    out, _ = proc.communicate(timeout=90)
    text = _decode_console(out)  # 中文 Windows 上启动器按 GBK 写管道
    assert proc.returncode == 0, text
    assert "没有在运行" in text, text


def test_status_reports_not_running_with_a_nonzero_exit(
    clean_machine: tuple[Path, Path],
) -> None:
    """``--status`` 未运行时要给非零退出码 —— 脚本靠它判断。"""
    home, vault = clean_machine
    env = _launcher_env(home, vault)
    port = _free_port()
    proc = _spawn_launcher(env, "--port", str(port), "--status")
    out, _ = proc.communicate(timeout=90)
    text = _decode_console(out)  # 中文 Windows 上启动器按 GBK 写管道
    assert proc.returncode == kf_main.EXIT_ERROR, text
    assert "未运行" in text, text


def test_check_never_prints_a_traceback(clean_machine: tuple[Path, Path]) -> None:
    """给用户看的输出里**不许**出现 traceback / ModuleNotFoundError。

    这一条同时守着**输出编码**。启动器的 stdout 在这里是个管道 —— 中文 Windows
    上它的编码就是 GBK，而 ``environment_summary`` 原本用的是 ``✓``（U+2713），
    不在 GBK 码表里。修之前这里必然抛 ``UnicodeEncodeError``：
    退出码 1、stdout **一个字节都没有**，用户唯一的线索是一行
    ``'gbk' codec can't encode character '\\u2713'``。

    两个配套细节，缺一这条就测不出东西：

    * 输出要按 ``_decode_console`` 解（先 UTF-8 后退 GBK）—— 只按 UTF-8 解会得到
      乱码，于是「中文断言失败」而看不出真正的原因；
    * ``_launcher_env`` 必须清掉 ``PYTHONIOENCODING`` / ``PYTHONUTF8`` ——
      否则构建机上顺手设的 ``utf-8`` 会把这个缺陷整个盖掉。
    """
    home, vault = clean_machine
    env = _launcher_env(home, vault)
    proc = _spawn_launcher(env, "--check")
    out, _ = proc.communicate(timeout=90)
    text = _decode_console(out)
    assert proc.returncode == 0, text
    assert "Traceback" not in text
    assert "ModuleNotFoundError" not in text
    assert "UnicodeEncodeError" not in text
    assert "后端程序" in text
    assert "内置运行环境" in text
