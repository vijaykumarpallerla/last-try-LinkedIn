import importlib, sys
sys.path.append('.')
mod = importlib.import_module('app')
print('Imported app, routes:')
for r in mod.app.url_map.iter_rules():
    print(' ', r.rule)
