# Contributing

[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md)

Thank you for improving Agent Evaluation Framework.

## Before submitting a change

1. Open an Issue describing the problem first to avoid duplicate work.
2. Do not submit real business data, secrets, personal information, or model output that cannot be made public.
3. Keep domain logic in adapters. The core framework must not embed business-specific rules.
4. Add the smallest reproducible test for any behavioral change.

## Local verification

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/verify_evidence.py --output evidence/verified-results.json
python scripts/verify_concurrency.py --output evidence/concurrency-results.json
python scripts/verify_soak.py --duration-seconds 10 --output evidence/soak-results.json
```

By submitting a contribution, you confirm that you have the right to provide it and agree to release it under the repository's [PolyForm Noncommercial License 1.0.0](LICENSE). A contribution does not grant commercial-use permission.
