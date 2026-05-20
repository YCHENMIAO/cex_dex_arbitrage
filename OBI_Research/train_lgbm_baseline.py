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
    "avgFirstGradient",
    "avgSecondCurvature",
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
    """Escape Python strings before inserting them into DolphinDB scripts."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ddb_date(value: str) -> str:
    """Convert YYYY-MM-DD to DolphinDB date literal text YYYY.MM.DD."""
    return value.replace("-", ".")


def build_dataset_query(
    db_path: str,
    dataset_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    """Build the DolphinDB query used only for exporting model data."""
    columns = ", ".join(BASE_COLS)
    return f"""
dataset = loadTable("{ddb_quote(db_path)}", "{ddb_quote(dataset_table)}")
select {columns}
from dataset
where symbol=`{ddb_quote(symbol)},
      tradeDate between {ddb_date(start_date)} : {ddb_date(end_date)},
      not isNull(dynamicWeightOBI),
      not isNull(avgFirstGradient),
      not isNull(avgSecondCurvature),
      not isNull(liqDirection),
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
    """Export a date slice from DolphinDB into a local pandas DataFrame."""
    query = build_dataset_query(db_path, dataset_table, symbol, start_date, end_date)
    result = session.run(query)
    if not isinstance(result, pd.DataFrame):
        result = pd.DataFrame(result)
    return result


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

    train_set = data.loc[mask(train_start, train_end)].copy()
    valid_set = data.loc[mask(valid_start, valid_end)].copy()
    test_set = data.loc[mask(test_start, test_end)].copy()
    return train_set, valid_set, test_set


def require_non_empty(name: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        raise SystemExit(f"{name} set is empty. Check symbol/date ranges in DolphinDB.")


def make_dataset(frame: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, np.ndarray]:
    x = frame.loc[:, FEATURE_COLS].astype("float64")
    y = frame[label_col].to_numpy()
    return x, y


def import_lightgbm():
    try:
        import lightgbm as lgbm
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Missing dependency: lightgbm. Install it before training.") from exc
    return lgbm


def class_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    labels: list[int],
    model_name: str,
) -> pd.DataFrame:
    rows = []
    for label in labels:
        support = int(np.sum(y_true == label))
        predicted = int(np.sum(y_pred == label))
        true_positive = int(np.sum((y_true == label) & (y_pred == label)))
        precision = 0.0 if predicted == 0 else true_positive / predicted
        recall = 0.0 if support == 0 else true_positive / support
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        rows.append(
            {
                "model": model_name,
                "classLabel": label,
                "support": support,
                "predicted": predicted,
                "truePositive": true_positive,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    return pd.DataFrame(rows)


def distribution(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    counts = (
        frame.groupby("liqDirection", dropna=False)
        .size()
        .rename("rows")
        .reset_index()
        .sort_values("liqDirection")
    )
    counts.insert(0, "split", name)
    return counts


def train_multiclass(
    train_set: pd.DataFrame,
    valid_set: pd.DataFrame,
    test_set: pd.DataFrame,
    num_boost_round: int,
) -> pd.DataFrame:
    lgbm = import_lightgbm()
    train_x, train_y_raw = make_dataset(train_set, "liqDirection")
    valid_x, valid_y_raw = make_dataset(valid_set, "liqDirection")
    test_x, test_y = make_dataset(test_set, "liqDirection")

    train_y = train_y_raw.astype(int) + 1
    valid_y = valid_y_raw.astype(int) + 1

    train_data = lgbm.Dataset(train_x, label=train_y)
    valid_data = lgbm.Dataset(valid_x, label=valid_y, reference=train_data)
    params = {
        "objective": "multiclassova",
        "num_class": 3,
        "metric": "multi_logloss",
        "boosting": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "is_unbalance": True,
        "verbose": -1,
    }
    model = lgbm.train(
        params,
        train_data,
        num_boost_round=num_boost_round,
        valid_sets=[valid_data],
    )
    pred_prob = model.predict(test_x)
    pred_label = np.argmax(pred_prob, axis=1).astype(int) - 1
    return class_metrics(test_y.astype(int), pred_label, [-1, 0, 1], "baseline_multiclass")


def train_two_step(
    train_set: pd.DataFrame,
    valid_set: pd.DataFrame,
    test_set: pd.DataFrame,
    risk_num_boost_round: int,
    direction_num_boost_round: int,
) -> pd.DataFrame:
    lgbm = import_lightgbm()
    risk_train_x, risk_train_y = make_dataset(train_set, "hasLiquidation")
    risk_valid_x, risk_valid_y = make_dataset(valid_set, "hasLiquidation")
    risk_test_x, risk_test_y = make_dataset(test_set, "hasLiquidation")

    risk_train_data = lgbm.Dataset(risk_train_x, label=risk_train_y.astype(int))
    risk_valid_data = lgbm.Dataset(risk_valid_x, label=risk_valid_y.astype(int), reference=risk_train_data)
    risk_params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "is_unbalance": True,
        "verbose": -1,
    }
    risk_model = lgbm.train(
        risk_params,
        risk_train_data,
        num_boost_round=risk_num_boost_round,
        valid_sets=[risk_valid_data],
    )
    risk_pred = (risk_model.predict(risk_test_x) >= 0.5).astype(int)
    risk_metrics = class_metrics(
        risk_test_y.astype(int),
        risk_pred,
        [0, 1],
        "risk_hasLiquidation",
    )

    direction_train_set = train_set.loc[train_set["liqDirection"] != 0].copy()
    direction_valid_set = valid_set.loc[valid_set["liqDirection"] != 0].copy()
    direction_test_set = test_set.loc[test_set["liqDirection"] != 0].copy()
    require_non_empty("direction train", direction_train_set)
    require_non_empty("direction valid", direction_valid_set)
    require_non_empty("direction test", direction_test_set)

    direction_train_x, direction_train_y_raw = make_dataset(direction_train_set, "liqDirection")
    direction_valid_x, direction_valid_y_raw = make_dataset(direction_valid_set, "liqDirection")
    direction_test_x, direction_test_y_raw = make_dataset(direction_test_set, "liqDirection")

    direction_train_y = (direction_train_y_raw.astype(int) == 1).astype(int)
    direction_valid_y = (direction_valid_y_raw.astype(int) == 1).astype(int)
    direction_test_y = (direction_test_y_raw.astype(int) == 1).astype(int)

    direction_train_data = lgbm.Dataset(direction_train_x, label=direction_train_y)
    direction_valid_data = lgbm.Dataset(
        direction_valid_x,
        label=direction_valid_y,
        reference=direction_train_data,
    )
    direction_params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 10,
        "is_unbalance": True,
        "verbose": -1,
    }
    direction_model = lgbm.train(
        direction_params,
        direction_train_data,
        num_boost_round=direction_num_boost_round,
        valid_sets=[direction_valid_data],
    )
    direction_pred_01 = (direction_model.predict(direction_test_x) >= 0.5).astype(int)
    direction_metrics = class_metrics(
        direction_test_y,
        direction_pred_01,
        [0, 1],
        "direction_nonzero",
    )
    direction_metrics["classLabel"] = direction_metrics["classLabel"].map({0: -1, 1: 1})

    return pd.concat([risk_metrics, direction_metrics], ignore_index=True)


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export data from DolphinDB and train a local Python LightGBM baseline."
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
    parser.add_argument("--risk-num-boost-round", type=int, default=100)
    parser.add_argument("--direction-num-boost-round", type=int, default=100)
    parser.add_argument(
        "--two-step",
        action="store_true",
        help="Also train the two-step binary risk and direction models.",
    )
    parser.add_argument(
        "--export-path",
        default=None,
        help="Optional local export path for the pulled DolphinDB data, e.g. data/obi_lgbm.csv.",
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

    distributions = pd.concat(
        [
            distribution(train_set, "train"),
            distribution(valid_set, "valid"),
            distribution(test_set, "test"),
        ],
        ignore_index=True,
    )
    print("Label distribution:")
    print(distributions.to_string(index=False))

    metrics = [train_multiclass(train_set, valid_set, test_set, args.num_boost_round)]
    if args.two_step:
        metrics.append(
            train_two_step(
                train_set,
                valid_set,
                test_set,
                risk_num_boost_round=args.risk_num_boost_round,
                direction_num_boost_round=args.direction_num_boost_round,
            )
        )

    final_metrics = pd.concat(metrics, ignore_index=True)
    print("\nMetrics:")
    print(final_metrics.to_string(index=False))


if __name__ == "__main__":
    main()
