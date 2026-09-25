/* ============================================================================
   v5-VE1-M2 Workbench · Frontend App
   ============================================================================ */

const API = {
  state:   () => fetch('/api/state').then(r => r.json()),
  tasks:   () => fetch('/api/tasks').then(r => r.json()),
  notes:   () => fetch('/api/notes').then(r => r.json()),
  metrics: () => fetch('/api/metrics').then(r => r.json()),
  graph:   () => fetch('/api/graph').then(r => r.json()),
  stats:   () => fetch('/api/stats').then(r => r.json()),
  seed:    () => fetch('/api/seed', {method:'POST'}).then(r => r.json()),
};

const callAPI = {
  patchTask: (id, body) => fetch(`/api/tasks/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  deleteTask: (id) => fetch(`/api/tasks/${id}`, {method:'DELETE'}).then(r=>r.json()),
  createTask: (body) => fetch('/api/tasks', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  createNote: (body) => fetch('/api/notes', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  patchNote: (id, body) => fetch(`/api/notes/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  deleteNote: (id) => fetch(`/api/notes/${id}`, {method:'DELETE'}).then(r=>r.json()),
  createMetric: (body) => fetch('/api/metrics', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  recordMetric: (id, value, note) => fetch(`/api/metrics/${id}/record`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value, note})
  }).then(r=>r.json()),
  patchMetric: (id, body) => fetch(`/api/metrics/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
  deleteMetric: (id) => fetch(`/api/metrics/${id}`, {method:'DELETE'}).then(r=>r.json()),
  putState: (key, value) => fetch(`/api/state/${key}`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value})
  }).then(r=>r.json()),
};

const CACHE = {state:null, tasks:null, notes:null, metrics:null, graph:null};

const SETTINGS = {
  theme: localStorage.getItem('wb_theme') || 'light',
  density: localStorage.getItem('wb_density') || 'comfortable',
  defaultView: localStorage.getItem('wb_defaultView') || 'dashboard',
  notifTask: localStorage.getItem('wb_notifTask') !== 'false',
  notifNote: localStorage.getItem('wb_notifNote') !== 'false',
  notifMetric: localStorage.getItem('wb_notifMetric') === 'true',
  shortcuts: JSON.parse(localStorage.getItem('wb_shortcuts') || '[]'),
};

const DEFAULT_SHORTCUTS = [
  {id:'sc1', icon:'🧮', label:'Math Cache', sub:'查看统计'},
  {id:'sc2', icon:'🔍', label:'LaTeX 解析', sub:'测试解析率'},
  {id:'sc3', icon:'📊', label:'基准测试', sub:'运行 benchmark'},
  {id:'sc4', icon:'🧪', label:'错误测试', sub:'注入错误样本'},
  {id:'sc5', icon:'📚', label:'M1 文档', sub:'evolution-v5-ve1-math.md'},
  {id:'sc6', icon:'🚀', label:'M2 路线图', sub:'evolution-v5-ve1-math-m2-roadmap.md'},
];

if (SETTINGS.shortcuts.length === 0) {
  SETTINGS.shortcuts = [...DEFAULT_SHORTCUTS];
  localStorage.setItem('wb_shortcuts', JSON.stringify(SETTINGS.shortcuts));
}

// ============================================================================
// Utilities
// ============================================================================

function $(sel, root=document) { return root.querySelector(sel); }
function $$(sel, root=document) { return [...root.querySelectorAll(sel)]]; }

function toast(msg, type='info') {
  const c = $('#toastContainer');
  const t = document.createElement('div');
  t.className = `toast toast--${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity='0'; t.style.transform='translateX(20px)'; }, 2700);
  setTimeout(() => t.remove(), 3000);
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('zh-CN', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'});
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ============================================================================
// Theme
// ============================================================================

function applyTheme(theme) {
  let actual = theme;
  if (theme === 'auto') {
    actual = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', actual);
  SETTINGS.theme = theme;
  localStorage.setItem('wb_theme', theme);
  // 重新渲染图表（颜色可能变）
  if (CACHE.metrics) renderMetrics();
  if (network) updateNetworkTheme();
}

function applyDensity() {
  const compact = SETTINGS.density === 'compact';
  document.body.classList.toggle('compact', compact);
}

let network = null;
let metricsChart = null;

function updateNetworkTheme() {
  if (!network) return;
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  network.setOptions({
    nodes: {font: {color: dark ? '#e6edf3' : '#1a1d24'}},
    edges: {color: {color: dark ? '#3a4555' : '#d1d5db'}},
  });
}

// ============================================================================
// View Routing
// ============================================================================

let currentView = SETTINGS.defaultView;

function showView(name) {
  currentView = name;
  $$('.view').forEach(v => v.style.display = v.getAttribute('data-view') === name ? '' : 'none');
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.getAttribute('data-view') === name));
  if (window.innerWidth <= 768) $('#sidebar').classList.remove('open');
  localStorage.setItem('wb_lastView', name);
  // 渲染触发
  if (name === 'graph' && !network) renderGraph();
  if (name === 'metrics' && !metricsChart) renderMetrics();
}

