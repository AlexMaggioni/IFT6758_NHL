import pickle
import tempfile
import comet_ml
from joblib import dump
from omegaconf import OmegaConf
import pandas as pd
from sklearn.base import BaseEstimator
from utils.comet_ml import log_data_splits_to_comet
from utils.model import create_model, train_classifier_model


def train_and_eval(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    logger,
    COMET_EXPERIMENT: comet_ml.Experiment,
    title: str,
    DATA_PIPELINE_CONFIG,
    MODEL_CONFIG,
    OUTPUT_DIR,
    USE_SAMPLE_WEIGHTS : bool,
    JUST_EVALUATE,
    RESUME_FROM_MODEL_CHECKPOINT,
    log_data_splits : bool = True,
    log_model_to_comet : bool = True,
    X_test: pd.DataFrame = None,
    y_test: pd.Series = None,
    gameType_testSet = None,
):
    STATS_EXPERIMENT = {title: {}}

    print("Train ", X_train.describe())
    print("Val ", X_val.describe())

    if log_data_splits:
        logger.info("Logging to comet_ml the data splits")
        log_data_splits_to_comet(
            COMET_EXPERIMENT=COMET_EXPERIMENT,
            X_train=X_train,
            y_train=y_train,
            X_val=X_val,
            y_val=y_val,
            title=title,
            logger=logger,
        )

    logger.info("Creating model")
    TRAINED_CLASSIFIER = create_model(
        MODEL_CONFIG = MODEL_CONFIG,
        DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
        logger = logger,
        RESUME_FROM_MODEL_CHECKPOINT = RESUME_FROM_MODEL_CHECKPOINT,
    )

    if JUST_EVALUATE is False:
        logger.info("Training model")
        TRAINED_CLASSIFIER, training_duration = train_classifier_model(
            X_train=X_train,
            y_train=y_train,
            X_val=X_val,
            y_val=y_val,
            MODEL_CONFIG=MODEL_CONFIG,
            CLS_MODEL=TRAINED_CLASSIFIER,
            logger=logger,
            USE_SAMPLE_WEIGHTS=USE_SAMPLE_WEIGHTS,
        )

    if log_model_to_comet:
        logger.info("Logging to comet_ml the model")
        with tempfile.NamedTemporaryFile() as fp:
            if isinstance(TRAINED_CLASSIFIER, BaseEstimator):
                logger.info(f"Saving Scikit-Learn model {MODEL_CONFIG.model_type} to disk ")
                dump(TRAINED_CLASSIFIER, fp.name)

            if MODEL_CONFIG.model_type == "XGBoostClassifier":
                TRAINED_CLASSIFIER.save_model(fp.name + ".json")

            COMET_EXPERIMENT.log_model(
                name=title,
                file_or_folder=fp.name,
                metadata=OmegaConf.to_container(MODEL_CONFIG),
            )

    # evaluate accuracy on val set
    y_val_preds = TRAINED_CLASSIFIER.predict(X_val)
    y_val_proba_preds = TRAINED_CLASSIFIER.predict_proba(X_val)

    logger.info("\t Assessing model performance on val split")
    from utils.metrics import assess_classifier_perf

    VAL_METRICS = assess_classifier_perf(
        y=y_val,
        y_pred=y_val_preds,
        title_model=title,
        info="val",
        logger=logger,
        COMET_EXPERIMENT=COMET_EXPERIMENT,
    )

    if X_test is not None and y_test is not None:
        logger.info("\t Assessing model performance on test split")
        res_test = eval_on_test_set(
            X_test=X_test,
            y_test=y_test,
            classifier=TRAINED_CLASSIFIER,
            title_model=title,
            logger=logger,
            COMET_EXPERIMENT=COMET_EXPERIMENT,
            gameType_testSet=gameType_testSet,
        )
        STATS_EXPERIMENT[title].update(res_test)

    # DONT MODIFY NAME OF THE KEYS IN THIS DICT ---> hard-coded IN OTHER FILES
    STATS_EXPERIMENT[title].update(
        {
            "model": TRAINED_CLASSIFIER,
            "val": {
                "data": (X_val, y_val),
                "proba_preds": y_val_proba_preds,
                "preds": y_val_preds,
                "performance": VAL_METRICS,
            },
        }
    )

    if JUST_EVALUATE is False:
        STATS_EXPERIMENT[title]["training_time"] =  training_duration

    if MODEL_CONFIG.model_type == "XGBoostClassifier" and JUST_EVALUATE is False:
        STATS_EXPERIMENT[title]["val"]["results"] = TRAINED_CLASSIFIER.evals_result()

        from utils.plot import plot_XGBOOST_feat_importance, plot_XGBOOST_losses

        logger.info(f"\t Plotting XGBOOST losses validation for model {title}")
        plot_XGBOOST_losses(
            OUTPUT_DIR=OUTPUT_DIR / "val",
            results=STATS_EXPERIMENT[title]["val"]["results"],
            title=title,
            COMET_EXPERIMENT=COMET_EXPERIMENT,
        )

        if getattr(TRAINED_CLASSIFIER, "importance_type", None):
            logger.info(f"\t Plotting XGBOOST feature importance for model {title}")
            plot_XGBOOST_feat_importance(
                OUTPUT_DIR=OUTPUT_DIR / "val",
                COMET_EXPERIMENT=COMET_EXPERIMENT,
                logger=logger,
                classifier=TRAINED_CLASSIFIER,
                X_train_samples=X_train.sample(
                    1000, random_state=DATA_PIPELINE_CONFIG.seed
                ),
                info=title,
            )

    return STATS_EXPERIMENT


