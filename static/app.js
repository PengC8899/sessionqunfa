const state = {
  groups: [],
  filteredGroups: [],
  token: '',
  includeChannels: false,
  selectedIds: new Set(),
  account: '',
  accounts: [],
  sending: false,
  summary: [],
  summaryFiltered: [],
  summarySortKey: 'account',
  summarySortAsc: true,
  authorizedAccounts: [],
  receiverMode: false,
};

function loadToken() {
  const t = localStorage.getItem('adminToken') || '';
  state.token = t;
  const input = document.getElementById('adminToken');
  if (input) input.value = t;
}

function saveToken() {
  const t = document.getElementById('adminToken').value.trim();
  state.token = t;
  localStorage.setItem('adminToken', t);
  const el = document.getElementById('tokenStatus');
  if (!t) {
    if (el) { el.textContent = '请输入令牌'; el.className = 'status-error'; }
    alert('请输入令牌');
    return;
  }
  // Remove "pc-20251206-7575" legacy token check if needed, but for now just save what user typed
  if (el) { el.textContent = '令牌已保存'; el.className = 'status-success'; }
  localStorage.setItem('tokenLocked', '1');
  updateTokenLockUI(); fetchAccounts().then(() => { fetchAuthStatus(); fetchGroups(); });
}

function groupsCacheKey() {
  const acc = state.account || '';
  const scope = state.includeChannels ? 'all' : 'groups';
  return `groups:${acc}:${scope}`;
}

function loadGroupsFromCache() {
  try {
    const raw = localStorage.getItem(groupsCacheKey());
    if (!raw) return null;
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return arr;
    return null;
  } catch { return null; }
}

function saveGroupsToCache(data) {
  try { localStorage.setItem(groupsCacheKey(), JSON.stringify(data || [])); } catch {}
}

function renderGroupsFromCacheIfAvailable() {
  const cached = loadGroupsFromCache();
  if (cached && cached.length) {
    state.groups = cached;
    state.filteredGroups = cached;
    renderGroups();
  }
}

function renderGlobalSummary() {
  const tbody = document.getElementById('globalSummaryBody');
  if (!tbody) return;
  tbody.innerHTML = '';
  const rows = state.summaryFiltered.length ? state.summaryFiltered : state.summary;
  rows.forEach(r => {
    const total = Math.max(0, parseInt(r.total || 0));
    const succ = Math.max(0, parseInt(r.success || 0));
    const fail = Math.max(0, parseInt(r.failed || 0));
    const pct = total > 0 ? Math.round((succ / total) * 100) : 0;
    const barColor = pct >= 80 ? '#52c41a' : (pct >= 50 ? '#1890ff' : '#f5222d');
    const progressBar = `<div style="width:100%; background:#f0f0f0; height:6px; border-radius:3px; overflow:hidden;"><div style="width:${pct}%; height:6px; background:${barColor}; transition:width 0.3s;"></div></div> <div style="font-size:11px; color:#888; text-align:right;">${pct}%</div>`;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${r.account}</td>
      <td>${progressBar}</td>
      <td class="status-success">${succ}</td>
      <td class="status-error">${fail}</td>
      <td>${(r.current_round||0)}/${(r.rounds||0)}</td>
    `;
    tbody.appendChild(tr);
  });
}

function applySummaryFilter() {
  const q = (document.getElementById('summarySearch')?.value || '').trim().toLowerCase();
  if (!q) { state.summaryFiltered = []; renderGlobalSummary(); return; }
  state.summaryFiltered = (state.summary || []).filter(x => (x.account || '').toLowerCase().includes(q));
  renderGlobalSummary();
}

function sortSummary(key) {
  const asc = (state.summarySortKey === key) ? !state.summarySortAsc : true;
  state.summarySortKey = key; state.summarySortAsc = asc;
  const arr = (state.summary || []).slice();
  arr.sort((a,b) => {
    const va = (key === 'progress') ? ((a.total||0) ? (a.success||0)/(a.total||0) : 0) : (a[key] || 0);
    const vb = (key === 'progress') ? ((b.total||0) ? (b.success||0)/(b.total||0) : 0) : (b[key] || 0);
    if (typeof va === 'string' || typeof vb === 'string') return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    return asc ? (va - vb) : (vb - va);
  });
  state.summary = arr;
  applySummaryFilter();
}

async function fetchGlobalSummary(force = false) {
  const loadingEl = document.getElementById('summaryLoading');
  if (loadingEl && force) loadingEl.classList.remove('d-none');
  try {
    const res = await fetchWithRetry('/api/tasks/summary', { headers: { 'X-Admin-Token': state.token } }, 3000, force ? 2 : 1);
    if (!res.ok) { if (loadingEl) loadingEl.classList.add('d-none'); return; }
    const data = await res.json();
    state.summary = Array.isArray(data) ? data : [];
    const upd = document.getElementById('summaryUpdatedAt');
    if (upd) upd.textContent = '更新于 ' + new Date().toLocaleTimeString();
    sortSummary(state.summarySortKey);
    if (loadingEl) loadingEl.classList.add('d-none');
  } catch (e) {
    if (loadingEl) loadingEl.classList.add('d-none');
  }
}

async function fetchGroups(forceRefresh = false) {
  const loading = document.getElementById('groupCount');
  if (loading) loading.textContent = '加载中...';
  
  const onlyGroups = state.includeChannels ? 'false' : 'true';
  const acc = encodeURIComponent(state.account || '');
  const refresh = forceRefresh ? 'true' : 'false';
  const res = await fetch(`/api/groups?only_groups=${onlyGroups}&account=${acc}&refresh=${refresh}`, {
    headers: { 'X-Admin-Token': state.token },
  });
  if (!res.ok) {
    try {
      const d = await res.json();
      if (res.status === 401) {
        alert('令牌错误，请在顶部输入后点击“保存”');
      } else if (res.status === 403 && d.detail === 'session_not_authorized') {
        alert('账号未登录，请在上方登录管理中发送验证码并确认登录');
      } else {
        alert('群列表获取失败，请稍后重试');
      }
    } catch {
      alert('群列表获取失败，请稍后重试');
    }
    if (loading) loading.textContent = '加载失败';
    return;
  }
  const data = await res.json();
  if (Array.isArray(data) && data.length === 0 && !state.includeChannels) {
    state.includeChannels = true;
    try { localStorage.setItem('includeChannels', '1'); } catch {}
    const res2 = await fetch(`/api/groups?only_groups=false&account=${acc}&refresh=${refresh}`, { headers: { 'X-Admin-Token': state.token } });
    if (res2.ok) {
      const data2 = await res2.json();
      state.groups = data2;
      state.filteredGroups = data2;
      saveGroupsToCache(data2);
      const el = document.getElementById('includeChannels');
      if (el) el.checked = true;
      renderGroups();
      return;
    }
  }
  state.groups = data;
  state.filteredGroups = data;
  saveGroupsToCache(data);
  renderGroups();
}

// Separate function for token locking UI
function updateTokenLockUI() {
  const locked = (localStorage.getItem('tokenLocked') === '1');
  const input = document.getElementById('adminToken');
  const saveBtn = document.getElementById('saveToken');
  const editBtn = document.getElementById('editToken');
  if (input) input.disabled = locked;
  if (saveBtn) saveBtn.classList.toggle('hidden', locked);
  if (editBtn) editBtn.classList.toggle('hidden', !locked);
}

async function fetchAccounts() {
  const res = await fetch('/api/accounts/status', { headers: { 'X-Admin-Token': state.token } });
  if (!res.ok) return;
  const data = await res.json();
  state.accounts = data.map(item => item.account);
  const sel = document.getElementById('accountSelect');
  sel.innerHTML = '';
  data.forEach(item => {
    const opt = document.createElement('option');
    opt.value = item.account;
    opt.textContent = item.authorized ? `${item.account} (已授权)` : item.account;
    sel.appendChild(opt);
  });
  const saved = localStorage.getItem('selectedAccount');
  state.account = saved && state.accounts.includes(saved) ? saved : (state.accounts[0] || '');
  sel.value = state.account;
  localStorage.setItem('selectedAccount', state.account);
}

function renderGroups() {
  const ul = document.getElementById('groupList');
  ul.innerHTML = '';
  state.filteredGroups.forEach(g => {
    const li = document.createElement('li');
    li.className = 'group-item';
    const badge = g.is_channel ? '频道' : (g.is_megagroup ? '超级群' : '群');
    const checked = state.selectedIds.has(g.id) ? 'checked' : '';
    const disabled = (g.is_channel && !g.is_megagroup) ? 'disabled' : '';
    li.innerHTML = `
      <label>
        <input type="checkbox" class="groupCheck" value="${g.id}" ${checked} ${disabled} />
        <div class="group-info">
          <div class="group-name">${g.title}</div>
          <div class="group-meta">
            <span>${g.member_count ? `${g.member_count}人` : ''}</span>
            <span>${badge}</span>
            ${g.username ? `<span>@${g.username}</span>` : ''}
            ${disabled ? `<span style="color:var(--error)">不可发送</span>` : ''}
          </div>
        </div>
      </label>
    `;
    ul.appendChild(li);
  });
  document.getElementById('groupCount').textContent = `共 ${state.filteredGroups.length} 项`;
}

function filterGroups() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  if (!q) {
    state.filteredGroups = state.groups;
  } else {
    state.filteredGroups = state.groups.filter(g => (g.title || '').toLowerCase().includes(q));
  }
  renderGroups();
}

function setAll(selected) {
  document.querySelectorAll('.groupCheck').forEach(ch => { 
    ch.checked = selected; 
    const id = parseInt(ch.value);
    if (selected) state.selectedIds.add(id); else state.selectedIds.delete(id);
  });
  persistSelected();
}

function getSelectedIds() {
  return Array.from(state.selectedIds);
}

async function send(path) {
  if (state.sending) return;
  state.sending = true;
  const sendBtn = document.getElementById('sendBtn');
  const testBtn = document.getElementById('testBtn');
  if (sendBtn) sendBtn.disabled = true;
  if (testBtn) testBtn.disabled = true;
  const resultEl = document.getElementById('result');
  if (resultEl) resultEl.textContent = '发送中...';
  if (!state.token) {
    alert('请输入令牌并点击“保存”');
    if (resultEl) resultEl.textContent = '未设置令牌';
    state.sending = false;
    if (sendBtn) sendBtn.disabled = false;
    if (testBtn) testBtn.disabled = false;
    return;
  }
  const ids = getSelectedIds();
  const msg = document.getElementById('message').value;
  const parseMode = document.getElementById('parseMode').value;
  const delayMs = parseInt(document.getElementById('delayMs').value || '60000');
  const rounds = parseInt(document.getElementById('rounds')?.value || '100');
  const roundInterval = parseInt(document.getElementById('roundInterval')?.value || '1200');
  const disablePreview = document.getElementById('disablePreview').checked;
  if (!ids.length) { alert('请选择至少一个群'); state.sending = false; if (sendBtn) sendBtn.disabled = false; if (testBtn) testBtn.disabled = false; return; }
  if (!msg.trim()) { alert('消息不能为空'); state.sending = false; if (sendBtn) sendBtn.disabled = false; if (testBtn) testBtn.disabled = false; return; }
  const reqId = generateRequestId();
  const expectedMs = (delayMs * Math.max(ids.length, 1)) + 15000;
  const timeoutMs = path === 'send' ? 30000 : Math.min(900000, Math.max(20000, expectedMs));
  try {
    const apiPath = path === 'send' ? 'send-async' : path;
    const res = await fetchWithRetry(`/api/${apiPath}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-Token': state.token,
      },
      body: JSON.stringify({
        group_ids: ids,
        message: msg,
        parse_mode: parseMode,
        disable_web_page_preview: disablePreview,
        delay_ms: delayMs,
        rounds: Math.max(1, rounds),
        round_interval_s: Math.max(0, roundInterval),
        account: state.account,
        request_id: reqId,
      }),
    }, timeoutMs, path === 'send' ? 2 : 3);
    if (!res.ok) {
      if (res.status === 429) {
        if (resultEl) resultEl.textContent = '请求频率过高或重复，已忽略';
      } else if (res.status === 401) {
        if (resultEl) resultEl.textContent = '令牌错误，请在顶部保存令牌';
      } else if (res.status === 403) {
        if (resultEl) resultEl.textContent = '账号未登录，请先完成登录';
      } else {
        if (resultEl) resultEl.textContent = '发送失败';
      }
    } else {
      const data = await res.json();
      if (path === 'send') {
        if (resultEl) resultEl.textContent = `任务已创建(${data.task_id})，正在发送...`;
        await pollTaskUntilDone(data.task_id, resultEl);
      } else {
        if (resultEl) resultEl.textContent = `总数 ${data.total}｜成功 ${data.success}｜失败 ${data.failed}`;
        await fetchLogs();
      }
    }
  } catch (e) {
    if (resultEl) resultEl.textContent = '请求超时或网络错误，后台可能仍在发送';
  } finally {
    state.sending = false;
    if (sendBtn) sendBtn.disabled = false;
    if (testBtn) testBtn.disabled = false;
  }
}