// ============================================================================
// Dashboard
// ============================================================================

async function renderDashboard() {
  if (!CACHE.state) CACHE.state = await API.state();
  const s = CACHE.state;
  const versionCard = $('#versionCard');
  const info = $('#versionInfo');

  const cur = s.current_version?.value || '-';
  const next = s.next_version?.value || '-';
  const release = s.release_date?.value || '-';
  const target = s.target_release?.value || '-';
  info.textContent = `${cur} → ${next} · 发布 ${release} · 目标 ${target}`;

  versionCard.innerHTML = `
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi__label">当前版本</div><div class="kpi__value">${escapeHtml(cur)}</div></div>
      <div class="kpi"><div class="kpi__label">下一版本</div><div class="kpi__value">${escapeHtml(next)}</div></div>
      <div class="kpi"><div class="kpi__label">子系统</div><div class="kpi__value" style="font-size:18px;">${escapeHtml(s.subsystem_zh?.value || '')}</div></div>
      <div class="kpi"><div class="kpi__label">能力</div><div class="kpi__value" style="font-size:14px;">${escapeHtml(s.capabilities?.value || '')}</div></div>
      <div class="kpi"><div class="kpi__label">目标发布</div><div class="kpi__value" style="font-size:18px;">${escapeHtml(target)}</div></div>
      <div class="kpi"><div class="kpi__label">Owner</div><div class="kpi__value" style="font-size:18px;">${escapeHtml(s.owner?.value || '')}</div></div>
    </div>
    <div style="margin-top:14px; padding:12px 14px; background:var(--bg-soft); border-radius:8px; font-size:13px; line-height:1.6;">
      ${escapeHtml(s.summary?.value || '')}
    </div>
  `;

  // 关键指标
  if (!CACHE.metrics) CACHE.metrics = await API.metrics();
  renderKpis($('#kpiGrid'), CACHE.metrics);

  // 阶段进度
  if (!CACHE.tasks) CACHE.tasks = await API.tasks();
  renderPhaseProgress($('#phaseProgress'), CACHE.tasks);

  $('#phaseLabel').textContent = s.phase_label?.value || '';
}

// ============================================================================
// KPIs
// ============================================================================

function renderKpis(container, metrics) {
  container.innerHTML = '';
  metrics.forEach(m => {
    const el = document.createElement('div');
    el.className = 'kpi';
    const cur = m.current_value;
    const tgt = m.target_value;
    const base = m.baseline_value;
    const dir = m.direction || 'up';
    let progress = 0;
    let delta = '';

    if (cur !== null && tgt !== null && base !== null) {
      if (dir === 'up') {
        progress = Math.min(100, Math.max(0, ((cur - base) / (tgt - base)) * 100));
        if (cur > base) delta = `<span class="kpi__delta--up">▲ ${(cur - base).toFixed(1)} vs baseline</span>`;
        else if (cur < base) delta = `<span class="kpi__delta--bad">▼ ${(base - cur).toFixed(1)} vs baseline</span>`;
        else delta = `<span class="kpi__delta">— baseline</span>`;
      } else {
        progress = Math.min(100, Math.max(0, ((base - cur) / (base - tgt)) * 100));
        if (cur < base) delta = `<span class="kpi__delta--up">▼ ${(base - cur).toFixed(1)} vs baseline</span>`;
        else delta = `<span class="kpi__delta--bad">▲ ${(cur - base).toFixed(1)} vs baseline</span>`;
      }
    }

    el.innerHTML = `
      <div class="kpi__label">${escapeHtml(m.name)}</div>
      <div class="kpi__value">${cur !== null ? cur : '—'}<span class="kpi__unit">${escapeHtml(m.unit || '')}</span></div>
      <div class="kpi__delta">${delta}</div>
      ${m.display_type === 'progress' || m.display_type === 'gauge' ?
        `<div class="kpi__progress"><div class="kpi__bar" style="width:${progress}%"></div></div>` : ''}
    `;
    container.appendChild(el);
  });
}

