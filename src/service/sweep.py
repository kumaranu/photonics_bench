import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict
from typing import Any

from drivers.base import InstrumentError, SafetyLimitError
from drivers.detector import Photodetector
from drivers.laser import TunableLaser
from service.manager import InstrumentActor
from service.storage import RunStorage

logger = logging.getLogger("bench.sweep")


def generate_wavelength_grid(start_nm: float, stop_nm: float, step_nm: float) -> list[float]:
    if step_nm <= 0:
        raise ValueError("step_nm must be positive")
    if stop_nm < start_nm:
        raise ValueError("stop_nm must be >= start_nm")
    points: list[float] = []
    current = start_nm
    # Epsilon guard against floating-point drift on final point
    while current <= stop_nm + (step_nm * 1e-6):
        points.append(round(current, 6))
        current += step_nm
    return points


class BenchController:
    """Coordinates bench-wide sweep leases, retries, fault tolerance, and persistence."""

    def __init__(
        self,
        laser: TunableLaser,
        detector: Photodetector,
        storage: RunStorage,
        call_timeout_s: float = 1.0,
        max_retries: int = 2,
    ) -> None:
        self.laser_driver = laser
        self.detector_driver = detector
        self.laser = InstrumentActor("laser", laser.resource_name, laser.serial_number, laser, call_timeout_s)
        self.detector = InstrumentActor(
            "detector", detector.resource_name, detector.serial_number, detector, call_timeout_s
        )
        self.storage = storage
        self.max_retries = max_retries

        self._sweep_lock = asyncio.Lock()
        self.active_run_id: str | None = None

    def get_instrument_states(self) -> list[dict[str, Any]]:
        return [asdict(self.laser.snapshot()), asdict(self.detector.snapshot())]

    def validate_sweep_bounds(self, start_nm: float, stop_nm: float, step_nm: float) -> list[float]:
        grid = generate_wavelength_grid(start_nm, stop_nm, step_nm)
        if not grid:
            raise ValueError("Wavelength range produces zero points")
        for wl in (grid[0], grid[-1]):
            if not (TunableLaser.MIN_WAVELENGTH_NM <= wl <= TunableLaser.MAX_WAVELENGTH_NM):
                raise SafetyLimitError(
                    f"Sweep wavelength {wl} nm violates laser safe range "
                    f"[{TunableLaser.MIN_WAVELENGTH_NM}, {TunableLaser.MAX_WAVELENGTH_NM}] nm"
                )
        return grid

    async def _measure_point_with_retry(self, target_nm: float) -> tuple[float | None, float | None, int, str | None]:
        """
        Commands laser and reads detector with bounded retries.
        Never lets a single flaky reading crash the entire sweep.
        """
        last_err: str | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                actual_nm = await self.laser.call(self.laser_driver.set_wavelength_nm, target_nm)
                power_dbm = await self.detector.call(self.detector_driver.read_power_dbm)
                return actual_nm, power_dbm, attempt, None
            except SafetyLimitError:
                # Never retry safety violations
                raise
            except (InstrumentError, asyncio.TimeoutError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                logger.warning("Point %.4f nm attempt %d failed: %s", target_nm, attempt, last_err)
        return None, None, self.max_retries + 1, last_err

    async def run_sweep_stream(
        self,
        start_nm: float,
        stop_nm: float,
        step_nm: float,
        operator: str = "anonymous",
        notes: str = "",
    ) -> AsyncGenerator[str, None]:
        """
        Acquires exclusive bench lease and yields NDJSON lines as readings arrive.
        Raises RuntimeError if a sweep is already active.
        """
        if self._sweep_lock.locked():
            raise RuntimeError(f"Bench is busy running sweep {self.active_run_id}")

        grid = self.validate_sweep_bounds(start_nm, stop_nm, step_nm)

        async with self._sweep_lock:
            run_id = str(uuid.uuid4())
            self.active_run_id = run_id
            fault_cfg = {
                "laser_timeout_rate": self.laser_driver.timeout_rate,
                "laser_corrupt_rate": self.laser_driver.corrupt_rate,
                "detector_timeout_rate": self.detector_driver.timeout_rate,
                "detector_corrupt_rate": self.detector_driver.corrupt_rate,
            }
            record = self.storage.create_run(
                run_id=run_id,
                operator=operator,
                notes=notes,
                start_nm=start_nm,
                stop_nm=stop_nm,
                step_nm=step_nm,
                retries_per_point=self.max_retries,
                instruments={i["name"]: i for i in self.get_instrument_states()},
                fault_config=fault_cfg,
            )

            logger.info("Started sweep %s (%.3f -> %.3f nm, step %.3f nm)", run_id, start_nm, stop_nm, step_nm)
            yield json.dumps({"type": "started", "run_id": run_id, "total_points": len(grid), "created_at": record.created_at}) + "\n"

            failed_points = 0
            final_status = "completed"

            try:
                for idx, target_nm in enumerate(grid):
                    actual_nm, power_dbm, attempts, error_msg = await self._measure_point_with_retry(target_nm)
                    if power_dbm is None:
                        failed_points += 1
                    point = self.storage.append_reading(
                        run_id=run_id,
                        step_index=idx,
                        target_nm=target_nm,
                        actual_nm=actual_nm,
                        power_dbm=power_dbm,
                        attempts=attempts,
                        error_msg=error_msg,
                    )
                    yield json.dumps({"type": "point", "run_id": run_id, **point}) + "\n"

                if failed_points > 0:
                    final_status = "partial"
            except asyncio.CancelledError:
                final_status = "aborted"
                logger.warning("Sweep %s aborted due to client disconnect", run_id)
                raise
            finally:
                self.storage.finish_run(run_id, final_status)
                self.active_run_id = None
                logger.info("Finished sweep %s with status=%s (failed_points=%d)", run_id, final_status, failed_points)

            yield json.dumps(
                {
                    "type": "complete",
                    "run_id": run_id,
                    "status": final_status,
                    "total_points": len(grid),
                    "failed_points": failed_points,
                }
            ) + "\n"

    def shutdown(self) -> None:
        self.laser.shutdown()
        self.detector.shutdown()
