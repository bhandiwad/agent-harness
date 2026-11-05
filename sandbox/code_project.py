"""Runs ONLY inside the disposable, network-disabled code container."""
import base64
import zlib
import contextlib
import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path


class Pause(BaseException):
    def __init__(self, request):
        self.request = request


class Context:
    def __init__(self, replay):
        self.replay, self.index, self.files = replay, 0, {}

    def effect(self, request):
        index = self.index
        self.index += 1
        if index == len(self.replay):
            raise Pause(request)
        prior = self.replay[index]
        if request != prior['request']:
            raise ValueError('An earlier call changed while resuming. Keep calls deterministic; use input and returned results.')
        return prior['result']

    def generate(self, prompt):
        return self.effect({'kind': 'model', 'prompt': prompt})

    def tool(self, name, arguments):
        return self.effect({'kind': 'tool', 'name': name, 'arguments': arguments})

    def write_file(self, path, content):
        self.files[path] = content


def main():
    payload = json.loads(Path('/workspace/project.json').read_text())
    project = payload['project']
    root = Path('/workspace/project')
    root.mkdir()
    for name, value in project['files'].items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    ctx, output = Context(payload['replay']), io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            sys.path.insert(0, "/opt/packages")
            sys.path.insert(0, str(root))
            spec = importlib.util.spec_from_file_location('user_agent', root / project['entrypoint'])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            answer = module.run(payload['input'], ctx)
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('run(input, ctx) must return a nonempty string.')
            result = {'done': True, 'answer': answer, 'files': ctx.files}
    except Pause as pause:
        result = {'done': False, 'request': pause.request}
    except BaseException as exc:
        result = {'error': (str(exc) or type(exc).__name__)[:2000]}
        output.write(traceback.format_exc(limit=5))
    result['logs'] = output.getvalue()[-4000:]
    raw = json.dumps(result).encode()
    if len(raw) > 600000:
        raw = json.dumps({'error':'Combined code outputs exceed 600 KB. Split the task into smaller batches.'}).encode()
    if len(raw) > 30000:
        raw = json.dumps({'encoding':'zlib-base64-v1','data':base64.b64encode(zlib.compress(raw)).decode()}).encode()
    if len(raw) > 32768:
        raw = json.dumps({'error':'Compressed outputs exceed the 32 KB transport limit. Split the task into smaller batches.'}).encode()
    Path('/workspace/result.json').write_bytes(raw)


if __name__ == '__main__':
    main()
