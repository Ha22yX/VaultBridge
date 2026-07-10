const state = {
  jobs: [],
  runs: [],
  selectedJobId: null,
  page: "dashboard",
  activeRunId: null,
  runPollTimer: null,
  runCache: {},
};

const $ = (id) => document.getElementById(id);

const defaults = {
  targetPath: "/storage/Files/服务器备份/147.189.128.208",
  includePath: "/www/wwwroot",
  excludePatterns: [".git", "node_modules", ".cache", "cache", "logs", "*.log", "tmp", ".DS_Store"],
};

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}

function lines(value) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function setConnectionStatus(message, kind = "") {
  const node = $("connectionStatus");
  node.textContent = message;
  node.className = `status-text ${kind}`.trim();
}

function formatSchedule(job) {
  const time = `${String(job.hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}`;
  if (job.schedule_kind === "weekly") {
    const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    return `${days[job.day_of_week ?? 0]} ${time}`;
  }
  return `每天 ${time}`;
}

function jobName(jobId) {
  return state.jobs.find((job) => job.id === jobId)?.name || `任务 #${jobId}`;
}

function activeJob() {
  return state.jobs.find((job) => job.id === state.selectedJobId) || state.jobs[0];
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function setPage(page) {
  state.page = page;
  $("dashboardPage").classList.toggle("active-page", page === "dashboard");
  $("versionsPage").classList.toggle("active-page", page === "versions");
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.pageLink === page);
  });
  if (page === "versions") {
    renderVersionJobSelect();
    loadSelectedVersions().catch((error) => toast(`加载失败：${error.message}`));
  }
}

