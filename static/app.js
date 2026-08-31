"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SENT = { "正面": "pos", "负面": "neg", "中性": "neu" };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showLogin(); throw new Error("未登录"); }
  return res.json();
}

// ---------- 认证 ----------
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}
function showApp(username) {
  $("#login-view").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-name").textContent = username;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", body: { username: $("#login-username").value, password: $("#login-password").value } });
    showApp(r.username);
    refreshAll();
  } catch (err) {
    $("#login-error").textContent = "用户名或密码错误";
  }
});
$("#logout-btn").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); showLogin(); });

// ---------- 导航 ----------
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
function switchView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "view-" + name));
  if (name === "stats") loadStats();
  if (name === "keywords") loadKeywords();
  if (name === "results") loadResults();
  if (name === "pushlog") loadPushLog();
  if (name === "settings") loadSettings();
}

// ---------- 概览 ----------
async function loadStats() {
  const s = await api("/api/stats");
  $("#stat-total").textContent = s.total;
  $("#stat-negative").textContent = s.negative;
  const kws = await api("/api/keywords");
  $("#stat-keywords").textContent = kws.length;

  const total = s.total || 1;
  const colors = { "正面": "var(--pos)", "负面": "var(--neg)", "中性": "var(--neu)" };
  $("#sentiment-bars").innerHTML = ["正面", "负面", "中性"].map((k) => {
    const n = s.by_sentiment[k] || 0;
    const pct = Math.round((n / total) * 100);
    return `<div class="bar-row"><span class="bar-label">${k}</span><div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${colors[k]}"></div></div><span class="bar-val">${n}</span></div>`;
  }).join("");

  const plat = Object.entries(s.by_platform).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const maxP = plat[0]?.[1] || 1;
  $("#platform-bars").innerHTML = plat.map(([k, n]) =>
    `<div class="bar-row"><span class="bar-label">${esc(k)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.round((n / maxP) * 100)}%;background:var(--accent)"></div></div><span class="bar-val">${n}</span></div>`
  ).join("") || '<div class="empty">暂无数据</div>';

  $("#recent-list").innerHTML = s.recent.map(resultRow).join("") || '<div class="empty">暂无数据</div>';
}

const LEVELS = ["一般", "较大", "重大", "特别重大"];
const LEVEL_TAG = { "一般": "lv1", "较大": "lv2", "重大": "lv3", "特别重大": "lv4" };

function resultRow(r) {
  const tag = SENT[r.sentiment] || "neu";
  const pushed = r.pushed ? '<span class="tag tag-pos">已推送</span>' : '<span class="tag tag-off">未推送</span>';
  const lv = r.level || "一般";
  const levelSel = `<select class="level-select" onchange="setLevel(${r.id}, this.value)">
    ${LEVELS.map((l) => `<option value="${l}" ${l === lv ? "selected" : ""}>${l}</option>`).join("")}
  </select>`;
  return `<tr>
    <td>${esc(r.found_at)}</td>
    <td>${esc(r.keyword || "")}</td>
    <td><span class="tag tag-${tag}">${esc(r.sentiment)}</span></td>
    <td>${esc(r.source_platform)}</td>
    <td><span class="tag tag-${LEVEL_TAG[lv] || "lv1"}">${esc(lv)}</span>${levelSel}</td>
    <td>${pushed}</td>
    <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.url)}</a>${r.snippet ? `<div class="snippet">${esc(r.snippet)}</div>` : ""}</td>
  </tr>`;
}

async function setLevel(id, level) {
  await api(`/api/results/${id}/level`, { method: "POST", body: { level } });
  loadResults();
}

// ---------- 关键词 ----------
async function loadKeywords() {
  const list = await api("/api/keywords");
  const tb = $("#kw-table tbody");
  tb.innerHTML = list.map((k) => `<tr>
    <td>${esc(k.keyword)}</td>
    <td><span class="tag ${k.enabled ? "tag-on" : "tag-off"}">${k.enabled ? "启用" : "停用"}</span></td>
    <td>${esc(k.platforms || "全部")}</td>
    <td>${esc(k.created_at)}</td>
    <td>
      <button class="secondary" onclick="toggleKw(${k.id})">${k.enabled ? "停用" : "启用"}</button>
      <button class="secondary" onclick="delKw(${k.id})">删除</button>
    </td>
  </tr>`).join("") || '<tr><td colspan="5" class="empty">暂无关键词，先添加一个</td></tr>';
}
async function toggleKw(id) { await api(`/api/keywords/${id}/toggle`, { method: "POST" }); loadKeywords(); }
async function delKw(id) {
  if (!confirm("删除该关键词及其全部监测结果？")) return;
  await api(`/api/keywords/${id}`, { method: "DELETE" });
  loadKeywords();
}
$("#kw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#kw-input").value.trim();
  if (!v) return;
  await api("/api/keywords", { method: "POST", body: { keyword: v, platforms: [] } });
  $("#kw-input").value = "";
  loadKeywords();
});

