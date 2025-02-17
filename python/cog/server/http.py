import argparse
import logging
import os
import signal
import textwrap
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union

import structlog
import uvicorn
from anyio import CapacityLimiter
from anyio.lowlevel import RunVar
from enum import Enum, auto, unique
from fastapi import Body, FastAPI, Header, HTTPException, Path, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic.error_wrappers import ErrorWrapper

from .. import schema
from ..files import upload_file
from ..json import upload_files
from ..logging import setup_logging
from ..predictor import (
    get_input_type,
    get_output_type,
    get_predictor_ref,
    load_config,
    load_predictor_from_ref,
)
from .probes import ProbeHelper
from .runner import PredictionRunner, RunnerBusyError, UnknownPredictionError

log = structlog.get_logger("cog.server.http")


@unique
class Health(Enum):
    UNKNOWN = auto()
    STARTING = auto()
    READY = auto()
    BUSY = auto()
    SETUP_FAILED = auto()


def create_app(
    predictor_ref: str,
    shutdown_event: threading.Event,
    threads: int = 1,
    upload_url: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(
        title="Cog",  # TODO: mention model name?
        # version=None # TODO
    )

    app.state.health = Health.STARTING
    app.state.setup_result = None

    runner = PredictionRunner(
        predictor_ref=predictor_ref,
        shutdown_event=shutdown_event,
        upload_url=upload_url,
    )
    # TODO: avoid loading predictor code in this process
    predictor = load_predictor_from_ref(predictor_ref)

    InputType = get_input_type(predictor)
    OutputType = get_output_type(predictor)

    PredictionRequest = schema.PredictionRequest.with_types(input_type=InputType)
    PredictionResponse = schema.PredictionResponse.with_types(
        input_type=InputType, output_type=OutputType
    )

    @app.on_event("startup")
    def startup() -> None:
        # https://github.com/tiangolo/fastapi/issues/4221
        RunVar("_default_thread_limiter").set(CapacityLimiter(threads))  # type: ignore

        setup_start = datetime.now(timezone.utc)
        setup_status, setup_logs = runner.setup()
        setup_complete = datetime.now(timezone.utc)

        app.state.setup_result = {
            "logs": setup_logs,
            "status": setup_status,
            "started_at": setup_start,
            "completed_at": setup_complete,
        }

        if setup_status == schema.Status.SUCCEEDED:
            app.state.health = Health.READY
        else:
            app.state.health = Health.SETUP_FAILED

        probes = ProbeHelper()
        probes.ready()

    @app.on_event("shutdown")
    def shutdown() -> None:
        runner.shutdown()

    @app.get("/")
    def root() -> Any:
        return {
            # "cog_version": "", # TODO
            "docs_url": "/docs",
            "openapi_url": "/openapi.json",
        }

    @app.get("/health-check")
    def healthcheck() -> Any:
        if app.state.health == Health.READY:
            health = Health.BUSY if runner.is_busy() else Health.READY
        else:
            health = app.state.health
        return jsonable_encoder(
            {
                "status": health.name,
                "setup": app.state.setup_result,
            }
        )

    @app.post(
        "/predictions",
        response_model=PredictionResponse,
        response_model_exclude_unset=True,
    )
    def predict(request: PredictionRequest = Body(default=None), prefer: Union[str, None] = Header(default=None)) -> Any:  # type: ignore
        """
        Run a single prediction on the model
        """
        if runner.is_busy():
            return JSONResponse(
                {"detail": "Already running a prediction"}, status_code=409
            )

        # TODO: spec-compliant parsing of Prefer header.
        respond_async = prefer == "respond-async"

        return _predict(request=request, respond_async=respond_async)

    @app.put(
        "/predictions/{prediction_id}",
        response_model=PredictionResponse,
        response_model_exclude_unset=True,
    )
    def predict_idempotent(
        prediction_id: str = Path(..., title="Prediction ID"),
        request: PredictionRequest = Body(..., title="Prediction Request"),
        prefer: Union[str, None] = Header(default=None),
    ) -> Any:
        """
        Run a single prediction on the model (idempotent creation).
        """
        if request.id is not None and request.id != prediction_id:
            raise RequestValidationError(
                [
                    ErrorWrapper(
                        ValueError(
                            "prediction ID must match the ID supplied in the URL"
                        ),
                        ("body", "id"),
                    )
                ]
            )

        # We've already checked that the IDs match, now ensure that an ID is
        # set on the prediction object
        request.id = prediction_id

        # TODO: spec-compliant parsing of Prefer header.
        respond_async = prefer == "respond-async"

        return _predict(request=request, respond_async=respond_async)

    def _predict(
        *, request: PredictionRequest, respond_async: bool = False
    ) -> Response:
        # [compat] If no body is supplied, assume that this model can be run
        # with empty input. This will throw a ValidationError if that's not
        # possible.
        if request is None:
            request = PredictionRequest(input={})
        # [compat] If body is supplied but input is None, set it to an empty
        # dictionary so that later code can be simpler.
        if request.input is None:
            request.input = {}

        try:
            # For now, we only ask PredictionRunner to handle file uploads for
            # async predictions. This is unfortunate but required to ensure
            # backwards-compatible behaviour for synchronous predictions.
            initial_response, async_result = runner.predict(
                request, upload=respond_async
            )
        except RunnerBusyError:
            return JSONResponse(
                {"detail": "Already running a prediction"}, status_code=409
            )

        if respond_async:
            return JSONResponse(jsonable_encoder(initial_response), status_code=202)

        try:
            response = PredictionResponse(**async_result.get().dict())
        except ValidationError as e:
            _log_invalid_output(e)
            raise HTTPException(status_code=500)

        response_object = response.dict()
        response_object["output"] = upload_files(
            response_object["output"],
            upload_file=lambda fh: upload_file(fh, request.output_file_prefix),  # type: ignore
        )

        # FIXME: clean up output files
        encoded_response = jsonable_encoder(response_object)
        return JSONResponse(content=encoded_response)

    @app.post("/predictions/{prediction_id}/cancel")
    def cancel(prediction_id: str = Path(..., title="Prediction ID")) -> Any:
        """
        Cancel a running prediction
        """
        if not runner.is_busy():
            return JSONResponse({}, status_code=404)
        try:
            runner.cancel(prediction_id)
        except UnknownPredictionError:
            return JSONResponse({}, status_code=404)
        else:
            return JSONResponse({}, status_code=200)

    @app.post("/shutdown")
    def start_shutdown() -> Any:
        log.info("shutdown requested via http")
        shutdown_event.set()
        return JSONResponse({}, status_code=200)

    return app


def _log_invalid_output(error: Any) -> None:
    log.error(
        textwrap.dedent(
            f"""\
            The return value of predict() was not valid:

            {error}

            Check that your predict function is in this form, where `output_type` is the same as the type you are returning (e.g. `str`):

                def predict(...) -> output_type:
                    ...
           """
        )
    )


class Server(uvicorn.Server):
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run)
        self._thread.start()

    def stop(self) -> None:
        log.info("stopping server")
        self.should_exit = True

        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            return

        log.warn("failed to exit after 5 seconds, setting force_exit")
        self.force_exit = True
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            return

        log.warn("failed to exit after another 5 seconds, sending SIGKILL")
        os.kill(os.getpid(), signal.SIGKILL)


