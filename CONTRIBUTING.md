# 参与贡献

[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md)

感谢你改进 Agent Evaluation Framework。

## 提交前

1. 先在 Issue 中说明要解决的问题，避免重复工作。
2. 不要提交真实业务数据、密钥、个人信息或无法公开的模型输出。
3. 保持领域逻辑在适配器中，核心框架不得内置特定业务口径。
4. 对行为变化增加最小可复现测试。

## 本地验证

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/verify_evidence.py --output evidence/verified-results.json
python scripts/verify_concurrency.py --output evidence/concurrency-results.json
python scripts/verify_soak.py --duration-seconds 10 --output evidence/soak-results.json
```

安装并启动 Docker 后，还可以运行真实容器隔离 smoke：

```powershell
python scripts/verify_container.py --output evidence/container-smoke-results.json
```

还可以复现 Docker Compose 容器拓扑矩阵。下面四条命令分别运行四类负载；每条命令使用 8 个 runner、每个 runner 使用 8 workers、每个 runner 处理 250 个 case：

```powershell
python scripts/verify_distributed_compose.py --profile short_io --runners 8 --workers 8 --cases-per-runner 250 --output evidence/reproduced-compose-short_io.json
python scripts/verify_distributed_compose.py --profile trace_heavy --runners 8 --workers 8 --cases-per-runner 250 --output evidence/reproduced-compose-trace_heavy.json
python scripts/verify_distributed_compose.py --profile mixed_io --runners 8 --workers 8 --cases-per-runner 250 --output evidence/reproduced-compose-mixed_io.json
python scripts/verify_distributed_compose.py --profile long_io --runners 8 --workers 8 --cases-per-runner 250 --output evidence/reproduced-compose-long_io.json
```

复现同机多进程 scale-out 矩阵：

```powershell
python scripts/verify_scale_out.py --output evidence/reproduced-scale-out.json --max-p95-agent-ms 25 --min-throughput 300
```

提交贡献即表示你有权提供相关内容，并同意贡献内容按照仓库的 [PolyForm Noncommercial License 1.0.0](LICENSE) 发布。贡献不会自动获得商业使用授权。