function renderPhaseProgress(container, tasks) {
  container.innerHTML = '';
  const phases = ['M2-A', 'M2-B', 'M2-C', 'M3'];
  const labels = {
    'M2-A': 'Phase A · 底座建设 (W1~W4)',
    'M2-B': 'Phase B · 概念对齐 (W5~W8)',
    'M2-C': 'Phase C · 推理能力 (W9~W12)',
    'M3': 'M3 · 设想 (vLLM judge 迁移)',
  };
  phases.forEach(p => {
    const list = tasks.filter(t => t.phase === p);
    const done = list.filter(t => t.status === 'done').length;
    const total = list.length;
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    const el = document.createElement('div');
    el.style.cssText = 'padding:12px 14px; border:1px solid var(--border); border-radius:8px; margin-bottom:8px; background:var(--bg-soft);';
    el.innerHTML = `
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
        <span style="font-weight:600; font-size:13px;">${labels[p]}</span>
        <span style="margin-left:auto; font-size:12px; color:var(--text-soft);">${done}/${total} 完成</span>
        <span style="font-weight:700; color:var(--primary);">${pct}%</span>
      </div>
      <div style="height:6px; background:var(--bg-hover); border-radius:3px; overflow:hidden;">
        <div style="height:100%; background:linear-gradient(90deg, var(--primary), var(--info)); width:${pct}%; transition:width .3s;"></div>
      </div>
    `;
    container.appendChild(el);
  });
}

// ============================================================================
// Roadmap
// ============================================================================

let activeTaskFilter = 'all';
let taskSearchTerm = '';

async function renderRoadmap() {
  if (!CACHE.tasks) CACHE.tasks = await API.tasks();
  $('#taskCount').textContent = CACHE.tasks.length;

  // Phase tabs
  const phases = ['all', ...new Set(CACHE.tasks.map(t => t.phase))];
  const tabs = $('#phaseTabs');
  tabs.innerHTML = '';
  const labels = {
    'all': '全部',
    'M2-A': 'Phase A',
    'M2-B': 'Phase B',
    'M2-C': 'Phase C',
    'M3': 'M3 设想',
    'tooling': '工具',
  };
  phases.forEach(p => {
    const tab = document.createElement('div');
    tab.className = 'phase-tab' + (p === activeTaskFilter ? ' active' : '');
    tab.textContent = labels[p] || p;
    tab.onclick = () => { activeTaskFilter = p; renderRoadmap(); };
    tabs.appendChild(tab);
  });

  // Tasks
  renderTaskList();
}

