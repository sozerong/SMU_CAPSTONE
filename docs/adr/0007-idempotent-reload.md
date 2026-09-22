# ADR-0007. 저장소별 재적재 방식

- 상태: Accepted (정리 수단 추가 완료)
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

**정리 수단 추가** — `neo4j/cleanup.py`

전체 삭제로 가면 누적 저장소라는 설계가 무너진다. 그래서 **회차 단위로만** 지운다.

```bash
python neo4j/cleanup.py stats                    # 현황 + 고아 노드
python neo4j/cleanup.py delete-round 2025-05-03  # 그 회차만
python neo4j/cleanup.py prune                    # 고아만
python neo4j/cleanup.py reset --yes              # 전체 (확인 필요)
```

`delete-round` 는 **그 회차에만 있던 Keyword 만** 지운다. 다른 회차에도 기록된 키워드는
`RECORDED_ON` 만 끊고 노드는 남긴다. 이어서 고아가 된 Tag·Combo·Ingredient 등을 정리한다.
`Combo` 를 `Ingredient` 보다 먼저 치워야 딸려 나오므로 삭제 순서가 고정돼 있다.

고아 정리를 `delete_round` 안에 넣었다. 호출자가 기억해야 하는 일로 두면 언젠가 빠뜨린다
(실제로 자체 검증이 그 상태를 잡아냈다).

실측: 712개 노드(Keyword 337 + 부속) 적재 → `delete-round` 1회 → **전부 정리, 잔여 0**.

### 같이 고친 것 — 지울 수 없는 노드가 쌓이던 원인

`related[]` 로 생기는 스텁 Keyword 에 `RECORDED_ON` 이 없었다.
337개 중 **251개(74%)** 가 회차 태그 없이 떠 있어 회차 단위 정리로 지울 수 없었다.
스텁 생성 시 `count = 0` 과 `RECORDED_ON` 을 함께 달도록 고쳤다.

## 결과

| 저장소 | 방식 | 재실행 안전 | 과거 회차 |
|---|---|---|---|
| Elasticsearch | 전체 삭제 후 재생성 + 고정 id | 예 | 남지 않음 (의도) |
| Neo4j | `MERGE` + `cleanup.py` 회차 삭제 | 예 | 남음 (의도) |

"범위 삭제 후 재삽입"은 둘 중 어느 쪽도 아니다. 초기 문서에 그렇게 적혀 있었으나
코드와 맞지 않아 이 ADR 로 정정한다.
