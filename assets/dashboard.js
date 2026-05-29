const API_BASE_STORAGE_KEY = "gameDataApiBase";
const IS_GITHUB_PAGES = location.hostname.endsWith("github.io");
const IS_LOCAL_DASHBOARD = ["localhost", "127.0.0.1", "::1"].includes(location.hostname);

function normalizeApiBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function resolveApiBase() {
  const params = new URLSearchParams(location.search);
  const urlApiBase = normalizeApiBase(params.get("api"));
  if (urlApiBase) {
    if (!IS_LOCAL_DASHBOARD) {
      localStorage.setItem(API_BASE_STORAGE_KEY, urlApiBase);
    }
    return urlApiBase;
  }
  if (IS_LOCAL_DASHBOARD) {
    return "";
  }
  const configuredApiBase = normalizeApiBase(window.GAME_DATA_API_BASE);
  if (configuredApiBase) {
    return configuredApiBase;
  }
  return normalizeApiBase(localStorage.getItem(API_BASE_STORAGE_KEY));
}

const API_BASE = resolveApiBase();
const DATA_URL = API_BASE ? `${API_BASE}/output/analysis.json` : "./output/analysis.json";
const RUN_URL = API_BASE ? `${API_BASE}/api/run` : "./api/run";
const RUNS_URL = API_BASE ? `${API_BASE}/api/runs` : "./api/runs";
const LANGUAGE_URL = API_BASE ? `${API_BASE}/api/language` : "./api/language";
const LANGUAGE_PRESETS_URL = API_BASE ? `${API_BASE}/api/language/presets` : "./api/language/presets";
const LANGUAGE_ACTIVE_URL = API_BASE ? `${API_BASE}/api/language/active` : "./api/language/active";

let dashboardData = null;
let languageConfig = null;
let languagePresets = [];
let activePresetId = "default";
let selectedIssueIndex = 0;
let selectedFiles = [];
let showDangerOnly = false;
let issueSort = "impact";
let activePoll = null;
let quickMappingCode = "";

const EVENT_TYPE_OPTIONS = [
  ["event", "일반"],
  ["session_start", "접속/시작"],
  ["content_enter", "콘텐츠 진입"],
  ["content_success", "성공/완료"],
  ["content_fail", "실패"],
  ["match_issue", "매칭/대기 문제"],
  ["product_view", "상품 조회"],
  ["purchase", "결제"],
  ["reward_claim", "보상"],
  ["exit", "이탈"],
];

const $ = (selector) => document.querySelector(selector);

function apiUnavailable() {
  return IS_GITHUB_PAGES && !API_BASE;
}

function withQuery(url, params) {
  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (!entries.length) return url;
  const query = new URLSearchParams(entries).toString();
  return `${url}${url.includes("?") ? "&" : "?"}${query}`;
}

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

function codeLabel(raw) {
  const code = String(raw || "").trim();
  if (!code) return "-";
  const configured = languageConfig?.event_labels?.[code];
  const label = configured?.label || code;
  return label === code ? code : `${label} / ${code}`;
}

function renderCodeWithAction(raw) {
  const code = String(raw || "").trim();
  if (!code || code === "-") return `<span class="code-with-action"><span>-</span></span>`;
  return `
    <button
      class="code-with-action"
      type="button"
      data-map-code="${escapeHtml(code)}"
      title="로그 이름 지정"
      aria-label="${escapeHtml(`${code} 로그 이름 지정`)}"
    >
      <span>${escapeHtml(codeLabel(code))}</span>
    </button>
  `;
}

function renderPathWithActions(steps) {
  const list = (steps || []).map((step) => String(step || "").trim()).filter(Boolean);
  if (!list.length) return "-";
  return list.map((step) => renderCodeWithAction(step)).join(`<span class="inline-arrow">→</span>`);
}

function extractLogCodesFromText(value) {
  const matches = String(value || "").match(/[A-Za-z0-9_:-]{3,}/g) || [];
  return matches
    .map((item) => item.replace(/^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g, ""))
    .filter(
      (item) =>
        item &&
        (item.length >= 4 || item.includes("_")) &&
        /[0-9_]/.test(item) &&
        !/^\d{4}-\d{2}-\d{2}$/.test(item),
    );
}

