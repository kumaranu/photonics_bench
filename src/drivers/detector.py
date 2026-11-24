import random
import time

from .base import InstrumentTimeoutError, MalformedResponseError
from .laser import TunableLaser


class Photodetector:
    """Vendor-style driver proxy for an optical power meter."""

    MIN_POWER_DBM = -60.0
    MAX_POWER_DBM = 10.0

    def __init__(
        self,
        optical_source: TunableLaser,
        resource_name: str = "SIM::DET0",
        resonance_nm: float = 1550.12,
        timeout_rate: float = 0.05,
        corrupt_rate: float = 0.02,
        rng: random.Random | None = None,
    ) -> None:
        self.resource_name = resource_name
        self.serial_number = "SIM-DET-001"
        self._source = optical_source
        self.resonance_nm = resonance_nm
        self.timeout_rate = timeout_rate
        self.corrupt_rate = corrupt_rate
        self._rng = rng or random.Random()

    def _simulate_transport(self) -> None:
        # Simulate a non-instant hardware call with a random 50-200 ms delay.
        time.sleep(self._rng.uniform(0.05, 0.20))

        # Failure bands are probabilities, not guaranteed counts: timeout_rate
        # covers [0, timeout_rate), then corrupt_rate covers the next band.
        # For example, 0.05 and 0.02 mean 5% timeouts and 2% corrupt responses;
        # 93% of calls succeed. Setting either rate to 0 disables that failure.
        roll = self._rng.random()
        if roll < self.timeout_rate:
            raise InstrumentTimeoutError(f"Detector [{self.resource_name}] timed out")
        if roll < self.timeout_rate + self.corrupt_rate:
            raise MalformedResponseError(
                f"Detector [{self.resource_name}] unparseable payload: NAN_#?"
            )

    def read_power_dbm(self) -> float:
        """Read optical power in dBm. Models a flat baseline with a Lorentzian resonance dip."""
        self._simulate_transport()

        # Read the true physical state of the coupled optical source
        wl = self._source._wavelength_nm

        # These are hard-coded simulation parameters for now:
        # - baseline_dbm is the power far from resonance;
        # - dip_depth_db is the maximum loss at the resonance;
        # - half_width_nm defines the 0.15 nm FWHM resonance width.
        baseline_dbm = -10.0
        dip_depth_db = 15.0
        half_width_nm = 0.075

        # Lorentzian is 1 at the exact resonance, producing the full 15 dB
        # dip, and approaches 0 as the wavelength moves farther away.
        lorentzian = dip_depth_db * (
            half_width_nm**2 / ((wl - self.resonance_nm) ** 2 + half_width_nm**2)
        )

        # Add small Gaussian measurement noise. For the current simulated
        # model, readings remain inside the class-level -60 to 10 dBm limits.
        noise = self._rng.gauss(0.0, 0.08)

        # Clamp defensively so future model changes cannot return values outside
        # the detector range. The MIN/MAX bounds are not reached by this model's
        # normal -10 dBm baseline and 15 dB maximum dip.
        reading = max(
            self.MIN_POWER_DBM, min(self.MAX_POWER_DBM, baseline_dbm - lorentzian + noise)
        )
        return round(reading, 4)
