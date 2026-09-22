# ADR-0010. 재현성 주장의 범위 — Spark 단계는 결정적, 흔들림은 코퍼스와 LLM 에서 온다

- 상태: Accepted
- 날짜: 2026-09 측정
- 프로젝트: VCC

## 맥락

포트폴리오에 "같은 파이프라인을 10회 돌리면 결과가 일치한다"는 주장이 있었다.
이 주장은 두 가지 이유로 성립하지 않는다고 문서화돼 있었다.

1. LLM 3단계가 temperature > 0 이고 캐시도 시드도 없다.
2. 저장된 회차 산출물끼리 비교하면 완전 일치가 0~10.7% 에 그친다.

문제는 **2번이 두 가지를 구분하지 못한다는 것**이다. 회차마다 수집된 영상이 달라서
비교 대상의 입력 자체가 다르다. 그러면 불일치의 원인이 둘 중 무엇인지 알 수 없다.

- **(a) 잡 자체의 비결정성** — 파티션 순서, 해시, 병렬성
- **(b) 배치 TF-IDF 의 코퍼스 의존성** — IDF 를 배치 전체 문서로 계산한다

(a) 라면 코드 문제라 고칠 수 있다. (b) 라면 알고리즘의 성질이라 설계를 바꿔야 한다.
대응이 완전히 다르므로 구분해야 한다. `verify_reproducibility.py` 에 `run` 서브커맨드가
그 목적으로 있었으나 Kafka 가 없어 실행된 적이 없었다.

## 측정

### 입력을 고정하는 방법

이 저장소에는 잡의 **입력**(영상 제목·설명문)이 남아 있지 않다. Kafka 토픽은 휘발됐고
보관된 것은 **산출물**(`data/*/keywords.jsonl` — video_id + 키워드)뿐이다.
크롤러를 다시 돌리면 매번 다른 영상이 들어와 "같은 입력"이 성립하지 않는다.

그래서 산출물에서 입력을 재구성했다 (`kafka/replay_to_kafka.py`).
video_id 는 그대로 쓰고 title 은 그 영상에서 뽑혔던 키워드를 공백으로 이어 붙인다.
**원본 영상 텍스트가 아니다.** 측정 대상이 "잡이 같은 입력에 같은 출력을 내는가"이므로
입력이 고정돼 있으면 질문에는 답할 수 있다.

키워드만 이어 붙인 문서는 같은 토큰이 문서당 대체로 1회만 나와 **TF 동점이 원문보다 많다.**
상위 8개를 고를 때 동점 구간이 넓다는 뜻이고, 동점 처리에 비결정성이 있다면 원문보다
잘 드러난다. 결정성에 대해 원문보다 **불리한** 조건이다.

주입은 회차마다 하지 않고 한 번만 한다. 매번 주입하면 입력이 같다는 보장이 사라진다.
크롤러와 달리 키를 video_id 로 줘서 파티션 배치도 해시로 고정했다.

### 결과 — 같은 입력 재실행

Redpanda 단일 노드(`localhost:9092`, 파티션 3), 토픽 `raw-youtube`, 1회 주입 후 N회 재실행.

| 주입 파일 | 문서 | 재실행 | 완전 일치 | 산출물 digest | p50 |
|---|---|---|---|---|---|
| `data/8차/keywords.jsonl` | 575 | **10회** | 575/575 (**100%**) | `ef6c1262bb2a3ae4` | 14.17s |
| `data/2025-05-03/keywords.jsonl` | 1,105 | **5회** | 1105/1105 (**100%**) | `535e0178e718e8fa` | 14.79s |
| 8차 + 05-03 신규 790건 | 1,365 | **3회** | 1365/1365 (**100%**) | `7789335fed5d7e95` | 13.50s |

**18회 전부 일치했다.** 서로 다른 digest 는 코퍼스당 1종뿐이고, 순서만 다른 건수는 0이다.
각 코퍼스는 토픽을 지우고 같은 파일로 다시 주입해도 같은 digest 가 나왔다 —
"같은 입력"이 Kafka 의 바이트 상태가 아니라 **원본 파일 수준에서** 성립한다.

Python 해시 랜덤화는 켜진 상태였다 (`PYTHONHASHSEED` 미설정, 회차마다 별도 프로세스라
문자열 해시 시드가 매번 달랐다). 그래도 결과가 갈리지 않았으므로 해시 순서 의존성은 없다.

### 결과 — 코퍼스만 키운 통제 실험

잡이 결정적임을 확인했으므로, 이제 코퍼스 의존성만 따로 잴 수 있다.
**같은 575개 문서를 글자 하나 바꾸지 않고** 두고 코퍼스만 575 → 1,365 로 키웠다
(겹치지 않는 790건 추가). `dropDuplicates(["video_id"])` 가 어느 쪽을 버릴지 보장되지
않으므로 중복 video_id 는 추가하지 않았다.

| | 575개 공통 영상 기준 |
|---|---|
| 완전 일치 | 107/575 (**18.6%**) |
| 집합 일치 | 182/575 (31.7%) |
| └ 순서만 다름 | 75 (13.0%) |
| 내용 다름 | 393/575 (**68.3%**) |

문서는 그대로인데 **68.3% 의 영상에서 상위 8개 키워드의 구성 자체가 바뀌었다.**

