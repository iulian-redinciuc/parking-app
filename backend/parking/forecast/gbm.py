"""A small gradient-boosted regression-tree model in numpy (forecast.md §4).

Squared loss, shallow trees grown on pre-binned features (at most `bins` thresholds per
feature, so a split search is one `bincount` per feature), shrinkage and row subsampling.
A missing value (NaN) always goes right, in training and in prediction. The model is plain
lists (`to_dict` / `from_dict`), stored as JSON: nothing is unpickled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LEAF = -1


@dataclass(frozen=True)
class Tree:
    feature: list[int]  # LEAF for a leaf
    threshold: list[float]  # rows with x <= threshold go left
    left: list[int]
    right: list[int]
    value: list[float]

    def predict(self, x: np.ndarray) -> np.ndarray:
        feature, threshold = np.array(self.feature), np.array(self.threshold)
        left, right = np.array(self.left), np.array(self.right)
        node = np.zeros(len(x), dtype=np.int64)
        rows = np.arange(len(x))
        while True:
            f = feature[node]
            inner = f != LEAF
            if not inner.any():
                return np.array(self.value)[node]
            go_left = x[rows, np.where(inner, f, 0)] <= threshold[node]
            node = np.where(inner, np.where(go_left, left[node], right[node]), node)


@dataclass(frozen=True)
class Gbm:
    base: float
    lr: float
    trees: list[Tree] = field(default_factory=list)

    def predict(self, x: np.ndarray) -> np.ndarray:
        out = np.full(len(x), self.base)
        for tree in self.trees:
            out += self.lr * tree.predict(x)
        return out

    def used_features(self) -> set[int]:
        return {f for tree in self.trees for f in tree.feature if f != LEAF}

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "lr": self.lr,
            "trees": [
                {"f": t.feature, "t": t.threshold, "l": t.left, "r": t.right, "v": t.value}
                for t in self.trees
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Gbm:
        trees = [Tree(t["f"], t["t"], t["l"], t["r"], t["v"]) for t in data["trees"]]
        return cls(float(data["base"]), float(data["lr"]), trees)


def thresholds(column: np.ndarray, bins: int) -> np.ndarray:
    """Up to `bins` split points of a feature: its distinct values, or their quantiles."""
    values = np.unique(column[np.isfinite(column)])
    if len(values) <= bins + 1:
        return values[:-1]
    return np.unique(np.quantile(values, np.linspace(0, 1, bins + 2)[1:-1]))


def _grow(binned, cuts, grad, rows, depth, min_leaf, l2, tree: Tree) -> int:
    """Add the node for `rows` (and its children) to `tree`; returns its index."""
    node = len(tree.feature)
    total, n = grad[rows].sum(), len(rows)
    tree.feature.append(LEAF)
    tree.threshold.append(0.0)
    tree.left.append(LEAF)
    tree.right.append(LEAF)
    tree.value.append(float(total / (n + l2)))
    if depth == 0 or n < 2 * min_leaf:
        return node
    best_gain, best = 1e-9, None
    for j, cut in enumerate(cuts):
        k = len(cut)
        if k == 0:
            continue
        b = binned[rows, j]
        count = np.bincount(b, minlength=k + 1)[:k].cumsum()
        left = np.bincount(b, weights=grad[rows], minlength=k + 1)[:k].cumsum()
        ok = (count >= min_leaf) & (n - count >= min_leaf)
        if not ok.any():
            continue
        gain = left**2 / (count + l2) + (total - left) ** 2 / (n - count + l2) - total**2 / (n + l2)
        i = int(np.argmax(np.where(ok, gain, -np.inf)))
        if gain[i] > best_gain:
            best_gain, best = float(gain[i]), (j, i)
    if best is None:
        return node
    j, i = best
    goes_left = binned[rows, j] <= i
    tree.feature[node] = j
    tree.threshold[node] = float(cuts[j][i])
    tree.left[node] = _grow(binned, cuts, grad, rows[goes_left], depth - 1, min_leaf, l2, tree)
    tree.right[node] = _grow(binned, cuts, grad, rows[~goes_left], depth - 1, min_leaf, l2, tree)
    return node


def fit(
    x: np.ndarray,
    y: np.ndarray,
    *,
    rounds: int = 150,
    depth: int = 3,
    lr: float = 0.05,
    min_leaf: int = 24,
    bins: int = 32,
    subsample: float = 0.8,
    l2: float = 1.0,
    seed: int = 0,
) -> Gbm:
    """Fit `y` from the feature matrix `x` (rows × features, NaN = missing)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    cuts = [thresholds(x[:, j], bins) for j in range(x.shape[1])]
    # bin b holds the values <= cuts[b]; NaN lands past the last cut, so it never goes left
    binned = np.column_stack(
        [np.searchsorted(cut, x[:, j], side="left") for j, cut in enumerate(cuts)]
    ).astype(np.int64)
    rng = np.random.default_rng(seed)
    base = float(y.mean())
    pred = np.full(len(y), base)
    trees: list[Tree] = []
    take = max(int(len(y) * subsample), min(len(y), 2 * min_leaf))
    for _ in range(rounds):
        rows = np.sort(rng.choice(len(y), size=take, replace=False))
        tree = Tree([], [], [], [], [])
        _grow(binned, cuts, y - pred, rows, depth, min_leaf, l2, tree)
        if len(tree.feature) == 1:  # nothing left to split on
            break
        trees.append(tree)
        pred += lr * tree.predict(x)
    return Gbm(base, lr, trees)
