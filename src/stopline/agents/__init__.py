"""Repair orchestration and independently restartable workers."""


def build_stage(db, gate, targets, sender, cluster="kind"):
    from .stage import Stage

    return Stage(db, gate, targets, sender, cluster=cluster)


__all__ = ["build_stage"]
