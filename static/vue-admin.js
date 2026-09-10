const { createApp, computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } = Vue;
const DISTRIBUTED_JOIN_PLAN_STORAGE_KEY = "distributedJoinPlan";

createApp({
  setup() {
    const navItems = [
      { key: "dashboard", label: "总览", desc: "任务、日志和状态" },
      { key: "accounts", label: "账号管理", desc: "登录、Session、资料" },
      { key: "groups", label: "群组管理", desc: "群组加载与缓存" },
      { key: "send", label: "邀请任务", desc: "发送与批量群发" },
      { key: "protocols", label: "协议号批量", desc: "上传、验证、加群" },
      { key: "health", label: "健康检查", desc: "检测并清理失效账号" },
      { key: "system", label: "系统设置", desc: "重置与同步" },
    ];

    const ui = reactive({
      activePage: "dashboard",
      sidebarOpen: false,
    });
    const loading = reactive({
      groups: false,
      send: false,
      protocolJoin: false,
      checkAccounts: false,
      groupJoin: false,
      clearInvalidAccounts: false,
      clearNoSendAccounts: false,
      deleteAccounts: false,
    });
    const notice = reactive({ type: "info", message: "" });
    let noticeTimer = null;
    let logsAutoRefreshTimer = null;
    let logsAutoRefreshInFlight = false;
    const HEALTH_CHECK_WORKERS = 3;

    const tokenInput = ref(localStorage.getItem("adminToken") || "");
    const token = ref(tokenInput.value);
    const tokenStatus = ref(token.value ? "令牌已加载" : "请先保存管理员令牌");
    const tokenStatusClass = computed(() => (token.value ? "success-text" : "warn-text"));

    const accounts = ref([]);
    const selectedAccount = ref(localStorage.getItem("selectedAccount") || "");
    const authStatusText = ref("未检查");

    const groups = ref([]);
    const groupLoadError = ref("");
    const groupSearch = ref("");
    const includeChannels = ref(localStorage.getItem("includeChannels") === "1");
    const selectedIds = ref(new Set());

    const tasksSummary = ref([]);
    const tasks = ref([]);
    const logs = ref([]);
    const authorizedProfiles = ref([]);
    const accountCheckResults = ref([]);

    const login = reactive({
      phone: "",
      code: "",
      password: "",
      forceSms: false,
    });
    const profile = reactive({
      nickname: "",
      about: "",
    });
    const joinLinksText = ref("");
    const joinActionResult = ref("");
    const joinProgress = reactive({
      mode: "",
      running: false,
      total: 0,
      completed: 0,
      success: 0,
      alreadyJoined: 0,
      failed: 0,
      current: "",
      rows: [],
    });
    const defaultProxySourceText = ref("");
    const sessionProxyBindingsText = ref("");
    const protocolProxyBindingsText = ref("");
    const resetSessions = ref(false);
    const proxyForm = reactive({
      proxy_url: "",
      enabled: true,
    });
    const proxySaveResult = ref("");

    const sendForm = reactive({
      message: "",
      parse_mode: "plain",
      delay_ms: 3000,
      rounds: 100,
      round_interval_s: 600,
      disable_web_page_preview: true,
      batch_mode: "broadcast_all",
    });
    const sendResult = ref("");
    const distributedJoinPlan = ref({
      updatedAt: "",
      rows: [],
    });

    const uploadedAccounts = ref([]);
    const protocolAccounts = ref([]);
    const protocolValidAccounts = ref([]);
    const protocolValidationResults = ref([]);
    const protocolResult = ref("");
    const protocolLinksText = ref("");
    const protocolJoin = reactive({
      accountDelay: 5,
      groupDelay: 3,
    });
    const protocolJoinResult = ref("");

    const sessionUploadInput = ref(null);
    const protocolUploadInput = ref(null);

    // ponytail: 调试会话默认关闭，真要追查卡顿再手动打开。
    const HEALTH_CHECK_DEBUG_ENABLED = false;
    const HEALTH_CHECK_DEBUG_URL = "http://127.0.0.1:7777/event";
    const HEALTH_CHECK_DEBUG_SESSION = "health-check-stall";
    function reportHealthCheckDebug(hypothesisId, location, msg, data = {}) {
      if (!HEALTH_CHECK_DEBUG_ENABLED) return;
      fetch(HEALTH_CHECK_DEBUG_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId: HEALTH_CHECK_DEBUG_SESSION,
          runId: "post-fix",
          hypothesisId,
          location,
          msg: `[DEBUG] ${msg}`,
          data,
          ts: Date.now(),
        }),
      }).catch(() => {});
    }
    // #endregion

    const filteredGroups = computed(() => {
      const q = groupSearch.value.trim().toLowerCase();
      if (!q) return groups.value;
      return groups.value.filter((item) => String(item.title || "").toLowerCase().includes(q));
    });
    const runningTaskCount = computed(() => tasksSummary.value.filter((item) => ["running", "waiting_next_round", "scheduled"].includes(item.status)).length);
    const authorizedAccountCount = computed(() => accounts.value.filter((item) => item.authorized).length);
    const unauthorizedAccountCount = computed(() => collectUnauthorizedAccountNames().length);
    const selectedGroupCount = computed(() => selectedIds.value.size);
    const invalidAccountCount = computed(() => accountCheckResults.value.filter((item) => item.status === "失效账号").length);
    const cannotSendAccountCount = computed(() => accountCheckResults.value.filter((item) => item.valid === true && item.can_send_in_groups === false).length);
    const canRunJoinActions = computed(() => Boolean(parseLinks(joinLinksText.value).length && currentJoinAccounts().length));
    const canRunProtocolJoin = computed(() => Boolean(parseLinks(protocolLinksText.value).length && activeBatchJoinAccounts().length));
    const canStartSend = computed(() => Boolean(selectedAccount.value && sendForm.message.trim() && selectedGroupCount.value));
    const distributedJoinPlanRows = computed(() => Array.isArray(distributedJoinPlan.value?.rows) ? distributedJoinPlan.value.rows : []);
    const distributedJoinPlanGroupCount = computed(() => distributedJoinPlanRows.value.length);
    const distributedJoinPlanAccountCount = computed(() => new Set(distributedJoinPlanRows.value.map((item) => item.account).filter(Boolean)).size);
    const canStartBatchSend = computed(() => {
      if (!sendForm.message.trim() || !activeAuthorizedAccounts().length) return false;
      if (sendForm.batch_mode === "distributed_join") return distributedJoinPlanGroupCount.value > 0;
      return selectedGroupCount.value > 0;
    });
    const joinProgressPercent = computed(() => {
      if (!joinProgress.total) return 0;
      return Math.round((joinProgress.completed / joinProgress.total) * 100);
    });
    const groupPanelStatusText = computed(() => {
      if (!token.value) return "请先保存管理员令牌";
      if (!selectedAccount.value) return "请先选择账号";
      if (loading.groups) return "群组加载中...";
      if (groupLoadError.value) return groupLoadError.value;
      if (!groups.value.length) return "当前账号还没有可用群组数据";
      return `已加载 ${groups.value.length} 个群组，已选 ${selectedGroupCount.value} 个`;
    });
    const logsAutoRefreshText = computed(() => token.value ? "最近日志自动刷新中（每 5 秒）" : "保存令牌后自动刷新最近日志");

    function setNotice(type, message) {
      if (noticeTimer) {
        clearTimeout(noticeTimer);
        noticeTimer = null;
      }
      notice.type = type;
      notice.message = message;
      if (!message) return;
      const delay = type === "error" ? 9000 : 4500;
      noticeTimer = setTimeout(() => {
        if (notice.message === message) {
          notice.message = "";
        }
      }, delay);
    }

    function resetProxyForm() {
      proxyForm.proxy_url = "";
      proxyForm.enabled = true;
      proxySaveResult.value = "";
    }

    function formatTime(value) {
      if (!value) return "-";
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) return String(value);
      return dt.toLocaleString("zh-CN", { hour12: false });
    }

    function formatProxy(proxy) {
      if (!proxy) return "-";
      return proxy.proxy_url_masked || proxy.proxy_url || "-";
    }

    const exactTextMap = {
      success: "成功",
      ok: "成功",
      done: "已完成",
      running: "运行中",
      paused: "已暂停",
      pending: "等待中",
      waiting: "等待中",
      waiting_next_round: "等待下一轮",
      queued: "排队中",
      stopped: "已停止",
      processing: "处理中",
      sending: "发送中",
      active: "运行中",
      failed: "失败",
      error: "异常",
      normal: "正常",
      authorized: "已授权",
      unauthorized: "未授权",
      invalid: "无效",
      checking: "检查中",
      cannot_send: "不可发送",
      no_sendable_groups: "无可发群",
      missing_file: "缺少文件",
      session_not_authorized: "账号 Session 未授权",
      account_not_authorized: "账号未授权",
      no_authorized_accounts: "当前没有已授权账号",
      no_accounts_available: "当前没有可用账号",
      task_db_unavailable: "任务数据库暂时不可用",
      chat_write_forbidden: "群内禁止发言",
      user_banned_in_channel: "账号在群组中被禁言",
      already_joined: "已加入",
      forbidden: "被拒绝",
      not_found: "未找到",
      timeout: "超时",
    };

    const actionTextMap = {
      pause: "暂停",
      resume: "恢复",
      stop: "停止",
    };

    const textReplaceRules = [
      [/http\s+(\d+)/gi, "请求失败（HTTP $1）"],
      [/Could not find the input entity for PeerChannel\(channel_id=(\d+)\).*$/gi, "找不到目标频道实体（ID: $1），可能该群组未缓存或当前账号无访问权限"],
      [/inline_send_failed:\s*You cannot send inline results in this chat.*$/gi, "内联发送失败：当前聊天不允许发送内联结果"],
      [/inline_send_failed:\s*The chat is restricted and cannot be used in that request.*$/gi, "内联发送失败：该聊天受限，当前请求无法使用"],
      [/A wait of (\d+) seconds is required before sending another message in this chat.*$/gi, "当前聊天触发频率限制，需等待 $1 秒后再发送"],
      [/Connection to Telegram failed (\d+) time\(s\)/gi, "连接 Telegram 失败（已重试 $1 次）"],
      [/RPCError 400:\s*TOPIC_CLOSED.*$/gi, "话题已关闭，无法发送消息"],
      [/session[_ ]not[_ ]authorized/gi, "账号 Session 未授权"],
      [/account[_ ]not[_ ]authorized/gi, "账号未授权"],
      [/no[_ ]authorized[_ ]accounts/gi, "当前没有已授权账号"],
      [/no[_ ]accounts[_ ]available/gi, "当前没有可用账号"],
      [/task[_ ]db[_ ]unavailable/gi, "任务数据库暂时不可用"],
      [/database disk image is malformed/gi, "数据库文件异常"],
      [/chat[_ ]write[_ ]forbidden/gi, "群内禁止发言"],
      [/user[_ ]banned[_ ]in[_ ]channel/gi, "账号在群组中被禁言"],
      [/already joined/gi, "已加入"],
      [/connection to telegram failed/gi, "连接 Telegram 失败"],
      [/telegram session not authorized/gi, "Telegram Session 未授权"],
      [/phone code invalid/gi, "验证码错误"],
      [/phone code expired/gi, "验证码已过期"],
      [/flood wait/gi, "触发频率限制"],
      [/timed out/gi, "超时"],
      [/\btimeout\b/gi, "超时"],
      [/\bforbidden\b/gi, "被拒绝"],
      [/\bunauthorized\b/gi, "未授权"],
      [/\bnot found\b/gi, "未找到"],
      [/\binvalid\b/gi, "无效"],
      [/\bfailed\b/gi, "失败"],
      [/\berror\b/gi, "异常"],
    ];

    function translateText(value, fallback = "-") {
      if (value === null || value === undefined) return fallback;
      const text = String(value).trim();
      if (!text) return fallback;
      const lower = text.toLowerCase();
      if (exactTextMap[lower]) return exactTextMap[lower];
      let translated = text;
      for (const [pattern, replacement] of textReplaceRules) {
        translated = translated.replace(pattern, replacement);
      }
      return translated;
    }

    function formatStatusLabel(value) {
      return translateText(value, "-");
    }

    function formatActionLabel(action) {
      return actionTextMap[String(action || "").trim().toLowerCase()] || String(action || "");
    }

    function formatLogDetail(row) {
      if (row?.error) return translateText(row.error);
      if (row?.message_id) return `消息 ID：${row.message_id}`;
      return "-";
    }

    function formatProfileAbout(row) {
      if (row?.ok) return row.about || "-";
      return translateText(row?.error || "读取失败");
    }

    function selectedAccountSummary() {
      if (!selectedAccount.value) return "未选择";
      const current = accounts.value.find((item) => item.account === selectedAccount.value);
      if (!current) return selectedAccount.value;
      return `${selectedAccount.value}${current.authorized ? " / 已授权" : " / 未授权"}`;
    }

    function _selfCheckUiHelpers() {
      console.assert(typeof selectedAccountSummary() === "string", "ui helper should return a string");
      console.assert(formatStatusLabel("running") === "运行中", "status translation should work");
    }

    _selfCheckUiHelpers();

    function statusClass(value, kind = "general") {
      const raw = String(value ?? "").trim().toLowerCase();
      const successValues = ["success", "done", "ok", "normal", "authorized", "可用", "正常", "已授权", "可发送", "成功", "已加入"];
      const runningValues = ["running", "sending", "processing", "active", "验证中", "进行中", "检查中"];
      const warningValues = ["paused", "pending", "waiting", "waiting_next_round", "queued", "stopped", "warning", "未检查", "missing_file", "不可发送", "no_sendable_groups", "等待中"];
      const errorValues = ["failed", "error", "banned", "unauthorized", "invalid", "不可用", "异常", "未授权", "失效账号", "不可发", "失败"];
      const successKeywords = ["success", "done", "ok", "authorized", "可用", "正常", "已授权", "可发送", "成功", "已加入"];
      const runningKeywords = ["running", "sending", "processing", "active", "进行中", "验证中", "检查中"];
      const warningKeywords = ["paused", "pending", "waiting", "waiting_next_round", "queued", "stopped", "未检查", "不可发送", "等待", "无可发群"];
      const errorKeywords = ["failed", "error", "banned", "unauthorized", "invalid", "不可用", "异常", "未授权", "失败", "失效账号", "不可发"];

      if (kind === "log") {
        if (raw === "success") return "status-text status-success";
        if (raw === "failed") return "status-text status-danger";
      }

      if (successValues.includes(raw)) return "status-text status-success";
      if (runningValues.includes(raw)) return "status-text status-info";
      if (warningValues.includes(raw)) return "status-text status-warning";
      if (errorValues.includes(raw)) return "status-text status-danger";
      if (successKeywords.some((item) => raw.includes(item))) return "status-text status-success";
      if (runningKeywords.some((item) => raw.includes(item))) return "status-text status-info";
      if (warningKeywords.some((item) => raw.includes(item))) return "status-text status-warning";
      if (errorKeywords.some((item) => raw.includes(item))) return "status-text status-danger";
      if (!raw || raw === "-") return "status-text status-muted";
      return "status-text status-muted";
    }

    function setPage(key) {
      ui.activePage = key;
      ui.sidebarOpen = false;
      if (key === "accounts" && selectedAccount.value) {
        fetchAuthStatus().catch(() => {});
        loadProxyForSelectedAccount().catch(() => {});
      }
      if (key === "health") {
        syncHealthRows();
      }
      if ((key === "accounts" || key === "health") && !authorizedProfiles.value.length) {
        fetchAuthorizedProfiles(false, false).catch(() => {});
      }
      if ((key === "groups" || key === "send") && !groups.value.length) {
        fetchGroups(false, false).catch(() => {});
      }
      if (key === "send" && !tasks.value.length) {
        fetchTasksSummary(true, false).catch(() => {});
      }
    }

    function syncHealthRows() {
      const sourceAccounts = authorizedProfiles.value.length
        ? authorizedProfiles.value.map((item) => ({
            account: item.account,
            detail: item.phone || item.nickname || "-",
          }))
        : accounts.value
            .filter((item) => item.authorized)
            .map((item) => ({
              account: item.account,
              detail: "-",
            }));
      const previousMap = new Map(
        accountCheckResults.value.map((item) => [item.account, item]),
      );
      accountCheckResults.value = sourceAccounts.map((item) => {
        const previous = previousMap.get(item.account);
        if (previous) return previous;
        return {
          account: item.account,
          valid: null,
          status: "未检查",
          can_send_in_groups: null,
          detail: item.detail || "-",
        };
      });
      return accountCheckResults.value;
    }

    function applyHealthCheckResult(account, result) {
      const rawStatus = result.status || "";
      let displayStatus = "失效账号";
      if (rawStatus === "checking") {
        displayStatus = "检查中";
      } else if (rawStatus === "未检查") {
        displayStatus = "未检查";
      } else if (result.valid && result.can_send_in_groups !== false) {
        displayStatus = "正常账号";
      }
      const next = {
        account,
        valid: result.valid,
        status: displayStatus,
        raw_status: rawStatus,
        can_send_in_groups: result.can_send_in_groups,
        detail: result.detail || result.phone || "-",
        phone: result.phone || "",
        checked_groups: result.checked_groups || 0,
        sendable_groups: result.sendable_groups || 0,
      };
      const idx = accountCheckResults.value.findIndex((item) => item.account === account);
      if (idx >= 0) {
        accountCheckResults.value.splice(idx, 1, next);
      } else {
        accountCheckResults.value.push(next);
      }
    }

    function removeHealthRows(accountsToRemove) {
      const targets = new Set((accountsToRemove || []).filter(Boolean));
      if (!targets.size) return;
      accountCheckResults.value = accountCheckResults.value.filter((item) => !targets.has(item.account));
      authorizedProfiles.value = authorizedProfiles.value.filter((item) => !targets.has(item.account));
      accounts.value = accounts.value.filter((item) => !targets.has(item.account));
      if (selectedAccount.value && targets.has(selectedAccount.value)) {
        selectedAccount.value = accounts.value[0]?.account || "";
        if (selectedAccount.value) {
          localStorage.setItem("selectedAccount", selectedAccount.value);
        } else {
          localStorage.removeItem("selectedAccount");
        }
      }
    }

    function collectKnownAccountNames() {
      const names = accounts.value.map((item) => item.account).filter(Boolean);
      if (names.length) return Array.from(new Set(names));
      return Array.from(new Set(authorizedProfiles.value.map((item) => item.account).filter(Boolean)));
    }

    function collectUnauthorizedAccountNames() {
      return Array.from(new Set(
        accounts.value
          .filter((item) => {
            const status = String(item?.status || "").trim().toLowerCase();
            const detail = String(item?.detail || "").trim().toLowerCase();
            return status === "unauthorized" || status === "auth_error" || detail.includes("未授权");
          })
          .map((item) => item.account)
          .filter(Boolean)
      ));
    }

    function saveToken() {
      token.value = tokenInput.value.trim();
      localStorage.setItem("adminToken", token.value);
      tokenStatus.value = token.value ? "令牌已保存" : "令牌为空";
      if (token.value) {
        loadDefaultProxySource().catch(() => {});
        fetchDashboard(true);
      }
    }

    function buildHeaders(isJson = false) {
      const headers = {};
      if (token.value) headers["X-Admin-Token"] = token.value;
      if (isJson) headers["Content-Type"] = "application/json";
      return headers;
    }

    async function request(url, options = {}) {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(translateText(data.detail || data.error || `HTTP ${res.status}`));
      }
      return data;
    }

    function restoreSelectedIds() {
      try {
        const raw = localStorage.getItem("selectedGroupIds");
        if (!raw) return;
        selectedIds.value = new Set(JSON.parse(raw));
      } catch {
        selectedIds.value = new Set();
      }
    }

    function persistSelectedIds() {
      localStorage.setItem("selectedGroupIds", JSON.stringify(Array.from(selectedIds.value)));
    }

    function groupBadge(group) {
      if (group.is_channel && !group.is_megagroup) return "频道";
      if (group.is_megagroup) return "超级群";
      return "群";
    }

    function isGroupDisabled(group) {
      return Boolean(group.is_channel && !group.is_megagroup);
    }

    function toggleGroup(id, checked) {
      const next = new Set(selectedIds.value);
      if (checked) next.add(id);
      else next.delete(id);
      selectedIds.value = next;
      persistSelectedIds();
    }

    function selectVisible(checked) {
      const next = new Set(selectedIds.value);
      filteredGroups.value.forEach((group) => {
        if (isGroupDisabled(group)) return;
        if (checked) next.add(group.id);
        else next.delete(group.id);
      });
      selectedIds.value = next;
      persistSelectedIds();
    }

    function selectedGroupIds() {
      return Array.from(selectedIds.value);
    }

    function activeAuthorizedAccounts() {
      if (authorizedProfiles.value.length) {
        return authorizedProfiles.value
          .filter((item) => item && item.ok === true)
          .map((item) => String(item.account || "").trim())
          .filter(Boolean);
      }
      return accounts.value
        .filter((item) => item.authorized)
        .map((item) => String(item.account || "").trim())
        .filter(Boolean);
    }

    function loadUploadedAccountsCache() {
      try {
        const raw = localStorage.getItem("uploadedAccounts");
        const parsed = raw ? JSON.parse(raw) : [];
        uploadedAccounts.value = Array.isArray(parsed) ? parsed.filter(Boolean) : [];
      } catch {
        uploadedAccounts.value = [];
      }
    }

    function persistUploadedAccounts() {
      localStorage.setItem("uploadedAccounts", JSON.stringify(uploadedAccounts.value));
    }

    function loadDistributedJoinPlan() {
      try {
        const raw = localStorage.getItem(DISTRIBUTED_JOIN_PLAN_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        const rows = Array.isArray(parsed?.rows) ? parsed.rows.filter((item) => item && item.account && item.group_id) : [];
        distributedJoinPlan.value = {
          updatedAt: parsed?.updatedAt || "",
          rows,
        };
      } catch {
        distributedJoinPlan.value = { updatedAt: "", rows: [] };
      }
    }

    function persistDistributedJoinPlan() {
      localStorage.setItem(DISTRIBUTED_JOIN_PLAN_STORAGE_KEY, JSON.stringify(distributedJoinPlan.value));
    }

    function saveDistributedJoinPlan(rows) {
      const seen = new Set();
      const cleaned = [];
      for (const item of rows || []) {
        const account = String(item?.account || "").trim();
        const invite_link = String(item?.invite_link || "").trim();
        const title = String(item?.title || item?.group_title || "").trim();
        const numericGroupId = Number(item?.group_id);
        if (!account || !Number.isFinite(numericGroupId)) continue;
        const key = `${account}:${numericGroupId}`;
        if (seen.has(key)) continue;
        seen.add(key);
        cleaned.push({
          account,
          group_id: numericGroupId,
          invite_link,
          title,
        });
      }
      distributedJoinPlan.value = {
        updatedAt: new Date().toISOString(),
        rows: cleaned,
      };
      persistDistributedJoinPlan();
    }

    function activeBatchJoinAccounts() {
      if (protocolValidAccounts.value.length) return protocolValidAccounts.value;
      if (uploadedAccounts.value.length) return uploadedAccounts.value;
      if (protocolAccounts.value.length) return protocolAccounts.value;
      const authorized = accounts.value.filter((item) => item.authorized).map((item) => item.account).filter(Boolean);
      if (authorized.length) return authorized;
      return [];
    }

    function currentJoinAccounts() {
      if (uploadedAccounts.value.length) return uploadedAccounts.value.slice();
      const authorized = accounts.value.filter((item) => item.authorized).map((item) => item.account).filter(Boolean);
      return authorized;
    }

    function firstAuthorizedAccount() {
      return accounts.value.find((item) => item.authorized)?.account || accounts.value[0]?.account || "";
    }

    async function refreshAccountContext(preferredAccount = "") {
      await fetchAccounts();
      const target = preferredAccount && accounts.value.some((item) => item.account === preferredAccount)
        ? preferredAccount
        : (selectedAccount.value || firstAuthorizedAccount());
      if (target && target !== selectedAccount.value) {
        selectedAccount.value = target;
        localStorage.setItem("selectedAccount", target);
      }
      if (selectedAccount.value) {
        await fetchAuthStatus();
        await loadProxyForSelectedAccount();
      }
    }

    function ensureSelectedAccount() {
      const target = selectedAccount.value || firstAuthorizedAccount();
      if (!target) {
        throw new Error("当前没有可用账号，请先上传或登录账号");
      }
      if (target !== selectedAccount.value) {
        selectedAccount.value = target;
        localStorage.setItem("selectedAccount", target);
      }
      return target;
    }

    async function fetchAccounts(includeSelectedDetails = true) {
      if (!token.value) return;
      accounts.value = await request("/api/accounts/status", { headers: buildHeaders() });
      if (!accounts.value.length) {
        selectedAccount.value = "";
        localStorage.removeItem("selectedAccount");
        authStatusText.value = "暂无账号";
        resetProxyForm();
        return;
      }
      const saved = selectedAccount.value || localStorage.getItem("selectedAccount");
      const fallback = saved && accounts.value.some((item) => item.account === saved)
        ? saved
        : accounts.value[0].account;
      selectedAccount.value = fallback;
      localStorage.setItem("selectedAccount", fallback);
      if (!includeSelectedDetails) return;
      await fetchAuthStatus();
      await loadProxyForSelectedAccount();
    }

    async function fetchAuthStatus() {
      if (!token.value || !selectedAccount.value) return;
      const data = await request(`/api/account-status?account=${encodeURIComponent(selectedAccount.value)}`, {
        headers: buildHeaders(),
      });
      authStatusText.value = data.authorized ? "当前账号已授权" : "当前账号未授权";
      if (!login.phone) {
        try {
          const phoneData = await request(`/api/login/default-phone?account=${encodeURIComponent(selectedAccount.value)}`, {
            headers: buildHeaders(),
          });
          login.phone = phoneData.phone || "";
        } catch {}
      }
    }

    async function fetchGroups(force = false, throwOnError = false) {
      if (!token.value || !selectedAccount.value) {
        groups.value = [];
        groupLoadError.value = "";
        return;
      }
      loading.groups = true;
      try {
        const url = `/api/groups?only_groups=${includeChannels.value ? "false" : "true"}&account=${encodeURIComponent(selectedAccount.value)}&refresh=${force ? "true" : "false"}`;
        groups.value = await request(url, { headers: buildHeaders() });
        groupLoadError.value = "";
      } catch (error) {
        groups.value = [];
        groupLoadError.value = `群组读取失败: ${error.message}`;
        if (throwOnError) throw error;
        setNotice("error", `群组加载失败: ${error.message}`);
      } finally {
        loading.groups = false;
      }
    }

    async function fetchLogs(throwOnError = false, silent = false) {
      if (!token.value) return;
      try {
        logs.value = await request("/api/logs?limit=50", { headers: buildHeaders() });
      } catch (error) {
        if (throwOnError) throw error;
        if (!silent) {
          setNotice("error", `读取日志失败: ${error.message}`);
        }
      }
    }

    function stopLogsAutoRefresh() {
      if (logsAutoRefreshTimer) {
        clearTimeout(logsAutoRefreshTimer);
        logsAutoRefreshTimer = null;
      }
    }

    function scheduleLogsAutoRefresh(delayMs = 5000) {
      stopLogsAutoRefresh();
      logsAutoRefreshTimer = setTimeout(async () => {
        if (!token.value || document.visibilityState !== "visible") {
          scheduleLogsAutoRefresh(5000);
          return;
        }
        if (logsAutoRefreshInFlight) {
          scheduleLogsAutoRefresh(2000);
          return;
        }
        logsAutoRefreshInFlight = true;
        try {
          await fetchLogs(false, true);
        } finally {
          logsAutoRefreshInFlight = false;
          scheduleLogsAutoRefresh(5000);
        }
      }, delayMs);
    }

    async function fetchTasksSummary(force = false, throwOnError = false) {
      if (!token.value) return;
      try {
        tasksSummary.value = await request("/api/tasks/summary", { headers: buildHeaders() });
        if (force) tasks.value = await request("/api/tasks", { headers: buildHeaders() });
      } catch (error) {
        if (throwOnError) throw error;
        setNotice("error", `读取任务失败: ${error.message}`);
      }
    }

    async function fetchAuthorizedProfiles(force = false, throwOnError = false) {
      if (!token.value) return;
      // #region debug-point A:profiles-fetch-start
      const startedAt = Date.now();
      reportHealthCheckDebug("A", "static/vue-admin.js:fetchAuthorizedProfiles:start", "fetchAuthorizedProfiles.start", {
        force: Boolean(force),
        hasCachedProfiles: authorizedProfiles.value.length,
      });
      // #endregion
      try {
        authorizedProfiles.value = (await request("/api/accounts/profiles-authorized", { headers: buildHeaders() })).profiles || [];
        syncHealthRows();
        // #region debug-point A:profiles-fetch-success
        reportHealthCheckDebug("A", "static/vue-admin.js:fetchAuthorizedProfiles:success", "fetchAuthorizedProfiles.success", {
          elapsedMs: Date.now() - startedAt,
          profiles: authorizedProfiles.value.length,
        });
        // #endregion
        if (force) {
          setNotice("success", `已刷新授权账号资料，共 ${authorizedProfiles.value.length} 个`);
        }
      } catch (error) {
        // #region debug-point A:profiles-fetch-error
        reportHealthCheckDebug("A", "static/vue-admin.js:fetchAuthorizedProfiles:error", "fetchAuthorizedProfiles.error", {
          elapsedMs: Date.now() - startedAt,
          error: error.message,
        });
        // #endregion
        if (throwOnError) throw error;
        setNotice("error", `读取授权账号失败: ${error.message}`);
      }
    }

    async function fetchDashboard(force = false) {
      try {
        await fetchAccounts(ui.activePage === "accounts");
        const shouldLoadGroups = ui.activePage === "groups" || ui.activePage === "send";
        const shouldLoadProfiles = ui.activePage === "accounts" || ui.activePage === "health";
        const shouldLoadTaskList = ui.activePage === "send";
        const results = await Promise.allSettled([
          fetchLogs(true),
          fetchTasksSummary(shouldLoadTaskList, true),
          ...(shouldLoadGroups ? [fetchGroups(force, true)] : []),
          ...(shouldLoadProfiles ? [fetchAuthorizedProfiles(false, true)] : []),
        ]);
        const errors = results
          .filter((item) => item.status === "rejected")
          .map((item) => item.reason?.message || "未知错误");
        if (errors.length) {
          setNotice("error", errors.join("；"));
        }
      } catch (error) {
        setNotice("error", error.message);
      }
    }

    async function onAccountChange() {
      localStorage.setItem("selectedAccount", selectedAccount.value);
      try {
        await fetchAuthStatus();
        await loadProxyForSelectedAccount();
        await fetchGroups(true);
      } catch (error) {
        setNotice("error", `切换账号失败: ${error.message}`);
      }
    }

    function onIncludeChannelsChange() {
      localStorage.setItem("includeChannels", includeChannels.value ? "1" : "0");
      fetchGroups(true);
    }

    async function loadAccountProfile() {
      if (!selectedAccount.value) {
        setNotice("error", "请先选择账号");
        return;
      }
      try {
        const data = await request(`/api/accounts/profile?account=${encodeURIComponent(selectedAccount.value)}`, {
          headers: buildHeaders(),
        });
        profile.nickname = data.nickname || data.first_name || "";
        profile.about = data.about || "";
        setNotice("success", `已读取 ${selectedAccount.value} 的资料`);
      } catch (error) {
        setNotice("error", `读取资料失败: ${error.message}`);
      }
    }

    async function saveAccountProfile() {
      try {
        const data = await request("/api/accounts/profile", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({
            nickname: profile.nickname,
            about: profile.about,
          }),
        });
        await fetchAuthorizedProfiles(false);
        setNotice("success", `资料保存完成：成功 ${data.success_count || 0} / ${data.accounts_total || 0}`);
      } catch (error) {
        setNotice("error", `保存资料失败: ${error.message}`);
      }
    }

    async function sendLoginCode() {
      try {
        await request("/api/login/send-code", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({
            account: selectedAccount.value,
            phone: login.phone,
            force_sms: login.forceSms,
          }),
        });
        setNotice("success", "验证码已发送");
      } catch (error) {
        setNotice("error", `发送验证码失败: ${error.message}`);
      }
    }

    async function submitLoginCode() {
      try {
        await request("/api/login/submit-code", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({
            account: selectedAccount.value,
            phone: login.phone,
            code: login.code,
            password: login.password,
          }),
        });
        setNotice("success", "登录成功");
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `登录失败: ${error.message}`);
      }
    }

    async function uploadFiles(inputRef, proxyBindingsText = "") {
      const files = inputRef.value?.files;
      if (!files || !files.length) {
        throw new Error("请先选择文件");
      }
      const formData = new FormData();
      Array.from(files).forEach((file) => formData.append("files", file));
      if ((proxyBindingsText || "").trim()) {
        formData.append("proxy_source_text", proxyBindingsText.trim());
      }
      return request("/api/accounts/upload-sessions", {
        method: "POST",
        headers: buildHeaders(),
        body: formData,
      });
    }

    async function loadDefaultProxySource(force = false) {
      if (!token.value) return;
      const data = await request("/api/accounts/proxy/default-source", {
        headers: buildHeaders(),
      });
      const savedSource = (data.proxy_source_text || "").trim();
      const previousDefault = defaultProxySourceText.value;
      defaultProxySourceText.value = savedSource;
      if (force || !sessionProxyBindingsText.value.trim() || sessionProxyBindingsText.value.trim() === previousDefault) {
        sessionProxyBindingsText.value = savedSource;
      }
      if (force || !protocolProxyBindingsText.value.trim() || protocolProxyBindingsText.value.trim() === previousDefault) {
        protocolProxyBindingsText.value = savedSource;
      }
    }

    async function loadProxyForSelectedAccount() {
      resetProxyForm();
      if (!selectedAccount.value || !token.value) return;
      try {
        const data = await request(`/api/accounts/proxy?account=${encodeURIComponent(selectedAccount.value)}`, {
          headers: buildHeaders(),
        });
        const proxy = data.proxy;
        proxyForm.proxy_url = proxy?.proxy_url || "";
        proxyForm.enabled = proxy ? Boolean(proxy.enabled) : true;
      } catch (error) {
        setNotice("error", `读取代理失败: ${error.message}`);
      }
    }

    async function saveProxyForSelectedAccount() {
      if (!selectedAccount.value) {
        setNotice("error", "请先选择账号");
        return;
      }
      try {
        const data = await request("/api/accounts/proxy", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({
            account: selectedAccount.value,
            proxy_url: proxyForm.proxy_url,
            enabled: proxyForm.enabled,
          }),
        });
        proxySaveResult.value = data.proxy
          ? `已保存: ${formatProxy(data.proxy)}`
          : "已清空当前账号代理绑定";
        await fetchAccounts();
        await fetchAuthorizedProfiles(false);
        setNotice("success", "代理保存成功");
      } catch (error) {
        setNotice("error", `保存代理失败: ${error.message}`);
      }
    }

    async function uploadSessions() {
      try {
        const data = await uploadFiles(sessionUploadInput, sessionProxyBindingsText.value);
        const errorText = Array.isArray(data.errors) && data.errors.length ? `\n警告:\n${data.errors.map((item) => translateText(item)).join("\n")}` : "";
        const currentUploadedAccounts = Array.isArray(data.validated_accounts) ? data.validated_accounts.filter(Boolean) : [];
        uploadedAccounts.value = currentUploadedAccounts;
        persistUploadedAccounts();
        const preferredAccount = currentUploadedAccounts.length
          ? currentUploadedAccounts[0]
          : "";
        if (sessionUploadInput.value) sessionUploadInput.value.value = "";
        sessionProxyBindingsText.value = defaultProxySourceText.value;
        await refreshAccountContext(preferredAccount);
        await fetchGroups(true, true).catch(() => {});
        await fetchAuthorizedProfiles(false);
        setNotice(
          "success",
          `上传完成：${data.validated || 0} 个账号有效，当前上传账号集 ${currentUploadedAccounts.length} 个，代理已绑定 ${data.proxy_assigned || 0} / ${data.proxy_accounts_total || currentUploadedAccounts.length} 个${errorText}`,
        );
      } catch (error) {
        setNotice("error", `上传 Session 失败: ${error.message}`);
      }
    }

    function parseLinks(text) {
      return text
        .split("\n")
        .map((item) => item.trim())
        .filter((item) => item && (item.includes("t.me") || item.startsWith("@")));
    }

    function joinProgressKey(account, inviteLink, index = 0) {
      return `${account || "-"}::${inviteLink || "-"}::${index}`;
    }

    function recalcJoinProgress() {
      joinProgress.success = joinProgress.rows.filter((item) => item.status === "成功").length;
      joinProgress.alreadyJoined = joinProgress.rows.filter((item) => item.status === "已加入").length;
      joinProgress.failed = joinProgress.rows.filter((item) => item.status === "失败").length;
      joinProgress.completed = joinProgress.success + joinProgress.alreadyJoined + joinProgress.failed;
    }

    function startJoinProgress(mode, items = []) {
      joinProgress.mode = mode;
      joinProgress.running = true;
      joinProgress.total = items.length;
      joinProgress.completed = 0;
      joinProgress.success = 0;
      joinProgress.alreadyJoined = 0;
      joinProgress.failed = 0;
      joinProgress.current = "";
      joinProgress.rows = items.map((item, index) => ({
        key: item.key || joinProgressKey(item.account, item.invite_link, index),
        account: item.account || "-",
        invite_link: item.invite_link || "-",
        status: "等待中",
        detail: "等待开始",
      }));
    }

    function finishJoinProgress() {
      joinProgress.running = false;
      recalcJoinProgress();
    }

    function updateJoinProgressRow(key, patch = {}) {
      const row = joinProgress.rows.find((item) => item.key === key);
      if (!row) return;
      Object.assign(row, patch);
      recalcJoinProgress();
    }

    function applyJoinResult(key, result = {}) {
      const detail = translateText(
        result.detail || result.error || result.group_title || result.title || (result.already_joined ? "已加入该群" : result.ok ? "加入成功" : "加入失败"),
        "-",
      );
      updateJoinProgressRow(key, {
        status: result.already_joined ? "已加入" : result.ok ? "成功" : "失败",
        detail,
      });
    }

    function normalizeJoinErrorText(result = {}) {
      return String(result?.detail || result?.error || "").trim().toLowerCase();
    }

    function isGroupSideJoinFailure(result = {}) {
      const text = normalizeJoinErrorText(result);
      if (!text) return false;
      const groupSideMarkers = [
        "invalid_invite_link",
        "invite_expired",
        "unknown_invite_type",
        "username invalid",
        "username not occupied",
        "channel_private",
        "chat_admin_required",
        "cannot find any entity",
        "no user has",
      ];
      return groupSideMarkers.some((marker) => text.includes(marker));
    }

    function shouldRetryJoinWithAnotherAccount(result = {}) {
      if (result.ok || result.already_joined) return false;
      if (isGroupSideJoinFailure(result)) return false;
      const text = normalizeJoinErrorText(result);
      if (!text) return true;
      const accountSideMarkers = [
        "unauthorized",
        "not authorized",
        "session",
        "auth",
        "proxy",
        "407",
        "timeout",
        "timed out",
        "connect",
        "connection",
        "socket",
        "network",
        "flood",
        "wait",
        "banned",
        "deactivated",
        "disconnected",
      ];
      return accountSideMarkers.some((marker) => text.includes(marker)) || !isGroupSideJoinFailure(result);
    }

    async function tryDistributedJoinWithFallback(item, targetAccounts, distributedPlanRows) {
      const candidateAccounts = [
        item.account,
        ...targetAccounts.filter((account) => account && account !== item.account),
      ];
      let lastResult = { ok: false, error: "没有可用账号可重试" };

      for (let index = 0; index < candidateAccounts.length; index += 1) {
        const account = candidateAccounts[index];
        joinProgress.current = `${account} -> ${item.invite_link}`;
        updateJoinProgressRow(item.key, {
          account,
          status: index === 0 ? "进行中" : "进行中",
          detail: index === 0
            ? "正在加入群组"
            : `原账号失败，正在切换第 ${index + 1} 个账号重试`,
        });
        try {
          const data = await request("/api/groups/join", {
            method: "POST",
            headers: buildHeaders(true),
            body: JSON.stringify({ account, invite_link: item.invite_link }),
          });
          lastResult = data;
          if ((data.ok || data.already_joined) && data.group_id !== undefined && data.group_id !== null) {
            distributedPlanRows.push({
              account,
              invite_link: item.invite_link,
              group_id: data.group_id,
              title: data.group_title || data.title || "",
            });
            applyJoinResult(item.key, data);
            return data;
          }
          if (!shouldRetryJoinWithAnotherAccount(data) || index === candidateAccounts.length - 1) {
            applyJoinResult(item.key, data);
            return data;
          }
        } catch (error) {
          lastResult = { ok: false, error: error.message };
          if (index === candidateAccounts.length - 1) {
            applyJoinResult(item.key, lastResult);
            return lastResult;
          }
          updateJoinProgressRow(item.key, {
            account,
            status: "进行中",
            detail: `账号异常：${translateText(error.message)}，正在切换其他账号重试`,
          });
        }
      }

      applyJoinResult(item.key, lastResult);
      return lastResult;
    }

    async function joinGroupsForCurrentAccount() {
      loading.groupJoin = true;
      try {
        const account = ensureSelectedAccount();
        const links = parseLinks(joinLinksText.value);
        if (!links.length) throw new Error("请输入有效群组链接");
        startJoinProgress("single", links.map((link, index) => ({ key: joinProgressKey(account, link, index), account, invite_link: link })));
        joinActionResult.value = `开始执行：当前账号 ${account}，共 ${links.length} 个链接`;
        for (let index = 0; index < links.length; index += 1) {
          const link = links[index];
          const key = joinProgressKey(account, link, index);
          joinProgress.current = `${account} -> ${link}`;
          updateJoinProgressRow(key, { status: "进行中", detail: "正在加入群组" });
          const data = await request("/api/groups/join", {
            method: "POST",
            headers: buildHeaders(true),
            body: JSON.stringify({ account, invite_link: link }),
          });
          applyJoinResult(key, data);
          joinActionResult.value = `当前账号加群进行中：${joinProgress.completed} / ${joinProgress.total}，成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`;
          await nextTick();
        }
        joinLinksText.value = "";
        await fetchGroups(true, true).catch(() => {});
        finishJoinProgress();
        joinActionResult.value = `当前账号加群完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`;
        setNotice("success", `当前账号加群完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`);
      } catch (error) {
        finishJoinProgress();
        joinActionResult.value = `当前账号加群失败：${error.message}`;
        setNotice("error", `当前账号加群失败: ${error.message}`);
      } finally {
        loading.groupJoin = false;
      }
    }

    async function batchJoinGroups() {
      loading.groupJoin = true;
      try {
        const links = parseLinks(joinLinksText.value);
        if (!links.length) throw new Error("请输入有效群组链接");
        const targetAccounts = currentJoinAccounts();
        if (!targetAccounts.length) throw new Error("当前没有可用于加群的账号");
        const distributedPlanRows = [];
        const assignments = links.map((link, index) => ({
          key: joinProgressKey(targetAccounts[index % targetAccounts.length], link, index),
          account: targetAccounts[index % targetAccounts.length],
          invite_link: link,
          delayMs: index * 5000,
        }));
        startJoinProgress("batch", assignments);
        joinActionResult.value = `开始执行：${targetAccounts.length} 个账号分配加入 ${links.length} 个链接`;
        await Promise.all(assignments.map(async (item) => {
          if (item.delayMs > 0) {
            updateJoinProgressRow(item.key, { status: "等待中", detail: `等待 ${(item.delayMs / 1000).toFixed(0)} 秒后开始` });
            await new Promise((resolve) => setTimeout(resolve, item.delayMs));
          }
          await tryDistributedJoinWithFallback(item, targetAccounts, distributedPlanRows);
          joinActionResult.value = `批量加群进行中：${joinProgress.completed} / ${joinProgress.total}，成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`;
          await nextTick();
        }));
        saveDistributedJoinPlan(distributedPlanRows);
        joinLinksText.value = "";
        await fetchGroups(true, true).catch(() => {});
        finishJoinProgress();
        joinActionResult.value = `批量加群完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}；已记录最近分配加入群 ${distributedPlanRows.length} 个`;
        setNotice("success", `批量加群完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}；已记录最近分配加入群 ${distributedPlanRows.length} 个`);
      } catch (error) {
        finishJoinProgress();
        joinActionResult.value = `多账号分配加群失败：${error.message}`;
        setNotice("error", `多账号分配加群失败: ${error.message}`);
      } finally {
        loading.groupJoin = false;
      }
    }

    async function joinAllAccounts() {
      loading.groupJoin = true;
      try {
        const links = parseLinks(joinLinksText.value);
        if (!links.length) throw new Error("请输入有效群组链接");
        const targetAccounts = currentJoinAccounts();
        if (!targetAccounts.length) throw new Error("当前没有可用于加群的账号");
        startJoinProgress("all", links.flatMap((link, linkIndex) => targetAccounts.map((account, accountIndex) => ({
          key: joinProgressKey(account, link, (linkIndex * targetAccounts.length) + accountIndex),
          account,
          invite_link: link,
        }))));
        joinActionResult.value = `开始执行：${targetAccounts.length} 个账号加入同一批群组，请稍候...`;
        for (const link of links) {
          joinProgress.current = `全部账号 -> ${link}`;
          joinProgress.rows
            .filter((item) => item.invite_link === link && item.status === "等待中")
            .forEach((item) => {
              item.status = "进行中";
              item.detail = "当前链接执行中";
            });
          const data = await request("/api/groups/join-all-accounts", {
            method: "POST",
            headers: buildHeaders(true),
            body: JSON.stringify({ invite_link: link, accounts: targetAccounts, delay_ms: 3000 }),
          });
          (data.results || []).forEach((item, index) => {
            applyJoinResult(joinProgressKey(item.account, link, index), item);
          });
          joinActionResult.value = `全部账号加群进行中：${joinProgress.completed} / ${joinProgress.total}，成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`;
          await nextTick();
        }
        joinLinksText.value = "";
        await fetchGroups(true, true).catch(() => {});
        finishJoinProgress();
        joinActionResult.value = `全部账号加入同一群完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`;
        setNotice("success", `全部账号加群已完成：成功 ${joinProgress.success}，已加入 ${joinProgress.alreadyJoined}，失败 ${joinProgress.failed}`);
      } catch (error) {
        finishJoinProgress();
        joinActionResult.value = `全部账号加入同一群失败：${error.message}`;
        setNotice("error", `全部账号加群失败: ${error.message}`);
      } finally {
        loading.groupJoin = false;
      }
    }

    async function clearGroupCache() {
      try {
        await request(`/api/groups/cache/clear?account=${encodeURIComponent(selectedAccount.value)}`, {
          headers: buildHeaders(),
        });
        setNotice("success", "群组缓存已清理");
        await fetchGroups(true);
      } catch (error) {
        setNotice("error", `清理缓存失败: ${error.message}`);
      }
    }

    async function clearLogs() {
      try {
        await request("/api/logs/clear", {
          method: "POST",
          headers: buildHeaders(),
        });
        setNotice("success", "日志已清空");
        await fetchLogs();
      } catch (error) {
        setNotice("error", `清空日志失败: ${error.message}`);
      }
    }

    async function controlTask(taskId, action) {
      try {
        await request("/api/task-control", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ task_id: taskId, action }),
        });
        setNotice("success", `任务${formatActionLabel(action)}已提交`);
        await fetchTasksSummary(true);
      } catch (error) {
        setNotice("error", `任务操作失败: ${error.message}`);
      }
    }

    async function stopAllTasks() {
      try {
        await request("/api/tasks/stop-all", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ account: selectedAccount.value }),
        });
        setNotice("success", "当前账号运行中的任务已请求停止");
        await fetchTasksSummary(true);
      } catch (error) {
        setNotice("error", `停止任务失败: ${error.message}`);
      }
    }

    async function deleteTask(taskId) {
      try {
        await request("/api/tasks/delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ task_id: taskId }),
        });
        setNotice("success", `任务 ${taskId} 已删除`);
        await fetchTasksSummary(true);
      } catch (error) {
        setNotice("error", `删除任务失败: ${error.message}`);
      }
    }

    async function clearAllTasks() {
      try {
        const data = await request("/api/tasks/clear", {
          method: "POST",
          headers: buildHeaders(),
        });
        setNotice("success", `已清空全部任务 ${data.tasks_deleted || 0} 条，事件 ${data.events_deleted || 0} 条`);
        await fetchTasksSummary(true);
      } catch (error) {
        setNotice("error", `清空全部任务失败: ${error.message}`);
      }
    }

    function ensureSendPayload() {
      const ids = selectedGroupIds();
      if (!ids.length) throw new Error("请先选择至少一个可发送群组");
      if (!sendForm.message.trim()) throw new Error("请输入消息内容");
      return {
        group_ids: ids,
        message: sendForm.message,
        parse_mode: sendForm.parse_mode,
        disable_web_page_preview: sendForm.disable_web_page_preview,
        delay_ms: Number(sendForm.delay_ms) || 0,
        rounds: Math.max(1, Number(sendForm.rounds) || 1),
        round_interval_s: Math.max(0, Number(sendForm.round_interval_s) || 0),
        account: selectedAccount.value,
        request_id: `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      };
    }

    async function pollTask(taskId) {
      for (;;) {
        const data = await request(`/api/task-status?task_id=${encodeURIComponent(taskId)}`, {
          headers: buildHeaders(),
        });
        sendResult.value = `任务 ${taskId}\n状态: ${formatStatusLabel(data.status)}\n成功 ${data.success || 0} / 失败 ${data.failed || 0}\n总体 ${data.overall_completed || 0} / ${data.overall_planned || data.total || 0}\n当前轮 ${data.current_round || 0} / ${data.rounds || 0}`;
        if (["done", "stopped", "error"].includes(data.status)) break;
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      await fetchDashboard(true);
    }

    async function startSend() {
      loading.send = true;
      try {
        const payload = ensureSendPayload();
        setNotice("info", `开始创建单账号群发任务：${payload.group_ids.length} 个群`);
        const data = await request("/api/send-async", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify(payload),
        });
        sendResult.value = `任务已创建: ${data.task_id}`;
        setNotice("success", `任务已创建：${data.task_id}`);
        await pollTask(data.task_id);
      } catch (error) {
        sendResult.value = `发送失败: ${error.message}`;
        setNotice("error", `发送失败: ${error.message}`);
      } finally {
        loading.send = false;
      }
    }

    async function testSend() {
      loading.send = true;
      try {
        const payload = ensureSendPayload();
        setNotice("info", `开始测试发送：${payload.group_ids.length} 个群`);
        const data = await request("/api/test-send", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify(payload),
        });
        sendResult.value = `测试完成：总数 ${data.total || 0}，成功 ${data.success || 0}，失败 ${data.failed || 0}`;
        setNotice("success", `测试完成：成功 ${data.success || 0}，失败 ${data.failed || 0}`);
        await fetchLogs();
      } catch (error) {
        sendResult.value = `测试失败: ${error.message}`;
        setNotice("error", `测试失败: ${error.message}`);
      } finally {
        loading.send = false;
      }
    }

    async function startBatchSend() {
      loading.send = true;
      try {
        const batchAccounts = activeAuthorizedAccounts();
        if (!batchAccounts.length) {
          throw new Error("当前没有已授权账号可用于批量群发");
        }
        const basePayload = {
          message: sendForm.message,
          parse_mode: sendForm.parse_mode,
          disable_web_page_preview: sendForm.disable_web_page_preview,
          delay_ms: Number(sendForm.delay_ms) || 0,
          rounds: Math.max(1, Number(sendForm.rounds) || 1),
          round_interval_s: Math.max(0, Number(sendForm.round_interval_s) || 0),
          request_id: `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        };
        let payload;
        if (sendForm.batch_mode === "distributed_join") {
          const rows = distributedJoinPlanRows.value;
          if (!rows.length) throw new Error("最近没有可用的多账号分配加入结果，请先执行一次“多账号分配加入”");
          const distributionMap = {};
          rows.forEach((item) => {
            if (!batchAccounts.includes(item.account)) return;
            if (!distributionMap[item.account]) distributionMap[item.account] = [];
            distributionMap[item.account].push(Number(item.group_id));
          });
          const plannedGroups = Object.values(distributionMap).reduce((sum, item) => sum + item.length, 0);
          if (!plannedGroups) throw new Error("最近分配加入结果里没有当前可用账号对应的群");
          payload = {
            ...basePayload,
            accounts: batchAccounts,
            strategy: "distributed_join_only",
            distribution_map: distributionMap,
          };
          setNotice("info", `开始创建按最近分配加入结果群发：${Object.keys(distributionMap).length} 个账号，${plannedGroups} 个账号-群目标`);
        } else {
          const directPayload = ensureSendPayload();
          payload = {
            ...basePayload,
            group_ids: directPayload.group_ids,
            accounts: batchAccounts,
          };
          setNotice("info", `开始创建批量群发任务：${batchAccounts.length} 个账号，${directPayload.group_ids.length} 个群`);
        }
        const data = await request("/api/send-async-batch", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({
            ...payload,
            delay_scope: "per_account",
            stagger_min_s: 0,
            stagger_max_s: 1,
          }),
        });
        const strategyText = data.strategy === "distributed_join_only" ? "按最近分配加入结果" : "全群广播";
        sendResult.value = `批量任务已创建：${data.tasks?.length || data.accounts_count || 0} 个账号任务，模式：${strategyText}`;
        setNotice("success", `批量任务已创建：${data.tasks?.length || data.accounts_count || 0} 个账号任务，模式：${strategyText}`);
        await fetchTasksSummary(true);
      } catch (error) {
        sendResult.value = `批量发送失败: ${error.message}`;
        setNotice("error", `批量发送失败: ${error.message}`);
      } finally {
        loading.send = false;
      }
    }

    async function verifyProtocolAccounts() {
      try {
        if (!protocolAccounts.value.length) throw new Error("请先上传协议号");
        const results = [];
        for (const account of protocolAccounts.value) {
          try {
            const data = await request("/api/accounts/check-single", {
              method: "POST",
              headers: buildHeaders(true),
              body: JSON.stringify({ account }),
            });
            results.push(data);
          } catch (error) {
            results.push({ account, valid: false, error: error.message });
          }
        }
        protocolValidationResults.value = results;
        protocolValidAccounts.value = results.filter((item) => item.valid).map((item) => item.account);
        protocolResult.value = `验证完成：可用 ${protocolValidAccounts.value.length} / ${protocolAccounts.value.length}`;
      } catch (error) {
        setNotice("error", `验证协议号失败: ${error.message}`);
      }
    }

    async function uploadProtocolFiles() {
      try {
        const data = await uploadFiles(protocolUploadInput, protocolProxyBindingsText.value);
        protocolAccounts.value = data.validated_accounts || [];
        uploadedAccounts.value = protocolAccounts.value.slice();
        persistUploadedAccounts();
        protocolValidAccounts.value = [];
        protocolValidationResults.value = [];
        protocolResult.value = `已上传 ${data.uploaded || 0} 个文件，解析出 ${protocolAccounts.value.length} 个账号`;
        if (Array.isArray(data.errors) && data.errors.length) {
          protocolResult.value += `\n警告:\n${data.errors.map((item) => translateText(item)).join("\n")}`;
        }
        if (protocolUploadInput.value) protocolUploadInput.value.value = "";
        protocolProxyBindingsText.value = defaultProxySourceText.value;
        await fetchAccounts();
        setNotice("success", `协议号上传完成：解析出 ${protocolAccounts.value.length} 个账号`);
      } catch (error) {
        setNotice("error", `上传协议号失败: ${error.message}`);
      }
    }

    async function assignProtocols() {
      try {
        if (!protocolValidAccounts.value.length) throw new Error("没有验证通过的账号");
        const data = await request("/api/accounts/assign-sequence", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: protocolValidAccounts.value }),
        });
        protocolResult.value = `加入账号序列完成：已分配 ${data.assigned?.length || 0} 个`;
        await fetchAccounts();
      } catch (error) {
        setNotice("error", `加入账号序列失败: ${error.message}`);
      }
    }

    async function batchJoinWithProtocols() {
      try {
        const joinAccounts = activeBatchJoinAccounts();
        if (!joinAccounts.length) throw new Error("请先上传账号；如果你做过验证，会优先使用验证通过账号");
        const links = parseLinks(protocolLinksText.value);
        if (!links.length) throw new Error("请输入有效群组链接");
        loading.protocolJoin = true;
        let success = 0;
        let failed = 0;
        for (let i = 0; i < links.length; i += 1) {
          const link = links[i];
          for (let j = 0; j < joinAccounts.length; j += 1) {
            const account = joinAccounts[j];
            try {
              const data = await request("/api/groups/join", {
                method: "POST",
                headers: buildHeaders(true),
                body: JSON.stringify({ account, invite_link: link }),
              });
              if (data.ok || data.already_joined) success += 1;
              else failed += 1;
            } catch {
              failed += 1;
            }
            if (j < joinAccounts.length - 1) {
              await new Promise((resolve) => setTimeout(resolve, protocolJoin.accountDelay * 1000));
            }
          }
          if (i < links.length - 1) {
            await new Promise((resolve) => setTimeout(resolve, protocolJoin.groupDelay * 1000));
          }
        }
        protocolJoinResult.value = `批量加群完成：成功 ${success}，失败 ${failed}。本次使用账号 ${joinAccounts.length} 个。`;
      } catch (error) {
        setNotice("error", `批量加群失败: ${error.message}`);
      } finally {
        loading.protocolJoin = false;
      }
    }

    async function checkAccounts() {
      loading.checkAccounts = true;
      // #region debug-point B:health-check-click
      const startedAt = Date.now();
      reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:start", "checkAccounts.start", {
        cachedProfiles: authorizedProfiles.value.length,
        accounts: accounts.value.length,
      });
      // #endregion
      try {
        let rows = syncHealthRows();
        if (!rows.length && !accounts.value.length) {
          await fetchAccounts();
          rows = syncHealthRows();
        }
        // #region debug-point B:health-check-rows
        reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:rows", "checkAccounts.rows-ready", {
          elapsedMs: Date.now() - startedAt,
          rows: rows.length,
        });
        // #endregion
        if (!rows.length) {
          throw new Error("当前没有已授权账号可检查");
        }
        setNotice("info", `健康检查已启动：共 ${rows.length} 个账号，正在逐个检测`);
        let completed = 0;
        let nextIndex = 0;
        const runOne = async (item) => {
          applyHealthCheckResult(item.account, {
            valid: false,
            status: "checking",
            can_send_in_groups: null,
            detail: "检查中...",
          });
          await nextTick();
          try {
            const itemStartedAt = Date.now();
            reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:item-start", "checkAccounts.item-start", {
              account: item.account,
              completed,
            });
            const data = await request("/api/accounts/check-single", {
              method: "POST",
              headers: buildHeaders(true),
              body: JSON.stringify({ account: item.account, include_group_send_check: true }),
            });
            applyHealthCheckResult(item.account, data);
            reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:item-success", "checkAccounts.item-success", {
              account: item.account,
              elapsedMs: Date.now() - itemStartedAt,
              status: data.status,
              valid: data.valid,
              canSend: data.can_send_in_groups,
            });
          } catch (error) {
            applyHealthCheckResult(item.account, {
              valid: false,
              status: "error",
              can_send_in_groups: false,
              detail: error.message,
            });
            reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:item-error", "checkAccounts.item-error", {
              account: item.account,
              error: error.message,
            });
          }
          completed += 1;
          setNotice("info", `健康检查进行中：${completed} / ${rows.length}`);
        };
        const workerCount = Math.min(HEALTH_CHECK_WORKERS, rows.length);
        const workers = Array.from({ length: workerCount }, async () => {
          while (nextIndex < rows.length) {
            const item = rows[nextIndex];
            nextIndex += 1;
            await runOne(item);
          }
        });
        await Promise.all(workers);
        const invalidCount = accountCheckResults.value.filter((item) => !item.valid || item.can_send_in_groups === false).length;
        // #region debug-point B:health-check-finish
        reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:finish", "checkAccounts.finish", {
          elapsedMs: Date.now() - startedAt,
          rows: rows.length,
          invalidCount,
        });
        // #endregion
        setNotice("success", `健康检查完成：共 ${rows.length} 个账号，失效账号 ${invalidCount} 个`);
      } catch (error) {
        // #region debug-point B:health-check-error
        reportHealthCheckDebug("B", "static/vue-admin.js:checkAccounts:error", "checkAccounts.error", {
          elapsedMs: Date.now() - startedAt,
          error: error.message,
        });
        // #endregion
        setNotice("error", `健康检查失败: ${error.message}`);
      } finally {
        loading.checkAccounts = false;
      }
    }

    async function clearNoSendAccounts() {
      loading.clearNoSendAccounts = true;
      try {
        const targets = accountCheckResults.value
          .filter((item) => item.status === "失效账号" && item.valid === true && item.can_send_in_groups === false)
          .map((item) => item.account)
          .filter(Boolean);
        if (!targets.length) throw new Error("当前没有可清理的群内不可发账号");
        const data = await request("/api/accounts/bulk-delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: targets }),
        });
        const deletedAccounts = Array.isArray(data.results)
          ? data.results.filter((item) => item.deleted).map((item) => item.account).filter(Boolean)
          : targets;
        removeHealthRows(deletedAccounts);
        setNotice("success", `已清理 ${deletedAccounts.length} 个群内不可发账号`);
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `清理群内不可发账号失败: ${error.message}`);
      } finally {
        loading.clearNoSendAccounts = false;
      }
    }

    async function deleteAuthorizedAccount(account) {
      loading.deleteAccounts = true;
      try {
        const data = await request("/api/accounts/bulk-delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: [account] }),
        });
        const row = Array.isArray(data.results) ? data.results[0] : null;
        removeHealthRows([account]);
        setNotice(
          "success",
          `账号 ${account} 已删除，同时清理任务 ${row?.task_records_deleted || 0} 条，事件 ${row?.task_events_deleted || 0} 条`
        );
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `删除账号失败: ${error.message}`);
      } finally {
        loading.deleteAccounts = false;
      }
    }

    async function deleteAllAuthorizedAccounts() {
      loading.deleteAccounts = true;
      try {
        let names = collectKnownAccountNames();
        if (!names.length) {
          await fetchAccounts(false);
          names = collectKnownAccountNames();
        }
        if (!names.length) {
          await fetchAuthorizedProfiles(false, false);
          names = collectKnownAccountNames();
        }
        if (!names.length) throw new Error("当前没有可删除账号");
        const data = await request("/api/accounts/bulk-delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: names }),
        });
        const results = Array.isArray(data.results) ? data.results : [];
        const deletedAccounts = results.filter((item) => item.deleted).map((item) => item.account).filter(Boolean);
        const taskRecordsDeleted = results.reduce((sum, item) => sum + Number(item.task_records_deleted || 0), 0);
        const taskEventsDeleted = results.reduce((sum, item) => sum + Number(item.task_events_deleted || 0), 0);
        removeHealthRows(deletedAccounts.length ? deletedAccounts : names);
        setNotice("success", `当前账号已清空，同时清理任务 ${taskRecordsDeleted} 条，事件 ${taskEventsDeleted} 条`);
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `清空当前账号失败: ${error.message}`);
      } finally {
        loading.deleteAccounts = false;
      }
    }

    async function clearUnauthorizedAccounts() {
      loading.deleteAccounts = true;
      try {
        let names = collectUnauthorizedAccountNames();
        if (!names.length) {
          await fetchAccounts(false);
          names = collectUnauthorizedAccountNames();
        }
        if (!names.length) throw new Error("当前没有可清理的未授权账号");
        const data = await request("/api/accounts/bulk-delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: names }),
        });
        const results = Array.isArray(data.results) ? data.results : [];
        const deletedAccounts = results.filter((item) => item.deleted).map((item) => item.account).filter(Boolean);
        removeHealthRows(deletedAccounts.length ? deletedAccounts : names);
        setNotice("success", `已清理 ${deletedAccounts.length || names.length} 个未授权账号`);
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `清理未授权账号失败: ${error.message}`);
      } finally {
        loading.deleteAccounts = false;
      }
    }

    async function clearInvalidAccounts() {
      loading.clearInvalidAccounts = true;
      try {
        const invalid = accountCheckResults.value
          .filter((item) => item.status === "失效账号")
          .map((item) => item.account)
          .filter(Boolean);
        if (!invalid.length) throw new Error("当前没有可清理失效账号");
        const data = await request("/api/accounts/bulk-delete", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ accounts: invalid }),
        });
        const deletedAccounts = Array.isArray(data.results)
          ? data.results.filter((item) => item.deleted).map((item) => item.account).filter(Boolean)
          : invalid;
        removeHealthRows(deletedAccounts);
        setNotice("success", `已清理 ${deletedAccounts.length} 个失效账号`);
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `清理失效账号失败: ${error.message}`);
      } finally {
        loading.clearInvalidAccounts = false;
      }
    }

    async function resetSystem() {
      try {
        const data = await request(`/api/system/reset?sessions=${resetSessions.value ? "true" : "false"}`, {
          method: "POST",
          headers: buildHeaders(),
        });
        setNotice("success", `系统已重置${resetSessions.value ? `，删除 Session ${data.deleted_sessions || 0} 个` : ""}`);
        await fetchDashboard(true);
      } catch (error) {
        setNotice("error", `系统重置失败: ${error.message}`);
      }
    }

    watch(selectedIds, persistSelectedIds, { deep: true });

    onMounted(async () => {
      restoreSelectedIds();
      loadUploadedAccountsCache();
      loadDistributedJoinPlan();
      scheduleLogsAutoRefresh(3000);
      if (token.value) {
        await loadDefaultProxySource(true).catch(() => {});
        await fetchDashboard(false);
      }
    });

    onBeforeUnmount(() => {
      stopLogsAutoRefresh();
    });

    return {
      navItems,
      ui,
      loading,
      notice,
      tokenInput,
      tokenStatus,
      tokenStatusClass,
      accounts,
      selectedAccount,
      authStatusText,
      groups,
      filteredGroups,
      groupSearch,
      includeChannels,
      selectedIds: computed(() => selectedIds.value),
      groupLoadError,
      tasksSummary,
      tasks,
      logs,
      authorizedProfiles,
      accountCheckResults,
      login,
      profile,
      joinLinksText,
      joinActionResult,
      joinProgress,
      joinProgressPercent,
      sessionProxyBindingsText,
      protocolProxyBindingsText,
      proxyForm,
      proxySaveResult,
      sendForm,
      sendResult,
      distributedJoinPlan,
      distributedJoinPlanGroupCount,
      distributedJoinPlanAccountCount,
      uploadedAccounts,
      protocolAccounts,
      protocolValidAccounts,
      protocolValidationResults,
      protocolResult,
      protocolLinksText,
      protocolJoin,
      protocolJoinResult,
      resetSessions,
      sessionUploadInput,
      protocolUploadInput,
      runningTaskCount,
      authorizedAccountCount,
      unauthorizedAccountCount,
      selectedGroupCount,
      invalidAccountCount,
      cannotSendAccountCount,
      canRunJoinActions,
      canRunProtocolJoin,
      canStartSend,
      canStartBatchSend,
      groupPanelStatusText,
      logsAutoRefreshText,
      selectedAccountSummary,
      translateText,
      formatStatusLabel,
      formatLogDetail,
      formatProfileAbout,
      formatTime,
      formatProxy,
      statusClass,
      setPage,
      saveToken,
      fetchDashboard,
      onAccountChange,
      onIncludeChannelsChange,
      fetchGroups,
      fetchTasksSummary,
      fetchLogs,
      fetchAuthorizedProfiles,
      groupBadge,
      isGroupDisabled,
      toggleGroup,
      selectVisible,
      sendLoginCode,
      submitLoginCode,
      loadProxyForSelectedAccount,
      saveProxyForSelectedAccount,
      uploadSessions,
      joinGroupsForCurrentAccount,
      batchJoinGroups,
      joinAllAccounts,
      loadAccountProfile,
      saveAccountProfile,
      clearGroupCache,
      clearLogs,
      controlTask,
      stopAllTasks,
      deleteTask,
      clearAllTasks,
      startSend,
      testSend,
      startBatchSend,
      uploadProtocolFiles,
      verifyProtocolAccounts,
      assignProtocols,
      batchJoinWithProtocols,
      checkAccounts,
      clearInvalidAccounts,
      clearNoSendAccounts,
      deleteAuthorizedAccount,
      deleteAllAuthorizedAccounts,
      clearUnauthorizedAccounts,
      resetSystem,
    };
  },
}).mount("#app");