// ---------- 监测结果 ----------
let resultPage = 1;
async function loadKeywordsSelect() {
  const list = await api("/api/keywords");
  const sel = $("#f-keyword");
  sel.innerHTML = '<option value="">全部关键词</option>' + list.map((k) => `<option value="${k.id}">${esc(k.keyword)}</option>`).join("");
}
async function loadPlatforms() {
  const list = await api("/api/results?size=1");
  const platforms = new Set();
  // 平台来自统计数据更全面
  const s = await api("/api/stats");
  Object.keys(s.by_platform).forEach((p) => platforms.add(p));
  $("#f-platform").innerHTML = '<option value="">全部平台</option>' + [...platforms].map((p) => `<option>${esc(p)}</option>`).join("");
}
async function loadResults() {
  await loadKeywordsSelect();
  await loadPlatforms();
  // 过滤空值，避免把空字符串传给后端 int 参数导致 422
  const params = new URLSearchParams();
  const setIf = (k, v) => { if (v !== "" && v !== null && v !== undefined) params.set(k, v); };
  setIf("keyword_id", $("#f-keyword").value);
  setIf("sentiment", $("#f-sentiment").value);
  setIf("platform", $("#f-platform").value);
  params.set("pushed", $("#f-pushed").value || "-1");
  setIf("q", $("#f-q").value);
  params.set("page", resultPage);
  params.set("size", 20);
  const r = await api("/api/results?" + params);
  $("#res-table tbody").innerHTML = r.items.map(resultRow).join("") || '<tr><td colspan="7" class="empty">暂无结果</td></tr>';
  $("#page-info").textContent = `第 ${r.page} 页 / 共 ${Math.ceil(r.total / r.size)} 页（${r.total} 条）`;
  $("#prev-page").disabled = r.page <= 1;
  $("#next-page").disabled = r.page * r.size >= r.total;
}
$("#f-apply").addEventListener("click", () => { resultPage = 1; loadResults(); });
$("#prev-page").addEventListener("click", () => { resultPage--; loadResults(); });
$("#next-page").addEventListener("click", () => { resultPage++; loadResults(); });

// 首页负面数字点击 → 跳转监测结果并筛出负面
$("#card-negative").addEventListener("click", () => {
  $("#f-sentiment").value = "负面";
  $("#f-pushed").value = "-1";
  resultPage = 1;
  switchView("results");
});

// ---------- 推送记录 ----------
function pushContentCell(l) {
  const list = l.results || [];
  if (!list.length) return esc(l.message || "（无内容）");
  return list.map((x) => {
    const tag = SENT[x.sentiment] || "neu";
    const emoji = { "正面": "🟢", "负面": "🔴", "中性": "⚪" }[x.sentiment] || "⚪";
    return `<div class="push-item">
      ${emoji} <span class="tag tag-${tag}">${esc(x.sentiment)}</span> <span class="plat">${esc(x.source_platform)}</span>
      <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.url)}</a>
    </div>`;
  }).join("");
}
async function loadPushLog() {
  const r = await api("/api/push-log");
  $("#log-table tbody").innerHTML = r.items.map((l) => `<tr>
    <td>${esc(l.created_at)}</td>
    <td>${esc(l.keyword || "")}</td>
    <td>${esc(l.channel)}</td>
    <td><span class="tag ${l.status === "ok" ? "tag-pos" : "tag-neg"}">${l.status === "ok" ? "成功" : "失败"}</span></td>
    <td>${pushContentCell(l)}</td>
  </tr>`).join("") || '<tr><td colspan="5" class="empty">暂无推送记录</td></tr>';
}

