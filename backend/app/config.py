"""Environment-backed settings for PingPulse."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# D:\pingpulse\.env  — resolved relative to this file so it works from any cwd.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""
    twilio_validate_signature: bool = False

    # LLM providers. Extra keys are optional; each provider rotates through the
    # ones present so a per-key rate limit costs one retry rather than a reply.
    groq_api_key: str = ""
    groq_api_key_2: str = ""
    groq_api_key_3: str = ""
    groq_api_key_4: str = ""
    groq_api_key_5: str = ""

    gemini_api_key: str = ""
    gemini_api_key_2: str = ""
    gemini_api_key_3: str = ""
    gemini_api_key_4: str = ""
    gemini_api_key_5: str = ""

    embedding_model: str = "gemini-embedding-001"
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-1.5-pro"
    llm_timeout_seconds: int = 20

    # Reject a reply that quotes a price absent from the business's price list.
    price_guard_enabled: bool = True

    @property
    def groq_api_keys(self) -> list[str]:
        return self._keys("groq_api_key")

    @property
    def gemini_api_keys(self) -> list[str]:
        return self._keys("gemini_api_key")

    def _keys(self, base: str) -> list[str]:
        """Deduplicated, in declared order, blanks dropped."""
        candidates = [getattr(self, base, "")] + [
            getattr(self, f"{base}_{index}", "") for index in range(2, 6)
        ]
        seen: list[str] = []
        for key in candidates:
            key = (key or "").strip()
            if key and key not in seen:
                seen.append(key)
        return seen

    # Database
    database_url: str = "postgresql+asyncpg://pingpulse:pingpulse@localhost:5433/pingpulse"

    # Which business answers a phone number we have never seen before. All
    # sandbox traffic arrives on one shared number, so there is nothing in the
    # payload to route on — without this the oldest organization silently wins.
    default_organization_id: str = ""

    # WhatsApp Web bridge. Reached over the compose network, never exposed.
    wa_qr_service_url: str = "http://wa-qr-service:3100"
    wa_qr_timeout_seconds: int = 30
    # Shared secret between the API and the bridge, so nothing else on the
    # network can send messages as a tenant.
    wa_qr_shared_secret: str = ""

    # Media. Kept on the D: volume and re-served from PUBLIC_BASE_URL so
    # WhatsApp can fetch what we store.
    media_dir: str = "/app/media"
    public_base_url: str = ""
    max_media_per_message: int = 4

    # Vision
    vision_enabled: bool = True
    vision_model: str = "gemini-3.8-flash"
    # The vision endpoint is rate-limited hard; each round tries every key.
    vision_attempts: int = 2

    # Scheduling / calendar
    scheduling_enabled: bool = True
    calcom_link: str = ""
    calendar_fallback_enabled: bool = True

    # Signed desktop installers the app auto-updates from.
    updates_dir: str = "/app/updates"

    # The dashboard bundle served at /app, for browser users. The same
    # build that ships inside the desktop app; empty means do not serve it.
    web_dir: str = "/app/web"

    # Follow-ups (Celery + Redis)
    redis_url: str = "redis://redis:6379/0"

    # ------------------------------------------------------------ alerts
    # Browser push. VAPID keys are self-signed and self-hosted - there is no
    # account to open and no third party to pay, which is why this is the one
    # alerting channel that works without the client arranging anything.
    #
    # Empty keys mean push is simply off: subscribing returns "not configured"
    # rather than failing, so a deployment without keys is a deployment with
    # one fewer channel rather than a broken settings page.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # Where a push service should complain to. Must be a mailto: or https URL.
    vapid_subject: str = "mailto:support@pingpulse.app"

    # Email, for the same alerts when a browser is closed. Also optional, and
    # deliberately plain SMTP rather than a vendor SDK: any mailbox works.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Where a notification should send somebody when they tap it.
    dashboard_url: str = "https://pingpulse.duckdns.org/app/"

    # Two alerts about the same contact and the same kind of event inside this
    # window collapse into one. A customer sending four angry messages in a row
    # is one situation, not four, and four buzzes is how a person learns to
    # ignore the buzz.
    notify_cooloff_minutes: int = 30

    # How often the outbound retry queue is walked. Short enough that a reply
    # parked during a container restart goes out while the customer is still
    # looking at the chat.
    outbox_drain_seconds: int = 15

    followups_enabled: bool = True
    followup_first_hours: float = 4
    followup_second_hours: float = 24
    # The last nudge. See MAX_FOLLOWUPS in tasks.py for why there is no fourth.
    followup_third_hours: float = 72

    # Auth
    secret_key: str = "change-me-in-production-please-32-chars-min"
    access_token_minutes: int = 60 * 24 * 7

    # App
    app_name: str = "PingPulse"
    app_env: str = "development"
    log_level: str = "INFO"
    backend_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    chat_history_limit: int = 12
    auto_migrate_on_startup: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def whatsapp_from(self) -> str:
        """Twilio expects the sender as `whatsapp:+1555...`."""
        number = self.twilio_whatsapp_number.strip()
        if not number:
            return ""
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"

    @property
    def sync_database_url(self) -> str:
        """psycopg-free sync URL, used only by tooling that cannot await."""
        return self.database_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
