import asyncio
import random
import time

import pytest
from httpx import ASGITransport, AsyncClient

from drivers.base import SafetyLimitError
from drivers.detector import Photodetector
from drivers.laser import TunableLaser
from service.app import create_app
from service.storage import RunStorage
from service.sweep import BenchController


def test_laser_rejects_out_of_range_before_io():
    laser = TunableLaser(timeout_rate=0.0, corrupt_rate=0.0)
    with pytest.raises(SafetyLimitError):
        laser.set_wavelength_nm(1499.99)
    with pytest.raises(SafetyLimitError):
        laser.set_wavelength_nm(1600.01)
    # State must remain untouched at initial 1550.0 nm
    assert laser.get_wavelength_nm() == 1550.0


def test_detector_shows_resonance_dip():
    rng = random.Random(42)
    laser = TunableLaser(timeout_rate=0.0, corrupt_rate=0.0, rng=rng)
    det = Photodetector(
        optical_source=laser, resonance_nm=1550.0, timeout_rate=0.0, corrupt_rate=0.0, rng=rng
    )

    # At 1548 nm, the laser is 2 nm away from the resonance. The detector
    # remains at its hard-coded -10 dBm baseline with only a small Lorentzian
    # dip, plus the seeded Gaussian noise.
    laser.set_wavelength_nm(1548.0)
    off_resonance_dbm = det.read_power_dbm()

    # At 1550 nm, the wavelength exactly matches the configured resonance.
    # The detector model currently hard-codes a 15 dB dip, so the expected
    # power is the -10 dBm baseline minus 15 dB, or approximately -25 dBm.
    laser.set_wavelength_nm(1550.0)
    on_resonance_dbm = det.read_power_dbm()

    # Approximation accounts for the detector's small simulated Gaussian noise.
    # Each assertion allows a 0.5 dBm difference from the hard-coded expected
    # values, rather than requiring exact floating-point equality.
    assert off_resonance_dbm == pytest.approx(-10.0, abs=0.5)
    assert on_resonance_dbm == pytest.approx(-25.0, abs=0.5)


async def test_stalled_detector_does_not_block_laser_or_health_endpoint(tmp_path):
    # This subclass replaces the real simulated detector read with a deliberately
    # blocking method. It lets the test isolate thread/actor isolation without
    # depending on random transport failures.
    class HangingDetector(Photodetector):
        def read_power_dbm(self) -> float:
            time.sleep(2.0)  # Exceeds service call_timeout_s of 0.25s
            return -10.0

    # Disable laser transport failures and give each instrument call only 250 ms.
    # max_retries=0 makes the sweep use exactly one measurement attempt.
    laser = TunableLaser(timeout_rate=0.0, corrupt_rate=0.0)
    det = HangingDetector(optical_source=laser)
    storage = RunStorage(str(tmp_path / "test.db"))
    ctrl = BenchController(
        laser=laser, detector=det, storage=storage, call_timeout_s=0.25, max_retries=0
    )
    app = create_app(controller=ctrl)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # create_task starts the HTTP request without waiting for its response.
        # The start=stop=step combination creates one 1550 nm sweep point.
        sweep_task = asyncio.create_task(
            client.post("/sweeps", json={"start_nm": 1550.0, "stop_nm": 1550.0, "step_nm": 1.0})
        )

        # Allow the sweep task to reach the laser and enter the blocked detector
        # call. This is a synchronization delay; the remaining assertions prove
        # that the system remains responsive while the detector is stalled.
        await asyncio.sleep(0.08)

        # The health endpoint should read actor snapshots rather than waiting on
        # hardware. monotonic measures elapsed time independently of wall-clock
        # changes and the sub-200 ms threshold leaves room for normal scheduling.
        t0 = time.monotonic()
        health_resp = await client.get("/health")
        elapsed = time.monotonic() - t0
        assert health_resp.status_code == 200
        assert elapsed < 0.2

        # The laser has a separate executor and asyncio lock from the detector.
        # It should still accept a wavelength query while the detector's worker
        # thread is sleeping. This also confirms the 1550 nm setpoint succeeded.
        wl = await ctrl.laser.call(laser.get_wavelength_nm)
        assert wl == 1550.0

        # By awaiting the sweep task now, the test waits for the detector actor's
        # 0.25-second timeout and the sweep's streaming response to complete.
        # With exactly one failed point and no retries, the run is "partial".
        resp = await sweep_task
        assert resp.status_code == 200
        assert '"status": "partial"' in resp.text

        # Stop the actor executor objects. shutdown(wait=False) does not wait for
        # the original two-second detector sleep to finish in its worker thread.
        ctrl.shutdown()


async def test_concurrent_sweep_returns_409_conflict(tmp_path):
    laser = TunableLaser(timeout_rate=0.0, corrupt_rate=0.0)
    det = Photodetector(optical_source=laser, timeout_rate=0.0, corrupt_rate=0.0)
    storage = RunStorage(str(tmp_path / "conflict.db"))
    ctrl = BenchController(laser=laser, detector=det, storage=storage)
    app = create_app(controller=ctrl)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Sweep with 10 points (~1s total)
        t1 = asyncio.create_task(
            client.post("/sweeps", json={"start_nm": 1549.0, "stop_nm": 1550.0, "step_nm": 0.1})
        )
        await asyncio.sleep(0.1)

        # Second sweep arriving mid-run must get 409 Conflict
        r2 = await client.post(
            "/sweeps", json={"start_nm": 1550.0, "stop_nm": 1551.0, "step_nm": 0.5}
        )
        assert r2.status_code == 409

        r1 = await t1
        assert r1.status_code == 200
        ctrl.shutdown()
