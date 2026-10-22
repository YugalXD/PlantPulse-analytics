from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://devuser:devpass@localhost:5432/chp_planner"
    analytics_token: str = "change-me"
    sentry_dsn: str = ""  # errors go to Sentry when set


settings = Settings()