## 결정

**재현성 주장을 단계별로 쪼개서 쓴다. 파이프라인 전체에 대한 단일 주장은 쓰지 않는다.**

| 구간 | 주장 | 근거 |
|---|---|---|
| Spark TF-IDF, 같은 입력 | **결정적** (18회 100% 일치) | 위 측정 |
| Spark TF-IDF, 코퍼스 변경 | 보장 없음 (문서 고정해도 68.3% 변동) | 위 통제 실험 |
| LLM 3단계 | 보장 불가 (temperature 0.3 / 0.2 / 0.3) | 코드 |

**"구조적으로 보장 불가"라는 기존 결론은 유지된다.** LLM 단계가 그대로이기 때문이다.
다만 그 원인의 소재가 바뀐다 — Spark 잡은 용의선상에서 빠지고, 남는 것은
**배치 TF-IDF 의 코퍼스 의존성과 LLM** 둘뿐이다.

기존 문서의 "회차 간 완전 일치 0~10.7%" 는 잡의 비결정성으로 읽히기 쉬웠다.
그 수치는 **코퍼스 의존성만을** 나타낸다.

## 기각한 대안

- **크롤러 재실행으로 입력 만들기** — 매번 다른 영상이 들어와 "같은 입력" 조건이 깨진다.
  측정하려는 것과 반대 방향이다.
- **원본 영상 텍스트 복원** — 저장돼 있지 않다. YouTube API 로 다시 받아도 그 사이
  제목·설명문이 수정됐을 수 있어 당시 입력이라는 보장이 없다.
- **IDF 고정으로 코퍼스 의존성 제거** — 온라인 IDF 나 고정 코퍼스로 바꾸는 설계 변경이다.
  재현성을 얻는 대신 "이번 주에 유독 튀는 키워드"를 못 잡게 되므로, 이 파이프라인의
  목적(주간 트렌드)과 상충한다. 지금 결정할 사안이 아니다.

## 한계

- 입력 코퍼스가 **재구성물**이다. 원문 제목·설명문으로 같은 결과가 나온다는 보장은 없다.
  다만 동점이 더 많은 조건에서 통과했으므로 원문에서 갈릴 여지는 더 작다.
- 단일 노드(`local[*]`, 16스레드)다. executor 가 여러 대인 실제 클러스터에서
  같은 결과가 나온다는 것은 이 측정으로 말할 수 없다.
- 브로커가 Redpanda 다. 원본 구성(Apache Kafka)과 브로커 구현이 다르다.
  잡은 배치 `read` 로 `earliest` 부터 전량을 읽으므로 영향은 없을 것으로 보지만 측정하지 않았다.
- 18회는 비결정성이 **드물게** 나타나는 경우를 배제하지 못한다. 18회 연속 일치는
  "매 실행 5% 확률로 갈린다"까지는 배제하지만, 0.1% 확률은 배제하지 못한다.

## 재현

```bash
docker run -d --name vcc-kafka -p 9092:9092 redpandadata/redpanda:latest \
  redpanda start --overprovisioned --smp 1 --memory 1G --node-id 0 --check=false \
  --kafka-addr PLAINTEXT://0.0.0.0:9092 --advertise-kafka-addr PLAINTEXT://localhost:9092
docker exec vcc-kafka rpk topic create raw-youtube -p 3 -r 1

python kafka/replay_to_kafka.py --source data/8차/keywords.jsonl
python spark/verify_reproducibility.py run --times 10 --source data/8차/keywords.jsonl

# 575개 코퍼스의 산출물을 따로 보관해 둔다 (아래 통제 실험의 A 쪽)
cp data/keywords.jsonl eval/repro_runs/corpus_575_8cha_only.jsonl

# 코퍼스만 키운 통제 실험 — 같은 575문서 + 겹치지 않는 790건
docker exec vcc-kafka rpk topic delete raw-youtube
docker exec vcc-kafka rpk topic create raw-youtube -p 3 -r 1
python kafka/replay_to_kafka.py --source data/8차/keywords.jsonl \
                               --extra  data/2025-05-03/keywords.jsonl
python spark/verify_reproducibility.py run --times 3 \
  --source data/8차/keywords.jsonl data/2025-05-03/keywords.jsonl
cp data/keywords.jsonl eval/repro_runs/corpus_1365_8cha_plus_0503.jsonl

python spark/verify_reproducibility.py compare \
  eval/repro_runs/corpus_575_8cha_only.jsonl \
  eval/repro_runs/corpus_1365_8cha_plus_0503.jsonl \
  --out spark/bench_results/repro_corpus_growth.json
```

`eval/repro_runs/` 는 `.gitignore` 대상이다. 결론에 필요한 digest 와 비교 결과는
`spark/bench_results/repro_*.json` 에 남으므로 회차별 원본 jsonl 은 보관하지 않는다.

원본: `spark/bench_results/repro_run_*.json`, `spark/bench_results/repro_corpus_growth_*.json`

측정 환경: AMD Ryzen 7 7800X3D(8C/16T) · RAM 63GB · Windows 11 ·
Python 3.9.7 · PySpark 3.3.4(`local[*]`) · JDK 17 · Redpanda(단일 노드)