async function pollTaskUntilDone(taskId, resultEl) {
  return new Promise(async (resolve) => {
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/api/task-status?task_id=${encodeURIComponent(taskId)}`, { headers: { 'X-Admin-Token': state.token } });
        if (!res.ok) return;
        const s = await res.json();
        const roundInfo = (s.rounds && s.current_round) ? `｜轮次 ${s.current_round}/${s.rounds}` : '';
        if (resultEl) resultEl.textContent = `总数 ${s.total}｜成功 ${s.success}｜失败 ${s.failed}${roundInfo}`;
        if (s.status === 'done') {
          clearInterval(timer);
          await fetchLogs();
          resolve();
        }
      } catch {}
    }, 1000);
  });
}

async function fetchLogs() {
  const res = await fetch('/api/logs?limit=50', {
    headers: { 'X-Admin-Token': state.token },
  });
  if (!res.ok) return;
  const data = await res.json();
  const tbody = document.getElementById('logsBody');
  tbody.innerHTML = '';
  data.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--text-muted); font-size:12px;">${r.created_at || ''}</td>
      <td style="font-size:12px;">${r.account_name || ''}</td>
      <td>${r.group_title || r.group_id}</td>
      <td class="${r.status === 'success' ? 'status-success' : 'status-error'}">${r.status}</td>
      <td style="font-size:12px;">${r.error || r.message_id || ''}</td>
    `;
    tbody.appendChild(tr);
  });
}

