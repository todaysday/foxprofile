from dataclasses import asdict, dataclass
from uuid import uuid4

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    # Local catalog identity. Portable profile archives deliberately omit it.
    profile_id: str = ""

    def __post_init__(self) -> None:
        if not self.profile_id:
            self.profile_id = str(uuid4())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_portable_dict(self) -> dict:
        data = asdict(self)
        data.pop("profile_id", None)
        return data
