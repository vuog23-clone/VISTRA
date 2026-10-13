"""Redact credentials from retrieval progress and CLI errors."""

import os
import re
import unicodedata
from dotenv import dotenv_values
from src.utils.utils import PROJECT_ROOT


KEY_NAMES = ("OPENAI_API_KEY", "JEV_API_KEY", "TYPESAFE_API_KEY", "ORS_API_KEY",
             "GEOAPIFY_API_KEY", "VISTRA_API_KEY", "POSTGRES_PASSWORD", "POSTGRES_DSN")
SECRET_NAME = re.compile(r"(?:API[_-]?KEY|PASSWORD|SECRET|TOKEN|DSN|AUTHORIZATION)$", re.I)


KEY_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")


ASSIGNMENT = re.compile(r"(?i)\b(?:[A-Z_]*(?:API_KEY|PASSWORD|SECRET|TOKEN|DSN)|authorization|api[_ -]?key)\s*[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?[^\s\"',;]{4,}")
DSN_PATTERN = re.compile(r"(?i)\bpostgres(?:ql)?://[^\s/@:]+:[^\s/@]+@[^\s\"']+")


class CredentialRedactor:
    def __init__(self, *, extra_secrets=()):
        values = dotenv_values(PROJECT_ROOT / ".env") if (PROJECT_ROOT / ".env").is_file() else {}
        names = set(KEY_NAMES) | {key for key in {*values, *os.environ} if SECRET_NAME.search(key)}
        # Include both sources: a process override must not leave the .env value exposed.
        self._secrets = sorted(
            {str(value) for key in names for value in (os.getenv(key), values.get(key))
                if value is not None and len(str(value)) >= 4}
            | {str(value) for value in extra_secrets if value is not None and len(str(value)) >= 4},
            key=len,
            reverse=True)

    def text(
        self, 
        value
    ):
        value = str(value)
        for secret in self._secrets:
            value = value.replace(secret, "[credential redacted]")
        value = DSN_PATTERN.sub("[credential redacted]", value)
        value = ASSIGNMENT.sub("[credential redacted]", KEY_PATTERN.sub("[credential redacted]", value))
        # Prevent terminal escape/control injection in progress and error output.
        return "".join(char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159
                            and unicodedata.category(char) != "Cs"
                            and (unicodedata.category(char) != "Cf" or char == "\u200d"))
                       else "�" for char in value)

    def data(
        self, 
        value
    ):
        if isinstance(value, str):
            return self.text(value)
        
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.data(item)
                    for key, item in value.items()}
        
        if isinstance(value, (list, tuple)):
            return [self.data(item) for item in value]
        
        return value
