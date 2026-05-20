from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc


DB_PATH = "dfs://BinanceMarket"
DATASET_TABLE = "obiLiqPredictionDataset"

FEATURE_COLS = [
    "dynamicWeightOBI",
    "cumulativeOFI1s",
    "avgFirstGradient",
    "avgSecondCurvature",
    "dynamicWeightOBIMean10",
    "dynamicWeightOBIMax10",
    "dynamicWeightOBIMin10",
    "dynamicWeightOBIChange10",
    "dynamicWeightOBIMean15",
    "dynamicWeightOBIChange15",
]

BASE_COLS = [
    "symbol",
    "tradeDate",
    "factorTime",
    *FEATURE_COLS,
    "liqDirection",
    "hasLiquidation",
]


def ddb_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ddb_date(value: str) -> str:
    return value.replace("-", ".")


def build_dataset_query(
    db_path: str,
    dataset_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    columns = ", ".join(BASE_COLS)
    return f"""
dataset = loadTable("{ddb_quote(db_path)}", "{ddb_quote(dataset_table)}")
select {columns}
from dataset
where symbol=`{ddb_quote(symbol)},
      tradeDate between {ddb_date(start_date)} : {ddb_date(end_date)},
      not isNull(dynamicWeightOBI),
      not isNull(cumulativeOFI1s),
      not isNull(avgFirstGradient),
      not isNull(avgSecondCurvature),
      not isNull(dynamicWeightOBIMean10),
      not isNull(dynamicWeightOBIMax10),
      not isNull(dynamicWeightOBIMin10),
      not isNull(dynamicWeightOBIChange10),
      not isNull(dynamicWeightOBIMean15),
      not isNull(dynamicWeightOBIChange15),
      not isNull(hasLiquidation)
order by tradeDate, factorTime
"""


def fetch_dataset(
    session: ddb.Session,
    db_path: str,
    dataset_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    query = build_dataset_query(db_path, dataset_table, symbol, start_date, end_date)
    result = session.run(query)
    if not isinstance(result, pd.DataFrame):
        result = pd.DataFrame(result)
    return result


def read_local_data(path: str) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    return pd.read_csv(path)


def export_dataset(data: pd.DataFrame, export_path: str | None) -> None:
    if not export_path:
        return

    path = Path(export_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        data.to_parquet(path, index=False)
    elif suffix in {".pkl", ".pickle"}:
        data.to_pickle(path)
    else:
        data.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Exported DolphinDB data to {path}")


def split_by_date(
    data: pd.DataFrame,
    train_start: str,
    train_end: str,
    valid_start: str,
    valid_end: str,
    test_start: str,
    test_end: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trade_date = pd.to_datetime(data["tradeDate"]).dt.date

    def mask(start: str, end: str) -> pd.Series:
        start_date = pd.to_datetime(start).date()
        end_date = pd.to_datetime(end).date()
        return (trade_date >= start_date) & (trade_date <= end_date)

    return (
        data.loc[mask(train_start, train_end)].copy(),
        data.loc[mask(valid_start, valid_end)].copy(),
        data.loc[mask(test_start, test_end)].copy(),
    )


def require_non_empty(name: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        raise SystemExit(f"{name} set is empty. Check symbol/date ranges.")


def import_lightgbm():
    try:
        import lightgbm as lgbm
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Missing dependency: lightgbm. Install it before training.") from exc
    return lgbm


def make_xy(frame: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    x = frame.loc[:, FEATURE_COLS].astype("float64")
    y = frame["hasLiquidation"].astype(int).to_numpy()
    return x, y


def binary_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    """Mann-Whitney AUC implementation, with average ranks for tied scores."""
    y_true = y_true.astype(int)
    positives = int(np.sum(y_true == 1))
    negatives = int(np.sum(y_true == 0))
    if positives == 0 or negatives == 0:
        return float("nan")

    order = np.argsort(score)
    sorted_score = score[order]
    ranks = np.empty(len(score), dtype="float64")
    i = 0
    while i < len(score):
        j = i + 1
        while j < len(score) and sorted_score[j] == sorted_score[i]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0
        i = j

    pos_rank_sum = float(np.sum(ranks[y_true == 1]))
    return (pos_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def average_precision(y_true: np.ndarray, score: np.ndarray) -> float:
    y_true = y_true.astype(int)
    positives = int(np.sum(y_true == 1))
    if positives == 0:
        return float("nan")

    order = np.argsort(-score)
    sorted_true = y_true[order]
    tp = np.cumsum(sorted_true == 1)
    fp = np.cumsum(sorted_true == 0)
    precision = tp / np.maximum(tp + fp, 1)
    return float(np.sum(precision[sorted_true == 1]) / positives)


def metrics_at_threshold(y_true: np.ndarray, score: np.ndarray, threshold: float) -> dict[str, float]:
    pred = (score >= threshold).astype(int)
    tp = int(np.sum((y_true == 1) & (pred == 1)))
    fp = int(np.sum((y_true == 0) & (pred == 1)))
    tn = int(np.sum((y_true == 0) & (pred == 0)))
    fn = int(np.sum((y_true == 1) & (pred == 0)))

    precision = 0.0 if tp + fp == 0 else tp / (tp + fp)
    recall = 0.0 if tp + fn == 0 else tp / (tp + fn)
    specificity = 0.0 if tn + fp == 0 else tn / (tn + fp)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    accuracy = (tp + tn) / max(len(y_true), 1)
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "f1": f1,
        "accuracy": accuracy,
        "predicted_positive_rate": float(np.mean(pred == 1)),
    }


def find_best_threshold(
    y_true: np.ndarray,
    score: np.ndarray,
    metric: str = "f1",
) -> tuple[float, pd.DataFrame]:
    thresholds = np.unique(np.quantile(score, np.linspace(0.01, 0.99, 99)))
    rows = [metrics_at_threshold(y_true, score, float(threshold)) for threshold in thresholds]
    table = pd.DataFrame(rows)
    best_row = table.sort_values([metric, "recall", "precision"], ascending=False).iloc[0]
    return float(best_row["threshold"]), table


def label_distribution(train_set: pd.DataFrame, valid_set: pd.DataFrame, test_set: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, frame in [("train", train_set), ("valid", valid_set), ("test", test_set)]:
        y = frame["hasLiquidation"].astype(int)
        rows.append(
            {
                "split": name,
                "rows": len(frame),
                "positive": int(np.sum(y == 1)),
                "negative": int(np.sum(y == 0)),
                "positive_rate": float(np.mean(y == 1)),
            }
        )
    return pd.DataFrame(rows)


def train_binary_lgbm(
    train_set: pd.DataFrame,
    valid_set: pd.DataFrame,
    test_set: pd.DataFrame,
    num_boost_round: int,
    threshold: float,
    choose_threshold: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    lgbm = import_lightgbm()

    train_x, train_y = make_xy(train_set)
    valid_x, valid_y = make_xy(valid_set)
    test_x, test_y = make_xy(test_set)

    positive = int(np.sum(train_y == 1))
    negative = int(np.sum(train_y == 0))
    scale_pos_weight = 1.0 if positive == 0 else negative / positive

    train_data = lgbm.Dataset(train_x, label=train_y)
    valid_data = lgbm.Dataset(valid_x, label=valid_y, reference=train_data)
    params = {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "boosting": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "scale_pos_weight": scale_pos_weight,
        "verbose": -1,
    }
    model = lgbm.train(
        params,
        train_data,
        num_boost_round=num_boost_round,
        valid_sets=[valid_data],
    )

    valid_score = model.predict(valid_x)
    test_score = model.predict(test_x)
    selected_threshold = threshold
    threshold_table = pd.DataFrame()
    if choose_threshold:
        selected_threshold, threshold_table = find_best_threshold(valid_y, valid_score)

    summary_rows = []
    for split, y_true, score in [
        ("valid", valid_y, valid_score),
        ("test", test_y, test_score),
    ]:
        threshold_metrics = metrics_at_threshold(y_true, score, selected_threshold)
        threshold_metrics.update(
            {
                "split": split,
                "auc": binary_auc(y_true, score),
                "pr_auc": average_precision(y_true, score),
                "positive_rate": float(np.mean(y_true == 1)),
            }
        )
        summary_rows.append(threshold_metrics)

    columns = [
        "split",
        "auc",
        "pr_auc",
        "positive_rate",
        "threshold",
        "precision",
        "recall",
        "specificity",
        "f1",
        "accuracy",
        "predicted_positive_rate",
        "tp",
        "fp",
        "tn",
        "fn",
    ]
    return pd.DataFrame(summary_rows).loc[:, columns], threshold_table


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train a local Python LightGBM binary classifier for hasLiquidation."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--dataset-table", default=DATASET_TABLE)
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--train-start", default="2026-04-20")
    parser.add_argument("--train-end", default="2026-04-25")
    parser.add_argument("--valid-start", default="2026-04-26")
    parser.add_argument("--valid-end", default="2026-04-26")
    parser.add_argument("--test-start", default="2026-04-27")
    parser.add_argument("--test-end", default="2026-04-28")
    parser.add_argument("--num-boost-round", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--choose-threshold",
        action="store_true",
        help="Choose the threshold with best validation F1, then evaluate test with that threshold.",
    )
    parser.add_argument(
        "--local-data",
        default=None,
        help="Read an exported CSV/Parquet/Pickle instead of connecting to DolphinDB.",
    )
    parser.add_argument(
        "--export-path",
        default=None,
        help="Optional local export path for DolphinDB data, e.g. data/liq_binary.csv.",
    )
    parser.add_argument(
        "--print-query",
        action="store_true",
        help="Print the DolphinDB export query instead of executing it.",
    )
    args = parser.parse_args()

    query_start = min(args.train_start, args.valid_start, args.test_start)
    query_end = max(args.train_end, args.valid_end, args.test_end)
    query = build_dataset_query(
        db_path=args.db_path,
        dataset_table=args.dataset_table,
        symbol=args.symbol,
        start_date=query_start,
        end_date=query_end,
    )
    if args.print_query:
        print(query)
        return

    if args.local_data:
        data = read_local_data(args.local_data)
    else:
        session = ddb.Session()
        try:
            session.connect(args.host, args.port, args.user, args.password)
            data = fetch_dataset(
                session=session,
                db_path=args.db_path,
                dataset_table=args.dataset_table,
                symbol=args.symbol,
                start_date=query_start,
                end_date=query_end,
            )
        finally:
            session.close()
        export_dataset(data, args.export_path)

    train_set, valid_set, test_set = split_by_date(
        data,
        train_start=args.train_start,
        train_end=args.train_end,
        valid_start=args.valid_start,
        valid_end=args.valid_end,
        test_start=args.test_start,
        test_end=args.test_end,
    )
    require_non_empty("train", train_set)
    require_non_empty("valid", valid_set)
    require_non_empty("test", test_set)

    print("Label distribution:")
    print(label_distribution(train_set, valid_set, test_set).to_string(index=False))

    metrics, threshold_table = train_binary_lgbm(
        train_set=train_set,
        valid_set=valid_set,
        test_set=test_set,
        num_boost_round=args.num_boost_round,
        threshold=args.threshold,
        choose_threshold=args.choose_threshold,
    )

    print("\nBinary metrics:")
    print(metrics.to_string(index=False))

    if args.choose_threshold and not threshold_table.empty:
        print("\nTop validation thresholds by F1:")
        print(
            threshold_table.sort_values(["f1", "recall", "precision"], ascending=False)
            .head(10)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
