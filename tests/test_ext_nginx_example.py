import re
from pathlib import Path


def test_nginx_example_matches_unlimited_archive_import_contract():
    config = Path(__file__).parents[1] / "deploy/extension/nginx.conf.example"
    directives = re.findall(
        r"(?m)^\s*client_max_body_size\s+([^;]+);", config.read_text(encoding="utf-8")
    )

    assert directives == ["0"]
