"""Configuration for a multi-account deployment."""

from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    public_url: AnyHttpUrl
    database_url: SecretStr = Field(validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"))
    encryption_key: SecretStr
    xingzhe_client_id: str
    xingzhe_client_secret: SecretStr

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: SecretStr) -> SecretStr:
        # The Supabase Vercel integration adds a client label that libpq does not recognize.
        parsed = urlsplit(value.get_secret_value())
        query = [(key, val) for key, val in parse_qsl(parsed.query) if key != "supa"]
        return SecretStr(urlunsplit(parsed._replace(query=urlencode(query))))

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        url = urlsplit(str(self.public_url))
        if url.path not in ("", "/") or url.query or url.fragment or url.username:
            raise ValueError("PUBLIC_URL must be an origin without a path or credentials")
        if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("PUBLIC_URL must use HTTPS outside localhost")
        Fernet(self.encryption_key.get_secret_value().encode())
        return self

    @property
    def origin(self) -> str:
        return str(self.public_url).rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.origin}/mcp"