function bindEvents() {
  document.getElementById('saveToken').addEventListener('click', () => { saveToken(); fetchAccounts(); fetchAuthStatus(); fetchGroups(true); fetchLogs(); });
  
  // Login Popover Toggle
  const toggleLoginBtn = document.getElementById('toggleLogin');
  const loginPopover = document.getElementById('loginPopover');
  if (toggleLoginBtn && loginPopover) {
    toggleLoginBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      state.receiverMode = false;
      loginPopover.classList.toggle('active');
    });
    document.addEventListener('click', (e) => {
      if (!loginPopover.contains(e.target) && e.target !== toggleLoginBtn) {
        loginPopover.classList.remove('active');
      }
    });
    loginPopover.addEventListener('click', (e) => e.stopPropagation());
  }
  const receiverBtn = document.getElementById('copyReceiverLogin');
  if (receiverBtn && loginPopover) {
    receiverBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      state.receiverMode = true;
      loginPopover.classList.add('active');
    });
  }

  const editTokenBtn = document.getElementById('editToken');
  if (editTokenBtn) {
    editTokenBtn.addEventListener('click', () => {
      localStorage.setItem('tokenLocked', '0');
      updateTokenLockUI(); fetchAccounts().then(() => { fetchAuthStatus(); fetchGroups(); });
      const el = document.getElementById('tokenStatus');
      if (el) { el.textContent = '已解锁'; el.className = ''; }
    });
  }
  document.getElementById('searchInput').addEventListener('input', filterGroups);
  document.getElementById('selectAll').addEventListener('click', () => setAll(true));
  document.getElementById('clearAll').addEventListener('click', () => setAll(false));
  document.getElementById('refreshGroups').addEventListener('click', () => fetchGroups(true));
  const clearBtn = document.getElementById('clearCacheBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', clearCache);
  }
  const clearLogsBtn = document.getElementById('clearLogsBtn');
  if (clearLogsBtn) {
    clearLogsBtn.addEventListener('click', clearLogs);
  }
  const summaryRefresh = document.getElementById('summaryRefresh');
  if (summaryRefresh) {
    summaryRefresh.addEventListener('click', () => fetchGlobalSummary(true));
  }
  const summarySearch = document.getElementById('summarySearch');
  if (summarySearch) {
    summarySearch.addEventListener('input', applySummaryFilter);
  }
  document.querySelectorAll('.summarySort').forEach(el => {
    el.addEventListener('click', () => sortSummary(el.getAttribute('data-key')));
  });
  document.getElementById('includeChannels').addEventListener('change', (e) => { state.includeChannels = e.target.checked; persistIncludeChannels(); fetchGroups(true); });
  const accountSel = document.getElementById('accountSelect');
  if (accountSel) {
    accountSel.addEventListener('change', (e) => {
      state.account = e.target.value;
      localStorage.setItem('selectedAccount', state.account);
      fetchGroups(true);
      fetchAuthStatus();
      try {
        const inp = document.getElementById('loginPhone');
        if (inp) inp.value = '';
      } catch {}
      (async () => {
        try {
          if (!state.account || !state.token) return;
          const res = await fetch(`/api/login/default-phone?account=${encodeURIComponent(state.account)}`, { headers: { 'X-Admin-Token': state.token } });
          if (!res.ok) return;
          const data = await res.json();
          if (data && data.phone) {
            const inp = document.getElementById('loginPhone');
            if (inp) inp.value = data.phone;
          }
        } catch {}
      })();
    });
  }
  document.getElementById('groupList').addEventListener('change', (e) => {
    if (e.target && e.target.classList.contains('groupCheck')) {
      const id = parseInt(e.target.value);
      if (e.target.checked) state.selectedIds.add(id); else state.selectedIds.delete(id);
      persistSelected();
    }
  });
  document.getElementById('sendBtn').addEventListener('click', () => send('send'));
  document.getElementById('testBtn').addEventListener('click', () => send('test-send'));
  const sendCodeBtn = document.getElementById('sendCodeBtn');
  if (sendCodeBtn) {
    sendCodeBtn.addEventListener('click', sendLoginCode);
  }
  const loginPhoneInput = document.getElementById('loginPhone');
  async function autofillDefaultPhone() {
    try {
      if (!state.account || !state.token) return;
      const res = await fetch(`/api/login/default-phone?account=${encodeURIComponent(state.account)}`, { headers: { 'X-Admin-Token': state.token } });
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.phone && loginPhoneInput) {
        loginPhoneInput.value = data.phone;
      }
    } catch {}
  }
  autofillDefaultPhone();
  const confirmLoginBtn = document.getElementById('confirmLoginBtn');
  if (confirmLoginBtn) {
    confirmLoginBtn.addEventListener('click', submitLoginCode);
  }
  const unlockBtn = document.getElementById('unlockAccount');
  if (unlockBtn) {
    unlockBtn.addEventListener('click', () => {
      setLoginInputsVisible(true);
    });
  }
}

function persistSelected() {
  try { localStorage.setItem('selectedGroupIds', JSON.stringify(Array.from(state.selectedIds))); } catch {}
}

function restoreSelected() {
  try {
    const raw = localStorage.getItem('selectedGroupIds');
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) state.selectedIds = new Set(arr.map(x => parseInt(x)));
    }
  } catch {}
}

function persistIncludeChannels() {
  try { localStorage.setItem('includeChannels', state.includeChannels ? '1' : '0'); } catch {}
}

function restoreIncludeChannels() {
  try {
    const v = localStorage.getItem('includeChannels');
    state.includeChannels = v === '1';
    const el = document.getElementById('includeChannels');
    if (el) el.checked = state.includeChannels;
  } catch {}
}

window.addEventListener('DOMContentLoaded', async () => {
  loadToken();
  updateTokenLockUI(); fetchAccounts().then(() => { fetchAuthStatus(); fetchGroups(); });
  restoreSelected();
  restoreIncludeChannels();
  initAccountCheck();
  bindEvents();
  await fetchAccounts();
  await fetchAuthStatus();
  renderGroupsFromCacheIfAvailable();
  await fetchGroups();
  await fetchLogs();
  await fetchGlobalSummary(true);
  setInterval(fetchGlobalSummary, 60000);
});

function generateRequestId() {
  return 'req_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
}

async function fetchWithRetry(url, options, timeoutMs = 5000, maxRetries = 3) {
  let lastErr;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: controller.signal });
      clearTimeout(timer);
      if (res.status === 429) return res;
      if (!res.ok && attempt < maxRetries) {
        lastErr = new Error('HTTP ' + res.status);
        continue;
      }
      return res;
    } catch (e) {
      clearTimeout(timer);
      lastErr = e;
      if (attempt >= maxRetries) throw e;
    }
  }
  throw lastErr || new Error('网络错误');
}

async function fetchAuthStatus() {
  if (!state.account) return;
  const res = await fetch(`/api/account-status?account=${encodeURIComponent(state.account)}`, { headers: { 'X-Admin-Token': state.token } });
  if (!res.ok) return;
  const data = await res.json();
  const el = document.getElementById('authStatus');
  if (el) {
    el.textContent = data.authorized ? '已授权' : '未授权';
    el.className = data.authorized ? 'status-success' : 'status-warning';
  }
  // Only hide login inputs if the specific account is authorized
  if (data.authorized) {
    setAccountLocked(true);
    setLoginInputsVisible(false);
  } else {
    setAccountLocked(false);
    setLoginInputsVisible(true);
  }
}

async function clearCache() {
  if (!state.token) { alert('请输入令牌并点击“保存”'); return; }
  const acc = encodeURIComponent(state.account || '');
  const url = acc ? `/api/groups/cache/clear?account=${acc}` : '/api/groups/cache/clear';
  const res = await fetch(url, { headers: { 'X-Admin-Token': state.token } });
  if (res.ok) {
    await fetchGroups(true);
    alert('缓存已清除');
  } else {
    alert('清除缓存失败');
  }
}

async function clearLogs() {
  if (!state.token) { alert('请输入令牌并点击“保存”'); return; }
  if (!confirm('确定要清空所有日志吗？此操作不可恢复。')) return;
  try {
    const res = await fetch('/api/logs/clear', {
      method: 'POST',
      headers: { 'X-Admin-Token': state.token }
    });
    if (res.ok) {
      alert('日志已清空');
      fetchLogs();
    } else {
      const d = await res.json();
      alert('清空失败: ' + (d.detail || '未知错误'));
    }
  } catch (e) {
    alert('请求出错: ' + e);
  }
}

async function sendLoginCode() {
  try {
    const phone = document.getElementById('loginPhone').value.trim();
    const forceSms = document.getElementById('forceSms')?.checked || false;
    if (!phone) { alert('请输入手机号'); return; }
    const res = await fetch('/api/login/send-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
      body: JSON.stringify({ account: state.account, phone, force_sms: forceSms })
    });
    if (res.ok) {
      const data = await res.json();
      let msg = '验证码已发送';
      if (data.type === 'app') msg += ' (已发送到Telegram App，请在手机/电脑客户端查看)';
      else if (data.type === 'sms') msg += ' (已发送短信)';
      else if (data.type === 'call') msg += ' (正在拨打电话)';
      alert(msg);
    } else if (res.status === 429) {
      const d = await res.json();
      alert(`发送频率过高，请在 ${d.retry_after || 60} 秒后重试`);
    } else {
      const d = await res.json().catch(() => ({}));
      alert(`发送验证码失败：${d.detail || '未知错误'}`);
    }
  } catch (e) {
    alert('发送请求出错: ' + e.message);
  }
}

async function submitLoginCode() {
  try {
    const phone = document.getElementById('loginPhone').value.trim();
    const code = document.getElementById('loginCode').value.trim();
    const password = document.getElementById('loginPassword').value.trim();
    if (!phone || !code) { alert('请输入手机号与验证码'); return; }
    const res = await fetch('/api/login/submit-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
      body: JSON.stringify({ account: state.account, phone, code, password })
    });
    if (res.ok) {
      alert('登录成功');
      if (state.receiverMode) {
        try {
          const r = await fetch('/api/copy-receiver', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
            body: JSON.stringify({ account: state.account, enabled: true })
          });
          if (r.ok) {
            alert('已设置为文案接收账号');
          }
        } catch {}
        state.receiverMode = false;
      }
      await fetchAccounts(); 
      await fetchAuthStatus();
      setLoginInputsVisible(false);
      await fetchGroups(true);
    } else {
      const d = await res.json().catch(() => ({}));
      alert('登录失败: ' + (d.detail || '未知错误'));
    }
  } catch (e) {
    alert('登录请求出错: ' + e.message);
  }
}

