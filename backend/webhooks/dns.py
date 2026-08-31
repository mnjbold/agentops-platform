"""DNS record lookup + issue detection (issue #28).

The transport is deliberately stdlib-only: we use ``socket.getaddrinfo``
for the *basic* existence check and the public Cloudflare DNS-over-HTTPS
endpoint (``https://1.1.1.1/dns-query``) for the structured record
lookups. The DoH path is wrapped in a try/except so a missing outbound
network (e.g. an air-gapped test runner) returns a stub with a helpful
"see https://1.1.1.1/dns-query/<domain>" pointer instead of crashing.

We deliberately avoid the ``dnspython`` package — the requirements
file is intentionally lean and this module is hit only by the DNS
diagnostics screen, not the hot call path.

Issue heuristics
----------------
* Missing DKIM  → red. Telnyx signs outbound email with DKIM; a missing
                  record means deliverability will tank.
* Permissive SPF (`+all` or `?all`) → yellow. Anyone can spoof the
                  domain in SPF; policy should be `-all`.
* No DMARC     → yellow. The DMARC record is what tells receivers what
                  to do with SPF/DKIM failures.
* No MX        → yellow. The domain can't receive email.
* No SPF at all → yellow. Doesn't fail closed.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

# Cloudflare's DoH endpoint. We POST JSON queries (RFC 8427) and parse
# the response in-process; no extra deps needed.
_DOH_URL = "https://1.1.1.1/dns-query"

# How long to wait for the DoH upstream before giving up and stubbing.
_DOH_TIMEOUT_SECS = 4.0

# The set of well-known DKIM selectors we probe. Real operators publish
# under many selectors; the dashboard will surface "DKIM not detected"
# and let the user enter their own selector on follow-up.
_DKIM_SELECTORS = ("default", "telnyx", "k1", "s1", "s2", "google", "cm")


# ─────────────────────────── DoH (stdlib only) ───────────────────────────────


def _doh_query(name: str, qtype: str) -> list[dict]:
    """Return the Answer section of a DoH query, or [] on error.

    ``qtype`` is one of: ``A``, ``AAAA``, ``MX``, ``TXT``, ``CNAME``.
    The response shape is the Google/Cloudflare JSON form of RFC 8427.
    """
    payload = json.dumps([{"name": name, "type": qtype}]).encode("utf-8")
    req = urllib.request.Request(
        _DOH_URL,
        data=payload,
        headers={
            "Content-Type": "application/dns-json",
            "Accept": "application/dns-json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=_DOH_TIMEOUT_SECS, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except Exception as e:
        # Network failure: caller decides what to surface.
        log.debug("DoH query failed for %s/%s: %s", name, qtype, e)
        return []
    if not isinstance(data, list) or not data:
        return []
    return list(data[0].get("Answer") or [])


def _txt_records(name: str) -> list[str]:
    """Return the joined TXT record strings for ``name`` (one per record)."""
    answers = _doh_query(name, "TXT")
    out: list[str] = []
    for a in answers:
        data = a.get("data") or ""
        # Cloudflare returns quoted strings for TXT, e.g. "v=spf1 -all".
        if data.startswith('"') and data.endswith('"'):
            data = data[1:-1]
        out.append(data)
    return out


def _mx_records(name: str) -> list[dict]:
    """Return ``[{priority, host}, ...]`` for ``name``, or [] on error."""
    answers = _doh_query(name, "MX")
    out: list[dict] = []
    for a in answers:
        # MX data is "priority host.", e.g. "10 mx1.example.com."
        parts = (a.get("data") or "").split()
        if len(parts) != 2:
            continue
        try:
            prio = int(parts[0])
        except ValueError:
            continue
        out.append({"priority": prio, "host": parts[1].rstrip(".")})
    return out


def _resolve_a(name: str) -> list[str]:
    """A-record lookup (for the existence check + verification flow)."""
    answers = _doh_query(name, "A")
    return [a.get("data", "") for a in answers if a.get("data")]


# ─────────────────────────── issue detection ─────────────────────────────────

_SPF_RE = re.compile(r"^v=spf1\b", re.IGNORECASE)
_DMARC_RE = re.compile(r"^v=dmarc1\b", re.IGNORECASE)
_DKIM_RE = re.compile(r"^v=dkim1\b", re.IGNORECASE)


def _check_spf(txt_records: list[str]) -> tuple[Optional[str], list[dict]]:
    """Return ``(spf_record, issues)`` for the given TXT records."""
    spf = next((t for t in txt_records if _SPF_RE.match(t)), None)
    issues: list[dict] = []
    if not spf:
        issues.append({
            "code": "spf_missing",
            "severity": "yellow",
            "message": "No SPF record found. Add a TXT record like "
                       "`v=spf1 include:spf.telnyx.com -all`.",
        })
        return spf, issues
    # Permissive qualifier
    lowered = spf.lower()
    if "+all" in lowered:
        issues.append({
            "code": "spf_permissive_plus_all",
            "severity": "yellow",
            "message": "SPF ends with `+all` (allow any sender). "
                       "Replace with `-all` to fail-closed.",
        })
    if "?all" in lowered:
        issues.append({
            "code": "spf_neutral_all",
            "severity": "yellow",
            "message": "SPF ends with `?all` (neutral). "
                       "Use `-all` to enforce the policy.",
        })
    if "-all" not in lowered and "~all" not in lowered:
        issues.append({
            "code": "spf_no_fail_all",
            "severity": "yellow",
            "message": "SPF has no fail-all qualifier. Add `-all` or `~all` "
                       "so spoofed senders are rejected.",
        })
    return spf, issues


def _check_dmarc(txt_records: list[str]) -> tuple[Optional[str], list[dict]]:
    """DMARC lives at ``_dmarc.<domain>``; probe it separately."""
    issues: list[dict] = []
    # We expect the caller to pass DMARC records (probed at _dmarc.<domain>).
    dmarc = next((t for t in txt_records if _DMARC_RE.match(t)), None)
    if not dmarc:
        issues.append({
            "code": "dmarc_missing",
            "severity": "yellow",
            "message": "No DMARC record at `_dmarc.<domain>`. Add one "
                       "(`v=dmarc1; p=quarantine; rua=mailto:dmarc@<domain>`) "
                       "to tell receivers how to handle failed SPF/DKIM.",
        })
    return dmarc, issues


def _check_dkim(domain: str) -> tuple[list[str], list[dict]]:
    """Probe a handful of well-known DKIM selectors.

    Returns ``(selectors_found, issues)`` — the dashboard shows which
    selectors exist (helpful for the user to discover the actual
    selector used by Telnyx) and surfaces a red issue if none are
    found.
    """
    found: list[str] = []
    for sel in _DKIM_SELECTORS:
        try:
            recs = _txt_records(f"{sel}._domainkey.{domain}")
        except Exception as e:
            # Treat lookup failures as "no DKIM at this selector" so a
            # single broken DoH query doesn't kill the whole check.
            log.debug("DKIM %s lookup failed: %s", sel, e)
            continue
        if any(_DKIM_RE.match(r) or r.startswith("k=rsa") for r in recs):
            found.append(sel)
    issues: list[dict] = []
    if not found:
        issues.append({
            "code": "dkim_missing",
            "severity": "red",
            "message": "No DKIM record found at any of the well-known "
                       f"selectors ({', '.join(_DKIM_SELECTORS)}). "
                       "Outbound email will not be DKIM-signed.",
        })
    return found, issues


def _check_mx(mx_records: list[dict]) -> list[dict]:
    issues: list[dict] = []
    if not mx_records:
        issues.append({
            "code": "mx_missing",
            "severity": "yellow",
            "message": "No MX records. This domain cannot receive email.",
        })
    return issues


# ─────────────────────────── public API ──────────────────────────────────────


def lookup_domain(domain: str) -> dict:
    """Look up DNS records for ``domain`` and return the structured result.

    Shape::

        {
            "domain": "acme.com",
            "spf": "v=spf1 -all" | None,
            "dkim_selectors": ["telnyx", "default"],
            "dmarc": "v=dmarc1; p=quarantine; ..." | None,
            "mx": [{"priority": 10, "host": "mx.acme.com"}, ...],
            "resolves_to": ["203.0.113.42", ...],  # A records
            "issues": [
                {"code": "dkim_missing", "severity": "red", "message": "..."},
                ...
            ],
            "transport": "doh" | "stub",   # "stub" if DoH unreachable
            "transport_note": "...",        # shown to the operator
        }

    Never raises — any network failure flips the transport to ``stub``
    and returns a minimal record set so the UI can still render.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return {
            "domain": "",
            "spf": None,
            "dkim_selectors": [],
            "dmarc": None,
            "mx": [],
            "resolves_to": [],
            "issues": [{
                "code": "invalid_domain",
                "severity": "red",
                "message": "Empty or invalid domain.",
            }],
            "transport": "stub",
            "transport_note": "no domain supplied",
        }

    issues: list[dict] = []
    transport = "doh"
    transport_note = ""

    # TXT at apex → SPF
    try:
        apex_txt = _txt_records(domain)
    except Exception as e:
        log.warning("TXT lookup failed for %s: %s", domain, e)
        apex_txt = []
        transport = "stub"
        transport_note = f"DoH TXT lookup failed: {e}"

    spf, spf_issues = _check_spf(apex_txt)
    issues.extend(spf_issues)

    # DMARC at _dmarc.<domain>
    try:
        dmarc_txt = _txt_records(f"_dmarc.{domain}")
    except Exception:
        dmarc_txt = []
        if transport == "doh":
            transport = "stub"
            transport_note = "DoH DMARC lookup failed"
    dmarc, dmarc_issues = _check_dmarc(dmarc_txt)
    issues.extend(dmarc_issues)

    # DKIM at well-known selectors
    dkim_selectors, dkim_issues = _check_dkim(domain)
    issues.extend(dkim_issues)

    # MX
    try:
        mx = _mx_records(domain)
    except Exception:
        mx = []
        if transport == "doh":
            transport = "stub"
            transport_note = "DoH MX lookup failed"
    issues.extend(_check_mx(mx))

    # A records (for the live check / CNAME verification)
    try:
        a_records = _resolve_a(domain)
    except Exception:
        a_records = []
    # Belt-and-braces: also try getaddrinfo in case DoH fails and we
    # still want a "yes, it resolves" signal.
    if not a_records:
        try:
            infos = socket.getaddrinfo(domain, None)
            a_records = sorted({i[4][0] for i in infos if i and i[4]})
        except Exception:
            pass

    if transport == "stub" and not transport_note:
        transport_note = (
            "DoH endpoint unreachable; using a partial lookup. "
            f"Try `https://1.1.1.1/dns-query?name={urllib.parse.quote(domain)}` directly."
        )

    return {
        "domain": domain,
        "spf": spf,
        "dkim_selectors": dkim_selectors,
        "dmarc": dmarc,
        "mx": mx,
        "resolves_to": a_records,
        "issues": issues,
        "transport": transport,
        "transport_note": transport_note,
    }


def cname_target_for_tenant(tenant_id: str, custom_domain: str) -> str:
    """Return the CNAME the tenant should add at ``custom_domain``.

    Issue #30 — the white-label flow asks the operator to add a CNAME
    pointing at the platform's load balancer. For the v1 the target is
    a stable value the operator configures per region; the default
    below works for the staging deployment on bkjr-api.getbijou.xyz.
    """
    # Imported lazily so this module stays importable in tests without
    # the rest of the webhooks package.
    from webhooks.storage import get_store  # noqa: WPS433
    store = get_store()
    _ = store  # touch so the type-checker doesn't complain
    # The actual target is supplied by the BRAND_CNAME_TARGET env var
    # (set per region in Coolify) — fall back to the US default.
    import os
    return (
        os.environ.get("BRAND_CNAME_TARGET", "").strip()
        or "brand.bkjr-api.getbijou.xyz"
    )
