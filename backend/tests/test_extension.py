"""Chrome 扩展（Phase 10）的静态守卫。

真加载扩展需要人在 ``chrome://extensions`` 里点「加载已解压的扩展程序」，
自动化不了；所以这里把**能静态查的、而且错了会静默失灵的东西**全部钉住：

1. ``manifest.json`` 是合法的 MV3 清单
2. **没有远程代码**（MV3 强制：远程脚本一律拒收）
3. 清单与 HTML 里引用的文件**真的存在**（浏览器只会静默 404）
4. 前端三条纪律：无 ``innerHTML``、无内联事件、``node --check`` 校验 JS
5. **只申请本机地址**（``host_permissions`` 不许出现公网 / 通配）
6. JS 里引用的每个 DOM id 都在 HTML 里（少了就是「点了没反应」）
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_web_ui import strip_js_comments

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = BACKEND_ROOT.parent / "extension"
MANIFEST = EXT_DIR / "manifest.json"
POPUP_HTML = EXT_DIR / "popup.html"
POPUP_JS = EXT_DIR / "popup.js"
POPUP_CSS = EXT_DIR / "popup.css"
URLMATCH_JS = EXT_DIR / "urlmatch.js"

#: JS 里引用的 DOM id。
GET_ID = re.compile(r'getElementById\("([^"]+)"\)')
#: HTML / JS 里出现的远程脚本地址。
REMOTE_URL = re.compile(r"https?://(?!127\.0\.0\.1)[^\s\"']+")


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_extension_dir_exists() -> None:
    for path in (MANIFEST, POPUP_HTML, POPUP_JS, POPUP_CSS):
        assert path.is_file(), f"扩展缺文件：{path}"


def test_manifest_is_valid_mv3() -> None:
    data = manifest()
    assert data["manifest_version"] == 3, "必须是 MV3（MV2 已被 Chrome 停用）"
    assert data["name"] == "KnowledgeFlow"
    assert data["action"]["default_popup"] == "popup.html"


def test_no_remote_code() -> None:
    """MV3 硬性要求：扩展包里**不能**加载远程脚本（会被直接拒收）。"""
    data = manifest()
    csp = data.get("content_security_policy", {})
    script_src = " ".join(csp.get("extension_pages", "").split())
    assert "http" not in script_src, f"CSP 里出现了远程地址：{script_src}"

    html = POPUP_HTML.read_text(encoding="utf-8")
    assert not re.search(r'<script[^>]+src="https?://', html), "HTML 引用了远程脚本"
    js = strip_js_comments(POPUP_JS.read_text(encoding="utf-8"))
    # 只有本地服务的地址允许出现在代码里（popup 要 fetch 它）
    for url in REMOTE_URL.findall(js):
        pytest.fail(f"JS 里出现了非本机地址：{url}")


def test_every_referenced_asset_exists() -> None:
    """HTML 里写了什么，磁盘上就得有什么 —— 少了浏览器只静默 404。"""
    html = POPUP_HTML.read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src)="([^":]+)"', html)
    assert refs, "没解析出任何资源引用（正则可能失效了）"
    for ref in refs:
        assert (EXT_DIR / ref).is_file(), f"popup.html 引用了不存在的 {ref}"

    data = manifest()
    for key in ("default_icon", "icons"):
        icons = data.get("action", {}).get(key) if key == "default_icon" else data.get(key)
        for _size, relative in (icons or {}).items():
            assert (EXT_DIR / relative).is_file(), f"清单引用了不存在的图标 {relative}"


def test_host_permissions_are_localhost_only() -> None:
    """只申请本机地址 —— 拿到 ``<all_urls>`` 等于能读你所有网页。"""
    hosts = manifest().get("host_permissions", [])
    assert hosts, "没有 host_permissions，扩展连不上本地服务"
    for host in hosts:
        assert "<all_urls>" not in host, f"申请了通配权限：{host}"
        assert host.startswith("http://127.0.0.1"), f"申请了非本机地址：{host}"


def test_no_inner_html_and_no_inline_handlers() -> None:
    source = strip_js_comments(POPUP_JS.read_text(encoding="utf-8"))
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\.(?:inner|outer)HTML\b|insertAdjacentHTML\s*\(", line)
    ]
    assert offenders == [], f"用了 innerHTML（XSS 风险）：{offenders}"

    html = POPUP_HTML.read_text(encoding="utf-8")
    assert re.findall(r"\son[a-z]+\s*=", html) == [], "HTML 里有内联事件处理器"


def test_javascript_syntax_is_valid() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过 JS 语法校验")
    result = subprocess.run(
        [node, "--check", str(POPUP_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"JS 语法错误：\n{result.stderr}"


def test_every_dom_id_referenced_by_js_exists_in_html() -> None:
    """JS 取的每个 id 都必须真的在 HTML 里 —— 少一个就是静默失灵。"""
    html = POPUP_HTML.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(GET_ID.findall(strip_js_comments(POPUP_JS.read_text(encoding="utf-8"))))
    assert used, "一个 getElementById 都没解析出来（正则可能失效了）"
    missing = sorted(used - ids)
    assert missing == [], f"popup.js 引用了 HTML 里不存在的 id：{missing}"


def test_popup_sends_the_douyin_ingest_request() -> None:
    """真的调采集接口、且要求跑完整 A 线 —— 只在文案里提一句不算「能用」。"""
    source = strip_js_comments(POPUP_JS.read_text(encoding="utf-8"))
    assert "/api/ingest/douyin" in source
    assert "process: true" in source


def test_douyin_host_detection() -> None:
    """判定逻辑：抖音域名放行，其它一律不放行（拿不到 URL 时也不许误判）。

    域名判定已经移到 ``urlmatch.js``（为了能被 node 真的跑一遍）。
    """
    source = URLMATCH_JS.read_text(encoding="utf-8")
    assert "douyin.com" in source and "iesdouyin.com" in source
    assert _node_run("KFUrlMatch.isDouyinHost('https://www.douyin.com/x')") == "true"
    assert _node_run("KFUrlMatch.isDouyinHost('https://douyin.com.evil.com/x')") == "false"
    assert _node_run("KFUrlMatch.isDouyinHost('not a url')") == "false"


def _node_run(expression: str) -> str:
    """用 node 真的跑一段 JS —— 判定逻辑不能只在文案里说「我们支持」。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过 JS 行为校验")
    script = f'require({str(URLMATCH_JS)!r});\nprocess.stdout.write(String({expression}));'
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"node 跑挂了：{result.stderr}"
    return result.stdout


