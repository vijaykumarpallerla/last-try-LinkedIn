import importlib, sys
sys.path.append('.')
mod = importlib.import_module('app')
print('Routes loaded:')
for r in sorted([rule.rule for rule in mod.app.url_map.iter_rules()]):
    print(' ', r)
