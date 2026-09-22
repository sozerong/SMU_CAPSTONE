"""저장된 산출물을 Kafka 에 재주입 — 재현성 측정용 고정 입력 만들기

## 왜 크롤러를 다시 돌리지 않는가

`youtube_crawler.py` 를 다시 돌리면 매번 다른 영상이 들어온다. 그러면 "같은 입력으로
N회 돌렸을 때 같은 결과가 나오는가"를 물을 수 없다. 입력이 달라져 버리기 때문이다.
재현성 측정에 필요한 것은 **한 번 주입하고 고정된 토픽**이다.

## 무엇을 주입하는가 — 원본 입력이 남아 있지 않다

이 저장소에 보관된 것은 Spark 잡의 **산출물**(`data/*/keywords.jsonl`, video_id + 키워드)뿐이고,
잡의 **입력**이었던 영상 제목·설명문은 어디에도 저장돼 있지 않다. Kafka 토픽은 휘발됐고
`.gitignore` 가 `data/**/*.jsonl` 을 막고 있어 원본 텍스트를 되살릴 방법이 없다.

그래서 여기서는 **산출물에서 입력을 재구성한다** — video_id 는 그대로 쓰고,
title 은 그 영상에서 뽑혔던 키워드를 공백으로 이어 붙인다.

    {"video_id": "-2drEK9XBFk", "keywords": ["양념", "짜파게티", ...]}
      → {"video_id": "-2drEK9XBFk", "title": "양념 짜파게티 ...", "description": ""}

**이 코퍼스는 원본 영상 텍스트가 아니다.** 측정하려는 것이 "잡이 같은 입력에 같은 출력을
내는가"(잡의 결정성)이지 "원본 코퍼스에서 어떤 키워드가 나오는가"가 아니므로,
입력이 고정돼 있기만 하면 질문에는 답할 수 있다. 다만 키워드만 이어 붙인 문서는
같은 토큰이 문서당 대체로 1회만 나와 **TF 동점이 원문보다 훨씬 많다.** TF-IDF 상위 8개를
고를 때 동점 구간이 넓다는 뜻이고, 잡에 동점 처리 비결정성이 있다면 원문보다 잘 드러난다.
즉 이 입력은 결정성에 대해 **원문보다 불리한(까다로운) 조건**이다.

## 파티션 배치를 고정하는 이유

원래 크롤러는 `producer.send(TOPIC, value=...)` 로 키 없이 보낸다. 키가 없으면 레코드가
파티션에 라운드로빈으로 흩어져서 주입할 때마다 배치가 달라진다. 여기서는 키를 video_id 로
줘서 파티션 배치를 해시로 고정한다. 어차피 주입은 1회뿐이라 N회 재실행은 완전히 같은
바이트를 읽는다.

## 실행

    python kafka/replay_to_kafka.py --source data/8차/keywords.jsonl --recreate
    python kafka/replay_to_kafka.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC = "raw-youtube"
BOOTSTRAP = "localhost:9092"


def build_records(path: str) -> list[dict]:
    """keywords.jsonl → Kafka 에 넣을 raw-youtube 레코드.

    같은 파일이면 항상 같은 리스트가 나와야 한다. 정렬도 하지 않고 파일 순서를 그대로
    쓴다 — 파일이 고정이므로 그것으로 충분하고, 굳이 정렬하면 원래 수집 순서를 잃는다.
    """
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            records.append({
                "video_id": rec["video_id"],
                "title": " ".join(rec.get("keywords", [])),
                "description": "",
            })
    return records


def merge(base: list[dict], extra: list[dict]) -> list[dict]:
    """base 에 없는 video_id 만 extra 에서 붙인다.

    코퍼스 의존성을 통제 조건에서 재려면 **같은 문서를 그대로 둔 채 코퍼스만 키워야** 한다.
    video_id 가 겹치면 잡의 `dropDuplicates(["video_id"])` 가 둘 중 하나를 버리는데
    어느 쪽이 남는지는 보장되지 않는다. 그러면 "문서는 같고 코퍼스만 커졌다"가 깨진다.
    """
    seen = {r["video_id"] for r in base}
    return base + [r for r in extra if r["video_id"] not in seen]


def publish(records: list[dict], bootstrap: str, topic: str) -> None:
    from kafka import KafkaProducer

    producer = KafkaProducer(
        bootstrap_servers=bootstrap,
        key_serializer=lambda k: k.encode("utf-8"),
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
    )
    for r in records:
        producer.send(topic, key=r["video_id"], value=r)
    producer.flush()
    producer.close()


def selftest() -> None:
    """재구성 로직 자체 검증 — Kafka 없이 돈다."""
    import tempfile

    src = [
        {"video_id": "v1", "keywords": ["딸기 케이크", "크림"]},
        {"video_id": "v2", "keywords": []},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as f:
        for r in src:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write("\n")            # 빈 줄은 건너뛰어야 한다
        tmp = f.name

    try:
        recs = build_records(tmp)
        assert len(recs) == 2, recs
        assert recs[0] == {"video_id": "v1", "title": "딸기 케이크 크림",
                           "description": ""}, recs[0]
        assert recs[1]["title"] == "", recs[1]      # 키워드가 없어도 죽지 않는다
        # 같은 파일을 두 번 읽으면 같은 결과여야 주입이 고정 입력이 된다
        assert build_records(tmp) == recs

        # merge: 겹치는 id 는 base 쪽이 이기고, 새 id 만 뒤에 붙는다
        extra = [{"video_id": "v1", "title": "다른 텍스트", "description": ""},
                 {"video_id": "v3", "title": "새 영상", "description": ""}]
        m = merge(recs, extra)
        assert [r["video_id"] for r in m] == ["v1", "v2", "v3"], m
        assert m[0]["title"] == "딸기 케이크 크림", m[0]   # base 문서가 그대로 남아야 한다
    finally:
        os.unlink(tmp)

    print("replay_to_kafka 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="저장된 산출물을 Kafka 에 재주입")
    ap.add_argument("--source", default=os.path.join(BASE_DIR, "data", "8차", "keywords.jsonl"))
    ap.add_argument("--extra", default=None,
                    help="코퍼스만 키울 때 쓸 추가 파일 (겹치는 video_id 는 무시)")
    ap.add_argument("--bootstrap", default=BOOTSTRAP)
    ap.add_argument("--topic", default=TOPIC)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        raise SystemExit(0)

    recs = build_records(a.source)
    label = os.path.relpath(a.source, BASE_DIR)
    if a.extra:
        before = len(recs)
        recs = merge(recs, build_records(a.extra))
        label += f" + {os.path.relpath(a.extra, BASE_DIR)}(신규 {len(recs) - before}건)"
    publish(recs, a.bootstrap, a.topic)
    print(f"{len(recs)}건 주입 → {a.topic} @ {a.bootstrap}  (source: {label})")
