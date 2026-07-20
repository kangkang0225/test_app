const ui = {
  connectionPill: document.querySelector('#connectionPill'),
  connectionButton: document.querySelector('#connectionButton'),
  attractionCount: document.querySelector('#attractionCount'),
  onlineCount: document.querySelector('#onlineCount'),
  nodeCount: document.querySelector('#nodeCount'),
  currentAttraction: document.querySelector('#currentAttraction'),
  journeyStatus: document.querySelector('#journeyStatus'),
  journeyCount: document.querySelector('#journeyCount'),
  journeyRequirement: document.querySelector('#journeyRequirement'),
  journeyBar: document.querySelector('#journeyBar'),
  journeyRewardState: document.querySelector('#journeyRewardState'),
  bindingSummary: document.querySelector('#bindingSummary'),
  bindingSource: document.querySelector('#bindingSource'),
  resetJourney: document.querySelector('#resetJourney'),
  attractionGrid: document.querySelector('#attractionGrid'),
  activityList: document.querySelector('#activityList'),
  nodeGrid: document.querySelector('#nodeGrid'),
  nodeSummary: document.querySelector('#nodeSummary'),
  heroEyebrow: document.querySelector('#heroEyebrow'),
  heroTitle: document.querySelector('#heroTitle'),
  heroDescription: document.querySelector('#heroDescription'),
  guideTitle: document.querySelector('#guideTitle'),
  guideImage: document.querySelector('#guideImage'),
  guideLarge: document.querySelector('#guideLarge'),
  guideButton: document.querySelector('#guideButton'),
  guideDialog: document.querySelector('#guideDialog'),
  closeGuide: document.querySelector('#closeGuide'),
  toast: document.querySelector('#toast')
};

let latestState = null;
let busy = false;
let toastTimer = null;

const tagLabels = {
  uhf_a: 'UHF-A 自动到访',
  uhf_b: 'UHF-B 抬腕',
  uhf_c: 'UHF-C 按键',
  hf: 'HF 贴卡'
};

const deviceLabels = {
  camera: '相机',
  light: '灯光',
  spray: '喷雾',
  fog: '雾幕',
  speaker: '音响',
  projection: '投影',
  screen: '屏幕'
};

const deviceIcons = {
  camera: '◉',
  light: '✦',
  spray: '≋',
  fog: '☁',
  speaker: '♪',
  projection: '▣',
  screen: '▤'
};

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function showToast(message, tone = 'success') {
  window.clearTimeout(toastTimer);
  ui.toast.textContent = message;
  ui.toast.className = `toast show ${tone}`;
  toastTimer = window.setTimeout(() => { ui.toast.className = 'toast'; }, 3200);
}