/* Removed redundant updateTokenLockUI definition that was here */
function setAccountLocked(locked) {
  // Logic to lock account UI if needed - currently unused or can be removed
}

function getAccountLocked() {
  return localStorage.getItem('tokenLocked') === '1';
}

function setLoginInputsVisible(visible) {
  const ids = ['loginPhone','loginCode','loginPassword','forceSms','sendCodeBtn','confirmLoginBtn'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const node = el.tagName === 'INPUT' && el.type === 'checkbox' ? el.parentElement : el;
    if (node) node.classList.toggle('hidden', !visible);
  });
  const unlockBtn = document.getElementById('unlockAccount');
  if (unlockBtn) unlockBtn.classList.toggle('hidden', visible);
  
  // Ensure inputs are enabled when visible
  if (visible) {
    ids.forEach(id => {
       const el = document.getElementById(id);
       if (el && el.tagName !== 'BUTTON') el.disabled = false;
    });
  }
}

// --- Account Check Logic ---
let isChecking = false;
let checkAbortController = null;

function initAccountCheck() {
  const modal = document.getElementById('checkModal');
  const btn = document.getElementById('checkAccountsBtn');
  const close = document.getElementById('closeCheckModal');
  const startBtn = document.getElementById('startCheckBtn');
  const stopBtn = document.getElementById('stopCheckBtn');
  const clearBtn = document.getElementById('clearInvalidBtn');
  
  if (!btn) return;
  
  btn.addEventListener('click', () => {
    modal.classList.remove('hidden');
  });
  
  close.addEventListener('click', () => {
    if (isChecking) stopCheck();
    modal.classList.add('hidden');
  });
  
  startBtn.addEventListener('click', startCheck);
  stopBtn.addEventListener('click', stopCheck);
  clearBtn.addEventListener('click', clearInvalidAccounts);
}

function stopCheck() {
  if (checkAbortController) {
    checkAbortController.abort();
    checkAbortController = null;
  }
  isChecking = false;
  updateCheckUI(false);
}

async function startCheck() {
  if (isChecking) return;
  isChecking = true;
  checkAbortController = new AbortController();
  updateCheckUI(true);
  
  const accounts = state.accounts; 
  const tbody = document.getElementById('checkResultsBody');
  tbody.innerHTML = '';
  
  const results = [];
  let processed = 0;
  
  document.getElementById('checkProgress').textContent = `0/${accounts.length}`;
  
  for (const acc of accounts) {
    if (!isChecking) break;
    
    const tr = document.createElement('tr');
    tr.id = `check-row-${acc}`;
    tr.innerHTML = `
      <td>${acc}</td>
      <td><span class="status-warn">检查中...</span></td>
      <td>-</td>
      <td>-</td>
    `;
    tbody.appendChild(tr);
    tr.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    
    try {
      // 创建带超时的 AbortController
      const timeoutController = new AbortController();
      const timeoutId = setTimeout(() => timeoutController.abort(), 20000); // 20秒超时
      
      // 合并用户停止信号和超时信号
      const combinedSignal = checkAbortController.signal;
      checkAbortController.signal.addEventListener('abort', () => timeoutController.abort());
      
      const res = await fetch('/api/accounts/check-single', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
        body: JSON.stringify({ account: acc }),
        signal: timeoutController.signal
      });
      
      clearTimeout(timeoutId);
      const data = await res.json();
      results.push(data);
      updateRow(tr, data);
      
    } catch (e) {
      if (e.name === 'AbortError') {
        // 检查是用户停止还是超时
        if (!checkAbortController.signal.aborted) {
          updateRow(tr, { account: acc, valid: false, status: 'timeout', detail: '请求超时' });
        } else {
          break;
        }
      } else {
        updateRow(tr, { account: acc, valid: false, status: 'network_error', detail: e.message });
      }
    }
    
    processed++;
    document.getElementById('checkProgress').textContent = `${processed}/${accounts.length}`;
  }
  
  isChecking = false;
  updateCheckUI(false);
  
  const hasInvalid = results.some(r => !r.valid && r.status !== 'missing_file');
  const clearBtn = document.getElementById('clearInvalidBtn');
  if (clearBtn) clearBtn.disabled = !hasInvalid;
}

function updateRow(tr, data) {
  let statusHtml = '';
  let detailHtml = '';
  let actionHtml = '';
  
  if (data.valid) {
    statusHtml = '<span class="status-ok">正常</span>';
    detailHtml = `ID: ${data.id || '-'}<br>Phone: ${data.phone || '-'}`;
  } else {
    statusHtml = `<span class="status-error">${data.status}</span>`;
    detailHtml = data.detail || '-';
    if (data.status !== 'missing_file') {
        actionHtml = `<button class="btn btn-danger btn-sm" onclick="deleteAccount('${data.account}')">删除</button>`;
    }
  }
  
  tr.innerHTML = `
    <td>${data.account}</td>
    <td>${statusHtml}</td>
    <td style="font-size:0.75rem; color:var(--text-secondary);">${detailHtml}</td>
    <td>${actionHtml}</td>
  `;
}

function updateCheckUI(checking) {
  const start = document.getElementById('startCheckBtn');
  const stop = document.getElementById('stopCheckBtn');
  if (checking) {
    start.classList.add('hidden');
    stop.classList.remove('hidden');
  } else {
    start.classList.remove('hidden');
    stop.classList.add('hidden');
  }
}

async function deleteAccount(account) {
  if (!confirm(`确定要删除账号 ${account} 的登录信息吗？`)) return;
  
  try {
    const res = await fetch('/api/accounts/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
      body: JSON.stringify({ account })
    });
    const data = await res.json();
    if (data.deleted) {
      const tr = document.getElementById(`check-row-${account}`);
      if (tr) tr.innerHTML = `<td>${account}</td><td class="status-warn">已删除</td><td>-</td><td>-</td>`;
      // Update login UI if current account was deleted
      if (state.account === account) {
          fetchAuthStatus();
      }
    } else {
      alert('删除失败');
    }
  } catch (e) {
    alert('删除出错: ' + e.message);
  }
}

window.deleteAccount = deleteAccount;

async function clearInvalidAccounts() {
  if (!confirm('确定要删除所有检测为失效/封禁的账号吗？此操作不可恢复！')) return;
  
  const rows = document.querySelectorAll('#checkResultsBody tr');
  const tasks = [];
  
  for (const tr of rows) {
    const acc = tr.cells[0].textContent;
    if (tr.querySelector('button')) { 
        tasks.push(
            fetch('/api/accounts/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
                body: JSON.stringify({ account: acc })
            }).then(res => res.json()).then(data => {
                if (data.deleted) {
                    tr.innerHTML = `<td>${acc}</td><td class="status-warn">已删除</td><td>-</td><td>-</td>`;
                }
            })
        );
    }
  }
  
  await Promise.all(tasks);
  alert('清理完成');
  document.getElementById('clearInvalidBtn').disabled = true;
}

// ========== Account Management ==========
function setupAccountManage() {
  const modal = document.getElementById('accountManageModal');
  const openBtn = document.getElementById('accountManageBtn');
  const closeBtn = document.getElementById('closeAccountManageModal');
  const uploadBtn = document.getElementById('uploadSessionBtn');
  const batchJoinBtn = document.getElementById('batchJoinBtn');
  const openCleanupBtn = document.getElementById('openAuthorizedCleanup');
  
  if (!modal || !openBtn) return;
  
  openBtn.addEventListener('click', () => {
    modal.classList.remove('hidden');
    loadAccountList();
  });
  
  closeBtn.addEventListener('click', () => {
    modal.classList.add('hidden');
  });
  
  uploadBtn.addEventListener('click', uploadSessionFiles);
  batchJoinBtn?.addEventListener('click', batchJoinGroups);

  if (openCleanupBtn) {
    openCleanupBtn.addEventListener('click', () => {
      const cleanupModal = document.getElementById('authorizedCleanupModal');
      if (cleanupModal) {
        cleanupModal.classList.remove('hidden');
        loadAuthorizedAccounts();
      }
    });
  }
}

