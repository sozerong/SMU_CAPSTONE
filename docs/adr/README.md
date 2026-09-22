# 설계 결정 기록 (ADR)

파이프라인 설계에서 내린 결정을 **결정 시점의 근거와 기각한 대안**과 함께 남긴 기록입니다.
기존 문서를 고치는 대신 새 ADR로 대체하며, 대체된 문서는 superseded 상태로 남깁니다.

| 번호 | 결정 | 상태 |
|---|---|---|
| [0006](0006-llm-hallucination-candidate-restriction.md) | LLM 환각 대응 — 후보 선행 제한 | Accepted (부분 성공 — 재료 경로 50% 미차단) |
| [0007](0007-idempotent-reload.md) | 저장소별 재적재 방식 | Accepted (Neo4j 정리 수단 미구현) |
| [0008](0008-skew-not-mitigated.md) | 스큐 대응 미적용 | Accepted |

번호 0001~0005, 0009 는 Illo-on 저장소의 `docs/adr` 에 있습니다.

기록하지 않은 것: 사후에 정한 SLO, 비용 목표.
프로젝트 진행 당시 제약으로 두지 않았던 항목은 지어내지 않고 비워 두었습니다.
