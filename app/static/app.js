const state = {
  jobs: [],
  selectedJobId: null,
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
  }, 4200);
}

function setConnectionStatus(message, kind = "") {
  const node = $("connectionStatus");
  node.textContent = message;
  node.className = `status-text ${kind}`.trim();
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

function uniqueLines(value, newLine) {
  const current = lines(value);
  if (!current.includes(newLine)) current.push(newLine);
  return current.join("\n");
}

function setDefaults() {
  $("includePaths").value = defaults.includePath;
  $("excludePatterns").value = defaults.excludePatterns.join("\n");
  $("targetPath").value = defaults.targetPath;
  $("browsePath").value = defaults.includePath;
  setConnectionStatus("先测试连接，再选择目录。");
}

function formatSchedule(job) {
  const time = `${String(job.hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}`;
  if (job.schedule_kind === "weekly") {
    const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    return `${days[job.day_of_week ?? 0]} ${time}`;
  }
  return `每天 ${time}`;
}

function activeJob() {
  return state.jobs.find((job) => job.id === state.selectedJobId) || state.jobs[0];
}

function connectionPayload() {
  return {
    host: $("host").value.trim(),
    port: Number($("port").value || 22),
    username: $("username").value.trim(),
    password: $("password").value,
  };
}

function validateConnectionPayload(payload) {
  if (!payload.host) return "请填写 SSH 地址。";
  if (!payload.port || payload.port < 1 || payload.port > 65535) return "请填写正确的 SSH 端口。";
  if (!payload.username) return "请填写 SSH 用户名。";
  if (!payload.password) return "请填写 SSH 密码。编辑旧任务时，如不想重新输入密码，请用任务列表里的测试或浏览。";
  return "";
}

function renderJobs() {
  $("jobCount").textContent = `${state.jobs.length} 个任务`;
  const list = $("jobsList");
  if (!state.jobs.length) {
    list.className = "list empty";
    list.textContent = "还没有任务。先在左侧测试连接、选择目录，然后保存任务。";
    return;
  }

  list.className = "list";
  list.innerHTML = state.jobs
    .map(
      (job) => `
        <div class="item">
          <div class="item-head">
            <div>
              <p class="item-title">${escapeHtml(job.name)}</p>
              <div class="meta">
                <span>${escapeHtml(job.username)}@${escapeHtml(job.host)}:${job.port}</span>
                <span>${formatSchedule(job)}</span>
                <span>${job.enabled ? "已启用" : "已暂停"}</span>
              </div>
            </div>
            <span class="badge">${state.selectedJobId === job.id ? "当前" : "任务"}</span>
          </div>
          <div class="meta">
            <span>目录：${escapeHtml(job.include_paths.join(", "))}</span>
            <span>目标：${escapeHtml(job.target_path)}</span>
          </div>
          <div class="item-actions">
            <button class="button secondary compact" data-action="select" data-id="${job.id}" type="button">选择</button>
            <button class="button secondary compact" data-action="edit" data-id="${job.id}" type="button">编辑</button>
            <button class="button secondary compact" data-action="test" data-id="${job.id}" type="button">测试</button>
            <button class="button secondary compact" data-action="browse-saved" data-id="${job.id}" type="button">浏览</button>
            <button class="button primary compact" data-action="run" data-id="${job.id}" type="button">立即备份</button>
            <button class="button danger compact" data-action="delete" data-id="${job.id}" type="button">删除</button>
          </div>
        </div>
      `,
    )
    .join("");
}

function renderRuns(runs) {
  const list = $("runsList");
  if (!runs.length) {
    list.className = "list empty";
    list.textContent = "暂无运行记录。";
    return;
  }
  list.className = "list";
  list.innerHTML = runs
    .map(
      (run) => `
        <div class="item">
          <div class="item-head">
            <p class="item-title">${escapeHtml(run.status)}</p>
            <span class="badge">${escapeHtml(run.started_at || "")}</span>
          </div>
          <div class="meta">
            <span>任务 #${run.job_id}</span>
            <span>${escapeHtml(run.commit_hash ? run.commit_hash.slice(0, 8) : "无提交")}</span>
          </div>
          <div class="meta">${escapeHtml(run.message || "")}</div>
        </div>
      `,
    )
    .join("");
}

function fillForm(job) {
  $("jobId").value = job.id;
  $("formMode").textContent = `编辑 #${job.id}`;
  $("name").value = job.name;
  $("host").value = job.host;
  $("port").value = job.port;
  $("username").value = job.username;
  $("password").value = "";
  $("password").required = false;
  $("targetPath").value = job.target_path;
  $("includePaths").value = job.include_paths.join("\n");
  $("excludePatterns").value = job.exclude_patterns.join("\n");
  $("scheduleKind").value = job.schedule_kind;
  $("dayOfWeek").value = job.day_of_week ?? 0;
  $("timeOfDay").value = `${String(job.hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}`;
  $("enabled").checked = job.enabled;
  setConnectionStatus("正在编辑已保存任务。留空密码不会覆盖旧密码。");
}

function resetForm() {
  $("jobForm").reset();
  $("jobId").value = "";
  $("formMode").textContent = "新任务";
  $("password").required = true;
  setDefaults();
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  if (!state.selectedJobId && state.jobs[0]) {
    state.selectedJobId = state.jobs[0].id;
  }
  renderJobs();
}

async function loadRuns() {
  const runs = await api("/api/runs");
  renderRuns(runs);
}

async function loadVersions() {
  const job = activeJob();
  const list = $("versionsList");
  if (!job) {
    list.className = "list empty";
    list.textContent = "先保存一个任务，再查看历史版本。";
    return;
  }
  const versions = await api(`/api/jobs/${job.id}/versions`);
  if (!versions.length) {
    list.className = "list empty";
    list.textContent = "还没有历史版本。先运行一次备份。";
    return;
  }
  list.className = "list";
  list.innerHTML = versions
    .map(
      (version) => `
        <div class="item">
          <div class="item-head">
            <div>
              <p class="item-title">${escapeHtml(version.date)}</p>
              <div class="meta">
                <span>${escapeHtml(version.commit.slice(0, 12))}</span>
                <span>${escapeHtml(version.subject)}</span>
              </div>
            </div>
            <a class="button primary compact" href="/api/jobs/${job.id}/versions/${version.commit}/download">下载 zip</a>
          </div>
        </div>
      `,
    )
    .join("");
}

async function testCurrentConnection() {
  const payload = connectionPayload();
  const validation = validateConnectionPayload(payload);
  if (validation) {
    setConnectionStatus(validation, "bad");
    toast(validation);
    return;
  }
  setConnectionStatus("正在连接 SSH...", "");
  await api("/api/ssh/test", { method: "POST", body: JSON.stringify(payload) });
  setConnectionStatus("连接成功，可以读取远程目录。", "ok");
  toast("SSH 连接成功。");
}

async function browseRemote(useSavedJobId = null) {
  const list = $("browserList");
  const path = $("browsePath").value || "/";
  let entries;

  if (useSavedJobId) {
    entries = await api(`/api/jobs/${useSavedJobId}/browse?path=${encodeURIComponent(path)}`);
  } else {
    const payload = { ...connectionPayload(), path };
    const validation = validateConnectionPayload(payload);
    if (validation) {
      list.className = "browser empty";
      list.textContent = validation;
      setConnectionStatus(validation, "bad");
      return;
    }
    entries = await api("/api/ssh/browse", { method: "POST", body: JSON.stringify(payload) });
    setConnectionStatus("目录读取成功。可以点“加入备份目录”。", "ok");
  }

  if (!entries.length) {
    list.className = "browser empty";
    list.textContent = "这个目录是空的。";
    return;
  }

  const parent = parentPath(path);
  list.className = "browser";
  list.innerHTML = `
    ${
      parent
        ? `<div class="browser-row">
            <div>
              <div class="browser-name">上级目录</div>
              <div class="meta">${escapeHtml(parent)}</div>
            </div>
            <div class="browser-actions">
              <button class="button secondary compact" data-action="open-dir" data-path="${escapeAttr(parent)}" type="button">打开</button>
            </div>
          </div>`
        : ""
    }
    ${entries
      .map(
        (entry) => `
          <div class="browser-row">
            <div>
              <div class="browser-name">${entry.is_dir ? "目录" : "文件"} ${escapeHtml(entry.name)}</div>
              <div class="meta">${escapeHtml(entry.path)} · ${entry.size} bytes</div>
            </div>
            <div class="browser-actions">
              ${
                entry.is_dir
                  ? `<button class="button secondary compact" data-action="open-dir" data-path="${escapeAttr(entry.path)}" type="button">打开</button>
                     <button class="button primary compact" data-action="add-dir" data-path="${escapeAttr(entry.path)}" type="button">加入备份目录</button>`
                  : ""
              }
            </div>
          </div>
        `,
      )
      .join("")}
  `;
}

function parentPath(path) {
  const clean = (path || "/").replace(/\/+$/, "") || "/";
  if (clean === "/") return "";
  const index = clean.lastIndexOf("/");
  return index <= 0 ? "/" : clean.slice(0, index);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

async function handleSubmit(event) {
  event.preventDefault();
  const [hour, minute] = $("timeOfDay").value.split(":").map(Number);
  const payload = {
    name: $("name").value.trim(),
    host: $("host").value.trim(),
    port: Number($("port").value),
    username: $("username").value.trim(),
    target_path: $("targetPath").value.trim(),
    include_paths: lines($("includePaths").value),
    exclude_patterns: lines($("excludePatterns").value),
    schedule_kind: $("scheduleKind").value,
    day_of_week: Number($("dayOfWeek").value),
    hour,
    minute,
    enabled: $("enabled").checked,
  };
  if ($("password").value) {
    payload.password = $("password").value;
  }

  const jobId = $("jobId").value;
  if (!jobId && !payload.password) {
    toast("新任务必须填写 SSH 密码。");
    return;
  }
  if (!payload.include_paths.length) {
    toast("请至少选择或填写一个备份目录。");
    return;
  }

  try {
    const saved = jobId
      ? await api(`/api/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(payload) })
      : await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    state.selectedJobId = saved.id;
    await loadJobs();
    resetForm();
    toast("任务已保存。");
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

async function handleJobAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const id = Number(button.dataset.id);
  const job = state.jobs.find((item) => item.id === id);
  if (!job) return;

  try {
    if (action === "select") {
      state.selectedJobId = id;
      renderJobs();
      toast(`已选择：${job.name}`);
    }
    if (action === "edit") {
      state.selectedJobId = id;
      fillForm(job);
      renderJobs();
    }
    if (action === "test") {
      button.disabled = true;
      await api(`/api/jobs/${id}/test`, { method: "POST" });
      toast("SSH 连接成功。");
    }
    if (action === "browse-saved") {
      state.selectedJobId = id;
      $("browsePath").value = job.include_paths[0] || defaults.includePath;
      await browseRemote(id);
      renderJobs();
    }
    if (action === "run") {
      button.disabled = true;
      toast("备份已加入后台队列。");
      await api(`/api/jobs/${id}/run`, { method: "POST" });
      setTimeout(() => {
        Promise.all([loadRuns(), loadVersions()]).catch((error) => toast(`刷新失败：${error.message}`));
      }, 1800);
    }
    if (action === "delete") {
      const yes = window.confirm(`确定删除任务“${job.name}”吗？本操作不删除备份仓库文件。`);
      if (!yes) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      if (state.selectedJobId === id) state.selectedJobId = null;
      await loadJobs();
      toast("任务已删除。");
    }
  } catch (error) {
    toast(`操作失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  $("jobForm").addEventListener("submit", handleSubmit);
  $("resetFormBtn").addEventListener("click", resetForm);
  $("testDraftBtn").addEventListener("click", () => testCurrentConnection().catch((error) => {
    setConnectionStatus(error.message, "bad");
    toast(`连接失败：${error.message}`);
  }));
  $("refreshBtn").addEventListener("click", async () => {
    await Promise.all([loadJobs(), loadRuns()]);
    toast("已刷新。");
  });
  $("jobsList").addEventListener("click", handleJobAction);
  $("browseBtn").addEventListener("click", () => browseRemote().catch((error) => {
    setConnectionStatus(error.message, "bad");
    toast(`读取失败：${error.message}`);
  }));
  $("browserList").addEventListener("click", (event) => {
    const openButton = event.target.closest("button[data-action='open-dir']");
    const addButton = event.target.closest("button[data-action='add-dir']");
    if (openButton) {
      $("browsePath").value = openButton.dataset.path;
      browseRemote(state.selectedJobId && !$("password").value ? state.selectedJobId : null).catch((error) =>
        toast(`读取失败：${error.message}`),
      );
    }
    if (addButton) {
      $("includePaths").value = uniqueLines($("includePaths").value, addButton.dataset.path);
      toast("已加入备份目录。");
    }
  });
  $("loadVersionsBtn").addEventListener("click", () => loadVersions().catch((error) => toast(`加载失败：${error.message}`)));
}

async function init() {
  setDefaults();
  bindEvents();
  try {
    await Promise.all([loadJobs(), loadRuns()]);
  } catch (error) {
    toast(`初始化失败：${error.message}`);
  }
}

init();
