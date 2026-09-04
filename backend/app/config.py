"""
Application configuration.

All environment-dependent values (DB connection, DB name) live here, in
one typed place, so nothing in the rest of the app reads os.environ
directly. Values have sane local-development defaults so the app can run
without any .env file present, but are overridable via environment
variables for real deployment.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "groww_watchlist"

    # Kept here (not hardcoded in the polling loop, which doesn't exist
    # yet) so Phase 3 can read it from the same place as everything else.
    poll_interval_seconds: int = 60


settings = Settings()
