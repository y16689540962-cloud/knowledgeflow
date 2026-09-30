/* KnowledgeFlow 最小可用界面 —— 零构建原生 JS。
 *
 * 三条纪律：
 * 1. **所有外部文本一律走 textContent**，不拼 innerHTML。
 *    「谁贴进来什么你就渲染什么」是这类应用最容易出的 XSS，
 *    而且数据还经过 LLM —— 更难预测内容里有什么。
 * 2. 失败要**看得见**：HTTP 状态码 + error_type + failed_step 全显示，
 *    不把 4xx/5xx 折成一句「出错了」。
 * 3. 不引入任何框架 / 构建步骤（定稿第二十八节：不开发 React UI）。
 */

const api = {
  async request(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(httpMessage(res.status, data));
      err.status = res.status;
      err.body = data;
      throw err;
    }
    return data;
  },
  health: () => api.request("GET", "/api/health"),
  list: (status) =>
    api.request("GET", "/api/contents?limit=50" + (status ? `&status=${status}` : "")),
  detail: (id) => api.request("GET", `/api/contents/${encodeURIComponent(id)}`),
  reprocess: (id) => api.request("POST", `/api/contents/${encodeURIComponent(id)}/reprocess`),
  ingest: (payload) => api.request("POST", "/api/ingest/manual", payload),
  ingestDouyin: (payload) => api.request("POST", "/api/ingest/douyin", payload),
};

/* ---------- 两种输入模式 ----------
 * 「抖音链接」这条是**这个插件的主入口** —— 之前只能 curl，用户拿不到。
 * 服务端要做的两件事它都不管：解析短链（`resolve_short_link`）与
 * 从详情接口取元数据（需要 DOUYIN_COOKIE）；UI 只负责把链接递过去、
 * 把失败原因如实显示出来（不绕验证码，429/验证页照样如实报）。
 */
const MODES = {
  manual: {
    label: "处理并写入 Obsidian",
    pane: "pane-manual",
    button: "mode-manual",
    sample: () => {
      document.getElementById("f-title").value = "AI 算力成本观察";
      document.getElementById("f-author").value = "王小明";
      document.getElementById("f-text").value =
        "王小明在视频里说，中国的人工智能产业在 2026 年会继续保持增长，他判断算力成本会下降三成以上。这个判断基于他过去两年的观察。";
    },
  },
  douyin: {
    label: "采集并写入 Obsidian",
    pane: "pane-douyin",
    button: "mode-douyin",
    sample: () => {
      // 只填一条**真实存在**的公开分享链接，方便一眼看出格式；
      // 值本身不影响任何逻辑。
      document.getElementById("f-douyin").value = "https://v.douyin.com/L4FJNR3/";
    },
  },
};

let mode = "manual";

