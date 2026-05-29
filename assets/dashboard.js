const API_BASE_STORAGE_KEY = "gameDataApiBase";
const IS_GITHUB_PAGES = location.hostname.endsWith("github.io");

function normalizeApiBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function resolveApiBase() {
  const params = new URLSearchParams(location.search);
  const configuredApiBase = normalizeApiBase(window.GAME_DATA_API_BASE);
  if (configuredApiBase) {
    return configuredApiBase;
  }
  const urlApiBase = normalizeApiBase(params.get("api"));
  if (urlApiBase) {
    localStorage.setItem(API_BASE_STORAGE_KEY, urlApiBase);
    return urlApiBase;
  }
  return normalizeApiBase(localStorage.getItem(API_BASE_STORAGE_KEY));
}

const API_BASE = resolveApiBase();
const DATA_URL = API_BASE ? `${API_BASE}/output/analysis.json` : "./output/analysis.json";
const RUN_URL = API_BASE ? `${API_BASE}/api/run` : "./api/run";
const RUNS_URL = API_BASE ? `${API_BASE}/api/runs` : "./api/runs";

let dashboardData = null;
let selectedIssueIndex = 0;
let selectedFiles = [];
let showDangerOnly = false;
let issueSort = "impact";
let activePoll = null;

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function number(value) {
  return new Intl.NumberFormat("ko-KR").format(Math.round(Number(value || 0)));
}

function currency(value) {
  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency: "KRW",
    maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function percent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function severityClass(severity) {
  if (severity === "위험") return "danger";
  if (severity === "주의") return "warning";
  return "neutral";
}

function baseIssues(data) {
  return [...(data.diagnosis?.content || []), ...(data.diagnosis?.revenue || [])].map((issue, index) => ({
    ...issue,
    _sourceIndex: index,
  }));
}

function severityRank(issue) {
  if (issue.severity === "위험") return 2;
  if (issue.severity === "주의") return 1;
  return 0;
}

function sortedIssues(data) {
  const issues = baseIssues(data);
  const sorters = {
    impact: (a, b) =>
      Number(b.impact_score || 0) - Number(a.impact_score || 0) ||
      Number(b.confidence || 0) - Number(a.confidence || 0),
    severity: (a, b) =>
      severityRank(b) - severityRank(a) ||
      Number(b.impact_score || 0) - Number(a.impact_score || 0),
    recent: (a, b) => b._sourceIndex - a._sourceIndex,
  };
  return issues.sort(sorters[issueSort] || sorters.impact);
}

function visibleIssues(data) {
  const issues = sortedIssues(data);
  return showDangerOnly ? issues.filter((issue) => issue.severity === "위험") : issues;
}

function contentRisk(row) {
  const wait = Number(row.avg_wait_sec || 0);
  const failure = Number(row.failure_rate || 0);
  const retry = Number(row.retry_after_failure_rate || 0);
  return failure * 0.5 + Math.min(wait / 60, 1) * 0.32 + (1 - retry) * 0.18;
}

function renderSummary(data) {
  const summary = data.summary || {};
  const quality = data.data_quality || {};
  const issues = baseIssues(data);
  const decisionNeeded = issues.filter((issue) => issue.severity === "위험").length;
  const impact = Math.round(issues.reduce((sum, issue) => sum + Number(issue.impact_score || 0), 0) * 100);

  $("#sideStatus").textContent = `${number(quality.normalized_rows)}건`;
  $("#boardSummary").innerHTML = `
    <div class="summary-line-item">
      <span>이슈</span>
      <strong>${number(issues.length)}</strong>
    </div>
    <div class="summary-line-item danger">
      <span>결정 필요</span>
      <strong>${number(decisionNeeded)}</strong>
    </div>
    <div class="summary-line-item">
      <span>영향도</span>
      <strong>${number(impact)}</strong>
    </div>
    <div class="summary-line-item success">
      <span>신뢰도</span>
      <strong>${percent(quality.quality_score)}</strong>
    </div>
    <div class="summary-line-item">
      <span>매출</span>
      <strong>${currency(summary.revenue)}</strong>
    </div>
  `;
}

function boardCard(issue, index) {
  return `
    <button class="board-card ${index === selectedIssueIndex ? "selected" : ""}" type="button" data-issue-index="${index}">
      <div class="board-card-top">
        <span class="badge ${severityClass(issue.severity)}">${escapeHtml(issue.severity || "정보")}</span>
      </div>
      <strong class="card-title">${escapeHtml(issue.title)}</strong>
    </button>
  `;
}

function renderBoard(data) {
  const issues = visibleIssues(data);
  $("#decisionBoard").innerHTML =
    issues.map((issue, index) => boardCard(issue, index)).join("") ||
    `<div class="board-empty">${showDangerOnly ? "위험 카드가 없습니다." : "표시할 이슈가 없습니다."}</div>`;

  document.querySelectorAll("[data-issue-index]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedIssueIndex = Number(button.dataset.issueIndex);
      renderBoard(dashboardData);
      renderDrawer(dashboardData);
    });
  });
}

