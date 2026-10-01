# 图片资源

README 与 GitHub 社交卡片用的图。**全部使用虚构示例数据** —— 仓库里不出现任何真实的
博主昵称、视频标题或本机路径。

| 文件 | 用在哪 |
|---|---|
| `pipeline.png` | README 顶部主图：「一条抖音 → 一份笔记」+ 三条差异点 |
| `web-ui-ingest.png` / `web-ui-library.png` | README「界面」段：Web 界面与内容库 |
| `extension-popup.png` | README「界面」段：Chrome 扩展面板 |
| `social-preview.png` | **不**在 README 里。上传到仓库 Settings → Social preview（1280×640），决定分享到微信/飞书/X 时那张卡片的缩略图 |

> 注意：这张是 **16:9 横版**，为 GitHub 社交卡而设计。要给小红书用（3:4 竖版）
> **不能直接裁** —— 流程链会被切断。需要另做一版竖向排版。
| `xhs-01-cover.png` · `xhs-02-why.png` · `xhs-03-install.png` | **小红书竖版三图**（3:4 · 2160×2880），发帖用。不是把横版裁一刀 —— 是单独排的版 |
| `src/*.html` | 设计源（改文案/配色改这里，再重新渲染） |

## 重新生成

```bash
# 需要 playwright + 本机 Chrome
python - <<'PY'
import asyncio, pathlib
from playwright.async_api import async_playwright
D = pathlib.Path("docs/images")
JOBS = [("src/social-preview.html", "social-preview.png", 1280, 640),
        ("src/pipeline.html",       "pipeline.png",       1600, 900)]
async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="chrome", headless=True)
        for src, out, w, h in JOBS:
            p = await b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            await p.goto(f"file://{D / src}", wait_until="load")
            await p.wait_for_timeout(400)
            await p.screenshot(path=str(D / out))
            await p.close()
        await b.close()
asyncio.run(main())
PY
```

界面截图（`web-ui-*.png` / `extension-popup.png`）是**真实界面代码 + 桩数据**渲染的：
把 `backend/app/web/index.html` 与 `extension/popup.html` 里的 `/style.css`、`/app.js`
换成 `file://` 绝对路径，并在脚本前插一段 `window.fetch` / `window.chrome` 桩即可。

## 小红书三图的重新生成

```python
# 渲染 3 张竖版（3:4 · 1080×1440，2x 出图）；第三张要嵌真实应用图标
p = await browser.new_page(viewport={"width": 1080, "height": 1440}, device_scale_factor=2)
await p.goto(f"file://{D/'src/xhs-3up.html'}?icon={D.parent/'packaging/macos/assets/AppIcon-1024.png'}")
for i, name in enumerate(["xhs-01-cover.png", "xhs-02-why.png", "xhs-03-install.png"], 1):
    await p.locator(f"#s{i}").screenshot(path=str(D/name))
```

**渲染后必须查每一张的溢出**（`scrollHeight - clientHeight`）—— 竖版画布高度固定 1440px，
内容超了会被静静裁掉，缩略图上根本看不出来（第一版第三张的界面截图就被裁断了半截）。
