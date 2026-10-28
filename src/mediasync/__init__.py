"""Media-aware one-way sync with approvals, policy files and quarantine."""

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("mediasync")
    except PackageNotFoundError:
        from setuptools_scm import get_version

        __version__ = get_version(root="..", relative_to=__file__)
except Exception:
    __version__ = "0.0.0"
