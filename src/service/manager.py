import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class DeviceStatus(str, Enum):
    CONNECTED = "connected"
    BUSY = "busy"
    UNRESPONSIVE = "unresponsive"


@dataclass
class InstrumentSnapshot:
    name: str
    resource: str
    serial_number: str
    status: DeviceStatus
    last_error: str | None
    last_seen_utc: str | None


class InstrumentActor:
    """
    Wraps a synchronous, blocking hardware driver in a dedicated single-thread executor
    with an async lock. Guarantees that:
      1. Commands to one device are serialized.
      2. A stalled device never blocks the event loop or other instruments.
    """

    def __init__(
        self, name: str, resource: str, serial_number: str, driver: object, timeout_s: float = 1.0
    ) -> None:
        self.name = name
        self.resource = resource
        self.serial_number = serial_number
        self.driver = driver
        self.timeout_s = timeout_s
        self.status = DeviceStatus.CONNECTED
        self.last_error: str | None = None
        self.last_seen_utc: str | None = datetime.now(timezone.utc).isoformat()

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"actor-{name}")
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[..., T], *args) -> T:
        async with self._lock:
            self.status = DeviceStatus.BUSY
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, fn, *args),
                    timeout=self.timeout_s,
                )
                self.status = DeviceStatus.CONNECTED
                self.last_error = None
                self.last_seen_utc = datetime.now(timezone.utc).isoformat()
                return result
            except ValueError as exc:
                # Validation/Safety errors mean the instrument itself is still healthy
                self.status = DeviceStatus.CONNECTED
                self.last_error = str(exc)
                raise
            except Exception as exc:
                self.status = DeviceStatus.UNRESPONSIVE
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise

    def snapshot(self) -> InstrumentSnapshot:
        return InstrumentSnapshot(
            name=self.name,
            resource=self.resource,
            serial_number=self.serial_number,
            status=self.status,
            last_error=self.last_error,
            last_seen_utc=self.last_seen_utc,
        )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
