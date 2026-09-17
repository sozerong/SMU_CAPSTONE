"""
유튜브 키워드 × 서울시 카페 메뉴 조인 파이프라인

    유튜브 키워드 (data/*/keywords.jsonl)
        └─ 정규화 ─┐
                   ├─ join ─→ 키워드별 집계 (몇 개 카페가 이미 팔고 있나)
    카페 메뉴 ─정규화┘
    (data/seoul_cafe_menus.jsonl)

⚠️  카페 메뉴는 합성 데이터다. spark/make_cafe_menu.py 주석 참조.

모드 (--mode):
    plain      기본 조인. autoBroadcastJoinThreshold=-1 로 두어 SortMergeJoin을 강제한다.
               (조인 대상이 작을 때 Spark가 알아서 broadcast 해버려서, 힌트의 효과를
                보려면 자동 broadcast를 꺼야 비교가 성립한다)
    broadcast  pyspark.sql.functions.broadcast() 힌트
    salt       broadcast + 빈도 상위 키에만 salt를 붙여 2단계 집계

파티션별 레코드 수는 mapPartitionsWithIndex 로 직접 세서 실제 편차를 기록한다.

실행:
    python spark/make_cafe_menu.py --cafes 20000
    python spark/menu_join.py --mode plain     --repeat 3
    python spark/menu_join.py --mode broadcast --repeat 3
    python spark/menu_join.py --mode salt --salt-buckets 16 --repeat 3
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

# Spark executor가 띄울 파이썬. "python" 만 넣으면 Windows에서 PATH를 못 찾아
# CreateProcess error=2 로 죽는다. 실행 중인 인터프리터를 그대로 쓴다.
os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "spark"))

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType

import menu_normalize

DATA_DIR    = os.path.join(BASE_DIR, "data")
MENU_PATH   = os.path.join(DATA_DIR, "seoul_cafe_menus.jsonl")
RESULTS_DIR = os.path.join(BASE_DIR, "spark", "bench_results")


# ── Spark ────────────────────────────────────────────────────
def create_spark(shuffle_partitions: int, auto_broadcast: bool) -> SparkSession:
    builder = (
        SparkSession.builder
        .master("local[*]")
        .appName("menu_join")
        .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
        # AQE는 스큐를 자동으로 완화해버린다. salting의 효과만 따로 보려면 꺼야 한다.
        # (실무에선 켜두는 게 맞고, 아래 측정은 "AQE 없이 손으로 고치면" 이라는 조건이다)
        .config("spark.sql.adaptive.enabled", "false")
    )
    if not auto_broadcast:
        builder = builder.config("spark.sql.autoBroadcastJoinThreshold", "-1")
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    # UDF가 menu_normalize를 참조한다. executor에도 같이 실어보내지 않으면
    # ModuleNotFoundError 로 죽는다.
    spark.sparkContext.addPyFile(os.path.join(BASE_DIR, "spark", "menu_normalize.py"))
    return spark


# ── 정규화 UDF ───────────────────────────────────────────────
normalize_udf = F.udf(menu_normalize.normalize, StringType())


# ── 데이터 로드 ──────────────────────────────────────────────
def load_menus(spark: SparkSession) -> DataFrame:
    if not os.path.isfile(MENU_PATH):
        raise FileNotFoundError(
            f"{MENU_PATH} 없음 — 먼저 실행: python spark/make_cafe_menu.py --cafes 20000"
        )
    df = spark.read.json(MENU_PATH)
    return (
        df.withColumn("norm_menu", normalize_udf(F.col("menu_name")))
          .filter(F.col("norm_menu") != "")
    )


def load_keywords(spark: SparkSession) -> DataFrame:
    """유튜브에서 뽑은 키워드 — video_id별 keywords 배열을 편다."""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*", "keywords.jsonl")))
    if not files:
        raise FileNotFoundError(f"키워드 파일 없음: {DATA_DIR}/*/keywords.jsonl")
    df = spark.read.json(files)
    return (
        df.select(F.explode("keywords").alias("keyword"))
          .withColumn("norm_keyword", normalize_udf(F.col("keyword")))
          .filter(F.col("norm_keyword") != "")
          .dropDuplicates(["norm_keyword"])
    )


# ── 파티션 편차 측정 ─────────────────────────────────────────
def partition_sizes(df: DataFrame) -> List[int]:
    """mapPartitionsWithIndex 로 파티션별 레코드 수를 직접 센다."""
    def count_partition(idx: int, it: Iterator[Any]) -> Iterator[Tuple[int, int]]:
        n = 0
        for _ in it:
            n += 1
        yield (idx, n)

    pairs = df.rdd.mapPartitionsWithIndex(count_partition).collect()
    sizes = [0] * (max(i for i, _ in pairs) + 1)
    for idx, n in pairs:
        sizes[idx] = n
    return sizes


def skew_report(sizes: List[int]) -> Dict[str, float]:
    nonzero = [s for s in sizes if s > 0]
    med = statistics.median(nonzero) if nonzero else 0
    mx  = max(sizes) if sizes else 0
    return {
        "partitions":      float(len(sizes)),
        "nonempty":        float(len(nonzero)),
        "max":             float(mx),
        "median":          float(med),
        "min":             float(min(nonzero)) if nonzero else 0.0,
        "max_over_median": float(mx / med) if med else 0.0,
        "total":           float(sum(sizes)),
    }


# ── 파이프라인 ───────────────────────────────────────────────
def build_joined(menus: DataFrame, keywords: DataFrame, mode: str) -> DataFrame:
    right = F.broadcast(keywords) if mode in ("broadcast", "salt") else keywords
    return menus.join(right, menus["norm_menu"] == right["norm_keyword"], "inner")


def aggregate_plain(joined: DataFrame) -> DataFrame:
    return (
        joined.groupBy("norm_keyword")
        .agg(
            F.countDistinct("cafe_id").alias("cafe_count"),
            F.count("*").alias("menu_rows"),
            F.countDistinct("gu").alias("gu_count"),
            F.round(F.avg("price"), 0).alias("avg_price"),
        )
    )


def aggregate_salted(joined: DataFrame, buckets: int, hot_keys: List[str]) -> DataFrame:
    """
    2단계 집계. **빈도 상위 키에만** salt를 붙인다.
    전체에 붙이면 재집계 비용만 늘고 얻는 게 없다.
    countDistinct 는 2단계로 쪼갤 수 없어 collect_set 합집합으로 처리한다.
    """
    hot = F.array([F.lit(k) for k in hot_keys]) if hot_keys else F.array()
    salted = joined.withColumn(
        "salt",
        F.when(
            F.array_contains(hot, F.col("norm_keyword")),
            (F.rand() * buckets).cast("int"),
        ).otherwise(F.lit(0)),
    )

    stage1 = (
        salted.groupBy("norm_keyword", "salt")
        .agg(
            F.collect_set("cafe_id").alias("cafes"),
            F.count("*").alias("menu_rows"),
            F.collect_set("gu").alias("gus"),
            F.sum("price").alias("price_sum"),
        )
    )

    return (
        stage1.groupBy("norm_keyword")
        .agg(
            F.size(F.array_distinct(F.flatten(F.collect_list("cafes")))).alias("cafe_count"),
            F.sum("menu_rows").alias("menu_rows"),
            F.size(F.array_distinct(F.flatten(F.collect_list("gus")))).alias("gu_count"),
            (F.sum("price_sum") / F.sum("menu_rows")).alias("avg_price_raw"),
        )
        .withColumn("avg_price", F.round(F.col("avg_price_raw"), 0))
        .drop("avg_price_raw")
    )


def find_hot_keys(joined: DataFrame, top_n: int) -> List[str]:
    rows = (
        joined.groupBy("norm_keyword").count()
        .orderBy(F.desc("count")).limit(top_n).collect()
    )
    return [r["norm_keyword"] for r in rows]


# ── 실행 ─────────────────────────────────────────────────────
def run_once(spark: SparkSession, args: argparse.Namespace) -> Dict[str, Any]:
    t0 = time.perf_counter()

    menus    = load_menus(spark)
    keywords = load_keywords(spark)

    t_load = time.perf_counter()
    menu_n, kw_n = menus.count(), keywords.count()
    load_seconds = time.perf_counter() - t_load

    joined = build_joined(menus, keywords, args.mode)

    plan = joined._jdf.queryExecution().executedPlan().toString()
    join_type = (
        "BroadcastHashJoin" if "BroadcastHashJoin" in plan else
        "SortMergeJoin"     if "SortMergeJoin"     in plan else
        "ShuffledHashJoin"  if "ShuffledHashJoin"  in plan else "기타"
    )

    hot_keys: List[str] = []
    if args.mode == "salt":
        hot_keys = find_hot_keys(joined, args.hot_keys)

    t_agg = time.perf_counter()
    if args.mode == "salt":
        result = aggregate_salted(joined, args.salt_buckets, hot_keys)
    else:
        result = aggregate_plain(joined)
    result = result.orderBy(F.desc("cafe_count"))
    rows = result.collect()
    agg_seconds = time.perf_counter() - t_agg

    # 집계 셔플이 실제로 어떻게 흩어지는지 — **그 모드가 쓰는 키 그대로** 재야 한다.
    # salt 모드인데 norm_keyword 만으로 재면 salting을 안 한 것과 같은 숫자가 나온다.
    pre_agg = joined.select("norm_keyword", "cafe_id", "gu", "price")
    if args.mode == "salt":
        hot = F.array([F.lit(k) for k in hot_keys]) if hot_keys else F.array()
        pre_agg = pre_agg.withColumn(
            "salt",
            F.when(F.array_contains(hot, F.col("norm_keyword")),
                   (F.rand() * args.salt_buckets).cast("int")).otherwise(F.lit(0)),
        ).repartition(args.shuffle_partitions, F.col("norm_keyword"), F.col("salt"))
    else:
        pre_agg = pre_agg.repartition(args.shuffle_partitions, F.col("norm_keyword"))
    sizes = partition_sizes(pre_agg)

    return {
        "mode":            args.mode,
        "join_type":       join_type,
        "menu_rows":       menu_n,
        "keyword_rows":    kw_n,
        "matched_keywords": len(rows),
        "total_seconds":   time.perf_counter() - t0,
        "load_seconds":    load_seconds,
        "agg_seconds":     agg_seconds,
        "skew":            skew_report(sizes),
        "hot_keys":        hot_keys,
        "top": [
            {"keyword": r["norm_keyword"], "cafe_count": r["cafe_count"],
             "menu_rows": r["menu_rows"], "gu_count": r["gu_count"],
             "avg_price": r["avg_price"]}
            for r in rows[:10]
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="유튜브 키워드 × 카페 메뉴 조인")
    ap.add_argument("--mode", choices=["plain", "broadcast", "salt"], default="plain")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--shuffle-partitions", type=int, default=16)
    ap.add_argument("--salt-buckets", type=int, default=16)
    ap.add_argument("--hot-keys", type=int, default=5,
                    help="salt를 붙일 빈도 상위 키 개수")
    args = ap.parse_args()

    spark = create_spark(args.shuffle_partitions, auto_broadcast=(args.mode != "plain"))
    print(f"Spark {spark.version} | mode={args.mode} | 명사 추출={menu_normalize.engine()}")

    runs: List[Dict[str, Any]] = []
    for i in range(1, args.repeat + 1):
        r = run_once(spark, args)
        runs.append(r)
        sk = r["skew"]
        print(f"[{i}/{args.repeat}] {r['total_seconds']:6.2f}s | {r['join_type']:<18} "
              f"| 파티션 max={sk['max']:.0f} median={sk['median']:.0f} "
              f"max/median={sk['max_over_median']:.2f}")

    totals = sorted(r["total_seconds"] for r in runs)
    p50 = totals[len(totals) // 2]
    last = runs[-1]

    print(f"\n=== {args.mode} — {args.repeat}회 ===")
    print(f"  조인 방식        : {last['join_type']}")
    print(f"  메뉴 행          : {last['menu_rows']:,}")
    print(f"  키워드 행        : {last['keyword_rows']:,}")
    print(f"  매칭 키워드      : {last['matched_keywords']:,}")
    print(f"  총 소요 p50      : {p50:.2f}s  (min {totals[0]:.2f} / max {totals[-1]:.2f})")
    sk = last["skew"]
    print(f"  파티션 편차      : max={sk['max']:.0f} median={sk['median']:.0f} "
          f"min={sk['min']:.0f} → max/median={sk['max_over_median']:.2f}")
    if last["hot_keys"]:
        print(f"  salt 적용 키     : {last['hot_keys']}")
    print("\n  상위 키워드 (카페 수):")
    for t in last["top"][:8]:
        print(f"    {t['keyword']:<16} {t['cafe_count']:>7,}개 카페  "
              f"{t['gu_count']:>2}개구  평균 {t['avg_price']:,.0f}원")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"menu_join_{args.mode}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"args": vars(args), "p50": p50, "runs": runs}, f, ensure_ascii=False, indent=2)
    print(f"\n저장: {path}")

    spark.stop()


if __name__ == "__main__":
    main()
