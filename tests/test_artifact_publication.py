import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACKAGE = load('package_artifact')
PUBLISH = load('publish_figshare')


def test_credentials_are_not_sent_to_other_hosts(monkeypatch):
    monkeypatch.setenv('FIGSHARE_TOKEN', 'test-only-not-a-real-token')
    client = PUBLISH.Figshare()
    with pytest.raises(ValueError, match='outside'):
        client.request('GET', 'https://example.org/account/articles')


def test_duplicate_article_title_is_not_silently_chosen(monkeypatch):
    monkeypatch.setenv('FIGSHARE_TOKEN', 'test-only-not-a-real-token')
    client = PUBLISH.Figshare()
    client.request = lambda *a: [{'id': 1, 'title': 'same'}, {'id': 2, 'title': 'same'}]
    with pytest.raises(ValueError, match='Multiple'):
        client.find_title('same')


def test_matching_upload_is_reused_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('FIGSHARE_TOKEN', 'test-only-not-a-real-token')
    client = PUBLISH.Figshare()
    archive = tmp_path / 'artifact.zip'
    archive.write_bytes(b'archive')
    checksum = hashlib.md5(b'archive').hexdigest()
    entry = {'id': 8, 'name': archive.name, 'size': 7,
             'supplied_md5': checksum, 'computed_md5': checksum}
    calls = []
    def request(method, path, *args):
        calls.append(method)
        return entry if path.endswith('/8') else [entry]
    client.request = request
    assert client.upload(10, archive)['id'] == 8
    assert calls == ['GET', 'GET']


def test_changed_archive_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv('FIGSHARE_TOKEN', 'test-only-not-a-real-token')
    client = PUBLISH.Figshare()
    archive = tmp_path / 'artifact.zip'
    archive.write_bytes(b'new')
    entry = {'id': 8, 'name': archive.name, 'size': 3, 'supplied_md5': 'different'}
    client.request = lambda method, path: entry if path.endswith('/8') else [entry]
    with pytest.raises(ValueError, match='different content'):
        client.upload(10, archive)


def test_package_enumerates_only_tracked_files(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tracked file').write_text('keep')
    (tmp_path / '.env').write_text('exclude untracked configuration')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'tracked file'], check=True)
    files = list(PACKAGE.tracked_files(tmp_path))
    assert len(files) == 1
    assert files[0][1] == Path('tracked file')
    assert files[0][2] == 0o100644


def test_package_digest_streams_correct_content(tmp_path):
    path = tmp_path / 'input.dcp'
    path.write_bytes(b'original-checkpoint')
    assert PACKAGE.digest(path) == hashlib.sha256(b'original-checkpoint').hexdigest()


def test_plain_text_completion_acknowledgement(monkeypatch):
    import io
    monkeypatch.setenv('FIGSHARE_TOKEN', 'test-only-not-a-real-token')
    monkeypatch.setattr(PUBLISH.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'202 Accepted'))
    client = PUBLISH.Figshare()
    assert client.request('POST', '/account/articles/10/files/8', json_response=False) == {}
