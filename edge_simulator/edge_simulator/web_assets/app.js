const state = { data: null, busy: false, toastTimer: null };

const $ = (id) => document.getElementById(id);

const tagLabels = {
  uhf_a: "UHF-A · 到访",
  uhf_b: "UHF-B · 按键",
  uhf_c: "UHF-C · 手势",
  hf: "HF · 近场"
};

const deviceLabels = {
  camera: "相机抓拍",
  light: "环境灯光",
  audio: "声音装置"
};

async function request(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function render(data) {
  state.data = data;
  const connected = data.connected;
  const pill = $("connectionPill");
  pill.className = `status-pill ${connected ? "online" : "offline"}`;
  pill.querySelector("span").textContent = connected ? "全部设备在线" : "设备未完全连接";
  $("connectionButton").textContent = connected ? "断开全部设备" : "连接全部设备";
  $("connectionButton").disabled = state.busy;

  $("attractionCount").textContent = data.attractions.length;
  $("onlineCount").textContent = data.online_count;
  $("nodeCount").textContent = ` / ${data.node_count}`;
  $("nodeSummary").textContent = `${data.online_count} / ${data.node_count} 在线`;

  const active = data.attractions.find((item) => item.inside);
  $("currentAttraction").textContent = active ? active.name : "尚未进入景点";
  $("currentSince").textContent = active
    ? `已模拟进入 · 再次点击该景点即可离开`
    : connected ? "请选择下方任一成都景点" : "请先连接全部模拟设备";
  $("journeyStatus").textContent = active ? "景点内" : connected ? "漫游中" : "等待连接";

  $("attractionGrid").innerHTML = data.attractions.map((item, index) => {
    const tags = item.tags.map((tag) => `<span>${escapeHtml(tagLabels[tag] || tag)}</span>`).join("");
    const devices = item.devices.length
      ? item.devices.map((device) => `<span>${escapeHtml(deviceLabels[device.type] || device.name)}</span>`).join("")
      : `<span>仅位置打卡</span>`;
    const disabled = state.busy || !connected || !item.ready;
    return `
      <button class="attraction-card ${item.inside ? "inside" : ""}" type="button"
        data-attraction-id="${escapeHtml(item.id)}" aria-pressed="${item.inside}"
        ${disabled ? "disabled" : ""} style="--accent:${escapeHtml(item.accent)}">
        <span class="card-topline"><b>${String(index + 1).padStart(2, "0")}</b><i>${item.inside ? "正在景点内" : item.ready ? "设备就绪" : "节点离线"}</i></span>
        <span class="district">${escapeHtml(item.district)}</span>
        <strong>${escapeHtml(item.name)}</strong>
        <span class="description">${escapeHtml(item.description)}</span>
        <span class="chip-label">腕带感知</span><span class="chips">${tags}</span>
        <span class="chip-label">边缘设备</span><span class="chips devices">${devices}</span>
        <span class="card-action"><b>${item.inside ? "离开景点" : "进入景点"}</b><i>${item.inside ? "再次点击" : `加速停留 ${item.dwell_seconds} 秒`}</i></span>
      </button>`;
  }).join("");

  document.querySelectorAll(".attraction-card").forEach((button) => {
    button.addEventListener("click", () => toggleAttraction(button.dataset.attractionId));
  });

  const logs = data.logs.slice(0, 24);
  $("activityList").innerHTML = logs.length ? logs.map((entry) => `
    <li class="log-${escapeHtml(entry.level.toLowerCase())}">
      <time>${escapeHtml(entry.timestamp)}</time>
      <span><b>${escapeHtml(entry.source)}</b>${escapeHtml(entry.message)}</span>
    </li>`).join("") : `<li class="empty-log">尚无交互记录</li>`;

  $("nodeGrid").innerHTML = data.nodes.map((node) => `
    <article class="node ${node.status}">
      <i></i><span><b>${escapeHtml(node.device_id)}</b><small>${escapeHtml(node.attraction)} · ${escapeHtml(node.type)}</small></span>
      <em>${node.status === "online" ? "在线" : "离线"}</em>
    </article>`).join("");
}

async function refresh(silent = true) {
  try {
    const payload = await request("/api/state");
    render(payload.data);
  } catch (error) {
    if (!silent) showToast(error.message, true);
  }
}

async function withBusy(task) {
  if (state.busy) return;
  state.busy = true;
  if (state.data) render(state.data);
  try {
    await task();
  } finally {
    state.busy = false;
    await refresh(true);
  }
}

async function toggleAttraction(id) {
  await withBusy(async () => {
    try {
      const payload = await request(`/api/attractions/${encodeURIComponent(id)}/toggle`, { method: "POST" });
      showToast(payload.message, false);
      render(payload.state);
      if (payload.warnings?.length) showToast(payload.warnings.join("；"), true);
    } catch (error) {
      showToast(error.message, true);
    }
  });
}

async function toggleConnection() {
  await withBusy(async () => {
    try {
      const path = state.data?.connected ? "/api/disconnect" : "/api/connect";
      const payload = await request(path, { method: "POST" });
      render(payload.data);
      showToast(path.endsWith("disconnect") ? "全部模拟设备已断开" : "全部模拟设备连接成功", false);
    } catch (error) {
      showToast(error.message, true);
    }
  });
}

function showToast(message, isError) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast visible ${isError ? "error" : "success"}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { toast.className = "toast"; }, 4200);
}

$("connectionButton").addEventListener("click", toggleConnection);
refresh(false);
setInterval(() => { if (!state.busy) refresh(true); }, 2000);
