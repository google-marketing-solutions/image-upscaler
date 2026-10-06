# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Security controls: Auth, SSRF prevention, magic-bytes, CSRF, rate limits."""

from __future__ import annotations

import collections
import functools
import hashlib
import hmac
import ipaddress
import os
import pathlib
import secrets
import socket
import time
from typing import Deque, Dict, Tuple
import urllib.parse

from app.config import CSRF_SECRET_KEY
from app.config import DEPLOYMENT_ACCESS_CODE
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware

# Allowed image magic signatures
IMAGE_MAGIC_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
)


def inspect_image_magic_bytes(data: bytes) -> str:
  """Validate that raw bytes begin with an allow-listed image magic header.

  Args:
    data: Raw image byte payload.

  Returns:
    Detected image format string (JPEG, PNG, GIF, or WEBP).

  Raises:
    ValueError: If the content is not a valid allow-listed image format.
  """
  if not data or len(data) < 12:
    raise ValueError("Payload is too small to be a valid image")

  for magic, fmt in IMAGE_MAGIC_SIGNATURES:
    if data.startswith(magic):
      return fmt

  if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
    return "WEBP"

  raise ValueError(
      "File content failed magic-byte validation (allowed: JPEG, PNG, WEBP,"
      " GIF)"
  )


def validate_csv_upload_filename(filename: str) -> str:
  """Validate uploaded feed filename extension and strip path components."""
  if not filename:
    raise ValueError("Missing upload filename")
  clean_name = os.path.basename(filename.replace("\\", "/"))
  ext = os.path.splitext(clean_name)[1].lower()
  if ext not in (".csv", ".tsv", ".txt"):
    raise ValueError(
        f"Unsupported feed file extension '{ext}'. Allowed: .csv, .tsv, .txt"
    )
  return clean_name


def safe_resolve_within_sandbox(
    sandbox_dir: pathlib.Path, relative_filename: str
) -> pathlib.Path:
  """Safely resolve a file path inside sandbox_dir with boundary checks.

  Args:
    sandbox_dir: Base sandbox directory path.
    relative_filename: Untrusted relative path inside the sandbox.

  Returns:
    Resolved Path guaranteed to reside inside sandbox_dir.

  Raises:
    ValueError: If the target path is empty or escapes the sandbox boundary.
  """
  sandbox_resolved = sandbox_dir.resolve()
  sandbox_resolved.mkdir(parents=True, exist_ok=True)

  parts = [
      os.path.basename(part)
      for part in relative_filename.replace("\\", "/").split("/")
      if part and part not in (".", "..")
  ]
  if not parts:
    raise ValueError("Invalid empty target path")

  candidate = sandbox_resolved.joinpath(*parts).resolve()
  sandbox_prefix = str(sandbox_resolved) + os.sep
  if not str(candidate).startswith(sandbox_prefix):
    raise ValueError(
        "Path traversal detected: target path escapes sandbox boundary"
    )
  return candidate


def is_ip_global_and_safe(ip_str: str) -> bool:
  """Return True only if IP is globally routable and not private/loopback.

  Args:
    ip_str: IPv4 or IPv6 address string.

  Returns:
    True if the IP address is globally routable and safe.
  """
  try:
    ip_obj = ipaddress.ip_address(ip_str)
  except ValueError:
    return False

  if (
      ip_obj.is_private
      or ip_obj.is_loopback
      or ip_obj.is_link_local
      or ip_obj.is_multicast
      or ip_obj.is_reserved
      or ip_obj.is_unspecified
  ):
    return False
  return True


@functools.lru_cache(maxsize=2048)
def _verify_hostname_resolves_to_public_ip(hostname: str) -> None:
  """Resolve hostname once and verify all A/AAAA records are public IPs."""
  try:
    addr_info = socket.getaddrinfo(hostname, None)
    for _, _, _, _, sockaddr in addr_info:
      ip_str = sockaddr[0]
      if not is_ip_global_and_safe(ip_str):
        raise ValueError(
            f"Hostname {hostname} resolved to non-public IP {ip_str}"
        )
  except socket.gaierror:
    pass


