"""The Privacy Readiness Score as the API returns it (app/scoring/ten_checks.py). One shape for the console and the
Consent Agent API."""

from pydantic import BaseModel, Field


class TenCheckModel(BaseModel):
    id: int
    name: str
    result: str = Field(description="Passed | Partially Passed | Failed | Not Assessed | Not Applicable")
    awarded: float | None = Field(description="1, 0.5 or 0; null when the check carries no marks")
    max: float | None = Field(description="1, or null when the check is left out of the maximum")
    evidence: str = Field(description="The text or technical observation the result rests on; empty when there is none")
    source_url: str | None = Field(description="The page or request the evidence came from")
    explanation: str
    legal: str | None = Field(description="The DPDP Act and Rules provisions behind the check")
    completed: bool = Field(description="False when the check could not be run to the end")
    limitation: str | None
    marks_text: str = Field(description='As printed: "1 / 1", "0.5 / 1", "0 / 1", "N/A" or "Not assessed"')
    kind: str = Field(description="dpdp | practice | technical | transparency")
    kind_label: str
    basis: str = Field(description="Why the result is what it is (Verified, Not evidenced, Access failed, ...)")
    quality: str = Field(description="Specific | General | Limited | None | Not assessed | n/a")
    coverage: str | None


class TenCounts(BaseModel):
    passed: int
    partial: int
    failed: int
    not_assessed: int
    not_applicable: int


class TenRecommendation(BaseModel):
    id: int = Field(description="The check it is for; 0 for the advice to re-run a scan whose checks could not all be assessed")
    text: str


class TenPages(BaseModel):
    discovered: int | None
    scanned: int
    failed: int
    skipped: int


class ScoreContext(BaseModel):
    """Facts about the scan the reports print beside the score. None of them changes the score."""
    scanned_at: str | None = Field(description="When the scan finished (ISO 8601)")
    page_limit: int = Field(description="The most pages a web scan visits")
    sector: dict | None = Field(description="What kind of website this appears to be, and the DPDP provisions that depend on it (app/scoring/sector.py)")


class ScoreResponse(BaseModel):
    methodology: str
    score: float | None = Field(description="0 to 100, one decimal, half up; null when no check could be assessed")
    score_text: str
    total_text: str
    awarded: float
    applicable_max: float
    formula: str
    counts: TenCounts
    incomplete: int = Field(description="Checks that could not be assessed (left out of the score, never scored 0)")
    limited_basis: bool = Field(description="True when fewer than five checks could be assessed: the score is provisional")
    summary: str
    checks: list[TenCheckModel]
    recommendations: list[TenRecommendation]
    pages: TenPages
    limitations: list[str]
    context: ScoreContext | None = None
