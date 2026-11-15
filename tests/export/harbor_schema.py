"""Generate/check schema using the unmodified, pinned Apache-2.0 Harbor models.

No network. Pydantic is a development dependency only. Modules are registered
under their original namespace to preserve upstream absolute imports.
"""
import importlib.util
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor/harbor'


def upstream_model():
    for name in ('harbor', 'harbor.models', 'harbor.models.trajectories'):
        module = types.ModuleType(name)
        module.__path__ = [str(VENDOR)]
        sys.modules[name] = module
    name = 'harbor.models.trajectories.trajectory'
    spec = importlib.util.spec_from_file_location(name, VENDOR / 'trajectory.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Trajectory


if __name__ == '__main__':
    schema = upstream_model().model_json_schema()
    (ROOT / 'atif-v1.8.schema.json').write_text(
        json.dumps(schema, indent=2, sort_keys=True) + '\n', encoding='utf-8')
