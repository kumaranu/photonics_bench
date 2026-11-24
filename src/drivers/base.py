class InstrumentError(Exception):
    """Base error for hardware communication failures."""


class SafetyLimitError(ValueError):
    """Raised before hardware I/O when a requested setpoint violates safe limits."""


class InstrumentTimeoutError(InstrumentError):
    """Raised when an instrument fails to respond within its expected window."""


class MalformedResponseError(InstrumentError):
    """Raised when an instrument returns unparseable bytes or framing."""
