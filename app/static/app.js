const state = {
  jobs: [],
  runs: [],
  versions: [],
  selectedJobId: null,
  page: "dashboard",
  activeRunId: null,
  activeVersion: null,
  activeVersionPath: "",
  versionTreeRequestId: 0,
  runCache: {},
  syncTimer: null,
  syncBusy: false,
  highlightRunId: null,
};

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const defaults = {
  targetPath: "/服务器备份/example-site",
  includePath: "/www/wwwroot",
  excludePatterns: [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "site-packages",
    "*.pyc",
    ".cache",
    "cache",
    "logs",
    "*.log",
    "tmp",
    ".DS_Store",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.zip",
    "*.7z",
    "*.rar",
    "*.bak",
    "*.dump",
    "*.sql.gz",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.db-shm",
    "*.db-wal",
  ],
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

function escapeHtml(value) {
  return String(value ?? "")
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

function formatClock() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function setConnectionStatus(message, kind = "") {
  const node = $("connectionStatus");
  node.textContent = message;
  node.className = `status-text ${kind}`.trim();
}

function setLiveStatus(message, syncing = false) {
  const node = $("liveStatus");
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("syncing", syncing);
}

function formatSchedule(job) {
  const time = `${String(job.hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}`;
  if (job.schedule_kind === "weekly") {
    const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    return `${days[job.day_of_week ?? 0]} ${time}`;
  }
  return `每天 ${time}`;
}

function statusLabel(status) {
  const labels = {
    running: "运行中",
    paused: "已暂停",
    stopped: "已结束",
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
    estimating: "统计远程文件",
    rsyncing: "增量同步",
    scanning: "扫描文件",
    syncing: "同步文件",
    committing: "写入 Git 版本",
    paused: "已暂停",
    stopped: "已结束",
    success: "已完成",
    failed: "失败",
    interrupted: "已中断",
  };
  return labels[phase] || phase || "等待状态";
}

function jobName(jobId) {
  return state.jobs.find((job) => job.id === jobId)?.name || `任务 #${jobId}`;
}

function jobById(jobId) {
  return state.jobs.find((job) => job.id === jobId);
}

function activeJob() {
  return state.jobs.find((job) => job.id === state.selectedJobId) || state.jobs[0];
}

function reconcileSelectedJob() {
  if (!state.jobs.length) {
    state.selectedJobId = null;
    return;
  }
  if (!state.jobs.some((job) => job.id === state.selectedJobId)) {
    state.selectedJobId = state.jobs[0].id;
  }
}

function setPage(page, options = {}) {
  const previousPage = state.page;
  const shouldAnimate = Boolean(options.animate) && previousPage !== page;
  state.page = page;

  const pageNodes = {
    dashboard: $("dashboardPage"),
    versions: $("versionsPage"),
    recent: $("recentPage"),
  };
  Object.entries(pageNodes).forEach(([pageName, node]) => {
    node.classList.toggle("active-page", page === pageName);
    node.classList.remove("is-entering");
  });
  if (shouldAnimate && pageNodes[page]) {
    requestAnimationFrame(() => {
      if (state.page === page) pageNodes[page].classList.add("is-entering");
    });
    clearTimeout(window.__routeAnimationTimer);
    window.__routeAnimationTimer = setTimeout(() => pageNodes[page].classList.remove("is-entering"), 320);
  }

  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.pageLink === page);
  });
  if (page === "versions") {
    renderVersionJobSelect();
    loadSelectedVersions().catch((error) => toast(`加载版本失败：${error.message}`));
  }
  if (page === "recent") {
    renderRuns();
  }
}

function navigateTo(page, options = {}) {
  history.replaceState(null, "", `#${page}`);
  setPage(page, options);
}

function progressSummary(run) {
  const copied = Number(run.copied_files || 0);
  const total = Number(run.total_files || 0);
  if (total > 0) return `${copied}/${total} 个文件`;
  if (copied > 0) return `已处理 ${copied} 个文件`;
  if (run.phase) return phaseLabel(run.phase);
  return "暂无进度";
}

