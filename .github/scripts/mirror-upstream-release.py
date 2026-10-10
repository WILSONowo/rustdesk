"""Mirror the pinned official release assets into a draft without rebuilding them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def api(path):
    return json.loads(gh('api', path))


def tag_commit(repository, tag):
    obj = api(f'repos/{repository}/git/ref/tags/{tag}')['object']
    while obj['type'] == 'tag':
        obj = api(f"repos/{repository}/git/tags/{obj['sha']}")['object']
    assert obj['type'] == 'commit'
    return obj['sha']


def asset_map(release):
    return {a['name']: (a['size'], a['digest']) for a in release['assets']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-source-only', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(Path('.github/release-mirror-1.5.0.json').read_text())
    upstream, tag, source = (manifest[k] for k in ['repository', 'tag', 'commit'])
    official = api(f'repos/{upstream}/releases/tags/{tag}')
    assert not official['draft'] and not official['prerelease']
    assert official['id'] == manifest['release_id']
    assert tag_commit(upstream, tag) == source
    expected = {a['name']: (a['size'], a['digest']) for a in manifest['assets']}
    assert asset_map(official) == expected, 'Official release assets changed; review the manifest'
    assert all(digest.startswith('sha256:') for size, digest in expected.values())
    if args.verify_source_only:
        print(f'Verified {len(expected)} official assets and source tag {tag}.')
        return

    target = os.environ['GITHUB_REPOSITORY']
    assert target == 'WILSONowo/rustdesk'
    folder = Path('mirror-assets')
    folder.mkdir(exist_ok=True)
    # Download and validate every file before making a release or uploading anything.
    gh('release', 'download', tag, '--repo', upstream, '--dir', str(folder))
    assert {p.name for p in folder.iterdir()} == set(expected)
    for name, (size, checksum) in expected.items():
        path = folder / name
        assert path.stat().st_size == size
        with path.open('rb') as file:
            assert 'sha256:' + hashlib.file_digest(file, 'sha256').hexdigest() == checksum, name

    refs = api(f'repos/{target}/git/matching-refs/tags/{tag}')
    if not any(ref['ref'] == f'refs/tags/{tag}' for ref in refs):
        # The Actions token creates the exact source tag without triggering upstream build workflows.
        gh('api', '--method', 'POST', f'repos/{target}/git/refs',
           '-f', f'ref=refs/tags/{tag}', '-f', f'sha={source}')
    assert tag_commit(target, tag) == source, 'Fork tag must match the official release commit'
    releases = api(f'repos/{target}/releases?per_page=100')
    release = next((r for r in releases if r['tag_name'] == tag), None)
    if release is None:
        # Use the create response; listing can briefly lag behind a newly created draft.
        release = json.loads(subprocess.check_output(
            ['gh', 'api', '--method', 'POST', f'repos/{target}/releases', '--input', '-'],
            input=json.dumps({'tag_name': tag, 'target_commitish': source,
                              'draft': True, 'prerelease': False, 'name': tag, 'body': ''}),
            text=True))
    assert release['draft'], 'Refusing to change a published release'
    assert release['target_commitish'] == source
    existing = asset_map(release)
    assert set(existing) <= set(expected), 'Unexpected existing draft assets'
    for name, value in existing.items():
        assert value == expected[name], f'Existing asset differs: {name}'
    for name in sorted(expected):
        if name not in existing:
            subprocess.run(['gh', 'release', 'upload', tag, '--repo', target, str(folder / name)], check=True)
    after = api(f"repos/{target}/releases/{release['id']}")
    assert asset_map(after) == expected, 'Uploaded files do not exactly match the official release'
    for key in ['id', 'tag_name', 'target_commitish', 'draft', 'prerelease', 'name', 'body']:
        assert after[key] == release[key], key
    print(f'Mirrored and verified {len(expected)} official files into draft {target}@{tag}.')


if __name__ == '__main__':
    main()
