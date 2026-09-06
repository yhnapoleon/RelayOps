"""Public demo configuration must not discover credentials or enable transports implicitly."""
from pathlib import Path

from core.config import Config


def config():
    value = Config.__new__(Config)
    value._raw = {}
    value._path = Path('missing-test-config.yaml')
    import core.config as module
    value._deepseek = getattr(module, '_UNSET', None)
    return value


def test_explicit_llm_environment(monkeypatch):
    monkeypatch.setenv('RELAYOPS_LLM_ENDPOINT', 'https://model.example.com/v1/')
    monkeypatch.setenv('RELAYOPS_LLM_API_KEY', 'synthetic-test-token')
    monkeypatch.setenv('RELAYOPS_LLM_MODEL', 'demo-model')
    cfg = config()
    assert cfg.llm_endpoint == 'https://model.example.com/v1'
    assert cfg.llm_bearer_token == 'synthetic-test-token'
    assert cfg.llm_model == 'demo-model'
    assert cfg.llm_configured


def test_unconfigured_llm_does_not_read_files(monkeypatch):
    for name in ('RELAYOPS_LLM_ENDPOINT', 'RELAYOPS_LLM_API_KEY', 'RELAYOPS_LLM_MODEL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-unrelated-key')
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: (_ for _ in ()).throw(AssertionError('implicit file read')))
    cfg = config()
    assert cfg.llm_endpoint == ''
    assert cfg.llm_bearer_token == ''
    assert not cfg.llm_configured


def test_demo_email_defaults_to_log():
    assert config().email_backend == 'log'
    assert config().email_domain == 'example.com'


def test_tls_verification_defaults_on():
    cfg = config()
    assert cfg.cml_platform_verify_ssl
    assert cfg.mmp_verify_ssl
    assert cfg.llm_verify_ssl
    assert cfg.confluence_verify_ssl


def test_database_and_jwt_environment(monkeypatch):
    monkeypatch.setenv('RELAYOPS_DATABASE_URL', 'postgresql://demo:demo@localhost:5432/relayops')
    expected_key = 'test-' * 8  # Deterministic synthetic fixture, never a deployed secret.
    monkeypatch.setenv('RELAYOPS_JWT_SECRET', expected_key)
    cfg = config()
    assert cfg.database_url == 'postgresql://demo:demo@localhost:5432/relayops'
    assert cfg.jwt_secret_key == expected_key


def test_missing_explicit_config_fails(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setenv('CONFIG_FILE', str(tmp_path / 'missing.yaml'))
    with pytest.raises(FileNotFoundError):
        Config()
