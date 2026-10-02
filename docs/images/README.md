# 图片资源

README 与 GitHub 社交卡片用的图。**全部使用虚构示例数据** —— 仓库里不出现任何真实的
博主昵称、视频标题或本机路径。

| 文件 | 用在哪 |
|---|---|
| `pipeline.png` | README 顶部主图：「一条抖音 → 一份笔记」+ 三条差异点 |
| `demo-douyin.gif` | README「演示」①：**真实抖音链路**——粘整段分享文案 → 采集 → 13 步（5.7 秒）|
| `demo-web-ui.gif` | README「演示」②：填表 → 点击 → 13 步 → 出笔记（5.4 秒） |
| `demo-douyin.mp4` · `demo-web-ui.mp4` | 上面两段的视频版（社媒用；GIF 在小红书会被当静态图）|
| `web-ui-ingest.png` / `web-ui-library.png` | README「截图」段：Web 界面与内容库 |
| `extension-popup.png` | README「截图」段：Chrome 扩展面板 |
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

## 两段演示的重新生成（GIF + MP4）

采集脚本用 **Playwright 逐帧截图**（本机没有屏幕录制权限）：

- 起一个**临时库 + 临时 vault** 的真服务（`DATABASE_URL` / `OBSIDIAN_VAULT_PATH` 用环境变量覆盖）
- 驾驶真实 UI 走完整流程，每步 `page.screenshot(clip=...)`，同时记下**光标坐标**
- 合成时：补白到统一高度并**顶部对齐**（结果面板出现时帧会变高，缩放会让画面跳）、
  按语义给**逐帧时长**（等模型那两帧给 1000ms）、画光标与点击反馈圈

**两个必须记住的坑**：

1. **打码要先把「同一个值在画面上出现几次」数清楚。**
   第一版只遮了输入框与 `note_path`，抽帧一看发现结果面板里 `title`/`author`
   和两条步骤明细（`file=….md`）也含同一个真实标题 —— **半遮半露比不遮更糟**。
   要么全遮（结果面板会变马赛克、演示报废），要么不遮 + 把说明写精确。
   **结论是后者**：说清哪些是真实数据，比糊一层马赛克有用。
2. **合成后必须抽帧核对**（体积正常 ≠ 画面正确）。第一版的问题就是只看体积没看画面。

MP4 编码写**回退链**：`h264_videotoolbox` → `libx264` → `mpeg4`
（这个 PyAV 构建里 libx264 打不开，报 `avcodec_open2` 失败；且 PyAV 18 的编码参数
要设 `stream.codec_context.options`，设 `stream.options` 会被拒）。
