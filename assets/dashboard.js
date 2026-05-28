const DATA_URL = "./output/analysis.json";
const RUN_URL = "./api/run";

let dashboardData = null;
let selectedIssueIndex = 0;
let selectedFiles = [];

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

function allIssues(data) {
  return [...(data.diagnosis?.content || []), ...(data.diagnosis?.revenue || [])].sort(
    (a, b) => Number(b.confidence || 0) - Number(a.confidence || 0),
  );
}

function alertByTitle(data) {
  return (data.alerts || []).reduce((acc, alert) => {
    acc[alert.title] = alert;
    return acc;
  }, {});
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
  const issues = allIssues(data);
  const decisionNeeded = issues.filter((issue) => issue.severity === "위험").length;
  const impact = Math.round(issues.reduce((sum, issue) => sum + Number(issue.impact_score || 0), 0) * 100);

  $("#sideStatus").textContent = `${number(quality.normalized_rows)}건`;
  $("#boardSummary").innerHTML = `
    <article class="summary-card">
      <span>Open Issues</span>
      <strong>${number(issues.length)}</strong>
      <small>데이터가 만든 원인 후보</small>
    </article>
    <article class="summary-card danger">
      <span>Decision Needed</span>
      <strong>${number(decisionNeeded)}</strong>
      <small>위험 등급 카드</small>
    </article>
    <article class="summary-card">
      <span>Estimated Impact</span>
      <strong>${number(impact)}</strong>
      <small>후보 영향도 합산 점수</small>
    </article>
    <article class="summary-card success">
      <span>Data Confidence</span>
      <strong>${percent(quality.quality_score)}</strong>
      <small>${number(quality.normalized_rows)}건 정규화</small>
    </article>
    <article class="summary-card">
      <span>Revenue</span>
      <strong>${currency(summary.revenue)}</strong>
      <small>오늘 로우데이터 기준</small>
    </article>
  `;
}

function issueStatus(issue, index, firstDangerIndex) {
  if (issue.severity === "위험" && index === firstDangerIndex) return "결정 필요";
  if (issue.severity === "위험") return "원인 분석";
  if (issue.severity === "주의") return "관찰 중";
  return "효과 검증";
}

function issueOwner(issue) {
  if (issue.type?.includes("revenue") || issue.type?.includes("product") || issue.type?.includes("whale")) {
    return "사업/BM";
  }
  if (issue.type?.includes("content")) return "기획";
  return "분석";
}

function issueKpi(issue) {
  if (issue.type?.includes("revenue") || issue.type?.includes("product") || issue.type?.includes("whale")) {
    return ["매출", "PU", "ARPPU"];
  }
  if (issue.type?.includes("failure")) return ["실패율", "재도전", "이탈"];
  if (issue.type?.includes("friction")) return ["대기시간", "참여율", "재참여"];
  return ["참여율", "근거", "영향도"];
}

function boardCard(issue, index, alertLookup) {
  const alert = alertLookup[issue.title] || {};
  const evidence = issue.evidence || alert.top_evidence || [];
  const shortCause = String(issue.cause_candidate || "").replace("가능성", "가능");
  return `
    <button class="board-card ${index === selectedIssueIndex ? "selected" : ""}" type="button" data-issue-index="${index}">
      <div class="board-card-top">
        <span class="badge ${severityClass(issue.severity)}">${escapeHtml(issue.severity || "정보")}</span>
        <strong>${escapeHtml(issue.title)}</strong>
      </div>
      <p>${escapeHtml(shortCause)}</p>
      <div class="card-evidence">${escapeHtml(evidence[0] || "근거 수치 확인 필요")}</div>
      <div class="card-meta">
        <span>${escapeHtml(issueOwner(issue))}</span>
        <span>${percent(issue.impact_score)}</span>
        <span>${escapeHtml(issue.confidence_label || "-")}</span>
      </div>
      <div class="card-kpis">
        ${issueKpi(issue).map((kpi) => `<i>${escapeHtml(kpi)}</i>`).join("")}
      </div>
    </button>
  `;
}

function renderBoard(data) {
  const issues = allIssues(data);
  const alertLookup = alertByTitle(data);
  const columns = ["관찰 중", "원인 분석", "결정 필요", "효과 검증"];
  const grouped = Object.fromEntries(columns.map((column) => [column, []]));
  const firstDangerIndex = issues.findIndex((issue) => issue.severity === "위험");

  issues.forEach((issue, index) => {
    const status = issueStatus(issue, index, firstDangerIndex);
    grouped[status].push({ issue, index });
  });

  $("#decisionBoard").innerHTML = columns
    .map(
      (column) => `
        <section class="board-column">
          <header>
            <h3>${escapeHtml(column)}</h3>
            <span>${number(grouped[column].length)}</span>
          </header>
          <div class="board-card-list">
            ${
              grouped[column]
                .map(({ issue, index }) => boardCard(issue, index, alertLookup))
                .join("") || `<div class="board-empty">대기 중인 카드 없음</div>`
            }
          </div>
        </section>
      `,
    )
    .join("");

  document.querySelectorAll("[data-issue-index]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedIssueIndex = Number(button.dataset.issueIndex);
      renderBoard(dashboardData);
      renderDrawer(dashboardData);
    });
  });
}

