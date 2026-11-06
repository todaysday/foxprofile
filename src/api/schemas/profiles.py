from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    proxy: StrictStr | None = None
    os_type: StrictStr | None = None
    timezone: StrictStr | None = Field(
        None, description="IANA timezone, e.g. Asia/Ho_Chi_Minh. Unset = follow the IP"
    )
    locale: StrictStr | None = Field(
        None, description="language-REGION, e.g. vi-VN. Unset = follow the IP"
    )
    engine: StrictStr = Field(
        "camoufox",
        description="camoufox (Firefox) or chrome (Chromium, fingerprint-chromium). "
        "Fixed once the profile is created",
    )


class ProfileUpdate(BaseModel):
    """All fields optional — only supplied fields are changed."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None = None
    proxy: StrictStr | None = None
    os_type: StrictStr | None = None
    timezone: StrictStr | None = Field(None, description='"" switches back to automatic')
    locale: StrictStr | None = Field(None, description='"" switches back to automatic')
    engine: StrictStr | None = Field(None, description="Cannot be changed; only the current value")


class ProfileResponse(BaseModel):
    name: str
    proxy: str | None
    os_type: str
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    data_dir: str
    is_running: bool


class ProfileListResponse(BaseModel):
    profiles: list[ProfileResponse]
    total: int


class DataDirResponse(BaseModel):
    name: str
    data_dir: str
    exists: bool


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_dir: StrictStr | None = None
    include_data: StrictBool = True


class ExportResponse(BaseModel):
    success: bool
    zip_path: str | None = None
    error: str | None = None


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zip_path: StrictStr
    overwrite: StrictBool = False


class ImportResponse(BaseModel):
    success: bool
    profile_name: str | None = None
    error: str | None = None


class CookieImportRequest(BaseModel):
    """Cookies as JSON (Cookie-Editor / EditThisCookie / Playwright) or Netscape text."""

    model_config = ConfigDict(extra="forbid")

    content: StrictStr


class CookieImportResponse(BaseModel):
    success: bool
    imported: int = 0
    error: str | None = None


class FingerprintResponse(BaseModel):
    name: str
    exists: bool
    os: str | None = None
    platform: str | None = None
    screen: str | None = None
    hardware_concurrency: int | None = None
