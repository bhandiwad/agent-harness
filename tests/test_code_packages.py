import json
from types import SimpleNamespace

import pytest

from pandita_agent.agent.code_contracts import CodeProject, GitSource, PackageRequest
from pandita_agent.agent.code_environments import Environments


@pytest.mark.parametrize('packages', [{'x': '^1.0'}, {'x': 'https://bad.example/pkg'}, {'-r': '1.0'}, {'thing': 'latest'}, {'x': '1.0\nRUN evil'}, {'Some_Pkg': '1.0', 'some-pkg': '2.0'}])
def test_reject_non_registry_or_unpinned_packages(packages):
    with pytest.raises(ValueError):
        PackageRequest(language='python', packages=packages)


def test_old_projects_keep_serialization_and_pins_match(tmp_path):
    project = CodeProject(language='python', entrypoint='agent.py', files={'agent.py': 'source'})
    assert set(project.model_dump()) == {'language', 'entrypoint', 'files', 'test_input'}
    settings = SimpleNamespace(state_dir=tmp_path, images={'shell': 'sha256:'+'a'*64})
    environments = Environments(settings)
    assert environments.executor_settings(project) is settings
    project = project.model_copy(update={'packages': {'humanize': '4.11.0'}})
    with pytest.raises(ValueError, match='Build packages'):
        environments.executor_settings(project)
    req = PackageRequest(language='python', packages=project.packages)
    key = environments.key(req)
    environments.write(key, {'id': key, 'status': 'ready', 'request': req.model_dump(), 'image': 'sha256:'+'b'*64})
    project = project.model_copy(update={'environment': key})
    project = project.model_copy(update={'packages': {'humanize': '4.10.0'}})
    with pytest.raises(ValueError, match='Packages changed'):
        environments.executor_settings(project)
    with pytest.raises(ValueError):
        GitSource(repository='org/repo', branch='main', directory='../credentials')


@pytest.mark.asyncio
async def test_build_queue_retries_and_never_receives_source(tmp_path, monkeypatch):
    settings = SimpleNamespace(state_dir=tmp_path, images={'shell': 'sha256:'+'a'*64})
    envs = Environments(settings)
    calls = []
    async def command(*args, **kwargs):
        calls.append(args)
        if args[0] == 'tag':
            return ''
        if args[0] == 'build':
            recipe = (__import__('pathlib').Path(args[-1])/'Dockerfile').read_text()
            assert '--only-binary=:all:' in recipe and 'source' not in recipe
            return ''
        if args[0] == 'image':
            return 'sha256:'+'b'*64
        return json.dumps({'install': []})
    monkeypatch.setattr(envs, 'command', command)
    req = PackageRequest(language='python', packages={'humanize': '4.11.0'})
    first = envs.start(req)
    assert envs.start(req)['id'] == first['id']
    await envs.tasks[first['id']]
    assert envs.read(first['id'])['status'] == 'ready'
    assert len(calls) == 4


def test_ready_package_image_from_superseded_base_cannot_execute(tmp_path):
    settings = SimpleNamespace(state_dir=tmp_path, images={'shell': 'sha256:'+'a'*64})
    environments = Environments(settings)
    request = PackageRequest(language='python', packages={'humanize': '4.11.0'})
    key = environments.key(request)
    environments.write(key, {'id':key, 'status':'ready', 'request':request.model_dump(), 'image':'sha256:'+'b'*64})
    project = CodeProject(language='python', entrypoint='agent.py', files={'agent.py':'source'}, packages=request.packages, environment=key)
    settings.images['shell'] = 'sha256:'+'c'*64
    with pytest.raises(ValueError, match='environment needs an update'):
        environments.executor_settings(project)
