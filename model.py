"""STEP 3 — Prediction agent.

Baseline probability = de-vigged sportsbook consensus. Every scan logs a feature
snapshot per market; once enough games settle, `train` fits an XGBoost classifier
with grouped cross-validation (by game, so the two sides of a game never leak across
folds). The model is only switched ON if its out-of-fold log loss beats the consensus
baseline. When on, a trade must show edge under BOTH the model and the consensus
(the confidence gate).
"""
import json
import os

from common import now_utc
from scanner import FEATURES


def vec(f):
    return [float(f.get(k, 0) or 0) for k in FEATURES]


def logit(p):
    import numpy as np
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def train(cfg, store, verbose=True):
    rows = store.labeled_snapshots()
    meta = {"active": False, "n": len(rows), "trained_ts": now_utc().isoformat()}
    if len(rows) < cfg["model_min_samples"]:
        meta["reason"] = f"need {cfg['model_min_samples']} labeled samples, have {len(rows)}"
        _save_meta(cfg, meta)
        if verbose:
            print("Model not trained:", meta["reason"])
        return meta
    import numpy as np
    import xgboost as xgb
    from sklearn.metrics import log_loss
    from sklearn.model_selection import GroupKFold

    X = np.array([vec(json.loads(r["features"])) for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    groups = np.array([r["event_ticker"] for r in rows])
    base = np.clip(np.array([r["consensus"] for r in rows]), 1e-4, 1 - 1e-4)

    # Boost FROM the sportsbook consensus: base_margin = logit(consensus), so trees only
    # learn corrections to the market prior instead of re-deriving it from scratch.
    margin = logit(base)

    def make():
        return xgb.XGBClassifier(n_estimators=150, max_depth=2, learning_rate=0.05,
                                 subsample=0.8, colsample_bytree=0.8, min_child_weight=10,
                                 reg_lambda=5.0, eval_metric="logloss")

    oof = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        clf = make()
        clf.fit(X[tr], y[tr], base_margin=margin[tr])
        oof[te] = clf.predict_proba(X[te], base_margin=margin[te])[:, 1]
    m_ll = float(log_loss(y, np.clip(oof, 1e-4, 1 - 1e-4), labels=[0, 1]))
    b_ll = float(log_loss(y, base, labels=[0, 1]))
    active = m_ll < b_ll - cfg["model_min_improvement"]
    final = make()
    final.fit(X, y, base_margin=margin)
    final.save_model(cfg["model_path"])
    meta.update(active=bool(active), model_logloss=m_ll, baseline_logloss=b_ll)
    if not active:
        meta["reason"] = "model did not beat sportsbook consensus out-of-sample"
    _save_meta(cfg, meta)
    if verbose:
        print(f"Trained on {len(y)} samples | model logloss {m_ll:.4f} vs consensus {b_ll:.4f} "
              f"-> {'ACTIVE' if active else 'inactive (using consensus)'}")
    return meta


def _save_meta(cfg, meta):
    with open(cfg["model_meta_path"], "w") as f:
        json.dump(meta, f, indent=2)


def load_meta(cfg):
    if os.path.exists(cfg["model_meta_path"]):
        with open(cfg["model_meta_path"]) as f:
            return json.load(f)
    return {"active": False, "n": 0}


class Predictor:
    def __init__(self, cfg):
        self.meta = load_meta(cfg)
        self.active = False
        self.clf = None
        if self.meta.get("active") and os.path.exists(cfg["model_path"]):
            import xgboost as xgb
            self.clf = xgb.XGBClassifier()
            self.clf.load_model(cfg["model_path"])
            self.active = True

    def predict(self, features):
        if not self.active:
            return None
        import numpy as np
        return float(self.clf.predict_proba(np.array([vec(features)]),
                                            base_margin=logit([features["fair"]]))[0, 1])


def maybe_retrain(cfg, store):
    n = len(store.labeled_snapshots())
    meta = load_meta(cfg)
    if n >= cfg["model_min_samples"] and n - meta.get("n", 0) >= cfg["retrain_every"]:
        return train(cfg, store)
    return None
