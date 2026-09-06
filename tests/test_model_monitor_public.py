from types import SimpleNamespace

import pytest

from core.integrations.mmp_interface import MmpApiError, MmpInterface


def monitor(monkeypatch, signals=None):
    client = MmpInterface('http://monitor.example.test', 'synthetic-demo-token', '')
    paths = []
    project = {'id': 1, 'name': 'demo-inventory', 'models': [
        {'id': 1, 'name': 'inventory-score', 'is_production': True, 'signals': signals or {'drifted': False}}
    ]}
    def get(path):
        paths.append(path)
        return {'projects': [project]} if path == '/api/projects' else project
    monkeypatch.setattr(client, '_get', get)
    return client, paths


def test_generic_directory_contract_and_cache(monkeypatch):
    client, paths = monitor(monkeypatch)
    assert client.list_projects_shallow()['demo-inventory']['models'][0]['name'] == 'inventory-score'
    client.list_projects_shallow()
    assert paths == ['/api/projects']
    client.clear_cache()
    client.list_projects_shallow()
    assert len(paths) == 2


@pytest.mark.parametrize('drifted,action', [(True, 'create_issue'), (False, 'close_issue')])
def test_normalized_drift_signals(monkeypatch, drifted, action):
    client, _ = monitor(monkeypatch, {'drifted': drifted, 'drift_details': 'Synthetic inventory shift'})
    result = client.check_drift('demo-inventory', 'inventory-score', SimpleNamespace())
    assert result.action == action
    assert result.drifted is drifted
    assert result.mmp_project_numeric_id == 1


def test_monitor_outage_is_inconclusive(monkeypatch):
    client, _ = monitor(monkeypatch)
    def fail(*args):
        raise MmpApiError('Synthetic outage')
    monkeypatch.setattr(client, '_get', fail)
    assert client.check_drift('demo-inventory', 'inventory-score', SimpleNamespace()).action == 'inconclusive'


def test_unknown_binding_is_skipped(monkeypatch):
    client, _ = monitor(monkeypatch)
    assert client.check_drift('missing', 'missing', SimpleNamespace()).action == 'skipped'


def test_missing_signal_is_not_treated_as_recovery(monkeypatch):
    client, _ = monitor(monkeypatch, {'drift_details': 'No status yet'})
    assert client.check_drift('demo-inventory', 'inventory-score', SimpleNamespace()).action == 'inconclusive'


def test_public_signals_are_available_to_verification_and_diagnosis(monkeypatch):
    from core.agent.live_tools import build_mmp_model_raw_view
    client, _ = monitor(monkeypatch, {'drifted': True, 'drift_details': 'Synthetic shift',
                                     'run_pending_approval': True})
    raw = client.get_model_raw('demo-inventory', 'inventory-score')
    assert raw['attention_required']['model_drifted']['status'] is True
    assert raw['attention_required']['run_pending_approval']['status'] is True
    view = build_mmp_model_raw_view(client, 'demo-inventory', 'inventory-score')
    assert view['attention_required']['model_drifted']['status'] is True
    assert view['signals']['drifted'] is True
