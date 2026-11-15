import ast
import pathlib
import unittest

BASE = pathlib.Path(__file__).resolve().parent
OLD = BASE.parent / 'routepilot-ubuntu-v1.7-modern-notifications' / 'routepilot.py'
NEW = BASE / 'routepilot.py'

class FeatureTests(unittest.TestCase):
    def test_features_exist(self):
        code=NEW.read_text()
        for key in ('set_theme(', 'dark_mode', 'query.trace_add', 'filter_mode.trace_add',
                    'wildcard_ip_counts()', 'Waiting DNS', 'Export backup',
                    'Import backup', 'Refresh status', 'validate(read_json(Path(path)'):
            self.assertIn(key,code)
    def test_routing_functions_unchanged(self):
        before={n.name:ast.dump(n,include_attributes=False) for n in ast.parse(OLD.read_text()).body if isinstance(n,ast.FunctionDef)}
        after={n.name:ast.dump(n,include_attributes=False) for n in ast.parse(NEW.read_text()).body if isinstance(n,ast.FunctionDef)}
        for name in ('reconcile','_reconcile_locked','wildcard_setup','wildcard_snat_reconcile',
                     'wildcard_snat_cleanup','observe_dns_forever','cleanup','install_config','validate'):
            self.assertEqual(before[name],after[name],name)

if __name__ == '__main__':unittest.main()
