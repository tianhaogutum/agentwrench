"""Compare saved action declarations without opening their bound databases."""
from .db import KEY, ScopeError, key
from .prune_plan import FORMAT as PLAN_FORMAT, WRITE_KEY, load_plan

FORMAT = 'lgckpt-scope.plan-comparison/v1'
DIFFERENCES = ('newly_selected', 'no_longer_selected', 'keep_to_delete', 'delete_to_keep')


def compare_plans(baseline_path, current_path, *, max_differences=None):
    if max_differences is not None and (type(max_differences) is not int or max_differences < 0):
        raise ScopeError('--max-differences must be a nonnegative integer')
    baseline, current = load_plan(baseline_path), load_plan(current_path)
    for saved in (baseline, current):
        thread = saved['scope']['thread']
        if thread is not None and any(row['thread_id'] != thread for row in saved['checkpoints']):
            raise ScopeError('invalid prune plan: checkpoint outside saved scope')
    if baseline['database'] != current['database'] or baseline['scope'] != current['scope']:
        raise ScopeError('incomparable prune plans: database binding and scope must match exactly')
    if baseline['format'] == current['format'] == PLAN_FORMAT:
        for table, fields in (('checkpoints', KEY), ('writes', WRITE_KEY)):
            def inventory(saved):
                return {tuple(row[f] for f in fields): row['sha256'] for row in saved['raw_rows'][table]}
            if inventory(baseline) != inventory(current):
                raise ScopeError('incomparable prune plans: contradictory raw-row snapshot evidence')

    previous = {key(row): row['action'] for row in baseline['checkpoints']}
    now = {key(row): row['action'] for row in current['checkpoints']}
    result = {'format': FORMAT, 'actions_identical': True, 'scope': baseline['scope'].copy(),
              'newly_selected': [{**dict(zip(KEY, k)), 'action': now[k]} for k in sorted(now.keys() - previous.keys())],
              'no_longer_selected': [{**dict(zip(KEY, k)), 'action': previous[k]} for k in sorted(previous.keys() - now.keys())]}
    common = sorted(previous.keys() & now.keys())
    for field, before, after in (('keep_to_delete', 'keep', 'delete'), ('delete_to_keep', 'delete', 'keep')):
        result[field] = [dict(zip(KEY, k)) for k in common if previous[k] == before and now[k] == after]
    result['actions_identical'] = not any(result[field] for field in DIFFERENCES)
    if max_differences is not None:
        result['max_differences'] = max_differences
        result['difference_summary'] = {}
        for field in DIFFERENCES:
            total = len(result[field])
            result[field] = result[field][:max_differences]
            shown = len(result[field])
            result['difference_summary'][field] = {
                'total': total, 'shown': shown, 'truncated': shown < total}
    return result
