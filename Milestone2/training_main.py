import tempfile
import time
import comet_ml
import datetime
import hydra
import os
from pathlib import Path
import loguru
from matplotlib import pyplot as plt
from omegaconf import DictConfig, OmegaConf
import pandas as pd
from rich import print
from comet_ml import Artifact, Experiment
from joblib import dump, load

from utils.comet_ml import log_data_splits_to_comet
from utils.misc import init_logger, verify_dotenv_file
from utils.model import train_classifier_model
from utils.data import init_data_for_isgoal_classification_experiment


verify_dotenv_file(Path(__file__).parent.parent)


def run_experiment(cfg: DictConfig, logger) -> None:

    MODEL_CONFIG = cfg.model
    DATA_PIPELINE_CONFIG = cfg.data_pipeline
    print(dict(cfg))

    MODEL_TYPE = MODEL_CONFIG.model_type

    now_date = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    OUTPUT_DIR = (
        Path(os.getenv("TRAINING_ARTIFACTS_PATH"))
        / MODEL_TYPE 
        / now_date
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    COMET_EXPERIMENT = Experiment(
        project_name=f'{MODEL_CONFIG.model_type}_single_training',
        workspace='nhl-project',
        experiment_key=f"{now_date.replace('_','')}{'distAngle' if cfg.SUBSET_TO_ANGLE_DIST else 'all'}Feat".zfill(33),
    )

    COMET_EXPERIMENT.log_parameters(dict(cfg), prefix='HYDRA_')

    # =================================Prepare Data=================================
    
    PATH_RAW_CSV = Path(os.getenv("DATA_FOLDER"))/ cfg.data_pipeline.raw_train_data_path
    FEATURE_ENG_VERSION = cfg.data_pipeline.feature_engineering_version
    TRAIN_TEST_SPLIT_CONDITION = cfg.data_pipeline.predicate_train_test_split
    LOAD_FROM_EXISTING_FEATURE_ENG_DATA = cfg.data_pipeline.load_engineered_data_from
    DATA_PREPROCESSOR_OBJ, DATA_FEATURE_ENG_OBJ = init_data_for_isgoal_classification_experiment(
        RAW_DATA_PATH = PATH_RAW_CSV,
        DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
        version = FEATURE_ENG_VERSION,
        comet_experiment_object = COMET_EXPERIMENT,
        TRAIN_TEST_PREDICATE_SPLITTING = TRAIN_TEST_SPLIT_CONDITION,
        load_engineered_data_from = LOAD_FROM_EXISTING_FEATURE_ENG_DATA,
        logger = logger,
    )  

    import pdb; pdb.set_trace()
    
    # =================================Task-Specific (Classification Prob goal) data pre-proc=================================
    # ================== eventType only SHOT | GOAL
    logger.info('SUBSETTING data TO SHOT | GOAL eventType')
    logger.info(f"\n Before subsetting : {DATA_PREPROCESSOR_OBJ.X_train.shape} rows in train set")
    logger.info(f"\n Before subsetting : {DATA_PREPROCESSOR_OBJ.X_test.shape} rows in test set")
    idx_train = DATA_PREPROCESSOR_OBJ.X_train.query("eventType == 'SHOT' | eventType == 'GOAL'").index
    idx_test = DATA_PREPROCESSOR_OBJ.X_test.query("eventType == 'SHOT' | eventType == 'GOAL'").index
    DATA_PREPROCESSOR_OBJ.X_train = DATA_PREPROCESSOR_OBJ.X_train[DATA_PREPROCESSOR_OBJ.X_train.index.isin(idx_train)]
    DATA_PREPROCESSOR_OBJ.y_train = DATA_PREPROCESSOR_OBJ.y_train[DATA_PREPROCESSOR_OBJ.y_train.index.isin(idx_train)]
    DATA_PREPROCESSOR_OBJ.X_test = DATA_PREPROCESSOR_OBJ.X_test[DATA_PREPROCESSOR_OBJ.X_test.index.isin(idx_test)]
    DATA_PREPROCESSOR_OBJ.y_test = DATA_PREPROCESSOR_OBJ.y_test[DATA_PREPROCESSOR_OBJ.y_test.index.isin(idx_test)]
    logger.info(f"\n After subsetting : {DATA_PREPROCESSOR_OBJ.X_train.shape} rows in train set")
    logger.info(f"\n After subsetting : {DATA_PREPROCESSOR_OBJ.X_test.shape} rows in test set")

    if cfg.SUBSET_TO_ANGLE_DIST:
        logger.info('SUBSETTING data TO ANGLE AND DISTANCE')
        DATA_PREPROCESSOR_OBJ.X_train = DATA_PREPROCESSOR_OBJ.X_train[['distanceToGoal', 'angleToGoal']]
        DATA_PREPROCESSOR_OBJ.X_test = DATA_PREPROCESSOR_OBJ.X_test[['distanceToGoal', 'angleToGoal']]

    # =================================Train Model=================================

    # for i, (train_index, val_index) in enumerate(CV_index_generator):
    #     print(f"Fold {i}:")
    CV_index_generator  = DATA_PREPROCESSOR_OBJ._split_data()
    train_index, val_index = CV_index_generator.__next__()


    STATS_EXPERIMENT = {}
    PATH_GRAPHS_TO_LOG_TO_COMET = []

    (OUTPUT_DIR / 'val').mkdir(parents=True, exist_ok=True)

    X_train = DATA_PREPROCESSOR_OBJ.X_train
    y_train = DATA_PREPROCESSOR_OBJ.y_train
    X_test = DATA_PREPROCESSOR_OBJ.X_test
    y_test = DATA_PREPROCESSOR_OBJ.y_test

    for features_to_include in [["distanceToGoal"], ["angleToGoal"], ["distanceToGoal", "angleToGoal"]]:

        import pdb; pdb.set_trace()
        X_train = X_train[X_train.index.isin(train_index)][features_to_include]
        y_train = y_train[y_train.index.isin(train_index)]
        X_val = X_train[X_train.index.isin(val_index)][features_to_include]
        y_val = y_train[y_train.index.isin(val_index)]
        X_test = X_test[features_to_include]


        logger.info(f"Training model {MODEL_CONFIG.model_type} with features : {features_to_include}")

        KEY_DICT_STAT = '+'.join(features_to_include)

        print('Train ',X_train.describe())
        print('Val ', X_val.describe())

        logger.info("Logging to comet_ml the data splits")
        log_data_splits_to_comet(
            COMET_EXPERIMENT = COMET_EXPERIMENT,
            X_train = X_train,
            y_train = y_train,
            X_val = X_val,
            y_val = y_val,
            X_test = X_test,
            y_test = y_test,
            title = '+'.join(features_to_include)
        )

        logger.info("Training model")
        TRAINED_CLASSIFIER, training_duration = train_classifier_model(
            X_train = X_train,
            y_train = y_train,
            X_val = X_val,
            y_val = y_val,
            DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
            MODEL_CONFIG = MODEL_CONFIG,
            logger = logger,
        )

        with tempfile.TemporaryFile() as fp:
            if MODEL_CONFIG.model_type == "LogisticRegression":
                dump(TRAINED_CLASSIFIER, fp.name)
            if MODEL_CONFIG.model_type == "XGBoostClassifier":
                TRAINED_CLASSIFIER.save_model(fp.name+'.json')

            COMET_EXPERIMENT.log_model(
                name=f'{MODEL_TYPE}_fit_on_{"+".join(features_to_include)}',
                file_or_folder=fp.name,
                metadata=OmegaConf.to_container(MODEL_CONFIG),
            )
        
        # evaluate accuracy on val set
        y_val_preds = TRAINED_CLASSIFIER.predict(X_val)
        y_val_proba_preds = TRAINED_CLASSIFIER.predict_proba(X_val)
        
        logger.info("Assessing model performance on val split")
        from utils.metrics import assess_classifier_perf
        VAL_METRICS = assess_classifier_perf(
            y=y_val,
            y_pred=y_val_preds,
            title_model=f'{MODEL_TYPE} trained on {"+".join(features_to_include)}',
        )

        COMET_EXPERIMENT.log_confusion_matrix(
            labels=['NO_GOAL','GOAL'],
            matrix=VAL_METRICS['conf_matrix'],
            file_name=f"confusion_matrix_val_set_{KEY_DICT_STAT}.png",
        )

        logger.info("Assessing model performance on test split")
        TEST_METRICS = assess_classifier_perf(
            y=y_test,
            y_pred=TRAINED_CLASSIFIER.predict(X_test),
            title_model=f'{MODEL_TYPE} trained on {"+".join(features_to_include)}',
        )

        COMET_EXPERIMENT.log_confusion_matrix(
            labels=['NO_GOAL','GOAL'],
            matrix=TEST_METRICS['conf_matrix'],
            file_name=f"confusion_matrix_test_set_{KEY_DICT_STAT}.png",
        )
        

        # DONT MODIFY NAME OF THE KEYS IN THIS DICT ---> hard-coded IN OTHER FILES
        STATS_EXPERIMENT[KEY_DICT_STAT] = {
            'model': TRAINED_CLASSIFIER,
            'training_time': training_duration,
            'val':{
                'data' : (X_val, y_val),
                'proba_preds' : y_val_proba_preds,
                'preds' : y_val_preds,
                'performance' : VAL_METRICS
            },
            'test':{
                'data' : (X_test, y_test),
                'proba_preds' : TRAINED_CLASSIFIER.predict_proba(X_test),
                'preds' : TRAINED_CLASSIFIER.predict(X_test),
                'performance' : TEST_METRICS
            }
        }

        if MODEL_CONFIG.model_type == "XGBoostClassifier":
            STATS_EXPERIMENT[KEY_DICT_STAT]['val']['results'] = TRAINED_CLASSIFIER.evals_result()

            from utils.plot import plot_XGBOOST_feat_importance, plot_XGBOOST_losses
            
            model_title = f'{MODEL_CONFIG.model_type}_{KEY_DICT_STAT}'

            logger.info("Plotting XGBOOST losses validation for model {model_title}")
            PATH_GRAPHS_TO_LOG_TO_COMET.extend(plot_XGBOOST_losses(
                OUTPUT_DIR = OUTPUT_DIR/ 'val',
                results=STATS_EXPERIMENT[KEY_DICT_STAT]['val']['results'],
                title=model_title,
            ))

            logger.info("Plotting XGBOOST feature importance for model {model_title}")
            PATH_GRAPHS_TO_LOG_TO_COMET.extend(plot_XGBOOST_feat_importance(
                OUTPUT_DIR = OUTPUT_DIR/ 'val',
                COMET_EXPERIMENT = COMET_EXPERIMENT,
                logger = logger,
                classifier = TRAINED_CLASSIFIER,
                X_train_samples=X_train.sample(1000, random_state=DATA_PIPELINE_CONFIG.seed),
            ))

    # _______________________ Training and Validation finished

    # Saving MODELS & EXPERIMENTS to disk and logging to comet_ml
    import pickle
    PATH_EXPERIMENT_ASSET = OUTPUT_DIR / "STATS_EXPERIMENT.pkl"
    logger.info(f"Saving models and experiment to disk at {PATH_EXPERIMENT_ASSET}")
    with open(PATH_EXPERIMENT_ASSET, "wb") as f:
        pickle.dump(STATS_EXPERIMENT, f)
    COMET_EXPERIMENT.log_asset(str(PATH_EXPERIMENT_ASSET), "STATS_EXPERIMENT.pkl")

    # PLOTTING GRAPHS : LOSSES, ROC, RATIO-GOAL, CUMUL-GOAL, CALIBRATION CURVES
    OUTPUT_DIR = OUTPUT_DIR / 'val'
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    from utils.plot import plotPerfModel
    logger.info(f"Plotting prob-oriented performance curves at {OUTPUT_DIR} on val set")
    PATH_GRAPHS_TO_LOG_TO_COMET.extend(plotPerfModel(
        predictionsTest = {
            f'{MODEL_TYPE} trained on {k}' : d['val']['proba_preds'] for k,d in STATS_EXPERIMENT.items()
        },
        yTest = y_val.to_numpy(),
        outputDir = OUTPUT_DIR,
        rocCurve = True,
        ratioGoalPercentileCurve = True,
        proportionGoalPercentileCurve = True,
        calibrationCurve = True,
    ))

    logger.info('List of plots that will be logged to comet_ml')
    print(PATH_GRAPHS_TO_LOG_TO_COMET)

    for p in PATH_GRAPHS_TO_LOG_TO_COMET:
        try:
            COMET_EXPERIMENT.log_asset(str(p), p.stem)
        except comet_ml.exceptions.APIError as e:
            logger.warning(f"Error while logging artifact {p} : {e}")



@hydra.main(
    version_base=None, config_path=os.getenv("YAML_CONF_DIR"), config_name="training_main_conf"
)
def main(cfg : DictConfig) -> None:
    logger = init_logger("training_main.log")
    run_experiment(cfg, logger)


if __name__ == "__main__":
    main()
