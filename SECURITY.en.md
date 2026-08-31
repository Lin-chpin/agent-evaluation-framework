# Security Policy

[中文](SECURITY.md) | [English](SECURITY.en.md)

## Reporting a vulnerability

Use a GitHub Security Advisory to report issues that could cause unauthorized code execution, sensitive-data exposure, or an isolation escape. Do not open a public Issue first.

Include the affected version, reproduction conditions, impact, and minimal reproduction steps. The maintainer will publish details after confirming the issue and preparing a fix.

## Security boundary

The text workspace and subprocess isolation protect domain-project files from accidental candidate changes. They are not a security sandbox for hostile code. Integrations that run untrusted candidates must use a container, virtual machine, or restricted execution service.

Project and auto-evolution adapters are imported as Python modules and have code-execution privileges in the current process. Load trusted adapter files only. Traces, model inputs, and candidate artifacts may contain business data, so the integrating project must redact and control access before writing reports or sending content to a model.