async function loadAuthorizedAccounts() {
  const container = document.getElementById('authorizedAccountsContainer');
  const summaryEl = document.getElementById('authorizedAccountsSummary');
  if (!container) return;
  container.innerHTML = '<p class="text-muted text-sm" style="padding:0.5rem;">加载中...</p>';
  if (summaryEl) summaryEl.textContent = '';
  try {
    const res = await fetch('/api/accounts/authorized-list', { headers: { 'X-Admin-Token': state.token } });
    if (!res.ok) {
      container.innerHTML = '<p class="text-danger text-sm" style="padding:0.5rem;">加载失败</p>';
      return;
    }
    const data = await res.json();
    state.authorizedAccounts = Array.isArray(data) ? data : [];
    renderAuthorizedAccounts();
  } catch (e) {
    container.innerHTML = `<p class="text-danger text-sm" style="padding:0.5rem;">加载失败: ${e.message}</p>`;
  }
}

function renderAuthorizedAccounts() {
  const container = document.getElementById('authorizedAccountsContainer');
  const summaryEl = document.getElementById('authorizedAccountsSummary');
  if (!container) return;
  const list = state.authorizedAccounts || [];
  if (!list.length) {
    container.innerHTML = '<p class="text-muted text-sm" style="padding:0.5rem;">当前没有已授权账号</p>';
    if (summaryEl) summaryEl.textContent = '';
    return;
  }
  let html = '<table style="width:100%; border-collapse:collapse; font-size:0.85rem;"><thead><tr><th style="text-align:left; padding:0.5rem;">账号</th><th style="text-align:left; padding:0.5rem;">状态</th><th style="text-align:right; padding:0.5rem;">操作</th></tr></thead><tbody>';
  list.forEach(item => {
    const statusText = item.has_running_tasks ? '运行中' : (item.authorized ? '已授权' : '未授权');
    const statusClass = item.has_running_tasks ? 'status-warning' : (item.authorized ? 'status-success' : 'text-muted');
    const extra = item.running_tasks ? ` (${item.running_tasks} 个任务)` : '';
    html += `<tr data-account="${item.account}">
      <td style="padding:0.5rem;">${item.account}</td>
      <td style="padding:0.5rem;" class="${statusClass}">${statusText}${extra}</td>
      <td style="padding:0.5rem; text-align:right;">
        <button class="btn btn-danger btn-sm authorized-delete-btn" data-account="${item.account}">删除</button>
      </td>
    </tr>`;
  });
  html += '</tbody></table>';
  container.innerHTML = html;
  if (summaryEl) summaryEl.textContent = `共 ${list.length} 个账号`;
  const deleteButtons = container.querySelectorAll('.authorized-delete-btn');
  deleteButtons.forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const acc = e.currentTarget.getAttribute('data-account');
      await deleteAuthorizedAccount(acc);
    });
  });
}

async function deleteAuthorizedAccount(account) {
  if (!account) return;
  if (!confirm(`确定要删除账号 ${account} 的会话吗？运行中的任务将被停止，此操作不可恢复。`)) return;
  try {
    const res = await fetch('/api/accounts/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
      body: JSON.stringify({ accounts: [account] }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(`删除失败: ${data.detail || '未知错误'}`);
    } else {
      const result = Array.isArray(data.results) && data.results[0] ? data.results[0] : null;
      if (!result || !result.deleted) {
        const msg = (result && result.error) || data.detail || '删除失败';
        alert(msg);
      }
    }
  } catch (e) {
    alert(`删除失败: ${e.message}`);
  }
  await loadAuthorizedAccounts();
  await fetchAccounts();
  await fetchAuthStatus();
}

async function deleteAllAuthorizedAccounts() {
  const list = state.authorizedAccounts || [];
  if (!list.length) {
    alert('当前没有可删除的账号');
    return;
  }
  if (!confirm('确定要清空所有已授权账号吗？所有对应会话将被删除，运行中的任务会被停止，此操作不可恢复。')) return;
  const names = list.map(x => x.account).filter(Boolean);
  try {
    const res = await fetch('/api/accounts/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
      body: JSON.stringify({ accounts: names }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(`清空失败: ${data.detail || '未知错误'}`);
    } else {
      alert('清空完成');
    }
  } catch (e) {
    alert(`清空失败: ${e.message}`);
  }
  await loadAuthorizedAccounts();
  await fetchAccounts();
  await fetchAuthStatus();
}

async function loadAccountList() {
  const container = document.getElementById('accountListContainer');
  const countEl = document.getElementById('accountCount');
  
  container.innerHTML = '<p class="text-muted text-sm">加载中...</p>';
  
  try {
    // Use the accounts from state
    const accounts = state.accounts || [];
    countEl.textContent = accounts.length;
    
    if (accounts.length === 0) {
      container.innerHTML = '<p class="text-muted text-sm">暂无账号</p>';
      return;
    }
    
    let html = '<div style="display:flex; flex-wrap:wrap; gap:0.5rem;">';
    for (const acc of accounts) {
      html += `<span style="background:var(--bg-secondary); padding:0.25rem 0.5rem; border-radius:4px; font-size:0.8rem;">${acc}</span>`;
    }
    html += '</div>';
    container.innerHTML = html;
    
  } catch (e) {
    container.innerHTML = `<p class="text-danger text-sm">加载失败: ${e.message}</p>`;
  }
}

async function uploadSessionFiles() {
  const fileInput = document.getElementById('sessionFileInput');
  const statusEl = document.getElementById('uploadStatus');
  
  if (!fileInput.files || fileInput.files.length === 0) {
    statusEl.textContent = '请选择文件';
    return;
  }
  
  statusEl.textContent = '上传中...';
  
  const formData = new FormData();
  for (const file of fileInput.files) {
    formData.append('files', file);
  }
  
  try {
    const res = await fetch('/api/accounts/upload-sessions', {
      method: 'POST',
      headers: { 'X-Admin-Token': state.token },
      body: formData
    });
    
    const data = await res.json();
    
    if (res.ok) {
      statusEl.textContent = `上传成功: ${data.uploaded || 0} 个文件`;
      fileInput.value = '';
      loadAccountList();
      fetchAuthStatus(); // Refresh account list
    } else {
      statusEl.textContent = `上传失败: ${data.detail || '未知错误'}`;
    }
  } catch (e) {
    statusEl.textContent = `上传出错: ${e.message}`;
  }
}

async function batchJoinGroups() {
  const linksInput = document.getElementById('joinLinksInput');
  const modeSelect = document.getElementById('joinMode');
  const delayInput = document.getElementById('joinDelayMs');
  const statusEl = document.getElementById('joinStatus');
  const btn = document.getElementById('batchJoinBtn');
  
  // 解析链接
  const linksText = linksInput?.value?.trim();
  if (!linksText) {
    statusEl.textContent = '请输入邀请链接';
    return;
  }
  
  const links = linksText.split('\n')
    .map(l => l.trim())
    .filter(l => l && (l.includes('t.me') || l.startsWith('@')));
  
  if (links.length === 0) {
    statusEl.textContent = '未找到有效链接';
    return;
  }
  
  const mode = modeSelect?.value || 'all';
  const delayMs = parseInt(delayInput?.value) || 3000;
  
  // 确认
  const modeText = mode === 'all' ? '所有账号加入每个群' : '分配账号到不同群';
  if (!confirm(`确定要批量加入 ${links.length} 个群组吗？\n\n模式: ${modeText}\n延迟: ${delayMs}ms`)) {
    return;
  }
  
  btn.disabled = true;
  btn.textContent = '加入中...';
  statusEl.textContent = '正在批量加入群组...';
  
  try {
    let endpoint, body;
    
    if (mode === 'all') {
      // 所有账号加入每个群 - 需要逐个群调用
      let successCount = 0;
      let failCount = 0;
      
      for (let i = 0; i < links.length; i++) {
        const link = links[i];
        statusEl.textContent = `正在加入 ${i + 1}/${links.length}: ${link.substring(0, 30)}...`;
        
        try {
          const res = await fetch('/api/groups/join-all-accounts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
            body: JSON.stringify({ invite_link: link, delay_ms: delayMs })
          });
          
          const data = await res.json();
          if (data.summary) {
            successCount += data.summary.success || 0;
            failCount += data.summary.failed || 0;
          }
        } catch (e) {
          failCount++;
        }
        
        // 群组之间延迟
        if (i < links.length - 1) {
          await new Promise(r => setTimeout(r, delayMs));
        }
      }
      statusEl.textContent = `✅ 完成! 成功: ${successCount}, 失败: ${failCount}`;
      statusEl.style.color = 'var(--success-color)';
      
    } else {
      // 分配模式 - 使用 join-batch API
      const res = await fetch('/api/groups/join-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token },
        body: JSON.stringify({ invite_links: links, delay_ms: delayMs })
      });
      
      const data = await res.json();
      
      if (res.ok && data.summary) {
        statusEl.textContent = `✅ 完成! 成功: ${data.summary.success}, 已加入: ${data.summary.already_joined}, 失败: ${data.summary.failed}`;
        statusEl.style.color = 'var(--success-color)';
      } else {
        statusEl.textContent = `❌ ${data.detail || '加入失败'}`;
        statusEl.style.color = 'var(--danger-color)';
      }
    }
    
  } catch (e) {
    statusEl.textContent = `❌ 请求出错: ${e.message}`;
    statusEl.style.color = 'var(--danger-color)';
  } finally {
    btn.disabled = false;
    btn.textContent = '开始加入';
  }
}