function renderDrawer(data) {
  const issues = visibleIssues(data);
  const issue = issues[selectedIssueIndex] || issues[0];
  if (!issue) {
    $("#issueDrawer").innerHTML = `<div class="drawer-empty">${showDangerOnly ? "현재 위험 등급 이슈가 없습니다." : "표시할 이슈가 없습니다."}</div>`;
    return;
  }
  const evidence = issue.evidence || [];
  $("#issueDrawer").innerHTML = `
    <div class="drawer-header">
      <span class="badge ${severityClass(issue.severity)}">${escapeHtml(issue.severity || "정보")}</span>
      <h2>${escapeHtml(issue.title)}</h2>
      <p>${escapeHtml(issue.cause_candidate)}</p>
    </div>

    <div class="drawer-decision">
      <span>확인 순서</span>
      <strong>${escapeHtml(issue.recommendation || "누적 데이터와 UID 흐름을 추가 확인하세요.")}</strong>
    </div>

    <div class="drawer-section">
      <h3>핵심 근거</h3>
      <div class="drawer-evidence">
        ${
          evidence
            .map(
              (item, index) => `
                <div>
                  <span>근거 ${index + 1}</span>
                  <strong>${escapeHtml(item)}</strong>
                </div>
              `,
            )
            .join("") || `<div><span>근거</span><strong>추가 데이터 필요</strong></div>`
        }
      </div>
    </div>

    <div class="drawer-metrics">
      <div><span>영향도</span><strong>${percent(issue.impact_score)}</strong></div>
      <div><span>근거 강도</span><strong>${percent(issue.evidence_score)}</strong></div>
      <div><span>데이터 충분성</span><strong>${percent(issue.data_sufficiency)}</strong></div>
      <div><span>확신도</span><strong>${escapeHtml(issue.confidence_label || "-")}</strong></div>
    </div>

    <div class="drawer-section">
      <h3>메모</h3>
      <p>기준선이 부족하면 확정 원인이 아니라 먼저 볼 지점으로 다룹니다.</p>
    </div>
  `;
}

function renderAiBriefing(data) {
  const summary = data.summary || {};
  const quality = data.data_quality || {};
  const issues = sortedIssues(data);
  $("#qualityBadge").textContent = percent(quality.quality_score);
  $("#aiBriefing").innerHTML = `
    <div class="brief-line">
      <strong>핵심 판단</strong>
      ${issues[0] ? escapeHtml(`${issues[0].title}을 먼저 확인하세요.`) : "주요 이슈가 없습니다."}
    </div>
    <div class="brief-line">
      <strong>범위</strong>
      AU ${number(summary.active_users)}명, 이벤트 ${number(summary.events)}건, 세션 ${number(summary.sessions)}개 기준입니다.
    </div>
    <div class="brief-line">
      <strong>기준</strong>
      전일/전주 기준선이 없으면 확인 순서로 봅니다.
    </div>
  `;
}