function renderTaskList() {
  const list = $('#taskList');
  list.innerHTML = '';

  let tasks = CACHE.tasks;
  if (activeTaskFilter !== 'all') tasks = tasks.filter(t => t.phase === activeTaskFilter);
  const statusFilter = $('#taskStatusFilter').value;
  if (statusFilter) tasks = tasks.filter(t => t.status === statusFilter);
  const priorityFilter = $('#taskPriorityFilter').value;
  if (priorityFilter) tasks = tasks.filter(t => t.priority === parseInt(priorityFilter));
  if (taskSearchTerm) {
    const s = taskSearchTerm.toLowerCase();
    tasks = tasks.filter(t => t.title.toLowerCase().includes(s) || (t.description || '').toLowerCase().includes(s));
  }

  if (tasks.length === 0) {
    list.innerHTML = '<div class="empty"><div class="empty__icon">📭</div>无匹配任务</div>';
    return;
  }

  tasks.forEach(t => {
    const row = document.createElement('div');
    row.className = 'task-row';
    const isMilestone = (t.tags || []).includes('milestone');
    const isFuture = t.phase === 'M3' || t.phase === 'tooling';
    const pTag = `tag tag--p${t.priority || 3}`;
    const tags = (t.tags || []).filter(x => !['milestone', '底座', 'future', 'tooling'].includes(x)).slice(0, 3);
    row.innerHTML = `
      <div class="task-row__check ${t.status === 'done' ? 'done' : ''}" data-id="${t.id}"></div>
      <div class="task-row__main">
        <div class="task-row__title ${t.status === 'done' ? 'done' : ''}">
          ${isMilestone ? '<span class="tag tag--milestone" style="margin-right:6px;">里程碑</span>' : ''}
          ${escapeHtml(t.title)}
        </div>
        <div class="task-row__desc">${escapeHtml(t.description || '')}</div>
        <div class="task-row__meta" style="margin-top:6px;">
          <span class="${pTag}">P${t.priority}</span>
          ${t.week ? `<span class="tag tag--week">${escapeHtml(t.week)}</span>` : ''}
          <span class="tag">${escapeHtml(t.category || t.phase)}</span>
          ${tags.map(x => `<span class="tag">${escapeHtml(x)}</span>`).join('')}
        </div>
      </div>
      ${t.progress > 0 ? `<div class="task-row__progress" title="${t.progress}%"><div class="task-row__progress-bar" style="width:${t.progress}%"></div></div>` : '<div style="width:60px;"></div>'}
    `;
    row.querySelector('.task-row__check').onclick = async () => {
      const newStatus = t.status === 'done' ? 'todo' : 'done';
      await callAPI.patchTask(t.id, {status: newStatus, progress: newStatus === 'done' ? 100 : 0});
      t.status = newStatus;
      t.progress = newStatus === 'done' ? 100 : 0;
      CACHE.tasks = await API.tasks();
      renderTaskList();
      renderPhaseProgress($('#phaseProgress'), CACHE.tasks);
      if (SETTINGS.notifTask) toast(`任务标记为 ${newStatus === 'done' ? '已完成' : '待办'}`, 'success');
    };
    row.onclick = (e) => {
      if (e.target.classList.contains('task-row__check')) return;
      showTaskDetail(t);
    };
    list.appendChild(row);
  });
}

function showTaskDetail(t) {
  showModal({
    title: t.title,
    body: `
      <p style="margin-top:0;">${escapeHtml(t.description || '无描述')}</p>
      <div style="display:grid; grid-template-columns:auto 1fr; gap:6px 12px; font-size:13px; margin-top:12px;">
        <div style="color:var(--text-soft);">阶段</div><div>${escapeHtml(t.phase)} · ${escapeHtml(t.week || '-')}</div>
        <div style="color:var(--text-soft);">分类</div><div>${escapeHtml(t.category || '')}</div>
        <div style="color:var(--text-soft);">优先级</div><div>P${t.priority}</div>
        <div style="color:var(--text-soft);">状态</div><div>${escapeHtml(t.status)} (${t.progress}%)</div>
        <div style="color:var(--text-soft);">ROI</div><div>${t.roi || '-'}</div>
        <div style="color:var(--text-soft);">标签</div><div>${(t.tags || []).join(', ')}</div>
      </div>
    `,
    footer: `
      <button class="btn btn--danger" id="delTaskBtn">删除</button>
      <button class="btn btn--primary" onclick="closeModal()">关闭</button>
    `,
    onOpen: (modal) => {
      modal.querySelector('#delTaskBtn').onclick = async () => {
        if (!confirm('确认删除？')) return;
        await callAPI.deleteTask(t.id);
        CACHE.tasks = await API.tasks();
        renderTaskList();
        closeModal();
        toast('任务已删除', 'success');
      };
    },
  });
}

// ============================================================================
// Concept Graph
// ============================================================================

async function renderGraph() {
  if (!CACHE.graph) CACHE.graph = await API.graph();

  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const colorMap = {
    '数学领域': '#4f46e5',
    '能力层': '#10b981',
    '工具': '#f59e0b',
    '模型': '#ec4899',
  };

  const nodes = CACHE.graph.nodes.map(n => ({
    id: n.id,
    label: n.name,
    title: n.description || n.name,
    color: {background: colorMap[n.category] || '#94a3b8', border: colorMap[n.category] || '#94a3b8'},
    font: {color: '#ffffff', size: 12, face: 'sans-serif'},
    shape: n.category === '数学领域' ? 'box' : 'dot',
    size: n.category === '数学领域' ? 18 : 14 + (n.capability_level || 1) * 2,
  }));
  const edges = CACHE.graph.edges.map(e => ({
    from: e.source_id,
    to: e.target_id,
    label: e.label || '',
    arrows: e.relation === 'enables' ? 'to' : (e.relation === 'depends_on' ? 'from' : ''),
    color: {color: dark ? '#3a4555' : '#d1d5db'},
    font: {color: dark ? '#9da7b3' : '#6b7280', size: 10, strokeWidth: 0},
    width: e.weight || 1,
    smooth: {type: 'curvedCW', roundness: 0.2},
  }));

  const container = $('#network');
  const data = {nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges)};
  const options = {
    layout: {hierarchical: false},
    physics: {
      enabled: true,
      barnesHut: {gravitationalConstant: -8000, springLength: 120, springConstant: 0.04},
      stabilization: {iterations: 200},
    },
    interaction: {hover: true, tooltipDelay: 100},
    nodes: {borderWidth: 2, shadow: false},
  };
  network = new vis.Network(container, data, options);
  updateNetworkTheme();
}