// ========== Reset System ==========
function setupResetSystem() {
  const btn = document.getElementById('resetSystemBtn');
  if (!btn) return;
  
  btn.addEventListener('click', async () => {
    const confirmText = prompt('此操作将清除所有任务和日志数据！\n\n如果同时要删除所有账号登录信息，请输入 "DELETE_ALL"\n否则直接点击确定只清除任务数据:');
    
    if (confirmText === null) return; // Cancelled
    
    const resetSessions = confirmText === 'DELETE_ALL';
    
    try {
      const res = await fetch(`/api/system/reset?sessions=${resetSessions}`, {
        method: 'POST',
        headers: { 'X-Admin-Token': state.token }
      });
      
      const data = await res.json();
      
      if (res.ok) {
        alert(`重置成功！${resetSessions ? `\n删除了 ${data.deleted_sessions || 0} 个账号` : ''}`);
        location.reload();
      } else {
        alert(`重置失败: ${data.detail || '未知错误'}`);
      }
    } catch (e) {
      alert(`重置出错: ${e.message}`);
    }
  });
}

// ========== Batch Send ==========
function setupBatchSend() {
  const btn = document.getElementById('batchSendBtn');
  if (!btn) return;
  
  btn.addEventListener('click', startBatchSend);
}

async function startBatchSend() {
  const btn = document.getElementById('batchSendBtn');
  const resultEl = document.getElementById('result');
  
  // 获取选中的群组
  const checkboxes = document.querySelectorAll('#groupList input[type="checkbox"]:checked');
  const groupIds = Array.from(checkboxes).map(cb => parseInt(cb.value));
  
  if (groupIds.length === 0) {
    alert('请先选择要发送的群组');
    return;
  }
  
  // 获取消息内容
  const message = document.getElementById('message')?.value?.trim();
  if (!message) {
    alert('请输入消息内容');
    return;
  }
  
  // 获取发送参数
  const parseMode = document.getElementById('parseMode')?.value || 'plain';
  const delayMs = parseInt(document.getElementById('delayMs')?.value) || 11000;
  const rounds = parseInt(document.getElementById('rounds')?.value) || 100;
  const roundIntervalS = parseInt(document.getElementById('roundInterval')?.value) || 600;
  const disablePreview = document.getElementById('disablePreview')?.checked ?? true;
  
  // 确认
  if (!confirm(`确定要使用所有已授权账号批量发送到 ${groupIds.length} 个群组吗？\n\n每条间隔: ${delayMs}ms\n发送轮数: ${rounds}\n每轮间隔: ${roundIntervalS}s`)) {
    return;
  }
  
  btn.disabled = true;
  btn.textContent = '批量创建任务中...';
  resultEl.textContent = '正在创建批量任务...';
  
  try {
    const res = await fetch('/api/send-async-batch', {
      method: 'POST',
      headers: { 
        'Content-Type': 'application/json', 
        'X-Admin-Token': state.token 
      },
      body: JSON.stringify({
        group_ids: groupIds,
        message: message,
        parse_mode: parseMode,
        disable_web_page_preview: disablePreview,
        delay_ms: delayMs,
        rounds: rounds,
        round_interval_s: roundIntervalS,
        request_id: `batch_${Date.now()}`
      })
    });
    
    const data = await res.json();
    
    if (res.ok) {
      const taskCount = data.tasks?.length || data.accounts_count || 0;
      resultEl.textContent = `✅ 已创建 ${taskCount} 个任务`;
      resultEl.style.color = 'var(--success-color)';
      
      // 刷新任务列表
      if (typeof fetchSummary === 'function') {
        fetchSummary();
      }
    } else {
      resultEl.textContent = `❌ ${data.detail || '创建失败'}`;
      resultEl.style.color = 'var(--danger-color)';
    }
  } catch (e) {
    resultEl.textContent = `❌ 请求出错: ${e.message}`;
    resultEl.style.color = 'var(--danger-color)';
  } finally {
    btn.disabled = false;
    btn.textContent = '批量开始群发 (已授权账号)';
  }
}

// Initialize new features
document.addEventListener('DOMContentLoaded', () => {
  setupAccountManage();
  setupResetSystem();
  setupBatchSend();
  setupLoginTabs();
  setupSessionUpload();
  setupProtocolManager();
  const closeAuthorizedModal = document.getElementById('closeAuthorizedCleanupModal');
  const authorizedModal = document.getElementById('authorizedCleanupModal');
  const deleteAllAuthorizedBtn = document.getElementById('deleteAllAuthorizedBtn');
  if (closeAuthorizedModal && authorizedModal) {
    closeAuthorizedModal.addEventListener('click', () => {
      authorizedModal.classList.add('hidden');
    });
    authorizedModal.addEventListener('click', (e) => {
      if (e.target === authorizedModal) authorizedModal.classList.add('hidden');
    });
  }
  if (deleteAllAuthorizedBtn) {
    deleteAllAuthorizedBtn.addEventListener('click', deleteAllAuthorizedAccounts);
  }
});

// ========== Login Tabs ==========
function setupLoginTabs() {
  const tabCode = document.getElementById('tabLoginCode');
  const tabSession = document.getElementById('tabLoginSession');
  const codePanel = document.getElementById('loginCodePanel');
  const sessionPanel = document.getElementById('loginSessionPanel');
  
  if (!tabCode || !tabSession) return;
  
  tabCode.addEventListener('click', () => {
    tabCode.classList.add('active');
    tabSession.classList.remove('active');
    tabCode.style.borderBottom = '2px solid var(--primary-color)';
    tabSession.style.borderBottom = '2px solid transparent';
    codePanel.style.display = 'block';
    sessionPanel.style.display = 'none';
  });
  
  tabSession.addEventListener('click', () => {
    tabSession.classList.add('active');
    tabCode.classList.remove('active');
    tabSession.style.borderBottom = '2px solid var(--primary-color)';
    tabCode.style.borderBottom = '2px solid transparent';
    sessionPanel.style.display = 'block';
    codePanel.style.display = 'none';
  });
  
  // 初始化状态
  tabCode.style.borderBottom = '2px solid var(--primary-color)';
}

// ========== Session Upload ==========
function setupSessionUpload() {
  const uploadBtn = document.getElementById('uploadSessionFileBtn');
  const joinBtn = document.getElementById('joinGroupsBtn');
  
  if (uploadBtn) {
    uploadBtn.addEventListener('click', uploadSessionFile);
  }
  
  if (joinBtn) {
    joinBtn.addEventListener('click', joinGroupsFromLogin);
  }
}

