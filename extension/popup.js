/* KnowledgeFlow Chrome 扩展 —— 零构建原生 JS。
 *
 * 三条纪律（与 app/web/app.js 一致）：
 * 1. **所有外部文本一律走 textContent**，不拼 innerHTML。
 *    页面标题 / URL / 后端报错都是「别人给的字符串」。
 * 2. 失败要**看得见**：HTTP 状态码 + error_type 全显示，不折成一句「出错了」。
 *    尤其是「服务没起」—— 那是最常见的失败，必须说清楚该做什么。
 * 3. 不引入任何框架 / 构建步骤。
 *
 * 为什么扩展只做「把 URL 递给后端」：抖音现在只吐 JS 壳页，
 * 页面里 `document.title` 是「抖音」甚至「验证码中间页」，正文什么都没有。
 * 能从页面上拿到的唯一可靠信息就是 **URL**，而后端已经能把 URL
 * 变成 aweme_id → 详情接口 → 结构化笔记。
 */

const DEFAULT_API_BASE = "http://127.0.0.1:8000";
const STORAGE_KEY = "apiBase";

/* ---------- DOM（一律 textContent） ---------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setChip(id, text, kind) {
  const node = document.getElementById(id);
  node.textContent = text;
  node.className = `chip ${kind || ""}`.trim();
}

function kvList(pairs) {
  const dl = el("dl", "kv");
  for (const [key, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(el("dt", null, key), el("dd", null, value));
  }
  return dl;
}

/* ---------- 判定（口径在 urlmatch.js 里，和后端一致） ---------- */
function describeUrl(url) {
  return KFUrlMatch.describe(url);
}

/* 后端 error_type → 人话。后端如实报，翻译在这里做 ——
 * 「最终 URL 里没有 aweme_id」这种话给用户看等于没说。 */
const ERROR_HINTS = {
  AWEME_ID_NOT_FOUND:
    "这个地址里没有视频 id。请点开具体视频（地址栏含 /video/数字），或粘贴分享链接。",
  COOKIE_REQUIRED:
    "抖音要求登录态。在 backend/.env 里配 DOUYIN_COOKIE（你自己的），然后重启服务。",
  REQUEST_BLOCKED: "抖音返回了验证页，按合规约定不绕过 —— 换个时间再试。",
  DOMAIN_NOT_ALLOWED: "域名不在白名单内。",
  INVALID_URL: "地址里没找到链接。",
};

/* ---------- 后端 ---------- */
async function getJson(base, path) {
  const res = await fetch(base + path, { method: "GET" });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

async function postJson(base, path, body) {
  const res = await fetch(base + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

async function loadApiBase() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const value = stored[STORAGE_KEY];
  return typeof value === "string" && value.trim() ? value.trim() : DEFAULT_API_BASE;
}

/* ---------- 结果渲染 ---------- */
function showError(title, detail) {
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.remove("hidden");
  box.append(el("span", "badge failed", title));
  if (detail) box.append(el("div", "error-box", detail));
}

function showIngestError(status, body) {
  const type = body && body.error_type ? body.error_type : "";
  const hint = ERROR_HINTS[type] || "";
  showError(`HTTP ${status}${type ? " · " + type : ""}`, hint || (body && body.message) || "");
}

function showResult(data) {
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.remove("hidden");

  const result = data.result || {};
  const raw = data.raw || {};
  box.append(el("span", `badge ${result.outcome || "?"}`, result.outcome || "?"));

  box.append(
    kvList([
      ["标题", raw.title],
      ["作者", raw.author],
      ["aweme_id", raw.source_id],
      ["笔记", result.note_path],
      ["claims", result.claim_count],
    ])
  );

  if (result.error_type) {
    box.append(el("div", "error-box", `${result.error_type} · ${result.error_message || ""}`));
  }
}

/* ---------- 主流程 ---------- */
async function probeService(base) {
  setChip("status", "检查服务…", "");
  try {
    const { body } = await getJson(base, "/api/health");
    if (body && body.status === "ok") {
      setChip("status", `${body.llm_provider} · vault 已配置`, "ok");
      return true;
    }
    setChip("status", "服务返回异常", "bad");
    return false;
  } catch {
    setChip("status", "服务未启动", "bad");
    showError(
      "连不上本地服务",
      `请先启动：cd backend && ../.venv/bin/python scripts/serve.py（当前地址 ${base}）。` +
        "装了开机自启的话用 scripts/install_autostart.sh --status 看一下。"
    );
    return false;
  }
}

async function onClip(base, url) {
  const button = document.getElementById("btn-clip");
  button.disabled = true;
  button.textContent = "分析中…（约 5–30 秒）";
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.add("hidden");

  try {
    const { status, body } = await postJson(base, "/api/ingest/douyin", {
      url,
      process: true,
    });
    if (status >= 400) {
      showIngestError(status, body);
      return;
    }
    showResult(body);
  } catch (err) {
    showError("请求失败", String(err && err.message ? err.message : err));
  } finally {
    button.disabled = false;
    button.textContent = "采集并写入 Obsidian";
  }
}

async function main() {
  const base = await loadApiBase();
  const input = document.getElementById("api-base");
  input.value = base;
  input.addEventListener("change", async () => {
    await chrome.storage.local.set({ [STORAGE_KEY]: input.value.trim() });
  });

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = (tab && tab.url) || "";
  const title = (tab && tab.title) || "";

  document.getElementById("page-title").textContent = title || "（无标题）";
  document.getElementById("page-url").textContent = url || "（拿不到 URL）";

  const verdict = document.getElementById("verdict");
  const serviceOk = await probeService(base);

  // 判定用「粘贴框优先，否则当前页」—— 首页 / 频道页拿不到 id 时，
  // 用户可以用抖音「复制链接」给的整段文案（后端会自动抠出链接）。
  const paste = document.getElementById("paste-url");
  const state = describeUrl(paste.value.trim() || url);
  verdict.textContent = state.text;
  verdict.className = `verdict ${state.kind === "ok" ? "ok" : "bad"}`;

  const button = document.getElementById("btn-clip");
  paste.addEventListener("input", () => {
    const next = describeUrl(paste.value.trim() || url);
    verdict.textContent = next.text;
    verdict.className = `verdict ${next.kind === "ok" ? "ok" : "bad"}`;
    button.disabled = !(next.kind === "ok" && button.dataset.serviceOk === "1");
  });

  button.dataset.serviceOk = serviceOk ? "1" : "0";
  button.disabled = !(state.kind === "ok" && serviceOk);
  button.addEventListener("click", () => onClip(base, paste.value.trim() || url));
}

document.addEventListener("DOMContentLoaded", main);
