"""Experiment with reusable similarity measures for subscriber time series.

Each (service, service_sub, channel) combination is an independent daily
series. The CLI analyzes all matching segments separately; partial filters
never combine them. Candidates always belong to the target segment. The analysis compares shape-normalized
windows so a large segment is not considered similar merely because it has a
large absolute subscriber count.

Example::

    python process/similarity/similarity_check.py --window-length 60 --top-n 5

The script writes CSV summaries and PNG plots beneath ``process/similarity``.
PyTorch is optional; the interpretable statistical methods are the default and
are also used when a neural-network dependency is unavailable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.spatial.distance import cosine as cosine_distance
from scipy.stats import rankdata

try:
    from numba import njit
except ImportError:
    def njit(function):
        return function


BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "database" / "broadband_company.db"
DEFAULT_OUTPUT_DIR = Path("/tmp/broadband-segment-similarity-results")
SEGMENT_COLUMNS = ("service", "service_sub", "channel")
DEFAULT_WINDOW_LENGTHS = (30, 60, 90)
DEFAULT_PREPROCESSORS = (
    "zscore",
    "minmax",
    "relative_mean",
    "smooth_zscore",
    "detrended_zscore",
    "difference_zscore",
    "pct_change_zscore",
    "log_zscore",
)
DEFAULT_METHODS = ("euclidean", "cosine", "correlation", "dtw", "feature", "hybrid")


with closing(sqlite3.connect(DB_PATH)) as _conn:
    # Keep the original project variable available for interactive use.
    df_subscribers = pd.read_sql_query(
        """
        SELECT date, service, service_sub, channel,
               subscriber_new AS subscribers
        FROM subscribers_historical
        """,
        _conn,
        parse_dates=["date"],
    )


@dataclass(frozen=True)
class WindowCollection:
    """Equal-length rolling windows and their positions in the source series."""

    raw: np.ndarray
    starts: np.ndarray
    ends: np.ndarray
    dates: pd.DatetimeIndex

    @property
    def count(self) -> int:
        return self.raw.shape[0]

    @property
    def length(self) -> int:
        return self.raw.shape[1]


@dataclass(frozen=True)
class SimilarityConfig:
    window_length: int
    preprocessing: str
    method: str


def select_daily_series(
    frame: pd.DataFrame,
    service: str | None = None,
    service_sub: str | None = None,
    channel: str | None = None,
) -> pd.Series:
    """Return exactly one leaf segment; never sum across dimension keys."""

    selected = frame.copy()
    for column, value in (("service", service), ("service_sub", service_sub), ("channel", channel)):
        if value is not None:
            selected = selected[selected[column].eq(value)]
    if selected.empty:
        raise ValueError("The requested service/service_sub/channel filters match no rows.")

    if selected[list(SEGMENT_COLUMNS)].isna().any().any():
        raise ValueError("Segment keys must not be missing")
    if len(selected[list(SEGMENT_COLUMNS)].drop_duplicates()) != 1:
        raise ValueError("Select exactly one (service, service_sub, channel) segment; aggregation is not allowed")
    if selected["date"].duplicated().any():
        raise ValueError("Duplicate daily records within a segment must be resolved before analysis")
    daily = selected.set_index("date")["subscribers"].sort_index().astype(float)
    full_index = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    daily = daily.reindex(full_index)
    # Do not silently fill a missing segment observation using future values.
    if not np.isfinite(daily.to_numpy()).all():
        raise ValueError("A segment has missing dates or non-finite counts; resolve the data before analysis")
    daily.index.name = "date"
    daily.attrs["segment"] = selected.iloc[0][list(SEGMENT_COLUMNS)].to_dict()
    return daily


def make_windows(series: pd.Series, window_length: int, step: int = 1) -> WindowCollection:
    """Create rolling windows while retaining source positions and date ranges."""

    if window_length < 2:
        raise ValueError("window_length must be at least 2")
    values = series.to_numpy(dtype=float)
    if len(values) < window_length:
        raise ValueError("The series is shorter than the requested window length.")
    starts = np.arange(0, len(values) - window_length + 1, step, dtype=int)
    ends = starts + window_length - 1
    raw = np.vstack([values[start : start + window_length] for start in starts])
    return WindowCollection(raw=raw, starts=starts, ends=ends, dates=series.index)


def _zscore(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    scale = float(np.std(values))
    return (values - float(np.mean(values))) / (scale if scale > 1e-12 else 1.0)


def _minmax(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    span = float(np.max(values) - np.min(values))
    return (values - float(np.min(values))) / (span if span > 1e-12 else 1.0)


def preprocess_window(values: Sequence[float], name: str) -> np.ndarray:
    """Apply a scale-robust transform to one window.

    Differencing and detrending intentionally remove level information. They
    are useful because the target is the shape of acquisition changes, not the
    absolute size of a service segment.
    """

    values = np.asarray(values, dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("A window contains a non-finite value.")
    if name == "zscore":
        return _zscore(values)
    if name == "minmax":
        return _minmax(values)
    if name == "relative_first":
        baseline = values[0] if abs(values[0]) > 1e-12 else np.mean(values)
        return values / (baseline if abs(baseline) > 1e-12 else 1.0) - 1.0
    if name == "relative_mean":
        baseline = float(np.mean(values))
        return values / (baseline if abs(baseline) > 1e-12 else 1.0) - 1.0
    if name in {"smooth_zscore", "smooth3_zscore", "smooth14_zscore"}:
        width = {"smooth_zscore": 7, "smooth3_zscore": 3, "smooth14_zscore": 14}[name]
        smoothed = pd.Series(values).rolling(width, center=True, min_periods=1).mean().to_numpy()
        return _zscore(smoothed)
    if name == "detrended_zscore":
        x = np.arange(len(values), dtype=float)
        slope, intercept = np.polyfit(x, values, 1)
        return _zscore(values - (slope * x + intercept))
    if name == "difference_zscore":
        return _zscore(np.diff(values))
    if name == "pct_change_zscore":
        denominator = np.maximum(np.abs(values[:-1]), 1e-12)
        return _zscore(np.diff(values) / denominator)
    if name == "log_zscore":
        # Counts are non-negative in this dataset; clipping keeps this safe for
        # a future extract containing a small negative correction.
        return _zscore(np.log1p(np.clip(values, a_min=0, a_max=None)))
    raise ValueError(f"Unknown preprocessing method: {name}")


def _correlation_distance(first: np.ndarray, second: np.ndarray) -> float:
    first_centered = first - np.mean(first)
    second_centered = second - np.mean(second)
    denominator = np.linalg.norm(first_centered) * np.linalg.norm(second_centered)
    if denominator <= 1e-12:
        return 0.0 if np.allclose(first, second) else 1.0
    correlation = float(np.dot(first_centered, second_centered) / denominator)
    return 1.0 - float(np.clip(correlation, -1.0, 1.0))


@njit
def dtw_distance(first: Sequence[float], second: Sequence[float], radius: int | None = None) -> float:
    """Return length-normalized DTW cost with an optional Sakoe-Chiba band."""

    first = np.asarray(first, dtype=np.float64)
    second = np.asarray(second, dtype=np.float64)
    if radius is None:
        radius = max(len(first), len(second))
    radius = max(int(radius), abs(len(first) - len(second)))
    previous = np.full(len(second) + 1, np.inf)
    previous[0] = 0.0
    path_length = np.full(len(second) + 1, np.inf)
    path_length[0] = 0.0
    for i, first_value in enumerate(first, 1):
        current = np.full(len(second) + 1, np.inf)
        current_path = np.full(len(second) + 1, np.inf)
        left = max(1, i - radius)
        right = min(len(second), i + radius)
        for j in range(left, right + 1):
            local_cost = abs(first_value - second[j - 1])
            predecessor_costs = (previous[j], current[j - 1], previous[j - 1])
            best_index = int(np.argmin(np.array(predecessor_costs)))
            predecessor_cost = predecessor_costs[best_index]
            predecessor_lengths = (path_length[j], current_path[j - 1], path_length[j - 1])
            current[j] = local_cost + predecessor_cost
            current_path[j] = predecessor_lengths[best_index] + 1.0
        previous, path_length = current, current_path
    if not np.isfinite(previous[-1]):
        return float("inf")
    return float(previous[-1] / max(path_length[-1], 1.0))


def _feature_vector(values: np.ndarray) -> np.ndarray:
    """Summarize shape, trend and short-term volatility after preprocessing."""

    values = np.asarray(values, dtype=float)
    x = np.arange(len(values), dtype=float)
    slope = np.polyfit(x, values, 1)[0] if len(values) > 1 else 0.0
    differences = np.diff(values)
    lag = min(7, len(values) - 1)
    autocorrelation = _correlation_distance(values[:-lag], values[lag:]) if lag else 0.0
    midpoint = len(values) // 2
    return np.array(
        [
            np.mean(values),
            np.std(values),
            np.quantile(values, 0.25),
            np.quantile(values, 0.75),
            slope,
            np.mean(np.abs(differences)) if len(differences) else 0.0,
            np.std(differences) if len(differences) else 0.0,
            np.mean(values[:midpoint]) - np.mean(values[midpoint:]),
            autocorrelation,
        ],
        dtype=float,
    )


def _pairwise_distances(transformed: np.ndarray, target_index: int, method: str, eligible: np.ndarray | None = None) -> np.ndarray:
    target = transformed[target_index]
    distances = np.full(transformed.shape[0], np.inf, dtype=float)
    for index, candidate in enumerate(transformed):
        if eligible is not None and not eligible[index]:
            continue
        if method == "euclidean":
            distances[index] = np.linalg.norm(target - candidate) / np.sqrt(len(target))
        elif method == "cosine":
            distances[index] = (float(cosine_distance(target, candidate)) if np.linalg.norm(target) * np.linalg.norm(candidate) > 1e-12 else (0.0 if np.allclose(target, candidate) else 1.0))
        elif method == "correlation":
            distances[index] = _correlation_distance(target, candidate)
        elif method.startswith("dtw"):
            fraction = float(method.split("_")[1]) if "_" in method else 0.15
            # A 15% band permits modest timing shifts without excessive warping.
            distances[index] = dtw_distance(target, candidate, radius=max(2, int(len(target) * fraction)))
        else:
            raise ValueError(f"Unsupported pairwise method: {method}")
    return distances


def _feature_distances(
    transformed: np.ndarray,
    target_index: int,
    fit_mask: np.ndarray | None = None,
) -> np.ndarray:
    features = np.vstack([_feature_vector(row) for row in transformed])
    fit_features = features if fit_mask is None else features[fit_mask]
    feature_center = np.mean(fit_features, axis=0)
    feature_scale = np.std(fit_features, axis=0)
    feature_scale[feature_scale <= 1e-12] = 1.0
    standardized = (features - feature_center) / feature_scale
    return np.linalg.norm(standardized - standardized[target_index], axis=1) / np.sqrt(features.shape[1])


def _siamese_cnn_distances(transformed: np.ndarray, target_index: int, fit_mask: np.ndarray | None = None, pooled: int = 1, seed: int = 7) -> np.ndarray:
    """Train an optional self-supervised Siamese 1D-CNN encoder.

    Each window is paired with a lightly noise-augmented view of itself. The
    contrastive objective learns an embedding in which those two views are
    close and other windows in the batch are negatives. This branch is
    deliberately optional: PyTorch is not required for the default,
    interpretable experiment and is imported only when explicitly requested.
    """

    try:
        import torch
        from torch import nn
    except ImportError as error:
        raise RuntimeError(
            "The siamese_cnn method requires optional PyTorch; use the default "
            "statistical methods or install torch first."
        ) from error

    torch.manual_seed(seed)
    torch.set_num_threads(1)
    inputs = torch.tensor(transformed, dtype=torch.float32).unsqueeze(1)

    training = inputs if fit_mask is None else inputs[fit_mask]
    if len(training) < 2:
        raise ValueError("CNN requires at least two historical training windows")

    class Encoder(nn.Module):
        def __init__(self, length: int) -> None:
            super().__init__()
            self.network = nn.Sequential(
                nn.Conv1d(1, 8, kernel_size=5, padding=2),
                nn.ReLU(),
                nn.Conv1d(8, 16, kernel_size=5, padding=2),
                nn.ReLU(),
                nn.AdaptiveAvgPool1d(pooled),
                nn.Flatten(),
                nn.Linear(16 * pooled, 16),
            )

        def forward(self, values: "torch.Tensor") -> "torch.Tensor":
            return nn.functional.normalize(self.network(values), dim=1)

    encoder = Encoder(inputs.shape[-1])
    optimizer = torch.optim.Adam(encoder.parameters(), lr=0.01)
    labels = torch.arange(len(training))
    for _ in range(60):
        first_view = training + torch.randn_like(training) * 0.02
        second_view = training + torch.randn_like(training) * 0.02
        first_embedding = encoder(first_view)
        second_embedding = encoder(second_view)
        logits = first_embedding @ second_embedding.T / 0.1
        loss = 0.5 * (
            nn.functional.cross_entropy(logits, labels)
            + nn.functional.cross_entropy(logits.T, labels)
        )
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    with torch.no_grad():
        embeddings = encoder(inputs)
        distances = 1.0 - embeddings[target_index] @ embeddings.T
    return distances.cpu().numpy().astype(float)


def _rank_normalize(distances: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """Convert heterogeneous distances to comparable percentile ranks."""

    result = np.ones_like(distances, dtype=float)
    values = distances[eligible & np.isfinite(distances)]
    if len(values) == 0:
        return result
    ranks = (rankdata(values, method="average") - 1) / max(len(values) - 1, 1)
    eligible_indices = np.flatnonzero(eligible & np.isfinite(distances))
    result[eligible_indices] = ranks
    return result


def calculate_distances(
    windows: WindowCollection,
    target_index: int,
    method: str,
    preprocessing: str,
    fit_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Calculate one method's distance from the target to every window."""

    transformed = np.vstack([preprocess_window(row, preprocessing) for row in windows.raw])
    if method in {"euclidean", "cosine", "correlation"} or method.startswith("dtw"):
        return _pairwise_distances(transformed, target_index, method)
    if method == "feature":
        return _feature_distances(transformed, target_index, fit_mask)
    if method == "siamese_cnn":
        return _siamese_cnn_distances(transformed, target_index, fit_mask)
    if method == "hybrid":
        # Rank blending puts DTW, correlation and Euclidean evidence on one
        # scale, preventing any one raw metric from dominating.
        components = [
            _pairwise_distances(transformed, target_index, "dtw"),
            _pairwise_distances(transformed, target_index, "correlation"),
            _pairwise_distances(transformed, target_index, "euclidean"),
        ]
        eligible = np.ones(windows.count, dtype=bool) if fit_mask is None else fit_mask
        return sum(
            weight * _rank_normalize(component, eligible)
            for weight, component in zip((0.45, 0.35, 0.20), components)
        )
    raise ValueError(f"Unknown similarity method: {method}")


