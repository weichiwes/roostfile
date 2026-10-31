"""The application has no network transport, including loopback connections."""
import sys


def _deny_network(event, args):
    if event.startswith("socket."):
        raise PermissionError("Network access is blocked.")


def enforce_offline():
    sys.addaudithook(_deny_network)