function renderContentMap(data) {
  const rows = (data.content_health || []).filter((row) => row.group && row.group !== "상품");
  const maxRevenue = Math.max(...rows.map((row) => Number(row.revenue_after_content || 0)), 1);
  $("#contentMap").innerHTML = `
    <span class="axis-label y">매출 연결 높음</span>
    <span class="axis-label x">참여율 높음</span>
    ${rows
      .map((row) => {
        const risk = contentRisk(row);
        const cls = risk >= 0.62 ? "danger" : risk >= 0.4 ? "warning" : "";
        const x = 14 + Math.min(0.92, Number(row.participant_rate || 0) * 1.65) * 72;
        const y = 16 + Math.min(0.92, Number(row.revenue_after_content || 0) / maxRevenue) * 72;
        const size = 44 + Math.min(1, Number(row.participant_users || 0) / 8) * 34;
        return `
          <div class="bubble ${cls}" style="--x:${x}%; --y:${y}%; --size:${size}px">${number(row.participant_users)}</div>
          <div class="bubble-label" style="--x:${x}%; --y:${y}%; --size:${size}px">${escapeHtml(row.group)}</div>
        `;
      })
      .join("")}
  `;
}

function renderProducts(data) {
  const products = data.product_performance || [];
  $("#productList").innerHTML =
    products
      .map(
        (product) => `
          <article class="product-row">
            <div class="product-top">
              <span class="product-title">${escapeHtml(product.product)}</span>
              <span class="badge success">${currency(product.revenue)}</span>
            </div>
            <div class="product-metrics">
              <div><span>구매자</span><strong>${number(product.buyers)}명</strong></div>
              <div><span>구매 횟수</span><strong>${number(product.purchase_count)}회</strong></div>
              <div><span>평균 금액</span><strong>${currency(product.avg_amount)}</strong></div>
            </div>
            <div class="chip-list">
              ${(product.top_context_groups || [])
                .map(([group, count]) => `<span class="chip">${escapeHtml(group)} ${number(count)}</span>`)
                .join("")}
            </div>
          </article>
        `,
      )
      .join("") || `<p class="empty">상품 구매 데이터가 없습니다.</p>`;

  const topEvents = data.purchase_contexts?.top_preceding_events || [];
  $("#purchaseContext").innerHTML =
    topEvents
      .slice(0, 8)
      .map(([label, count]) => `<span class="chip">${escapeHtml(label)} ${number(count)}</span>`)
      .join("") || `<span class="chip">구매 직전 행동 없음</span>`;
}

function qualityRow(label, value) {
  return `
    <div class="quality-row">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function renderDataQuality(data) {
  const quality = data.data_quality || {};
  const storage = data.storage || {};
  $("#dataQuality").innerHTML = [
    qualityRow("품질 점수", percent(quality.quality_score)),
    qualityRow("원본 파일", `${number(quality.raw_files?.length || 0)}개`),
    qualityRow("정규화 로그", `${number(quality.normalized_rows)}건`),
    qualityRow("UID 누락", `${number(quality.missing_uid_rows)}건`),
    qualityRow("시간 누락", `${number(quality.missing_timestamp_rows)}건`),
    qualityRow("중복 후보", `${number(quality.duplicate_event_rows)}건`),
    storage.run_id ? qualityRow("실행 ID", storage.run_id) : "",
    storage.processed_dir ? qualityRow("가공 저장소", storage.processed_dir) : "",
    storage.warehouse_db ? qualityRow("DuckDB", storage.warehouse_db) : "",
    storage.latest_analysis_json ? qualityRow("보드 입력", storage.latest_analysis_json) : "",
  ].join("");

  const suggestions = data.language?.suggestions || [];
  const need = suggestions.filter((item) => item.needs_confirmation).length;
  $("#languageStatus").innerHTML = [
    qualityRow("발견 로그", `${number(suggestions.length)}개`),
    qualityRow("확인 필요", `${number(need)}개`),
    qualityRow("AI 추론 행", `${number(quality.inferred_language_rows)}건`),
  ].join("");
}

function render(data) {
  if (!data) return;
  dashboardData = data;
  applyControls();
  renderSummary(data);
  renderBoard(data);
  renderDrawer(data);
  renderAiBriefing(data);
  renderContentMap(data);
  renderProducts(data);
  renderDataQuality(data);
}

function applyControls() {
  const dangerToggle = $("#dangerToggle");
  if (dangerToggle) {
    dangerToggle.classList.toggle("active", showDangerOnly);
    dangerToggle.setAttribute("aria-pressed", String(showDangerOnly));
    dangerToggle.textContent = showDangerOnly ? "전체 보기" : "위험만";
  }
}

function setUploadStatus(message, tone = "muted") {
  const status = $("#uploadStatus");
  status.textContent = message;
  status.dataset.tone = tone;
}

function progressPercent(value) {
  const raw = Number(value || 0);
  const ratio = raw > 1 ? raw / 100 : raw;
  return Math.max(0, Math.min(100, Math.round(ratio * 100)));
}

function setRunProgress(value, visible = true) {
  const track = $("#runProgress");
  const bar = $("#runProgressBar");
  const label = $("#runProgressPercent");
  if (!track || !bar || !label) return;
  const percentValue = progressPercent(value);
  track.hidden = !visible;
  track.setAttribute("aria-hidden", String(!visible));
  label.hidden = !visible;
  label.setAttribute("aria-hidden", String(!visible));
  bar.style.width = `${percentValue}%`;
  label.textContent = `${percentValue}%`;
}

function runStatusLabel(payload) {
  const status = payload.status || "unknown";
  const progress = `${progressPercent(payload.progress)}%`;
  if (status === "queued") {
    const position = payload.queue_position ? ` #${payload.queue_position}` : "";
    return `대기 중${position} ${progress}`;
  }
  if (status === "running") return `${payload.message || "적용 중"} ${progress}`;
  if (status === "done") return "적용 완료 100%";
  if (status === "failed") return `실패: ${payload.error || payload.message || "확인 필요"}`;
  return payload.message || status;
}

