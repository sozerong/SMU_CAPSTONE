"""
후보 안/밖 판정 — 생성 시점과 측정 시점이 같은 기준을 쓰게 하는 단일 출처

이 판정이 두 곳에서 필요하다.
  - `graphrag_aq.py`   : 생성 직후 검증해서 후보 밖이면 격리 (런타임)
  - `eval_grounding.py`: 저장된 답변으로 후보 밖 생성률 집계 (사후 측정)

둘이 각자 판정 로직을 들고 있으면 언제든 갈라진다.
(이 저장소에는 이미 그런 사고가 있었다 — 이벤트 스키마가 ORM 과 생성기에서 갈라져
분석 단계 3개가 조용히 실패했다.) 그래서 한 파일에 둔다.

## 두 가지 기준

**strict** — 정규화 후 완전 일치해야 후보 안.
**lenient** — 후보 키워드가 생성 이름에 부분 문자열로 들어 있으면 후보 안.

이 프로젝트는 **후보를 조합해 새 이름을 만드는 것이 의도된 동작**이다
("딸기"+"크림"+"케이크" → "딸기 크림 케이크"). 그래서 strict 를 쓰면 정상 결과가
전부 위반으로 잡힌다 (실측 70.5%). 런타임 격리는 **lenient** 를 쓴다.

측정할 때는 둘 다 낸다 — 기준에 따라 6배 차이가 나므로 하나만 쓰면 기준 논쟁이 붙는다.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

_WS = re.compile(r"\s+")


def normalize(s: Any) -> str:
    """공백 제거 + 소문자화. '딸기 크림' 과 '딸기크림' 을 같게 본다."""
    return _WS.sub("", str(s)).lower()


def is_grounded(name: str, candidates: List[str], strict: bool = False) -> bool:
    """생성된 이름이 후보 안인가."""
    n = normalize(name)
    cands = [normalize(c) for c in candidates if c]
    if strict:
        return n in cands
    return any(c and c in n for c in cands)


def strip_code_fence(s: str) -> str:
    """
    ```json ... ``` 펜스를 벗긴다.

    gpt-4 는 JSON 을 그대로 돌려주지만 gpt-4o-mini 는 마크다운 펜스로 감싼다.
    벗기지 않으면 `json.loads` 가 전부 실패해 **모델을 바꾸는 순간 전량 dead letter** 가 된다.
    파싱 실패로 집계되니 원인도 "모델이 형식을 안 지킨다"로 오독된다.

    펜스가 없으면 원문을 그대로 돌려주므로 기존 gpt-4 산출물의 판정은 바뀌지 않는다.
    """
    t = s.strip()
    if not t.startswith("```"):
        return s
    t = t[3:]
    # ```json / ```JSON 처럼 언어 태그가 붙는다. 첫 줄바꿈까지가 태그다.
    nl = t.find("\n")
    if nl != -1 and t[:nl].strip().isalpha():
        t = t[nl + 1:]
    end = t.rfind("```")
    return t[:end] if end != -1 else t


def validate_answer(content: Any, candidates: List[str]) -> Tuple[bool, Any]:
    """
    LLM 응답 검증. 통과하면 `(True, 파싱된 리스트)`, 아니면 `(False, 사유)`.

    파싱 실패만 잡으면 부족하다. 스키마는 맞는데 후보 밖 값을 넣어오는 경우가
    실제로 있었다 (재료 경로 50%, 메뉴 경로 2.2%).
    후보 밖은 파이프라인의 전제를 깨뜨리므로 파싱 실패와 같이 격리한다.

    사유 문자열:
      json_parse_error / not_a_list / missing_name_field / ungrounded:<이름들>
    """
    if isinstance(content, str):
        try:
            parsed = json.loads(strip_code_fence(content))
        except (json.JSONDecodeError, ValueError):
            return False, "json_parse_error"
    else:
        parsed = content

    if not isinstance(parsed, list) or not parsed:
        return False, "not_a_list"

    names: List[str] = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("name"):
            return False, "missing_name_field"
        names.append(str(item["name"]))

    ungrounded = [n for n in names if not is_grounded(n, candidates)]
    if ungrounded:
        return False, "ungrounded:" + ",".join(ungrounded[:3])

    return True, parsed


def demo() -> None:
    """자체 검증 — `python GragpRAG/grounding.py`"""
    cands = ["딸기", "크림", "케이크", "초코"]

    # 후보 조합은 통과해야 한다 (이 프로젝트의 의도된 동작)
    ok, out = validate_answer('[{"name":"딸기 크림 케이크","description":"x"}]', cands)
    assert ok and out[0]["name"] == "딸기 크림 케이크", out

    # 후보 밖은 격리
    ok, why = validate_answer('[{"name":"아보카도","description":"x"}]', cands)
    assert not ok and why.startswith("ungrounded"), why

    # 여러 개 중 하나라도 후보 밖이면 격리
    ok, why = validate_answer(
        '[{"name":"딸기 케이크","description":"x"},{"name":"쿠알라룸푸르","description":"y"}]',
        cands)
    assert not ok and "쿠알라룸푸르" in why, why

    ok, why = validate_answer("이건 JSON 이 아니다", cands)
    assert not ok and why == "json_parse_error", why

    ok, why = validate_answer('{"name":"딸기"}', cands)
    assert not ok and why == "not_a_list", why

    ok, why = validate_answer('[{"description":"이름 없음"}]', cands)
    assert not ok and why == "missing_name_field", why

    ok, why = validate_answer("[]", cands)
    assert not ok and why == "not_a_list", why

    # 이미 파싱된 리스트를 그대로 넘겨도 동작해야 한다 (측정 스크립트가 그렇게 쓴다)
    ok, _ = validate_answer([{"name": "초코 케이크"}], cands)
    assert ok

    # 공백·대소문자 무시
    assert is_grounded("딸기크림", cands)
    assert is_grounded("CREAM latte", ["cream"])

    # strict 와 lenient 의 차이가 실제로 갈려야 한다
    assert is_grounded("딸기 크림 케이크", cands, strict=False)
    assert not is_grounded("딸기 크림 케이크", cands, strict=True)
    assert is_grounded("딸기", cands, strict=True)

    # 후보가 비면 아무것도 후보 안이 아니다
    assert not is_grounded("딸기", [])

    # 마크다운 펜스 — gpt-4o-mini 가 이 형식으로 답한다.
    # 벗기지 못하면 모델을 바꾸는 순간 전량 json_parse_error 가 된다.
    fenced = '```json\n[{"name": "딸기 케이크", "description": "x"}]\n```'
    ok, parsed = validate_answer(fenced, cands)
    assert ok and parsed[0]["name"] == "딸기 케이크", (ok, parsed)

    assert strip_code_fence('```\n[1]\n```').strip() == "[1]"        # 언어 태그 없음
    assert strip_code_fence('```JSON\n[1]\n```').strip() == "[1]"    # 대문자 태그
    assert strip_code_fence('[1]') == '[1]'                          # 펜스 없으면 그대로
    # 닫는 펜스가 없어도 앞부분은 살린다 (응답이 잘린 경우)
    assert strip_code_fence('```json\n[1]').strip() == "[1]"

    # 펜스를 벗겨도 후보 밖 판정은 그대로 걸려야 한다
    ok, reason = validate_answer('```json\n[{"name": "망고 빙수"}]\n```', cands)
    assert not ok and reason.startswith("ungrounded:"), (ok, reason)

    print("grounding 자체 검증 통과")


if __name__ == "__main__":
    demo()
