"""
메뉴명 / 키워드 정규화

카페 메뉴명에는 사이즈·온도·옵션 표기가 붙는다:
    "(ICE) 바닐라 라떼 L 샷추가"  →  "바닐라 라떼"

이걸 걷어내지 않으면 같은 메뉴가 수십 가지 문자열로 흩어져 조인이 걸리지 않는다.

명사 추출은 konlpy(Okt)를 쓴다. 다만 Okt는 JVM(JPype)을 요구해서 환경에 따라
설치가 안 되는 경우가 있고, Spark executor마다 형태소 분석기를 올리는 비용도 크다.
그래서 **정규식 기반 폴백**을 두고, 어느 쪽을 썼는지 로그로 남긴다.
(폴백은 조사/접미 제거 수준이라 Okt보다 거칠다 — 결과 해석 시 감안할 것)
"""

from __future__ import annotations

import re
from typing import List, Optional

# ── 걷어낼 표기 ───────────────────────────────────────────────
# 온도
_TEMP = r"(?:\(?\s*(?:ICE|HOT|아이스|핫|따뜻한|차가운)\s*\)?)"
# 사이즈
_SIZE = r"(?:\(?\s*(?:[LRSML]|라지|톨|그란데|벤티|숏|레귤러|스몰|미디움|라rge)\s*\)?)"
# 옵션 / 프로모션
_OPTION = r"(?:샷\s*추가|연하게|휘핑\s*추가|시럽\s*추가|디카페인|무설탕|저당|1\s*\+\s*1|리필)"
# 용량·수량 표기
_QTY = r"(?:\d+\s*(?:ml|ML|oz|OZ|g|G|개|잔|인분))"

_STRIP_PATTERNS = [
    re.compile(_TEMP),
    re.compile(_OPTION),
    re.compile(_QTY),
]

# 사이즈는 단독 토큰일 때만 지운다 — "L" 이 메뉴명 안의 글자를 물어뜯지 않도록
_SIZE_TOKEN = re.compile(r"(?:^|\s)" + _SIZE + r"(?=\s|$)")

_PUNCT   = re.compile(r"[\[\]\(\)\{\}<>/,+_*·•\-–—~!?\"'`]")
_SPACES  = re.compile(r"\s+")

# 폴백에서 떼어낼 흔한 조사/접미
_JOSA = re.compile(r"(?:은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|까지|부터)$")

_okt = None
_okt_tried = False


def _get_okt():
    """Okt 인스턴스를 지연 로딩. 실패하면 None (폴백 사용)."""
    global _okt, _okt_tried
    if _okt_tried:
        return _okt
    _okt_tried = True
    try:
        from konlpy.tag import Okt
        _okt = Okt()
    except Exception:
        _okt = None
    return _okt


def strip_markers(text: str) -> str:
    """사이즈/온도/옵션/용량 표기와 구두점을 제거한다."""
    if not text:
        return ""
    s = text
    for pat in _STRIP_PATTERNS:
        s = pat.sub(" ", s)
    s = _SIZE_TOKEN.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def _fallback_nouns(text: str) -> List[str]:
    """Okt 없이 쓰는 거친 명사 근사 — 조사 제거 + 1글자 토큰 버리기."""
    out = []
    for tok in text.split():
        tok = _JOSA.sub("", tok)
        # 한글/영문/숫자만 남긴다
        tok = re.sub(r"[^0-9A-Za-z가-힣]", "", tok)
        if len(tok) >= 2:
            out.append(tok)
    return out


def nouns(text: str) -> List[str]:
    """명사 추출. Okt가 있으면 Okt, 없으면 폴백."""
    okt = _get_okt()
    if okt is None:
        return _fallback_nouns(text)
    try:
        return [w for w, t in okt.pos(text, norm=True, stem=True) if t == "Noun" and len(w) >= 2]
    except Exception:
        return _fallback_nouns(text)


def normalize(text: Optional[str]) -> str:
    """
    메뉴명/키워드 → 정규화된 조인 키.
    표기 제거 → 명사 추출 → 공백 하나로 결합.
    """
    if not text:
        return ""
    stripped = strip_markers(text)
    ns = nouns(stripped)
    return " ".join(ns) if ns else stripped.lower()


def engine() -> str:
    return "okt" if _get_okt() is not None else "regex-fallback"


def demo() -> None:
    """자체 검증 — python spark/menu_normalize.py"""
    assert strip_markers("(ICE) 바닐라 라떼 L 샷추가") == "바닐라 라떼", strip_markers("(ICE) 바닐라 라떼 L 샷추가")
    assert strip_markers("HOT 아메리카노 그란데") == "아메리카노"
    assert strip_markers("딸기 케이크 1+1") == "딸기 케이크"
    assert strip_markers("콜드브루 500ml") == "콜드브루"

    # 사이즈 표기가 단어 안의 글자를 먹지 않아야 한다
    assert "라떼" in strip_markers("카페 라떼 R")
    assert strip_markers("소금 빵") == "소금 빵"

    # 정규화는 표기가 달라도 같은 키를 만들어야 한다 (조인이 걸리는 조건)
    variants = ["(ICE) 바닐라 라떼 L", "HOT 바닐라 라떼 톨 샷추가", "바닐라 라떼"]
    keys = {normalize(v) for v in variants}
    assert len(keys) == 1, f"표기별로 키가 갈라진다: {keys}"

    assert normalize("") == ""
    assert normalize(None) == ""

    print(f"menu_normalize 자체 검증 통과 (명사 추출: {engine()})")


if __name__ == "__main__":
    demo()