VIDEO = "https://www.douyin.com/video/7684593425094789475"
HOME = "https://www.douyin.com/?recommend=1"


def test_video_url_yields_an_aweme_id() -> None:
    assert _node_run(f"KFUrlMatch.awemeIdOf({VIDEO!r})") == "7684593425094789475"


def test_home_page_has_no_aweme_id() -> None:
    """首页 / 频道页**不许**猜 —— 实测那里连一条视频链接都没有。"""
    assert _node_run(f"KFUrlMatch.awemeIdOf({HOME!r})") == "null"
    assert _node_run(f"KFUrlMatch.describe({HOME!r}).kind") == "noid"


def test_non_douyin_page_is_rejected() -> None:
    assert _node_run("KFUrlMatch.describe('https://www.bilibili.com/video/BV1').kind") == "other"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/7684593425094789475",
        "https://www.douyin.com/note/7684593425094789475",
        "https://www.douyin.com/?modal_id=7684593425094789475",
        "https://www.douyin.com/share/video/7684593425094789475",
    ],
)
def test_every_documented_url_shape_is_recognised(url: str) -> None:
    """与后端 ``AWEME_ID_PATTERNS`` 同口径：后端能解析的，扩展也得认。"""
    assert _node_run(f"KFUrlMatch.awemeIdOf({url!r})") == "7684593425094789475"


def test_path_like_video_list_is_not_an_id() -> None:
    """/video/list 这种路径不能当成 id —— 口径与后端一致（都要 6 位以上数字）。"""
    assert _node_run("KFUrlMatch.awemeIdOf('https://www.douyin.com/video/list')") == "null"


def test_popup_loads_urlmatch_before_popup_js() -> None:
    """popup.js 依赖 KFUrlMatch —— 脚本顺序反了就是运行时 undefined。"""
    html = POPUP_HTML.read_text(encoding="utf-8")
    assert html.index('src="urlmatch.js"') < html.index('src="popup.js"')
    assert "KFUrlMatch" in POPUP_JS.read_text(encoding="utf-8")


def test_error_types_are_translated_for_humans() -> None:
    """后端报 ``AWEME_ID_NOT_FOUND`` 给用户看等于没说 —— 必须翻译成下一步动作。"""
    source = POPUP_JS.read_text(encoding="utf-8")
    assert "AWEME_ID_NOT_FOUND" in source
    assert "COOKIE_REQUIRED" in source
