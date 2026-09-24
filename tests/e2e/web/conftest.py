"""Web 面板 e2e 共享设施

隔离策略：
- PROXY_POOL_CONFIG 指向临时配置目录（复制自 config/，补丁 cache_db / 日志目录 / 本地 mock 探测 URL）
- 端口默认运行时挑**空闲端口**（本机 8082/8083 常被手动运行的 serve 占用，写死就得先停面板），
  需要固定端口时用环境变量 PP_E2E_SERVER_PORT / PP_E2E_WEB_PORT 指定
- 队列数据来自本地 mock 上游代理（复用 tests/e2e/mock_upstream），不依赖外网
- serve 以子进程运行（真实 aiohttp 面板 + Vue 前端构建产物）
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests

pytest.importorskip("playwright.sync_api", reason="需要 playwright（pip install playwright && playwright install chromium）")

from playwright.sync_api import sync_playwright  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.e2e.mock_upstream import start_mock_upstream  # noqa: E402
from tests.e2e.web.logpanel_probe import build_and_serve_probe  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_SRC = PROJECT_ROOT / "config"


def _free_ports(count: int) -> list[int]:
    """取 count 个当前空闲端口（全部 bind 后再统一释放，避免两次取到同一端口）"""
    socks = [socket.socket() for _ in range(count)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def _resolve_port(env_name: str, fallback: int) -> int:
    raw = os.environ.get(env_name)
    return int(raw) if raw else fallback


# 默认挑空闲端口：写死 8082/8083 会与手动运行的 serve 撞车，而 Windows SO_REUSEADDR 允许
# 后起进程绑同一端口并劫持连接（测试结果会指向错误实例且极难排查）——挑空闲端口天然规避。
_free = _free_ports(2)
SERVER_PORT = _resolve_port("PP_E2E_SERVER_PORT", _free[0])
WEB_PORT = _resolve_port("PP_E2E_WEB_PORT", _free[1])
BASE_URL = f"http://127.0.0.1:{WEB_PORT}"


class _MockProxyLoop:
    """在独立线程跑 asyncio loop，承载 mock 上游代理（serve 子进程的外部依赖）"""

    def __init__(self, count: int):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.servers: list = []
        self.ports: list[int] = []
        self.count = count
        self._ready = threading.Event()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self):
        async def _boot():
            for _ in range(self.count):
                srv, port = await start_mock_upstream()
                self.servers.append(srv)
                self.ports.append(port)
            self._ready.set()

        self.thread.start()
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(_boot()))
        assert self._ready.wait(10), "mock 上游启动超时"

    def stop(self):
        for srv in self.servers:
            srv.close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def _make_config_dir(tmpdir: Path, probe_port: int) -> Path:
    """复制真实配置目录并定向补丁：config.toml 改独立 cache_db/日志路径，
    serve.toml 改本地 mock 探测 URL；其余（组/源/geoip/ippure）与真实语义一致。

    纯净度检查在 e2e 中恒为禁用态：ippure.toml 的 source 置空（不依赖环境变量状态——
    父进程导入 config 时会把 .env 注入 os.environ 并被子进程继承）；同时删除 .env，
    避免把真实密钥带进测试进程。
    """
    dst = tmpdir / "config"
    shutil.copytree(CONFIG_SRC, dst)
    (dst / ".env").unlink(missing_ok=True)
    ippure = dst / "ippure.toml"
    ippure.write_text(
        re.sub(r'^source = "[^"]*"', 'source = ""', ippure.read_text(encoding="utf-8"),
               count=1, flags=re.M),
        encoding="utf-8",
    )
    cfg = dst / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace('cache_db = ".cache.db"', f'cache_db = "{(tmpdir / "e2e.cache.db").as_posix()}"')
    text = text.replace('log_dir = "logs"', f'log_dir = "{(tmpdir / "logs").as_posix()}"')
    cfg.write_text(text, encoding="utf-8")
    serve = dst / "serve.toml"
    text = serve.read_text(encoding="utf-8")
    text = re.sub(r'heartbeat_probe_url = "[^"]+"', f'heartbeat_probe_url = "http://127.0.0.1:{probe_port}/"', text)
    text = re.sub(
        r'network_probe_urls = \[[^\]]*\]',
        f'network_probe_urls = [\n    "http://127.0.0.1:{probe_port}/",\n]',
        text,
    )
    serve.write_text(text, encoding="utf-8")
    return dst


@pytest.fixture(scope="session")
def panel(request):
    """启动 mock 上游 + serve 子进程，种入代理并等待队列填充；返回 base_url

    种入策略：standby fill 会把可请求组代理 move 进队列组，故分两路——
    前 3 个进 default-http（被心跳补入队列，供队列页测试），
    后 3 个进 staging（非可请求组不被清空，供代理组页测试）。

    临时目录（配置副本 + cache_db + serve.out）在会话结束时清理；有失败用例时保留并打印
    路径——serve.out 是启动失败的唯一线索，不能随成功用例一起删掉。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="proxy-pool-e2e-"))
    # 防呆：Windows SO_REUSEADDR 允许后起进程绑同端口并劫持连接，若面板/代理端口
    # 已被占用（如手动跑着的 serve），测试结果会指向错误实例且极难排查——直接失败
    for port in (SERVER_PORT, WEB_PORT):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                pytest.fail(
                    f"端口 {port} 已被占用：e2e 需独占面板/代理端口"
                    "（默认取空闲端口，被占通常意味着并发跑了同一会话；"
                    "可用 PP_E2E_SERVER_PORT / PP_E2E_WEB_PORT 指定其它端口）"
                )
    mocks = _MockProxyLoop(count=6)
    mocks.start()
    config_dir = _make_config_dir(tmpdir, mocks.ports[0])

    env = dict(os.environ, PROXY_POOL_CONFIG=str(config_dir))
    # --type http：serve.toml 默认 proxy_types=["https"]，而 mock 上游是纯 HTTP 代理；
    # 允许类型不含 http 时 standby.fill 一个候选都选不中（队列永不填充）。
    proc = subprocess.Popen(
        [sys.executable, "cli.py", "serve", "--port", str(SERVER_PORT),
         "--web-port", str(WEB_PORT), "--type", "http"],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=open(tmpdir / "serve.out", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_panel(proc)
        queue_proxies = [f"http://127.0.0.1:{p}" for p in mocks.ports[:3]]
        group_proxies = [f"http://127.0.0.1:{p}" for p in mocks.ports[3:]]
        r = requests.post(f"{BASE_URL}/api/group/default-http/add", json={"proxies": queue_proxies}, timeout=10)
        assert r.ok and r.json()["added"] == len(queue_proxies), r.text
        r = requests.post(f"{BASE_URL}/api/group/staging/add", json={"proxies": group_proxies}, timeout=10)
        assert r.ok and r.json()["added"] == len(group_proxies), r.text
        _wait_standby_filled()
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mocks.stop()
        if request.session.testsfailed:
            print(f"\n[e2e] 有失败用例，保留临时目录供排查：{tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _wait_panel(proc: subprocess.Popen) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"serve 进程提前退出，见 serve.out")
        try:
            if requests.get(f"{BASE_URL}/api/status", timeout=2).ok:
                return
        except requests.ConnectionError:
            time.sleep(0.3)
    raise RuntimeError("面板启动超时（30s）")


def _wait_standby_filled() -> None:
    deadline = time.time() + 40
    while time.time() < deadline:
        st = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        if st["standby_count"] >= 1:
            return
        time.sleep(1)
    raise RuntimeError("代理服务队列未在 40s 内填充（mock 探测失败？）")


@pytest.fixture()
def page(panel):
    """每测试独立 playwright 上下文（函数级！）

    sync_playwright 的事件循环在 greenlet 中"暂停未退出"，会话期间主线程
    _running_loop 线程局部值残留，会使后续 IsolatedAsyncioTestCase 报
    "Runner.run() cannot be called from a running event loop"。
    函数级开关在测试间彻底清场，代价约 1s/测试。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = context.new_page()
        pg.goto(panel + "/#/dashboard")
        pg.wait_for_selector('[data-testid="kpi-grid"] .kpi-value', timeout=15000)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def api_status() -> dict:
    return requests.get(f"{BASE_URL}/api/status", timeout=5).json()


@pytest.fixture(scope="module")
def probe_server():
    """组件探针页：构建产物 + 静态托管（模块内只构建托管一次）"""
    yield from build_and_serve_probe()