def eval_on_test_set(
    X_test,
    y_test,
    classifier,
    title_model,
    logger,
    COMET_EXPERIMENT,
    gameType_testSet,
):

    mask_idx_playoff = gameType_testSet == 'P'

    res = {'test':{}}

    logger.info("\t\t Evaluation des metriques sur les matchs des séries éliminatoires/playoff 2019/20")
    from utils.metrics import assess_classifier_perf
    X_test_playoff = X_test[mask_idx_playoff]
    y_test_playoff = y_test[mask_idx_playoff]
    logger.info(f"X_test_playoff shape {X_test_playoff.shape}")
    preds_playoff = classifier.predict(X_test_playoff)

    TEST_METRICS_PLAYOFFS = assess_classifier_perf(
        y=y_test_playoff,
        y_pred=preds_playoff,
        title_model=title_model,
        info = "test_playoffs",
        logger = logger,
        COMET_EXPERIMENT=COMET_EXPERIMENT,
    )

    res['test_playoffs']={            
        "data": (X_test_playoff, y_test_playoff),
        "proba_preds": classifier.predict_proba(X_test_playoff),
        "preds": preds_playoff,
        "performance": TEST_METRICS_PLAYOFFS,
    }

    logger.info("\t\t Evaluation des metriques sur l'ensemble de données intact de la saison régulière 2019/20")

    X_test_reg = X_test[~mask_idx_playoff]
    y_test_reg = y_test[~mask_idx_playoff]
    logger.info(f"X_test_reg shape {X_test_reg.shape}")
    preds_reg = classifier.predict(X_test_reg)

    TEST_METRICS_REG = assess_classifier_perf(
        y=y_test_reg,
        y_pred=preds_reg,
        title_model=title_model,
        info = "test_regularSeason",
        logger = logger,
        COMET_EXPERIMENT=COMET_EXPERIMENT,
    )

    res['test_regular_season']={
        "data": (X_test_reg, y_test_reg),
        "proba_preds": classifier.predict_proba(X_test_reg),
        "preds": preds_reg,
        "performance": TEST_METRICS_REG,
    }

    return res