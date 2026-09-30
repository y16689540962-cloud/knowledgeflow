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
| `src/*.html` | 前两张的**设计源**（改文案/配色改这里，再重新渲染） |

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
