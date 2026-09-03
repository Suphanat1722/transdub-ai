from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    cue_id: int | None = None

    @field_validator("action")
    @classmethod
    def supported_action(cls, value: str) -> str:
        allowed = {
            "pause",
            "resume",
            "retry",
            "cancel",
            "approve_transcript",
            "approve_translation",
            "remux",
            "regenerate_cue",
            "retranslate",
        }
        if value not in allowed:
            raise ValueError("action ไม่รองรับ")
        return value


class CueEditRequest(BaseModel):
    layer: str
    text: str = Field(min_length=1, max_length=20_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @field_validator("layer")
    @classmethod
    def supported_layer(cls, value: str) -> str:
        if value not in {"source", "translation"}:
            raise ValueError("layer ต้องเป็น source หรือ translation")
        return value


class TranslationPromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)


class JobSettingsRequest(BaseModel):
    """Optional job-level settings changed after creation.

    Changing ``voice`` or ``tts_rate`` invalidates generated cue audio so the
    worker re-synthesizes every cue; volumes and output_dir take effect on the
    next mux without any regeneration.
    """

    voice: str | None = Field(default=None, min_length=1, max_length=200)
    tts_rate: int | None = Field(default=None, ge=-50, le=50)
    background_volume: float | None = Field(default=None, ge=0, le=150)
    voice_volume: float | None = Field(default=None, ge=0, le=150)
    output_dir: str | None = Field(default=None, max_length=500)


class LocalSettings(BaseModel):
    max_start_delay_ms: int = Field(default=2000, ge=0, le=5000)
    voice: str = "th-TH-NiwatNeural"
    tts_rate: int = Field(default=0, ge=-50, le=50)


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)


class FolderCheckRequest(BaseModel):
    path: str = Field(default="", max_length=500)
