(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const MAX_FILE = 500 * 1024 * 1024;

  const state = {
    tab: "download",
    source: "search",
    query: "",
    page: 1,
    pages: 1,
    results: [],
    biliCookie: localStorage.getItem("mediadock_bili_cookie") || "",
    neteaseCookie: localStorage.getItem("mediadock_netease_cookie") || "",
    podcasts: [],
    selectedPodcast: null,
    audioFile: null,
    jobs: [],
    jobsBusy: false,
    jobsTimer: 0,
    uploadBusy: false,
    titleAuto: "",
  };

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));

  const safeHttp = (value) => /^https?:\/\//i.test(String(value || "")) ? String(value) : "";

  function fmtBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return `${(n / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function fmtSpeed(bytes) {
    if (!bytes) return "";
    return `${fmtBytes(bytes)}/s`;
  }

  function fmtEta(seconds) {
    if (!Number.isFinite(Number(seconds))) return "";
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total >= 3600) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
    if (total >= 60) return `${Math.floor(total / 60)}m ${total % 60}s`;
    return `${total}s`;
  }

  function fmtCount(value) {
    const n = Number(value) || 0;
    if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
    if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
    return String(n);
  }

  function toast(message, type = "info") {
    const node = document.createElement("div");
    node.className = `toast${type === "error" ? " is-error" : type === "ok" ? " is-ok" : ""}`;
    node.textContent = message;
    $("#toastWrap").appendChild(node);
    window.setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      window.setTimeout(() => node.remove(), 220);
    }, 5200);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { error: text }; }
    if (!response.ok) {
      throw new Error(payload?.error || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  }

  function switchTab(name) {
    state.tab = name;
    $$(".tab").forEach((button) => button.classList.toggle("is-active", button.dataset.tab === name));
    $("#tab-download").hidden = name !== "download";
    $("#tab-upload").hidden = name !== "upload";
    if (window.history.replaceState) {
      window.history.replaceState(null, "", name === "upload" ? "#upload" : "#download");
    }
  }

  function setSource(mode) {
    state.source = mode;
    $$("#sourceMode button").forEach((button) => button.classList.toggle("is-active", button.dataset.source === mode));
    $("#searchPane").hidden = mode !== "search";
    $("#linkPane").hidden = mode !== "link";
    if (mode === "link") {
      $("#resultsHead").hidden = true;
      $("#searchResults").innerHTML = "";
    } else if (state.results.length) {
      $("#resultsHead").hidden = false;
    }
  }

  async function checkHealth() {
    const pill = $("#envPill");
    try {
      const health = await api("/api/health");
      if (health.ffmpeg) {
        pill.className = "env-pill is-ok";
        pill.innerHTML = "<i></i><span>环境就绪</span>";
      } else {
        pill.className = "env-pill is-bad";
        pill.innerHTML = "<i></i><span>缺少 ffmpeg</span>";
        toast("未检测到 ffmpeg：视频合并与 MP3 转码不可用。请安装 ffmpeg 并加入 PATH。", "error");
      }
      if (health.max_upload_mb) {
        $("#maxUploadLabel").textContent = `${health.max_upload_mb}MB`;
      }
    } catch (error) {
      pill.className = "env-pill is-bad";
      pill.innerHTML = "<i></i><span>服务异常</span>";
      toast(error.message, "error");
    }
  }

  function renderResults() {
    const container = $("#searchResults");
    if (!state.results.length) {
      container.innerHTML = '<div class="empty" style="grid-column:1/-1">没有找到相关视频，换个关键词试试。</div>';
      return;
    }
    container.innerHTML = state.results.map((item, index) => {
      const image = safeHttp(item.pic);
      return `
        <article class="video-card">
          <div class="thumb">
            ${image ? `<img src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
            ${item.duration ? `<span class="duration">${esc(item.duration)}</span>` : ""}
          </div>
          <div class="video-body">
            <h3 class="video-title">${esc(item.title)}</h3>
            <div class="video-meta">
              <span>@${esc(item.author || "UP主")}</span>
              <span>${fmtCount(item.play)} 播放</span>
              ${item.pubdate ? `<span>${esc(item.pubdate)}</span>` : ""}
            </div>
            <div class="video-actions">
              <button class="btn btn-primary" type="button" data-action="download" data-index="${index}" data-mode="video">
                <svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" stroke-width="1.7"/><path d="m10 9 5 3-5 3V9Z" fill="currentColor"/></svg>
                MP4
              </button>
              <button class="btn btn-soft" type="button" data-action="download" data-index="${index}" data-mode="audio">
                <svg viewBox="0 0 24 24" fill="none"><path d="M9 18V6l9-2v12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="7" cy="18" r="2.4" stroke="currentColor" stroke-width="1.7"/><circle cx="16" cy="16" r="2.4" stroke="currentColor" stroke-width="1.7"/></svg>
                MP3
              </button>
            </div>
          </div>
        </article>`;
    }).join("");
    $$("#searchResults .thumb img").forEach((image) => {
      image.addEventListener("error", () => { image.style.display = "none"; }, { once: true });
    });
  }

  function renderPager() {
    $("#pageLabel").textContent = `${state.page} / ${Math.max(state.pages, 1)}`;
    $("#prevPage").disabled = state.page <= 1;
    $("#nextPage").disabled = state.page >= state.pages;
  }

  async function doSearch(page = 1, focusResults = true) {
    const keyword = $("#searchInput").value.trim();
    if (!keyword) {
      toast("请输入搜索关键词", "error");
      $("#searchInput").focus();
      return;
    }
    state.query = keyword;
    state.page = page;
    $("#searchBtn").disabled = true;
    $("#searchBtn").textContent = "搜索中…";
    $("#resultsHead").hidden = false;
    $("#resultMeta").textContent = "正在检索…";
    $("#searchResults").innerHTML = '<div class="loading-grid"><div class="skeleton"></div><div class="skeleton"></div></div>';
    try {
      const biliCookie = $("#biliCookie").value.trim();
      const headers = biliCookie ? { "X-Bili-Cookie": biliCookie } : {};
      const data = await api(`/api/search/bilibili?q=${encodeURIComponent(keyword)}&page=${page}`, { headers });
      state.results = data.items || [];
      state.pages = Math.min(Math.max(Number(data.pages) || 1, 1), 50);
      state.page = Math.min(Math.max(Number(data.page) || page, 1), state.pages);
      $("#resultMeta").textContent = `${fmtCount(data.total)} 个结果 · 第 ${state.page} 页`;
      renderResults();
      renderPager();
      if (focusResults) $("#resultsHead").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      state.results = [];
      $("#resultMeta").textContent = "";
      $("#searchResults").innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(error.message)}</div>`;
      toast(error.message, "error");
    } finally {
      $("#searchBtn").disabled = false;
      $("#searchBtn").textContent = "搜索";
    }
  }

  async function startDownload(url, mode, title = "") {
    if (!url) {
      toast("请先输入链接", "error");
      return;
    }
    try {
      const job = await api("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode, title, cookie: $("#biliCookie").value.trim() }),
      });
      toast(mode === "audio" ? "MP3 任务已创建" : "MP4 任务已创建", "ok");
      refreshJobs(true);
      window.setTimeout(() => {
        document.querySelector("#downloadJobs")?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 300);
      return job;
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function inspectLink() {
    const url = $("#linkInput").value.trim();
    if (!url) {
      toast("请先粘贴链接", "error");
      return;
    }
    $("#inspectBtn").disabled = true;
    $("#inspectBtn").textContent = "解析中…";
    try {
      const info = await api("/api/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const image = safeHttp(info.thumbnail);
      $("#linkPreview").innerHTML = `
        ${image ? `<img src="${esc(image)}" alt="" referrerpolicy="no-referrer">` : ""}
        <div class="preview-info">
          <h4>${esc(info.title || "未命名媒体")}</h4>
          <p>${esc(info.platform || "媒体")}${info.uploader ? ` · ${esc(info.uploader)}` : ""}${info.duration ? ` · ${fmtEta(info.duration)}` : ""}</p>
        </div>`;
      $("#linkPreview").hidden = false;
    } catch (error) {
      $("#linkPreview").hidden = true;
      toast(error.message, "error");
    } finally {
      $("#inspectBtn").disabled = false;
      $("#inspectBtn").textContent = "解析";
    }
  }

  function jobStatus(job) {
    if (job.status === "done") return { text: "完成", className: "is-done" };
    if (job.status === "error") return { text: "失败", className: "is-error" };
    if (job.status === "running") return { text: "进行中", className: "" };
    return { text: "排队中", className: "" };
  }

  function jobCard(job) {
    const status = jobStatus(job);
    const icon = job.kind === "upload"
      ? '<svg viewBox="0 0 24 24" fill="none"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 15v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none"><path d="M12 4v12m0 0 5-5m-5 5-5-5M5 20h14" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const modeLabel = job.kind === "upload" ? "网易云播客" : job.mode === "audio" ? "MP3 音频" : "MP4 视频";
    const sub = [];
    if (job.kind === "download" && job.platform) sub.push(esc(job.platform));
    sub.push(modeLabel);
    if (job.status === "running" && job.speed) sub.push(`${fmtSpeed(job.speed)} · 剩余 ${fmtEta(job.eta) || "—"}`);
    if (job.status === "done" && job.file_size) sub.push(fmtBytes(job.file_size));
    if (job.status === "error") sub.push(`<span class="error-text">${esc(job.error || "任务失败")}</span>`);
    const save = job.status === "done" && job.file_url
      ? `<a class="btn btn-soft job-save" href="${esc(job.file_url)}"><svg viewBox="0 0 24 24" fill="none"><path d="M12 4v12m0 0 5-5m-5 5-5-5M5 20h14" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>保存</a>`
      : "";
    return `
      <article class="job ${job.status === "error" ? "is-error" : job.status === "done" ? "is-done" : ""}">
        <div class="job-icon ${job.kind === "upload" ? "upload" : ""}">${icon}</div>
        <div class="job-main">
          <div class="job-top">
            <span class="job-title">${esc(job.title || (job.kind === "upload" ? "上传任务" : "解析中…"))}</span>
            <span class="job-status ${status.className}">${esc(job.stage || status.text)}</span>
          </div>
          <div class="job-sub">${sub.map((item) => `<span>${item}</span>`).join("")}</div>
          <div class="bar"><span style="width:${Math.max(0, Math.min(100, Number(job.progress) || 0))}%"></span></div>
        </div>
        <div class="job-side">
          <span class="job-pct">${Math.round(Number(job.progress) || 0)}%</span>
          ${save}
        </div>
      </article>`;
  }

  function renderJobs() {
    const downloads = state.jobs.filter((job) => job.kind === "download");
    const uploads = state.jobs.filter((job) => job.kind === "upload");
    $("#downloadJobCount").textContent = downloads.length;
    $("#uploadJobCount").textContent = uploads.length;
    $("#downloadJobs").innerHTML = downloads.length
      ? downloads.map(jobCard).join("")
      : '<div class="empty">还没有任务。搜索一个视频，或粘贴链接开始。</div>';
    $("#uploadJobs").innerHTML = uploads.length
      ? uploads.map(jobCard).join("")
      : '<div class="empty">上传进度会显示在这里。</div>';
  }

  async function refreshJobs(force = false) {
    if (state.jobsBusy) return;
    state.jobsBusy = true;
    try {
      const data = await api("/api/jobs");
      state.jobs = data.jobs || [];
      renderJobs();
    } catch (error) {
      if (force) toast(error.message, "error");
    } finally {
      state.jobsBusy = false;
      const active = state.jobs.some((job) => job.status === "queued" || job.status === "running");
      window.clearTimeout(state.jobsTimer);
      state.jobsTimer = window.setTimeout(() => refreshJobs(false), active ? 1200 : 5000);
    }
  }

  function setNeteaseStatus(message, type = "") {
    const node = $("#neteaseStatus");
    node.textContent = message;
    node.className = `status-line${type ? ` ${type}` : ""}`;
  }

  function renderPodcasts() {
    const container = $("#podcastList");
    if (!state.podcasts.length) {
      container.innerHTML = '<div class="empty compact">没有读取到播客。请确认账号已开通播客权限，或重新获取 Cookie。</div>';
      return;
    }
    container.innerHTML = state.podcasts.map((podcast) => {
      const selected = String(podcast.voiceListId) === String(state.selectedPodcast?.voiceListId);
      const cover = safeHttp(podcast.cover);
      return `
        <button type="button" class="podcast-item${selected ? " is-selected" : ""}" data-podcast-id="${esc(podcast.voiceListId)}">
          ${cover
            ? `<img class="podcast-cover" src="${esc(cover)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
            : `<span class="podcast-cover-fallback"><svg viewBox="0 0 24 24" fill="none"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z" stroke="currentColor" stroke-width="1.7"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg></span>`}
          <span class="podcast-info">
            <span class="podcast-name">${esc(podcast.name)}</span>
            <span class="podcast-meta">${fmtCount(podcast.voiceCount)} 个声音</span>
          </span>
          <span class="radio" aria-hidden="true"></span>
        </button>`;
    }).join("");
    $$("#podcastList .podcast-cover").forEach((image) => {
      image.addEventListener("error", () => {
        image.outerHTML = '<span class="podcast-cover-fallback"><svg viewBox="0 0 24 24" fill="none"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z" stroke="currentColor" stroke-width="1.7"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg></span>';
      }, { once: true });
    });
  }

  async function connectNetease() {
    const cookie = $("#neteaseCookie").value.trim();
    if (!cookie) {
      toast("请先粘贴网易云 Cookie", "error");
      $("#neteaseCookie").focus();
      return;
    }
    $("#connectBtn").disabled = true;
    $("#connectBtn").textContent = "连接中…";
    setNeteaseStatus("正在验证 Cookie 并读取播客…");
    try {
      const data = await api("/api/netease/podcasts", {
        method: "POST",
        headers: { "X-Netease-Cookie": cookie },
      });
      state.neteaseCookie = cookie;
      localStorage.setItem("mediadock_netease_cookie", cookie);
      state.podcasts = data.podcasts || [];
      state.selectedPodcast = state.podcasts[0] || null;
      renderPodcasts();
      setNeteaseStatus(`已连接，读取到 ${state.podcasts.length} 个播客`, "is-ok");
      toast(state.podcasts.length ? "播客列表已更新" : "连接成功，但没有可用播客", state.podcasts.length ? "ok" : "info");
    } catch (error) {
      state.podcasts = [];
      state.selectedPodcast = null;
      renderPodcasts();
      setNeteaseStatus(error.message, "is-error");
      toast(error.message, "error");
    } finally {
      $("#connectBtn").disabled = false;
      $("#connectBtn").textContent = "读取我的播客";
    }
  }

  function setFile(file) {
    if (!file) return;
    const allowed = [".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"];
    const suffix = file.name.includes(".") ? `.${file.name.split(".").pop().toLowerCase()}` : "";
    if (!allowed.includes(suffix) && !String(file.type).startsWith("audio/")) {
      toast("请选择 mp3 / flac / wav / m4a / aac / ogg 音频", "error");
      return;
    }
    if (file.size > MAX_FILE) {
      toast("文件超过 500MB 上限", "error");
      return;
    }
    state.audioFile = file;
    const zone = $("#dropzone");
    zone.classList.add("has-file");
    zone.querySelector("p").innerHTML = `<b>${esc(file.name)}</b>`;
    $("#fileHint").textContent = `${fmtBytes(file.size)} · ${suffix.replace(".", "").toUpperCase() || "AUDIO"}`;
    const autoTitle = file.name.replace(/\.[^.]+$/, "").slice(0, 100);
    const titleInput = $("#podcastTitle");
    if (!titleInput.value.trim() || titleInput.value === state.titleAuto) {
      titleInput.value = autoTitle;
      state.titleAuto = autoTitle;
    }
  }

  function resetFile() {
    state.audioFile = null;
    state.titleAuto = "";
    $("#audioFile").value = "";
    const zone = $("#dropzone");
    zone.classList.remove("has-file");
    zone.querySelector("p").innerHTML = "<b>点击选择</b> 或把音频拖到这里";
    $("#fileHint").textContent = "MP3 会直接上传，其他格式自动转码";
  }

  function uploadPodcast() {
    if (state.uploadBusy) return;
    const cookie = $("#neteaseCookie").value.trim();
    if (!cookie) {
      toast("请先连接网易云", "error");
      switchTab("upload");
      $("#neteaseCookie").focus();
      return;
    }
    if (!state.selectedPodcast) {
      toast("请选择目标播客", "error");
      return;
    }
    if (!state.audioFile) {
      toast("请选择要上传的音频文件", "error");
      $("#dropzone").focus();
      return;
    }
    const title = $("#podcastTitle").value.trim();
    if (!title) {
      toast("请填写节目标题", "error");
      $("#podcastTitle").focus();
      return;
    }
    const params = new URLSearchParams({
      filename: state.audioFile.name,
      title,
      description: $("#podcastDesc").value.trim(),
      voiceListId: String(state.selectedPodcast.voiceListId),
      coverImgId: String(state.selectedPodcast.coverImgId || ""),
      categoryId: String(state.selectedPodcast.categoryId || ""),
      secondCategoryId: String(state.selectedPodcast.secondCategoryId || ""),
      autoPublish: $("#autoPublish").checked ? "1" : "0",
    });
    state.uploadBusy = true;
    $("#uploadBtn").disabled = true;
    $("#uploadBtn").textContent = "上传中…";
    $("#localUpload").hidden = false;
    $("#localUploadBar").style.width = "0%";
    $("#localUploadText").textContent = "正在传到本地服务…";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/netease/upload?${params.toString()}`);
    xhr.setRequestHeader("X-Netease-Cookie", cookie);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const pct = Math.round((event.loaded / event.total) * 100);
      $("#localUploadBar").style.width = `${pct}%`;
      $("#localUploadText").textContent = `传到本地服务 ${pct}%`;
    };
    xhr.onload = () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText || "{}"); } catch { payload = {}; }
      state.uploadBusy = false;
      $("#uploadBtn").disabled = false;
      $("#uploadBtn").textContent = "上传到所选播客";
      if (xhr.status >= 200 && xhr.status < 300) {
        $("#localUploadBar").style.width = "100%";
        $("#localUploadText").textContent = "已接收，正在上传到网易云…";
        window.setTimeout(() => { $("#localUpload").hidden = true; }, 1600);
        toast("上传任务已创建，进度见下方", "ok");
        resetFile();
        refreshJobs(true);
        window.setTimeout(() => {
          document.querySelector("#uploadJobs")?.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 400);
      } else {
        $("#localUpload").hidden = true;
        toast(payload.error || `上传失败（HTTP ${xhr.status}）`, "error");
      }
    };
    xhr.onerror = () => {
      state.uploadBusy = false;
      $("#uploadBtn").disabled = false;
      $("#uploadBtn").textContent = "上传到所选播客";
      $("#localUpload").hidden = true;
      toast("网络错误，无法连接本地服务", "error");
    };
    xhr.send(state.audioFile);
  }

  function bindEvents() {
    $$(".tab").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
    $$("#sourceMode button").forEach((button) => button.addEventListener("click", () => setSource(button.dataset.source)));

    $("#searchForm").addEventListener("submit", (event) => {
      event.preventDefault();
      doSearch(1);
    });
    $$(".chips button").forEach((button) => {
      button.addEventListener("click", () => {
        $("#searchInput").value = button.dataset.query;
        doSearch(1);
      });
    });
    $("#prevPage").addEventListener("click", () => doSearch(Math.max(1, state.page - 1)));
    $("#nextPage").addEventListener("click", () => doSearch(Math.min(state.pages, state.page + 1)));

    $("#searchResults").addEventListener("click", (event) => {
      const button = event.target.closest('[data-action="download"]');
      if (!button) return;
      const item = state.results[Number(button.dataset.index)];
      if (item) startDownload(item.url, button.dataset.mode, item.title);
    });

    $("#inspectBtn").addEventListener("click", inspectLink);
    $("#linkForm").addEventListener("submit", (event) => event.preventDefault());
    $("#linkVideoBtn").addEventListener("click", () => startDownload($("#linkInput").value.trim(), "video"));
    $("#linkAudioBtn").addEventListener("click", () => startDownload($("#linkInput").value.trim(), "audio"));
    $("#linkInput").addEventListener("input", () => { $("#linkPreview").hidden = true; });

    const biliInput = $("#biliCookie");
    biliInput.value = state.biliCookie;
    biliInput.addEventListener("input", () => {
      state.biliCookie = biliInput.value.trim();
    });
    biliInput.addEventListener("change", () => {
      state.biliCookie = biliInput.value.trim();
      if (state.biliCookie) localStorage.setItem("mediadock_bili_cookie", state.biliCookie);
      else localStorage.removeItem("mediadock_bili_cookie");
    });
    $("#toggleBiliCookie").addEventListener("click", () => {
      biliInput.type = biliInput.type === "password" ? "text" : "password";
    });

    const neteaseInput = $("#neteaseCookie");
    neteaseInput.value = state.neteaseCookie;
    if (state.neteaseCookie) setNeteaseStatus("已保存 Cookie，点击「读取我的播客」连接");
    $("#toggleNeteaseCookie").addEventListener("click", () => {
      neteaseInput.type = neteaseInput.type === "password" ? "text" : "password";
    });
    $("#connectBtn").addEventListener("click", connectNetease);

    $("#podcastList").addEventListener("click", (event) => {
      const item = event.target.closest("[data-podcast-id]");
      if (!item) return;
      state.selectedPodcast = state.podcasts.find((podcast) => String(podcast.voiceListId) === item.dataset.podcastId) || null;
      renderPodcasts();
    });

    const dropzone = $("#dropzone");
    const fileInput = $("#audioFile");
    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    fileInput.addEventListener("change", () => setFile(fileInput.files?.[0]));
    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-drag");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-drag");
      });
    });
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (file) setFile(file);
    });

    $("#uploadBtn").addEventListener("click", uploadPodcast);
  }

  function init() {
    bindEvents();
    checkHealth();
    if (location.hash === "#upload") switchTab("upload");
    refreshJobs(true);
    const initialQuery = new URLSearchParams(location.search).get("q");
    if (initialQuery) {
      $("#searchInput").value = initialQuery;
      doSearch(1, false);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
