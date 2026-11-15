"""Offline budget planner: strip annotations, then cap descriptions; never deny."""

from __future__ import annotations

from typing import Any

from .config import DEFAULT_ANNOTATIONS, InputError
from .jsjson import is_safe_integer
from .slim import apply_config, ceil_div4, largest_first, measure


def plan(tools: list[dict[str, Any]], budget: Any) -> dict[str, Any]:
    if not is_safe_integer(budget) or budget < 1:
        raise InputError('budget: expected a positive safe integer')
    before = measure(tools)
    config: dict[str, Any] = {}
    after = before
    stages = [{'stage': 'original', 'bytes': before['bytes'], 'estimatedTokens': before['estimatedTokens']}]
    if after['estimatedTokens'] > budget:
        config['stripSchemaAnnotations'] = list(DEFAULT_ANNOTATIONS)
        after = measure(apply_config(tools, config))
        stages.append({'stage': 'strip schema annotations (keep default)', 'bytes': after['bytes'], 'estimatedTokens': after['estimatedTokens']})
    if after['estimatedTokens'] > budget:
        lo, hi = 0, max([0, *(len(tool.get('description') or '') for tool in tools)])
        # Truncated prefixes grow monotonically, including sentence/word snapping.
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if measure(apply_config(tools, {**config, 'maxDescriptionChars': mid}))['estimatedTokens'] <= budget:
                lo = mid
            else:
                hi = mid - 1
        config['maxDescriptionChars'] = lo
        after = measure(apply_config(tools, config))
        stages.append({'stage': f'description cap {lo} code points', 'bytes': after['bytes'], 'estimatedTokens': after['estimatedTokens']})
    remaining = after['bytes']
    deny_suggestions = []
    for tool in largest_first(after['perTool']):
        if ceil_div4(remaining) <= budget:
            break
        remaining -= tool['bytes']
        deny_suggestions.append({'name': tool['name'], 'bytes': tool['bytes'], 'estimatedTokensIfDenied': ceil_div4(remaining)})
    return {
        'budget': budget, 'config': config, 'before': before, 'after': after, 'stages': stages,
        'meetsBudget': after['estimatedTokens'] <= budget, 'denySuggestions': deny_suggestions,
    }