function knownLogCodes(data) {
  const behavior = data?.behavior_flow || {};
  const codes = new Set();
  Object.keys(languageConfig?.event_labels || {}).forEach((code) => codes.add(String(code)));
  (data?.language?.suggestions || []).forEach((item) => item?.raw && codes.add(String(item.raw)));
  (behavior.participation || behavior.top_codes || []).map(behaviorCode).forEach((item) => item?.code && codes.add(String(item.code)));
  (behavior.common_paths || []).forEach((item) => pathSteps(item).forEach((step) => codes.add(String(step))));
  (behavior.transition_rates || behavior.top_transitions || []).map(behaviorCode).forEach((item) => {
    if (item?.from) codes.add(String(item.from));
    if (item?.to) codes.add(String(item.to));
  });
  (behavior.loop_patterns || []).forEach((item) => item?.from && codes.add(String(item.from)));
  (behavior.short_interval_repeats || []).forEach((item) => item?.code && codes.add(String(item.code)));
  [...(behavior.entry_events || []), ...(behavior.exit_events || [])].forEach((item) => {
    if (item?.code) codes.add(String(item.code));
    if (item?.label) codes.add(String(item.label));
  });
  return codes;
}

function issueMappingCandidates(issue, data) {
  const source = [issue?.title, issue?.cause_candidate, issue?.recommendation, ...(issue?.evidence || [])].join(" ");
  const known = knownLogCodes(data);
  return [...new Set(extractLogCodesFromText(source))].filter((code) => known.has(code)).slice(0, 12);
}

