# pspeed

命令行代理测速工具，支持 http(s)/socks4/socks5，内置多种测速节点，JSON 输出（子进程友好）。

## 安装

```bash
pip install -e .
```

## 用法

```bash
# 单个代理
pspeed --proxy socks5://1.2.3.4:1080

# 多个代理
pspeed --proxy http://1.2.3.4:8080 --proxy socks5://5.6.7.8:1080

# 从文件批量读取（每行一个，无前缀按端口猜协议）
pspeed --proxy-file proxies.txt

# 全维度测速（含上传）
pspeed --proxy socks5://1.2.3.4:1080 --test tcp,ip,download,upload

# 指定测速节点、多次取峰值
pspeed --proxy-file proxies.txt --nodes cloudflare,aliyun --multi 3

# 筛选 + 美化输出
pspeed --proxy-file proxies.txt --min-speed 1 --max-latency 2000 --pretty

# 静默（只输出最终 JSON，适合子进程消费）
pspeed --proxy-file proxies.txt --quiet
```

## 参数

| 参数 | 说明 |
|---|---|
| `--proxy URL` | 代理地址，可多次指定 |
| `--proxy-file PATH` | 批量读取代理文件 |
| `--test LIST` | 测速维度: tcp,ip,download,upload |
| `--download-bytes N` | 下载字节数(支持 K/M/G) |
| `--upload-bytes N` | 上传字节数 |
| `--nodes LIST` | 下载测速节点或 all |
| `--upload-nodes LIST` | 上传测速节点或 all |
| `--multi N` | 每节点重复次数，取峰值 |
| `--concurrency N` | 并发数 |
| `--timeout SEC` | 单次探测硬性总时限 |
| `--min-speed MBPS` | 筛选最低下载速度 |
| `--max-latency MS` | 筛选最高 TCP 延迟 |
| `--pretty` | JSON 美化 |
| `--quiet` | 静默模式 |

## JSON 输出

```json
{
  "version": "1.0.0",
  "summary": {"total": 15, "ok": 13, "fail": 2},
  "results": [{
    "proxy": "socks5://1.2.3.4:1080",
    "ok": true,
    "tcp": {"connected": true, "latency_ms": 125},
    "ip": {"out_ip": "1.2.3.4", "geo": {"country": "DE"}},
    "download": {"best_mbps": 3.63, "avg_mbps": 2.1, "node": "cloudflare"},
    "upload": {"best_mbps": 1.2, "node": "cloudflare"}
  }]
}
```
