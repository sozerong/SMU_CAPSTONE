# ADR-0013. 그래프와 검증 규칙을 MCP 도구로 연다

- 상태: Accepted
- 날짜: 2026-11-03
- 프로젝트: VCC
- 코드: `app/mcp_server.py`

## 맥락

"후보 안에서만 추천한다"는 규칙은 지금 이 저장소의 파이프라인 안에서만 집행된다.
Claude 같은 외부 에이전트가 이 그래프로 추천을 하려면 같은 규칙에 묶여야 한다.
후보 쿼리와 판정 로직을 에이전트 쪽에 따로 옮기면 기준이 두 곳으로 갈라진다
(`grounding.py` 를 단일 출처로 둔 이유와 같다).

## 결정

MCP 서버(stdio)가 기존 함수를 그대로 도구로 노출한다. 새 판정 로직은 만들지 않는다.

| 도구 | 하는 일 | 재사용 |
|---|---|---|
| `graph_vocabulary()` | 조건에 쓸 수 있는 Tag/Season/Category | `ask_graph.graph_vocabulary` |
| `find_candidates(question, [조건])` | 후보 키워드. 조건을 주면 어휘 밖 값은 `unknown` 으로 돌려주고 버린다 | `ask_graph.retrieve_candidates` (`/ask` 와 같은 조건 완화) |
| `validate_recommendations(answer, candidates)` | 후보 안/밖 판정 | `grounding.validate_answer` |
| `keyword_neighbors(keyword)` | 태그·계절·종류·콤보(재료)·연관어 | Cypher 1회 (패턴 컴프리헨션) |
| `trending_keywords(limit)` | 최근 기록일, 언급 수 순 | |
| `ask(question)` | 서버 쪽 로컬 LLM 으로 `/ask` 와 같은 워크플로 | `ask_graph.build_app` |

설계 요점:

- **조건 추출을 외부 에이전트에 맡길 수 있다.** 에이전트가 `graph_vocabulary` 로 선택지를 받고
  `find_candidates` 에 조건을 넘긴다. 서버는 어휘 밖 값을 걸러 낸다. 질의 서비스(ADR-0011)가
  enum 으로 하는 일을 프로토콜 경계에서 한다.
- 에이전트는 스스로 생성하되 `validate_recommendations` 로 파이프라인과 같은 기준의 판정을 받는다.
- Neo4j 에는 읽기만 한다 (`execute_read`).

## 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| REST API 만 제공 | 에이전트마다 도구 스키마와 호출 코드를 따로 써야 한다. MCP 는 도구 목록과 인자 스키마를 프로토콜로 준다 |
| 에이전트에 Cypher 실행 도구를 연다 | 임의 쿼리는 후보 제한 규칙을 우회할 수 있고 쓰기 쿼리도 막아야 한다 |

## 측정

`python app/mcp_server.py --selftest` 가 서버를 자식 프로세스로 띄워 MCP stdio 프로토콜로 호출한다.
함수를 직접 부르면 도구 등록, 인자 스키마, 직렬화가 검증되지 않기 때문이다.

호출 왕복 지연 (클라이언트 측 `call_tool`, stdio + JSON + Neo4j 읽기 1회, 20회 순차, 3회 실행의 p50):

| 도구 | p50 범위 |
|---|---|
| `find_candidates` (메뉴 경로) | 5.17 ~ 6.95 ms |
| `find_candidates` (재료 경로) | 4.77 ~ 6.26 ms |
| `keyword_neighbors` | 4.30 ~ 4.69 ms |

조건: Ryzen 7 7800X3D, Windows 11, Python 3.12, mcp 2.3.0, Neo4j 5.15 docker, Keyword 337개.

## 알아둘 것

- mcp 2.3.0 에서 `mcp.server.fastmcp` 는 없어졌고 `mcp.server.mcpserver.MCPServer` 를 쓴다.
- 반환형을 `typing.Dict` 로 쓰면 결과가 `{"result": ...}` 로 한 번 더 감싸진다. 내장 `dict` 를 쓴다.
- MCP 클라이언트는 부모 환경 변수를 일부만 넘긴다. 기본값과 다른 접속 정보는 등록 설정의 `env` 에 적는다.
