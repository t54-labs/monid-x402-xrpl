#!/usr/bin/env python3
"""Send versioned deployment tooling to one EC2 instance; never fetch app secrets."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time

RUNTIME_FILES = ('install.sh', 'deploy.sh', 'load_environment.py',
                 'monid-environment.service', 'monid-gateway.service')


def aws(*arguments):
    result = subprocess.run(['aws', *arguments, '--region', os.environ['AWS_REGION'],
                             '--output', 'json'], capture_output=True, text=True)
    if result.returncode:
        if 'InvocationDoesNotExist' in result.stderr:
            return None
        raise RuntimeError('AWS operation failed: ' + arguments[1])
    return json.loads(result.stdout)


def remote_command(region, prefix, registry, repository, operation):
    patterns = ((region, r'[a-z0-9-]+'), (prefix, r'/[A-Za-z0-9/_-]+'),
                (registry, r'[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com'),
                (repository, r'[a-z0-9-]+'))
    if not all(re.fullmatch(pattern, value) for value, pattern in patterns):
        raise ValueError('Invalid deployment target')
    if operation != 'refresh-config' and not re.fullmatch(
        re.escape(registry + '/' + repository) + r'@sha256:[a-f0-9]{64}', operation
    ):
        raise ValueError('Expected an immutable image from the configured repository')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in RUNTIME_FILES:
            archive.add(Path(__file__).parent / 'runtime' / name, arcname=name)
    bundle = base64.b64encode(stream.getvalue()).decode()
    arguments = ' '.join(shlex.quote(value) for value in
                         (region, prefix, registry, repository, operation))
    return ("set -eu\nwork=$(mktemp -d)\ntrap 'rm -rf \"$work\"' EXIT\n"
            + "printf '%s' '" + bundle + "' | base64 -d | tar -xz -C \"$work\"\n"
            + 'bash "$work/install.sh" ' + arguments + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='')
    parser.add_argument('--operation', choices=('deploy', 'refresh-config'), default='deploy')
    args = parser.parse_args()
    instance = os.environ['EC2_INSTANCE_ID']
    if not re.fullmatch(r'i-[a-f0-9]+', instance):
        raise ValueError('Invalid instance ID')
    command = remote_command(os.environ['AWS_REGION'], os.environ['PARAMETER_PREFIX'],
                             os.environ['ECR_REGISTRY'], os.environ['ECR_REPOSITORY'],
                             args.image if args.operation == 'deploy' else 'refresh-config')
    response = aws('ssm', 'send-command', '--cli-input-json', json.dumps({
        'InstanceIds': [instance], 'DocumentName': 'AWS-RunShellScript',
        'Comment': 'Deploy Monid gateway from GitHub Actions',
        'TimeoutSeconds': 120,
        'Parameters': {'commands': [command], 'executionTimeout': ['1000']},
    }))
    command_id = response['Command']['CommandId']
    print('SSM command:', command_id, flush=True)
    deadline = time.monotonic() + 1100
    while time.monotonic() < deadline:
        result = aws('ssm', 'get-command-invocation', '--command-id', command_id,
                     '--instance-id', instance)
        if result and result['Status'] not in ('Pending', 'InProgress', 'Delayed'):
            print(result.get('StandardOutputContent', ''))
            if result['Status'] != 'Success':
                print(result.get('StandardErrorContent', ''))
                raise RuntimeError('Remote deployment failed: ' + result['Status'])
            return
        time.sleep(5)
    raise RuntimeError('Deployment wait timed out; inspect SSM before retrying')


if __name__ == '__main__':
    main()
