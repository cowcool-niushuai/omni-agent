'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  BackendError,
  BackendManager,
  isOmniHealth,
  probe,
} = require('../backend');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'omni-desktop-test-'));
}

function listen(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      resolve({
        server,
        port: address.port,
        url: `http://127.0.0.1:${address.port}`,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}

test('isOmniHealth only accepts the OmniAgent health payload', () => {
  assert.equal(
    isOmniHealth({
      reachable: true,
      data: { service: 'omni-agent', status: 'healthy' },
    }),
    true,
  );
  assert.equal(
    isOmniHealth({ reachable: true, data: { service: 'other', status: 'healthy' } }),
    false,
  );
  assert.equal(isOmniHealth({ reachable: false, data: null }), false);
});

test('probe parses JSON and treats invalid JSON as reachable', async () => {
  const json = await listen((_request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ service: 'omni-agent', status: 'healthy' }));
  });
  const plain = await listen((_request, response) => response.end('not-json'));
  try {
    const parsed = await probe(`${json.url}/health`);
    assert.equal(parsed.reachable, true);
    assert.equal(parsed.data.service, 'omni-agent');

    const invalid = await probe(`${plain.url}/health`);
    assert.equal(invalid.reachable, true);
    assert.equal(invalid.data, null);
  } finally {
    await json.close();
    await plain.close();
  }
});

test('ensureRunning reuses an existing healthy backend', async () => {
  const server = await listen((_request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ service: 'omni-agent', status: 'healthy' }));
  });
  try {
    const manager = new BackendManager({
      repoRoot: tempDir(),
      appData: tempDir(),
      port: server.port,
    });
    const result = await manager.ensureRunning();
    assert.equal(result.started, false);
    assert.equal(manager.owned, false);
  } finally {
    await server.close();
  }
});

test('ensureRunning refuses a port occupied by another service', async () => {
  const server = await listen((_request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ status: 'ok', service: 'another-app' }));
  });
  try {
    const manager = new BackendManager({
      repoRoot: tempDir(),
      appData: tempDir(),
      port: server.port,
    });
    await assert.rejects(manager.ensureRunning(), BackendError);
  } finally {
    await server.close();
  }
});

test('development command uses the local virtualenv and uvicorn', () => {
  const root = tempDir();
  const python = path.join(root, '.venv', 'bin', 'python');
  fs.mkdirSync(path.dirname(python), { recursive: true });
  fs.writeFileSync(python, '');

  const manager = new BackendManager({
    repoRoot: root,
    appData: tempDir(),
    port: 8123,
  });
  const command = manager.resolveCommand();
  assert.equal(command.command, python);
  assert.deepEqual(command.args.slice(0, 4), [
    '-m',
    'uvicorn',
    'server:app',
    '--app-dir',
  ]);
  assert.equal(command.args.at(-1), '8123');
});

test('stop is a no-op when the backend was not started by the app', async () => {
  const manager = new BackendManager({
    repoRoot: tempDir(),
    appData: tempDir(),
  });
  await manager.stop();
  assert.equal(manager.child, null);
});
