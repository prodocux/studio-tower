from enum import Enum

from pydantic import BaseModel, Field, field_validator


class VFXTier(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    HERO = "hero"


class StuntLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class ResourceRequirement(BaseModel):
    cast: list[str] = Field(default_factory=list, description="Characters required in this scene")
    props: list[str] = Field(default_factory=list, description="Hero props, weapons, vehicles")
    locations: list[str] = Field(default_factory=list, description="Specific set or location requirements")
    vfx_tier: VFXTier = Field(default=VFXTier.NONE, description="VFX complexity level")
    stunt_level: StuntLevel = Field(default=StuntLevel.NONE, description="Stunt safety risk tier")


class ConflictType(str, Enum):
    CASTING_DOUBLE_BOOKING = "casting_double_booking"
    AGE_LINE_CONTINUITY = "age_line_continuity"
    HERO_TECH_OVERALLOCATED = "hero_tech_overallocated"
    LOCATION_OVERLAP = "location_overlap"
    BUDGET_TIER_RISK = "budget_tier_risk"


class ConflictItem(BaseModel):
    conflict_type: ConflictType
    description: str
    severity: str = "warning"  # info, warning, critical
    affected_scenes: list[int] = Field(default_factory=list)


class RiskGateProposal(BaseModel):
    gate_title: str
    description: str
    risk_level: str = "medium"  # low, medium, high, critical
    required_role: str = "Production Coordinator"
    mitigation_notes: str = ""


class SceneItem(BaseModel):
    scene_number: int
    slugline: str
    act: int = 1
    description: str
    resources: ResourceRequirement = Field(default_factory=ResourceRequirement)
    risk_gate: RiskGateProposal | None = None


class SceneBreakdown(BaseModel):
    project_title: str
    summary: str
    scenes: list[SceneItem] = Field(default_factory=list)
    detected_conflicts: list[ConflictItem] = Field(default_factory=list)
    recommended_gates: list[RiskGateProposal] = Field(default_factory=list)


class DeliverableScene(BaseModel):
    scene_number: str
    slugline: str
    day_night: str = "TBD"
    location: str = "TBD"
    characters: list[str] = Field(default_factory=list)
    production_elements: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("scene_number", mode="before")
    @classmethod
    def coerce_scene_number(cls, value: object) -> str:
        return str(value).strip() if value is not None and str(value).strip() else "TBD"


class DeliverableContent(BaseModel):
    title: str = ""
    summary: str = ""
    scenes: list[DeliverableScene] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    stunts_found: list[str] = Field(default_factory=list)
    production_elements: list[str] = Field(default_factory=list)
