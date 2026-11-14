"""The gate's immutable policy and audit record validator."""
import hashlib

from stopline.contract import AuditKind, Reason

POLICY_VERSION = 1
POLICY_TEXT = (
    "Authority follows an active, unexpired mandate chain. Tools and targets must be "
    "within every ancestor's scope. Child action budgets sum to no more than the "
    "parent budget; every admission consumes each ancestor's shared action budget. "
    "Traffic shifts obey every ancestor's percentage ceiling. Hand-down, admission "
    "and stop serialize on the root counter. Stop cancels unsent actions; a receipt "
    "closes only after every covered action has a terminal outcome."
)
POLICY_HASH = hashlib.sha256(POLICY_TEXT.encode()).hexdigest()

AUDIT_VALIDATOR = {"$jsonSchema": {
    "bsonType": "object",
    "required": ["_id", "run_id", "kind", "principal", "mandate_id", "root_id", "action_id",
                 "policy_version", "policy_hash", "reason", "seq", "at", "detail"],
    "additionalProperties": False,
    "properties": {
        "_id": {},
        "run_id": {"bsonType": "string"},
        "kind": {"enum": [str(item) for item in AuditKind]},
        "principal": {"bsonType": "string"},
        "mandate_id": {"bsonType": ["string", "null"]},
        "root_id": {"bsonType": ["string", "null"]},
        "action_id": {"bsonType": ["string", "null"]},
        "policy_version": {"bsonType": ["int", "long"], "minimum": 1},
        "policy_hash": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
        "reason": {"enum": [str(item) for item in Reason]},
        "seq": {"bsonType": ["int", "long", "null"]},
        "at": {"bsonType": "date"},
        "detail": {"bsonType": "object"},
    },
}}

_ID = {"bsonType": "string", "minLength": 1}
_OK = {"enum": [str(Reason.OK)]}
_DONE = {"reason": _OK, "mandate_id": _ID, "root_id": _ID, "run_id": _ID, "principal": _ID,
         "seq": {"bsonType": ["int", "long"], "minimum": 0}}
AUDIT_VALIDATOR["$jsonSchema"]["oneOf"] = [
    {"properties": {"kind": {"enum": ["allow", "claim", "outcome", "undo"]}, "action_id": _ID, **_DONE}},
    {"properties": {"kind": {"enum": ["hand_down", "stop"]}, "action_id": {"bsonType": "null"}, **_DONE}},
    {"properties": {"kind": {"enum": ["deny"]}, "reason": {"not": _OK}}},
]
