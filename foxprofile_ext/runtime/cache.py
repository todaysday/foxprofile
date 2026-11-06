"""Prepare upstream GeoIP data in the trusted parent; confined runners only read it."""

from pathlib import Path

from ..locking import catalog_lock


def geoip_cache_directory() -> Path:
    """Return the exact upstream cache subtree, never the containing user cache."""
    from camoufox.geolocation import GEOIP_DIR

    directory = Path(GEOIP_DIR)
    if (
        not directory.is_absolute()
        or directory.parts[-2:] != ("camoufox", "geoip")
        or directory != directory.resolve()
    ):
        raise RuntimeError(
            "Protected GeoIP cache must be an absolute real camoufox/geoip directory"
        )
    return directory


def _geoip_config() -> dict:
    from camoufox import geolocation

    directory = geoip_cache_directory()
    for path in (geolocation.GEOIP_CONFIG, geolocation.MMDB_DIR):
        if path.is_symlink() or path.resolve().parent != directory:
            raise RuntimeError("Protected GeoIP cache contains an unsafe path")
    config = geolocation.load_geoip_config()
    for version in ("ipv4", "ipv6"):
        path = geolocation.get_mmdb_path(version, config)
        if path.is_symlink() or path.resolve().parent != directory / "mmdb":
            raise RuntimeError("Protected GeoIP database is outside its cache")
    return config


def _needs_download(config: dict) -> bool:
    from camoufox import geolocation

    return geolocation.needs_update(config) or any(
        not geolocation.get_mmdb_path(version, config).is_file() for version in ("ipv4", "ipv6")
    )


def require_geoip_cache() -> None:
    """Fail before upstream geolocation can try a download inside confinement."""
    from camoufox import geolocation

    geolocation.geoip_allowed()
    config = _geoip_config()
    if _needs_download(config):
        raise RuntimeError(
            "Protected GeoIP cache is missing or stale; relaunch the profile to prepare it "
            "in the trusted parent, or run 'python -m foxprofile_ext.runtime.cache' "
            "as the service user"
        )


def prepare_geoip_cache() -> None:
    """Refresh only on an explicit browser launch, using the active upstream source."""
    from camoufox import geolocation

    geolocation.geoip_allowed()
    with catalog_lock(geoip_cache_directory() / ".prepare.lock"):
        config = _geoip_config()
        if _needs_download(config):
            geolocation.download_mmdb()
        require_geoip_cache()


if __name__ == "__main__":
    prepare_geoip_cache()
