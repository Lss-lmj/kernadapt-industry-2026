#!/usr/bin/env python3
"""Prepare pinned local packages and derive a manifest; preserve original inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-manifest', type=Path, required=True)
    parser.add_argument('--shared-directory', type=Path, required=True)
    parser.add_argument('--seed', type=Path, help='Optional existing directory of package files')
    args = parser.parse_args()
    if args.output_manifest.exists():
        raise FileExistsError(args.output_manifest)
    manifest = json.loads(args.manifest.read_text())
    cases = [case for case in manifest['cases'] if case['instance_id'] == 'dask__dask-5510']
    if not cases:
        raise ValueError('Input manifest does not contain dask__dask-5510')
    lock = json.loads((ROOT / 'environments/dask-5510-wheelhouse/packages.json').read_text())
    wheelhouse = args.shared_directory / 'wheelhouse/dask-5510'
    wheelhouse.mkdir(parents=True, exist_ok=True)
    for package in lock['packages']:
        target = wheelhouse / package['file']
        if target.exists():
            content = target.read_bytes()
        elif args.seed and (args.seed / target.name).is_file():
            content = (args.seed / target.name).read_bytes()
        else:
            url = package.get('url')
            if not url:
                with urllib.request.urlopen(
                    f'https://pypi.org/pypi/{package["project"]}/{package["version"]}/json', timeout=30
                ) as response:
                    metadata = json.load(response)
                url = next(item['url'] for item in metadata['urls'] if item['filename'] == target.name)
            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read()
        if hashlib.sha256(content).hexdigest() != package['sha256']:
            raise ValueError(f'Package checksum mismatch: {target}')
        if not target.exists():
            target.write_bytes(content)
    (wheelhouse / 'SHA256SUMS').write_text(''.join(
        package['sha256'] + '  ' + package['file'] + '\n'
        for package in sorted(lock['packages'], key=lambda item: item['file'])))
    settings = {'PIP_NO_INDEX':'1', 'PIP_FIND_LINKS':'/hrb-shared/wheelhouse/dask-5510',
                'PIP_DISABLE_PIP_VERSION_CHECK':'1'}
    for case in cases:
        case['container_env'] = [item for item in case.get('container_env', [])
                                 if item.split('=', 1)[0] not in settings]
        case['container_env'].extend(f'{key}={value}' for key,value in settings.items())
    manifest['suite_id'] += '-offline-dask5510'
    manifest['description'] = ('Original commands and full timing; Dask 5510 packages come from a '
        'checksummed local directory. Installation, uninstall, unpack and the original failed '
        'source build remain timed.')
    args.output_manifest.write_text(json.dumps(manifest, indent=2)+'\n')
    print(args.output_manifest)
    print(wheelhouse)


if __name__ == '__main__':
    main()
