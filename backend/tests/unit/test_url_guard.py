import pytest

from secagent.security.url_guard import BlockedUrl, UrlGuard


def test_guard_blocks_private_loopback_and_metadata_addresses() -> None:
    guard = UrlGuard(allowed_hosts=set(), resolver=lambda host: ["127.0.0.1"])
    with pytest.raises(BlockedUrl):
        guard.check("http://example.test/")
    for url in (
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ):
        with pytest.raises(BlockedUrl):
            guard.check(url)


def test_explicit_demo_host_is_allowed_but_not_arbitrary_private_host() -> None:
    guard = UrlGuard(
        allowed_hosts={"web-demo"}, resolver=lambda host: ["172.20.0.10"]
    )
    assert guard.check("http://web-demo/").hostname == "web-demo"
    with pytest.raises(BlockedUrl):
        guard.check("http://internal-admin/")
