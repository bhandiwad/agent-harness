"""Immutable package images built from fixed recipes, never submitted Dockerfiles.

Only registry packages reach this builder. Source, secrets and model credentials do
not. Runs use the resulting image ID with the existing network-disabled executor.
"""
import asyncio
import json
import re
import tempfile
from pathlib import Path

from pandita_agent.contracts import content_digest
from pandita_agent.agent.code_contracts import PackageRequest


class Environments:
    def __init__(self, settings):
        self.settings = settings
        self.directory = settings.state_dir / "environments"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.docker_config = self.directory / "docker-config"
        self.docker_config.mkdir(mode=0o700, exist_ok=True)
        (self.docker_config / "config.json").write_text("{}")
        self.tasks = {}
        self.gate = asyncio.Semaphore(1)

    def key(self, request):
        return content_digest({"recipe": 1, "base": self.settings.images["shell"], **request.model_dump(mode="json")})

    def read(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid environment.")
        path = self.directory / (key + ".json")
        if not path.exists():
            return {"id": key, "status": "missing", "message": "Build packages for this project."}
        data = json.loads(path.read_text())
        if data['status'] in {'queued', 'building'} and key not in self.tasks:
            data.update(status='failed', message='The build was interrupted. Choose Build Packages to retry.')
        return data

    def write(self, key, value):
        path = self.directory / (key + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(path)

    def start(self, request):
        key = self.key(request)
        current = self.read(key)
        if current['status'] == 'ready' or key in self.tasks:
            return current
        if len(self.tasks) >= 4:
            raise ValueError('The package build queue is full. Try again shortly.')
        state = {'id': key, 'status': 'queued', 'message': 'Waiting to build packages.', 'request': request.model_dump(mode='json')}
        self.write(key, state)
        self.tasks[key] = asyncio.create_task(self.build(key, request, state))
        return state

    async def command(self, *args, deadline=240):
        process = await asyncio.create_subprocess_exec(
            str(self.settings.docker_binary), *args,
            env={'PATH': '/usr/bin:/bin', 'DOCKER_HOST': self.settings.docker_host, 'DOCKER_CONFIG': str(self.docker_config)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async def output():
            result = bytearray()
            while chunk := await process.stdout.read(4096):
                result.extend(chunk)
                if len(result) > 262144:
                    raise ValueError('Package build output exceeded its limit.')
            await process.wait()
            return bytes(result).decode(errors='replace')
        try:
            value = await asyncio.wait_for(output(), deadline)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            # Build output can contain package-controlled text. Return a bounded diagnostic.
            raise ValueError('Package build failed. Check names and versions; Python needs wheels and JavaScript install scripts are disabled.\n' + value[-1800:])
        return value

    async def build(self, key, request, state):
        tag = 'pandita-code-env:' + key
        try:
            async with self.gate:
                state.update(status='building', message='Installing and locking packages…')
                self.write(key, state)
                with tempfile.TemporaryDirectory(prefix='agent-packages-') as folder:
                    root = Path(folder)
                    base_tag = 'pandita-code-base:' + self.settings.images['shell'].split(':')[-1]
                    await self.command('tag', self.settings.images['shell'], base_tag, deadline=15)
                    recipe = 'FROM ' + base_tag + '\nUSER 0\nWORKDIR /opt/packages\n'
                    if request.language == 'python':
                        (root / 'requirements.txt').write_text('\n'.join(n+'=='+v for n,v in sorted(request.packages.items())))
                        recipe += 'COPY requirements.txt /tmp/requirements.txt\nRUN python -I -m pip install --isolated --disable-pip-version-check --no-cache-dir --only-binary=:all: --index-url https://pypi.org/simple --target /opt/packages --report /opt/package-lock.json -r /tmp/requirements.txt\n'
                    else:
                        (root / 'package.json').write_text(json.dumps({'private': True, 'dependencies': request.packages}))
                        recipe += 'COPY package.json /opt/packages/package.json\nRUN npm install --package-lock-only --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org && node -e "const p=require(\'./package-lock.json\'); for(const [k,v] of Object.entries(p.packages)){if(k && (!v.resolved?.startsWith(\'https://registry.npmjs.org/\') || !v.integrity))throw Error(\'Only integrity-locked npm registry packages are supported\');}" && npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org && cp package-lock.json /opt/package-lock.json\n'
                    recipe += 'USER 65532:65532\nWORKDIR /workspace\n'
                    (root / 'Dockerfile').write_text(recipe)
                    await self.command('build', '--pull=false', '-t', tag, folder)
                image = (await self.command('image', 'inspect', '--format', '{{.Id}}', tag, deadline=15)).strip()
                if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
                    raise ValueError('Package image could not be verified.')
                # Read metadata without invoking any installed package or Python .pth file.
                lock = await self.command('run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--entrypoint', '/bin/cat', image, '/opt/package-lock.json', deadline=15)
                state.update(status='ready', message='Packages built. Save this draft before testing.', image=image, lock=json.loads(lock))
                self.write(key, state)
        except (Exception, asyncio.CancelledError) as exc:
            state.update(status='failed', message=str(exc)[:2000] or 'Build interrupted. Retry Build Packages.')
            self.write(key, state)
        finally:
            self.tasks.pop(key, None)

    def executor_settings(self, project):
        if not project.packages:
            return self.settings
        if not project.environment:
            raise ValueError('Build packages and save this draft before running it.')
        state = self.read(project.environment)
        if state['status'] != 'ready' or state.get('request') != {'language': project.language, 'packages': project.packages}:
            raise ValueError('Packages changed or their environment is unavailable. Rebuild packages and save the draft.')
        if project.environment != self.key(PackageRequest(language=project.language, packages=project.packages)):
            raise ValueError('This package environment needs an update. Choose Build Packages and save the draft before running it.')
        return self.settings.model_copy(update={'images': {**self.settings.images, 'shell': state['image']}})
