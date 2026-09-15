"""Focused regression checks for similarity boundaries and numerical behavior."""
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
import json

import numpy as np
import pandas as pd

import similarity_check as similarity


class SimilarityTests(unittest.TestCase):
    @staticmethod
    def segment_frame():
        return pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01", "2026-01-02"] * 2),
            "service": ["A"] * 4,
            "service_sub": ["IP"] * 4,
            "channel": ["direct", "direct", "partner", "partner"],
            "subscribers": [10, 20, 1000, 2000],
        })

    def test_multiple_segments_cannot_be_aggregated(self):
        with self.assertRaisesRegex(ValueError, "aggregation is not allowed"):
            similarity.select_daily_series(self.segment_frame(), service="A")

    def test_selected_leaf_is_independent_of_other_channels(self):
        frame = self.segment_frame()
        before = similarity.select_daily_series(frame, "A", "IP", "direct")
        frame.loc[frame.channel == "partner", "subscribers"] *= 100
        after = similarity.select_daily_series(frame, "A", "IP", "direct")
        np.testing.assert_array_equal(before.values, [10, 20])
        pd.testing.assert_series_equal(before, after)
        self.assertEqual(before.attrs["segment"], dict(service="A", service_sub="IP", channel="direct"))

    def test_leaf_key_includes_all_three_dimensions(self):
        for column in similarity.SEGMENT_COLUMNS:
            with self.subTest(column=column):
                frame = self.segment_frame()
                frame["channel"] = "direct"
                frame.loc[2:, column] = "other"
                selected = similarity.select_daily_series(frame, "A", "IP", "direct")
                np.testing.assert_array_equal(selected.values, [10, 20])

    def test_candidate_only_distances_match_full_evaluation(self):
        transformed = np.random.default_rng(5).normal(size=(5, 30))
        eligible = np.array([True, True, False, False, False])
        for method in ("correlation", "euclidean", "dtw_0.15"):
            with self.subTest(method=method):
                full = similarity._pairwise_distances(transformed, 4, method)
                limited = similarity._pairwise_distances(transformed, 4, method, eligible)
                np.testing.assert_allclose(limited[eligible], full[eligible])
                self.assertTrue(np.isinf(limited[~eligible]).all())

    def test_duplicate_and_missing_segment_days_are_rejected(self):
        frame = self.segment_frame().iloc[:2].copy()
        duplicate = pd.concat([frame, frame.iloc[:1]])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            similarity.select_daily_series(duplicate)
        frame.loc[1, "date"] = pd.Timestamp("2026-01-03")
        with self.assertRaisesRegex(ValueError, "missing dates"):
            similarity.select_daily_series(frame)

    def test_partial_filter_dispatches_separate_segments(self):
        seen = []

        def fake_job(payload):
            _, segment = payload
            seen.append(segment)
            return dict(record=segment, matches=[segment], metadata=segment)

        with TemporaryDirectory() as directory:
            with patch.object(similarity, "df_subscribers", self.segment_frame()), patch.object(similarity, "_segment_job", side_effect=fake_job):
                similarity.main(["--service", "A", "--output-dir", directory])
            manifest = json.loads((Path(directory) / "segment_manifest.json").read_text())
            self.assertEqual(manifest["segment_count"], 2)
            self.assertEqual({item["channel"] for item in seen}, {"direct", "partner"})

    def test_constant_preprocessing_is_finite(self):
        for name in (*similarity.DEFAULT_PREPROCESSORS, "relative_first", "smooth3_zscore", "smooth14_zscore"):
            with self.subTest(name=name):
                self.assertTrue(np.isfinite(similarity.preprocess_window(np.ones(30), name)).all())

    def test_dtw_identity_and_no_warping(self):
        values = np.array([1., 2., 3., 2.])
        self.assertEqual(similarity.dtw_distance(values, values, 1), 0)
        self.assertAlmostEqual(similarity.dtw_distance(values, values + 1, 0), 1)

    def test_equal_distances_receive_equal_ranks(self):
        result = similarity._rank_normalize(np.array([1., 1., 3.]), np.ones(3, dtype=bool))
        np.testing.assert_allclose(result, [.25, .25, 1.])

    def test_historical_gap_and_zero_gap_exclude_overlap(self):
        series = pd.Series(np.arange(200.), index=pd.date_range("2020-01-01", periods=200))
        windows = similarity.make_windows(series, 30)
        for gap in (0, 15):
            eligible = similarity._eligible_mask(windows, 120, gap)
            self.assertTrue(np.all(windows.ends[eligible] <= 120 - max(1, gap)))

    def test_cnn_training_excludes_future_windows(self):
        try:
            import torch  # noqa: F401
        except ImportError:
            self.skipTest("optional PyTorch is not installed")
        values = np.random.default_rng(2).normal(size=(12, 30))
        historical = np.arange(12) < 7
        first = similarity._siamese_cnn_distances(values, 8, historical)
        values[9:] += 100
        second = similarity._siamese_cnn_distances(values, 8, historical)
        np.testing.assert_allclose(first[:9], second[:9], atol=1e-6)


if __name__ == "__main__":
    unittest.main()
