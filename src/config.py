from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    laser_timeout_rate: float = 0.05
    laser_corrupt_rate: float = 0.02
    detector_timeout_rate: float = 0.05
    detector_corrupt_rate: float = 0.02
    resonance_nm: float = 1550.12
    db_path: str = "bench.db"
    hardware_call_timeout_s: float = 1.0
    max_retries_per_point: int = 2


settings = Settings()