async function uploadSessionFile() {
  const fileInput = document.getElementById('sessionFileUpload');
  const statusEl = document.getElementById('sessionStatus');
  const accountSelect = document.getElementById('accountSelect');
  
  if (!fileInput.files || fileInput.files.length === 0) {
    statusEl.textContent = '请选择 session 文件';
    statusEl.style.color = 'var(--danger-color)';
    return;
  }
  
  const currentAccount = accountSelect?.value;
  if (!currentAccount) {
    statusEl.textContent = '请先选择一个账号';
    statusEl.style.color = 'var(--danger-color)';
    return;
  }
  
  const file = fileInput.files[0];
  
  // 检查文件名是否与当前账号匹配
  const fileName = file.name.replace('.session', '');
  if (fileName !== currentAccount) {
    if (!confirm(`文件名 "${fileName}" 与当前账号 "${currentAccount}" 不匹配。\n确定要上传吗？\n\n注意：文件将被重命名为 ${currentAccount}.session`)) {
      return;
    }
  }
  
  statusEl.textContent = '上传中...';
  statusEl.style.color = 'var(--text-muted)';
  
  const formData = new FormData();
  
  // 创建新的 File 对象，使用当前账号名
  const renamedFile = new File([file], `${currentAccount}.session`, { type: file.type });
  formData.append('files', renamedFile);
  
  try {
    const res = await fetch('/api/accounts/upload-sessions', {
      method: 'POST',
      headers: { 'X-Admin-Token': state.token },
      body: formData
    });
    
    const data = await res.json();
    
    if (res.ok) {
      if (data.validated > 0) {
        statusEl.textContent = `✅ 上传成功！已验证 ${data.validated} 个账号`;
        statusEl.style.color = 'var(--success-color)';
        fileInput.value = '';
        
        // 显示详细信息
        if (data.validated_accounts && data.validated_accounts.length > 0) {
          statusEl.textContent += `\n已验证账号: ${data.validated_accounts.join(', ')}`;
        }
        
        if (data.errors && data.errors.length > 0) {
          statusEl.textContent += `\n\n警告:\n${data.errors.join('\n')}`;
          statusEl.style.color = 'orange';
        }
        
        // 刷新账号列表
        setTimeout(() => {
          fetchAuthStatus();
          // 自动选择刚上传的账号
          if (accountSelect) {
            accountSelect.value = currentAccount;
          }
          // 刷新群组列表
          const refreshBtn = document.getElementById('refreshGroups');
          if (refreshBtn) refreshBtn.click();
        }, 1500);
      } else {
        statusEl.textContent = `⚠️ 文件已上传，但验证失败\n${data.errors ? data.errors.join('\n') : ''}`;
        statusEl.style.color = 'orange';
      }
    } else {
      statusEl.textContent = `❌ 上传失败: ${data.detail || '未知错误'}`;
      statusEl.style.color = 'var(--danger-color)';
    }
  } catch (e) {
    statusEl.textContent = `❌ 上传出错: ${e.message}`;
    statusEl.style.color = 'var(--danger-color)';
  }
}

async function joinGroupsFromLogin() {
  const linksInput = document.getElementById('joinGroupLinks');
  const statusEl = document.getElementById('sessionStatus');
  const accountSelect = document.getElementById('accountSelect');
  
  const currentAccount = accountSelect?.value;
  if (!currentAccount) {
    statusEl.textContent = '请先选择一个账号';
    statusEl.style.color = 'var(--danger-color)';
    return;
  }
  
  const linksText = linksInput?.value?.trim();
  if (!linksText) {
    statusEl.textContent = '请输入群组链接';
    statusEl.style.color = 'var(--danger-color)';
    return;
  }
  
  const links = linksText.split('\n')
    .map(l => l.trim())
    .filter(l => l && (l.includes('t.me') || l.startsWith('@')));
  
  if (links.length === 0) {
    statusEl.textContent = '未找到有效链接';
    statusEl.style.color = 'var(--danger-color)';
    return;
  }
  
  statusEl.textContent = `正在加入 ${links.length} 个群组...`;
  statusEl.style.color = 'var(--text-muted)';
  
  let successCount = 0;
  let failCount = 0;
  
  for (let i = 0; i < links.length; i++) {
    const link = links[i];
    statusEl.textContent = `正在加入 ${i + 1}/${links.length}: ${link.substring(0, 20)}...`;
    
    try {
      const res = await fetch('/api/groups/join', {
        method: 'POST',
        headers: { 
          'Content-Type': 'application/json',
          'X-Admin-Token': state.token 
        },
        body: JSON.stringify({ 
          account: currentAccount, 
          invite_link: link 
        })
      });
      
      const data = await res.json();
      if (data.ok || data.already_joined) {
        successCount++;
      } else {
        failCount++;
      }
    } catch (e) {
      failCount++;
    }
    
    // 延迟避免触发风控
    if (i < links.length - 1) {
      await new Promise(r => setTimeout(r, 3000));
    }
  }
  
  statusEl.textContent = `✅ 完成！成功: ${successCount}, 失败: ${failCount}`;
  statusEl.style.color = 'var(--success-color)';
  
  // 清空输入框
  linksInput.value = '';
  
  // 刷新群组列表
  setTimeout(() => {
    const refreshBtn = document.getElementById('refreshGroups');
    if (refreshBtn) refreshBtn.click();
  }, 1000);
}

// ========== Protocol Manager ==========
let protocolAccounts = []; // 存储上传的协议号列表
let protocolValidAccounts = [];

function setupProtocolManager() {
  const openBtn = document.getElementById('openProtocolManager');
  const modal = document.getElementById('protocolManagerModal');
  const closeBtn = document.getElementById('closeProtocolManager');
  const uploadBtn = document.getElementById('uploadProtocolsBtn');
  const verifyBtn = document.getElementById('verifyProtocolsBtn');
  const joinBtn = document.getElementById('batchJoinProtocolBtn');
  const assignBtn = document.getElementById('assignProtocolsBtn');
  
  if (!openBtn || !modal) return;
  
  openBtn.addEventListener('click', () => {
    modal.classList.remove('hidden');
    protocolAccounts = [];
    protocolValidAccounts = [];
    if (assignBtn) assignBtn.disabled = true;
  });
  
  closeBtn.addEventListener('click', () => {
    modal.classList.add('hidden');
  });
  
  uploadBtn?.addEventListener('click', uploadProtocolFiles);
  verifyBtn?.addEventListener('click', verifyProtocolAccounts);
  joinBtn?.addEventListener('click', batchJoinWithProtocols);
  assignBtn?.addEventListener('click', assignProtocolAccountsToSequence);
}

async function uploadProtocolFiles() {
  const fileInput = document.getElementById('protocolFiles');
  const progressEl = document.getElementById('uploadProgress');
  const btn = document.getElementById('uploadProtocolsBtn');
  
  if (!fileInput.files || fileInput.files.length === 0) {
    progressEl.textContent = '❌ 请选择文件';
    progressEl.style.color = 'var(--danger-color)';
    return;
  }
  
  btn.disabled = true;
  btn.textContent = '上传中...';
  progressEl.textContent = `正在上传 ${fileInput.files.length} 个文件...`;
  progressEl.style.color = 'var(--text-muted)';
  
  const formData = new FormData();
  protocolAccounts = [];
  
  for (const file of fileInput.files) {
    formData.append('files', file);
    const accountName = file.name.replace('.session', '');
    protocolAccounts.push(accountName);
  }
  
  try {
    const res = await fetch('/api/accounts/upload-sessions', {
      method: 'POST',
      headers: { 'X-Admin-Token': state.token },
      body: formData
    });
    
    const data = await res.json();
    
    if (res.ok) {
      progressEl.textContent = `✅ 成功上传 ${data.uploaded} 个文件\n\n账号列表:\n${protocolAccounts.join('\n')}`;
      progressEl.style.color = 'var(--success-color)';
      
      if (data.errors && data.errors.length > 0) {
        progressEl.textContent += `\n\n⚠️ 警告:\n${data.errors.join('\n')}`;
        progressEl.style.color = 'orange';
      }
      
      fileInput.value = '';
      try { await fetchAccounts(); } catch {}
    } else {
      progressEl.textContent = `❌ 上传失败: ${data.detail || '未知错误'}`;
      progressEl.style.color = 'var(--danger-color)';
    }
  } catch (e) {
    progressEl.textContent = `❌ 上传出错: ${e.message}`;
    progressEl.style.color = 'var(--danger-color)';
  } finally {
    btn.disabled = false;
    btn.textContent = '上传全部';
  }
}

