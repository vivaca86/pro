from __future__ import annotations

from typing import Any

import pandas as pd


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "유력"
    if score >= 0.5:
        return "가능성 있음"
    return "데이터 부족"


def _issue(
    title: str,
    issue_type: str,
    severity: str,
    cause: str,
    evidence: list[str],
    impact_score: float,
    evidence_score: float,
    data_sufficiency: float,
    recommendation: str,
) -> dict[str, Any]:
    confidence = round((impact_score * 0.4) + (evidence_score * 0.4) + (data_sufficiency * 0.2), 3)
    return {
        "title": title,
        "type": issue_type,
        "severity": severity,
        "cause_candidate": cause,
        "evidence": evidence,
        "impact_score": round(impact_score, 3),
        "evidence_score": round(evidence_score, 3),
        "data_sufficiency": round(data_sufficiency, 3),
        "confidence": confidence,
        "confidence_label": _confidence_label(confidence),
        "recommendation": recommendation,
    }


def diagnose_content(content: pd.DataFrame) -> list[dict[str, Any]]:
    if content.empty:
        return []
    issues: list[dict[str, Any]] = []
    participant_median = float(content["participant_rate"].median()) if "participant_rate" in content else 0
    for row in content.to_dict("records"):
        group = row["group"]
        participant_rate = float(row.get("participant_rate", 0))
        failure_rate = float(row.get("failure_rate", 0))
        retry_rate = float(row.get("retry_after_failure_rate", 0))
        avg_wait = float(row.get("avg_wait_sec", 0))
        revenue_after_content = float(row.get("revenue_after_content", 0))

        if avg_wait >= 30:
            issues.append(
                _issue(
                    title=f"{group} 대기/매칭 지연 의심",
                    issue_type="content_friction",
                    severity="위험" if avg_wait >= 45 else "주의",
                    cause="대기시간 증가 또는 매칭 실패가 참여/재참여를 낮출 가능성",
                    evidence=[
                        f"평균 대기시간 {avg_wait:.1f}초",
                        f"실패 후 재도전율 {retry_rate * 100:.1f}%",
                        f"참여율 {participant_rate * 100:.1f}%",
                    ],
                    impact_score=min(1, avg_wait / 60),
                    evidence_score=0.75 if retry_rate < 0.35 else 0.55,
                    data_sufficiency=0.75,
                    recommendation="매칭 허용 범위, 봇 매칭, 대기 보상, 첫 매칭 UX를 우선 확인하세요.",
                )
            )

        if failure_rate >= 0.4:
            issues.append(
                _issue(
                    title=f"{group} 실패율 높음",
                    issue_type="content_failure",
                    severity="위험" if failure_rate >= 0.6 else "주의",
                    cause="초반 실패 경험이 이탈 또는 재참여 저하로 이어질 가능성",
                    evidence=[
                        f"실패율 {failure_rate * 100:.1f}%",
                        f"실패 후 재도전율 {retry_rate * 100:.1f}%",
                        f"참여 유저 {int(row.get('participant_users', 0))}명",
                    ],
                    impact_score=failure_rate,
                    evidence_score=0.7 if retry_rate < 0.35 else 0.5,
                    data_sufficiency=0.8,
                    recommendation="첫 실패 구간, 난이도, 실패 후 보상/가이드, 재도전 비용을 확인하세요.",
                )
            )

        if participant_median and participant_rate < participant_median * 0.55 and revenue_after_content > 0:
            issues.append(
                _issue(
                    title=f"{group} 이용률 낮지만 매출 연결 있음",
                    issue_type="content_usage_revenue_gap",
                    severity="주의",
                    cause="적은 유저가 이용하지만 상품 구매와 연결되어 있어 병목 해소 가치가 있을 가능성",
                    evidence=[
                        f"참여율 {participant_rate * 100:.1f}%",
                        f"콘텐츠 이후 연결 매출 {revenue_after_content:,.0f}",
                        f"전체 콘텐츠 참여율 중앙값 {participant_median * 100:.1f}%",
                    ],
                    impact_score=0.6,
                    evidence_score=0.6,
                    data_sufficiency=0.65,
                    recommendation="해금/노출/진입 흐름과 해당 콘텐츠 관련 상품 퍼널을 함께 확인하세요.",
                )
            )
    return sorted(issues, key=lambda item: item["confidence"], reverse=True)


