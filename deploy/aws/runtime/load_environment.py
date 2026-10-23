#!/usr/bin/env python3
"""Resolve Parameter Store values on the host without logging their contents."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

ALLOWED = frozenset('''LOG_LEVEL PUBLIC_BASE_URL DATABASE_PATH GATEWAY_BRAND_NAME
GATEWAY_DESCRIPTION GATEWAY_LOGO_URL UPSTREAM_LOGO_BASE_URL HIDE_UPSTREAM_BRAND
UPSTREAM_BRAND_TERMS MONID_API_BASE_URL MONID_API_KEY MONID_REQUEST_TIMEOUT_SECONDS
MONID_MIN_BALANCE_USD MONID_BALANCE_CHECK_SECONDS CATALOG_SNAPSHOT_PATH
CATALOG_REFRESH_SECONDS CATALOG_PAGE_SIZE EXCLUDED_PROVIDERS EXCLUDED_ENDPOINTS
PRICE_MARKUP PRICE_QUANTUM_RLUSD MIN_PRICE_RLUSD DEFAULT_RESULT_UNITS
DEFAULT_METERED_UNITS X402_PAYMENT_ENABLED X402_XRPL_NETWORK X402_XRPL_PAYTO_ADDRESS
X402_XRPL_FACILITATOR_URL X402_XRPL_ASSET X402_XRPL_ISSUER X402_SOURCE_TAG
X402_MAX_TIMEOUT_SECONDS X402_ALLOW_UNSUBMITTED_RETRY RUN_WAIT_SECONDS
RUN_POLL_INTERVAL_SECONDS MAX_CONCURRENT_RUNS PYTHONDONTWRITEBYTECODE'''.split())
PLACEHOLDER = '__NOT_CONFIGURED__'


def render_parameters(parameters, prefix):
    values = {}
    for parameter in parameters:
        name = parameter['Name']
        if not name.startswith(prefix + '/'):
            raise ValueError('Unexpected parameter path')
        key = name[len(prefix) + 1:]
        if key not in ALLOWED or key in values:
            raise ValueError('Unknown or duplicate configuration key')
        if key == 'MONID_API_KEY' and parameter.get('Type') != 'SecureString':
            raise ValueError('API key must use SecureString')
        value = parameter['Value']
        if any(c in value for c in '\r\n\x00'):
            raise ValueError('Multiline environment values are not supported')
        values[key] = '' if value == PLACEHOLDER else value
    if values.get('X402_PAYMENT_ENABLED', '').lower() != 'true':
        raise ValueError('Payment enforcement must remain enabled')
    for key, expected in {'DATABASE_PATH': '/app/data/gateway.sqlite3',
                          'CATALOG_SNAPSHOT_PATH': '/app/data/catalog.json'}.items():
        if values.get(key) != expected:
            raise ValueError('Persistent data paths cannot be changed during deployment')
    url = urlparse(values.get('PUBLIC_BASE_URL', ''))
    if url.scheme != 'https' or not url.netloc or url.username or url.password:
        raise ValueError('A public HTTPS base URL is required')
    return ''.join(key + '=' + value + '\n' for key, value in sorted(values.items()))


def write_environment(content, target):
    target = Path(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.environment-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    prefix = os.environ['PARAMETER_PREFIX'].rstrip('/')
    region = os.environ['AWS_REGION']
    if not re.fullmatch(r'/[A-Za-z0-9/_-]+', prefix):
        raise ValueError('Invalid parameter prefix')
    result = subprocess.run(
        ['aws', 'ssm', 'get-parameters-by-path', '--path', prefix,
         '--with-decryption', '--region', region, '--output', 'json'],
        check=True, capture_output=True, text=True, timeout=60,
    )
    content = render_parameters(json.loads(result.stdout)['Parameters'], prefix)
    write_environment(content, '/run/monid/gateway.env')
    print('Runtime configuration loaded from Parameter Store; values are hidden.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never include CLI output, parameter values, or exception input in logs.
        print('Runtime configuration could not be loaded or validated.', file=sys.stderr)
        sys.exit(1)