function stopPolling() {
  if (activePoll) {
    clearTimeout(activePoll);
    activePoll = null;
  }
}

function resetUploadButton() {
  const button = $("#uploadButton");
  button.disabled = !selectedFiles.length;
  button.textContent = "분석 실행";
}

async function loadRunAnalysis(runId) {
  const response = await fetch(`${RUNS_URL}/${encodeURIComponent(runId)}/analysis?v=${Date.now()}`);
  if (!response.ok) throw new Error(`분석 결과를 읽을 수 없습니다. HTTP ${response.status}`);
  return response.json();
}

async function pollRun(runId) {
  try {
    const response = await fetch(`${RUNS_URL}/${encodeURIComponent(runId)}/status?v=${Date.now()}`);
    if (!response.ok) throw new Error(`상태를 읽을 수 없습니다. HTTP ${response.status}`);
    const status = await response.json();
    setRunProgress(status.progress, status.status !== "failed");
    setUploadStatus(runStatusLabel(status), status.status === "failed" ? "danger" : "muted");

    if (status.status === "done") {
      const payload = await loadRunAnalysis(runId);
      selectedIssueIndex = 0;
      render(payload);
      updateSelectedFiles([]);
      setRunProgress(1, true);
      setUploadStatus(`완료: ${status.uploaded_files?.join(", ") || runId}`, "success");
      stopPolling();
      resetUploadButton();
      return;
    }
    if (status.status === "failed") {
      setRunProgress(1, false);
      stopPolling();
      resetUploadButton();
      return;
    }
    activePoll = setTimeout(() => pollRun(runId), 1500);
  } catch (error) {
    setRunProgress(1, false);
    setUploadStatus(error.message, "danger");
    stopPolling();
    resetUploadButton();
  }
}

function updateSelectedFiles(files) {
  selectedFiles = [...files];
  const label = $("#fileLabel");
  const button = $("#uploadButton");
  if (IS_GITHUB_PAGES && !API_BASE) {
    label.textContent = selectedFiles.length ? selectedFiles.map((file) => file.name).join(", ") : "파일 선택 또는 드래그";
    button.disabled = true;
    setRunProgress(0, false);
    setUploadStatus("중앙 API 서버가 아직 연결되지 않았습니다.", "danger");
    return;
  }
  if (!selectedFiles.length) {
    label.textContent = "파일 선택 또는 드래그";
    button.disabled = true;
    setRunProgress(0, false);
    setUploadStatus("입력 대기");
    return;
  }
  label.textContent = selectedFiles.map((file) => file.name).join(", ");
  button.disabled = false;
  setRunProgress(0, false);
  setUploadStatus(`${number(selectedFiles.length)}개 파일 선택됨`);
}

