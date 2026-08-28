"""Appwrite integration for agentops.

Public surface:
- client.get_appwrite() — typed Appwrite client (singleton)
- client.health() — reachability probe
- bootstrap.run() — one-shot schema bootstrap (idempotent)
"""
from appx.client import get_appwrite, health

__all__ = ["get_appwrite", "health", "bootstrap"]
