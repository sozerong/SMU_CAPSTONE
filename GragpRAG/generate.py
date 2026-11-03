"""
생성 단계 — 프롬프트, 모델 선택, 구조화 출력의 단일 출처

세 곳이 같은 생성 함수를 쓴다.
  - `graphrag_aq.py`  : 주간 배치 (고정 질문 6개)
  - `ask_graph.py`    : 질의 서비스 (임의 질문, 재생성 루프)
  - `eval_models.py`  : 모델·디코딩 방식 비교

비교 스크립트가 서비스와 다른 프롬프트를 쓰면 비교 결과를 서비스에 옮길 수 없다.
그래서 프롬프트와 호출 방식을 이 파일 하나에 둔다.

## 구조화 출력

`structured=True` 면 Pydantic 스키마를 모델에 넘긴다 (OpenAI json_schema,
Ollama `format` 문법 제약). 자유 텍스트를 받아 `json.loads` 하던 방식은
모델이 펜스를 붙이거나 앞뒤에 설명을 달면 파싱이 깨진다.

프롬프트는 두 모드가 **같다.** 바뀌는 변수는 디코딩 제약 하나뿐이어야
`eval_models.py` 의 비교가 성립한다.

## 모델 선택

`LLM_PROVIDER` = `ollama`(기본) | `openai`. 기본을 로컬로 둔 이유는
공공·금융 같은 폐쇄망에서 외부 API 를 못 쓰는 경우를 전제로 하기 때문이다.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

# qwen2.5:7b 를 기본으로 둔 근거: 같은 9개 입력 × 10회에서 후보 밖 생성률이 gpt-4 와
# 구별되지 않았다 (lenient 0.7% vs 0.3%, 구간 겹침). exaone3.5:7.8b 는 3.3% 로 구별되게 높았다.
# 원본: eval/model_compare_*.json (`python GragpRAG/eval_models.py table`)
DEFAULT_MODELS = {"ollama": "qwen2.5:7b", "openai": "gpt-4"}
# 구조화 출력에서 JSON 을 닫은 뒤에도 디코딩이 서버 상한(40,960 토큰)까지 이어져
# 한 호출이 491초 걸린 사례가 있다. 정상 답변은 수백 토큰이라 상한과 타임아웃을 둔다.
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "768"))
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))


class Item(BaseModel):
    name: str = Field(description="추천 이름. 후보 키워드를 쓰거나 조합한다")
    description: str


class Recommendations(BaseModel):
    # 프롬프트가 3~5개를 요구한다. 1로 두면 2개짜리 답이 스키마를 통과했다 (qwen 2/90).
    items: List[Item] = Field(min_length=3, max_length=5)


def build_system_prompt(question: str, keywords: List[str],
                        feedback: Optional[str] = None) -> str:
    """배치 시절 프롬프트를 그대로 옮겼다. feedback 은 재생성 루프에서만 붙는다."""
    bullets = "\n".join(f"- {kw}" for kw in keywords)
    if "재료" in question:
        head = (f"당신은 카페 재료 기획 전문가입니다.\n"
                f"다음은 사용 가능한 재료 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 재료 3~5개를 추천하세요.\n"
                f"❗ 주어진 키워드 외의 재료를 생성하면 안 됩니다.\n\n")
    else:
        head = (f"당신은 디저트 마케팅 전문가입니다.\n"
                f"다음은 사용 가능한 디저트 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 메뉴를 3~5개 추천하세요.\n"
                f"❗ 제공된 키워드 외 메뉴를 생성하지 마세요.\n\n")
    prompt = (head + f"{bullets}\n\n"
              f"출력 형식: JSON 배열 [{{\"name\": \"이름\", \"description\": \"설명\"}} ...]")
    if feedback:
        prompt += (f"\n\n이전 답변이 검증에서 거부됐습니다 ({feedback}). "
                   f"위 키워드 목록 안의 단어만 사용해 다시 답하세요.")
    return prompt


def make_llm(provider: Optional[str] = None, model: Optional[str] = None,
             temperature: float = 0.3):
    provider = provider or os.getenv("LLM_PROVIDER", "ollama")
    model = model or os.getenv("LLM_MODEL") or DEFAULT_MODELS[provider]
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=temperature,
                          max_tokens=MAX_TOKENS, timeout=TIMEOUT_S)
    from langchain_ollama import ChatOllama
    # 한 호스트에 Ollama 가 둘이면 localhost 가 어느 쪽으로 가는지 보장이 없다.
    # JSON 스키마 format 은 Ollama 0.5 이상만 받는다 (0.4.1 은 HTTP 400). OLLAMA_URL 로 고정할 것.
    return ChatOllama(model=model, temperature=temperature, num_predict=MAX_TOKENS,
                      client_kwargs={"timeout": TIMEOUT_S},
                      base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"))


def generate(llm, question: str, keywords: List[str],
             feedback: Optional[str] = None, structured: bool = True) -> Any:
    """
    structured=True  → 성공 시 list[dict], 스키마 실패 시 원문 문자열
    structured=False → 원문 문자열

    어느 쪽이든 `grounding.validate_answer` 에 그대로 넣으면 된다.
    스키마 실패를 예외로 던지지 않는 이유: 실패 사유 집계가 validate_answer 한 곳에서 나와야 한다.
    """
    msgs = [SystemMessage(content=build_system_prompt(question, keywords, feedback)),
            HumanMessage(content=question)]
    if not structured:
        return llm.invoke(msgs).content
    out = llm.with_structured_output(Recommendations, method="json_schema",
                                     include_raw=True).invoke(msgs)
    if out.get("parsed") is not None:
        return [i.model_dump() for i in out["parsed"].items]
    raw = out.get("raw")
    return getattr(raw, "content", "") or ""


def demo() -> None:
    """자체 검증 (모델 호출 없음) — `python GragpRAG/generate.py`"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    p = build_system_prompt("최근 유행하는 재료", ["딸기", "크림"])
    assert "재료 기획" in p and "- 딸기\n- 크림" in p and "거부" not in p, p
    assert "디저트 마케팅" in build_system_prompt("비주얼이 예쁜 메뉴", ["딸기"])
    assert "거부됐습니다 (ungrounded:망고)" in build_system_prompt("x", ["딸기"], "ungrounded:망고")

    fake = FakeListChatModel(responses=['[{"name":"딸기 크림","description":"x"}]'])
    raw = generate(fake, "카페 신메뉴 추천", ["딸기", "크림"], structured=False)
    assert raw.startswith("["), raw

    s = Recommendations.model_json_schema()
    assert s["properties"]["items"]["maxItems"] == 5
    assert s["properties"]["items"]["minItems"] == 3
    llm = make_llm("ollama", "qwen2.5:7b")
    assert llm.num_predict == MAX_TOKENS
    print("generate 자체 검증 통과")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    demo()
