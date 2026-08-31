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
    action: str
    cue_id: int | None = None
    nfe_step: int | None = None

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
        }
        if value not in allowed:
            raise ValueError("action ไม่รองรับ")
        return value

    @field_validator("nfe_step")
    @classmethod
    def supported_nfe(cls, value: int | None) -> int | None:
        if value is not None and value not in {16, 32}:
            raise ValueError("nfe_step ต้องเป็น 16 หรือ 32")
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
    voice_profile_id: str
    nfe_step: int = 32
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("nfe_step")
    @classmethod
    def supported_nfe(cls, value: int) -> int:
        if value not in {16, 32}:
            raise ValueError("nfe_step ต้องเป็น 16 หรือ 32")
        return value


class LocalSettings(BaseModel):
    nfe_step: int = 32
    inference_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    max_start_delay_ms: int = Field(default=2000, ge=0, le=5000)
    allow_cpu: bool = True

    @field_validator("nfe_step")
    @classmethod
    def supported_nfe(cls, value: int) -> int:
        if value not in {16, 32}:
            raise ValueError("nfe_step ต้องเป็น 16 หรือ 32")
        return value


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