// ============================================================================
// Notes
// ============================================================================

let noteSearchTerm = '';
let noteTypeFilter = '';

async function renderNotes() {
  if (!CACHE.notes) CACHE.notes = await API.notes();
  renderNotesList();
}

function renderNotesList() {
  const list = $('#notesList');
  list.innerHTML = '';
  let notes = CACHE.notes;
  if (noteTypeFilter) notes = notes.filter(n => n.reflection_type === noteTypeFilter);
  if (noteSearchTerm) {
    const s = noteSearchTerm.toLowerCase();
    notes = notes.filter(n => n.title.toLowerCase().includes(s) || n.content.toLowerCase().includes(s));
  }
  if (notes.length === 0) {
    list.innerHTML = '<div class="empty"><div class="empty__icon">📝</div>暂无笔记</div>';
    return;
  }
  notes.forEach(n => {
    const card = document.createElement('div');
    const type = n.reflection_type || 'learning';
    card.className = `note-card note-card--${type}`;
    card.innerHTML = `
      <div class="note-card__title">${escapeHtml(n.title)}</div>
      <div class="note-card__content">${escapeHtml(n.content)}</div>
      <div class="note-card__meta">
        <span class="tag">${escapeHtml(type)}</span>
        ${(n.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}
        <span style="margin-left:auto;">${formatDate(n.updated_at)}</span>
        <button class="btn btn--sm btn--danger" data-id="${n.id}" style="margin-left:8px;">删除</button>
      </div>
    `;
    card.querySelector('button').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm('确认删除？')) return;
      await callAPI.deleteNote(n.id);
      CACHE.notes = await API.notes();
      renderNotesList();
      toast('笔记已删除', 'success');
    };
    list.appendChild(card);
  });
}

// ============================================================================
// Metrics
// ============================================================================

async function renderMetrics() {
  if (!CACHE.metrics) CACHE.metrics = await API.metrics();
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  renderKpis($('#metricsGrid'), CACHE.metrics);

  // Chart
  const ctx = $('#metricsChart');
  if (metricsChart) metricsChart.destroy();

  const labels = ['Baseline', 'Current', 'Target'];
  const datasets = CACHE.metrics.map(m => {
    const cur = m.current_value ?? 0;
    const base = m.baseline_value ?? 0;
    const tgt = m.target_value ?? 0;
    return {
      label: m.name,
      data: [base, cur, tgt],
      borderWidth: 2,
      tension: 0.3,
    };
  });

  metricsChart = new Chart(ctx, {
    type: 'radar',
    data: {labels, datasets},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {position: 'bottom', labels: {color: dark ? '#e6edf3' : '#1a1d24', font: {size: 11}}},
      },
      scales: {
        r: {
          ticks: {color: dark ? '#9da7b3' : '#6b7280', backdropColor: 'transparent'},
          grid: {color: dark ? '#2a3340' : '#e5e7eb'},
          angleLines: {color: dark ? '#2a3340' : '#e5e7eb'},
          pointLabels: {color: dark ? '#e6edf3' : '#1a1d24'},
        },
      },
    },
  });
}

// ============================================================================
// Shortcuts
// ============================================================================