function renderJobs() {
  $("jobCount").textContent = `${state.jobs.length} 个任务`;
  const list = $("jobsList");
  if (!state.jobs.length) {
    list.className = "task-list empty";
    list.textContent = "还没有任务。点击右上角“新建任务”开始配置。";
    return;
  }

  list.className = "task-list";
  list.innerHTML = state.jobs
    .map(
      (job) => `
        <article class="task-row" data-open-job="${job.id}" tabindex="0">
          <div class="task-main">
            <p class="task-title">
              <span class="status-dot ${job.enabled ? "" : "off"}"></span>
              ${escapeHtml(job.name)}
            </p>
            <div class="meta">
              <span>${escapeHtml(job.username)}@${escapeHtml(job.host)}:${job.port}</span>
              <span>${formatSchedule(job)}</span>
              <span>${job.enabled ? "已启用" : "已暂停"}</span>
            </div>
            <div class="meta">
              <span>目录：${escapeHtml(job.include_paths.join(", "))}</span>
              <span>目标：${escapeHtml(job.target_path)}</span>
            </div>
          </div>
          <div class="task-actions">
            <button class="button secondary compact" data-action="test" data-id="${job.id}" type="button">测试</button>
            <button class="button primary compact" data-action="run" data-id="${job.id}" type="button">立即备份</button>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderRuns(targetId = "runsList", runs = state.runs) {
  const list = $(targetId);
  if (!runs.length) {
    list.className = "timeline empty";
    list.textContent = "暂无运行记录。";
    return;
  }
  list.className = "timeline";
  runs.forEach((run) => {
    state.runCache[run.id] = run;
  });
  list.innerHTML = runs
    .map(
      (run) => `
        <div class="run-row" data-run-id="${run.id}" tabindex="0">
          <strong>${escapeHtml(statusLabel(run.status))} · ${escapeHtml(jobName(run.job_id))}</strong>
          <p>${escapeHtml(run.started_at || "")}${run.finished_at ? ` 至 ${escapeHtml(run.finished_at)}` : ""}</p>
          <p>${escapeHtml(run.message || "无消息")}</p>
          <p>${escapeHtml(run.commit_hash ? `提交 ${run.commit_hash.slice(0, 12)}` : progressSummary(run))}</p>
        </div>
      `,
    )
    .join("");
}

function statusLabel(status) {
  const labels = {
    running: "运行中",
    success: "已完成",
    failed: "失败",
    queued: "排队中",
  };
  return labels[status] || status || "未知";
}

function phaseLabel(phase) {
  const labels = {
    starting: "准备开始",
    preparing: "准备本地仓库",
    connecting: "连接服务器",
    scanning: "扫描文件",
    syncing: "同步文件",
    committing: "写入 Git 版本",
    success: "已完成",
    failed: "失败",
    interrupted: "已中断",
  };
  return labels[phase] || phase || "等待状态";
}

function progressSummary(run) {
  const copied = Number(run.copied_files || 0);
  const total = Number(run.total_files || 0);
  if (total > 0) return `${copied}/${total} 个文件`;
  if (run.phase) return phaseLabel(run.phase);
  return "暂无提交";
}

function progressForRun(run) {
  const total = Number(run.total_files || 0);
  const copied = Number(run.copied_files || 0);
  const phase = run.phase || "";
  if (run.status === "success") {
    return { percent: 100, text: "备份完成", failed: false };
  }
  if (run.status === "failed") {
    return { percent: 100, text: "备份失败", failed: true };
  }
  if (phase === "scanning") {
    return { percent: 12, text: `正在扫描文件：已发现 ${total} 个`, failed: false };
  }
  if (phase === "syncing" && total > 0) {
    const percent = Math.max(15, Math.min(92, Math.round((copied / total) * 100)));
    return { percent, text: `正在同步：${copied}/${total} 个文件`, failed: false };
  }
  if (phase === "committing") {
    return { percent: 96, text: "正在写入 Git 版本", failed: false };
  }
  if (phase === "connecting") {
    return { percent: 6, text: "正在连接服务器", failed: false };
  }
  if (phase === "preparing") {
    return { percent: 4, text: "正在准备本地仓库", failed: false };
  }
  return { percent: 2, text: phaseLabel(phase), failed: false };
}

function runById(runId) {
  return state.runs.find((run) => run.id === runId) || state.runCache[runId];
}

function jobById(jobId) {
  return state.jobs.find((job) => job.id === jobId);
}

function renderRunDialog(run) {
  const job = jobById(run.job_id);
  const progress = progressForRun(run);
  const copiedFiles = Number(run.copied_files || 0);
  const totalFiles = Number(run.total_files || 0);
  const copiedBytes = Number(run.copied_bytes || 0);
  const totalBytes = Number(run.total_bytes || 0);
  $("runDialogTitle").textContent = `${job ? job.name : `任务 #${run.job_id}`} · ${statusLabel(run.status)}`;
  $("runStatusBadge").textContent = statusLabel(run.status);
  $("runProgressText").textContent = progress.text;
  $("runProgressBar").style.width = `${progress.percent}%`;
  $("runProgressBar").classList.toggle("failed", progress.failed);
  $("runDetailBody").innerHTML = `
    <div class="detail-cell">
      <span>任务名称</span>
      <strong>${escapeHtml(job ? job.name : `任务 #${run.job_id}`)}</strong>
    </div>
    <div class="detail-cell">
      <span>当前阶段</span>
      <strong>${escapeHtml(phaseLabel(run.phase))}</strong>
    </div>
    <div class="detail-cell">
      <span>文件进度</span>
      <strong>${copiedFiles}/${totalFiles || "未知"} 个文件</strong>
    </div>
    <div class="detail-cell">
      <span>大小进度</span>
      <strong>${formatBytes(copiedBytes)} / ${totalBytes ? formatBytes(totalBytes) : "未知"}</strong>
    </div>
    <div class="detail-cell">
      <span>开始时间</span>
      <strong>${escapeHtml(run.started_at || "未记录")}</strong>
    </div>
    <div class="detail-cell">
      <span>结束时间</span>
      <strong>${escapeHtml(run.finished_at || "尚未结束")}</strong>
    </div>
    <div class="detail-cell">
      <span>提交版本</span>
      <strong>${escapeHtml(run.commit_hash || "暂无提交")}</strong>
    </div>
    <div class="detail-cell">
      <span>计划时间</span>
      <strong>${escapeHtml(job ? formatSchedule(job) : "未知")}</strong>
    </div>
    <div class="detail-cell wide">
      <span>当前路径</span>
      <p>${escapeHtml(run.current_path || "尚未开始读取文件")}</p>
    </div>
    <div class="detail-cell wide">
      <span>备份目录</span>
      <p>${escapeHtml(job ? job.include_paths.join("\\n") : "未知")}</p>
    </div>
    <div class="detail-cell wide">
      <span>目标目录</span>
      <p>${escapeHtml(job ? job.target_path : "未知")}</p>
    </div>
    <div class="detail-cell wide">
      <span>运行消息</span>
      <p>${escapeHtml(run.message || "暂无消息")}</p>
    </div>
  `;
}

async function refreshRunDialog() {
  if (!state.activeRunId) return;
  const current = runById(state.activeRunId);
  if (!current) return;
  const latestRuns = await api(`/api/runs?job_id=${current.job_id}`);
  const latest = latestRuns.find((run) => run.id === state.activeRunId);
  if (!latest) return;
  state.runCache[latest.id] = latest;
  state.runs = state.runs.map((run) => (run.id === latest.id ? latest : run));
  renderRunDialog(latest);
  renderRuns();
  if (state.page === "versions") {
    renderRuns("versionRunsList", latestRuns);
  }
  if (latest.status !== "running") {
    stopRunPolling();
  }
}

function stopRunPolling() {
  if (state.runPollTimer) {
    clearInterval(state.runPollTimer);
    state.runPollTimer = null;
  }
}

function openRunDialog(runId) {
  const run = runById(runId);
  if (!run) return;
  state.activeRunId = runId;
  renderRunDialog(run);
  $("runDialog").showModal();
  stopRunPolling();
  if (run.status === "running") {
    state.runPollTimer = setInterval(() => {
      refreshRunDialog().catch((error) => toast(`刷新运行详情失败：${error.message}`));
    }, 3000);
  }
}

function closeRunDialog() {
  stopRunPolling();
  state.activeRunId = null;
  $("runDialog").close();
}

function renderVersionJobSelect() {
  const select = $("versionsJobSelect");
  if (!state.jobs.length) {
    select.innerHTML = `<option value="">暂无任务</option>`;
    return;
  }
  if (!state.selectedJobId) {
    state.selectedJobId = state.jobs[0].id;
  }
  select.innerHTML = state.jobs
    .map((job) => `<option value="${job.id}" ${job.id === state.selectedJobId ? "selected" : ""}>${escapeHtml(job.name)}</option>`)
    .join("");
}

function renderVersions(versions) {
  const list = $("versionsList");
  if (!versions.length) {
    list.className = "version-list empty";
    list.textContent = "这个任务还没有备份版本。运行一次备份后会显示在这里。";
    return;
  }
  list.className = "version-list";
  list.innerHTML = versions
    .map(
      (version) => `
        <div class="version-row">
          <div>
            <strong>${escapeHtml(version.date)}</strong>
            <p>${escapeHtml(version.subject)} · ${escapeHtml(version.commit.slice(0, 12))}</p>
          </div>
          <a class="button primary compact" href="/api/jobs/${state.selectedJobId}/versions/${version.commit}/download">下载 zip</a>
        </div>
      `,
    )
    .join("");
}

function resetDialog() {
  $("jobForm").reset();
  $("jobId").value = "";
  $("dialogTitle").textContent = "新建任务";
  $("deleteFromDialogBtn").hidden = true;
  $("password").required = true;
  $("password").type = "password";
  $("showPassword").checked = false;
  $("targetPath").value = defaults.targetPath;
  $("includePaths").value = defaults.includePath;
  $("port").value = 22;
  $("username").value = "root";
  $("timeOfDay").value = "03:00";
  $("enabled").checked = true;
  setConnectionStatus("保存前可先测试 SSH 连接。");
}

function openJobDialog(job = null) {
  resetDialog();
  if (job) {
    $("jobId").value = job.id;
    $("dialogTitle").textContent = `编辑：${job.name}`;
    $("deleteFromDialogBtn").hidden = false;
    $("password").required = false;
    $("name").value = job.name;
    $("host").value = job.host;
    $("port").value = job.port;
    $("username").value = job.username;
    $("password").value = job.password || "";
    $("targetPath").value = job.target_path;
    $("includePaths").value = job.include_paths.join("\n");
    $("scheduleKind").value = job.schedule_kind;
    $("dayOfWeek").value = job.day_of_week ?? 0;
    $("timeOfDay").value = `${String(job.hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}`;
    $("enabled").checked = job.enabled;
    setConnectionStatus("已载入当前 SSH 密码，可直接测试或修改。");
  }
  $("jobDialog").showModal();
  $("name").focus();
}

async function openJobById(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  openJobDialog(job);
}

function closeJobDialog() {
  $("jobDialog").close();
}

function formPayload() {
  const [hour, minute] = $("timeOfDay").value.split(":").map(Number);
  const payload = {
    name: $("name").value.trim(),
    host: $("host").value.trim(),
    port: Number($("port").value),
    username: $("username").value.trim(),
    target_path: $("targetPath").value.trim(),
    include_paths: lines($("includePaths").value),
    exclude_patterns: defaults.excludePatterns,
    schedule_kind: $("scheduleKind").value,
    day_of_week: Number($("dayOfWeek").value),
    hour,
    minute,
    enabled: $("enabled").checked,
  };
  if ($("password").value) {
    payload.password = $("password").value;
  }
  return payload;
}

function validatePayload(payload, isNew) {
  if (!payload.name) return "请填写任务名称。";
  if (!payload.host) return "请填写 SSH 地址。";
  if (!payload.username) return "请填写用户名。";
  if (!payload.target_path) return "请填写备份目标目录。";
  if (!payload.include_paths.length) return "请至少填写一个备份目录。";
  if (isNew && !$("password").value) return "新任务必须填写 SSH 密码。";
  return "";
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  if (!state.selectedJobId && state.jobs[0]) {
    state.selectedJobId = state.jobs[0].id;
  }
  renderJobs();
  renderVersionJobSelect();
}

async function loadRuns() {
  state.runs = await api("/api/runs");
  renderRuns();
}

async function loadSelectedVersions() {
  const job = activeJob();
  if (!job) {
    $("versionsList").className = "version-list empty";
    $("versionsList").textContent = "暂无任务。";
    $("versionRunsList").className = "timeline empty";
    $("versionRunsList").textContent = "暂无任务。";
    return;
  }
  const [versions, runs] = await Promise.all([
    api(`/api/jobs/${job.id}/versions`),
    api(`/api/runs?job_id=${job.id}`),
  ]);
  renderVersions(versions);
  renderRuns("versionRunsList", runs);
}

async function saveJob(event) {
  event.preventDefault();
  const jobId = $("jobId").value;
  const payload = formPayload();
  const validation = validatePayload(payload, !jobId);
  if (validation) {
    toast(validation);
    return;
  }
  try {
    const saved = jobId
      ? await api(`/api/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(payload) })
      : await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    state.selectedJobId = saved.id;
    await Promise.all([loadJobs(), loadRuns()]);
    closeJobDialog();
    toast("任务已保存。");
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

async function testDialogConnection() {
  const payload = formPayload();
  if (!payload.host || !payload.username) {
    setConnectionStatus("请先填写 SSH 地址和用户名。", "bad");
    return;
  }
  if (!$("password").value) {
    setConnectionStatus("测试连接需要填写密码。", "bad");
    return;
  }
  setConnectionStatus("正在连接 SSH...");
  try {
    await api("/api/ssh/test", {
      method: "POST",
      body: JSON.stringify({
        host: payload.host,
        port: payload.port,
        username: payload.username,
        password: $("password").value,
      }),
    });
    setConnectionStatus("SSH 连接成功。", "ok");
  } catch (error) {
    setConnectionStatus(error.message, "bad");
  }
}

async function deleteCurrentJob() {
  const jobId = Number($("jobId").value);
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  const yes = window.confirm(`确定删除任务“${job.name}”吗？本操作不删除备份仓库文件。`);
  if (!yes) return;
  await api(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (state.selectedJobId === jobId) state.selectedJobId = state.jobs.find((item) => item.id !== jobId)?.id || null;
  await Promise.all([loadJobs(), loadRuns()]);
  closeJobDialog();
  toast("任务已删除。");
}

async function runJob(jobId) {
  toast("备份已加入后台队列。");
  await api(`/api/jobs/${jobId}/run`, { method: "POST" });
  setTimeout(() => {
    Promise.all([loadRuns(), state.page === "versions" ? loadSelectedVersions() : Promise.resolve()]).catch((error) =>
      toast(`刷新失败：${error.message}`),
    );
  }, 1800);
}

async function testSavedJob(jobId) {
  await api(`/api/jobs/${jobId}/test`, { method: "POST" });
  toast("SSH 连接成功。");
}

function bindEvents() {
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const page = link.dataset.pageLink;
      history.replaceState(null, "", `#${page}`);
      setPage(page);
    });
  });

  $("newJobBtn").addEventListener("click", () => openJobDialog());
  $("refreshBtn").addEventListener("click", async () => {
    await Promise.all([loadJobs(), loadRuns()]);
    toast("已刷新。");
  });
  $("refreshVersionsBtn").addEventListener("click", () => loadSelectedVersions().catch((error) => toast(`刷新失败：${error.message}`)));
  $("versionsJobSelect").addEventListener("change", (event) => {
    state.selectedJobId = Number(event.target.value);
    loadSelectedVersions().catch((error) => toast(`加载失败：${error.message}`));
  });

  $("jobsList").addEventListener("click", (event) => {
    const actionButton = event.target.closest("button[data-action]");
    const row = event.target.closest("[data-open-job]");
    if (actionButton) {
      event.stopPropagation();
      const id = Number(actionButton.dataset.id);
      if (actionButton.dataset.action === "run") runJob(id).catch((error) => toast(`操作失败：${error.message}`));
      if (actionButton.dataset.action === "test") testSavedJob(id).catch((error) => toast(`测试失败：${error.message}`));
      return;
    }
    if (row) {
      openJobById(Number(row.dataset.openJob)).catch((error) => toast(`打开失败：${error.message}`));
    }
  });

  $("jobsList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-open-job]");
    if (!row) return;
    event.preventDefault();
    openJobById(Number(row.dataset.openJob)).catch((error) => toast(`打开失败：${error.message}`));
  });

  ["runsList", "versionRunsList"].forEach((listId) => {
    $(listId).addEventListener("click", (event) => {
      const row = event.target.closest("[data-run-id]");
      if (!row) return;
      openRunDialog(Number(row.dataset.runId));
    });
    $(listId).addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest("[data-run-id]");
      if (!row) return;
      event.preventDefault();
      openRunDialog(Number(row.dataset.runId));
    });
  });

  $("jobForm").addEventListener("submit", saveJob);
  $("testConnectionBtn").addEventListener("click", testDialogConnection);
  $("showPassword").addEventListener("change", () => {
    $("password").type = $("showPassword").checked ? "text" : "password";
  });
  $("deleteFromDialogBtn").addEventListener("click", () => deleteCurrentJob().catch((error) => toast(`删除失败：${error.message}`)));
  $("closeDialogBtn").addEventListener("click", closeJobDialog);
  $("cancelDialogBtn").addEventListener("click", closeJobDialog);
  $("jobDialog").addEventListener("click", (event) => {
    if (event.target === $("jobDialog")) closeJobDialog();
  });
  $("refreshRunDialogBtn").addEventListener("click", () => refreshRunDialog().catch((error) => toast(`刷新失败：${error.message}`)));
  $("closeRunDialogBtn").addEventListener("click", closeRunDialog);
  $("closeRunDialogBottomBtn").addEventListener("click", closeRunDialog);
  $("runDialog").addEventListener("click", (event) => {
    if (event.target === $("runDialog")) closeRunDialog();
  });
}

async function init() {
  bindEvents();
  await Promise.all([loadJobs(), loadRuns()]);
  const hashPage = location.hash.replace("#", "");
  setPage(hashPage === "versions" ? "versions" : "dashboard");
}

init().catch((error) => toast(`初始化失败：${error.message}`));
