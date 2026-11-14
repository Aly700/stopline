"""MongoDB authority gate."""
from .core import Gate


def build_gate(db):
    return Gate(db)


__all__ = ["Gate", "build_gate"]
