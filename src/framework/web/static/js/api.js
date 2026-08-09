/**
 * API 封装：统一处理请求、响应解析、错误抛出。
 * 所有模块通过 window.api 调用后端 REST 接口。
 */
(function () {
  'use strict';

  // 页面调度注册表；先初始化确保后续页面模块可安全注册
  window.pageHandlers = window.pageHandlers || {};

  async function request(path, method, body) {
    const options = {
      method: method || 'GET',
      headers: { 'Content-Type': 'application/json' },
    };
    if (body !== undefined) {
      options.body = JSON.stringify(body);
    }

    const response = await fetch(path, options);
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }

    if (!response.ok) {
      const message = data.error || `HTTP ${response.status}`;
      const err = new Error(message);
      err.status = response.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  window.api = {
    status() {
      return request('/api/status');
    },
    group(name) {
      return request(`/api/group/${encodeURIComponent(name)}`);
    },
    fetch() {
      return request('/api/fetch', 'POST', {});
    },
    blacklist(proxies) {
      return request('/api/blacklist', 'POST', { proxies });
    },
    trust(proxies) {
      return request('/api/trust', 'POST', { proxies });
    },
    pauseSource(source, ttl) {
      return request('/api/source/pause', 'POST', { source, ttl });
    },
    resumeSource(source) {
      return request('/api/source/resume', 'POST', { source });
    },
    pin(proxy) {
      return request('/api/standby/pin', 'POST', { proxy });
    },
    unpin(proxy) {
      return request('/api/standby/unpin', 'POST', { proxy });
    },
    lock(proxy) {
      return request('/api/standby/lock', 'POST', { proxy });
    },
    unlock() {
      return request('/api/standby/unlock', 'POST', {});
    },
    settings(patch) {
      return request('/api/settings', 'POST', patch);
    },
    proxyHistory(bind) {
      return request(`/api/proxy/history?bind=${encodeURIComponent(bind)}`);
    },
    speedTest(bind) {
      return request('/api/proxy/speedtest', 'POST', { bind });
    },
  };
})();