// ---------- 设置 ----------
async function loadBotStatus() {
  try {
    const c = await api("/api/wecom-chats");
    if (!c.bot_configured) {
      $("#bot-status").textContent = "连接状态：未配置 Bot ID/Secret";
      return;
    }
    const conn = c.connected ? "已连接 ✓" : "连接中/未连接";
    const chats = c.chats.length ? `已记录 ${c.chats.length} 个会话` : "还没收到会话（请在企业微信里给机器人发一句话）";
    $("#bot-status").textContent = `连接状态：${conn}；${chats}`;
  } catch (e) {
    $("#bot-status").textContent = "连接状态：查询失败";
  }
}
const PUSH_FIELD_OPTIONS = [
  ["title", "标题"], ["snippet", "摘要"], ["url", "链接"],
  ["platform", "平台"], ["sentiment", "倾向"], ["level", "级别"],
];
async function loadSettings() {
  const s = await api("/api/settings");
  $("#s-wecom").value = s.wecom_key || "";
  $("#s-bot-id").value = s.wecom_bot_id || "";
  $("#s-bot-secret").value = s.wecom_bot_secret || "";
  $("#s-deepseek").value = s.deepseek_key || "";
  $("#s-model").value = s.deepseek_model || "deepseek-chat";
  $("#s-interval").value = s.interval || 30;
  // 推送配置
  $("#s-push-mode").value = s.push_mode || "realtime";
  $("#s-push-time").value = s.push_time || "09:00";
  $("#s-push-wstart").value = s.push_window_start || "";
  $("#s-push-wend").value = s.push_window_end || "";
  $("#s-push-batch").value = s.push_batch_size || 5;
  $("#s-push-minlevel").value = s.push_min_level || "";
  $("#s-scan-wstart").value = s.scan_window_start || "";
  $("#s-scan-wend").value = s.scan_window_end || "";
  const pf = new Set(s.push_fields);
  $("#s-push-fields").innerHTML = PUSH_FIELD_OPTIONS.map(([k, name]) =>
    `<label><input type="checkbox" value="${k}" ${pf.has(k) ? "checked" : ""} /> ${name}</label>`
  ).join("");
  const sources = await api("/api/sources");
  const enabled = new Set(s.sources);
  $("#s-sources").innerHTML = Object.entries(sources).map(([k, name]) =>
    `<label><input type="checkbox" value="${k}" ${enabled.has(k) ? "checked" : ""} /> ${esc(name)}</label>`
  ).join("");
  // 来源平台（内置 + 自动发现）
  const plats = await api("/api/platforms");
  $("#s-platforms").innerHTML = plats.platforms.map((p) => `<span class="tag tag-off">${esc(p)}</span>`).join("") || '<span class="empty">暂无</span>';
  loadBotStatus();
}
$("#save-settings").addEventListener("click", async () => {
  const sources = [...$$("#s-sources input:checked")].map((i) => i.value);
  const push_fields = [...$$("#s-push-fields input:checked")].map((i) => i.value);
  await api("/api/settings", { method: "POST", body: {
    wecom_key: $("#s-wecom").value.trim(),
    wecom_bot_id: $("#s-bot-id").value.trim(),
    wecom_bot_secret: $("#s-bot-secret").value.trim(),
    deepseek_key: $("#s-deepseek").value.trim(),
    deepseek_model: $("#s-model").value.trim(),
    interval: parseInt($("#s-interval").value) || 30,
    sources,
    push_mode: $("#s-push-mode").value,
    push_time: $("#s-push-time").value.trim(),
    push_window_start: $("#s-push-wstart").value.trim(),
    push_window_end: $("#s-push-wend").value.trim(),
    push_fields,
    push_batch_size: parseInt($("#s-push-batch").value) || 5,
    push_min_level: $("#s-push-minlevel").value,
    scan_window_start: $("#s-scan-wstart").value.trim(),
    scan_window_end: $("#s-scan-wend").value.trim(),
  }});
  $("#settings-msg").textContent = "已保存 ✓";
  setTimeout(() => ($("#settings-msg").textContent = ""), 2000);
  loadBotStatus();
});
$("#test-push").addEventListener("click", async () => {
  $("#settings-msg").textContent = "发送中…";
  const r = await api("/api/test-push", { method: "POST" });
  $("#settings-msg").textContent = r.ok ? "推送成功 ✓" : "推送失败: " + (r.error || r.detail);
});
$("#run-now").addEventListener("click", async () => {
  $("#settings-msg").textContent = "监控执行中…（约需数十秒）";
  const r = await api("/api/run", { method: "POST" });
  $("#settings-msg").textContent = r.ok ? `完成：新增 ${r.summary.found} 条，推送 ${r.summary.pushed} 条` : "执行失败: " + (r.error || r.detail);
});
$("#digest-now").addEventListener("click", async () => {
  $("#settings-msg").textContent = "定时汇总推送执行中…";
  const r = await api("/api/digest", { method: "POST" });
  $("#settings-msg").textContent = r.ok ? `完成：汇总推送 ${r.pushed} 条` : "执行失败: " + (r.error || r.detail);
});

function refreshAll() {
  switchView("stats");
}

// 初始化
(async () => {
  try {
    const me = await api("/api/me");
    showApp(me.username);
    refreshAll();
  } catch (e) {
    showLogin();
  }
})();
