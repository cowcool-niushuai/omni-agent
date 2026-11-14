'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');

const HEALTH_TIMEOUT_MS = 1500;
const STARTUP_TIMEOUT_MS = 60000;
const SHUTDOWN_TIMEOUT_MS = 7000;

class BackendError extends Error {
  constructor(message, logPath = '') {
    super(message);
    this.name = 'BackendError';
    this.logPath = logPath;
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function probe(url, timeout = HEALTH_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const request = http.get(
      url,
      {
        timeout,
        headers: { Accept: 'application/json' },
      },
      (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => {
          if (body.length < 65536) body += chunk;
        });
        response.on('end', () => {
          let data = null;
          try {
            data = JSON.parse(body);
          } catch {
            // Not an OmniAgent JSON response.
          }
          resolve({
            reachable: true,
            statusCode: response.statusCode || 0,
            data,
          });
        });
      },
    );
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.on('error', (error) => {
      resolve({ reachable: false, statusCode: 0, data: null, error: error.message });
    });
  });
}

function isOmniHealth(result) {
  return Boolean(
    result &&
      result.reachable &&
      result.data &&
      result.data.service === 'omni-agent' &&
      result.data.status === 'healthy',
  );
}

class BackendManager {
  constructor({
    repoRoot,
    appData,
    port = 8001,
    env = process.env,
    packaged = false,
  }) {
    this.repoRoot = repoRoot;
    this.appData = appData;
    this.port = Number(port) || 8001;
    this.env = env;
    this.packaged = packaged;
    this.child = null;
    this.owned = false;
    this.logStream = null;
    this.logPath = path.join(appData, 'backend.log');
  }

  get url() {
    return `http://127.0.0.1:${this.port}`;
  }

  get healthUrl() {
    return `${this.url}/health`;
  }

  async ensureRunning() {
    const existing = await probe(this.healthUrl);
    if (isOmniHealth(existing)) {
      this.owned = false;
      return { started: false, url: this.url };
    }
    if (existing.reachable) {
      throw new BackendError(
        `端口 ${this.port} 已被其他程序占用，无法启动 OmniAgent 后端。`,
      );
    }

    this.start();
    await this.waitForHealthy();
    return { started: true, url: this.url };
  }

  resolveCommand() {
    if (this.env.OMNI_DESKTOP_BACKEND) {
      return {
        command: this.env.OMNI_DESKTOP_BACKEND,
        args: [
          '--host',
          '127.0.0.1',
          '--port',
          String(this.port),
        ],
        cwd: this.repoRoot,
      };
    }

    if (this.packaged) {
      const binary = process.platform === 'win32'
        ? path.join(process.resourcesPath, 'backend', 'omni-agent-server.exe')
        : path.join(process.resourcesPath, 'backend', 'omni-agent-server');
      if (fs.existsSync(binary)) {
        return {
          command: binary,
          args: ['--host', '127.0.0.1', '--port', String(this.port)],
          cwd: this.repoRoot,
        };
      }
      throw new BackendError(
        '当前安装包没有内置后端。请设置 OMNI_DESKTOP_BACKEND，或使用开发模式从源码启动。',
      );
    }

    const configured = this.env.OMNI_DESKTOP_PYTHON;
    const candidates = [
      configured,
      path.join(this.repoRoot, '.venv', 'bin', 'python'),
      path.join(this.repoRoot, '.venv', 'Scripts', 'python.exe'),
      'python3',
      'python',
    ].filter(Boolean);

    let python = null;
    for (const candidate of candidates) {
      if (candidate.includes(path.sep) && !fs.existsSync(candidate)) continue;
      python = candidate;
      break;
    }
    if (!python) {
      throw new BackendError(
        '没有找到 Python 解释器。请先创建 .venv，或设置 OMNI_DESKTOP_PYTHON。',
      );
    }

    return {
      command: python,
      args: [
        '-m',
        'uvicorn',
        'server:app',
        '--app-dir',
        this.repoRoot,
        '--host',
        '127.0.0.1',
        '--port',
        String(this.port),
      ],
      cwd: this.repoRoot,
    };
  }

  start() {
    if (this.child) return;
    const { command, args, cwd } = this.resolveCommand();
    fs.mkdirSync(this.appData, { recursive: true });
    this.logStream = fs.createWriteStream(this.logPath, { flags: 'a' });
    this.logStream.write(
      `\n[${new Date().toISOString()}] starting ${command} ${args.join(' ')}\n`,
    );

    this.child = spawn(command, args, {
      cwd,
      env: {
        ...this.env,
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: [cwd, this.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        OMNI_DESKTOP: '1',
      },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.owned = true;

    this.child.stdout.on('data', (chunk) => this.logStream.write(chunk));
    this.child.stderr.on('data', (chunk) => this.logStream.write(chunk));
    this.child.on('error', (error) => {
      this.logStream.write(`\n[backend error] ${error.stack || error.message}\n`);
    });
    this.child.on('exit', (code, signal) => {
      this.logStream.write(
        `\n[${new Date().toISOString()}] backend exited code=${code} signal=${signal}\n`,
      );
      this.child = null;
      this.owned = false;
    });
  }

  async waitForHealthy(timeout = STARTUP_TIMEOUT_MS) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (!this.child) {
        throw new BackendError('后端进程提前退出。', this.logPath);
      }
      const result = await probe(this.healthUrl);
      if (isOmniHealth(result)) return;
      await delay(400);
    }
    throw new BackendError('后端启动超时。', this.logPath);
  }

  tail(maxChars = 5000) {
    try {
      const content = fs.readFileSync(this.logPath, 'utf8');
      return content.slice(-maxChars);
    } catch {
      return '';
    }
  }

  async stop() {
    const child = this.child;
    if (!this.owned || !child) return;
    this.owned = false;
    const exited = new Promise((resolve) => child.once('exit', resolve));
    child.kill('SIGTERM');
    const timeout = delay(SHUTDOWN_TIMEOUT_MS).then(() => 'timeout');
    if ((await Promise.race([exited, timeout])) === 'timeout' && child.exitCode === null) {
      child.kill('SIGKILL');
      await Promise.race([exited, delay(2000)]);
    }
    this.child = null;
    if (this.logStream) {
      this.logStream.end();
      this.logStream = null;
    }
  }
}

module.exports = {
  BackendError,
  BackendManager,
  isOmniHealth,
  probe,
};