function progressForRun(run) {
  const total = Number(run.total_files || 0);
  const copied = Number(run.copied_files || 0);
  const copiedBytes = Number(run.copied_bytes || 0);
  const totalBytes = Number(run.total_bytes || 0);
  const phase = run.phase || "";
  if (run.status === "success") return { percent: 100, text: "备份完成", failed: false };
  if (run.status === "failed") return { percent: 100, text: "备份失败", failed: true };
  if (run.status === "stopped") return { percent: Math.max(1, total ? Math.round((copied / total) * 100) : 0), text: "任务已结束，可继续恢复", failed: true };
  if (run.status === "paused") return { percent: Math.max(1, total ? Math.round((copied / total) * 100) : 10), text: "任务已暂停", failed: false };
  if (phase === "rsyncing" && total > 0) {
    const percent = Math.max(8, Math.min(94, Math.round((copied / total) * 100)));
    return { percent, text: `正在增量同步：已检查 ${copied}/${total} 项，已传输 ${formatBytes(copiedBytes)}`, failed: false };
  }
  if (phase === "rsyncing") return { percent: 12, text: `正在增量同步：已传输 ${formatBytes(copiedBytes)}`, failed: false };
  if (phase === "estimating") return { percent: 10, text: "正在服务器端快速统计文件数量", failed: false };
  if (phase === "scanning") return { percent: 12, text: `正在扫描文件：已发现 ${total} 个`, failed: false };
  if (phase === "syncing" && total > 0) {
    const percent = Math.max(15, Math.min(92, Math.round((copied / total) * 100)));
    return { percent, text: `正在快速传输：${copied}/${total} 个文件，${formatBytes(copiedBytes)}`, failed: false };
  }
  if (phase === "syncing") {
    const percent = Math.min(90, 18 + (copied % 40));
    return { percent, text: `正在快速传输：已接收 ${copied} 个文件，${formatBytes(copiedBytes)}`, failed: false };
  }
  if (phase === "committing") return { percent: 96, text: "正在写入 Git 版本", failed: false };
  if (phase === "connecting") return { percent: 6, text: "正在连接服务器", failed: false };
  if (phase === "preparing") return { percent: 4, text: "正在准备本地仓库", failed: false };
  if (totalBytes) return { percent: 4, text: `准备同步 ${formatBytes(totalBytes)}`, failed: false };
  return { percent: 2, text: phaseLabel(phase), failed: false };
}