function renderShortcuts() {
  const grid = $('#shortcutsGrid');
  grid.innerHTML = '';
  SETTINGS.shortcuts.forEach((sc, idx) => {
    const tile = document.createElement('div');
    tile.className = 'shortcut-tile';
    tile.dataset.id = sc.id;
    tile.innerHTML = `
      <div class="shortcut-tile__del">×</div>
      <div class="shortcut-tile__icon">${sc.icon}</div>
      <div class="shortcut-tile__label">${escapeHtml(sc.label)}</div>
      <div class="shortcut-tile__sub">${escapeHtml(sc.sub || '')}</div>
    `;
    tile.onclick = (e) => {
      if (e.target.classList.contains('shortcut-tile__del')) return;
      toast(`${sc.label} - ${sc.sub || ''}`, 'info');
    };
    tile.querySelector('.shortcut-tile__del').onclick = (e) => {
      e.stopPropagation();
      SETTINGS.shortcuts = SETTINGS.shortcuts.filter(x => x.id !== sc.id);
      localStorage.setItem('wb_shortcuts', JSON.stringify(SETTINGS.shortcuts));
      renderShortcuts();
      toast('快捷方式已删除', 'success');
    };
    grid.appendChild(tile);
  });

  // Add tile
  const add = document.createElement('div');
  add.className = 'shortcut-tile shortcut-tile__add';
  add.innerHTML = `
    <div class="shortcut-tile__icon" style="background:transparent; color:var(--text-soft);">+</div>
    <div class="shortcut-tile__label">添加</div>
  `;
  add.onclick = showAddShortcutModal;
  grid.appendChild(add);

  // SortableJS
  new Sortable(grid, {
    animation: 150,
    handle: '.shortcut-tile',
    filter: '.shortcut-tile__add',
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    onEnd: () => {
      const ids = [...grid.querySelectorAll('.shortcut-tile:not(.shortcut-tile__add)')].map(x => x.dataset.id);
      SETTINGS.shortcuts = ids.map(id => SETTINGS.shortcuts.find(x => x.id === id)).filter(Boolean);
      localStorage.setItem('wb_shortcuts', JSON.stringify(SETTINGS.shortcuts));
      renderShortcuts();
    },
  });
}

function showAddShortcutModal() {
  showModal({
    title: '添加快捷方式',
    body: `
      <label class="form-label">图标（Emoji）</label>
      <input class="input" id="scIcon" placeholder="如：📊" maxlength="4" />
      <label class="form-label">名称</label>
      <input class="input" id="scLabel" placeholder="如：基准测试" />
      <label class="form-label">说明</label>
      <input class="input" id="scSub" placeholder="如：跑 benchmark" />
    `,
    footer: `
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn--primary" id="scAddBtn">添加</button>
    `,
    onOpen: (modal) => {
      modal.querySelector('#scAddBtn').onclick = () => {
        const icon = modal.querySelector('#scIcon').value || '🔗';
        const label = modal.querySelector('#scLabel').value || '新快捷';
        const sub = modal.querySelector('#scSub').value || '';
        SETTINGS.shortcuts.push({id: 'sc_'+Date.now(), icon, label, sub});
        localStorage.setItem('wb_shortcuts', JSON.stringify(SETTINGS.shortcuts));
        renderShortcuts();
        closeModal();
        toast('快捷方式已添加', 'success');
      };
    },
  });
}

// ============================================================================
// Settings
// ============================================================================

function renderSettings() {
  $('#settingTheme').value = SETTINGS.theme;
  $('#settingDensity').value = SETTINGS.density;
  $('#settingDefaultView').value = SETTINGS.defaultView;
  $('#settingNotifTask').checked = SETTINGS.notifTask;
  $('#settingNotifNote').checked = SETTINGS.notifNote;
  $('#settingNotifMetric').checked = SETTINGS.notifMetric;
}

// ============================================================================
// Modal
// ============================================================================

function showModal({title, body, footer, onOpen}) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = body;
  if (footer) {
    $('#modalFooter').innerHTML = footer;
    $('#modalFooter').style.display = '';
  } else {
    $('#modalFooter').style.display = 'none';
  }
  $('#modalBg').style.display = 'grid';
  if (onOpen) onOpen($('#modal'));
}

function closeModal() {
  $('#modalBg').style.display = 'none';
}

// ============================================================================
// Init
// ============================================================================