def diagnose_data_quality(data_quality: dict[str, Any], language_suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_rows = int(data_quality.get("normalized_rows", 0) or 0)
    inferred_rows = int(data_quality.get("inferred_language_rows", 0) or 0)
    needs_confirmation = [item for item in language_suggestions if item.get("needs_confirmation")]
    if normalized_rows <= 0 or not needs_confirmation:
        return []

    inferred_ratio = inferred_rows / normalized_rows if normalized_rows else 0
    if inferred_ratio < 0.5:
        return []

    examples = ", ".join(str(item.get("raw")) for item in needs_confirmation[:5])
    return [
        _issue(
            title="로그 코드 매핑 필요",
            issue_type="log_mapping_needed",
            severity="주의",
            cause="업로드 데이터의 이벤트 코드 의미가 사전에 없어 행동/콘텐츠/결제 해석이 제한됨",
            evidence=[
                f"미확인 로그 {len(needs_confirmation)}개",
                f"추론 처리 행 {inferred_rows:,}건",
                f"예시 코드: {examples}",
            ],
            impact_score=min(1, inferred_ratio),
            evidence_score=0.8,
            data_sufficiency=0.7,
            recommendation="logtype/e_code별 의미를 로그 언어 설정에 추가하면 콘텐츠, 실패, 보상, 결제 흐름까지 분리됩니다.",
        )
    ]


def diagnose_revenue(
    summary: dict[str, Any],
    products: pd.DataFrame,
    purchase_contexts: dict[str, Any],
    concentration: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    revenue = float(summary.get("revenue", 0))
    if revenue <= 0:
        issues.append(
            _issue(
                title="결제 데이터 없음",
                issue_type="revenue_missing",
                severity="주의",
                cause="오늘 데이터에서 결제 이벤트 또는 금액 컬럼이 확인되지 않음",
                evidence=["결제 유저 0명", "매출 0"],
                impact_score=0.8,
                evidence_score=0.8,
                data_sufficiency=0.5,
                recommendation="결제 로그, 상품 ID, 금액 컬럼 매핑이 맞는지 먼저 확인하세요.",
            )
        )
        return issues

    if concentration.get("top_1_user_share", 0) >= 0.35:
        issues.append(
            _issue(
                title="고래 유저 매출 의존도 높음",
                issue_type="whale_concentration",
                severity="주의",
                cause="상위 유저 소수의 구매 변화가 전체 매출 변동처럼 보일 가능성",
                evidence=[
                    f"최고 매출 유저 비중 {concentration['top_1_user_share'] * 100:.1f}%",
                    f"상위 5% 매출 비중 {concentration['top_5pct_share'] * 100:.1f}%",
                ],
                impact_score=float(concentration["top_1_user_share"]),
                evidence_score=0.7,
                data_sufficiency=0.75,
                recommendation="매출 변동 분석 시 상위 결제 유저 포함/제외 두 버전을 함께 보세요.",
            )
        )

    if not products.empty:
        top = products.iloc[0]
        top_share = float(top["revenue"]) / revenue if revenue else 0
        if top_share >= 0.5:
            issues.append(
                _issue(
                    title="특정 상품 매출 집중",
                    issue_type="product_concentration",
                    severity="주의",
                    cause="특정 상품 성과 변화가 전체 매출을 크게 흔들 가능성",
                    evidence=[
                        f"최상위 상품 {top['product']} 매출 비중 {top_share * 100:.1f}%",
                        f"구매자 {int(top['buyers'])}명",
                        f"상품 매출 {float(top['revenue']):,.0f}",
                    ],
                    impact_score=top_share,
                    evidence_score=0.75,
                    data_sufficiency=0.75,
                    recommendation="해당 상품의 노출/클릭/구매 직전 행동과 연결 콘텐츠를 우선 확인하세요.",
                )
            )

    top_context_groups = purchase_contexts.get("top_preceding_groups", [])
    if top_context_groups:
        group, count = top_context_groups[0]
        issues.append(
            _issue(
                title=f"{group} 이후 구매 흐름 강함",
                issue_type="content_to_purchase_context",
                severity="정보",
                cause="구매 직전 행동에서 특정 콘텐츠 맥락이 반복적으로 나타남",
                evidence=[
                    f"구매 직전 {group} 관련 로그 {count}회",
                    f"상위 구매 전 행동: {purchase_contexts.get('top_preceding_events', [])[:3]}",
                ],
                impact_score=0.55,
                evidence_score=0.65,
                data_sufficiency=0.65,
                recommendation="해당 콘텐츠 참여 변화가 관련 상품 구매 변화와 같이 움직이는지 누적 데이터에서 추적하세요.",
            )
        )
    return sorted(issues, key=lambda item: item["confidence"], reverse=True)


def build_alerts(diagnosis: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates = diagnosis.get("content", []) + diagnosis.get("revenue", [])
    severity_rank = {"위험": 0, "주의": 1, "정보": 2}
    candidates = sorted(
        candidates,
        key=lambda item: (severity_rank.get(item["severity"], 3), -item["confidence"]),
    )
    return [
        {
            "severity": item["severity"],
            "title": item["title"],
            "cause_candidate": item["cause_candidate"],
            "top_evidence": item["evidence"][:3],
            "confidence_label": item["confidence_label"],
        }
        for item in candidates[:5]
    ]
