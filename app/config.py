"""Settings loaded from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # WhatsApp Cloud API
    verify_token: str = field(default_factory=lambda: _env("WA_VERIFY_TOKEN", "demo-verify-token"))
    app_secret: str = field(default_factory=lambda: _env("WA_APP_SECRET"))
    access_token: str = field(default_factory=lambda: _env("WA_ACCESS_TOKEN"))
    phone_number_id: str = field(default_factory=lambda: _env("WA_PHONE_NUMBER_ID"))
    graph_version: str = field(default_factory=lambda: _env("WA_GRAPH_VERSION", "v21.0"))
    reminder_template: str = field(default_factory=lambda: _env("WA_REMINDER_TEMPLATE", "appointment_reminder"))
    template_lang: str = field(default_factory=lambda: _env("WA_TEMPLATE_LANG", "en"))

    # Bot behaviour
    bot_mode: str = field(default_factory=lambda: _env("BOT_MODE", "booking"))  # booking | ai
    clinic_name: str = field(default_factory=lambda: _env("CLINIC_NAME", "Smile Dental Clinic"))
    slot_hold_minutes: int = field(default_factory=lambda: int(_env("SLOT_HOLD_MINUTES", "5")))
    run_scheduler: bool = field(default_factory=lambda: _env("RUN_SCHEDULER", "1") == "1")

    # AI agent
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))

    # Voice agent (Twilio Programmable Voice)
    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_from_number: str = field(default_factory=lambda: _env("TWILIO_FROM_NUMBER"))
    clinic_transfer_number: str = field(default_factory=lambda: _env("CLINIC_TRANSFER_NUMBER"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL"))

    # CRM
    crm_webhook_url: str = field(default_factory=lambda: _env("CRM_WEBHOOK_URL"))

    # Storage
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./demo.db"))

    @property
    def cloud_api_enabled(self) -> bool:
        return bool(self.access_token and self.phone_number_id)


settings = Settings()
