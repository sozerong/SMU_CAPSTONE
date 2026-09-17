"""
서울시 카페 메뉴 데이터 생성 — **합성 데이터다.**

⚠️  실제 공공데이터가 아니다.
서울열린데이터광장에는 일반음식점 인허가 정보(상호/주소)는 있지만
**메뉴명 단위 공개 데이터셋은 없다.** 조인 파이프라인을 만들고 성능을 재려면
메뉴명 테이블이 있어야 해서, 구조만 현실적으로 흉내낸 데이터를 만든다.

현실성을 맞춘 부분 (이게 있어야 정규화·스큐 실험이 의미가 있다):
  - 메뉴명에 사이즈/온도/옵션 표기가 섞인다: (ICE), HOT, L, 톨, 샷추가, 1+1 ...
  - 메뉴 분포가 롱테일이다. "아메리카노"·"라떼"는 거의 모든 카페에 있고
    "스웨디시 젤리" 같은 건 몇 곳에만 있다 → 조인 키가 심하게 쏠린다
  - 기본 메뉴명 일부는 저장소의 실제 키워드(filtered_keywords_with_count.jsonl)에서 가져와
    유튜브 키워드와 실제로 조인이 걸리게 한다

실행:
    python spark/make_cafe_menu.py --cafes 20000
    → data/seoul_cafe_menus.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from typing import Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_PATH = os.path.join(DATA_DIR, "seoul_cafe_menus.jsonl")

SEOUL_GU = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구",
]

# 거의 모든 카페에 있는 메뉴 — 조인 키 쏠림(skew)의 원인이 된다
HEAD_MENUS = [
    ("아메리카노", 0.97), ("카페 라떼", 0.92), ("카푸치노", 0.70), ("바닐라 라떼", 0.66),
    ("카페 모카", 0.62), ("에스프레소", 0.58), ("녹차", 0.45), ("아이스크림", 0.40),
]

# 사이즈 / 온도 / 옵션 표기 — 정규화가 걷어내야 할 것들
SIZE_TOKENS   = ["", "", "", "L", "R", "라지", "톨", "그란데", "(L)", "(R)", "벤티"]
TEMP_TOKENS   = ["", "", "", "ICE", "HOT", "(ICE)", "(HOT)", "아이스", "핫"]
OPTION_TOKENS = ["", "", "", "", "샷추가", "연하게", "휘핑추가", "1+1", "시럽추가", "디카페인"]

CAFE_BRANDS = [
    "카페", "커피", "로스터리", "브루잉", "빈스", "베이커리 카페", "디저트 카페",
    "커피하우스", "에스프레소바", "커피랩",
]


def load_tail_menus() -> List[Tuple[str, int]]:
    """저장소의 실제 키워드 → 롱테일 메뉴. count를 가중치로 쓴다."""
    rows: Dict[str, int] = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*", "filtered_keywords_with_count.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                kw, cnt = obj.get("keyword"), int(obj.get("count", 1))
                if kw:
                    rows[kw] = max(rows.get(kw, 0), cnt)
    if not rows:
        raise RuntimeError(f"키워드 파일을 찾지 못했다: {DATA_DIR}/*/filtered_keywords_with_count.jsonl")
    return sorted(rows.items(), key=lambda x: -x[1])


def decorate(base: str, rnd: random.Random) -> str:
    """기본 메뉴명에 사이즈/온도/옵션 표기를 섞는다."""
    parts = [rnd.choice(TEMP_TOKENS), base, rnd.choice(SIZE_TOKENS), rnd.choice(OPTION_TOKENS)]
    return " ".join(p for p in parts if p).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="서울시 카페 메뉴 합성 데이터 생성")
    ap.add_argument("--cafes", type=int, default=20000, help="카페 수")
    ap.add_argument("--seed",  type=int, default=42)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    tail = load_tail_menus()
    tail_names   = [k for k, _ in tail]
    tail_weights = [c for _, c in tail]

    os.makedirs(DATA_DIR, exist_ok=True)
    rows = 0
    head_hits = {name: 0 for name, _ in HEAD_MENUS}

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for i in range(args.cafes):
            gu   = rnd.choice(SEOUL_GU)
            name = f"{rnd.choice(CAFE_BRANDS)} {gu[:-1]}{i % 977}점"
            cafe_id = f"cafe-{i:06d}"

            menus: List[str] = []
            # 헤드 메뉴 — 대부분의 카페가 갖고 있다
            for base, p in HEAD_MENUS:
                if rnd.random() < p:
                    menus.append(base)
                    head_hits[base] += 1
            # 테일 메뉴 — count 가중 추출
            for base in rnd.choices(tail_names, weights=tail_weights, k=rnd.randint(2, 9)):
                menus.append(base)

            for base in menus:
                f.write(json.dumps({
                    "cafe_id":   cafe_id,
                    "cafe_name": name,
                    "gu":        gu,
                    "menu_name": decorate(base, rnd),
                    "price":     rnd.randrange(2500, 9500, 500),
                }, ensure_ascii=False) + "\n")
                rows += 1

    size = os.path.getsize(OUT_PATH)
    print(f"카페 {args.cafes:,}개 / 메뉴 {rows:,}행 → {OUT_PATH} ({size/1e6:.1f} MB)")
    print(f"테일 메뉴 후보: {len(tail_names)}종 (저장소 실제 키워드)")
    print("헤드 메뉴 보유 카페 수 (조인 키 쏠림의 원인):")
    for base, cnt in sorted(head_hits.items(), key=lambda x: -x[1]):
        print(f"  {base:<12} {cnt:,}개 카페")


if __name__ == "__main__":
    main()