async function uploadAndRun() {
  if (!selectedFiles.length) return;
  if (IS_GITHUB_PAGES && !API_BASE) {
    setUploadStatus("중앙 API 서버가 아직 연결되지 않았습니다.", "danger");
    return;
  }
  const button = $("#uploadButton");
  const form = new FormData();
  selectedFiles.forEach((file) => form.append("files", file));
  button.disabled = true;
  button.textContent = "적용 중";
  stopPolling();
  setRunProgress(0.02, true);
  setUploadStatus("업로드 중 2%");
  try {
    const response = await fetch(RUN_URL, { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `분석 실행 실패: HTTP ${response.status}`);
    }
    if (payload.run_id && payload.status && !payload.summary) {
      setRunProgress(payload.progress, true);
      setUploadStatus(`${runStatusLabel(payload)}: ${payload.run_id}`);
      activePoll = setTimeout(() => pollRun(payload.run_id), 900);
      return;
    }
    selectedIssueIndex = 0;
    render(payload);
    setRunProgress(1, true);
    setUploadStatus(`완료: ${payload.uploaded_files?.join(", ") || "파일 분석됨"}`, "success");
  } catch (error) {
    setRunProgress(1, false);
    setUploadStatus(error.message, "danger");
  } finally {
    if (!activePoll) {
      resetUploadButton();
    }
  }
}

async function loadDashboard() {
  try {
    let data;
    if (API_BASE) {
      const latest = await fetch(`${RUNS_URL}/latest?v=${Date.now()}`);
      if (latest.ok) {
        const latestPayload = await latest.json();
        data = latestPayload.run_id ? await loadRunAnalysis(latestPayload.run_id) : latestPayload;
      }
    }
    if (!data) {
      const response = await fetch(`${DATA_URL}?v=${Date.now()}`);
      if (!response.ok) throw new Error(`analysis.json을 읽을 수 없습니다. HTTP ${response.status}`);
      data = await response.json();
    }
    const firstDangerIndex = visibleIssues(data).findIndex((issue) => issue.severity === "위험");
    selectedIssueIndex = firstDangerIndex >= 0 ? firstDangerIndex : 0;
    render(data);
  } catch (error) {
    const hint = IS_GITHUB_PAGES && !API_BASE ? " 중앙 API 서버가 아직 연결되지 않았습니다." : "";
    $("#boardSummary").innerHTML = `
      <div class="error-box">
        ${escapeHtml(error.message + hint)} 로컬 서버 루트에서 실행 중인지 확인해주세요.
      </div>
    `;
  }
}

function downloadCurrentReport() {
  if (!dashboardData) return;
  const blob = new Blob([JSON.stringify(dashboardData, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const runId = dashboardData.storage?.run_id || "latest";
  anchor.href = url;
  anchor.download = `analysis-${runId}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

$("#reloadButton").addEventListener("click", loadDashboard);
$("#uploadButton").addEventListener("click", uploadAndRun);
$("#fileInput").addEventListener("change", (event) => updateSelectedFiles(event.target.files));
$("#sortSelect").addEventListener("change", (event) => {
  if (!dashboardData) return;
  const label = event.target.value;
  issueSort = label.includes("위험도") ? "severity" : label.includes("최근") ? "recent" : "impact";
  selectedIssueIndex = 0;
  render(dashboardData);
});
$("#dangerToggle").addEventListener("click", () => {
  if (!dashboardData) return;
  showDangerOnly = !showDangerOnly;
  selectedIssueIndex = 0;
  applyControls();
  renderBoard(dashboardData);
  renderDrawer(dashboardData);
});
$("#downloadButton").addEventListener("click", downloadCurrentReport);

const fileDrop = $("#fileDrop");
fileDrop.addEventListener("dragover", (event) => {
  event.preventDefault();
  fileDrop.classList.add("dragging");
});
fileDrop.addEventListener("dragleave", () => fileDrop.classList.remove("dragging"));
fileDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  fileDrop.classList.remove("dragging");
  updateSelectedFiles(event.dataTransfer.files);
});

if (IS_GITHUB_PAGES && !API_BASE) {
  setUploadStatus("중앙 API 서버가 아직 연결되지 않았습니다.", "danger");
}

loadDashboard();