function renderJobs() {
  $("jobCount").textContent = `${state.jobs.length} 个任务`;
  const list = $("jobsList");
  if (!state.jobs.length) {
    list.className = "task-list empty-state";
    list.innerHTML = `
      <strong>还没有备份任务</strong>
      <p>点击右上角“新建任务”，填写 SSH、备份目录和计划时间。</p>
    `;
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
              <span>${job.enabled ? "已启用" : "已暂停计划"}</span>
            </div>
            <div class="meta">
              <span>目录：${escapeHtml(job.include_paths.join(", "))}</span>
              <span>目标：${escapeHtml(job.target_path)}</span>
            </div>
          </div>
          <div class="task-actions">
            <button class="button secondary compact" data-action="test" data-id="${job.id}" type="button">测试</button>
            <button class="button primary compact launch-button" data-action="run" data-id="${job.id}" type="button">立即备份</button>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderRuns() {
  const list = $("recentTasksList");
  if (!state.runs.length) {
    list.className = "timeline empty-state";
    list.innerHTML = `
      <strong>暂无执行记录</strong>
      <p>从任务配置里点击“立即备份”，这里会自动出现新任务。</p>
    `;
    return;
  }
  list.className = "timeline";
  state.runs.forEach((run) => {
    state.runCache[run.id] = run;
  });
  list.innerHTML = state.runs
    .map((run) => {
      const progress = progressForRun(run);
      const highlighted = state.highlightRunId === run.id ? " highlighted" : "";
      return `
        <article class="run-row${highlighted}" data-run-id="${run.id}" tabindex="0">
          <div class="run-copy">
            <strong>${escapeHtml(statusLabel(run.status))} · ${escapeHtml(jobName(run.job_id))}</strong>
            <p>${escapeHtml(run.started_at || "")}${run.finished_at ? ` 至 ${escapeHtml(run.finished_at)}` : ""}</p>
            <p>${escapeHtml(run.message || "暂无消息")}</p>
            <div class="mini-progress" aria-hidden="true"><span style="width: ${progress.percent}%"></span></div>
            <p>${escapeHtml(run.commit_hash ? `提交 ${run.commit_hash.slice(0, 12)}` : progressSummary(run))}</p>
          </div>
          <div class="run-actions">
            ${run.status === "running" ? `<button class="button secondary compact" data-run-action="pause" data-id="${run.id}" type="button">暂停</button>` : ""}
            ${run.status === "paused" || run.status === "stopped" || run.status === "failed" || run.phase === "interrupted" ? `<button class="button primary compact" data-run-action="resume" data-id="${run.id}" type="button">继续</button>` : ""}
            ${run.status === "running" || run.status === "paused" ? `<button class="button danger compact" data-run-action="stop" data-id="${run.id}" type="button">结束</button>` : ""}
            <button class="button secondary compact" data-run-action="detail" data-id="${run.id}" type="button">详情</button>
            <button class="button danger compact" data-run-action="delete" data-id="${run.id}" type="button">删除</button>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderVersionJobSelect() {
  const select = $("versionsJobSelect");
  if (!state.jobs.length) {
    select.innerHTML = `<option value="">暂无任务</option>`;
    return;
  }
  reconcileSelectedJob();
  select.innerHTML = state.jobs
    .map((job) => `<option value="${job.id}" ${job.id === state.selectedJobId ? "selected" : ""}>${escapeHtml(job.name)}</option>`)
    .join("");
}

function renderVersions(versions) {
  const list = $("versionsList");
  $("versionCount").textContent = `${versions.length} 个版本`;
  if (!versions.length) {
    list.className = "version-list empty-state";
    list.innerHTML = `
      <strong>还没有备份版本</strong>
      <p>运行一次备份后，这里会显示 Git 历史版本和可下载快照。</p>
    `;
    return;
  }
  list.className = "version-list";
  list.innerHTML = versions
    .map(
      (version) => `
        <article class="version-row" data-version-commit="${escapeHtml(version.commit)}" tabindex="0">
          <div>
            <strong>${escapeHtml(version.date)}</strong>
            <p>${escapeHtml(version.subject)} · ${escapeHtml(version.commit.slice(0, 12))}</p>
            <div class="meta">
              <span>${Number(version.file_count || 0)} 个文件</span>
              <span>${formatBytes(version.total_bytes || 0)}</span>
            </div>
          </div>
          <div class="row-actions">
            <button class="button secondary compact" data-version-action="browse" data-commit="${escapeHtml(version.commit)}" type="button">查看</button>
            <a class="button primary compact" data-version-action="download" href="/api/jobs/${state.selectedJobId}/versions/${version.commit}/download">下载 zip</a>
          </div>
        </article>
      `,
    )
    .join("");
}

function runById(runId) {
  return state.runs.find((run) => run.id === runId) || state.runCache[runId];
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
      <p>${escapeHtml(job ? job.include_paths.join("\n") : "未知")}</p>
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

function openRunDialog(runId) {
  const run = runById(runId);
  if (!run) return;
  state.activeRunId = runId;
  renderRunDialog(run);
  $("runDialog").showModal();
}

function closeRunDialog() {
  state.activeRunId = null;
  $("runDialog").close();
}

function renderVersionSummary(detail) {
  $("versionDialogTitle").textContent = `版本 ${detail.commit.slice(0, 12)}`;
  $("versionSummary").innerHTML = `
    <div class="detail-cell">
      <span>提交时间</span>
      <strong>${escapeHtml(detail.date)}</strong>
    </div>
    <div class="detail-cell">
      <span>文件数量</span>
      <strong>${Number(detail.file_count || 0)} 个文件</strong>
    </div>
    <div class="detail-cell">
      <span>版本大小</span>
      <strong>${formatBytes(detail.total_bytes || 0)}</strong>
    </div>
    <div class="detail-cell">
      <span>提交信息</span>
      <strong>${escapeHtml(detail.subject)}</strong>
    </div>
  `;
  $("versionDownloadBtn").href = `/api/jobs/${state.selectedJobId}/versions/${detail.commit}/download`;
}

function renderBreadcrumbs(path) {
  const parts = path ? path.split("/") : [];
  const crumbs = [`<button class="crumb" data-version-path="" type="button">snapshot</button>`];
  parts.forEach((part, index) => {
    const crumbPath = parts.slice(0, index + 1).join("/");
    crumbs.push(`<span>/</span><button class="crumb" data-version-path="${escapeHtml(crumbPath)}" type="button">${escapeHtml(part)}</button>`);
  });
  $("versionBreadcrumbs").innerHTML = crumbs.join("");
}

function treeRowMotion(index) {
  if (index > 36) return "";
  return `style="--row-index: ${Math.min(index, 12)}" data-tree-animated="true"`;
}

function animateHeightChange(node, update, duration = 360) {
  if (!node) {
    update();
    return;
  }
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  const startHeight = node.offsetHeight;
  node.style.height = `${startHeight}px`;
  node.classList.add("is-resizing");

  update();
  node.classList.add("is-resizing");

  node.style.height = "auto";
  const targetHeight = node.offsetHeight;
  node.style.height = `${startHeight}px`;
  node.offsetHeight;

  clearTimeout(node.__heightAnimationTimer);
  if (reduceMotion || Math.abs(targetHeight - startHeight) < 1) {
    node.style.removeProperty("height");
    node.classList.remove("is-resizing");
    return;
  }

  requestAnimationFrame(() => {
    node.style.height = `${targetHeight}px`;
  });

  node.__heightAnimationTimer = setTimeout(() => {
    node.style.removeProperty("height");
    node.classList.remove("is-resizing");
  }, duration);
}

function setVersionTreeLoading(nextPath) {
  const treeNode = $("versionTree");
  animateHeightChange(treeNode, () => {
    renderBreadcrumbs(nextPath);
    treeNode.setAttribute("aria-busy", "true");
    treeNode.className = "file-browser loading-state is-switching";
    treeNode.innerHTML = `<span></span><span></span><span></span>`;
  }, 300);
}

function renderVersionTree(tree) {
  state.activeVersionPath = tree.path || "";
  renderBreadcrumbs(state.activeVersionPath);
  const rows = [];
  if (tree.parent !== null && tree.parent !== undefined) {
    rows.push(`
      <button class="file-row" ${treeRowMotion(rows.length)} data-version-path="${escapeHtml(tree.parent)}" type="button">
        <span class="file-icon">↩</span>
        <span class="file-name">返回上一级</span>
        <span class="file-size"></span>
      </button>
    `);
  }
  if (!tree.entries.length) {
    rows.push(`<div class="file-empty" ${treeRowMotion(rows.length)}>这个目录是空的。</div>`);
  }
  tree.entries.forEach((entry) => {
    const isDir = entry.type === "dir";
    rows.push(`
      <button class="file-row ${isDir ? "is-dir" : "is-file"}" ${treeRowMotion(rows.length)} ${isDir ? `data-version-path="${escapeHtml(entry.path)}"` : "disabled"} type="button">
        <span class="file-icon">${isDir ? "▸" : "·"}</span>
        <span class="file-name">${escapeHtml(entry.name)}</span>
        <span class="file-size">${isDir ? "文件夹" : formatBytes(entry.size || 0)}</span>
      </button>
    `);
  });
  const treeNode = $("versionTree");
  animateHeightChange(treeNode, () => {
    treeNode.className = "file-browser is-settled";
    treeNode.removeAttribute("aria-busy");
    treeNode.innerHTML = rows.join("");
  });
}

async function loadVersionTree(path = "") {
  if (!state.activeVersion) return;
  const treeNode = $("versionTree");
  if (path === state.activeVersionPath && treeNode.classList.contains("is-settled")) return;
  const requestId = (state.versionTreeRequestId += 1);
  const previousPath = state.activeVersionPath;
  const previousHtml = treeNode.innerHTML;
  const previousClass = treeNode.className;
  setVersionTreeLoading(path);
  try {
    const tree = await api(
      `/api/jobs/${state.selectedJobId}/versions/${state.activeVersion.commit}/tree?path=${encodeURIComponent(path)}`,
    );
    if (requestId !== state.versionTreeRequestId) return;
    renderVersionTree(tree);
  } catch (error) {
    if (requestId === state.versionTreeRequestId) {
      state.activeVersionPath = previousPath;
      renderBreadcrumbs(previousPath);
      treeNode.className = previousClass || "file-browser is-settled";
      treeNode.removeAttribute("aria-busy");
      treeNode.innerHTML = previousHtml;
      treeNode.style.removeProperty("--tree-lock-height");
    }
    throw error;
  }
}

async function openVersionDialog(commit) {
  const version = state.versions.find((item) => item.commit === commit) || { commit };
  state.activeVersion = version;
  state.activeVersionPath = "";
  $("versionDialog").showModal();
  $("versionSummary").innerHTML = `<div class="loading-state"><span></span><span></span></div>`;
  $("versionTree").className = "file-browser loading-state";
  $("versionTree").innerHTML = `<span></span><span></span><span></span>`;
  const detail = await api(`/api/jobs/${state.selectedJobId}/versions/${commit}`);
  state.activeVersion = detail;
  renderVersionSummary(detail);
  await loadVersionTree("");
}

function closeVersionDialog() {
  state.activeVersion = null;
  state.activeVersionPath = "";
  $("versionDialog").close();
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
  if ($("password").value) payload.password = $("password").value;
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
  reconcileSelectedJob();
  renderJobs();
  renderVersionJobSelect();
}

async function loadRuns() {
  state.runs = await api("/api/runs");
  state.runs.forEach((run) => {
    state.runCache[run.id] = run;
  });
  if (state.page === "recent") renderRuns();
  if (state.activeRunId) {
    const latest = runById(state.activeRunId);
    if (latest) renderRunDialog(latest);
  }
}

async function loadSelectedVersions() {
  const job = activeJob();
  if (!job) {
    state.versions = [];
    $("versionCount").textContent = "0 个版本";
    $("versionsList").className = "version-list empty-state";
    $("versionsList").innerHTML = `<strong>暂无任务</strong><p>先创建一个备份任务。</p>`;
    return;
  }
  state.versions = await api(`/api/jobs/${job.id}/versions`);
  renderVersions(state.versions);
}

async function syncAll() {
  if (state.syncBusy) return;
  state.syncBusy = true;
  setLiveStatus("正在同步状态", true);
  try {
    await Promise.all([loadJobs(), loadRuns()]);
    if (state.page === "versions") await loadSelectedVersions();
    setLiveStatus(`实时同步中 · ${formatClock()}`, false);
  } catch (error) {
    setLiveStatus("同步失败，稍后重试", false);
    if (!document.hidden) toast(`同步失败：${error.message}`);
  } finally {
    state.syncBusy = false;
  }
}

function startAutoSync() {
  clearInterval(state.syncTimer);
  state.syncTimer = setInterval(() => {
    if (!document.hidden) syncAll();
  }, 2000);
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
    await syncAll();
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
  const yes = window.confirm(`确定删除任务“${job.name}”吗？本操作不会删除备份仓库文件。`);
  if (!yes) return;
  await api(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (state.selectedJobId === jobId) state.selectedJobId = state.jobs.find((item) => item.id !== jobId)?.id || null;
  await syncAll();
  closeJobDialog();
  toast("任务已删除。");
}

async function runJob(jobId) {
  const maxRunId = Math.max(0, ...state.runs.map((run) => Number(run.id || 0)));
  toast("备份已启动，正在跳转到最近任务。");
  await api(`/api/jobs/${jobId}/run`, { method: "POST" });
  navigateTo("recent", { animate: true });
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await sleep(attempt === 0 ? 450 : 700);
    await syncAll();
    const created = state.runs.find((run) => run.job_id === jobId && Number(run.id) > maxRunId);
    if (created) {
      state.highlightRunId = created.id;
      renderRuns();
      setTimeout(() => {
        if (state.highlightRunId === created.id) {
          state.highlightRunId = null;
          renderRuns();
        }
      }, 6000);
      return;
    }
  }
}

async function testSavedJob(jobId) {
  await api(`/api/jobs/${jobId}/test`, { method: "POST" });
  toast("SSH 连接成功。");
}

async function controlRun(runId, action) {
  await api(`/api/runs/${runId}/control`, {
    method: "PATCH",
    body: JSON.stringify({ action }),
  });
  const messages = {
    pause: "已请求暂停。当前文件完成后会暂停。",
    resume: "已请求继续。任务会从已有文件恢复。",
    stop: "已请求结束。当前文件完成后会停止。",
  };
  toast(messages[action] || "操作已提交。");
  await syncAll();
}

async function deleteRun(runId) {
  const yes = window.confirm("确定删除这条执行记录吗？这不会删除备份仓库文件。");
  if (!yes) return;
  await api(`/api/runs/${runId}`, { method: "DELETE" });
  if (state.activeRunId === runId) closeRunDialog();
  await syncAll();
  toast("执行记录已删除。");
}

function bindEvents() {
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigateTo(link.dataset.pageLink, { animate: true });
    });
  });

  $("newJobBtn").addEventListener("click", () => openJobDialog());
  $("versionsJobSelect").addEventListener("change", (event) => {
    state.selectedJobId = Number(event.target.value);
    loadSelectedVersions().catch((error) => toast(`加载版本失败：${error.message}`));
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
    if (row) openJobById(Number(row.dataset.openJob)).catch((error) => toast(`打开失败：${error.message}`));
  });

  $("jobsList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-open-job]");
    if (!row) return;
    event.preventDefault();
    openJobById(Number(row.dataset.openJob)).catch((error) => toast(`打开失败：${error.message}`));
  });

  $("recentTasksList").addEventListener("click", (event) => {
    const actionButton = event.target.closest("button[data-run-action]");
    const row = event.target.closest("[data-run-id]");
    if (actionButton) {
      event.stopPropagation();
      const id = Number(actionButton.dataset.id);
      const action = actionButton.dataset.runAction;
      if (action === "detail") openRunDialog(id);
      if (action === "pause") controlRun(id, "pause").catch((error) => toast(`暂停失败：${error.message}`));
      if (action === "resume") controlRun(id, "resume").catch((error) => toast(`继续失败：${error.message}`));
      if (action === "stop") controlRun(id, "stop").catch((error) => toast(`结束失败：${error.message}`));
      if (action === "delete") deleteRun(id).catch((error) => toast(`删除失败：${error.message}`));
      return;
    }
    if (row) openRunDialog(Number(row.dataset.runId));
  });

  $("recentTasksList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-run-id]");
    if (!row) return;
    event.preventDefault();
    openRunDialog(Number(row.dataset.runId));
  });

  $("versionsList").addEventListener("click", (event) => {
    const download = event.target.closest("a[data-version-action='download']");
    if (download) return;
    const button = event.target.closest("[data-version-commit], button[data-version-action='browse']");
    if (!button) return;
    event.preventDefault();
    const commit = button.dataset.commit || button.dataset.versionCommit || button.closest("[data-version-commit]")?.dataset.versionCommit;
    if (commit) openVersionDialog(commit).catch((error) => toast(`打开版本失败：${error.message}`));
  });

  $("versionsList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-version-commit]");
    if (!row) return;
    event.preventDefault();
    openVersionDialog(row.dataset.versionCommit).catch((error) => toast(`打开版本失败：${error.message}`));
  });

  $("versionTree").addEventListener("click", (event) => {
    const row = event.target.closest("[data-version-path]");
    if (!row) return;
    loadVersionTree(row.dataset.versionPath || "").catch((error) => toast(`读取目录失败：${error.message}`));
  });

  $("versionBreadcrumbs").addEventListener("click", (event) => {
    const crumb = event.target.closest("[data-version-path]");
    if (!crumb) return;
    loadVersionTree(crumb.dataset.versionPath || "").catch((error) => toast(`读取目录失败：${error.message}`));
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

  $("closeRunDialogBtn").addEventListener("click", closeRunDialog);
  $("closeRunDialogBottomBtn").addEventListener("click", closeRunDialog);
  $("runDialog").addEventListener("click", (event) => {
    if (event.target === $("runDialog")) closeRunDialog();
  });

  $("closeVersionDialogBtn").addEventListener("click", closeVersionDialog);
  $("closeVersionDialogBottomBtn").addEventListener("click", closeVersionDialog);
  $("versionDialog").addEventListener("click", (event) => {
    if (event.target === $("versionDialog")) closeVersionDialog();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) syncAll();
  });
}

async function init() {
  bindEvents();
  await syncAll();
  const hashPage = location.hash.replace("#", "");
  setPage(["dashboard", "versions", "recent"].includes(hashPage) ? hashPage : "dashboard");
  startAutoSync();
}

init().catch((error) => toast(`初始化失败：${error.message}`));