function init() {
  // Theme
  applyTheme(SETTINGS.theme);
  applyDensity();

  // Nav
  $$('.nav-item').forEach(n => {
    n.onclick = () => showView(n.getAttribute('data-view'));
  });

  // Theme toggle
  $('#themeToggle').onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    applyTheme(cur === 'dark' ? 'light' : 'dark');
  };

  // Mobile menu
  $('#mobileMenuBtn').onclick = () => $('#sidebar').classList.toggle('open');

  // Modal close
  $('#modalClose').onclick = closeModal;
  $('#modalBg').onclick = (e) => { if (e.target.id === 'modalBg') closeModal(); };

  // Reset / Seed
  $('#seedBtn').onclick = async () => {
    if (!confirm('确认重置全部数据为种子？')) return;
    const r = await API.seed();
    CACHE.state = CACHE.tasks = CACHE.notes = CACHE.metrics = CACHE.graph = null;
    await renderCurrentView();
    toast(`重置完成: ${JSON.stringify(r.seeded)}`, 'success');
  };

  // Settings
  $('#settingsBtn').onclick = () => showView('settings');
  $('#settingTheme').onchange = (e) => applyTheme(e.target.value);
  $('#settingDensity').onchange = (e) => { SETTINGS.density = e.target.value; localStorage.setItem('wb_density', e.target.value); applyDensity(); };
  $('#settingDefaultView').onchange = (e) => { SETTINGS.defaultView = e.target.value; localStorage.setItem('wb_defaultView', e.target.value); };
  $('#settingNotifTask').onchange = (e) => { SETTINGS.notifTask = e.target.checked; localStorage.setItem('wb_notifTask', e.target.checked); };
  $('#settingNotifNote').onchange = (e) => { SETTINGS.notifNote = e.target.checked; localStorage.setItem('wb_notifNote', e.target.checked); };
  $('#settingNotifMetric').onchange = (e) => { SETTINGS.notifMetric = e.target.checked; localStorage.setItem('wb_notifMetric', e.target.checked); };

  $('#exportDataBtn').onclick = exportData;
  $('#resetBtn').onclick = () => {
    if (confirm('确认清空全部数据？此操作不可恢复！')) {
      localStorage.clear();
      location.reload();
    }
  };

  // Roadmap filters
  $('#taskSearch').oninput = (e) => { taskSearchTerm = e.target.value; renderTaskList(); };
  $('#taskStatusFilter').onchange = () => renderTaskList();
  $('#taskPriorityFilter').onchange = () => renderTaskList();
  $('#addTaskBtn').onclick = showAddTaskModal;

  // Notes filters
  $('#noteSearch').oninput = (e) => { noteSearchTerm = e.target.value; renderNotesList(); };
  $('#noteTypeFilter').onchange = (e) => { noteTypeFilter = e.target.value; renderNotesList(); };
  $('#addNoteBtn').onclick = showAddNoteModal;

  // Metrics
  $('#addMetricBtn').onclick = showAddMetricModal;

  // Shortcuts
  $('#addShortcutBtn').onclick = showAddShortcutModal;

  // Initial render
  renderCurrentView();
}

async function renderCurrentView() {
  switch (currentView) {
    case 'dashboard': await renderDashboard(); break;
    case 'roadmap':   await renderRoadmap(); break;
    case 'graph':     if (!network) await renderGraph(); break;
    case 'notes':     await renderNotes(); break;
    case 'metrics':   await renderMetrics(); break;
    case 'shortcuts': renderShortcuts(); break;
    case 'settings':  renderSettings(); break;
  }
  showView(currentView);
}