async function verifyProtocolAccounts() {
  const listEl = document.getElementById('protocolStatusList');
  const btn = document.getElementById('verifyProtocolsBtn');
  const assignBtn = document.getElementById('assignProtocolsBtn');
  const assignProgress = document.getElementById('assignProgress');
  
  if (protocolAccounts.length === 0) {
    listEl.innerHTML = '<p class="text-danger">❌ 请先上传协议号文件</p>';
    return;
  }
  
  btn.disabled = true;
  btn.textContent = '验证中...';
  listEl.innerHTML = '<p class="text-muted">正在验证账号状态...</p>';
  
  let html = '<table style="width:100%; border-collapse: collapse; font-size:0.85rem;">';
  html += '<thead><tr style="background:var(--bg-secondary);"><th style="padding:0.5rem; text-align:left;">账号</th><th style="padding:0.5rem;">状态</th><th style="padding:0.5rem;">详情</th></tr></thead><tbody>';
  
  let validCount = 0;
  let invalidCount = 0;
  protocolValidAccounts = [];
  
  for (const account of protocolAccounts) {
    try {
      const res = await fetch('/api/accounts/check-single', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Token': state.token
        },
        body: JSON.stringify({ account })
      });
      
      const data = await res.json();
      
      let statusIcon, statusText, statusColor;
      if (data.valid) {
        statusIcon = '✅';
        statusText = '可用';
        statusColor = 'var(--success-color)';
        validCount++;
        protocolValidAccounts.push(account);
      } else {
        statusIcon = '❌';
        statusText = '不可用';
        statusColor = 'var(--danger-color)';
        invalidCount++;
      }
      
      const detail = data.detail || data.status || '-';
      html += `<tr style="border-bottom:1px solid var(--border-color);">
        <td style="padding:0.5rem;">${account}</td>
        <td style="padding:0.5rem; text-align:center; color:${statusColor};">${statusIcon} ${statusText}</td>
        <td style="padding:0.5rem; font-size:0.75rem; color:var(--text-muted);">${detail}</td>
      </tr>`;
      
    } catch (e) {
      html += `<tr style="border-bottom:1px solid var(--border-color);">
        <td style="padding:0.5rem;">${account}</td>
        <td style="padding:0.5rem; text-align:center; color:var(--danger-color);">❌ 错误</td>
        <td style="padding:0.5rem; font-size:0.75rem; color:var(--text-muted);">${e.message}</td>
      </tr>`;
      invalidCount++;
    }
  }
  
  html += '</tbody></table>';
  html += `<div style="margin-top:1rem; padding:0.75rem; background:var(--bg-secondary); border-radius:4px; text-align:center;">
    <strong>✅ 可用: ${validCount}</strong> | <strong style="color:var(--danger-color);">❌ 不可用: ${invalidCount}</strong>
  </div>`;
  
  listEl.innerHTML = html;
  btn.disabled = false;
  btn.textContent = '重新验证';
  try { await fetchAccounts(); } catch {}
  if (assignProgress) assignProgress.textContent = '';
  if (assignBtn) assignBtn.disabled = protocolValidAccounts.length === 0;
}

async function assignProtocolAccountsToSequence() {
  const btn = document.getElementById('assignProtocolsBtn');
  const progressEl = document.getElementById('assignProgress');
  if (!btn || !progressEl) return;
  if (protocolValidAccounts.length === 0) {
    progressEl.textContent = '❌ 没有可加入的账号，请先验证';
    progressEl.style.color = 'var(--danger-color)';
    return;
  }
  if (!confirm(`确定要把 ${protocolValidAccounts.length} 个验证成功的账号加入账号序列吗？\n\n系统会按顺序填充空的 account_XX 槽位，不会覆盖已有账号。`)) {
    return;
  }
  btn.disabled = true;
  progressEl.textContent = '处理中...';
  progressEl.style.color = 'var(--text-muted)';
  try {
    const res = await fetch('/api/accounts/assign-sequence', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-Token': state.token
      },
      body: JSON.stringify({ accounts: protocolValidAccounts })
    });
    const data = await res.json();
    if (!res.ok) {
      progressEl.textContent = `❌ 操作失败: ${data.detail || '未知错误'}`;
      progressEl.style.color = 'var(--danger-color)';
      return;
    }
    const assigned = data.assigned || [];
    const skipped = data.skipped || [];
    const errors = data.errors || [];
    let text = `✅ 已加入账号序列: ${assigned.length}`;
    if (assigned.length) {
      text += `\n\n映射:\n` + assigned.map(x => `${x.from} → ${x.to}`).join('\n');
    }
    if (skipped.length) {
      text += `\n\n⚠️ 未分配(槽位不足或被跳过):\n` + skipped.join('\n');
    }
    if (errors.length) {
      text += `\n\n⚠️ 错误:\n` + errors.join('\n');
    }
    progressEl.textContent = text;
    progressEl.style.color = assigned.length ? 'var(--success-color)' : 'orange';
    try { await fetchAccounts(); } catch {}
  } catch (e) {
    progressEl.textContent = `❌ 请求失败: ${e.message}`;
    progressEl.style.color = 'var(--danger-color)';
  } finally {
    btn.disabled = false;
  }
}

async function batchJoinWithProtocols() {
  const linksInput = document.getElementById('protocolGroupLinks');
  const delayInput = document.getElementById('protocolDelay');
  const groupDelayInput = document.getElementById('protocolGroupDelay');
  const btn = document.getElementById('batchJoinProtocolBtn');
  const progressBar = document.getElementById('joinProgressBar');
  const progressFill = document.getElementById('joinProgressFill');
  const progressText = document.getElementById('joinProgressText');
  const resultsEl = document.getElementById('joinResultsList');
  
  if (protocolAccounts.length === 0) {
    resultsEl.innerHTML = '<p class="text-danger">❌ 请先上传并验证协议号</p>';
    return;
  }
  
  const linksText = linksInput?.value?.trim();
  if (!linksText) {
    resultsEl.innerHTML = '<p class="text-danger">❌ 请输入群组链接</p>';
    return;
  }
  
  const links = linksText.split('\n')
    .map(l => l.trim())
    .filter(l => l && (l.includes('t.me') || l.startsWith('@')));
  
  if (links.length === 0) {
    resultsEl.innerHTML = '<p class="text-danger">❌ 未找到有效链接</p>';
    return;
  }
  
  const accountDelay = parseInt(delayInput?.value) || 5;
  const groupDelay = parseInt(groupDelayInput?.value) || 3;
  
  if (!confirm(`确定要使用 ${protocolAccounts.length} 个协议号加入 ${links.length} 个群组吗？\n\n账号间隔: ${accountDelay}秒\n群组间隔: ${groupDelay}秒`)) {
    return;
  }
  
  btn.disabled = true;
  btn.textContent = '加入中...';
  progressBar.style.display = 'block';
  resultsEl.innerHTML = '';
  
  let totalTasks = protocolAccounts.length * links.length;
  let completedTasks = 0;
  let successCount = 0;
  let failCount = 0;
  
  for (let i = 0; i < links.length; i++) {
    const link = links[i];
    
    progressText.textContent = `正在加入第 ${i + 1}/${links.length} 个群组...`;
    
    for (let j = 0; j < protocolAccounts.length; j++) {
      const account = protocolAccounts[j];
      
      progressText.textContent = `群组 ${i + 1}/${links.length} | 账号 ${j + 1}/${protocolAccounts.length}`;
      
      try {
        const res = await fetch('/api/groups/join', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Admin-Token': state.token
          },
          body: JSON.stringify({ account, invite_link: link })
        });
        
        const data = await res.json();
        
        let resultIcon, resultText, resultColor;
        if (data.ok || data.already_joined) {
          resultIcon = '✅';
          resultText = data.already_joined ? '已加入' : '成功';
          resultColor = 'var(--success-color)';
          successCount++;
        } else {
          resultIcon = '❌';
          resultText = data.error || '失败';
          resultColor = 'var(--danger-color)';
          failCount++;
        }
        
        resultsEl.innerHTML += `<div style="padding:0.25rem; border-bottom:1px solid var(--border-color); color:${resultColor};">
          ${resultIcon} <strong>${account}</strong> → ${link.substring(0, 30)}... : ${resultText}
        </div>`;
        resultsEl.scrollTop = resultsEl.scrollHeight;
        
      } catch (e) {
        resultsEl.innerHTML += `<div style="padding:0.25rem; border-bottom:1px solid var(--border-color); color:var(--danger-color);">
          ❌ <strong>${account}</strong> → ${link.substring(0, 30)}... : 错误 - ${e.message}
        </div>`;
        failCount++;
      }
      
      completedTasks++;
      const progress = (completedTasks / totalTasks) * 100;
      progressFill.style.width = `${progress}%`;
      
      // 账号间延迟（最后一个账号不延迟）
      if (j < protocolAccounts.length - 1) {
        await new Promise(r => setTimeout(r, accountDelay * 1000));
      }
    }
    
    // 群组间延迟（最后一个群组不延迟）
    if (i < links.length - 1) {
      progressText.textContent = `等待 ${groupDelay} 秒后加入下一个群组...`;
      await new Promise(r => setTimeout(r, groupDelay * 1000));
    }
  }
  
  progressText.textContent = `✅ 全部完成！成功: ${successCount}, 失败: ${failCount}`;
  progressFill.style.width = '100%';
  btn.disabled = false;
  btn.textContent = '🚀 开始批量加入';
}
