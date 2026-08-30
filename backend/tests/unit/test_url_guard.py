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


def test_wildcard_allowlist_disables_all_host_restrictions() -> None:
    guard = UrlGuard({"*"})
    assert guard.check("http://127.0.0.1:8080/flag").hostname == "127.0.0.1"
    assert guard.check("http://10.0.0.5:9000/admin").hostname == "10.0.0.5"
    assert guard.check("http://node4.anna.nssctf.cn:25916/secret.php").port == 25916


def test_default_guard_still_blocks_private_targets() -> None:
    guard = UrlGuard({"example.test"})
    import pytest

    with pytest.raises(BlockedUrl):
        guard.check("http://127.0.0.1:8080/flag")
