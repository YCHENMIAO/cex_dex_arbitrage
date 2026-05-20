from __future__ import annotations

import argparse
from dataclasses import dataclass

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc


DB_PATH = "dfs://BinanceMarket"
BOOK_DEPTH_TABLE = "bookDepth"
FACTOR_TABLE = "depthFactors"
DEBUG_TABLE = "depthFactorDebug"


@dataclass(frozen=True)
class FactorConfig:
    """因子计算使用的盘口档位和 OBI 权重。"""

    # 当前 bookDepth 是长表：每行是一个 percentage 档位的累计 depth。
    # 这里用 -level 作为 bid 档位，用 +level 作为 ask 档位，近似 L1-L5。
    levels: tuple[float, ...] = (0.2, 1.0, 2.0, 3.0, 4.0)
    weights: tuple[float, ...] = (1.0, 0.5, 0.3, 0.2, 0.1)

    def validate(self) -> None:
        if len(self.levels) != len(self.weights):
            raise ValueError("levels and weights must have the same length")
        if len(self.levels) < 3:
            raise ValueError("at least 3 levels are required to calculate curvature")
        if any(level <= 0 for level in self.levels):
            raise ValueError("all levels must be positive")
        if any(self.levels[i] >= self.levels[i + 1] for i in range(len(self.levels) - 1)):
            raise ValueError("levels must be strictly increasing")


def ddb_quote(value: str) -> str:
    """转义传给 DolphinDB 脚本的字符串。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_float_tuple(value: str) -> tuple[float, ...]:
    """解析形如 '0.2,1,2,3,4' 的命令行参数。"""
    return tuple(float(item.strip()) for item in value.split(",") if item.strip())


def ddb_float(value: float) -> str:
    """把 Python float 稳定地渲染成 DolphinDB 数值文本。"""
    return f"{value:.12g}"


def ddb_date(value: str) -> str:
    """把 YYYY-MM-DD 转成 DolphinDB 日期字面量 YYYY.MM.DD。"""
    return value.replace("-", ".")


def create_factor_table(session: ddb.Session, db_path: str, table_name: str) -> None:
    """Step 1: 创建正式因子表。"""
    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

if(!existsTable(dbPath, "{table_name}")){{
    schema = table(
        1:0,
        `symbol`tradeDate`Time`dynamicWeightOBI`cumulativeOFI1s`avgFirstGradient`avgSecondCurvature,
        [SYMBOL, DATE, TIMESTAMP, DOUBLE, DOUBLE, DOUBLE, DOUBLE]
    )
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{table_name}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`Time,
        keepDuplicates=LAST
    )
}}
"""
    session.run(script)


def create_debug_table(
    session: ddb.Session,
    db_path: str,
    table_name: str,
    config: FactorConfig,
) -> None:
    """可选：创建 debug 表，保存中间档位和局部结构因子，便于排查计算结果。"""
    config.validate()

    level_cols = []
    level_types = []
    for idx in range(1, len(config.levels) + 1):
        level_cols.extend([f"bq{idx}", f"aq{idx}"])
        level_types.extend(["DOUBLE", "DOUBLE"])

    cols = [
        "symbol",
        "tradeDate",
        "Time",
        "ofiBucket",
        *level_cols,
        "dynamicWeightOBI",
        "cumulativeOFI1s",
        "avgFirstGradient",
        "avgSecondCurvature",
    ]
    types = [
        "SYMBOL",
        "DATE",
        "TIMESTAMP",
        "TIMESTAMP",
        *level_types,
        "DOUBLE",
        "DOUBLE",
        "DOUBLE",
        "DOUBLE",
    ]

    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

