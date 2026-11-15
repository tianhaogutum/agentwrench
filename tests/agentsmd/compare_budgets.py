"""Reproducible instruction-retention experiment, no timing claims or network.

Run from the installed environment: python tests/agentsmd/compare_budgets.py
"""
import json
from pathlib import Path

from agentwrench.agentsmd.core import inspect_codex, load_settings


def compare() -> list[dict]:
    fixtures = Path(__file__).resolve().parent / 'fixtures'
    results = []
    for budget in (32768, 65536):
        settings, warnings = load_settings(fixtures/'codex_home', max_bytes=budget)
        report = inspect_codex(fixtures/'big/pkg/api', fixtures/'codex_home', settings, warnings)
        if report.diagnostics:
            raise RuntimeError(report.diagnostics)
        project = [row for row in report.entries if row['scope'] == 'project']
        source = sum(row['size'] for row in project)
        results.append(dict(budget=budget, project_source_bytes=source,
                            project_loaded_bytes=report.project_bytes_used,
                            project_dropped_bytes=source-report.project_bytes_used,
                            partial_files=sum(row['status'] == 'partial' for row in project),
                            dropped_files=sum(row['status'] == 'dropped-over-budget' for row in project),
                            fully_dropped_headings=sum(len(c['headings_fully_dropped']) for c in report.truncations),
                            rendered_bytes=len(report.text.encode('utf-8'))))
    return results


if __name__ == '__main__':
    print(json.dumps(compare(), indent=2))
