(function () {
  const PAGE_DEFAULT = "dashboard";
  const POLL_INTERVAL_MS = 30000;
  const state = {
    activePage: PAGE_DEFAULT,
    timer: null,
    toastTimer: null,
  };

  function getToken() {
    return localStorage.getItem("adminToken") || "";
  }

  function getHeaders() {
    const token = getToken();
    return token ? { "X-Admin-Token": token } : {};
  }

  async function fetchJson(url) {
    const res = await fetch(url, { headers: getHeaders() });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return res.json();
  }

  function formatTime(value) {
    if (!value) return "--";
    try {
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) return String(value);
      return dt.toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    } catch (_) {
      return String(value);
    }
  }

  function updateText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function readText(id) {
    const el = document.getElementById(id);
    return el ? el.textContent || "" : "";
  }

  function ratio(success, total) {
    if (!total) return "0%";
    return `${Math.round((success / total) * 100)}%`;
  }

  function groupByHour(logs) {
    const map = new Map();
    logs.forEach((row) => {
      const dt = row.created_at ? new Date(row.created_at) : null;
      if (!dt || Number.isNaN(dt.getTime())) return;
      const key = `${String(dt.getHours()).padStart(2, "0")}:00`;
      const prev = map.get(key) || { label: key, success: 0, failed: 0 };
      if ((row.status || "").toLowerCase() === "success") prev.success += 1;
      else if ((row.status || "").toLowerCase() === "failed") prev.failed += 1;
      map.set(key, prev);
    });
    return Array.from(map.values()).sort((a, b) => a.label.localeCompare(b.label)).slice(-8);
  }

  function renderTrendChart(logs) {
    const svg = document.getElementById("dashboardTrendSvg");
    if (!svg) return;
    const points = groupByHour(logs);
    if (!points.length) {
      svg.innerHTML = "";
      return;
    }

    const width = 640;
    const height = 240;
    const paddingX = 30;
    const paddingY = 22;
    const maxValue = Math.max(1, ...points.map((item) => Math.max(item.success, item.failed)));
    const stepX = points.length > 1 ? (width - paddingX * 2) / (points.length - 1) : 0;
    const toY = (value) => height - paddingY - ((height - paddingY * 2) * value) / maxValue;
    const successLine = points.map((item, index) => `${paddingX + stepX * index},${toY(item.success)}`).join(" ");
    const failedLine = points.map((item, index) => `${paddingX + stepX * index},${toY(item.failed)}`).join(" ");

    const labelNodes = points.map((item, index) => {
      const x = paddingX + stepX * index;
      return `<text x="${x}" y="${height - 6}" fill="rgba(159,180,211,0.68)" font-size="10" text-anchor="middle">${item.label}</text>`;
    }).join("");

    const grid = [0, 0.33, 0.66, 1].map((step) => {
      const y = paddingY + (height - paddingY * 2) * step;
      return `<line x1="${paddingX}" y1="${y}" x2="${width - paddingX}" y2="${y}" stroke="rgba(135,164,205,0.12)" stroke-dasharray="4 6" />`;
    }).join("");

    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.innerHTML = `
      <defs>
        <linearGradient id="trendGlow" x1="0%" x2="100%">
          <stop offset="0%" stop-color="rgba(59,188,255,0.0)" />
          <stop offset="100%" stop-color="rgba(59,188,255,0.26)" />
        </linearGradient>
      </defs>
      ${grid}
      <polyline points="${successLine}" fill="none" stroke="#3bbcff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
      <polyline points="${failedLine}" fill="none" stroke="#ff7f7f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"></polyline>
      ${labelNodes}
    `;
  }

  function renderRecentLogs(logs) {
    const body = document.getElementById("dashboardRecentLogsBody");
    if (!body) return;
    const rows = logs.slice(0, 6).map((row) => {
      const status = (row.status || "").toLowerCase();
      const pillClass = status === "success" ? "success" : status === "failed" ? "danger" : "info";
      return `
        <tr>
          <td>${formatTime(row.created_at)}</td>
          <td>${row.account_name || "--"}</td>
          <td>${row.group_title || row.group_id || "--"}</td>
          <td><span class="status-pill ${pillClass}">${row.status || "INFO"}</span></td>
        </tr>
      `;
    }).join("");
    body.innerHTML = rows || `
      <tr>
        <td colspan="4">
          <div class="empty-panel">暂无日志数据，任务开始后会在这里实时更新。</div>
        </td>
      </tr>
    `;
  }

  function renderDrawer(logs, summary) {
    const alerts = document.getElementById("drawerAlerts");
    if (!alerts) return;
    const failedLogs = logs.filter((row) => (row.status || "").toLowerCase() === "failed").slice(0, 4);
    const taskAlerts = summary.filter((row) => row.status && row.status !== "running").slice(0, 2);
    const items = [];

    failedLogs.forEach((row) => {
      items.push(`
        <div class="drawer-note">
          <div class="drawer-note-title">发送失败: ${row.account_name || "未知账号"}</div>
          <div class="drawer-note-desc">${row.group_title || row.group_id || "未知目标"} · ${row.error || "请检查发送限制与黑名单"}</div>
          <div class="drawer-note-meta">${formatTime(row.created_at)}</div>
        </div>
      `);
    });

    taskAlerts.forEach((row) => {
      items.push(`
        <div class="drawer-note">
          <div class="drawer-note-title">任务状态更新: ${row.account || "未知账号"}</div>
          <div class="drawer-note-desc">当前状态为 ${row.status || "unknown"}，轮次 ${row.current_round || 0}/${row.rounds || 0}</div>
          <div class="drawer-note-meta">${row.last_updated_at ? formatTime(row.last_updated_at) : "刚刚"}</div>
        </div>
      `);
    });

    alerts.innerHTML = items.join("") || `
      <div class="empty-panel">当前没有需要处理的系统提醒。</div>
    `;
  }

  function renderStatus(summary, accounts, logs) {
    const runningTasks = summary.filter((row) => row.status === "running");
    const todayLogs = logs.filter((row) => {
      if (!row.created_at) return false;
      const dt = new Date(row.created_at);
      if (Number.isNaN(dt.getTime())) return false;
      const now = new Date();
      return dt.getFullYear() === now.getFullYear()
        && dt.getMonth() === now.getMonth()
        && dt.getDate() === now.getDate();
    });
    const successLogs = todayLogs.filter((row) => (row.status || "").toLowerCase() === "success");
    const failedLogs = todayLogs.filter((row) => (row.status || "").toLowerCase() === "failed");
    const authorized = accounts.filter((row) => row.authorized);
    const selectedCount = document.querySelectorAll("#groupList input[type='checkbox']:checked").length;
    const loadedGroups = document.querySelectorAll("#groupList .group-item").length;

    updateText("dashboardBotCount", String(accounts.length || 0));
    updateText("dashboardGroupCount", String(loadedGroups || 0));
    updateText("dashboardTodayInvites", String(todayLogs.length || 0));
    updateText("dashboardSuccessRate", ratio(successLogs.length, successLogs.length + failedLogs.length));
    updateText("overviewLogCount", String(logs.length || 0));
    updateText("overviewSelectedCount", String(loadedGroups || 0));
    updateText("miniSelectedCount", String(selectedCount || 0));
    updateText("dashboardApiStatus", logs.length ? "在线" : "待命");
    updateText("dashboardOnlineAccounts", String(authorized.length || 0));
    updateText("dashboardTaskQueue", String(runningTasks.length || 0));
    updateText("analyticsCompletionRate", ratio(successLogs.length, successLogs.length + failedLogs.length));
    updateText("analyticsSuccessCount", String(successLogs.length || 0));
    updateText("analyticsFailedCount", String(failedLogs.length || 0));
    updateText("analyticsRunningCount", String(runningTasks.length || 0));
    updateText("miniSelectedCountMirror", String(selectedCount || 0));

    const statusList = document.getElementById("dashboardRealtimeStatus");
    if (statusList) {
      statusList.innerHTML = `
        <div class="status-item">
          <div>
            <div class="status-label"><i data-lucide="waypoints"></i> Telegram API</div>
            <div class="status-meta">基于最近日志与接口轮询状态生成</div>
          </div>
          <div class="status-metric">
            <strong>${logs.length ? "稳定" : "待唤醒"}</strong>
            <span class="status-pill ${logs.length ? "success" : "warning"}">${logs.length ? "Connected" : "Idle"}</span>
          </div>
        </div>
        <div class="status-item">
          <div>
            <div class="status-label"><i data-lucide="users"></i> 在线账号</div>
            <div class="status-meta">已授权账号 / 当前会话发现</div>
          </div>
          <div class="status-metric">
            <strong>${authorized.length}/${accounts.length || 0}</strong>
            <span class="status-pill info">Authorized</span>
          </div>
        </div>
        <div class="status-item">
          <div>
            <div class="status-label"><i data-lucide="list-todo"></i> 任务队列</div>
            <div class="status-meta">运行中任务数量与最近状态更新</div>
          </div>
          <div class="status-metric">
            <strong>${runningTasks.length}</strong>
            <span class="status-pill ${runningTasks.length ? "success" : "warning"}">${runningTasks.length ? "Running" : "Paused"}</span>
          </div>
        </div>
      `;
    }

    const modules = document.getElementById("moduleOverviewList");
    if (modules) {
      modules.innerHTML = `
        <div class="module-item">
          <div class="activity-title">Bot 管理</div>
          <div class="activity-desc">当前共识别 ${accounts.length || 0} 个会话账号，可通过账号管理和协议号批量功能维护。</div>
        </div>
        <div class="module-item">
          <div class="activity-title">邀请任务</div>
          <div class="activity-desc">当前运行中任务 ${runningTasks.length || 0} 个，已选择群组 ${selectedCount || 0} 个。</div>
        </div>
        <div class="module-item">
          <div class="activity-title">统计分析</div>
          <div class="activity-desc">今日日志 ${todayLogs.length || 0} 条，成功率 ${ratio(successLogs.length, successLogs.length + failedLogs.length)}。</div>
        </div>
      `;
    }
  }

  function showToast(title, desc) {
    const toast = document.getElementById("drawerToast");
    if (!toast) return;
    toast.innerHTML = `
      <div class="drawer-toast-title">${title}</div>
      <div class="drawer-toast-desc">${desc}</div>
    `;
    toast.classList.add("active");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toast.classList.remove("active"), 2600);
  }

  function renderAdminShell(summary, logs) {
    const audit = document.getElementById("auditSummaryList");
    if (audit) {
      const items = logs.slice(0, 5).map((row) => `
        <div class="activity-item">
          <div class="activity-copy">
            <div class="activity-title">${row.account_name || "未知账号"} → ${row.group_title || row.group_id || "未知目标"}</div>
            <div class="activity-desc">${row.error || row.message_preview || "已发送消息"}</div>
          </div>
          <div class="activity-time">${formatTime(row.created_at)}</div>
        </div>
      `).join("");
      audit.innerHTML = items || `<div class="empty-panel">暂无日志记录。</div>`;
    }

    const queue = document.getElementById("taskSummaryMini");
    if (queue) {
      const items = summary.slice(0, 5).map((row) => `
        <div class="activity-item">
          <div class="activity-copy">
            <div class="activity-title">${row.account || "未知账号"} · ${row.status || "unknown"}</div>
            <div class="activity-desc">轮次 ${row.current_round || 0}/${row.rounds || 0} · 成功 ${row.success || 0} · 失败 ${row.failed || 0}</div>
          </div>
          <div class="activity-time">${row.last_updated_at ? formatTime(row.last_updated_at) : "--"}</div>
        </div>
      `).join("");
      queue.innerHTML = items || `<div class="empty-panel">当前没有任务摘要。</div>`;
    }
  }

  async function refreshDashboardData() {
    const token = getToken();
    if (!token) return;
    try {
      const [summary, logs, accounts] = await Promise.all([
        fetchJson("/api/tasks/summary"),
        fetchJson("/api/logs?limit=48"),
        fetchJson("/api/accounts/status"),
      ]);
      renderTrendChart(logs);
      renderRecentLogs(logs);
      renderDrawer(logs, summary);
      renderStatus(summary, accounts, logs);
      renderAdminShell(summary, logs);
      updateText("miniChannelModeMirror", readText("miniChannelMode"));
      updateText("miniAuthStateMirror", readText("miniAuthState"));
    } catch (err) {
      showToast("后台数据同步失败", "请确认管理员令牌已保存，或稍后重试。");
    } finally {
      if (window.lucide && typeof window.lucide.createIcons === "function") {
        window.lucide.createIcons();
      }
    }
  }

  function setPage(page) {
    state.activePage = page;
    document.querySelectorAll("[data-page-target]").forEach((node) => {
      node.classList.toggle("active", node.dataset.pageTarget === page);
    });
    document.querySelectorAll(".app-page").forEach((node) => {
      node.classList.toggle("active", node.dataset.page === page);
    });
  }

  function bindNavigation() {
    document.querySelectorAll("[data-page-target]").forEach((node) => {
      node.addEventListener("click", () => setPage(node.dataset.pageTarget));
    });
  }

  function bindLayoutControls() {
    const appContainer = document.querySelector(".app-container");
    const collapseBtn = document.getElementById("sidebarCollapse");
    const drawerToggle = document.getElementById("drawerToggle");
    const drawerClose = document.getElementById("drawerClose");

    if (collapseBtn && appContainer) {
      collapseBtn.addEventListener("click", () => {
        appContainer.classList.toggle("sidebar-collapsed");
      });
    }

    function toggleDrawer(forceHidden) {
      if (!appContainer) return;
      if (typeof forceHidden === "boolean") {
        appContainer.classList.toggle("drawer-hidden", forceHidden);
      } else {
        appContainer.classList.toggle("drawer-hidden");
      }
    }

    if (drawerToggle) {
      drawerToggle.addEventListener("click", () => toggleDrawer());
    }
    if (drawerClose) {
      drawerClose.addEventListener("click", () => toggleDrawer(true));
    }
  }

  function bindQuickActions() {
    const inviteBtn = document.getElementById("jumpToInvitePage");
    const logsBtn = document.getElementById("jumpToLogsPage");
    const refreshBtn = document.getElementById("dashboardRefresh");
    if (inviteBtn) inviteBtn.addEventListener("click", () => setPage("invites"));
    if (logsBtn) logsBtn.addEventListener("click", () => setPage("logs"));
    if (refreshBtn) refreshBtn.addEventListener("click", refreshDashboardData);

    const bridges = [
      ["openProtocolManagerSecondary", "openProtocolManager"],
      ["accountManageBtnSecondary", "accountManageBtn"],
      ["checkAccountsBtnSecondary", "checkAccountsBtn"],
      ["accountManageBtnSettings", "accountManageBtn"],
      ["checkAccountsBtnSettings", "checkAccountsBtn"],
      ["resetSystemBtnSettings", "resetSystemBtn"],
    ];
    bridges.forEach(([fromId, toId]) => {
      const from = document.getElementById(fromId);
      const to = document.getElementById(toId);
      if (from && to) {
        from.addEventListener("click", () => to.click());
      }
    });
  }

  function initSkeletons() {
    ["dashboardBotCount", "dashboardGroupCount", "dashboardTodayInvites", "dashboardSuccessRate"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.classList.add("skeleton");
    });
    window.setTimeout(() => {
      ["dashboardBotCount", "dashboardGroupCount", "dashboardTodayInvites", "dashboardSuccessRate"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.classList.remove("skeleton");
      });
    }, 1200);
  }

  function init() {
    bindNavigation();
    bindLayoutControls();
    bindQuickActions();
    initSkeletons();
    setPage(PAGE_DEFAULT);
    refreshDashboardData();
    window.clearInterval(state.timer);
    state.timer = window.setInterval(refreshDashboardData, POLL_INTERVAL_MS);
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
