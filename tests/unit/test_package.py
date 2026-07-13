import sentry_jaga


def test_package_exposes_version() -> None:
    assert sentry_jaga.__version__ == "0.1.0"
