import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import settings
from drivers.base import SafetyLimitError
from drivers.detector import Photodetector
from drivers.laser import TunableLaser
from service.storage import RunStorage
from service.sweep import BenchController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class SweepRequest(BaseModel):
    start_nm: float = Field(..., description="Start wavelength in nm (1500-1600)")
    stop_nm: float = Field(..., description="Stop wavelength in nm (1500-1600)")
    step_nm: float = Field(..., gt=0, description="Step size in nm (> 0)")
    operator: str = Field(default="researcher", description="User or script identifier")
    notes: str = Field(default="", description="Experimental context or sample notes")


class FaultConfigUpdate(BaseModel):
    laser_timeout_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    laser_corrupt_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    detector_timeout_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    detector_corrupt_rate: float | None = Field(default=None, ge=0.0, le=1.0)


def create_app(controller: BenchController | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if controller is not None:
            yield
        else:
            laser = TunableLaser(
                timeout_rate=settings.laser_timeout_rate,
                corrupt_rate=settings.laser_corrupt_rate,
            )
            detector = Photodetector(
                optical_source=laser,
                resonance_nm=settings.resonance_nm,
                timeout_rate=settings.detector_timeout_rate,
                corrupt_rate=settings.detector_corrupt_rate,
            )
            storage = RunStorage(settings.db_path)
            ctrl = BenchController(
                laser=laser,
                detector=detector,
                storage=storage,
                call_timeout_s=settings.hardware_call_timeout_s,
                max_retries=settings.max_retries_per_point,
            )
            app.state.controller = ctrl
            yield
            ctrl.shutdown()

    app = FastAPI(title="Photonics Bench Instrument Service", version="0.1.0", lifespan=lifespan)
    if controller is not None:
        app.state.controller = controller

    def get_ctrl(request: Request) -> BenchController:
        return request.app.state.controller

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ctrl = get_ctrl(request)
        return {
            "status": "ok",
            "active_run_id": ctrl.active_run_id,
            "instruments": ctrl.get_instrument_states(),
        }

    @app.get("/instruments")
    async def list_instruments(request: Request) -> list[dict[str, Any]]:
        return get_ctrl(request).get_instrument_states()

    @app.post("/instruments/fault-rates")
    async def update_fault_rates(payload: FaultConfigUpdate, request: Request) -> dict[str, float]:
        ctrl = get_ctrl(request)
        if payload.laser_timeout_rate is not None:
            ctrl.laser_driver.timeout_rate = payload.laser_timeout_rate
        if payload.laser_corrupt_rate is not None:
            ctrl.laser_driver.corrupt_rate = payload.laser_corrupt_rate
        if payload.detector_timeout_rate is not None:
            ctrl.detector_driver.timeout_rate = payload.detector_timeout_rate
        if payload.detector_corrupt_rate is not None:
            ctrl.detector_driver.corrupt_rate = payload.detector_corrupt_rate
        return {
            "laser_timeout_rate": ctrl.laser_driver.timeout_rate,
            "laser_corrupt_rate": ctrl.laser_driver.corrupt_rate,
            "detector_timeout_rate": ctrl.detector_driver.timeout_rate,
            "detector_corrupt_rate": ctrl.detector_driver.corrupt_rate,
        }

    @app.post("/sweeps")
    async def start_sweep(payload: SweepRequest, request: Request) -> StreamingResponse:
        ctrl = get_ctrl(request)
        try:
            ctrl.validate_sweep_bounds(payload.start_nm, payload.stop_nm, payload.step_nm)
        except (SafetyLimitError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if ctrl._sweep_lock.locked():
            raise HTTPException(
                status_code=409,
                detail=f"Bench is currently busy with active sweep {ctrl.active_run_id}",
            )

        return StreamingResponse(
            ctrl.run_sweep_stream(
                start_nm=payload.start_nm,
                stop_nm=payload.stop_nm,
                step_nm=payload.step_nm,
                operator=payload.operator,
                notes=payload.notes,
            ),
            media_type="application/x-ndjson",
        )

    @app.get("/sweeps")
    async def list_sweeps(request: Request, limit: int = 25) -> list[dict[str, Any]]:
        return get_ctrl(request).storage.list_runs(limit=limit)

    @app.get("/sweeps/{run_id}")
    async def get_sweep(run_id: str, request: Request) -> dict[str, Any]:
        run = get_ctrl(request).storage.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        return run

    return app


app = create_app()