function durationLabel(seconds) {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}시간 ${minutes}분`;
  if (minutes) return `${minutes}분 ${secs}초`;
  return `${secs}초`;
}

function behaviorCode(item) {
  if (Array.isArray(item)) {
    return {
      code: item[0],
      count: item[1],
      label: item[0],
      user_count: item[2],
    };
  }
  return item || {};
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
  const mappingCandidates = issueMappingCandidates(issue, data);
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

    ${
      mappingCandidates.length
        ? `
          <div class="drawer-section quick-map-section">
            <h3>로그 이름 빠른 지정</h3>
            <div class="quick-map-chip-list">
              ${mappingCandidates.map((code) => renderCodeWithAction(code)).join("")}
            </div>
          </div>
        `
        : ""
    }

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

function pathSteps(item) {
  if (!item) return [];
  const rawSteps =
    (Array.isArray(item.labels) && item.labels.length && item.labels) ||
    (Array.isArray(item.path) && item.path.length && item.path) ||
    String(item.path_text || item.code_path_text || "")
      .split(/\s*->\s*/)
      .filter(Boolean);
  return rawSteps.map((step) => String(step).trim()).filter(Boolean);
}

function behaviorItemLabel(item) {
  return codeLabel(item?.label || item?.code || item?.from || item?.to || "-");
}

function behaviorPathText(item) {
  const steps = pathSteps(item);
  return steps.length ? steps.map(codeLabel).join(" → ") : "-";
}

function renderBehaviorFlow(data) {
  const behavior = data.behavior_flow || {};
  const participation = (behavior.participation || behavior.top_codes || []).map(behaviorCode);
  const transitions = (behavior.transition_rates || behavior.top_transitions || []).map(behaviorCode);
  const commonPaths = behavior.common_paths || [];
  const loopPatterns = behavior.loop_patterns || [];
  const shortIntervalRepeats = behavior.short_interval_repeats || [];
  const outliers = behavior.outliers || [];
  const contentRows = behavior.content_participation || [];
  const entryEvents = behavior.entry_events || [];
  const exitEvents = behavior.exit_events || [];
  const badge = $("#behaviorBadge");
  const container = $("#behaviorFlow");
  if (!container) return;

  if (badge) {
    badge.textContent = `${number(behavior.user_count)}명 / ${number(behavior.event_count)}건`;
  }

  if (!participation.length && !transitions.length && !commonPaths.length) {
    container.innerHTML = `<p class="empty">행동 흐름 데이터가 없습니다.</p>`;
    return;
  }

  const strongestPath = commonPaths[0];
  const strongestPathSteps = pathSteps(strongestPath).slice(0, 7);
  const strongestOutlier = outliers[0];
  const dominantTransition = transitions[0];
  const strongestLoop = loopPatterns[0];
  const strongestShortRepeat = shortIntervalRepeats[0];
  const stopPoint = exitEvents[0];
  const pathRate = Number(strongestPath?.user_rate || 0);
  const transitionRate = Number(dominantTransition?.transition_rate || dominantTransition?.user_rate || 0);
  const loopRate = Number(strongestLoop?.user_rate || 0);
  const stopRate = Number(stopPoint?.user_rate || 0);
  const pathSentence = strongestPath
    ? `${number(strongestPath.user_count)}명 중 ${percent(pathRate)}에서 같은 행동 순서가 관찰됩니다.`
    : "반복해서 관찰된 행동 순서가 아직 부족합니다.";
  const transitionSentence = dominantTransition
    ? `${dominantTransition.from || "-"} → ${dominantTransition.to || "-"} 다음 행동 비율은 ${percent(transitionRate)}입니다.`
    : "다음 행동 비율을 계산할 구간이 부족합니다.";
  const loopSentence = strongestLoop
    ? `${strongestLoop.from || "-"} 반복이 ${number(strongestLoop.count)}회 관찰됩니다.`
    : "같은 코드 반복은 아직 두드러지지 않습니다.";
  const shortRepeatSentence = strongestShortRepeat
    ? `${strongestShortRepeat.code || "-"}는 ${number(strongestShortRepeat.window_seconds)}초 안 최대 ${number(strongestShortRepeat.max_events_in_window)}회 생성됩니다.`
    : "";

  const journeySteps =
    strongestPathSteps
      .map(
        (step, index) => `
          <div class="flow-step">
            <span>${String(index + 1).padStart(2, "0")}</span>
            <strong>${renderCodeWithAction(step)}</strong>
          </div>
        `,
      )
      .join(`<span class="flow-connector">→</span>`) || `<p class="empty">반복 관찰된 순서 데이터가 없습니다.</p>`;

  const insightCards = [
    {
      tone: pathRate >= 0.8 ? "strong" : "watch",
      label: "많이 나온 순서",
      title: strongestPath ? behaviorPathText(strongestPath) : "공통 순서 부족",
      finding: strongestPath
        ? pathRate >= 0.8
          ? "현재 데이터에서 가장 많이 반복 관찰된 행동 순서입니다."
          : "일부 유저에게서 반복 관찰된 행동 순서입니다."
        : "공통 행동 순서가 충분히 반복되지 않았습니다.",
      evidence: strongestPath
        ? `${number(strongestPath.user_count)}명 · ${percent(pathRate)} · ${number(strongestPath.occurrence_count)}회`
        : "근거 부족",
    },
    {
      tone: transitionRate >= 0.8 ? "strong" : "watch",
      label: "다음 행동 비율",
      title: dominantTransition ? `${dominantTransition.from || "-"} → ${dominantTransition.to || "-"}` : "전환 부족",
      finding: dominantTransition
        ? transitionRate >= 0.8
          ? "앞 행동 이후 같은 다음 행동이 많이 관찰됩니다."
          : "앞 행동 이후 다음 행동이 여러 방향으로 나뉩니다."
        : "전환을 판단할 만큼 이어진 행동이 부족합니다.",
      evidence: dominantTransition
        ? `${number(dominantTransition.from_user_count || dominantTransition.user_count)}명 중 ${number(dominantTransition.user_count)}명 · ${percent(transitionRate)}`
        : "근거 부족",
    },
    {
      tone: strongestShortRepeat ? "danger" : strongestLoop ? "watch" : "calm",
      label: strongestShortRepeat ? "짧은 시간 반복" : "반복 발생",
      title: strongestShortRepeat
        ? codeLabel(strongestShortRepeat.code || "-")
        : strongestLoop
          ? `${strongestLoop.from || "-"} 반복`
          : "뚜렷한 반복 없음",
      finding: strongestShortRepeat
        ? "같은 UID에서 짧은 시간 안에 같은 코드가 여러 번 생성됩니다. 의미 확인 우선순위가 높습니다."
        : strongestLoop
          ? "같은 코드가 연속 또는 반복으로 관찰됩니다. 의미는 로그 이름을 맞춘 뒤 확인합니다."
          : "같은 코드 반복이 두드러지지 않습니다.",
      evidence: strongestShortRepeat
        ? `${number(strongestShortRepeat.user_count)}명 · ${number(strongestShortRepeat.window_seconds)}초 안 최대 ${number(strongestShortRepeat.max_events_in_window)}회`
        : strongestLoop
          ? `${number(strongestLoop.user_count)}명 · ${percent(loopRate)} · ${number(strongestLoop.count)}회`
          : "관찰 낮음",
    },
    {
      tone: stopRate >= 0.7 ? "watch" : "calm",
      label: "마지막 행동",
      title: stopPoint ? behaviorItemLabel(stopPoint) : "종료 분산",
      finding: stopPoint
        ? stopRate >= 0.7
          ? "마지막으로 관찰된 행동이 한 코드에 많이 모입니다."
          : "마지막으로 관찰된 행동이 여러 코드로 나뉩니다."
        : "마지막 행동 분포가 부족합니다.",
      evidence: stopPoint ? `${number(stopPoint.user_count)}명 · ${percent(stopRate)}` : "근거 부족",
    },
  ];

  const insightRows = insightCards
    .map(
      (item) => `
        <article class="flow-insight-card ${item.tone}">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(item.title)}</strong>
          <p>${escapeHtml(item.finding)}</p>
          <em>${escapeHtml(item.evidence)}</em>
        </article>
      `,
    )
    .join("");

  const transitionFocusRows =
    transitions
      .slice(0, 6)
      .map((item) => {
        const rate = Number(item.transition_rate || item.user_rate || 0);
        return `
          <div class="flow-transition-row">
            <div>
              <strong>${renderPathWithActions([item.from || "-", item.to || "-"])}</strong>
              <span>${number(item.from_user_count || item.user_count)}명 중 ${number(item.user_count)}명 · ${number(item.count)}회</span>
            </div>
            <div class="flow-bar" aria-label="전환율 ${percent(rate)}">
              <span style="width: ${Math.max(4, Math.min(100, rate * 100))}%"></span>
            </div>
            <em>${percent(rate)}</em>
          </div>
        `;
      })
      .join("") || `<p class="empty">전환 데이터가 없습니다.</p>`;

  const commonPathRows = commonPaths
    .slice(0, 8)
    .map(
      (item) => `
        <article class="common-path-card">
          <strong>${renderPathWithActions(pathSteps(item))}</strong>
          <span>${number(item.user_count)}명 · ${percent(item.user_rate)} · ${number(item.occurrence_count)}회</span>
        </article>
      `,
    )
    .join("");

  const outlierRows = outliers
    .slice(0, 8)
    .map(
      (item) => `
        <div class="outlier-row">
          <strong>${escapeHtml(item.title || "-")}</strong>
          <span>${escapeHtml(item.evidence || "")}</span>
        </div>
      `,
    )
    .join("");

  const participationRows = participation
    .slice(0, 12)
    .map(
      (item) => `
        <article class="participation-card">
          <div class="participation-card-head">
            <div>
              <strong>${escapeHtml(codeLabel(item.code || item.label || "-"))}</strong>
              <span>${escapeHtml(item.code || "-")}</span>
            </div>
            <em>${percent(item.user_rate)}</em>
          </div>
          <div class="behavior-stat-grid">
            <div><span>참여 유저</span><strong>${number(item.user_count)}명</strong></div>
            <div><span>이벤트</span><strong>${number(item.count)}건</strong></div>
            <div><span>인당 평균</span><strong>${Number(item.events_per_user || 0).toFixed(1)}건</strong></div>
          </div>
          <div class="behavior-code-list">
            <span>${escapeHtml(item.event_type || "event")}</span>
            ${item.group ? `<span>${escapeHtml(item.group)}</span>` : ""}
          </div>
        </article>
      `,
    )
    .join("");

  const transitionRows = transitions
    .slice(0, 8)
    .map(
      (item) => `
        <div class="transition-row">
          <strong>${renderPathWithActions([item.from || "-", item.to || "-"])}</strong>
          <span>${number(item.from_user_count || item.user_count)}명 중 ${number(item.user_count)}명 · 전환율 ${percent(item.transition_rate || item.user_rate)} · ${number(item.count)}회</span>
        </div>
      `,
    )
    .join("");

  const loopRows = loopPatterns
    .slice(0, 6)
    .map(
      (item) => `
        <div class="transition-row loop">
          <strong>${renderCodeWithAction(item.from || "-")} 반복</strong>
          <span>${number(item.user_count)}명 · ${number(item.count)}회 · ${percent(item.user_rate)}</span>
        </div>
      `,
    )
    .join("");

  const shortRepeatRows = shortIntervalRepeats
    .slice(0, 8)
    .map(
      (item) => `
        <div class="transition-row danger">
          <strong>${renderCodeWithAction(item.code || "-")}</strong>
          <span>${number(item.user_count)}명 · ${number(item.window_seconds)}초 안 최대 ${number(item.max_events_in_window)}회 · 반복 구간 ${number(item.burst_window_count)}개</span>
        </div>
      `,
    )
    .join("");

  const contentParticipation = contentRows
    .slice(0, 8)
    .map(
      (item) => `
        <div class="content-participation-row">
          <div>
            <strong>${escapeHtml(item.group || "-")}</strong>
            <span>${number(item.code_count)}개 코드 · ${number(item.event_count)}건</span>
          </div>
          <em>${number(item.user_count)}명 · ${percent(item.user_rate)}</em>
        </div>
      `,
    )
    .join("");

  const distributionRows = (items, title) => `
    <div class="behavior-block">
      <div class="behavior-block-title">
        <span>${title}</span>
        <strong>${number(items.length)}개</strong>
      </div>
      <div class="event-distribution">
        ${
          items
            .slice(0, 6)
            .map(
              (item) => `
                <div>
                  <strong>${renderCodeWithAction(item.code || item.label || "-")}</strong>
                  <span>${number(item.user_count)}명 · ${percent(item.user_rate)}</span>
                </div>
              `,
            )
            .join("") || `<p class="empty">분포 데이터가 없습니다.</p>`
        }
      </div>
    </div>
  `;

  container.innerHTML = `
    <section class="flow-analysis-card">
      <div class="flow-analysis-copy">
        <span>데이터 관찰</span>
        <h3>${escapeHtml(pathSentence)}</h3>
        <p>${escapeHtml(`${transitionSentence} ${loopSentence}${shortRepeatSentence ? ` ${shortRepeatSentence}` : ""}`)}</p>
      </div>
      <div class="flow-analysis-metrics">
        <div><span>분석 유저</span><strong>${number(behavior.user_count)}명</strong></div>
        <div><span>행동 로그</span><strong>${number(behavior.event_count)}건</strong></div>
        <div><span>최다 순서</span><strong>${percent(pathRate)}</strong></div>
      </div>
    </section>

    <section class="journey-map">
      <div class="journey-header">
        <div>
          <span>가장 많이 나온 순서</span>
          <strong>${strongestPath ? `${number(strongestPath.user_count)}명 · ${number(strongestPath.occurrence_count)}회 관찰` : "반복 관찰된 순서 없음"}</strong>
        </div>
        <em>${strongestPath ? percent(pathRate) : "-"}</em>
      </div>
      <div class="journey-rail">${journeySteps}</div>
    </section>

    <section class="flow-insight-grid">${insightRows}</section>

    <section class="flow-transition-board">
      <div class="behavior-block-title">
        <span>다음 행동 비율</span>
        <strong>상위 ${number(Math.min(transitions.length, 6))}개 전환</strong>
      </div>
      ${transitionFocusRows}
    </section>

    <details class="behavior-detail">
      <summary>
        <span>근거 데이터 펼치기</span>
        <strong>공통 ${number(commonPaths.length)} · 튀는 부분 ${number(outliers.length)} · 이벤트 ${number(participation.length)} · 짧은 반복 ${number(shortIntervalRepeats.length)}</strong>
      </summary>
      <div class="behavior-columns">
        <div class="behavior-block">
          <div class="behavior-block-title">
            <span>공통 행동 패턴</span>
            <strong>상위 ${number(Math.min(commonPaths.length, 8))}개</strong>
          </div>
          <div class="common-path-list">${commonPathRows || `<p class="empty">공통 경로 데이터가 없습니다.</p>`}</div>
        </div>
        <div class="behavior-block">
          <div class="behavior-block-title">
            <span>튀는 부분</span>
            <strong>상위 ${number(Math.min(outliers.length, 8))}개</strong>
          </div>
          <div class="outlier-list">${outlierRows || `<p class="empty">뚜렷한 이상 패턴은 아직 없습니다.</p>`}</div>
        </div>
      </div>
      <div class="behavior-block">
        <div class="behavior-block-title">
          <span>이벤트 참여</span>
          <strong>상위 ${number(Math.min(participation.length, 12))}개</strong>
        </div>
        <div class="participation-grid">${participationRows}</div>
      </div>
      <div class="behavior-columns">
        <div class="behavior-block">
          <div class="behavior-block-title">
            <span>콘텐츠/그룹 참여</span>
            <strong>상위 ${number(Math.min(contentRows.length, 8))}개</strong>
          </div>
          <div class="content-participation-list">
            ${contentParticipation || `<p class="empty">아직 그룹 매핑이 없어 코드 단위로만 표시됩니다.</p>`}
          </div>
        </div>
        <div class="behavior-block">
          <div class="behavior-block-title">
            <span>전환율</span>
            <strong>상위 ${number(Math.min(transitions.length, 8))}개</strong>
          </div>
          <div class="transition-list">${transitionRows || `<p class="empty">전환 데이터가 없습니다.</p>`}</div>
        </div>
      </div>
      <div class="behavior-block">
        <div class="behavior-block-title">
          <span>짧은 시간 반복</span>
          <strong>상위 ${number(Math.min(shortIntervalRepeats.length, 8))}개</strong>
        </div>
        <div class="transition-list compact">${shortRepeatRows || `<p class="empty">짧은 시간 반복 신호가 두드러지지 않습니다.</p>`}</div>
      </div>
      <div class="behavior-block">
        <div class="behavior-block-title">
          <span>연속 반복</span>
          <strong>상위 ${number(Math.min(loopPatterns.length, 6))}개</strong>
        </div>
        <div class="transition-list compact">${loopRows || `<p class="empty">반복 루프가 두드러지지 않습니다.</p>`}</div>
      </div>
      <div class="behavior-columns compact">
        ${distributionRows(entryEvents, "첫 행동 분포")}
        ${distributionRows(exitEvents, "마지막 행동 분포")}
      </div>
    </details>

    <p class="behavior-note">${escapeHtml(behavior.note || "코드 의미는 확정하지 않고 순서와 빈도만 보여줍니다.")}</p>
  `;
}

function eventTypeOptions(selectedValue) {
  return EVENT_TYPE_OPTIONS.map(
    ([value, label]) =>
      `<option value="${escapeHtml(value)}" ${value === selectedValue ? "selected" : ""}>${escapeHtml(label)}</option>`,
  ).join("");
}

function mappingValue(raw, field, fallback = "") {
  const configured = languageConfig?.event_labels?.[raw];
  if (configured && configured[field] !== undefined && configured[field] !== null) {
    return configured[field];
  }
  return fallback ?? "";
}

function activePresetName() {
  return (
    languagePresets.find((preset) => preset.id === activePresetId)?.name ||
    languageConfig?.preset?.name ||
    activePresetId ||
    "기본 프리셋"
  );
}

function renderPresetControls() {
  const select = $("#presetSelect");
  const status = $("#presetStatus");
  const createButton = $("#createPresetButton");
  const input = $("#presetNameInput");
  if (!select) return;

  const presets = languagePresets.length
    ? languagePresets
    : [{ id: activePresetId, name: activePresetName(), event_label_count: 0 }];

  select.innerHTML = presets
    .map(
      (preset) =>
        `<option value="${escapeHtml(preset.id)}" ${preset.id === activePresetId ? "selected" : ""}>${escapeHtml(preset.name || preset.id)}</option>`,
    )
    .join("");
  select.disabled = apiUnavailable();

  if (createButton) createButton.disabled = apiUnavailable();
  if (input) input.disabled = apiUnavailable();
  if (status) {
    status.textContent = apiUnavailable()
      ? "중앙 API 연결 후 프리셋을 저장할 수 있습니다."
      : `현재 프리셋: ${activePresetName()}`;
  }
}

async function loadLanguagePresets() {
  if (apiUnavailable()) {
    languagePresets = [{ id: "default", name: "기본 프리셋", event_label_count: 0 }];
    activePresetId = "default";
    renderPresetControls();
    return;
  }
  try {
    const response = await fetch(withQuery(LANGUAGE_PRESETS_URL, { v: Date.now() }));
    if (!response.ok) throw new Error(`프리셋을 읽을 수 없습니다. HTTP ${response.status}`);
    const payload = await response.json();
    languagePresets = payload.presets || [];
    activePresetId = payload.active_preset_id || activePresetId || "default";
  } catch {
    languagePresets = [{ id: activePresetId, name: activePresetName(), event_label_count: 0 }];
  }
  renderPresetControls();
}

async function switchLanguagePreset(presetId) {
  if (!presetId || apiUnavailable()) return;
  const status = $("#presetStatus");
  activePresetId = presetId;
  renderPresetControls();
  if (status) status.textContent = "프리셋 전환 중";
  try {
    const response = await fetch(LANGUAGE_ACTIVE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset_id: presetId }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `프리셋 전환 실패: HTTP ${response.status}`);
    }
    languagePresets = payload.presets || languagePresets;
    activePresetId = payload.active_preset_id || presetId;
    await loadLanguageConfig(false);
    renderLanguageSettings(dashboardData || {});
    renderDataQuality(dashboardData || {});
    if (status) status.textContent = `${activePresetName()} 프리셋 사용 중. 다음 분석부터 적용됩니다.`;
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    renderPresetControls();
  }
}

async function createLanguagePreset() {
  if (apiUnavailable()) return;
  const input = $("#presetNameInput");
  const status = $("#presetStatus");
  const createButton = $("#createPresetButton");
  const name = input?.value?.trim() || "";
  if (!name) {
    if (status) status.textContent = "프리셋 이름을 입력하세요.";
    return;
  }
  if (createButton) createButton.disabled = true;
  if (status) status.textContent = "프리셋 생성 중";
  try {
    const response = await fetch(LANGUAGE_PRESETS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `프리셋 생성 실패: HTTP ${response.status}`);
    }
    languagePresets = payload.presets || [];
    activePresetId = payload.active_preset_id || payload.preset?.id || activePresetId;
    if (input) input.value = "";
    await loadLanguageConfig(false);
    renderLanguageSettings(dashboardData || {});
    renderDataQuality(dashboardData || {});
    if (status) status.textContent = `${activePresetName()} 프리셋이 생성되었습니다.`;
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    if (createButton) createButton.disabled = apiUnavailable();
    renderPresetControls();
  }
}

function renderLanguageSettings(data) {
  const suggestions = data.language?.suggestions || [];
  const configured = languageConfig?.event_labels || {};
  const rows = suggestions
    .map((item) => ({
      ...item,
      configured: Boolean(configured[item.raw]),
    }))
    .sort(
      (a, b) =>
        Number(b.needs_confirmation || b.configured) - Number(a.needs_confirmation || a.configured) ||
        Number(b.count || 0) - Number(a.count || 0),
    )
    .slice(0, 40);

  const container = $("#languageMappings");
  const saveButton = $("#saveLanguageButton");
  const status = $("#languageSaveStatus");
  if (!container || !saveButton || !status) return;

  renderPresetControls();

  if (!rows.length) {
    container.innerHTML = `<p class="empty">설정할 로그 코드가 없습니다.</p>`;
    saveButton.disabled = true;
    status.textContent = "";
    return;
  }

  container.innerHTML = rows
    .map((item) => {
      const raw = String(item.raw || "");
      const label = mappingValue(raw, "label", item.suggested_label || raw);
      const eventType = mappingValue(raw, "event_type", item.event_type || "event");
      const group = mappingValue(raw, "group", item.group || "");
      return `
        <article class="mapping-row" data-log-code="${escapeHtml(raw)}">
          <div class="mapping-code">
            <strong>${escapeHtml(raw)}</strong>
            <span>${number(item.count)}건 · ${item.configured ? "설정됨" : item.needs_confirmation ? "확인 필요" : "관찰됨"}</span>
          </div>
          <label>
            <span>이름</span>
            <input data-map-field="label" value="${escapeHtml(label)}" placeholder="예: 접속 시작" />
          </label>
          <label>
            <span>유형</span>
            <select data-map-field="event_type">${eventTypeOptions(eventType)}</select>
          </label>
          <label>
            <span>그룹</span>
            <input data-map-field="group" value="${escapeHtml(group || "")}" placeholder="예: 튜토리얼, 상점" />
          </label>
        </article>
      `;
    })
    .join("");

  saveButton.disabled = apiUnavailable();
  status.textContent = saveButton.disabled
    ? "중앙 API 연결 후 저장할 수 있습니다."
    : `${activePresetName()}에 저장하면 다음 분석부터 적용됩니다.`;
  saveButton.onclick = saveLanguageMappings;
}

function collectLanguageMappings() {
  return [...document.querySelectorAll(".mapping-row")]
    .map((row) => {
      const valueOf = (field) => row.querySelector(`[data-map-field="${field}"]`)?.value?.trim() || "";
      return {
        raw: row.dataset.logCode || "",
        label: valueOf("label"),
        event_type: valueOf("event_type") || "event",
        group: valueOf("group"),
      };
    })
    .filter((item) => item.raw && item.label);
}

async function loadLanguageConfig(loadPresets = true) {
  if (apiUnavailable()) {
    languageConfig = null;
    await loadLanguagePresets();
    return;
  }
  if (loadPresets) {
    await loadLanguagePresets();
  }
  try {
    const response = await fetch(withQuery(LANGUAGE_URL, { preset: activePresetId, v: Date.now() }));
    languageConfig = response.ok ? await response.json() : null;
    activePresetId = languageConfig?.active_preset_id || activePresetId;
  } catch {
    languageConfig = null;
  }
  renderPresetControls();
}

async function saveLanguageMappings() {
  const button = $("#saveLanguageButton");
  const status = $("#languageSaveStatus");
  const mappings = collectLanguageMappings();
  if (!mappings.length) {
    status.textContent = "저장할 매핑이 없습니다.";
    return;
  }
  button.disabled = true;
  status.textContent = "저장 중";
  try {
    const response = await fetch(LANGUAGE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset_id: activePresetId, mappings }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `저장 실패: HTTP ${response.status}`);
    }
    languageConfig = payload;
    languagePresets = payload.presets || languagePresets;
    activePresetId = payload.active_preset_id || activePresetId;
    renderLanguageSettings(dashboardData || {});
    status.textContent = `${number(payload.updated)}개 저장됨. ${activePresetName()}으로 다시 분석하면 반영됩니다.`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = apiUnavailable();
  }
}

function openQuickMapping(rawCode) {
  const code = String(rawCode || "").trim();
  if (!code) return;
  quickMappingCode = code;
  const configured = languageConfig?.event_labels?.[code] || {};
  const panel = $("#quickMapPanel");
  if (!panel) return;
  $("#quickMapCode").textContent = code;
  $("#quickMapPreset").textContent = activePresetName();
  $("#quickMapLabel").value = configured.label || code;
  $("#quickMapEventType").innerHTML = eventTypeOptions(configured.event_type || "event");
  $("#quickMapGroup").value = configured.group || "";
  $("#quickMapStatus").textContent = apiUnavailable()
    ? "중앙 API 연결 후 저장할 수 있습니다."
    : "현재 프리셋에 저장됩니다.";
  $("#quickMapSaveButton").disabled = apiUnavailable();
  panel.hidden = false;
  $("#quickMapLabel").focus();
}

function closeQuickMapping() {
  quickMappingCode = "";
  const panel = $("#quickMapPanel");
  if (panel) panel.hidden = true;
}

async function saveQuickMapping() {
  if (!quickMappingCode) return;
  const button = $("#quickMapSaveButton");
  const status = $("#quickMapStatus");
  const mapping = {
    raw: quickMappingCode,
    label: $("#quickMapLabel")?.value?.trim() || quickMappingCode,
    event_type: $("#quickMapEventType")?.value || "event",
    group: $("#quickMapGroup")?.value?.trim() || "",
  };
  button.disabled = true;
  status.textContent = "저장 중";
  try {
    const response = await fetch(LANGUAGE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset_id: activePresetId, mappings: [mapping] }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `저장 실패: HTTP ${response.status}`);
    }
    languageConfig = payload;
    languagePresets = payload.presets || languagePresets;
    activePresetId = payload.active_preset_id || activePresetId;
    status.textContent = "저장됨";
    closeQuickMapping();
    render(dashboardData || {});
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = apiUnavailable();
  }
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
  renderLanguageSettings(data);
}

function render(data) {
  if (!data) return;
  dashboardData = data;
  applyControls();
  renderSummary(data);
  renderBoard(data);
  renderDrawer(data);
  renderAiBriefing(data);
  renderBehaviorFlow(data);
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
  if (apiUnavailable()) {
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
  if (apiUnavailable()) {
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
    const response = await fetch(withQuery(RUN_URL, { preset: activePresetId }), { method: "POST", body: form });
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
    await loadLanguageConfig();
    const firstDangerIndex = visibleIssues(data).findIndex((issue) => issue.severity === "위험");
    selectedIssueIndex = firstDangerIndex >= 0 ? firstDangerIndex : 0;
    render(data);
  } catch (error) {
    const hint = apiUnavailable() ? " 중앙 API 서버가 아직 연결되지 않았습니다." : "";
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
$("#presetSelect")?.addEventListener("change", (event) => switchLanguagePreset(event.target.value));
$("#createPresetButton")?.addEventListener("click", createLanguagePreset);
$("#quickMapCancelButton")?.addEventListener("click", closeQuickMapping);
$("#quickMapSaveButton")?.addEventListener("click", saveQuickMapping);
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-map-code]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  openQuickMapping(button.dataset.mapCode);
});

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

if (apiUnavailable()) {
  setUploadStatus("중앙 API 서버가 아직 연결되지 않았습니다.", "danger");
}

loadDashboard();
