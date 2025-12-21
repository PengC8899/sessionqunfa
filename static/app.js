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
  const rounds = parseInt(document.getElementById('rounds')?.value || '30');
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
      loginPopover.classList.toggle('active');
    });
    document.addEventListener('click', (e) => {
      if (!loginPopover.contains(e.target) && e.target !== toggleLoginBtn) {
        loginPopover.classList.remove('active');
      }
    });
    loginPopover.addEventListener('click', (e) => e.stopPropagation());
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
