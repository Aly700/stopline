"""Target factories; importing the mock setup does not require Kubernetes."""

from collections.abc import Mapping

from stopline.contract import ROUTER_URL, GateAPI, Sender, TargetAdapter


def build_targets(cluster: str = 'kind', router_url: str = ROUTER_URL) -> dict[str, TargetAdapter]:
    from .router import RouterAdapter

    if cluster == 'kind':
        from .kube import KubeCluster
        adapter = KubeCluster()
    elif cluster == 'mock':
        from .mock import MockCluster
        adapter = MockCluster()
    else:
        raise ValueError(f'unknown cluster: {cluster}')
    return {'cluster': adapter, 'router': RouterAdapter(router_url)}


def build_sender(gate: GateAPI, targets: Mapping[str, TargetAdapter], sender_id: str = 'sender') -> Sender:
    from .sender import TargetSender
    return TargetSender(gate, targets, sender_id)


def reset_all(targets: Mapping[str, TargetAdapter]) -> None:
    """Call between runs, after workers and outstanding sends have stopped."""
    for target in targets.values():
        target.reset()
