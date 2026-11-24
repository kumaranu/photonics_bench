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
        time.sleep(self._rng.uniform(0.05, 0.20))
        roll = self._rng.random()
        if roll < self.timeout_rate:
            raise InstrumentTimeoutError(f"Detector [{self.resource_name}] timed out")
        if roll < self.timeout_rate + self.corrupt_rate:
            raise MalformedResponseError(f"Detector [{self.resource_name}] unparseable payload: NAN_#?")

    def read_power_dbm(self) -> float:
        """Read optical power in dBm. Models a flat baseline with a Lorentzian resonance dip."""
        self._simulate_transport()

        # Read the true physical state of the coupled optical source
        wl = self._source._wavelength_nm
        baseline_dbm = -10.0
        dip_depth_db = 15.0
        half_width_nm = 0.075  # 0.15 nm FWHM

        lorentzian = dip_depth_db * (half_width_nm**2 / ((wl - self.resonance_nm) ** 2 + half_width_nm**2))
        noise = self._rng.gauss(0.0, 0.08)
        reading = max(self.MIN_POWER_DBM, min(self.MAX_POWER_DBM, baseline_dbm - lorentzian + noise))
        return round(reading, 4)
