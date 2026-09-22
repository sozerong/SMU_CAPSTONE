# ADR-0007. 저장소별 재적재 방식

- 상태: Accepted (Neo4j 는 미완 — 아래 참조)
- 프로젝트: VCC

## 맥락

주간 배치라 같은 파이프라인이 반복 실행된다. 재실행했을 때 결과가 누적되거나
중복되면 "이번 주 트렌드"라는 산출물의 의미가 사라진다.

## 결정

저장소 성격이 달라 같은 방식을 쓰지 않았다.

### Elasticsearch — 인덱스 전체 삭제 후 재생성

```python
if es.indices.exists(index=INDEX_NAME):
    es.indices.delete(index=INDEX_NAME)
es.indices.create(index=INDEX_NAME, body=MAPPINGS)
...
es.index(index=INDEX_NAME, id=f"q_{idx+1}", document=new_doc)
```

서빙용 조회 전용 인덱스라 과거 회차를 남길 이유가 없다. 매핑 변경도 같이 반영된다.
문서 id를 `q_N` 으로 고정해 회차 내에서도 중복이 생기지 않는다.

**한계**: 삭제와 재생성 사이에 실패하면 인덱스가 비어 있는 구간이 생긴다.
서빙 중이라면 alias 교체(신규 인덱스 생성 → alias 전환 → 구 인덱스 삭제)로 가야 한다.
현재는 서빙 트래픽이 없어 그대로 뒀다.

### Neo4j — `MERGE` (upsert)

```cypher
MERGE (k:Keyword {name: $name})
MERGE (d:Date {value: $today})
MERGE (k)-[:RECORDED_ON]->(d)
```

그래프는 **시계열 축(`Date`, `RECORDED_ON`)을 갖는 누적 저장소**로 설계했다.
"지난주에는 있었는데 이번 주에 빠진 키워드"를 물으려면 과거가 남아 있어야 한다.
그래서 전체 삭제가 아니라 upsert 다.

**한계 — 여기가 미완이다.** 삭제가 전혀 없어서, 잘못 들어간 노드나 더 이상 유효하지 않은
관계를 지울 방법이 없다. 같은 키워드가 매주 `RECORDED_ON` 만 늘려가는 것은 의도대로지만,
스키마를 바꾸거나 오적재를 고칠 때 손댈 수단이 없다.
회차 태깅 후 정리하는 경로가 필요하다.

## 결과

| 저장소 | 방식 | 재실행 안전 | 과거 회차 |
|---|---|---|---|
| Elasticsearch | 전체 삭제 후 재생성 + 고정 id | 예 | 남지 않음 (의도) |
| Neo4j | `MERGE` only | 중복은 안 생김 | 남음 (의도) — 단 정리 수단 없음 |

"범위 삭제 후 재삽입"은 둘 중 어느 쪽도 아니다. 초기 문서에 그렇게 적혀 있었으나
코드와 맞지 않아 이 ADR 로 정정한다.