async function request(path, options = {}) {
  const headers = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json; charset=utf-8';
  const response = await fetch(path, {
    method: options.method || 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    if (payload.data) render(payload.data);
    throw new Error(payload.error || '操作失败');
  }
  return payload.data ?? payload;
}

function render(state) {
  latestState = state;
  const current = state.attractions.find((item) => item.inside);
  const config = state.ui || {};
  ui.heroEyebrow.textContent = config.eyebrow || 'DU FU COTTAGE · RFID EDGE LAB';
  if (config.title) {
    ui.heroTitle.innerHTML = `${escapeHtml(config.title)}<br><em>真实链路模拟</em>`;
    document.title = config.title;
  }
  ui.heroDescription.textContent = config.description || '';
  ui.guideTitle.textContent = config.guide_title || '景区导览图';
  if (config.guide_image_url) {
    ui.guideImage.src = config.guide_image_url;
    ui.guideLarge.src = config.guide_image_url;
    ui.guideButton.hidden = false;
  } else {
    ui.guideButton.hidden = true;
  }

  ui.attractionCount.textContent = state.attractions.length;
  ui.onlineCount.textContent = state.online_count;
  ui.nodeCount.textContent = ` / ${state.node_count}`;
  ui.currentAttraction.textContent = current ? current.name : '尚未入场';
  ui.journeyStatus.textContent = !state.connected ? '等待连接' : current ? '游览进行中' : '设备已就绪';
  ui.connectionPill.className = `status-pill ${state.connected ? 'online' : 'offline'}`;
  ui.connectionPill.querySelector('span').textContent = state.connected ? '全部设备在线' : '设备未连接';
  ui.connectionButton.textContent = state.connected ? '断开全部设备' : '连接全部设备';

  const journey = state.journey || {};
  ui.journeyCount.textContent = `${journey.completed_spots || 0} / ${journey.total_spots || 0}`;
  ui.journeyRequirement.textContent = `达到 ${journey.required_spots || 0} / ${journey.total_spots || 0} 可兑换`;
  ui.journeyBar.style.width = `${Math.min(100, Math.max(0, journey.progress_percent || 0))}%`;
  ui.journeyRewardState.textContent = journey.qualified ? '已达到兑换条件 · 可模拟领取礼品' : '尚未达到兑换条件';
  ui.journeyRewardState.className = journey.qualified ? 'qualified' : '';

  const bindings = state.bindings || [];
  ui.bindingSummary.textContent = bindings.length
    ? bindings.map((binding) => `${binding.tag_type || binding.tag.toUpperCase()} → ${deviceLabels[binding.device_type] || binding.device_type || '未配置'}`).join(' · ')
    : '当前没有有效绑定';
  ui.bindingSource.textContent = state.binding_source === 'app_backend'
    ? '已同步 App 后台实时配置'
    : `使用测试默认值${state.binding_error ? ` · ${state.binding_error}` : ''}`;

  renderAttractions(state.attractions, state.connected);
  renderLogs(state.logs);
  renderNodes(state.nodes);
}

function renderAttractions(attractions, connected) {
  ui.attractionGrid.innerHTML = attractions.map((item) => {
    const controls = item.devices.flatMap((device) =>
      (device.controls || []).map((control) => ({ ...control, device }))
    );
    const triggerButtons = (item.interaction_bindings || []).map((binding) => {
      const target = binding.configured
        ? (deviceLabels[binding.device_type] || binding.device_type)
        : '未配置';
      const action = binding.action === 'capture' ? '拍照' : binding.action === 'adjust' ? '调节' : binding.action === 'on' ? '开启' : binding.action || '—';
      const availability = binding.installed
        ? (binding.available ? `本站设备：${binding.device_name}` : '本站设备离线')
        : `本站未安装${target}`;
      const mode = binding.tag === 'uhf_b' ? '抬腕' : '按键';
      return `<button class="trigger-button tag-${binding.tag} ${binding.installed ? 'target-installed' : 'target-missing'}" type="button" data-action="tag" data-attraction="${escapeHtml(item.id)}" data-tag="${binding.tag}"><strong>${binding.tag_label} · ${mode}</strong><span>当前：${escapeHtml(target)} / ${escapeHtml(action)}</span><small>${escapeHtml(availability)}</small></button>`;
    }).join('');
    const hfButtons = [
      item.hf_checkin_available
        ? `<button class="trigger-button tag-hf ${item.checked_in ? 'checked' : ''}" type="button" data-action="tag" data-attraction="${escapeHtml(item.id)}" data-tag="hf_checkin"><strong>HF 景点打卡</strong><span>${item.checked_in ? '本轮已完成，再次贴卡不重复计数' : '计入 80% 礼品兑换进度'}</span></button>`
        : '',
      item.hf_control_available
        ? `<button class="trigger-button tag-hf-control ${item.hf_ready ? 'checked' : ''}" type="button" data-action="tag" data-attraction="${escapeHtml(item.id)}" data-tag="hf_control"><strong>HF 设备控制</strong><span>${item.hf_ready ? '已确权，可操作本站固定设备' : '贴卡获取近场控制权限'}</span></button>`
        : ''
    ].join('');
    const controlButtons = controls.map(({ device, ...control }) => {
      const params = control.params || {};
      const paramKey = Object.keys(params)[0];
      if (control.action === 'adjust' && paramKey) {
        const value = Number(params[paramKey]) || 0;
        return `<div class="control-adjust"><label><span>${deviceIcons[device.type] || '◆'} ${escapeHtml(device.name)}</span><output>${value}%</output></label><input type="range" min="0" max="100" value="${value}" data-control-input data-param="${escapeHtml(paramKey)}"><button class="control-button" type="button" data-action="control" data-attraction="${escapeHtml(item.id)}" data-device="${escapeHtml(device.device_id)}" data-control="${escapeHtml(control.id)}" ${item.hf_ready ? '' : 'disabled'}>${escapeHtml(paramKey === 'brightness' ? '设置亮度' : paramKey === 'volume' ? '设置音量' : control.label)}</button></div>`;
      }
      return `<button class="control-button" type="button" data-action="control" data-attraction="${escapeHtml(item.id)}" data-device="${escapeHtml(device.device_id)}" data-control="${escapeHtml(control.id)}" ${item.hf_ready ? '' : 'disabled'}><span>${deviceIcons[device.type] || '◆'}</span>${escapeHtml(control.label)}</button>`;
    }).join('');
    const deviceChips = item.devices.map((device) =>
      `<span class="device-chip ${device.status}"><i>${deviceIcons[device.type] || '◆'}</i>${escapeHtml(device.name)}<small>${device.status === 'online' ? '在线' : '离线'}</small></span>`
    ).join('');
    const tagChips = item.tags.map((tag) =>
      `<span class="tag-chip ${tag}">${escapeHtml(tagLabels[tag] || tag)}</span>`
    ).join('');
    const insidePanel = item.inside ? `
      <div class="interaction-lab">
        <div class="lab-heading"><span>腕带触发</span><small>UHF-A 已自动完成；B/C 读取 App 当前绑定</small></div>
        <div class="trigger-grid">${triggerButtons}</div>
        <div class="hf-zone"><div class="lab-heading"><span>HF 近场操作</span><small>打卡和设备控制相互独立</small></div><div class="trigger-grid">${hfButtons}</div></div>
        ${controls.length ? `<div class="environment-controls"><div class="lab-heading"><span>本站固定设备控制</span><small>${item.hf_ready ? '已确权，可以下发' : '请先点击 HF 设备控制'}</small></div><div class="control-grid">${controlButtons}</div></div>` : ''}
      </div>` : '';
    const photo = item.capture
      ? `<img src="${escapeHtml(item.capture.image_url)}" alt="${escapeHtml(item.name)}模拟拍摄结果"><div class="capture-caption"><strong>模拟拍摄成功</strong><span>命令 #${escapeHtml(item.capture.command_id)} · ${escapeHtml(item.capture.file_name)}</span></div>`
      : item.camera_installed
        ? '<div class="photo-placeholder camera-armed"><b>◉</b><span>相机已埋伏</span><small>等待 UHF-B 或 UHF-C 当前绑定相机后触发</small></div>'
        : '<div class="photo-placeholder no-camera"><b>—</b><span>本站未安装相机</span><small>App 绑定相机也不会产生照片</small></div>';
    return `
      <article class="route-card ${item.inside ? 'active' : ''} ${item.ready ? '' : 'not-ready'}" style="--accent:${escapeHtml(item.accent)}">
        <div class="route-photo">
          ${photo}
          <span class="route-number">${String(item.order).padStart(2, '0')}</span>
          ${item.inside ? '<span class="inside-ribbon">当前所在</span>' : ''}
        </div>
        <div class="route-body">
          <p class="route-district">${escapeHtml(item.district)}</p>
          <div class="route-title"><h3>${escapeHtml(item.name)}</h3><span>${item.dwell_seconds}s 模拟停留</span></div>
          <p class="route-description">${escapeHtml(item.description)}</p>
          <div class="tag-list">${tagChips}</div>
          <div class="inventory-title"><span>本站固定设备</span><small>${item.devices.length ? `${item.devices.length} 台` : '无环境设备'}</small></div>
          ${item.devices.length ? `<div class="device-list">${deviceChips}</div>` : '<div class="device-empty">只有 Reader，不会因 App 绑定自动增加设备</div>'}
          ${insidePanel}
          <button class="visit-button ${item.inside ? 'leave' : ''}" type="button" data-action="toggle" data-attraction="${escapeHtml(item.id)}" ${(!connected || !item.ready) ? 'disabled' : ''}>
            ${item.inside ? '离开此景点' : '进入景点 · UHF-A 自动打卡'}
          </button>
        </div>
      </article>`;
  }).join('');
}

function renderLogs(logs) {
  if (!logs.length) {
    ui.activityList.innerHTML = '<li class="empty-log">连接设备并进入第一站后，这里会显示完整链路。</li>';
    return;
  }
  ui.activityList.innerHTML = logs.slice(0, 36).map((entry) => `
    <li class="log-${escapeHtml(entry.level.toLowerCase())}">
      <time>${escapeHtml(entry.timestamp)}</time>
      <div><b>${escapeHtml(entry.source)}</b><p>${escapeHtml(entry.message)}</p></div>
    </li>`).join('');
}

function renderNodes(nodes) {
  ui.nodeSummary.textContent = `${nodes.length} 个节点`;
  ui.nodeGrid.innerHTML = nodes.map((node) => `
    <article class="node-card ${escapeHtml(node.status)}">
      <i></i><div><strong>${escapeHtml(node.device_id)}</strong><span>${node.role === 'reader' ? 'Reader' : deviceLabels[node.type.toLowerCase()] || node.type} · ${escapeHtml(node.attraction)}</span></div>
      <small>${node.status === 'online' ? 'ONLINE' : 'OFFLINE'}</small>
    </article>`).join('');
}

async function runAction(button) {
  if (busy) return;
  busy = true;
  document.body.classList.add('is-busy');
  try {
    const attraction = encodeURIComponent(button.dataset.attraction);
    let path;
    let body;
    if (button.dataset.action === 'toggle') {
      path = `/api/attractions/${attraction}/toggle`;
    } else if (button.dataset.action === 'tag') {
      path = `/api/attractions/${attraction}/tags/${encodeURIComponent(button.dataset.tag)}`;
    } else {
      path = `/api/attractions/${attraction}/controls/${encodeURIComponent(button.dataset.device)}/${encodeURIComponent(button.dataset.control)}`;
      const adjust = button.closest('.control-adjust');
      const input = adjust?.querySelector('[data-control-input]');
      if (input) body = { params: { [input.dataset.param]: Number(input.value) } };
    }
    const result = await request(path, { method: 'POST', body });
    if (result.state) render(result.state);
    showToast(result.message || '操作已完成');
  } catch (error) {
    showToast(error.message || '操作失败', 'error');
    await refresh();
  } finally {
    busy = false;
    document.body.classList.remove('is-busy');
  }
}

async function toggleConnection() {
  if (busy) return;
  busy = true;
  document.body.classList.add('is-busy');
  try {
    const state = await request(latestState?.connected ? '/api/disconnect' : '/api/connect', { method: 'POST' });
    render(state);
    showToast(state.connected ? '全部模拟节点已连接' : '全部模拟节点已断开');
  } catch (error) {
    showToast(error.message || '连接失败', 'error');
  } finally {
    busy = false;
    document.body.classList.remove('is-busy');
  }
}

async function refresh() {
  try {
    const state = await request('/api/state');
    render(state);
  } catch (error) {
    showToast(error.message || '无法读取模拟器状态', 'error');
  }
}

ui.connectionButton.addEventListener('click', toggleConnection);
ui.attractionGrid.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action]');
  if (button && !button.disabled) runAction(button);
});
ui.attractionGrid.addEventListener('input', (event) => {
  const input = event.target.closest('[data-control-input]');
  if (input) {
    const output = input.closest('.control-adjust')?.querySelector('output');
    if (output) output.textContent = `${input.value}%`;
  }
});
ui.resetJourney.addEventListener('click', async () => {
  if (busy) return;
  try {
    const state = await request('/api/journey/reset', { method: 'POST' });
    render(state);
    showToast('本轮 HF 打卡进度已重置');
  } catch (error) {
    showToast(error.message || '重置失败', 'error');
  }
});
ui.guideButton.addEventListener('click', () => ui.guideDialog.showModal());
ui.closeGuide.addEventListener('click', () => ui.guideDialog.close());
ui.guideDialog.addEventListener('click', (event) => {
  if (event.target === ui.guideDialog) ui.guideDialog.close();
});

refresh();
window.setInterval(refresh, 1500);
