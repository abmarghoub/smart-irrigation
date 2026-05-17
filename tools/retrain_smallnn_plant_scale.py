"""
Réentraîne SmallNN (classif + régresseur) avec une cible volume en litres/cycle
réaliste pour plante/pot, dérivée du CSV (irrigation_amount_m3 = litres réels).

Cible régression (évite la saturation au plafond 3 L de l’ancienne formule linéaire) :
  y_reg = 0 si pas d’irrigation, sinon clip(sqrt(litres)/22, 0.12, 1.8) litres.

Usage (depuis la racine du projet):
  python tools/retrain_smallnn_plant_scale.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder


def repo_root() -> Path:
    p = Path(__file__).resolve().parent.parent
    if (p / "Smart_irrigation_dataset.csv").is_file():
        return p
    raise FileNotFoundError("Smart_irrigation_dataset.csv introuvable.")


def mlp_weights_to_c_header(mlp, path: Path, name_prefix: str) -> None:
    lines = [
        f"/* {name_prefix}: sklearn MLP — couches relu sauf sortie (identite ou logistique selon le modele) */",
        "/* Ordre des poids: W0, b0, W1, b1 ... (row-major C) */",
        "",
    ]
    for i, (w, b) in enumerate(zip(mlp.coefs_, mlp.intercepts_)):
        w = np.asarray(w, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        flat_w = w.ravel(order="C")
        lines.append(f"static const float {name_prefix}_W{i}[{flat_w.size}] = {{")
        lines.append("  " + ", ".join(f"{x:.8f}f" for x in flat_w))
        lines.append("};")
        lines.append(f"static const float {name_prefix}_B{i}[{b.size}] = {{")
        lines.append("  " + ", ".join(f"{x:.8f}f" for x in b))
        lines.append("};")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_preprocess_params_h(mm: MinMaxScaler, out: Path) -> None:
    lines = [
        "#ifndef PREPROCESS_PARAMS_H",
        "#define PREPROCESS_PARAMS_H",
        "",
        "#define IRR_INPUT_DIM 13",
        "#define IRR_NUM_FEATURES 5",
        "#define IRR_CROP_CLASSES 4",
        "#define IRR_SOIL_CLASSES 4",
        "",
        "static const float IRR_NUM_SCALE[IRR_NUM_FEATURES] = {",
    ]
    scales = ",\n  ".join(f"{float(x):.17f}f" for x in mm.scale_)
    lines.append("  " + scales)
    lines.append("};")
    lines.append("")
    lines.append("static const float IRR_NUM_MIN[IRR_NUM_FEATURES] = {")
    mins = ",\n  ".join(f"{float(x):.17f}f" for x in mm.min_)
    lines.append("  " + mins)
    lines.append("};")
    lines.append("")
    lines.append("#endif")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    root = repo_root()
    df = pd.read_csv(root / "Smart_irrigation_dataset.csv")

    SENSOR_COLS = ["soil_moisture_%", "temperature_C", "humidity_%", "rainfall_mm"]
    USER_COLS = ["crop_name", "soil_type", "crop_age_days"]
    FEATURE_COLS = SENSOR_COLS + USER_COLS

    missing = [c for c in FEATURE_COLS + ["irrigate", "irrigation_amount_m3"] if c not in df.columns]
    if missing:
        raise ValueError(f"Colonnes manquantes: {missing}")

    X = df[FEATURE_COLS].copy()
    y_class = df["irrigate"]
    _liters = df["irrigation_amount_m3"].astype(float)
    # Compression racine: les gros volumes terrain ne saturent plus toutes au même max.
    y_reg = np.where(
        _liters <= 0.0,
        0.0,
        np.clip(np.sqrt(_liters) / 22.0, 0.12, 1.8),
    )

    num_cols = SENSOR_COLS + ["crop_age_days"]
    cat_cols = ["crop_name", "soil_type"]

    preprocessor_tpl = ColumnTransformer(
        transformers=[
            (
                "num",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", MinMaxScaler()),
                    ]
                ),
                num_cols,
            ),
            (
                "cat",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                cat_cols,
            ),
        ]
    )

    # Un seul split stratifié + préprocesseurs clonés (évite partage d'état entre les deux pipelines)
    X_train, X_test, y_train_c, y_test_c, y_train_r, y_test_r = train_test_split(
        X, y_class, y_reg, test_size=0.2, random_state=42, stratify=y_class
    )

    prep_c = clone(preprocessor_tpl)
    prep_r = clone(preprocessor_tpl)

    clf_pipeline = Pipeline(
        steps=[
            ("preprocess", prep_c),
            (
                "model",
                MLPClassifier(
                    hidden_layer_sizes=(8,),
                    activation="relu",
                    solver="adam",
                    alpha=1e-4,
                    learning_rate_init=1e-3,
                    max_iter=500,
                    random_state=42,
                    early_stopping=True,
                    validation_fraction=0.1,
                    n_iter_no_change=20,
                ),
            ),
        ]
    )
    reg_pipeline = Pipeline(
        steps=[
            ("preprocess", prep_r),
            (
                "model",
                MLPRegressor(
                    hidden_layer_sizes=(8,),
                    activation="relu",
                    solver="adam",
                    alpha=1e-4,
                    learning_rate_init=1e-3,
                    max_iter=500,
                    random_state=42,
                    early_stopping=True,
                    validation_fraction=0.1,
                    n_iter_no_change=20,
                ),
            ),
        ]
    )

    clf_pipeline.fit(X_train, y_train_c)
    y_pred_c = clf_pipeline.predict(X_test)
    acc = accuracy_score(y_test_c, y_pred_c)
    print(f"Accuracy (SmallNN - classification): {acc:.4f}")
    print(classification_report(y_test_c, y_pred_c))

    reg_pipeline.fit(X_train, y_train_r)
    y_pred_r = reg_pipeline.predict(X_test)
    mae = mean_absolute_error(y_test_r, y_pred_r)
    rmse = float(np.sqrt(mean_squared_error(y_test_r, y_pred_r)))
    r2 = r2_score(y_test_r, y_pred_r)
    print(f"MAE / RMSE / R2 (régression litres ~0.12-1.8): {mae:.4f} / {rmse:.4f} / {r2:.4f}")

    out_dir = root / "smallNN" / "models"
    out_dir.mkdir(parents=True, exist_ok=True)

    joblib.dump(clf_pipeline, out_dir / "smallnn_classifier.joblib")
    joblib.dump(reg_pipeline, out_dir / "smallnn_regressor.joblib")
    joblib.dump(clf_pipeline.named_steps["preprocess"], out_dir / "smallnn_preprocessor.joblib")

    pre = clf_pipeline.named_steps["preprocess"]
    mm: MinMaxScaler = pre.named_transformers_["num"].named_steps["scaler"]
    ohe: OneHotEncoder = pre.named_transformers_["cat"].named_steps["onehot"]
    scaler_meta = {
        "numeric_feature_names": list(pre.transformers_[0][2]),
        "data_min": mm.data_min_.tolist(),
        "data_max": mm.data_max_.tolist(),
        "scale": mm.scale_.tolist(),
        "min": mm.min_.tolist(),
        "onehot_feature_names_in": list(pre.transformers_[1][2]),
        "onehot_categories": [np.asarray(c).tolist() for c in ohe.categories_],
        "regression_target_note": "y_reg = 0 si L<=0 else clip(sqrt(L)/22, 0.12, 1.8) — MLP sortie litres/cycle",
    }
    (out_dir / "scaler_encoder_params.json").write_text(json.dumps(scaler_meta, indent=2), encoding="utf-8")

    mlp_weights_to_c_header(
        clf_pipeline.named_steps["model"], out_dir / "smallnn_classifier_weights.h", "SMALLNN_CLF"
    )
    mlp_weights_to_c_header(
        reg_pipeline.named_steps["model"], out_dir / "smallnn_regressor_weights.h", "SMALLNN_REG"
    )

    wx = root / "esp32_weather_station"
    for name in ("smallnn_classifier_weights.h", "smallnn_regressor_weights.h"):
        shutil.copy2(out_dir / name, wx / name)
    write_preprocess_params_h(mm, wx / "preprocess_params.h")
    print("Copié vers esp32_weather_station:", wx.resolve())


if __name__ == "__main__":
    main()
