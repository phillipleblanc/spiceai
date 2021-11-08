from concurrent import futures
import json
import os
from pathlib import Path
import signal
import threading
import traceback
from typing import Dict
import multiprocessing

import grpc
import pandas as pd
from psutil import Process
import requests

from algorithms.factory import get_agent
from algorithms.agent_interface import SpiceAIAgent
from cleanup import cleanup_on_shutdown
from connector.manager import ConnectorManager
from data import DataManager
from event_loop import EventLoop
from exception import UnexpectedException
from inference import GetInferenceHandler
from proto.aiengine.v1 import aiengine_pb2, aiengine_pb2_grpc
from train import Trainer
from validation import validate_rewards

work_queue = multiprocessing.SimpleQueue()
data_managers: Dict[str, DataManager] = {}
connector_managers: Dict[str, ConnectorManager] = {}


class Dispatch:
    TRAINING_THREAD = None
    INIT_LOCK = threading.Lock()


def train_agent(
        pod_name: str, data_manager: DataManager, connector_manager: ConnectorManager, algorithm: str,
        number_episodes: int, flight: str, training_goal: str):
    try:
        Trainer(pod_name, data_manager, connector_manager, algorithm, number_episodes, flight, training_goal).train()
    except Exception:
        request_url = Trainer.BASE_URL + f"/{pod_name}/training_runs/{flight}/episodes"
        requests.post(request_url, json=UnexpectedException(traceback.format_exc()).get_error_body())


def dispatch_train_agent(
        pod_name: str, data_manager: DataManager, connector_manager: ConnectorManager, algorithm: str,
        number_episodes: int, flight: str, training_goal: str):
    if Trainer.TRAINING_LOCK.locked():
        return False

    Dispatch.TRAINING_THREAD = threading.Thread(
        target=train_agent,
        args=(pod_name, data_manager, connector_manager, algorithm, number_episodes, flight, training_goal))
    Dispatch.TRAINING_THREAD.start()
    return True


class AIEngine(aiengine_pb2_grpc.AIEngineServicer):
    def GetHealth(self, request, context):
        return aiengine_pb2.Response(result="ok")

    def AddData(self, request: aiengine_pb2.AddDataRequest, context):
        work_queue.put(("add_data", request))

        return aiengine_pb2.Response(result="ok")

    def AddInterpretations(self, request: aiengine_pb2.AddInterpretationsRequest, context):
        data_manager = data_managers[request.pod]
        data_manager.add_interpretations(request.indexed_interpretations)
        return aiengine_pb2.Response(result="ok")

    def StartTraining(self, request: aiengine_pb2.StartTrainingRequest, context):
        data_manager = data_managers[request.pod]
        connector_manager: ConnectorManager = connector_managers[request.pod]

        if request.epoch_time != 0:
            new_epoch_time = pd.to_datetime(request.epoch_time, unit="s")
            if new_epoch_time < data_manager.param.epoch_time:
                return aiengine_pb2.Response(
                    result="epoch_time_invalid",
                    message=f"epoch time should be after {data_manager.param.epoch_time.timestamp()}",
                    error=True,
                )
            data_manager.param.epoch_time = new_epoch_time
            data_manager.param.end_time = data_manager.param.epoch_time + data_manager.param.period_secs

        algorithm = request.learning_algorithm
        number_episodes = request.number_episodes if request.number_episodes != 0 else 30
        flight = request.flight
        training_goal = request.training_goal

        index_of_epoch = data_manager.massive_table_filled.index.get_loc(
            data_manager.param.epoch_time, "ffill"
        )

        if len(data_manager.massive_table_filled.iloc[index_of_epoch:]) < data_manager.get_window_span():
            return aiengine_pb2.Response(
                result="not_enough_data_for_training",
                error=True,
            )

        started = dispatch_train_agent(
            request.pod, data_manager, connector_manager, algorithm, number_episodes, flight, training_goal)
        result = "started_training" if started else "already_training"
        return aiengine_pb2.Response(result=result)

    def GetInference(self, request: aiengine_pb2.InferenceRequest, context):
        handler = GetInferenceHandler(request, data_managers)
        return handler.get_result()

    def Init(self, request: aiengine_pb2.InitRequest, context):
        if len(request.actions) == 0:
            return aiengine_pb2.Response(result="missing_actions", error=True)
        action_rewards = request.actions
        if not validate_rewards(action_rewards, request.external_reward_funcs):
            return aiengine_pb2.Response(
                result="invalid_reward_function", error=True
            )

        if len(request.fields) == 0:
            return aiengine_pb2.Response(result="missing_fields", error=True)

        work_queue.put(("init", request))

        return aiengine_pb2.Response(result="ok")

    def ExportModel(self, request: aiengine_pb2.ExportModelRequest, context):
        if request.pod not in Trainer.SAVED_MODELS:
            return aiengine_pb2.ExportModelResult(
                response=aiengine_pb2.Response(
                    result="pod_not_trained",
                    message="Unable to export a model that hasn't finished at least one training run",
                    error=True))

        if request.pod not in data_managers:
            return aiengine_pb2.ExportModelResult(
                resopnse=aiengine_pb2.Response(result="pod_not_initialized", error=True))

        if request.tag != "latest":
            return aiengine_pb2.ExportModelResult(
                response=aiengine_pb2.Response(
                    result="tag_not_yet_supported",
                    message="Support for multiple tags coming soon!",
                    error=True))

        return aiengine_pb2.ExportModelResult(
            response=aiengine_pb2.Response(result="ok"),
            model_path=str(Trainer.SAVED_MODELS[request.pod]))

    def ImportModel(self, request: aiengine_pb2.ImportModelRequest, context):
        if request.pod not in data_managers:
            return aiengine_pb2.Response(result="pod_not_initialized", error=True)

        data_manager = data_managers[request.pod]
        model_data_shape = data_manager.get_shape()
        import_path = Path(request.import_path)

        if not (import_path / "meta.json").exists():
            return aiengine_pb2.Response(
                result="unable_to_load_model_metadata",
                message=f"Unable to find meta data at {import_path}",
                error=True,
            )
        with open(import_path / "meta.json", "r", encoding="utf-8") as meta_file:
            algorithm = json.loads(meta_file.read())["algorithm"]

        agent: SpiceAIAgent = get_agent(algorithm, model_data_shape, len(data_manager.action_names))
        if not agent.load(import_path):
            return aiengine_pb2.Response(
                result="unable_to_load_model",
                message=f"Unable to find a model at {import_path}",
                error=True)

        Trainer.SAVED_MODELS[request.pod] = Path(request.import_path)

        return aiengine_pb2.Response(result="ok")


def wait_parent_process():
    current_process = Process(os.getpid())
    parent_process: Process = current_process.parent()

    parent_process.wait()


def main():
    # Preventing tensorflow verbose initialization
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    import tensorflow as tf  # pylint: disable=import-outside-toplevel

    # Eager execution is too slow to use, so disabling
    tf.compat.v1.disable_eager_execution()

    signal.signal(signal.SIGINT, cleanup_on_shutdown)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    aiengine_pb2_grpc.add_AIEngineServicer_to_server(AIEngine(), server)
    server.add_insecure_port("[::]:8004")
    server.start()
    print(f"AIEngine: gRPC server listening on port {8004}")

    event_loop = EventLoop(work_queue=work_queue, data_managers=data_managers, connector_managers=connector_managers)
    event_loop.start()

    wait_parent_process()
    cleanup_on_shutdown()


if __name__ == "__main__":
    main()
