"""
pytest 부트스트랩 — 저장소 루트가 `import neo4j` 를 가리는 것을 막는다.

`python -m pytest` 는 현재 디렉터리를 `sys.path[0]` 에 넣는다. 저장소 루트에는
`neo4j/` 디렉터리가 있고 `__init__.py` 가 없어 네임스페이스 패키지로 잡히므로,
`import neo4j` 가 site-packages 의 공식 드라이버 대신 이 디렉터리를 집는다.
그러면 `neo4j.GraphDatabase` 가 없어 Neo4j 픽스처가 통째로 skip/error 가 된다.

테스트는 루트의 최상위 모듈을 import 하지 않는다(필요한 경로는 각 테스트가
직접 `sys.path` 에 넣는다). 그래서 루트만 빼면 된다. 디렉터리 이름을 바꾸지
않는 이유는 문서와 실행 명령이 전부 `python neo4j/cleanup.py` 를 쓰기 때문이다.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

# '' 는 현재 디렉터리를 뜻하므로 abspath 로 정규화해 함께 걸러낸다.
sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _ROOT]
