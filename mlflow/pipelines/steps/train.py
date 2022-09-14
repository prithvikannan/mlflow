import importlib
import logging
import os
import re
import sys
import datetime
import yaml

import cloudpickle

import mlflow
from mlflow.entities import SourceType, ViewType
from mlflow.exceptions import MlflowException, INVALID_PARAMETER_VALUE, BAD_REQUEST
from mlflow.pipelines.cards import BaseCard
from mlflow.pipelines.step import BaseStep
from mlflow.pipelines.utils.execution import (
    get_step_output_path,
    _MLFLOW_PIPELINES_EXECUTION_TARGET_STEP_NAME_ENV_VAR,
)
from mlflow.pipelines.utils.metrics import (
    BUILTIN_PIPELINE_METRICS,
    _get_primary_metric,
    _get_custom_metrics,
    _load_custom_metric_functions,
)
from mlflow.pipelines.utils.step import get_merged_eval_metrics, get_pandas_data_profile
from mlflow.pipelines.utils.tracking import (
    get_pipeline_tracking_config,
    apply_pipeline_tracking_config,
    TrackingConfig,
    get_run_tags_env_vars,
    log_code_snapshot,
)
from mlflow.projects.utils import get_databricks_env_vars
from mlflow.tracking import MlflowClient
from mlflow.tracking.fluent import _get_experiment_id
from mlflow.utils.databricks_utils import get_databricks_run_url
from mlflow.utils.mlflow_tags import (
    MLFLOW_SOURCE_TYPE,
    MLFLOW_PIPELINE_TEMPLATE_NAME,
    MLFLOW_PIPELINE_PROFILE_NAME,
    MLFLOW_PIPELINE_STEP_NAME,
)

_logger = logging.getLogger(__name__)