def _eligible_mask(
    windows: WindowCollection,
    target_index: int,
    minimum_gap: int,
    causal_only: bool = True,
) -> np.ndarray:
    """Exclude the target, overlaps, and near-duplicate windows."""

    target_start = windows.starts[target_index]
    target_end = windows.ends[target_index]
    minimum_gap = max(1, minimum_gap)
    if causal_only:
        eligible = windows.ends <= target_start - minimum_gap
    else:
        eligible = (windows.ends <= target_start - minimum_gap) | (windows.starts >= target_end + minimum_gap)
    eligible[target_index] = False
    return eligible


def retrieve_similar_windows(
    windows: WindowCollection,
    target_index: int,
    method: str,
    preprocessing: str,
    top_n: int = 5,
    minimum_gap: int | None = None,
    causal_only: bool = True,
) -> pd.DataFrame:
    """Return historical matches that do not overlap the target window."""

    if top_n < 1:
        raise ValueError("top_n must be positive")
    minimum_gap = minimum_gap if minimum_gap is not None else max(7, windows.length // 2)
    eligible = _eligible_mask(windows, target_index, minimum_gap, causal_only)
    # Fit scaling, neural encoders and ensemble ranks on historical eligible
    # candidates only; the query is transformed without fitting on it.
    fit_mask = eligible.copy()
    fit_mask[target_index] = False
    distances = calculate_distances(windows, target_index, method, preprocessing, fit_mask)
    distances[~eligible] = np.inf
    order = np.argsort(distances, kind="stable")[:top_n]
    order = order[np.isfinite(distances[order])]
    target_start = windows.starts[target_index]
    records = []
    for rank, candidate_index in enumerate(order, start=1):
        separation = target_start - windows.ends[candidate_index]
        distance = float(distances[candidate_index])
        records.append(
            {
                "rank": rank,
                "candidate_index": int(candidate_index),
                "start": windows.dates[windows.starts[candidate_index]],
                "end": windows.dates[windows.ends[candidate_index]],
                "distance": distance,
                "similarity_score": 1.0 / (1.0 + distance),
                "separation_days": int(separation),
            }
        )
    return pd.DataFrame(records)


def _relative_continuation(values: np.ndarray) -> np.ndarray:
    baseline = values[0] if abs(values[0]) > 1e-12 else np.mean(values)
    return values / (baseline if abs(baseline) > 1e-12 else 1.0) - 1.0


def _continuation_metrics(target: np.ndarray, candidate: np.ndarray) -> tuple[float, float]:
    target_relative = _relative_continuation(target)
    candidate_relative = _relative_continuation(candidate)
    rmse = float(np.sqrt(np.mean((target_relative - candidate_relative) ** 2)))
    correlation = 1.0 - _correlation_distance(target_relative, candidate_relative)
    return rmse, correlation


def evaluate_configuration(
    series: pd.Series,
    config: SimilarityConfig,
    query_count: int = 4,
    horizon: int = 7,
    top_n: int = 5,
) -> dict[str, float | int | str]:
    """Use future continuation as a quantitative, leakage-safe proxy label.

    For each historical query, only windows ending before the query are
    eligible. A good shape match should also have a similar relative path in
    the following seven days, making method comparison less subjective than a
    single visual inspection.
    """

    windows = make_windows(series, config.window_length)
    minimum_gap = max(7, config.window_length // 2)
    # Leave room for one complete candidate window plus the separation gap
    # before the first query window.
    first_end = 2 * config.window_length + minimum_gap - 2
    last_end = len(series) - horizon - 1
    if first_end > last_end:
        return {
            "window_length": config.window_length,
            "preprocessing": config.preprocessing,
            "method": config.method,
            "queries": 0,
            "top1_rmse": np.nan,
            "top3_rmse": np.nan,
            "top5_rmse": np.nan,
            "top1_corr": np.nan,
        }
    query_ends = np.unique(np.linspace(first_end, last_end, query_count, dtype=int))
    top1_rmse: list[float] = []
    top3_rmse: list[float] = []
    top5_rmse: list[float] = []
    top1_corr: list[float] = []
    series_values = series.to_numpy(dtype=float)
    for query_end in query_ends:
        target_index = query_end - config.window_length + 1
        matches = retrieve_similar_windows(
            windows,
            target_index,
            config.method,
            config.preprocessing,
            top_n=top_n,
            minimum_gap=minimum_gap,
            causal_only=True,
        )
        if matches.empty:
            continue
        target_future = series_values[query_end + 1 : query_end + horizon + 1]
        errors = []
        correlations = []
        for candidate_index in matches["candidate_index"]:
            candidate_end = windows.ends[int(candidate_index)]
            candidate_future = series_values[candidate_end + 1 : candidate_end + horizon + 1]
            if len(candidate_future) != horizon:
                continue
            rmse, correlation = _continuation_metrics(target_future, candidate_future)
            errors.append(rmse)
            correlations.append(correlation)
        if errors:
            top1_rmse.append(errors[0])
            top3_rmse.append(float(np.mean(errors[:3])))
            top5_rmse.append(float(np.mean(errors[:5])))
            top1_corr.append(correlations[0])
    return {
        "window_length": config.window_length,
        "preprocessing": config.preprocessing,
        "method": config.method,
        "queries": len(top1_rmse),
        "top1_rmse": float(np.mean(top1_rmse)) if top1_rmse else np.nan,
        "top3_rmse": float(np.mean(top3_rmse)) if top3_rmse else np.nan,
        "top5_rmse": float(np.mean(top5_rmse)) if top5_rmse else np.nan,
        "top1_corr": float(np.mean(top1_corr)) if top1_corr else np.nan,
    }


def run_experiments(
    series: pd.Series,
    window_lengths: Iterable[int] = DEFAULT_WINDOW_LENGTHS,
    preprocessors: Iterable[str] = DEFAULT_PREPROCESSORS,
    methods: Iterable[str] = DEFAULT_METHODS,
    query_count: int = 4,
) -> pd.DataFrame:
    """Run the full parameter sweep and return a ranked evaluation table."""

    results = []
    for window_length in window_lengths:
        for preprocessing in preprocessors:
            for method in methods:
                config = SimilarityConfig(window_length, preprocessing, method)
                results.append(evaluate_configuration(series, config, query_count=query_count))
    summary = pd.DataFrame(results)
    return summary.sort_values(["top3_rmse", "top1_rmse"], na_position="last").reset_index(drop=True)


def _resolve_target_end(series: pd.Series, requested: str | None) -> int:
    if requested is None:
        return len(series) - 1
    timestamp = pd.Timestamp(requested)
    eligible = np.flatnonzero(series.index <= timestamp)
    if len(eligible) == 0:
        raise ValueError("target_end precedes the first date in the selected series.")
    return int(eligible[-1])


def plot_method_summary(summary: pd.DataFrame, output_path: Path) -> None:
    plot_data = summary.dropna(subset=["top3_rmse"]).head(12).copy()
    plot_data["label"] = plot_data.apply(
        lambda row: f"{row['method']}\n{row['preprocessing']}\n{int(row['window_length'])}d", axis=1
    )
    figure, axis = plt.subplots(figsize=(13, 6))
    axis.bar(np.arange(len(plot_data)), plot_data["top3_rmse"], color="#386cb0")
    axis.set_xticks(np.arange(len(plot_data)), plot_data["label"], rotation=45, ha="right")
    axis.set_ylabel("Mean continuation RMSE (lower is better)")
    axis.set_title("Time-series similarity experiment: best configurations")
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_path, dpi=150)
    plt.close(figure)


def plot_matches(
    windows: WindowCollection,
    target_index: int,
    matches: pd.DataFrame,
    config: SimilarityConfig,
    output_path: Path,
) -> None:
    count = len(matches)
    figure, axes = plt.subplots(count + 1, 1, figsize=(13, max(7, 2.4 * (count + 1))), sharex=False)
    axes = np.atleast_1d(axes)
    target = windows.raw[target_index]
    target_normalized = preprocess_window(target, config.preprocessing)
    target_dates = windows.dates[windows.starts[target_index] : windows.ends[target_index] + 1]
    axes[0].plot(np.arange(len(target_normalized)), target_normalized, color="#d95f02", linewidth=2, label="target")
    axes[0].set_title(f"Target: {target_dates[0].date()} to {target_dates[-1].date()}")
    axes[0].legend(loc="upper right")
    axes[0].grid(alpha=0.25)
    for axis, (_, match) in zip(axes[1:], matches.iterrows()):
        index = int(match["candidate_index"])
        dates = windows.dates[windows.starts[index] : windows.ends[index] + 1]
        axis.plot(np.arange(len(preprocess_window(windows.raw[index], config.preprocessing))), preprocess_window(windows.raw[index], config.preprocessing), color="#1b9e77", linewidth=1.7, label="match")
        axis.plot(np.arange(len(target_normalized)), target_normalized, color="#d95f02", linewidth=1.3, alpha=0.8, label="target")
        axis.set_title(f"Rank {int(match['rank'])}: {dates[0].date()} to {dates[-1].date()} | distance={match['distance']:.4f}")
        axis.legend(loc="upper right")
        axis.grid(alpha=0.25)
    figure.suptitle(f"Top historical matches ({config.method}, {config.preprocessing}, {config.window_length} days)", y=0.995)
    figure.tight_layout()
    figure.savefig(output_path, dpi=150)
    plt.close(figure)


def _parse_csv(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comprehensive", action="store_true", help="Run broad trials with common tuning and held-out query dates (requires torch).")
    parser.add_argument("--workers", type=int, default=1, help="Independent segment processes; default 1.")
    parser.add_argument("--service")
    parser.add_argument("--service-sub")
    parser.add_argument("--channel")
    parser.add_argument("--target-end", help="Target window end date; defaults to the latest available date.")
    parser.add_argument("--window-length", type=int, help="Run a single window length instead of 30, 60, and 90.")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--query-count", type=int, default=4, help="Backtest query windows per configuration.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--preprocessors", help="Comma-separated preprocessing methods to test.")
    parser.add_argument("--methods", help="Comma-separated methods to test.")
    return parser


def plot_comprehensive_diagnostics(series: pd.Series, summary: pd.DataFrame, latest: pd.DataFrame, output: Path) -> None:
    """Compare algorithm champions in common visual coordinates."""
    keys = ["window_length", "preprocessing", "method"]
    tuning = summary[(summary.split == "tune") & (summary.horizon == 7)]
    tuning = tuning[~tuning.method.isin(["recent", "weekday_recent"])].sort_values("top3_rmse")
    champions = tuning.groupby("method", sort=False).head(1)
    holdout = summary[(summary.split == "holdout") & (summary.horizon == 7)]
    comparison = champions.merge(holdout, on=keys, suffixes=("_tune", "_holdout"))
    figure, axes = plt.subplots(2, 1, figsize=(12, 7))
    axes[0].plot(series.index, series.values, linewidth=.8)
    axes[0].plot(series.index, series.rolling(7).mean(), label="7-day mean")
    axes[0].legend()
    axes[0].set_ylabel("Daily new subscribers")
    weekday = series.groupby(series.index.dayofweek).mean()
    axes[1].bar(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], weekday)
    axes[1].set_ylabel("Mean daily subscribers")
    figure.tight_layout()
    figure.savefig(output / "comprehensive_data_profile.png", dpi=140)
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(12, 6))
    x = np.arange(len(comparison))
    axis.bar(x - .2, comparison.top3_rmse_tune, .4, label="Tuning")
    axis.bar(x + .2, comparison.top3_rmse_holdout, .4, label="Held out")
    axis.set_xticks(x, comparison.method, rotation=45, ha="right")
    axis.set_ylabel("Mean individual top-3 continuation RMSE")
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "comprehensive_holdout_comparison.png", dpi=140)
    plt.close(figure)
    latest_champions = latest.merge(champions[keys], on=keys)
    figure, axes = plt.subplots((len(champions) + 1) // 2, 2, figsize=(14, 19))
    for axis, (method, group) in zip(axes.flat, latest_champions.groupby("method", sort=False)):
        row = group[group["rank"] == 1].iloc[0]
        length = int(row.window_length)
        axis.plot(_zscore(series.iloc[-length:].values), label="target", color="#d95f02")
        axis.plot(_zscore(series.loc[row.start:row.end].values), label="rank 1", alpha=.8, color="#1b9e77")
        axis.set_title(f"{method}, {length}d, {row.preprocessing}\nr={row.shape_corr:.3f}")
        axis.legend(fontsize=7)
        axis.set_xlabel("Day within window")
    figure.tight_layout()
    figure.savefig(output / "comprehensive_method_overlays.png", dpi=120)
    plt.close(figure)


def run_weekday_refinement(series: pd.Series, output: Path) -> None:
    """Exploratory follow-up to observed DTW weekday shifts; not selection data."""
    metadata = json.loads((output / "comprehensive_metadata.json").read_text())
    windows = make_windows(series, 45)
    rows = []
    for preprocessing in ("zscore", "minmax"):
        for query_date in [*metadata["tuning_endpoints"], *metadata["holdout_endpoints"], str(series.index[-1].date())]:
            end = series.index.get_loc(query_date)
            target = end - 44
            eligible = _eligible_mask(windows, target, 22)
            distances = calculate_distances(windows, target, "dtw_0.15", preprocessing, eligible)
            ranks = _rank_normalize(distances, eligible)
            for penalty in (0.25, 0.5, 1.0):
                adjusted = ranks + penalty * ((windows.starts - target) % 7 != 0)
                selected = []
                for index in np.argsort(np.where(eligible, adjusted, np.inf), kind="stable"):
                    if eligible[index] and all(abs(index - previous) >= 7 for previous in selected):
                        selected.append(int(index))
                    if len(selected) == 5:
                        break
                latest = query_date == str(series.index[-1].date())
                error = np.nan
                if not latest:
                    actual = series.iloc[end+1:end+8].to_numpy() / windows.raw[target].mean()
                    error = np.mean([np.sqrt(np.mean((series.iloc[index+45:index+52].to_numpy()/windows.raw[index].mean()-actual)**2)) for index in selected[:3]])
                rows.append(dict(preprocessing=preprocessing, penalty=penalty, query_end=query_date,
                    split="latest" if latest else ("tune" if query_date in metadata["tuning_endpoints"] else "holdout"),
                    top3_rmse=error, shape_corr=1-_correlation_distance(windows.raw[target], windows.raw[selected[0]]),
                    top1_start=str(series.index[selected[0]].date())))
    pd.DataFrame(rows).to_csv(output / "exploratory_weekday_dtw.csv", index=False)


def comprehensive_analysis(series: pd.Series, output: Path, top_n: int = 5) -> None:
    """Broad, chronological experiment; selection uses tuning queries only.

    Continuations are scaled by the observed window mean, never their own
    first future observation. Seven-day spacing among retrieved starts reduces
    duplicate neighbors without exhausting the short historical record.
    """
    import hashlib
    import platform
    import torch

    output.mkdir(parents=True, exist_ok=True)
    if top_n < 1:
        raise ValueError("top_n must be positive")
    lengths = (30, 45, 60, 75, 90)
    preprocessors = (*DEFAULT_PREPROCESSORS, "relative_first", "smooth3_zscore", "smooth14_zscore")
    # Common query endpoints make window-length comparisons fair. Fourteen-day
    # spacing prevents continuation overlap at both tested horizons.
    endpoints = np.arange(254, len(series) - 14, 14)
    if len(endpoints) < 8:
        raise ValueError("Comprehensive analysis requires at least 367 daily observations")
    split = len(endpoints) * 2 // 3
    values = series.to_numpy()
    rows, latest_rows = [], []

    def select(distances, eligible, count):
        chosen = []
        for index in np.argsort(np.where(eligible, distances, np.inf), kind="stable"):
            if not eligible[index] or not np.isfinite(distances[index]):
                continue
            if all(abs(index - previous) >= 7 for previous in chosen):
                chosen.append(int(index))
            if len(chosen) == count:
                break
        return chosen

    for length in lengths:
        windows = make_windows(series, length)
        for prep in preprocessors:
            transformed = np.vstack([preprocess_window(row, prep) for row in windows.raw])
            for qi, end in enumerate([*endpoints, len(series) - 1]):
                target = int(end - length + 1)
                eligible = _eligible_mask(windows, target, max(14, length // 2))
                # Compute each primitive once, then reuse it in the ensembles.
                scores = {method: _pairwise_distances(transformed, target, method, eligible)
                          for method in ("euclidean", "cosine", "correlation", "dtw_0.05", "dtw_0.15", "dtw_0.30")}
                scores["feature"] = _feature_distances(transformed, target, eligible)
                for name, weights in (("hybrid", (.45, .35, .20)), ("hybrid_shape", (.20, .60, .20)), ("hybrid_warp", (.70, .20, .10))):
                    scores[name] = sum(weight * _rank_normalize(scores[metric], eligible)
                                       for weight, metric in zip(weights, ("dtw_0.15", "correlation", "euclidean")))
                # Architecture and seed ablation on the two most interpretable
                # neural inputs; CNNs are fitted only to historical candidates.
                if prep in ("zscore", "relative_mean"):
                    for pooled in (1, 4):
                        for seed in (7, 19):
                            scores[f"cnn_pool{pooled}_seed{seed}"] = _siamese_cnn_distances(transformed, target, eligible, pooled, seed)
                scores["recent"] = -windows.ends.astype(float)
                # Weekday alignment is a meaningful simple seasonal baseline.
                scores["weekday_recent"] = np.where((windows.starts - target) % 7 == 0, -windows.ends, 1e6 - windows.ends).astype(float)
                for method, distances in scores.items():
                    chosen = select(distances, eligible, max(top_n, 5))
                    if qi == len(endpoints):
                        for rank, index in enumerate(chosen[:top_n], 1):
                            latest_rows.append(dict(window_length=length, preprocessing=prep, method=method, rank=rank,
                                target_start=str(series.index[target].date()), target_end=str(series.index[end].date()),
                                start=str(series.index[index].date()), end=str(series.index[index + length - 1].date()),
                                candidate_index=index, distance=float(distances[index]),
                                similarity_score=float(1 / (1 + max(0, distances[index]))) if method not in ("recent", "weekday_recent") else np.nan,
                                shape_corr=1 - _correlation_distance(windows.raw[target], windows.raw[index]),
                                mean_ratio=float(windows.raw[index].mean() / windows.raw[target].mean())))
                        continue
                    for horizon in (7, 14):
                        actual = values[end + 1:end + horizon + 1] / windows.raw[target].mean()
                        predictions = np.vstack([values[index + length:index + length + horizon] / windows.raw[index].mean() for index in chosen])
                        errors = np.sqrt(np.mean((predictions - actual) ** 2, axis=1))
                        rows.append(dict(window_length=length, preprocessing=prep, method=method,
                            query_end=str(series.index[end].date()), split="tune" if qi < split else "holdout", horizon=horizon,
                            top1_rmse=errors[0], top3_rmse=errors[:3].mean(), top5_rmse=errors[:5].mean(),
                            ensemble3_rmse=np.sqrt(np.mean((predictions[:3].mean(axis=0)-actual)**2)),
                            shape_corr=1-_correlation_distance(windows.raw[target], windows.raw[chosen[0]]),
                            candidate_count=int(eligible.sum()), retrieved_count=len(chosen)))
            print(f"Completed {length}d / {prep}", flush=True)
    detail = pd.DataFrame(rows)
    keys = ["window_length", "preprocessing", "method"]
    metrics = ["top1_rmse", "top3_rmse", "top5_rmse", "ensemble3_rmse", "shape_corr"]
    summary = detail.groupby(keys + ["split", "horizon"], as_index=False)[metrics].mean()
    tuning = summary[(summary.split == "tune") & (summary.horizon == 7)].sort_values("top3_rmse")
    tuning = tuning[~tuning.method.isin(["recent", "weekday_recent"])]
    winner = tuning.iloc[0]
    config = SimilarityConfig(int(winner.window_length), winner.preprocessing, winner.method)
    all_latest = pd.DataFrame(latest_rows)
    matches = all_latest[(all_latest.window_length == config.window_length) & (all_latest.preprocessing == config.preprocessing) & (all_latest.method == config.method)]
    detail.to_csv(output / "comprehensive_query_metrics.csv", index=False)
    summary.to_csv(output / "comprehensive_method_comparison.csv", index=False)
    all_latest.to_csv(output / "comprehensive_all_matches.csv", index=False)
    matches.to_csv(output / "comprehensive_top_matches.csv", index=False)
    plot_matches(make_windows(series, config.window_length), len(series)-config.window_length, matches, config, output / "comprehensive_top_matches.png")
    plot_method_summary(tuning, output / "comprehensive_method_comparison.png")
    metadata = dict(series_start=str(series.index[0].date()), series_end=str(series.index[-1].date()), days=len(series),
        series_sha256=hashlib.sha256(series.to_csv().encode()).hexdigest(),
        selected_configuration=config.__dict__, tuning_endpoints=[str(series.index[x].date()) for x in endpoints[:split]],
        holdout_endpoints=[str(series.index[x].date()) for x in endpoints[split:]],
        configurations=len(summary[keys].drop_duplicates()), neural_configurations=len(summary[summary.method.str.startswith("cnn")][keys].drop_duplicates()),
        versions=dict(python=platform.python_version(), numpy=np.__version__, pandas=pd.__version__, torch=torch.__version__),
        selection="Minimum tuning mean top3 RMSE at horizon 7; holdout never used for selection",
        statistics=series.describe().to_dict())
    (output / "comprehensive_metadata.json").write_text(json.dumps(metadata, indent=2))
    plot_comprehensive_diagnostics(series, summary, all_latest, output)
    run_weekday_refinement(series, output)
    print(json.dumps(metadata, indent=2))


def _run_single_segment(args: argparse.Namespace, series: pd.Series) -> int:
    if args.comprehensive:
        comprehensive_analysis(series.loc[:args.target_end] if args.target_end else series, args.output_dir, args.top_n)
        return 0
    window_lengths = [args.window_length] if args.window_length else list(DEFAULT_WINDOW_LENGTHS)
    preprocessors = _parse_csv(args.preprocessors) or list(DEFAULT_PREPROCESSORS)
    methods = _parse_csv(args.methods) or list(DEFAULT_METHODS)
    if any(length < 30 or length > 90 for length in window_lengths):
        raise ValueError("window lengths must be between 30 and 90 days")
    target_end_position = _resolve_target_end(series, args.target_end)
    series = series.iloc[:target_end_position + 1]

    print(f"Series: {len(series)} daily observations ({series.index[0].date()} to {series.index[-1].date()})")
    print(f"Experimenting with {len(window_lengths)} window lengths, {len(preprocessors)} preprocessors, {len(methods)} methods")
    summary = run_experiments(series, window_lengths, preprocessors, methods, args.query_count)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.output_dir / "similarity_method_comparison.csv", index=False)

    best = summary.dropna(subset=["top3_rmse"]).iloc[0]
    config = SimilarityConfig(int(best["window_length"]), str(best["preprocessing"]), str(best["method"]))
    windows = make_windows(series, config.window_length)
    target_index = target_end_position - config.window_length + 1
    if target_index < 0 or target_index >= windows.count:
        raise ValueError("The target date is too early for the selected window length.")
    matches = retrieve_similar_windows(windows, target_index, config.method, config.preprocessing, args.top_n)
    matches.insert(0, "target_start", windows.dates[windows.starts[target_index]])
    matches.insert(1, "target_end", windows.dates[windows.ends[target_index]])
    matches.insert(2, "method", config.method)
    matches.insert(3, "preprocessing", config.preprocessing)
    matches.insert(4, "window_length", config.window_length)
    matches.to_csv(args.output_dir / "similarity_top_matches.csv", index=False)

    metadata = {
        "series_start": str(series.index[0].date()),
        "series_end": str(series.index[-1].date()),
        "target_start": str(windows.dates[windows.starts[target_index]].date()),
        "target_end": str(windows.dates[windows.ends[target_index]].date()),
        "selected_configuration": config.__dict__,
        "tested_configurations": int(len(summary)),
        "neural_network_note": "CNN is optional; consult tested methods for whether it ran.",
    }
    (args.output_dir / "similarity_run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print("\nBest configuration by mean top-3 continuation RMSE:")
    print(summary.head(10).to_string(index=False))
    print("\nTop historical matches:")
    print(matches.to_string(index=False))
    if not args.no_plots:
        plot_method_summary(summary, args.output_dir / "similarity_method_comparison.png")
        plot_matches(windows, target_index, matches, config, args.output_dir / "similarity_top_matches.png")
        print(f"\nPlots written to {args.output_dir}")
    return 0


def _segment_job(payload: tuple[dict, dict]) -> dict:
    """Run one isolated leaf series and label every saved result with its key."""
    import hashlib

    options, segment = payload
    args = argparse.Namespace(**options)
    segment_id = hashlib.sha256(json.dumps(segment, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    args.output_dir = Path(args.output_dir) / segment_id
    series = select_daily_series(df_subscribers, **segment)
    print(f"Starting segment {segment_id}: {segment}", flush=True)
    _run_single_segment(args, series)
    prefix = "comprehensive" if args.comprehensive else "similarity"
    metadata_name = "comprehensive_metadata.json" if args.comprehensive else "similarity_run_metadata.json"
    metadata_path = args.output_dir / metadata_name
    metadata = json.loads(metadata_path.read_text())
    metadata.update(segment=segment, segment_id=segment_id, analysis_unit="service/service_sub/channel", candidate_scope="same segment only")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    # Include identities even when CSVs are separated from their directory.
    for csv in args.output_dir.glob("*.csv"):
        frame = pd.read_csv(csv)
        for key, value in reversed(list(segment.items())):
            frame.insert(0, key, value)
        frame.insert(0, "segment_id", segment_id)
        frame.to_csv(csv, index=False)
    matches = pd.read_csv(args.output_dir / f"{prefix}_top_matches.csv")
    summary = pd.read_csv(args.output_dir / f"{prefix}_method_comparison.csv")
    config = metadata["selected_configuration"]
    selected = summary
    for key, value in config.items():
        selected = selected[selected[key] == value]
    record = dict(segment_id=segment_id, **segment, **config)
    if args.comprehensive:
        for split in ("tune", "holdout"):
            row = selected[(selected.split == split) & (selected.horizon == 7)].iloc[0]
            for metric in ("top1_rmse", "top3_rmse", "top5_rmse", "ensemble3_rmse", "shape_corr"):
                record[f"{metric}_{split}"] = float(row[metric])
    else:
        record.update(selected.iloc[0].to_dict())
    print(f"Finished segment {segment_id}", flush=True)
    return dict(record=record, matches=matches.to_dict(orient="records"), metadata=metadata)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch each selected dimension key independently, including by default."""
    from concurrent.futures import ProcessPoolExecutor

    args = build_parser().parse_args(argv)
    if args.workers < 1:
        raise ValueError("workers must be positive")
    selected = df_subscribers
    for column in SEGMENT_COLUMNS:
        value = getattr(args, column)
        if value is not None:
            selected = selected[selected[column] == value]
    if selected.empty:
        raise ValueError("The requested segment filters match no rows")
    if selected[list(SEGMENT_COLUMNS)].isna().any().any():
        raise ValueError("Segment keys must not be missing")
    segments = selected[list(SEGMENT_COLUMNS)].drop_duplicates().sort_values(list(SEGMENT_COLUMNS)).to_dict(orient="records")
    payloads = [(vars(args), segment) for segment in segments]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.workers == 1:
        results = [_segment_job(payload) for payload in payloads]
    else:
        with ProcessPoolExecutor(max_workers=min(args.workers, len(segments))) as executor:
            results = list(executor.map(_segment_job, payloads))
    pd.DataFrame([item["record"] for item in results]).to_csv(args.output_dir / "segment_selected_configurations.csv", index=False)
    pd.DataFrame([match for item in results for match in item["matches"]]).to_csv(args.output_dir / "segment_top_matches.csv", index=False)
    manifest = dict(analysis_unit="service/service_sub/channel", candidate_scope="same segment only", segment_count=len(results),
                    segments=[item["metadata"] for item in results])
    (args.output_dir / "segment_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Completed {len(results)} independent segments. Results: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
