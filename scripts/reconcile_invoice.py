"""Compatibility entry point for source checkouts; the packaged CLI is gateway.reconcile."""

from gateway.reconcile import main, reconcile  # noqa: F401

if __name__ == "__main__":
    main()