function renderDrawer(data) {
  const issues = allIssues(data);
  const issue = issues[selectedIssueIndex] || issues[0];
  if (!issue) {
    $("#issueDrawer").innerHTML = `<div class="drawer-empty">표시할 이슈가 없습니다.</div>`;
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
      <span>추천 확인 순서</span>
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
      <h3>AI 노트</h3>
      <p>이 카드는 로우 로그를 UID 흐름으로 재구성한 뒤, 콘텐츠/상품/세션 맥락을 근거로 만든 원인 후보입니다. 첫날 데이터에서는 확정 원인이 아니라 우선 확인할 지점으로 다룹니다.</p>
    </div>
  `;
}

function renderAiBriefing(data) {
  const summary = data.summary || {};
  const quality = data.data_quality || {};
  const issues = allIssues(data);
  $("#qualityBadge").textContent = percent(quality.quality_score);
  $("#aiBriefing").innerHTML = `
    <div class="brief-line">
      <strong>핵심 판단</strong>
      ${issues[0] ? escapeHtml(`${issues[0].title}이 오늘 가장 먼저 볼 의사결정 후보입니다.`) : "주요 이슈가 없습니다."}
    </div>
    <div class="brief-line">
      <strong>분석 범위</strong>
      AU ${number(summary.active_users)}명, 이벤트 ${number(summary.events)}건, 세션 ${number(summary.sessions)}개를 UID 흐름으로 재구성했습니다.
    </div>
    <div class="brief-line">
      <strong>해석 기준</strong>
      전일/전주 기준선이 없으므로 오늘은 "하락 원인"이 아니라 "위험 후보와 확인 순서"를 보여줍니다.
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
  dashboardData = data;
  renderSummary(data);
  renderBoard(data);
  renderDrawer(data);
  renderAiBriefing(data);
  renderContentMap(data);
  renderProducts(data);
  renderDataQuality(data);
}

function setUploadStatus(message, tone = "muted") {
  const status = $("#uploadStatus");
  status.textContent = message;
  status.dataset.tone = tone;
}

function updateSelectedFiles(files) {
  selectedFiles = [...files];
  const label = $("#fileLabel");
  const button = $("#uploadButton");
  if (!selectedFiles.length) {
    label.textContent = "파일 선택 또는 드래그";
    button.disabled = true;
    setUploadStatus("입력 대기");
    return;
  }
  label.textContent = selectedFiles.map((file) => file.name).join(", ");
  button.disabled = false;
  setUploadStatus(`${number(selectedFiles.length)}개 파일 선택됨`);
}

async function uploadAndRun() {
  if (!selectedFiles.length) return;
  const button = $("#uploadButton");
  const form = new FormData();
  selectedFiles.forEach((file) => form.append("files", file));
  button.disabled = true;
  button.textContent = "분석 중";
  setUploadStatus("업로드 후 엔진 실행 중입니다.");
  try {
    const response = await fetch(RUN_URL, { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `분석 실행 실패: HTTP ${response.status}`);
    }
    selectedIssueIndex = 0;
    render(payload);
    setUploadStatus(`완료: ${payload.uploaded_files?.join(", ") || "파일 분석됨"}`, "success");
  } catch (error) {
    setUploadStatus(error.message, "danger");
  } finally {
    button.disabled = !selectedFiles.length;
    button.textContent = "분석 실행";
  }
}

async function loadDashboard() {
  try {
    const response = await fetch(`${DATA_URL}?v=${Date.now()}`);
    if (!response.ok) throw new Error(`analysis.json을 읽을 수 없습니다. HTTP ${response.status}`);
    const data = await response.json();
    const firstDangerIndex = allIssues(data).findIndex((issue) => issue.severity === "위험");
    selectedIssueIndex = firstDangerIndex >= 0 ? firstDangerIndex : 0;
    render(data);
  } catch (error) {
    $("#boardSummary").innerHTML = `
      <div class="error-box">
        ${escapeHtml(error.message)} 로컬 서버 루트에서 실행 중인지 확인해주세요.
      </div>
    `;
  }
}

$("#reloadButton").addEventListener("click", loadDashboard);
$("#uploadButton").addEventListener("click", uploadAndRun);
$("#fileInput").addEventListener("change", (event) => updateSelectedFiles(event.target.files));

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

loadDashboard();