if(!existsTable(dbPath, "{table_name}")){{
    schema = table(
        1:0,
        `{"`".join(cols)},
        [{", ".join(types)}]
    )
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{table_name}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`Time,
        keepDuplicates=LAST
    )
}}
"""
    session.run(script)


def build_factor_script(
    db_path: str,
    source_table: str,
    factor_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
    config: FactorConfig,
    overwrite: bool,
    write_debug: bool,
    debug_table: str,
) -> str:
    """生成 DolphinDB 脚本：从 bookDepth 合成正式因子表。"""
    config.validate()

    signed_levels = [-level for level in config.levels] + list(config.levels)
    level_filters = ",".join(ddb_float(level) for level in signed_levels)

    temp_tables: list[str] = []
    joins: list[str] = []
    weighted_terms: list[str] = []
    gradient_terms: list[str] = []
    curvature_terms: list[str] = []
    debug_level_cols: list[str] = []

    # Step 2: 从 bookDepth 的 percentage 长表生成 levelData 宽表。
    # levelData 会包含 bq1/aq1, bq2/aq2 ...，后续所有因子都基于它计算。
    for idx, (level, weight) in enumerate(zip(config.levels, config.weights), start=1):
        bid_table = f"bid{idx}"
        ask_table = f"ask{idx}"
        bq_col = f"bq{idx}"
        aq_col = f"aq{idx}"
        level_text = ddb_float(level)
        weight_text = ddb_float(weight)

        debug_level_cols.extend([bq_col, aq_col])

        temp_tables.append(
            f"""
{bid_table} = select symbol, eventTime, first(depth) as {bq_col}
from depth
where percentage = -{level_text}
group by symbol, eventTime
"""
        )
        temp_tables.append(
            f"""
{ask_table} = select symbol, eventTime, first(depth) as {aq_col}
from depth
where percentage = {level_text}
group by symbol, eventTime
"""
        )

        if idx == 1:
            joins.append(f"levelData = lj({bid_table}, {ask_table}, `symbol`eventTime)")
        else:
            joins.append(f"levelData = lj(levelData, {bid_table}, `symbol`eventTime)")
            joins.append(f"levelData = lj(levelData, {ask_table}, `symbol`eventTime)")

        weighted_terms.append(
            f"iif(isNull({bq_col}) or isNull({aq_col}) or ({bq_col}+{aq_col})=0, "
            f"0.0, ({bq_col}-{aq_col})/({bq_col}+{aq_col})*{weight_text})"
        )

    # Step 3: 一阶梯度、二阶曲率。
    # 伪代码里的分子是绝对深度差，截图里 3 万左右的梯度就是这样来的。
    # 为了降低量纲影响，这里改成相对深度变化：
    #   relative_grad = ((q_next - q_current) / q_current) / level_gap
    # 曲率再用相邻 relative_grad 的差分。这样不同日期、不同交易对之间更可比。
    for idx in range(1, len(config.levels)):
        gap = config.levels[idx] - config.levels[idx - 1]
        denom = ddb_float(gap if gap != 0 else 0.0001)
        gradient_terms.append(
            f"iif(isNull(bq{idx}) or isNull(bq{idx + 1}), 0.0, "
            f"iif(abs(bq{idx})<0.0001, 0.0, ((bq{idx + 1}-bq{idx})/bq{idx})/{denom}))"
        )
        gradient_terms.append(
            f"iif(isNull(aq{idx}) or isNull(aq{idx + 1}), 0.0, "
            f"iif(abs(aq{idx})<0.0001, 0.0, ((aq{idx + 1}-aq{idx})/aq{idx})/{denom}))"
        )

    for idx in range(1, len(config.levels) - 1):
        gap1 = config.levels[idx] - config.levels[idx - 1]
        gap2 = config.levels[idx + 1] - config.levels[idx]
        denom1 = ddb_float(gap1 if gap1 != 0 else 0.0001)
        denom2 = ddb_float(gap2 if gap2 != 0 else 0.0001)
        curvature_terms.append(
            f"iif(isNull(bq{idx}) or isNull(bq{idx + 1}) or isNull(bq{idx + 2}), 0.0, "
            f"iif(abs(bq{idx})<0.0001 or abs(bq{idx + 1})<0.0001, 0.0, "
            f"((bq{idx + 2}-bq{idx + 1})/bq{idx + 1})/{denom2} - "
            f"((bq{idx + 1}-bq{idx})/bq{idx})/{denom1}))"
        )
        curvature_terms.append(
            f"iif(isNull(aq{idx}) or isNull(aq{idx + 1}) or isNull(aq{idx + 2}), 0.0, "
            f"iif(abs(aq{idx})<0.0001 or abs(aq{idx + 1})<0.0001, 0.0, "
            f"((aq{idx + 2}-aq{idx + 1})/aq{idx + 1})/{denom2} - "
            f"((aq{idx + 1}-aq{idx})/aq{idx})/{denom1}))"
        )

    weighted_obi_expr = " + ".join(weighted_terms)
    avg_gradient_expr = f"({'+'.join(gradient_terms)})/{len(gradient_terms)}"
    avg_curvature_expr = f"({'+'.join(curvature_terms)})/{len(curvature_terms)}"
    start_ddb_date = ddb_date(start_date)
    end_ddb_date = ddb_date(end_date)

    overwrite_script = ""
    if overwrite:
        overwrite_script = f"""
delete from factorTable
where symbol=`{symbol}, tradeDate between {start_ddb_date} : {end_ddb_date}
"""
        if write_debug:
            overwrite_script += f"""
debugTable = loadTable(dbPath, "{debug_table}")
delete from debugTable
where symbol=`{symbol}, tradeDate between {start_ddb_date} : {end_ddb_date}
"""

    debug_script = ""
    if write_debug:
        debug_cols = ", ".join(debug_level_cols)
        debug_script = f"""
debugTable = loadTable(dbPath, "{debug_table}")
debugFactors = select
    symbol,
    tradeDate,
    Time,
    ofiBucket,
    {debug_cols},
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature
from factorsWithBucket
tableInsert(debugTable, debugFactors)
"""

    return f"""
dbPath = "{ddb_quote(db_path)}"
depth = loadTable(dbPath, "{source_table}")
factorTable = loadTable(dbPath, "{factor_table}")

{overwrite_script}

// Step 2: 过滤原始 bookDepth，只保留目标交易对、日期区间和本次需要的档位。
depth = select symbol, tradeDate, eventTime, percentage, depth
from depth
where symbol=`{symbol},
      tradeDate between {start_ddb_date} : {end_ddb_date},
      percentage in [{level_filters}]

{''.join(temp_tables)}
{chr(10).join(joins)}

levelData = select * from levelData order by symbol, eventTime

// Step 3: 计算动态权重 OBI、平均一阶梯度、平均二阶曲率。
// 这三类因子都天然以盘口快照 eventTime 为时间轴。
// avgFirstGradient 和 avgSecondCurvature 使用相对深度变化，避免绝对 depth 规模主导因子。
snapshotFactors = select
    symbol,
    date(eventTime) as tradeDate,
    eventTime as Time,
    bar(eventTime, 1s) as ofiBucket,
    {weighted_obi_expr} as dynamicWeightOBI,
    {avg_gradient_expr} as avgFirstGradient,
    {avg_curvature_expr} as avgSecondCurvature
from levelData

// Step 4: 计算 1 秒累计 OFI，并用 ofiBucket 对齐回盘口快照。
// 标准 OFI 需要最优价 b1/a1；当前表只有最内侧档位 bq1/aq1，
// 因此先用 delta(bq1) - delta(aq1) 作为近似 OFI。
ofiRaw = select
    symbol,
    eventTime,
    iif(isNull(prev(bq1)), 0.0, bq1 - prev(bq1)) as bidOFI,
    iif(isNull(prev(aq1)), 0.0, aq1 - prev(aq1)) as askOFI
from levelData
context by symbol

ofi1s = select
    symbol,
    bar(eventTime, 1s) as ofiBucket,
    sum(bidOFI - askOFI) as cumulativeOFI1s
from ofiRaw
group by symbol, bar(eventTime, 1s)

factorsWithBucket = lj(snapshotFactors, ofi1s, `symbol`ofiBucket)

// Step 5: 写入正式因子表。
// 正式表不保留 ofiBucket，只保留建模需要的最终因子列。
factors = select
    symbol,
    tradeDate,
    Time,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature
from factorsWithBucket

tableInsert(factorTable, factors)

// Step 6: 可选写 debug 表，保留中间档位，方便检查因子构造。
{debug_script}
"""


def calculate_factors(
    session: ddb.Session,
    db_path: str,
    source_table: str,
    factor_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
    config: FactorConfig,
    overwrite: bool,
    write_debug: bool,
    debug_table: str,
) -> None:
    """执行 DolphinDB 因子合成脚本。"""
    script = build_factor_script(
        db_path=db_path,
        source_table=source_table,
        factor_table=factor_table,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        config=config,
        overwrite=overwrite,
        write_debug=write_debug,
        debug_table=debug_table,
    )
    session.run(script)


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate and merge depth factors in DolphinDB.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--source-table", default=BOOK_DEPTH_TABLE)
    parser.add_argument("--factor-table", default=FACTOR_TABLE)
    parser.add_argument("--debug-table", default=DEBUG_TABLE)
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--start-date", default="2026-04-20")
    parser.add_argument("--end-date", default="2026-04-28")
    parser.add_argument("--levels", default="0.2,1,2,3,4")
    parser.add_argument("--weights", default="1,0.5,0.3,0.2,0.1")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete existing factor rows for the same symbol/date range before inserting.",
    )
    parser.add_argument(
        "--write-debug",
        action="store_true",
        help="Also write intermediate level data to the debug table.",
    )
    args = parser.parse_args()

    config = FactorConfig(
        levels=parse_float_tuple(args.levels),
        weights=parse_float_tuple(args.weights),
    )

    session = ddb.Session()
    try:
        session.connect(args.host, args.port, args.user, args.password)
        create_factor_table(session, args.db_path, args.factor_table)
        if args.write_debug:
            create_debug_table(session, args.db_path, args.debug_table, config)
        calculate_factors(
            session=session,
            db_path=args.db_path,
            source_table=args.source_table,
            factor_table=args.factor_table,
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            config=config,
            overwrite=args.overwrite,
            write_debug=args.write_debug,
            debug_table=args.debug_table,
        )
        print(f"Factor table updated: {args.db_path}/{args.factor_table}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
