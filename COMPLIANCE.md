# 合规与数据说明 · Compliance & Data Notes

> 这份文档把 KnowledgeFlow 的**行为边界**集中说清楚：它存了什么、发给谁、你怎么撤回。
> 内容全部对应仓库里的真实实现，每条都能在代码里找到出处。
>
> 这是一份**行为说明**，不是法律意见。你的具体使用场景是否合规，需要你自己判断。

**English summary** is at the [bottom of this file](#english-summary).

---

## 一、它存了你什么

| 内容 | 位置（macOS） | 位置（Windows） | 保护措施 |
|---|---|---|---|
| API Key、Vault 路径、模型配置 | `~/Library/Application Support/KnowledgeFlow/config/settings.json` | `%LOCALAPPDATA%\KnowledgeFlow\config\settings.json` | macOS 写 `0600`；Windows 用 `icacls` 断掉继承、只留当前用户 |
| 抖音登录态（可选） | 同上，`DOUYIN_COOKIE` 字段 | 同上 | 同左；**值永不回显、永不进日志** |
| 采集的内容与结构化分析 | `.../data/knowledgeflow.db`（SQLite） | 同结构 | 仅本机文件权限 |
| 下载的媒体文件 | `.../data/media/` | 同结构 | 仅本机文件权限 |
| 运行日志 | `.../logs/app.log` | 同结构 | 日志脱敏（见下） |

**用户数据绝不写在 `.app` / 解压目录里** —— 升级替换程序不会动你的数据，
删除程序也不会连数据一起删。这是刻意的设计（见 README 打包章节）。

### 两条硬性约束（代码级）

1. **敏感值不进日志**：`backend/app/logging_config.py` 里有一个禁止写入日志的键名清单
   （`api_key` / `token` / `cookie` / `authorization` / `signature` 等），
   并有一条正则对 `key=value` 形态做二次脱敏。
2. **`/api/health` 不回显任何密钥与本机路径** —— 它只返回 provider 名、模型名、
   vault 是否已配置、以及启动恢复报告。

---

## 二、它把数据发给谁

### 网络请求只去这些地方

后端域名白名单（`backend/app/normalization/url.py` 的 `DEFAULT_ALLOWED_HOSTS`），
白名单外一律 `DOMAIN_NOT_ALLOWED` 拒绝：

```
douyin.com · iesdouyin.com · bilibili.com · b23.tv
youtube.com · youtu.be · x.com · twitter.com
github.com · xiaohongshu.com · xhslink.com · mp.weixin.qq.com
```

> 说明：这些域名是**解析链接**用的白名单。当前实际采集链路已实现的是
> **抖音**与**手动粘贴**；其余域名在白名单内，属于为后续来源预留。

### 你的内容会离开本机的唯一情况

**调用 LLM 分析时**，待分析文本会发给你自己配置的 LLM 服务商
（默认 `openai` 兼容接口，项目开发与演示使用 DeepSeek）。
这是本项目的核心功能，无法在不联网的情况下完成结构化分析。

- 用的是**你自己的 API Key**，请求直接发往你配置的 `*_BASE_URL`，**不经过本项目作者**
- 若你不希望内容离开本机，**不要配置 LLM**，则只能使用「手动粘贴」以外的能力会受限；
  纯离线能力（ASR/OCR）仅是链路中的一环，不能独立产出笔记

### 抖音采集的合规边界（第二十九条）

- 只对**公开可访问的页面**做 GET / HEAD
- **不伪造签名**、**不破解加密**、**不绕过验证码**
- Cookie **只接受你自己显式填写的值**，不窃取、不伪造
- 被平台挡住时**如实抛错**（`REQUEST_BLOCKED` / `COOKIE_REQUIRED`），
  不做重试绕过 —— 这条边界由 `backend/tests/test_extension.py` 等测试钉死
- 采集失败**绝不伪造** `RawContent`，不做静默 `except: pass`

> ⚠️ **使用你自己的登录态，不改变采集方式本身要遵守的平台规则。**
> "身份合法"与"方式合规"是两件事。相关约束请见
> [抖音用户服务协议](https://www.douyin.com/agreements/?id=6773901168964798477)。

### 浏览器扩展的网络面

`extension/manifest.json` 的权限范围：

| 项 | 值 | 含义 |
|---|---|---|
| `permissions` | `activeTab`, `storage` | 只在你点击图标时访问当前标签页 |
| `host_permissions` | `http://127.0.0.1:8000/*` | **只连本机**，无公网地址、无 `<all_urls>` |

后端 CORS **只放行 `chrome-extension://`** 来源（正则 `^chrome-extension://[a-p]{32}$`），
**绝不放 `*`**。

**这个防线的已知局限**（README 里已主动写明）：它拦不住"你自己装的另一个扩展"，
因为任何已装扩展都满足那个来源格式。服务只绑 `127.0.0.1`，把暴露面缩到"本机能跑的东西"。

---

## 三、你怎么撤回

| 想撤销 | 怎么做 |
|---|---|
| 抖音登录态 | 编辑配置文件删掉 `DOUYIN_COOKIE` 的值，重启服务 |
| API Key | 编辑配置文件清空对应字段；再去服务商后台吊销该 Key |
| 开机自启 | `scripts/install_autostart.sh --uninstall`（macOS，会停掉并移除） |
| 停止服务 | `KnowledgeFlow --stop`（macOS）/ `KnowledgeFlow.cmd --stop`（Windows） |
| 卸载扩展 | `chrome://extensions/` → 移除 KnowledgeFlow |
| **删除全部本地数据** | 删掉 `~/Library/Application Support/KnowledgeFlow/`（macOS）或 `%LOCALAPPDATA%\KnowledgeFlow\`（Windows） |
| 已写入 Obsidian 的笔记 | 笔记是你 vault 里的普通 Markdown 文件，自行删除即可 |

**没有云端账户，没有遥测，本项目作者无法访问你的任何数据。**

---

## 四、已知限制

- **扩展未上架**：需以"加载已解压的扩展程序"方式安装，Chrome 会提示停用开发者模式扩展，
  属未打包扩展的常态。
- **采集链路随平台变动**：抖音页面结构变化会导致采集失败（已实测经历过一次：
  页面从内嵌 JSON 改为 JS 壳页）。失败时如实报错，不伪造数据。
- **不配抖音登录态时**，抖音采集大概率撞验证页；此时请用「手动粘贴」入口，功能不受影响。
- **桌面安装包未做代码签名/公证**：macOS 首次打开需右键→打开；Windows 会被 SmartScreen 拦一次。
  **不要为此关闭系统安全功能**（README 有正确放行步骤）。

---

## English summary

**What it stores.** Configuration (API key, optional Douyin session, vault path) in
`config/settings.json` — mode `0600` on macOS, ACL-restricted to the current user on
Windows. Collected content and analysis in a local SQLite database. Nothing is written
inside the app bundle, so upgrading or deleting the app never touches your data.
**No cloud account, no telemetry — the author cannot access your data.**

**Where data goes.** URL resolution is restricted to a hard-coded host allowlist
(douyin.com, bilibili.com, youtube.com, x.com, github.com, xiaohongshu.com,
mp.weixin.qq.com, and related short-link domains); anything else is rejected. The only
time your content leaves the machine is when the LLM you configured analyzes it — using
**your own API key**, sent directly to **your** configured endpoint, never through the
project author.

**Douyin boundary.** Public pages only, GET/HEAD only. **No forged signatures, no broken
encryption, no CAPTCHA bypass.** Only a session you supply yourself. When blocked, it
reports the error and stops — the boundary is pinned by tests.

**Your responsibility.** Using your own session does not change the platform rules that
apply to automated collection. Review the
[Douyin Terms of Service](https://www.douyin.com/agreements/?id=6773901168964798477)
before use.

**How to revoke.** Clear `DOUYIN_COOKIE` or the API key from the config file and restart;
remove the extension via `chrome://extensions/`; delete
`~/Library/Application Support/KnowledgeFlow/` (macOS) or `%LOCALAPPDATA%\KnowledgeFlow\`
(Windows) to remove all local data.

**License.** MIT — see [LICENSE](LICENSE). This document describes behavior, not legal
advice.
