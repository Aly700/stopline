"""MongoDB authority gate."""
from pymongo.database import Database

from stopline.contract import GateAPI
from .core import Gate


def build_gate(db: Database) -> GateAPI:
    return Gate(db)


__all__ = ["Gate", "build_gate"]
