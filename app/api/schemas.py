from pydantic import BaseModel, ConfigDict, Field, field_validator


class CueResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    position: int
    source_index: str
    start_ms: int
    end_ms: int
    text: str
    status: str
    warnings: list[str] = Field(default_factory=list)
    inference_text: str | None = None


class CuePageResponse(BaseModel):
    items: list[CueResponse]
    offset: int
    limit: int
    total: int


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    filename: str
    status: str
    total_cues: int
    completed_cues: int
    counts: dict[str, int]


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


class GlossaryResponse(BaseModel):
    rules: list["GlossaryRule"]
    revision: int


class StartRequest(BaseModel):
    voice: str = "th-TH-NiwatNeural"
    tts_rate: int = Field(default=0, ge=-50, le=50)


class VoiceSettings(BaseModel):
    voice: str = "th-TH-NiwatNeural"
    tts_rate: int = Field(default=0, ge=-50, le=50)


class LocalSettings(BaseModel):
    max_start_delay_ms: int = Field(default=2000, ge=0, le=5000)
    voice: str = "th-TH-NiwatNeural"
    tts_rate: int = Field(default=0, ge=-50, le=50)


class TranslationPromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)


class RetryCueRequest(BaseModel):
    duration_mode: str = "normal"

    @field_validator("duration_mode")
    @classmethod
    def supported_mode(cls, value: str) -> str:
        if value not in {"normal", "longer"}:
            raise ValueError("duration_mode ต้องเป็น normal หรือ longer")
        return value


class GlossaryRule(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    spoken: str = Field(min_length=1, max_length=200)

    @field_validator("source", "spoken")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("คำใน glossary ห้ามว่าง")
        return value


class GlossaryRequest(BaseModel):
    rules: list[GlossaryRule] = Field(default_factory=list, max_length=500)

    @field_validator("rules")
    @classmethod
    def unique_sources(cls, value: list[GlossaryRule]) -> list[GlossaryRule]:
        sources = [rule.source for rule in value]
        if len(sources) != len(set(sources)):
            raise ValueError("source ใน glossary ห้ามซ้ำ")
        return value