class TrainStep(BaseStep):

    MODEL_ARTIFACT_RELATIVE_PATH = "model"

    def __init__(self, step_config, pipeline_root, pipeline_config=None):
        super().__init__(step_config, pipeline_root)
        self.pipeline_config = pipeline_config
        self.tracking_config = TrackingConfig.from_dict(step_config)
        self.target_col = self.step_config.get("target_col")
        self.skip_data_profiling = self.step_config.get("skip_data_profiling", False)
        self.train_module_name, self.estimator_method_name = self.step_config[
            "estimator_method"
        ].rsplit(".", 1)
        self.primary_metric = _get_primary_metric(self.step_config)
        self.evaluation_metrics = {metric.name: metric for metric in BUILTIN_PIPELINE_METRICS}
        self.evaluation_metrics.update(
            {metric.name: metric for metric in _get_custom_metrics(self.step_config)}
        )
        self.evaluation_metrics_greater_is_better = {
            metric.name: metric.greater_is_better for metric in BUILTIN_PIPELINE_METRICS
        }
        self.evaluation_metrics_greater_is_better.update(
            {
                metric.name: metric.greater_is_better
                for metric in _get_custom_metrics(self.step_config)
            }
        )
        if self.primary_metric is not None and self.primary_metric not in self.evaluation_metrics:
            raise MlflowException(
                f"The primary metric {self.primary_metric} is a custom metric, but its"
                " corresponding custom metric configuration is missing from `pipeline.yaml`.",
                error_code=INVALID_PARAMETER_VALUE,
            )
        self.code_paths = [os.path.join(self.pipeline_root, "steps")]

    @classmethod
    def _construct_search_space_from_yaml(cls, params):
        from hyperopt import hp

        search_space = {}
        for param_name, param_details in params.items():
            if "values" in param_details:
                search_space[param_name] = hp.choice(param_name, **param_details)
            elif "distribution" in param_details:
                hp_tuning_fn = getattr(hp, param_details["distribution"])
                param_details_to_pass = param_details.copy()
                param_details_to_pass.pop("distribution")
                search_space[param_name] = hp_tuning_fn(param_name, **param_details_to_pass)
            else:
                raise MlflowException(
                    f"Parameter {param_name} must contain either a list of 'values' or a "
                    f"'distribution' following hyperopt parameter expressions",
                    error_code=INVALID_PARAMETER_VALUE,
                )
        return search_space

    def _run(self, output_directory):
        import pandas as pd
        import shutil
        from sklearn.pipeline import make_pipeline
        from mlflow.models.signature import infer_signature

        apply_pipeline_tracking_config(self.tracking_config)

        transformed_training_data_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="transform",
            relative_path="transformed_training_data.parquet",
        )
        train_df = pd.read_parquet(transformed_training_data_path)
        X_train, y_train = train_df.drop(columns=[self.target_col]), train_df[self.target_col]

        transformed_validation_data_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="transform",
            relative_path="transformed_validation_data.parquet",
        )
        validation_df = pd.read_parquet(transformed_validation_data_path)

        raw_training_data_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="split",
            relative_path="train.parquet",
        )
        raw_train_df = pd.read_parquet(raw_training_data_path)
        raw_X_train = raw_train_df.drop(columns=[self.target_col])

        raw_validation_data_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="split",
            relative_path="validation.parquet",
        )
        raw_validation_df = pd.read_parquet(raw_validation_data_path)

        transformer_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="transform",
            relative_path="transformer.pkl",
        )

        sys.path.append(self.pipeline_root)
        estimator_fn = getattr(
            importlib.import_module(self.train_module_name), self.estimator_method_name
        )

        tags = {
            MLFLOW_SOURCE_TYPE: SourceType.to_string(SourceType.PIPELINE),
            MLFLOW_PIPELINE_TEMPLATE_NAME: self.step_config["template_name"],
            MLFLOW_PIPELINE_PROFILE_NAME: self.step_config["profile"],
            MLFLOW_PIPELINE_STEP_NAME: os.getenv(
                _MLFLOW_PIPELINES_EXECUTION_TARGET_STEP_NAME_ENV_VAR
            ),
        }

        best_estimator_params = None
        mlflow.autolog(log_models=False)
        with mlflow.start_run(tags=tags) as run:
            estimator_hardcoded_params = self.step_config["estimator_params"]
            if self.step_config["tuning_enabled"]:
                best_estimator_params = self._tune_and_get_best_estimator_params(
                    estimator_hardcoded_params,
                    estimator_fn,
                    X_train,
                    y_train,
                    validation_df,
                    output_directory,
                )
                estimator = estimator_fn(best_estimator_params)
            else:
                estimator = estimator_fn(estimator_hardcoded_params)

            estimator.fit(X_train, y_train)

            logged_estimator = self._log_estimator_to_mlflow(estimator, X_train)

            # Create a pipeline consisting of the transformer+model for test data evaluation
            with open(transformer_path, "rb") as f:
                transformer = cloudpickle.load(f)
            mlflow.sklearn.log_model(
                transformer, "transform/transformer", code_paths=self.code_paths
            )
            model = make_pipeline(transformer, estimator)
            model_schema = infer_signature(raw_X_train, model.predict(raw_X_train.copy()))
            model_info = mlflow.sklearn.log_model(
                model, f"{self.name}/model", signature=model_schema, code_paths=self.code_paths
            )
            output_model_path = get_step_output_path(
                pipeline_root_path=self.pipeline_root,
                step_name=self.name,
                relative_path=TrainStep.MODEL_ARTIFACT_RELATIVE_PATH,
            )
            if os.path.exists(output_model_path) and os.path.isdir(output_model_path):
                shutil.rmtree(output_model_path)
            mlflow.sklearn.save_model(model, output_model_path)

            with open(os.path.join(output_directory, "run_id"), "w") as f:
                f.write(run.info.run_id)
            log_code_snapshot(
                self.pipeline_root, run.info.run_id, pipeline_config=self.pipeline_config
            )

            eval_metrics = {}
            for dataset_name, dataset in {
                "training": train_df,
                "validation": validation_df,
            }.items():
                eval_result = mlflow.evaluate(
                    model=logged_estimator.model_uri,
                    data=dataset,
                    targets=self.target_col,
                    model_type="regressor",
                    evaluators="default",
                    dataset_name=dataset_name,
                    custom_metrics=_load_custom_metric_functions(
                        self.pipeline_root,
                        self.evaluation_metrics.values(),
                    ),
                    evaluator_config={
                        "log_model_explainability": False,
                    },
                )
                eval_result.save(os.path.join(output_directory, f"eval_{dataset_name}"))
                eval_metrics[dataset_name] = eval_result.metrics

        target_data = raw_validation_df[self.target_col]
        prediction_result = model.predict(raw_validation_df.drop(self.target_col, axis=1))
        pred_and_error_df = pd.DataFrame(
            {
                "target": target_data,
                "prediction": prediction_result,
                "error": prediction_result - target_data,
            }
        )
        train_predictions = model.predict(raw_train_df.drop(self.target_col, axis=1))
        worst_examples_df = BaseStep._generate_worst_examples_dataframe(
            raw_train_df, train_predictions, self.target_col
        )
        leaderboard_df = None
        try:
            leaderboard_df = self._get_leaderboard_df(run, eval_metrics)
        except Exception as e:
            _logger.warning("Failed to build model leaderboard due to unexpected failure: %s", e)
        tuning_df = None
        if best_estimator_params:
            try:
                tuning_df = self._get_tuning_df(run, params=best_estimator_params.keys())
            except Exception as e:
                _logger.warning("Failed to build tuning table due to unexpected failure: %s", e)

        card = self._build_step_card(
            eval_metrics=eval_metrics,
            pred_and_error_df=pred_and_error_df,
            model=model,
            model_schema=model_schema,
            run_id=run.info.run_id,
            model_uri=model_info.model_uri,
            worst_examples_df=worst_examples_df,
            output_directory=output_directory,
            leaderboard_df=leaderboard_df,
            tuning_df=tuning_df,
            best_estimator_params=best_estimator_params,
        )
        card.save_as_html(output_directory)
        for step_name in ("ingest", "split", "transform", "train"):
            self._log_step_card(run.info.run_id, step_name)

        return card

    def _get_leaderboard_df(self, run, eval_metrics):
        import pandas as pd

        mlflow_client = MlflowClient()
        exp_id = _get_experiment_id()

        primary_metric_greater_is_better = self.evaluation_metrics[
            self.primary_metric
        ].greater_is_better
        primary_metric_order = "DESC" if primary_metric_greater_is_better else "ASC"

        search_max_results = 100
        search_result = mlflow_client.search_runs(
            experiment_ids=exp_id,
            run_view_type=ViewType.ACTIVE_ONLY,
            max_results=search_max_results,
            order_by=[f"metrics.{self.primary_metric}_on_data_validation {primary_metric_order}"],
        )

        metric_names = self.evaluation_metrics.keys()
        metric_keys = [f"{metric_name}_on_data_validation" for metric_name in metric_names]

        leaderboard_items = []
        for old_run in search_result:
            if all(metric_key in old_run.data.metrics for metric_key in metric_keys):
                leaderboard_items.append(
                    {
                        "Run ID": old_run.info.run_id,
                        "Run Time": datetime.datetime.fromtimestamp(
                            old_run.info.start_time // 1000
                        ),
                        **{
                            metric_name: old_run.data.metrics[metric_key]
                            for metric_name, metric_key in zip(metric_names, metric_keys)
                        },
                    }
                )

        top_leaderboard_items = [
            {"Model Rank": i + 1, **t} for i, t in enumerate(leaderboard_items[:2])
        ]

        if (
            len(top_leaderboard_items) == 2
            and top_leaderboard_items[0][self.primary_metric]
            == top_leaderboard_items[1][self.primary_metric]
        ):
            # If top1 and top2 model primary metrics are equal,
            # then their rank are both 1.
            top_leaderboard_items[1]["Model Rank"] = "1"

        top_leaderboard_item_index_values = ["Best", "2nd Best"][: len(top_leaderboard_items)]

        latest_model_item = {
            "Run ID": run.info.run_id,
            "Run Time": datetime.datetime.fromtimestamp(run.info.start_time // 1000),
            **eval_metrics["validation"],
        }

        for i, leaderboard_item in enumerate(leaderboard_items):
            latest_value = latest_model_item[self.primary_metric]
            historical_value = leaderboard_item[self.primary_metric]
            if (primary_metric_greater_is_better and latest_value >= historical_value) or (
                not primary_metric_greater_is_better and latest_value <= historical_value
            ):
                latest_model_item["Model Rank"] = str(i + 1)
                break
        else:
            latest_model_item["Model Rank"] = f"> {len(leaderboard_items)}"

        # metric columns order: primary metric, custom metrics, builtin metrics.
        def sorter(m):
            if m == self.primary_metric:
                return 0, m
            elif self.evaluation_metrics[m].custom_function is not None:
                return 1, m
            else:
                return 2, m

        metric_columns = sorted(metric_names, key=sorter)

        leaderboard_df = (
            pd.DataFrame.from_records(
                [latest_model_item, *top_leaderboard_items],
                columns=["Model Rank", *metric_columns, "Run Time", "Run ID"],
            )
            .apply(
                lambda s: s.map(lambda x: "{:.6g}".format(x))  # pylint: disable=unnecessary-lambda
                if s.name in metric_names
                else s,  # pylint: disable=unnecessary-lambda
                axis=0,
            )
            .set_axis(["Latest"] + top_leaderboard_item_index_values, axis="index")
            .transpose()
        )
        return leaderboard_df

    def _get_tuning_df(self, run, params=None):
        exp_id = _get_experiment_id()
        tuning_runs = mlflow.search_runs(
            [exp_id],
            filter_string=f"tags.mlflow.parentRunId like '{run.info.run_id}'",
        )
        if params:
            params = [f"params.{param}" for param in params]
            tuning_runs = tuning_runs.filter(
                [f"metrics.{self.primary_metric}_on_data_validation", *params]
            )
        else:
            tuning_runs = tuning_runs.filter([f"metrics.{self.primary_metric}_on_data_validation"])
        return tuning_runs

    def _build_step_card(
        self,
        eval_metrics,
        pred_and_error_df,
        model,
        model_schema,
        run_id,
        model_uri,
        worst_examples_df,
        output_directory,
        leaderboard_df=None,
        tuning_df=None,
        best_estimator_params=None,
    ):
        import pandas as pd
        from sklearn.utils import estimator_html_repr
        from sklearn import set_config

        card = BaseCard(self.pipeline_name, self.name)
        # Tab 0: model performance summary.
        metric_df = (
            get_merged_eval_metrics(eval_metrics, ordered_metric_names=[self.primary_metric])
            .reset_index()
            .rename(columns={"index": "Metric"})
        )

        def row_style(row):
            if row.Metric == self.primary_metric:
                return pd.Series("font-weight: bold", row.index)
            else:
                return pd.Series("", row.index)

        metric_table_html = BaseCard.render_table(
            metric_df.style.format({"training": "{:.6g}", "validation": "{:.6g}"}).apply(
                row_style, axis=1
            )
        )

        # Tab 1: Model performance summary metrics.
        card.add_tab(
            "Model Performance Summary Metrics",
            "<h3 class='section-title'>Summary Metrics</h3>{{ METRICS }} ",
        ).add_html("METRICS", metric_table_html)

        if not self.skip_data_profiling:
            # Tab 2: Prediction and error data profile.
            pred_and_error_df_profile = get_pandas_data_profile(
                pred_and_error_df.reset_index(drop=True),
                "Predictions and Errors (Validation Dataset)",
            )
            card.add_tab("Profile of Predictions and Errors", "{{PROFILE}}").add_pandas_profile(
                "PROFILE", pred_and_error_df_profile
            )
        # Tab 3: Model architecture.
        set_config(display="diagram")
        model_repr = estimator_html_repr(model)
        card.add_tab("Model Architecture", "{{MODEL_ARCH}}").add_html("MODEL_ARCH", model_repr)

        # Tab 4: Inferred model (transformer + estimator) schema.
        def render_schema(inputs, title):
            from mlflow.types import ColSpec

            table = BaseCard.render_table(
                (
                    {
                        "Name": "  " + (spec.name or "-"),
                        "Type": repr(spec.type) if isinstance(spec, ColSpec) else repr(spec),
                    }
                    for spec in inputs
                )
            )
            return '<div style="margin: 5px"><h2>{title}</h2>{table}</div>'.format(
                title=title, table=table
            )

        schema_tables = [render_schema(model_schema.inputs.inputs, "Inputs")]
        if model_schema.outputs:
            schema_tables += [render_schema(model_schema.outputs.inputs, "Outputs")]

        card.add_tab("Model Schema", "{{MODEL_SCHEMA}}").add_html(
            "MODEL_SCHEMA",
            '<div style="display: flex">{tables}</div>'.format(tables="\n".join(schema_tables)),
        )

        # Tab 5: Examples with Largest Prediction Error
        (
            card.add_tab(
                "Training Examples with Largest Prediction Error", "{{ WORST_EXAMPLES_TABLE }}"
            ).add_html("WORST_EXAMPLES_TABLE", BaseCard.render_table(worst_examples_df))
        )

        # Tab 6: Leaderboard
        if leaderboard_df is not None:
            (
                card.add_tab("Leaderboard", "{{ LEADERBOARD_TABLE }}").add_html(
                    "LEADERBOARD_TABLE", BaseCard.render_table(leaderboard_df, hide_index=False)
                )
            )

        # Tab 7: Run summary.
        run_card_tab = card.add_tab(
            "Run Summary",
            "{{ RUN_ID }} " + "{{ MODEL_URI }}" + "{{ EXE_DURATION }}" + "{{ LAST_UPDATE_TIME }}",
        )
        run_url = get_databricks_run_url(
            tracking_uri=mlflow.get_tracking_uri(),
            run_id=run_id,
        )
        model_url = get_databricks_run_url(
            tracking_uri=mlflow.get_tracking_uri(),
            run_id=run_id,
            artifact_path=re.sub(r"^.*?%s" % run_id, "", model_uri),
        )

        if run_url is not None:
            run_card_tab.add_html(
                "RUN_ID", f"<b>MLflow Run ID:</b> <a href={run_url}>{run_id}</a><br><br>"
            )
        else:
            run_card_tab.add_markdown("RUN_ID", f"**MLflow Run ID:** `{run_id}`")

        if model_url is not None:
            run_card_tab.add_html(
                "MODEL_URI", f"<b>MLflow Model URI:</b> <a href={model_url}>{model_uri}</a>"
            )
        else:
            run_card_tab.add_markdown("MODEL_URI", f"**MLflow Model URI:** `{model_uri}`")

        # Tab 8: Best Parameters
        if best_estimator_params:
            tuning_params_card_tab = card.add_tab(
                "Best Parameters",
                "{{ SEARCH_SPACE }} " + "{{ BEST_PARAMETERS }} ",
            )
            tuning_params = yaml.dump(self.step_config["tuning"]["parameters"])
            tuning_params_card_tab.add_html(
                "SEARCH_SPACE",
                f"<b>Tuning search space:</b> <br><pre>{tuning_params}</pre><br><br>",
            )
            best_parameters_yaml = os.path.join(output_directory, "best_parameters.yaml")
            if os.path.exists(best_parameters_yaml):
                best_hardcoded_parameters = open(best_parameters_yaml).read()
                tuning_params_card_tab.add_html(
                    "BEST_PARAMETERS",
                    f"<b>Best parameters:</b><br>"
                    f"<pre>{best_hardcoded_parameters}</pre><br><br>",
                )

        # Tab 9: HP trials
        if tuning_df is not None:
            (
                card.add_tab("Tuning Trials", "{{ TUNING_TABLE }}").add_html(
                    "TUNING_TABLE", BaseCard.render_table(tuning_df, hide_index=False)
                )
            )

        return card

    @classmethod
    def from_pipeline_config(cls, pipeline_config, pipeline_root):
        try:
            step_config = pipeline_config["steps"]["train"]
            step_config["metrics"] = pipeline_config.get("metrics")
            step_config["template_name"] = pipeline_config.get("template")
            step_config["profile"] = pipeline_config.get("profile")
            step_config["run_args"] = pipeline_config.get("run_args")
            if "using" in step_config:
                if step_config["using"] not in ["estimator_spec"]:
                    raise MlflowException(
                        f"Invalid train step configuration value {step_config['using']} for key "
                        f"'using'. Supported values are: ['estimator_spec']",
                        error_code=INVALID_PARAMETER_VALUE,
                    )
            else:
                step_config["using"] = "estimator_spec"

            if "tuning" in step_config:
                if "enabled" in step_config["tuning"] and isinstance(
                    step_config["tuning"]["enabled"], bool
                ):
                    step_config["tuning_enabled"] = step_config["tuning"]["enabled"]
                else:
                    raise MlflowException(
                        "The 'tuning' configuration in the train step must include an "
                        "'enabled' key whose value is either true or false.",
                        error_code=INVALID_PARAMETER_VALUE,
                    )
                if step_config["tuning_enabled"]:
                    if "sample_fraction" in step_config["tuning"]:
                        sample_fraction = float(step_config["tuning"]["sample_fraction"])
                        if sample_fraction > 0 and sample_fraction <= 1.0:
                            step_config["sample_fraction"] = sample_fraction
                        else:
                            raise MlflowException(
                                "The 'sample_fraction' configuration in the train step must be "
                                "between 0 and 1.",
                                error_code=INVALID_PARAMETER_VALUE,
                            )
                    else:
                        step_config["sample_fraction"] = 1.0

                    if "algorithm" not in step_config["tuning"]:
                        step_config["tuning"]["algorithm"] = "hyperopt.rand.suggest"

                    if "parallelism" not in step_config["tuning"]:
                        step_config["tuning"]["parallelism"] = 1

                    if "max_trials" not in step_config["tuning"]:
                        raise MlflowException(
                            "The 'max_trials' configuration in the train step must be provided.",
                            error_code=INVALID_PARAMETER_VALUE,
                        )

                    if "parameters" not in step_config["tuning"]:
                        raise MlflowException(
                            "The 'parameters' configuration in the train step must be provided.",
                            error_code=INVALID_PARAMETER_VALUE,
                        )

            else:
                step_config["tuning_enabled"] = False

            if "estimator_params" not in step_config:
                step_config["estimator_params"] = {}

            step_config.update(
                get_pipeline_tracking_config(
                    pipeline_root_path=pipeline_root,
                    pipeline_config=pipeline_config,
                ).to_dict()
            )
        except KeyError:
            raise MlflowException(
                "Config for train step is not found.", error_code=INVALID_PARAMETER_VALUE
            )
        step_config["target_col"] = pipeline_config.get("target_col")
        return cls(step_config, pipeline_root, pipeline_config=pipeline_config)

    @property
    def name(self):
        return "train"

    @property
    def environment(self):
        environ = get_databricks_env_vars(tracking_uri=self.tracking_config.tracking_uri)
        environ.update(get_run_tags_env_vars(pipeline_root_path=self.pipeline_root))
        return environ

    def _tune_and_get_best_estimator_params(
        self,
        estimator_hardcoded_params,
        estimator_fn,
        X_train,
        y_train,
        validation_df,
        output_directory,
    ):
        tuning_params = self.step_config["tuning"]
        try:
            from hyperopt import fmin, Trials
        except ModuleNotFoundError:
            raise MlflowException(
                "Hyperopt not installed and is required if tuning is enabled",
                error_code=BAD_REQUEST,
            )

        search_space = TrainStep._construct_search_space_from_yaml(tuning_params["parameters"])
        algo_type, algo_name = tuning_params["algorithm"].rsplit(".", 1)
        tuning_algo = getattr(importlib.import_module(algo_type, "hyperopt"), algo_name)
        max_trials = tuning_params["max_trials"]
        parallelism = tuning_params["parallelism"]

        if parallelism > 1:
            from hyperopt import SparkTrials
            from mlflow.utils._spark_utils import _get_active_spark_session

            spark_session = _get_active_spark_session()
            sc = spark_session.sparkContext

            print("X_train (before broadcast): ", X_train)
            X_train = sc.broadcast(X_train)
            print("X_train (after broadcast): ", X_train)
            y_train = sc.broadcast(y_train)
            validation_df = sc.broadcast(validation_df)

            hp_trials = SparkTrials(parallelism, spark_session=spark_session)
        else:
            hp_trials = Trials()

        # wrap training in objective fn
        def objective(hyperparameter_args):
            with mlflow.start_run(nested=True) as tuning_run:
                estimator_args = dict(estimator_hardcoded_params, **hyperparameter_args)
                estimator = estimator_fn(estimator_args)

                sample_fraction = self.step_config["sample_fraction"]

                # if sparktrials, then read from broadcast
                if parallelism > 1:
                    # getting an error here
                    X_train = X_train.value
                    y_train = y_train.value
                    validation_df = validation_df.value

                X_train_sampled = X_train.sample(frac=sample_fraction, random_state=42)
                y_train_sampled = y_train.sample(frac=sample_fraction, random_state=42)

                estimator.fit(X_train_sampled, y_train_sampled)

                logged_estimator = self._log_estimator_to_mlflow(estimator, X_train_sampled)

                eval_result = mlflow.evaluate(
                    model=logged_estimator.model_uri,
                    data=validation_df,
                    targets=self.target_col,
                    model_type="regressor",
                    evaluators="default",
                    dataset_name="validation",
                    custom_metrics=_load_custom_metric_functions(
                        self.pipeline_root,
                        self.evaluation_metrics.values(),
                    ),
                    evaluator_config={
                        "log_model_explainability": False,
                    },
                )
                autologged_params = mlflow.get_run(run_id=tuning_run.info.run_id).data.params
                for param_name, param_value in estimator_args.items():
                    if param_name in autologged_params:
                        if not self._is_equal(param_value, autologged_params[param_name]):
                            _logger.warning(
                                f"Failed to log parameter {param_name} due to "
                                f"conflict. old_value: {autologged_params[param_name]} "
                                f"new_value: {param_value} type: {type(param_value)}"
                            )
                    else:
                        mlflow.log_param(param_name, param_value)

                # return +/- metric
                sign = -1 if self.evaluation_metrics_greater_is_better[self.primary_metric] else 1
                return sign * eval_result.metrics[self.primary_metric]

        best_hp_params = fmin(
            objective,
            search_space,
            algo=tuning_algo,
            max_evals=max_trials,
            trials=hp_trials,
        )
        best_hp_estimator_loss = hp_trials.best_trial["result"]["loss"]
        hardcoded_estimator_loss = objective(estimator_hardcoded_params)

        if best_hp_estimator_loss < hardcoded_estimator_loss:
            best_hardcoded_params = {
                param_name: param_value
                for param_name, param_value in estimator_hardcoded_params.items()
                if param_name not in best_hp_params
            }
        else:
            best_hp_params = {}
            best_hardcoded_params = estimator_hardcoded_params

        best_combined_params = dict(estimator_hardcoded_params, **best_hp_params)
        self._write_yaml_output(best_hp_params, best_hardcoded_params, output_directory)
        return best_combined_params

    def _log_estimator_to_mlflow(self, estimator, X_train_sampled):
        from mlflow.models.signature import infer_signature

        if hasattr(estimator, "best_score_"):
            mlflow.log_metric("best_cv_score", estimator.best_score_)
        if hasattr(estimator, "best_params_"):
            mlflow.log_params(estimator.best_params_)

        estimator_schema = infer_signature(
            X_train_sampled, estimator.predict(X_train_sampled.copy())
        )
        logged_estimator = mlflow.sklearn.log_model(
            estimator,
            f"{self.name}/estimator",
            signature=estimator_schema,
            code_paths=self.code_paths,
        )
        return logged_estimator

    def _write_yaml_output(self, best_hp_params, best_hardcoded_params, output_directory):
        best_parameters_path = os.path.join(output_directory, "best_parameters.yaml")
        if os.path.exists(best_parameters_path):
            os.remove(best_parameters_path)
        with open(best_parameters_path, "a") as file:
            file.write("# tuned hyperparameters\n")
            self._process_and_safe_dump(best_hp_params, file, default_flow_style=False)
            file.write("# hardcoded parameters\n")
            self._process_and_safe_dump(best_hardcoded_params, file, default_flow_style=False)
        mlflow.log_artifact(best_parameters_path)

    def _process_and_safe_dump(self, data, file, **kwargs):
        import numpy as np

        processed_data = {}
        for key, value in data.items():
            if isinstance(value, np.floating):
                processed_data[key] = float(value)
            elif isinstance(value, np.integer):
                processed_data[key] = int(value)
            else:
                processed_data[key] = value
        return yaml.safe_dump(processed_data, file, **kwargs)

    def _is_equal(self, new_param, logged_param):
        if isinstance(new_param, int):
            return new_param == int(logged_param)
        elif isinstance(new_param, float):
            return new_param == float(logged_param)
        elif isinstance(new_param, str):
            return new_param.strip() == logged_param.strip()
        else:
            return new_param == logged_param
