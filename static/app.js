const $ = (selector) => document.querySelector(selector);
let currentAnalysis = null;
let bulkPollTimer = null;
let bulkCompletionNotice = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const detail = payload.details && typeof payload.details === "string" ? `\n${payload.details}` : "";
    throw new Error((payload.error || `HTTP ${response.status}`) + detail);
  }
  return payload;
}

function showToast(message, kind = "error") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast ${kind}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 7000);
}

function showAnalyzeError(message = "") {
  const panel = $("#analyzeError");
  panel.textContent = message;
  panel.classList.toggle("hidden", !message);
}

function setBusy(button, busy, label) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.label;
}

function formatDuration(ms) {
  if (!ms) return "";
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

async function refreshStatus() {
  const status = await api("/api/status");
  const badge = $("#authBadge");
  if (status.authenticated) {
    const name = status.profile?.display_name || status.profile?.id || "Spotify 使用者";
    badge.textContent = `已連結 · ${name}`;
    badge.className = "badge connected";
    $("#authForm").classList.add("connected-form");
    $("#authButton").textContent = "重新連結";
  } else {
    badge.textContent = "尚未連結";
    badge.className = "badge neutral";
  }
  return status;
}

$("#authButton").addEventListener("click", async () => {
  const clientId = $("#clientId").value.trim();
  if (!clientId) return showToast("請先貼上 Spotify Client ID。");
  localStorage.setItem("ytmigrate_client_id", clientId);
  try {
    setBusy($("#authButton"), true, "準備登入…");
    const result = await api("/api/auth/start", { method: "POST", body: JSON.stringify({ client_id: clientId }) });
    window.location.assign(result.authorize_url);
  } catch (error) {
    setBusy($("#authButton"), false);
    showToast(error.message);
  }
});

$("#threshold").addEventListener("input", (event) => {
  $("#thresholdValue").textContent = `${Math.round(Number(event.target.value) * 100)}%`;
});

$("#analyzeButton").addEventListener("click", async () => {
  const status = await refreshStatus();
  if (!status.authenticated) return showToast("請先完成 Spotify 連結。");
  const playlistUrl = $("#playlistUrl").value.trim();
  if (!playlistUrl) return showToast("請貼上 YouTube Music 播放清單網址。");
  const button = $("#analyzeButton");
  try {
    setBusy(button, true, "分析中…");
    showAnalyzeError();
    $("#progress").classList.remove("hidden");
    $("#reviewCard").classList.add("hidden");
    $("#successCard").classList.add("hidden");
    currentAnalysis = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({
        playlist_url: playlistUrl,
        cookies_browser: $("#cookiesBrowser").value,
        threshold: Number($("#threshold").value),
      }),
    });
    renderAnalysis(currentAnalysis);
  } catch (error) {
    showAnalyzeError(error.message);
    showToast(error.message);
  } finally {
    setBusy(button, false);
    $("#progress").classList.add("hidden");
  }
});

const bulkActiveStates = new Set(["queued", "extracting", "creating", "matching", "adding"]);

function renderBulkStatus(job) {
  if (!job || job.status === "idle") return;
  $("#bulkStatus").classList.remove("hidden");
  const total = Number(job.total || 0);
  const current = Number(job.scanned || job.processed || 0);
  const percent = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  $("#bulkProgress").max = total || 1;
  $("#bulkProgress").value = total ? current : 0;
  $("#bulkPercent").textContent = total ? `${percent}%` : "讀取中";
  $("#bulkMessage").textContent = job.message || "處理中…";
  $("#bulkStats").textContent = total
    ? `已完成批次 ${job.processed || 0}/${total} · 已加入 ${job.added || 0} · 找不到 ${job.unmatched || 0} · 低信心候選 ${job.low_confidence || 0}`
    : "正在取得播放清單歌曲數量…";

  const playlistLink = $("#bulkSpotifyLink");
  if (job.spotify_playlist_url) {
    playlistLink.href = job.spotify_playlist_url;
    playlistLink.classList.remove("hidden");
  }

  const errorPanel = $("#bulkError");
  if (job.error) {
    const details = typeof job.error_details === "string" ? `\n${job.error_details}` : "";
    errorPanel.textContent = job.error + details;
    errorPanel.classList.remove("hidden");
  } else {
    errorPanel.classList.add("hidden");
  }

  $("#bulkResumeButton").classList.toggle("hidden", !job.can_resume);
  $("#bulkCancelButton").classList.toggle("hidden", !bulkActiveStates.has(job.status));
  setBusy($("#bulkStartButton"), bulkActiveStates.has(job.status), "自動轉移進行中…");

  if (job.status === "complete" && bulkCompletionNotice !== job.id) {
    bulkCompletionNotice = job.id;
    showToast(`大型清單轉移完成：已加入 ${job.added} 首。`, "success");
  }
}

function scheduleBulkPoll(delay = 1800) {
  window.clearTimeout(bulkPollTimer);
  bulkPollTimer = window.setTimeout(async () => {
    try {
      const job = await api("/api/bulk/status");
      renderBulkStatus(job);
      if (bulkActiveStates.has(job.status)) scheduleBulkPoll();
    } catch (error) {
      showToast(error.message);
      scheduleBulkPoll(5000);
    }
  }, delay);
}

