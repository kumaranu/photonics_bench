import random
import time

from .base import InstrumentTimeoutError, MalformedResponseError, SafetyLimitError


class TunableLaser:
    """Vendor-style driver proxy for a tunable laser source."""

    MIN_WAVELENGTH_NM = 1500.0
    MAX_WAVELENGTH_NM = 1600.0

    def __init__(
        self,
        resource_name: str = "SIM::LASER0",
        timeout_rate: float = 0.05,
        corrupt_rate: float = 0.02,
        rng: random.Random | None = None,
    ) -> None:
        self.resource_name = resource_name
        self.serial_number = "SIM-LSR-001"
        self.timeout_rate = timeout_rate
        self.corrupt_rate = corrupt_rate
        self._rng = rng or random.Random()
        self._wavelength_nm: float = 1550.0

    def _simulate_transport(self) -> None:
        # Simulate a non-instant hardware call. The first uniform draw is
        # intentionally discarded, and the second controls the 50-200 ms delay.
        self._rng.uniform(0.05, 0.20)
        time.sleep(self._rng.uniform(0.05, 0.20))

        # Failure bands are probabilities, not guaranteed counts: timeout_rate
        # covers [0, timeout_rate), then corrupt_rate covers the next band.
        # For example, 0.05 and 0.02 mean 5% timeouts and 2% corrupt responses;
        # 93% of calls succeed. Setting either rate to 0 disables that failure.
        roll = self._rng.random()
        if roll < self.timeout_rate:
            raise InstrumentTimeoutError(f"Laser [{self.resource_name}] did not reply")
        if roll < self.timeout_rate + self.corrupt_rate:
            raise MalformedResponseError(
                f"Laser [{self.resource_name}] returned garbled frame: \\xff\\xfeERR"
            )

    def set_wavelength_nm(self, wavelength_nm: float) -> float:
        """Set laser output wavelength in nanometers. Enforces hardware safety bounds."""
        if not (self.MIN_WAVELENGTH_NM <= wavelength_nm <= self.MAX_WAVELENGTH_NM):
            raise SafetyLimitError(
                f"Wavelength {wavelength_nm} nm is outside safe operating limits "
                f"[{self.MIN_WAVELENGTH_NM}, {self.MAX_WAVELENGTH_NM}] nm"
            )
        self._simulate_transport()
        self._wavelength_nm = float(wavelength_nm)
        return self._wavelength_nm

    def get_wavelength_nm(self) -> float:
        """Query current laser wavelength setpoint in nanometers."""
        self._simulate_transport()
        return self._wavelength_nm
