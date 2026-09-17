# 벤치마크 기록 — 키워드 × 카페 메뉴 조인

모든 수치는 `spark/menu_join.py` 를 실제로 실행해서 얻은 값이다.
원본은 `spark/bench_results/*.json` 에 있다.

## 측정 환경

- 하드웨어: AMD Ryzen 7 7800X3D (8C/16T), RAM 63.1 GB
- OS: Windows 11 Home 10.0.26200
- Spark 3.3.4, `local[*]` (16 스레드), Java 8
- `spark.sql.shuffle.partitions = 16`
- **`spark.sql.adaptive.enabled = false`** — AQE를 켜두면 스큐를 자동으로 완화해서
  salting의 효과가 측정되지 않는다. 아래 수치는 "AQE 없이 손으로 고쳤을 때"라는 조건이다.
- 회차: 각 3회, p50 사용 (첫 회차의 JVM 워밍업 포함)

## 데이터

| | 출처 | 규모 |
|---|---|---|
| 유튜브 키워드 | 저장소 실제 데이터 `data/*/keywords.jsonl` | 6,494종 (중복 제거 후) |
| 카페 메뉴 | **합성** `spark/make_cafe_menu.py` | 216,441행 / 1,296,229행 |

⚠️ 카페 메뉴는 합성 데이터다. 서울시 공공데이터에 **메뉴명 단위 데이터셋이 없다.**
기본 메뉴명은 저장소의 실제 키워드(`filtered_keywords_with_count.jsonl`)에서 가져와
조인이 실제로 걸리게 했고, 사이즈/온도/옵션 표기와 롱테일 분포는 현실을 흉내낸 것이다.

명사 추출은 **regex 폴백**을 썼다 (konlpy/Okt 미설치 환경). Okt를 쓰면 정규화 품질이
올라가 매칭 키워드 수가 달라질 수 있다 — 아래 수치는 폴백 기준이다.

---

## 모드

| 모드 | 내용 |
|---|---|
| plain | `autoBroadcastJoinThreshold=-1` 로 SortMergeJoin 강제 |
| broadcast | `F.broadcast()` 힌트 |
| salt | broadcast + 빈도 상위 5개 키에만 salt(16버킷) 2단계 집계 |

`plain` 에서 자동 broadcast를 끈 이유: 키워드 테이블이 작아서 Spark가 알아서
broadcast 해버린다. 힌트의 효과를 보려면 자동 broadcast를 꺼야 비교가 성립한다.

---

## 216,441행 (카페 20,000개)

| 모드 | 조인 방식 | 총 소요 p50 | 파티션 max | median | max/median |
|---|---|---|---|---|---|
| plain | SortMergeJoin | **34.88s** | 18,842 | 3,934 | 4.79 |
| broadcast | BroadcastHashJoin | **34.45s** | 18,842 | 3,934 | 4.79 |
| salt | BroadcastHashJoin | **44.94s** | 13,111 | 4,579 | **2.86** |

## 1,296,229행 (카페 120,000개)

| 모드 | 조인 방식 | 총 소요 p50 | 회차 | 파티션 max | median | max/median |
|---|---|---|---|---|---|---|
| plain | SortMergeJoin | **44.44s** | 47.9 / 44.4 / 43.9 | 113,414 | 23,808 | 4.76 |
| broadcast | BroadcastHashJoin | **47.96s** | 49.8 / 48.0 / 44.2 | 113,414 | 23,808 | 4.76 |
| salt | BroadcastHashJoin | **62.03s** | 69.9 / 62.0 / 60.8 | 65,192 | 27,314 | **2.39** |

---

## 결론

**broadcast 힌트는 효과가 없다.** 216k에서 34.88 → 34.45s (차이 1%, 회차 편차 안),
1.3M에서는 44.44 → 47.96s로 **오히려 8% 느리다.** `explain()` 상 조인 방식은
SortMergeJoin → BroadcastHashJoin으로 확실히 바뀌었다. 바뀌었는데도 빨라지지 않았다는 뜻이다.
키워드 쪽이 6,494행뿐이라 어느 쪽으로 붙이든 셔플 비용이 지배적이지 않다.

**salting은 파티션 편차를 실제로 줄인다.** max/median 4.76 → 2.39 (1.3M 기준, 절반).
**그런데 총 소요는 40% 늘어난다** (44.44 → 62.03s). 2단계 집계에서
`collect_set` → `flatten` → `array_distinct` 를 거치는 비용이, 줄인 편차로 얻는 것보다 크다.

즉 **이 규모에서는 스큐가 병목이 아니다.** 최대 파티션이 11만 행이어도 16스레드가
그걸 처리하는 시간은 전체에서 지배적이지 않다. salting이 이기려면 편차가 훨씬
크거나(수십 배), 파티션 하나가 executor 메모리를 넘겨 spill이 나는 상황이어야 한다.

**남은 한계**
- 단일 노드 `local[*]` 다. 진짜 클러스터에서는 네트워크 셔플 비용이 커서
  broadcast의 이득이 여기보다 클 수 있다. 이 측정으로 "broadcast는 무용하다"고
  일반화할 수는 없다.
- 합성 데이터의 쏠림(4.8배)이 실제 서울시 카페 메뉴 분포와 같다는 보장이 없다.
- regex 폴백 정규화라 Okt 기준 수치와 다를 수 있다.
