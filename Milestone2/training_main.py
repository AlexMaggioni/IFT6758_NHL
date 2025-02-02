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
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.feature_selection import SelectFromModel


from utils.comet_ml import log_data_splits_to_comet
from utils.misc import init_logger, verify_dotenv_file
from utils.model import train_classifier_model
from utils.data import init_data_for_isgoal_classification_experiment
from utils.trainer import train_and_eval


verify_dotenv_file(Path(__file__).parent.parent)


def run_experiment(cfg: DictConfig, logger) -> None:

    MODEL_CONFIG = cfg.model
    DATA_PIPELINE_CONFIG = cfg.data_pipeline
    print(dict(cfg))

    MODEL_TYPE = MODEL_CONFIG.model_type

    PROJECT_NAME = f'train_{MODEL_CONFIG.model_type}' \
    + ('_baseline' if cfg.BASELINE_SUBSET_TO_ANGLE_DIST else '') \
    + ('_CV' if cfg.USE_CROSS_VALIDATION else '') \
    + ('_noTestSet' if cfg.holdout_test else '') \
    + ('_featSel' if DATA_PIPELINE_CONFIG.feature_selection_type is not None else '') \


    now_date = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    OUTPUT_DIR = (
        Path(os.getenv("TRAINING_ARTIFACTS_PATH"))
        / PROJECT_NAME 
        / now_date
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # =================================COMET ML=================================

    COMET_EXPERIMENT = Experiment(
        project_name=PROJECT_NAME,
        workspace='nhl-project',
    )
    EXP_NAME = f"{now_date.replace('_','')}"
    COMET_EXPERIMENT.set_name(EXP_NAME)
    COMET_EXPERIMENT.log_parameters(dict(cfg), prefix='HYDRA_')
    if cfg.COMET_EXPERIEMENT_TAGS is not None:
        COMET_EXPERIMENT.add_tags(OmegaConf.to_container(cfg.COMET_EXPERIEMENT_TAGS))


    # =================================Prepare Data=================================
    
    PATH_RAW_CSV = Path(os.getenv("DATA_FOLDER"))/ cfg.data_pipeline.raw_train_data_path
    FEATURE_ENG_VERSION = cfg.data_pipeline.feature_engineering_version
    TRAIN_TEST_SPLIT_CONDITION = cfg.data_pipeline.predicate_train_test_split
    LOAD_FROM_EXISTING_FEATURE_ENG_DATA = cfg.data_pipeline.load_engineered_data_from
    
    DATA_PREPROCESSOR_OBJ, DATA_FEATURE_ENG_OBJ = init_data_for_isgoal_classification_experiment(
        RAW_DATA_PATH = PATH_RAW_CSV,
        DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
        version = FEATURE_ENG_VERSION,
        TRAIN_TEST_PREDICATE_SPLITTING = TRAIN_TEST_SPLIT_CONDITION,
        load_engineered_data_from = LOAD_FROM_EXISTING_FEATURE_ENG_DATA,
        logger = logger,
    )  

    # =================================Task-Specific (Classification Prob goal) data pre-proc=================================
    # ================== eventType only SHOT | GOAL
    logger.info('SUBSETTING data TO SHOT | GOAL eventType')
    idx_train = DATA_PREPROCESSOR_OBJ.X_train.query("eventType == 'SHOT' | eventType == 'GOAL'").index
    idx_test = DATA_PREPROCESSOR_OBJ.X_test.query("eventType == 'SHOT' | eventType == 'GOAL'").index
    
    logger.info(f"\t Before subsetting : {DATA_PREPROCESSOR_OBJ.X_train.shape} rows in train set")
    DATA_PREPROCESSOR_OBJ.X_train = DATA_PREPROCESSOR_OBJ.X_train[DATA_PREPROCESSOR_OBJ.X_train.index.isin(idx_train)]
    DATA_PREPROCESSOR_OBJ.y_train = DATA_PREPROCESSOR_OBJ.y_train[DATA_PREPROCESSOR_OBJ.y_train.index.isin(idx_train)]
    logger.info(f"\t After subsetting : {DATA_PREPROCESSOR_OBJ.X_train.shape} rows in train set")
    
    logger.info(f"\t Before subsetting : {DATA_PREPROCESSOR_OBJ.X_test.shape} rows in test set")
    DATA_PREPROCESSOR_OBJ.X_test = DATA_PREPROCESSOR_OBJ.X_test[DATA_PREPROCESSOR_OBJ.X_test.index.isin(idx_test)]
    DATA_PREPROCESSOR_OBJ.y_test = DATA_PREPROCESSOR_OBJ.y_test[DATA_PREPROCESSOR_OBJ.y_test.index.isin(idx_test)]
    logger.info(f"\t After subsetting : {DATA_PREPROCESSOR_OBJ.X_test.shape} rows in test set")

    logger.info("DROPPING eventType column")
    DATA_PREPROCESSOR_OBJ.X_train = DATA_PREPROCESSOR_OBJ.X_train.drop(columns=['eventType'])
    DATA_PREPROCESSOR_OBJ.X_test = DATA_PREPROCESSOR_OBJ.X_test.drop(columns=['eventType'])

    # =================================Task-Specific (Classification Prob goal) data pre-proc=================================
    feature_selection_type = DATA_PIPELINE_CONFIG.feature_selection_type
    if feature_selection_type is not None:
        if feature_selection_type=='tree_based_feature_selection':

            logger.info('FEATURE SELECTION : tree_based_feature_selection fit on train set')

            clf = ExtraTreesClassifier(n_estimators=50)
            # import pdb;pdb.set_trace()
            clf = clf.fit(DATA_PREPROCESSOR_OBJ.X_train, DATA_PREPROCESSOR_OBJ.y_train.to_numpy().ravel())
            feat_imp = sorted(dict(zip(DATA_PREPROCESSOR_OBJ.X_train.columns,clf.feature_importances_)).items(),key=lambda x:x[1],reverse=True)
            logger.info(f"Feature importance : {feat_imp}")
            
            model = SelectFromModel(clf, prefit=True)

            DATA_PREPROCESSOR_OBJ.X_train = model.transform(DATA_PREPROCESSOR_OBJ.X_train)
            DATA_PREPROCESSOR_OBJ.X_test = model.transform(DATA_PREPROCESSOR_OBJ.X_test)

            sub_features_names = clf.feature_names_in_[model.get_support(True)]
            logger.info(f"Feature selection : {len(sub_features_names)} features selected : {sub_features_names}")

            DATA_PREPROCESSOR_OBJ.X_train = pd.DataFrame(DATA_PREPROCESSOR_OBJ.X_train, columns=sub_features_names)
            DATA_PREPROCESSOR_OBJ.X_test = pd.DataFrame(DATA_PREPROCESSOR_OBJ.X_test, columns=sub_features_names)

            fname = OUTPUT_DIR / f"feature_selection_{feature_selection_type}_model.pkl"
            with open(fname, "wb") as f:
                dump(model, f)
            COMET_EXPERIMENT.log_asset(
                str(fname), 
                fname.name,
                metadata={
                    'nb_features_selected' : len(sub_features_names),
                    'features_selected' : sub_features_names,
                    'feature_selection_type' : feature_selection_type,
                    'feature_importance_scores' : feat_imp,
                }
            )
        else :
            raise NotImplementedError(f"feature_selection_type {feature_selection_type} not implemented")


    # =================================Train Model=================================

    STATS_EXPERIMENT = {}

    (OUTPUT_DIR / 'val').mkdir(parents=True, exist_ok=True)

    if cfg.BASELINE_SUBSET_TO_ANGLE_DIST:
        for features_to_include in [["distanceToGoal"], ["angleToGoal"], ["distanceToGoal", "angleToGoal"]]:
            CV_index_generator  = DATA_PREPROCESSOR_OBJ._split_data()
            train_index, val_index = CV_index_generator.__next__()

            X_train = DATA_PREPROCESSOR_OBJ.X_train.iloc[train_index,:][features_to_include]
            y_train = DATA_PREPROCESSOR_OBJ.y_train.iloc[train_index,:]
            X_val = DATA_PREPROCESSOR_OBJ.X_train.iloc[val_index,:][features_to_include]
            y_val = DATA_PREPROCESSOR_OBJ.y_train.iloc[val_index,:]
            X_test = DATA_PREPROCESSOR_OBJ.X_test[features_to_include+['gameType']]
            y_test = DATA_PREPROCESSOR_OBJ.y_test

            logger.info(f"Training model {MODEL_CONFIG.model_type} with features : {features_to_include}")

            MODEL_KEY = f"{MODEL_TYPE}_{'+'.join(features_to_include)}"

            RES_EXP = train_and_eval(
                X_train = X_train,
                y_train = y_train,
                X_val = X_val,
                y_val = y_val,
                logger = logger,
                COMET_EXPERIMENT = COMET_EXPERIMENT,
                title = MODEL_KEY,
                DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
                MODEL_CONFIG = MODEL_CONFIG,
                OUTPUT_DIR = OUTPUT_DIR,
                X_test = None if cfg.holdout_test else X_test,
                y_test = None if cfg.holdout_test else y_test,
                USE_SAMPLE_WEIGHTS = cfg.USE_SAMPLE_WEIGHTS,
                RESUME_FROM_MODEL_CHECKPOINT = cfg.RESUME_FROM_MODEL_CHECKPOINT,
                JUST_EVALUATE=cfg.JUST_EVALUATE,
            )

            STATS_EXPERIMENT.update(RES_EXP)
    else :
        CV_index_generator  = DATA_PREPROCESSOR_OBJ._split_data()
        if cfg.USE_CROSS_VALIDATION: 
            GENERATOR_IDX = CV_index_generator
        else:
            CV_index_generator  = DATA_PREPROCESSOR_OBJ._split_data()
            GENERATOR_IDX = [CV_index_generator.__next__()]
        for i, (train_index, val_index) in enumerate(GENERATOR_IDX):
            logger.info(f"Cross-Valisation Fold {i}:")

            subset_idx = lambda obj, idx : obj.iloc[idx,:] if isinstance(obj, pd.DataFrame) else obj[idx,:]

            X_train = subset_idx(DATA_PREPROCESSOR_OBJ.X_train, train_index)
            y_train = subset_idx(DATA_PREPROCESSOR_OBJ.y_train, train_index)
            X_val = subset_idx(DATA_PREPROCESSOR_OBJ.X_train, val_index)
            y_val = subset_idx(DATA_PREPROCESSOR_OBJ.y_train, val_index)
            X_test = DATA_PREPROCESSOR_OBJ.X_test
            y_test = DATA_PREPROCESSOR_OBJ.y_test

            MODEL_KEY = f'{MODEL_CONFIG.model_type}__cv_{i}'
            if feature_selection_type:
                MODEL_KEY += '_withFeatureSelection'

            RES_EXP = train_and_eval(
                X_train = X_train,
                y_train = y_train,
                X_val = X_val,
                y_val = y_val,
                logger = logger,
                COMET_EXPERIMENT = COMET_EXPERIMENT,
                title = MODEL_KEY,
                DATA_PIPELINE_CONFIG = DATA_PIPELINE_CONFIG,
                MODEL_CONFIG = MODEL_CONFIG,
                OUTPUT_DIR = OUTPUT_DIR,
                X_test = None if cfg.holdout_test else X_test,
                y_test = None if cfg.holdout_test else y_test,
                gameType_testSet = None if cfg.holdout_test else DATA_PREPROCESSOR_OBJ.gameType_Testset,
                USE_SAMPLE_WEIGHTS = cfg.USE_SAMPLE_WEIGHTS,
                JUST_EVALUATE = cfg.JUST_EVALUATE,
                RESUME_FROM_MODEL_CHECKPOINT = cfg.RESUME_FROM_MODEL_CHECKPOINT,
                log_data_splits = cfg.LOG_DATA_SPLITS_BEFORE_TRAIN,
                log_model_to_comet = cfg.LOG_TRAINED_MODEL,

            )

            STATS_EXPERIMENT.update(RES_EXP)

    # _______________________ Training and Validation finished

    # ====================== BIG PICKLE FILE : STATS_EXPERIMENT CONTAINING MODELS, DATA, PREDS, METRICS
    import pickle
    PATH_EXPERIMENT_ASSET = OUTPUT_DIR / "STATS_EXPERIMENT.pkl"
    logger.info(f"Saving models and experiment to disk at {PATH_EXPERIMENT_ASSET}")
    with open(PATH_EXPERIMENT_ASSET, "wb") as f:
        pickle.dump(STATS_EXPERIMENT, f)
    COMET_EXPERIMENT.log_asset(str(PATH_EXPERIMENT_ASSET), "STATS_EXPERIMENT.pkl")

    if not cfg.USE_CROSS_VALIDATION:
        # ====================== PLOT PROB-ORIENTED PERFORMANCE CURVES : ROC, RATIO-GOAL, CUMUL-GOAL, CALIBRATION CURVES
        split_to_plot = [('val', y_val)]
        if not cfg.holdout_test:
            split_to_plot.append(('test_playoffs', STATS_EXPERIMENT[MODEL_KEY]['test_playoffs']['data'][1]))
            split_to_plot.append(('test_regular_season', STATS_EXPERIMENT[MODEL_KEY]['test_regular_season']['data'][1]))
        
        for split, y_true in split_to_plot:
            OUTPUT_DIR = OUTPUT_DIR / split
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f"\t Plotting prob-oriented performance curves at {OUTPUT_DIR} on {split} set")

            from utils.plot import plotPerfModel
            plotPerfModel(
                predictionsTest={title_model : d[split]['proba_preds'] for title_model, d in STATS_EXPERIMENT.items()},
                yTest=y_true.to_numpy(),
                outputDir=OUTPUT_DIR,
                rocCurve=True,
                ratioGoalPercentileCurve=True,
                proportionGoalPercentileCurve=True,
                calibrationCurve=True,
                COMET_EXPERIMENT=COMET_EXPERIMENT,
                fn_info = f'{split}',
            )
            OUTPUT_DIR = (OUTPUT_DIR).parent

@hydra.main(
    version_base=None, config_path=os.getenv("YAML_CONF_DIR"), config_name="training_main_conf"
)
def main(cfg : DictConfig) -> None:
    logger = init_logger("training_main.log")
    run_experiment(cfg, logger)


if __name__ == "__main__":
    main()