$("#bulkStartButton").addEventListener("click", async () => {
  const status = await refreshStatus();
  if (!status.authenticated) return showToast("請先完成 Spotify 連結。");
  const playlistUrl = $("#playlistUrl").value.trim();
  if (!playlistUrl) return showToast("請貼上 YouTube Music 播放清單網址。");
  try {
    setBusy($("#bulkStartButton"), true, "正在啟動…");
    $("#bulkError").classList.add("hidden");
    const job = await api("/api/bulk/start", {
      method: "POST",
      body: JSON.stringify({
        playlist_url: playlistUrl,
        cookies_browser: $("#cookiesBrowser").value,
        threshold: Number($("#threshold").value),
        include_low_confidence: $("#bulkIncludeLow").checked,
        name: $("#bulkName").value.trim(),
        public: $("#bulkPublic").checked,
      }),
    });
    renderBulkStatus(job);
    scheduleBulkPoll(500);
  } catch (error) {
    setBusy($("#bulkStartButton"), false);
    showToast(error.message);
  }
});

$("#bulkResumeButton").addEventListener("click", async () => {
  try {
    const job = await api("/api/bulk/resume", { method: "POST", body: "{}" });
    renderBulkStatus(job);
    scheduleBulkPoll(500);
  } catch (error) {
    showToast(error.message);
  }
});

$("#bulkCancelButton").addEventListener("click", async () => {
  try {
    const job = await api("/api/bulk/cancel", { method: "POST", body: "{}" });
    renderBulkStatus(job);
  } catch (error) {
    showToast(error.message);
  }
});

function renderAnalysis(analysis) {
  $("#playlistTitle").textContent = analysis.playlist.title;
  $("#targetName").value = analysis.playlist.title;
  const selected = analysis.tracks.filter((item) => item.selected).length;
  const matched = analysis.tracks.filter((item) => item.match).length;
  $("#summaryText").textContent = `${analysis.tracks.length} 首來源歌曲 · ${matched} 首找到候選 · ${selected} 首已自動勾選`;
  $("#matchesBody").innerHTML = analysis.tracks.map((item, index) => {
    const source = item.source;
    const match = item.match;
    const score = Math.round(item.score.total * 100);
    const matchHtml = match ? `
      <a href="${escapeHtml(match.url)}" target="_blank" rel="noopener"><strong>${escapeHtml(match.name)}</strong></a>
      <span>${escapeHtml(match.artists)}${match.album ? ` · ${escapeHtml(match.album)}` : ""}</span>
      <small>${formatDuration(match.duration_ms)}</small>` : `<em>找不到 Spotify 候選</em>`;
    return `<tr class="confidence-${item.confidence}">
      <td class="check-col"><input class="track-check" type="checkbox" data-index="${index}" ${item.selected ? "checked" : ""} ${match ? "" : "disabled"}></td>
      <td><strong>${escapeHtml(source.title)}</strong><span>${escapeHtml(source.artist)}</span><small>${formatDuration(source.duration_ms)}</small></td>
      <td>${matchHtml}</td>
      <td><div class="score ${item.confidence}">${score}</div></td>
    </tr>`;
  }).join("");
  $("#reviewCard").classList.remove("hidden");
  $("#reviewCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("#transferButton").addEventListener("click", async () => {
  if (!currentAnalysis) return showToast("請先分析播放清單。");
  const uris = [...document.querySelectorAll(".track-check:checked")]
    .map((box) => currentAnalysis.tracks[Number(box.dataset.index)]?.match?.uri)
    .filter(Boolean);
  if (!uris.length) return showToast("至少勾選一首歌曲。");
  const button = $("#transferButton");
  try {
    setBusy(button, true, "正在建立…");
    const result = await api("/api/transfer", {
      method: "POST",
      body: JSON.stringify({
        name: $("#targetName").value.trim(),
        public: $("#publicPlaylist").checked,
        items: uris,
      }),
    });
    $("#successTitle").textContent = result.name;
    $("#successText").textContent = `成功加入 ${result.added} 首，略過 ${result.skipped} 首。`;
    $("#spotifyLink").href = result.url;
    $("#successCard").classList.remove("hidden");
    $("#successCard").scrollIntoView({ behavior: "smooth", block: "center" });
    showToast("Spotify 播放清單建立完成。", "success");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(button, false);
  }
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(button.dataset.copy);
    showToast("Redirect URI 已複製。", "success");
  });
});

window.addEventListener("DOMContentLoaded", async () => {
  $("#clientId").value = localStorage.getItem("ytmigrate_client_id") || "";
  const params = new URLSearchParams(window.location.search);
  if (params.get("auth") === "ok") {
    showToast("Spotify 已成功連結。", "success");
    history.replaceState({}, "", "/");
  } else if (params.get("auth") === "error") {
    showToast(`Spotify 登入未完成：${params.get("message") || "未知錯誤"}`);
    history.replaceState({}, "", "/");
  }
  try {
    await refreshStatus();
    const job = await api("/api/bulk/status");
    renderBulkStatus(job);
    if (bulkActiveStates.has(job.status)) scheduleBulkPoll(500);
  } catch (error) { showToast(error.message); }
});
