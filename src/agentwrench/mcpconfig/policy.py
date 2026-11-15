"""Strict, bounded paired-rule artifacts. Artifact and report versions are independent."""
import json

from .formats import ConfigError

POLICY_KINDS = ('loss', 'secret', 'interpolation')
POLICY_SCOPES = ('server', 'document')
POLICY_RULES = tuple(f'{kind}:{scope}' for kind in POLICY_KINDS for scope in POLICY_SCOPES)
POLICY_MAX_BYTES = 64 * 1024
POLICY_MAX_RULES = 128


def parse_policy(text, *, version=1):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError('duplicate JSON key in policy file')
            result[key] = value
        return result

    def nonfinite(value):
        raise ConfigError('non-finite JSON constant in policy file')

    try:
        artifact = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
    except ConfigError:
        raise
    except (ValueError, RecursionError):
        raise ConfigError('invalid strict JSON policy file (details masked)') from None
    if not isinstance(artifact, dict) or set(artifact) != {'schemaVersion', 'rules'}:
        raise ConfigError('policy file requires exactly schemaVersion and rules')
    if type(artifact['schemaVersion']) is not int or artifact['schemaVersion'] != version:
        raise ConfigError(f'unsupported policy file schemaVersion; expected integer {version}')
    rules = artifact['rules']
    if not isinstance(rules, list) or len(rules) > POLICY_MAX_RULES:
        raise ConfigError('policy rules must be an array of at most 128 entries')
    if version == 2:
        for rule in rules:
            if (not isinstance(rule, dict) or not {'kind', 'scope'} <= set(rule)
                    or set(rule) - {'kind', 'scope', 'server', 'path'}
                    or not isinstance(rule['kind'], str) or rule['kind'] not in POLICY_KINDS
                    or not isinstance(rule['scope'], str) or rule['scope'] not in POLICY_SCOPES):
                raise ConfigError('policy rule requires supported kind/scope and optional server/path only')
            if 'server' in rule and (rule['scope'] != 'server' or not isinstance(rule['server'], str)):
                raise ConfigError('policy server selector must be a string in server scope only')
            if 'path' in rule:
                path = rule['path']
                if (not isinstance(path, list) or len(path) > 64 or any(
                        not (isinstance(part, str) or type(part) is int and 0 <= part <= 2147483647)
                        for part in path)):
                    raise ConfigError('policy path requires at most 64 string/integer segments; indices 0..2147483647')
        return normalize_selectors(rules)
    selected = set()
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'kind', 'scope'}
                or not isinstance(rule['kind'], str) or rule['kind'] not in POLICY_KINDS
                or not isinstance(rule['scope'], str) or rule['scope'] not in POLICY_SCOPES):
            raise ConfigError('policy rule requires exactly a supported kind and scope pair')
        selected.add(f"{rule['kind']}:{rule['scope']}")
    # No partial selections escape if any later rule is invalid.
    return [rule for rule in POLICY_RULES if rule in selected]


def normalize_selectors(rules):
    """Normalize complete original selectors; never deduplicate masked identities."""
    def key(rule):
        return (POLICY_KINDS.index(rule['kind']), POLICY_SCOPES.index(rule['scope']),
                ('server' in rule, rule.get('server', '')),
                ('path' in rule, tuple((0, part) if isinstance(part, str) else (1, part)
                                       for part in rule.get('path', []))))
    unique = {key(rule): {k: rule[k] for k in ('kind', 'scope', 'server', 'path') if k in rule}
              for rule in rules}
    return [unique[k] for k in sorted(unique)]


def selector_matches(findings, rules):
    """All explicit selectors must match the same unmasked native finding."""
    return [rule for rule in rules if any(
        item.kind == rule['kind'] and item.scope == rule['scope']
        and ('server' not in rule or item.server == rule['server'])
        and ('path' not in rule or item.path == tuple(rule['path']))
        for item in findings)]