def validate_external_image_url(
    url: str, allow_synthetic_scheme: bool = True
) -> str:
  """Validate an image URL against SSRF risks with cached DNS resolution.

  Args:
    url: Candidate image URL string.
    allow_synthetic_scheme: Whether `synthetic://` test URLs are permitted.

  Returns:
    Validated URL string.

  Raises:
    ValueError: If the URL is empty, uses a disallowed scheme, or targets a
      non-public IP or internal metadata host.
  """
  cleaned = url.strip()
  if not cleaned:
    raise ValueError("Empty URL")

  parsed = urllib.parse.urlparse(cleaned)
  if allow_synthetic_scheme and parsed.scheme == "synthetic":
    return cleaned

  if parsed.scheme not in ("https", "http"):
    raise ValueError(
        f"Disallowed URL scheme '{parsed.scheme}'. Only https/http allowed."
    )

  hostname = parsed.hostname
  if not hostname:
    raise ValueError("URL is missing a valid hostname")

  lowered_host = hostname.lower()
  if lowered_host in (
      "localhost",
      "metadata.google.internal",
      "metadata",
      "169.254.169.254",
  ):
    raise ValueError(f"Blocked internal/metadata hostname: {hostname}")

  try:
    ipaddress.ip_address(lowered_host)
    if not is_ip_global_and_safe(lowered_host):
      raise ValueError(f"Blocked non-public IP address: {lowered_host}")
    return cleaned
  except ValueError as exc:
    if "Blocked non-public IP" in str(exc):
      raise

  _verify_hostname_resolves_to_public_ip(lowered_host)
  return cleaned


def generate_csrf_token(session_id: str) -> str:
  """Generate an HMAC-signed CSRF token bound to the session identifier."""
  nonce = secrets.token_hex(16)
  payload = f"{session_id}:{nonce}"
  sig = hmac.new(
      CSRF_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
  ).hexdigest()
  return f"{nonce}.{sig}"


def verify_csrf_token(session_id: str, token: str) -> bool:
  """Constant-time verification of the HMAC-signed CSRF token."""
  if not session_id or not token or "." not in token:
    return False
  nonce, provided_sig = token.split(".", 1)
  payload = f"{session_id}:{nonce}"
  expected_sig = hmac.new(
      CSRF_SECRET_KEY.encode("utf-8"),
      payload.encode("utf-8"),
      hashlib.sha256,
  ).hexdigest()
  return hmac.compare_digest(provided_sig, expected_sig)


def verify_deployment_access_code(provided_code: str) -> bool:
  """Compare user-supplied access code against the deployment ACCESS_CODE."""
  if not provided_code:
    return False
  return hmac.compare_digest(
      provided_code.strip().encode("utf-8"),
      DEPLOYMENT_ACCESS_CODE.strip().encode("utf-8"),
  )


def create_auth_session_cookie(session_id: str) -> str:
  """Create an HMAC-signed authentication badge bound to `session_id`."""
  exp = int(time.time()) + (3600 * 8)
  payload = f"auth:{session_id}:{exp}"
  sig = hmac.new(
      CSRF_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
  ).hexdigest()
  return f"{exp}.{sig}"


def verify_auth_session_cookie(session_id: str, cookie_val: str) -> bool:
  """Verify the HMAC-signed authentication cookie and check its expiration."""
  if not session_id or not cookie_val or "." not in cookie_val:
    return False
  exp_str, provided_sig = cookie_val.split(".", 1)
  try:
    exp = int(exp_str)
  except ValueError:
    return False
  if int(time.time()) > exp:
    return False
  payload = f"auth:{session_id}:{exp}"
  expected_sig = hmac.new(
      CSRF_SECRET_KEY.encode("utf-8"),
      payload.encode("utf-8"),
      hashlib.sha256,
  ).hexdigest()
  return hmac.compare_digest(provided_sig, expected_sig)


class SlidingWindowRateLimiter:
  """In-memory per-client IP sliding-window rate limiter."""

  def __init__(self, max_requests: int = 240, window_seconds: int = 60) -> None:
    self.max_requests = max_requests
    self.window_seconds = window_seconds
    self._hits: Dict[str, Deque[float]] = collections.defaultdict(
        collections.deque
    )

  def check(self, client_key: str) -> None:
    """Enforce sliding-window rate limit for `client_key`."""
    now = time.monotonic()
    q = self._hits[client_key]
    cutoff = now - self.window_seconds
    while q and q[0] < cutoff:
      q.popleft()
    if len(q) >= self.max_requests:
      raise HTTPException(
          status_code=status.HTTP_429_TOO_MANY_REQUESTS,
          detail=(
              "Rate limit exceeded. Please wait before submitting "
              "additional requests."
          ),
      )
    q.append(now)


rate_limiter = SlidingWindowRateLimiter(max_requests=240, window_seconds=60)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
  """Applies HTTP security headers and method allow-listing to responses."""

  ALLOWED_METHODS = {"GET", "POST", "HEAD", "OPTIONS"}

  async def dispatch(self, request: Request, call_next) -> Response:
    if request.method not in self.ALLOWED_METHODS:
      return Response(
          status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
          content="Method Not Allowed",
      )

    client_host = request.client.host if request.client else "local"
    rate_limiter.check(client_host)

    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https: http:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self';"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    if "Cache-Control" not in response.headers:
      response.headers["Cache-Control"] = "no-store, max-age=0"
    return response