def signal_ignore(signum: Any, frame: Any) -> None:
    log.warn("Got a signal to exit, ignoring it...", signal=signal.Signals(signum).name)


def signal_set_event(event: threading.Event) -> Callable:
    def _signal_set_event(signum: Any, frame: Any) -> None:
        event.set()

    return _signal_set_event


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cog HTTP server")
    parser.add_argument(
        "--threads",
        dest="threads",
        type=int,
        default=None,
        help="Number of worker processes. Defaults to number of CPUs, or 1 if using a GPU.",
    )
    parser.add_argument(
        "--upload-url",
        dest="upload_url",
        type=str,
        default=None,
        help="An endpoint for Cog to PUT output files to",
    )
    parser.add_argument(
        "--await-explicit-shutdown",
        dest="await_explicit_shutdown",
        type=bool,
        default=False,
        help="Ignore SIGTERM and wait for a request to /shutdown (or a SIGINT) before exiting",
    )
    args = parser.parse_args()

    # log level is configurable so we can make it quiet or verbose for `cog predict`
    # cog predict --debug       # -> debug
    # cog predict               # -> warning
    # docker run <image-name>   # -> info (default)
    log_level = logging.getLevelName(os.environ.get("COG_LOG_LEVEL", "INFO").upper())
    setup_logging(log_level=log_level)

    config = load_config()

    threads = args.threads
    if threads is None:
        if config.get("build", {}).get("gpu", False):
            threads = 1
        else:
            threads = os.cpu_count()

    shutdown_event = threading.Event()
    predictor_ref = get_predictor_ref(config)
    app = create_app(
        predictor_ref=predictor_ref,
        shutdown_event=shutdown_event,
        threads=threads,
        upload_url=args.upload_url,
    )

    port = int(os.getenv("PORT", 5000))
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,
        # This is the default, but to be explicit: only run a single worker
        workers=1,
    )

    if args.await_explicit_shutdown:
        signal.signal(signal.SIGTERM, signal_ignore)
    else:
        signal.signal(signal.SIGTERM, signal_set_event(shutdown_event))

    s = Server(config=server_config)
    s.start()

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        pass

    s.stop()