function setMode(next) {
  mode = MODES[next] ? next : "manual";
  for (const [name, spec] of Object.entries(MODES)) {
    const active = name === mode;
    document.getElementById(spec.pane).classList.toggle("hidden", !active);
    const button = document.getElementById(spec.button);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
  const submit = document.getElementById("btn-submit");
  submit.textContent = MODES[mode].label;
  // 切模式时清掉上一次的结果：留着会让人以为刚提交的已经出结果了。
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.add("hidden");
}

function httpMessage(status, data) {
  if (data.error_type) return `${status} ${data.error_type}`;
  if (Array.isArray(data.detail)) {
    const first = data.detail[0];
    return `${status} 字段 ${(first.loc || []).slice(1).join(".")} ${first.msg || ""}`.trim();
  }
  return `HTTP ${status}`;
}

/* ---------- DOM 工具（一律 textContent） ---------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function kvList(pairs) {
  const dl = el("dl", "kv");
  for (const [key, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(el("dt", null, key), el("dd", null, value));
  }
  return dl;
}

/* ---------- 健康状态 ---------- */
async function loadHealth() {
  const box = document.getElementById("health");
  box.textContent = "";
  try {
    const h = await api.health();
    const chips = [
      [`API ${h.version}`, ""],
      [`LLM ${h.llm_provider} / ${h.llm_model}`, ""],
      [h.vault_configured ? "vault 已配置" : "vault 未配置", h.vault_configured ? "ok" : "bad"],
    ];
    if (h.recovery) {
      const r = h.recovery;
      chips.push([
        `启动恢复：重置 ${r.reset.length} / 续跑 ${r.resumed.length} / 失败 ${r.resume_failures.length}`,
        r.resume_failures.length ? "bad" : "",
      ]);
    }
    if (h.recovery_error) chips.push([`恢复流程报错：${h.recovery_error}`, "bad"]);
    for (const [text, cls] of chips) box.append(el("span", `chip ${cls}`.trim(), text));
  } catch (err) {
    box.append(el("span", "chip bad", `健康检查失败：${err.message}`));
  }
}

/* ---------- 处理结果 ---------- */
function renderSteps(steps) {
  const ul = el("ul", "steps");
  for (const s of steps) {
    const li = el("li", s.status === "failed" ? "fail" : "");
    li.append(el("span", "name", s.step), el("span", "ms", `${s.elapsed_ms.toFixed(1)} ms`));
    if (s.detail) li.lastChild.textContent += ` · ${s.detail}`;
    ul.append(li);
  }
  return ul;
}

function renderResult(result) {
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.remove("hidden");

  box.append(el("span", `badge ${result.outcome}`, result.outcome));

  box.append(
    kvList([
      ["content_id", result.content_id],
      ["analysis_id", result.analysis_id],
      ["note_path", result.note_path],
      ["duplicate_of", result.duplicate_of],
      ["deduplicated_by", result.deduplicated_by],
      ["model", result.model],
      ["prompt_version", result.prompt_version],
      ["claims", `${result.claim_count}（需验证 ${result.unverified_count}）`],
      ["entities / topics", `${result.entity_count} / ${result.topic_count}`],
      ["needs_manual_review", result.needs_manual_review ? "是" : "否"],
    ])
  );

  if (result.error_type) {
    box.append(
      el(
        "div",
        "error-box",
        `${result.error_type} · 失败于 ${result.failed_step} · ${result.error_message || ""}`
      )
    );
  }

  if (result.steps && result.steps.length) {
    box.append(el("div", "steps-title", "步骤"), renderSteps(result.steps));
  }
  refreshList();
}

function showSubmitError(err) {
  // Pipeline 处理失败时，服务端仍然返回完整的 ProcessingResult —— 别浪费它。
  if (err.body && err.body.result) {
    renderResult(err.body.result);
    return;
  }
  const box = document.getElementById("result");
  box.textContent = "";
  box.classList.remove("hidden");
  box.append(el("span", "badge failed", `HTTP ${err.status}`));
  box.append(el("div", "error-box", `${err.message} ${JSON.stringify(err.body)}`));
}

/* ---------- 处理表单 ---------- */
function renderRaw(raw) {
  const box = document.getElementById("result");
  box.append(el("div", "steps-title", "采集到的内容"));
  box.append(
    kvList([
      ["source", raw.source],
      ["source_id", raw.source_id],
      ["source_url", raw.source_url],
      ["title", raw.title],
      ["author", raw.author || "（无）"],
      ["media_type", raw.media_type],
      ["raw_text", `${raw.raw_text_chars} 字`],
    ])
  );
}

async function onSubmit(event) {
  event.preventDefault();
  const button = document.getElementById("btn-submit");
  const label = MODES[mode].label;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    if (mode === "douyin") {
      const url = document.getElementById("f-douyin").value.trim();
      if (!url) throw Object.assign(new Error("请先粘贴抖音分享链接"), { status: 0, body: {} });
      // process 为 true：采集完直接走完整 A 线并写进 Obsidian。
      const data = await api.ingestDouyin({ url, process: true });
      // 顺序要紧：renderResult 会先清空面板，所以「采集到的内容」放在它之后追加。
      if (data.result) renderResult(data.result);
      else {
        const box = document.getElementById("result");
        box.textContent = "";
        box.classList.remove("hidden");
      }
      renderRaw(data.raw);
      return;
    }
    const payload = {
      title: document.getElementById("f-title").value,
      author: document.getElementById("f-author").value,
      source_url: document.getElementById("f-url").value,
      raw_text: document.getElementById("f-text").value,
      media_type: "text",
    };
    const data = await api.ingest(payload);
    renderResult(data.result);
  } catch (err) {
    showSubmitError(err);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function fillSample() {
  MODES[mode].sample();
}

/* ---------- 内容库 ---------- */
async function refreshList() {
  const box = document.getElementById("list");
  box.textContent = "";
  box.append(el("div", "empty", "载入中…"));
  const status = document.getElementById("f-status").value;
  let data;
  try {
    data = await api.list(status);
  } catch (err) {
    box.textContent = "";
    box.append(el("div", "error-box", `列表加载失败：${err.message}`));
    return;
  }
  box.textContent = "";
  if (!data.items.length) {
    box.append(el("div", "empty", "还没有内容 —— 在上面处理一条试试。"));
    return;
  }
  for (const item of data.items) {
    const row = el("div", "row");
    row.append(
      el("span", "title", item.title || `（无标题）${item.id.slice(0, 8)}`),
      el("span", "meta", `${item.status} · ${item.media_type || "-"} · ${item.created_at}`)
    );
    const view = el("button", null, "详情");
    view.addEventListener("click", () => showDetail(item.id));
    const redo = el("button", null, "重跑");
    redo.addEventListener("click", () => onReprocess(item.id, redo));
    row.append(view, redo);
    box.append(row);
  }
}

async function onReprocess(id, button) {
  button.disabled = true;
  try {
    const result = await api.reprocess(id);
    renderResult(result);
    document.getElementById("detail").scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    if (err.body && err.body.result) renderResult(err.body.result);
    else showSubmitError(err);
    button.disabled = false;
  }
}

function beginDetail(id) {
  const box = document.getElementById("detail");
  box.textContent = "";
  box.classList.remove("hidden");
  box.append(el("h3", null, "详情"), el("div", "empty", `载入中… ${id.slice(0, 8)}`));
  box.scrollIntoView({ behavior: "smooth" });
  return box;
}

async function showDetail(id) {
  // 先给加载态：慢的时候什么都不显示，用户会以为点了没反应。
  const box = beginDetail(id);
  let d;
  try {
    d = await api.detail(id);
  } catch (err) {
    box.append(el("div", "error-box", `详情加载失败：${err.message}`));
    return;
  }
  box.textContent = "";
  box.append(el("h3", null, "详情"));
  box.append(
    kvList([
      ["id", d.id],
      ["source / source_id", `${d.source} / ${d.source_id}`],
      ["status", d.status],
      ["content_hash", `${d.content_hash.slice(0, 16)}…（v${d.content_hash_version}）`],
      ["analyses", `${d.analysis_count} 个版本`],
      ["analysis_type", d.current_analysis ? d.current_analysis.analysis_type : null],
    ])
  );
  if (d.current_analysis && d.current_analysis.summary) {
    box.append(el("h3", null, "摘要"));
    box.append(el("pre", null, d.current_analysis.summary));
  }
  if (d.raw_text) {
    box.append(el("h3", null, "原文"));
    box.append(el("pre", null, d.raw_text));
  }
  box.scrollIntoView({ behavior: "smooth" });
}

/* ---------- 启动 ---------- */
document.getElementById("ingest-form").addEventListener("submit", onSubmit);
document.getElementById("btn-sample").addEventListener("click", fillSample);
document.getElementById("btn-refresh").addEventListener("click", refreshList);
document.getElementById("f-status").addEventListener("change", refreshList);
document.getElementById("mode-manual").addEventListener("click", () => setMode("manual"));
document.getElementById("mode-douyin").addEventListener("click", () => setMode("douyin"));
setMode("manual");
loadHealth();
refreshList();