function exportData() {
  const data = {
    state: CACHE.state, tasks: CACHE.tasks, notes: CACHE.notes,
    metrics: CACHE.metrics, graph: CACHE.graph,
    shortcuts: SETTINGS.shortcuts,
    exported_at: new Date().toISOString(),
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `workbench-export-${new Date().toISOString().slice(0,10)}.json`;
  a.click();
  toast('数据已导出', 'success');
}

function showAddTaskModal() {
  showModal({
    title: '新增任务',
    body: `
      <label class="form-label">标题</label>
      <input class="input" id="tTitle" />
      <label class="form-label">描述</label>
      <textarea class="textarea" id="tDesc"></textarea>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
        <div>
          <label class="form-label">阶段</label>
          <select class="select" id="tPhase">
            <option value="M2-A">M2-A</option>
            <option value="M2-B">M2-B</option>
            <option value="M2-C">M2-C</option>
            <option value="M3">M3</option>
            <option value="backlog">backlog</option>
          </select>
        </div>
        <div>
          <label class="form-label">周</label>
          <input class="input" id="tWeek" placeholder="如 W5" />
        </div>
      </div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
        <div>
          <label class="form-label">优先级</label>
          <select class="select" id="tPriority">
            <option value="1">P1</option><option value="2" selected>P2</option><option value="3">P3</option>
          </select>
        </div>
        <div>
          <label class="form-label">分类</label>
          <input class="input" id="tCategory" placeholder="如 D6 性能" />
        </div>
      </div>
    `,
    footer: `
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn--primary" id="tAddBtn">添加</button>
    `,
    onOpen: (modal) => {
      modal.querySelector('#tAddBtn').onclick = async () => {
        const t = {
          title: modal.querySelector('#tTitle').value || '新任务',
          phase: modal.querySelector('#tPhase').value,
          week: modal.querySelector('#tWeek').value || null,
          priority: parseInt(modal.querySelector('#tPriority').value),
          category: modal.querySelector('#tCategory').value || null,
          description: modal.querySelector('#tDesc').value || null,
        };
        await callAPI.createTask(t);
        CACHE.tasks = await API.tasks();
        renderTaskList();
        renderPhaseProgress($('#phaseProgress'), CACHE.tasks);
        closeModal();
        toast('任务已添加', 'success');
      };
    },
  });
}

function showAddNoteModal() {
  showModal({
    title: '新增笔记',
    body: `
      <label class="form-label">标题</label>
      <input class="input" id="nTitle" />
      <label class="form-label">内容（支持 markdown）</label>
      <textarea class="textarea" id="nContent" style="min-height:140px;"></textarea>
      <label class="form-label">类型</label>
      <select class="select" id="nType">
        <option value="learning">学习</option>
        <option value="issue">问题</option>
        <option value="decision">决策</option>
        <option value="idea">想法</option>
      </select>
      <label class="form-label">标签（逗号分隔）</label>
      <input class="input" id="nTags" placeholder="如：W1, D6" />
    `,
    footer: `
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn--primary" id="nAddBtn">添加</button>
    `,
    onOpen: (modal) => {
      modal.querySelector('#nAddBtn').onclick = async () => {
        const n = {
          title: modal.querySelector('#nTitle').value || '无标题',
          content: modal.querySelector('#nContent').value || '',
          reflection_type: modal.querySelector('#nType').value,
          tags: modal.querySelector('#nTags').value.split(',').map(x => x.trim()).filter(Boolean),
        };
        await callAPI.createNote(n);
        CACHE.notes = await API.notes();
        renderNotesList();
        closeModal();
        if (SETTINGS.notifNote) toast('笔记已添加', 'success');
      };
    },
  });
}

function showAddMetricModal() {
  showModal({
    title: '新增指标',
    body: `
      <label class="form-label">名称</label>
      <input class="input" id="mName" />
      <label class="form-label">分类</label>
      <select class="select" id="mCategory">
        <option>解析</option><option>推理</option><option>性能</option><option>对齐</option><option>异常</option><option>自定义</option>
      </select>
      <label class="form-label">单位</label>
      <input class="input" id="mUnit" placeholder="如：%" />
      <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px;">
        <div><label class="form-label">基线</label><input class="input" type="number" id="mBase" /></div>
        <div><label class="form-label">当前</label><input class="input" type="number" id="mCur" /></div>
        <div><label class="form-label">目标</label><input class="input" type="number" id="mTgt" /></div>
      </div>
      <label class="form-label">方向</label>
      <select class="select" id="mDir">
        <option value="up">越高越好</option>
        <option value="down">越低越好</option>
      </select>
    `,
    footer: `
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn--primary" id="mAddBtn">添加</button>
    `,
    onOpen: (modal) => {
      modal.querySelector('#mAddBtn').onclick = async () => {
        const m = {
          name: modal.querySelector('#mName').value || '新指标',
          category: modal.querySelector('#mCategory').value,
          unit: modal.querySelector('#mUnit').value || '',
          baseline_value: parseFloat(modal.querySelector('#mBase').value) || 0,
          current_value: parseFloat(modal.querySelector('#mCur').value) || 0,
          target_value: parseFloat(modal.querySelector('#mTgt').value) || 0,
          direction: modal.querySelector('#mDir').value,
        };
        await callAPI.createMetric(m);
        CACHE.metrics = await API.metrics();
        renderMetrics();
        closeModal();
        toast('指标已添加', 'success');
      };
    },
  });
}

document.addEventListener('DOMContentLoaded', init);