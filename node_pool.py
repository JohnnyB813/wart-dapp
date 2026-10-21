"""Try configured nodes for reads/connection only. Never retries a broadcast."""
import time
from threading import Event
from node_client import NodeClient, NodeError


def probe_nodes(peers, network, cancel=None, require_synced=False, lowest_fee=False):
    cancel = cancel or Event()
    deadline = time.monotonic() + 30
    last_error, readable, synced, best = None, None, None, None
    checks = []
    for peer in dict.fromkeys(peers):
        if cancel.is_set():
            raise NodeError('Operation cancelled.')
        if time.monotonic() >= deadline:
            break
        try:
            state = NodeClient(peer, cancel=cancel, network=network,
                               deadline=min(deadline, time.monotonic() + 7)).probe()
            if state['network'] != network:
                raise NodeError('The node did not verify the selected network.')
            checks.append(state)
            if state['synced']:
                if not lowest_fee:
                    return dict(state, node_checks=checks)
                synced = synced or state
                fee = state.get('min_fee_e8')
                # Unknown fees are never ranked as zero or as the cheapest.
                if type(fee) is int and fee >= 0 and (best is None or fee < best['min_fee_e8']):
                    best = state
                continue
            readable = readable or state
            last_error = NodeError('Configured nodes are still synchronizing.')
        except NodeError as exc:
            last_error = exc
            checks.append(dict(peer=peer, network=network, error=str(exc)))
    if cancel.is_set():
        raise NodeError('Operation cancelled.')
    if best or synced:
        return dict(best or synced, node_checks=checks)
    if readable and not require_synced:
        return dict(readable, node_checks=checks)
    raise NodeError('No synced node is available on the selected network. '
                    + str(last_error or 'Add or enable a node in Settings.'))
