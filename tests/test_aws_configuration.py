"""Host configuration must fail closed without disclosing parameter values."""
import importlib.util
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('runtime_environment', ROOT / 'deploy/aws/runtime/load_environment.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
PREFIX = '/monid-x402-xrpl/runtime'


def parameters(**overrides):
    values = {'PUBLIC_BASE_URL': 'https://gateway.example.com',
              'DATABASE_PATH': '/app/data/gateway.sqlite3',
              'CATALOG_SNAPSHOT_PATH': '/app/data/catalog.json',
              'X402_PAYMENT_ENABLED': 'true', 'MONID_API_KEY': runtime.PLACEHOLDER}
    values.update(overrides)
    return [{'Name': PREFIX + '/' + name, 'Value': value,
             'Type': 'SecureString' if name == 'MONID_API_KEY' else 'String'}
            for name, value in values.items()]


def test_placeholder_is_empty_and_file_is_private(tmp_path):
    target = tmp_path / 'private' / 'gateway.env'
    runtime.write_environment(runtime.render_parameters(parameters(), PREFIX), target)
    assert 'MONID_API_KEY=\n' in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize('overrides', [
    {'X402_PAYMENT_ENABLED': 'false'},
    {'DATABASE_PATH': '/tmp/discarded.sqlite3'},
    {'CATALOG_SNAPSHOT_PATH': '/tmp/catalog.json'},
    {'AWS_ACCESS_KEY_ID': 'unexpected'},
    {'MONID_API_KEY': 'secret\nX402_PAYMENT_ENABLED=false'},
    {'PUBLIC_BASE_URL': 'http://gateway.example.com'},
])
def test_unsafe_configuration_is_rejected_without_values(overrides):
    with pytest.raises(ValueError) as error:
        runtime.render_parameters(parameters(**overrides), PREFIX)
    assert 'secret' not in str(error.value)
    assert 'unexpected' not in str(error.value)


def test_plaintext_api_key_parameter_is_rejected():
    items = parameters(MONID_API_KEY='never-log-this')
    items[-1]['Type'] = 'String'
    with pytest.raises(ValueError, match='SecureString'):
        runtime.render_parameters(items, PREFIX)


def test_atomic_replacement_keeps_permissions(tmp_path):
    target = tmp_path / 'gateway.env'
    target.write_text('old')
    runtime.write_environment('new', target)
    assert target.read_text() == 'new'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob('.environment-*'))


def test_deployment_requires_digest_and_expected_registry():
    spec = importlib.util.spec_from_file_location('deploy_transport', ROOT / 'deploy/aws/deploy_via_ssm.py')
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    registry = '123456789012.dkr.ecr.us-east-1.amazonaws.com'
    target = ('us-east-1', PREFIX, registry, 'monid-x402-xrpl')
    for image in ('evil.example/image@sha256:' + 'a' * 64,
                  registry + '/monid-x402-xrpl:latest',
                  registry + '/monid-x402-xrpl@sha256:' + 'a' * 64 + ';id'):
        with pytest.raises(ValueError):
            transport.remote_command(*target, image)
    command = transport.remote_command(*target, registry + '/monid-x402-xrpl@sha256:' + 'a' * 64)
    assert 'install.sh' in command
    assert '__NOT_CONFIGURED__' not in command
