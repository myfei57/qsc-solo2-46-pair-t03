#!/usr/bin/env bash
set -euo pipefail
docker build -f benzhi.Dockerfile -t cranefleet-python:latest .
