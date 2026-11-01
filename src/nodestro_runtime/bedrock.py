"""Amazon Bedrock: settings, AWS credentials per operating profile and the free verification check.

Bedrock has no API key (see :mod:`nodestro_runtime.providers`): agents sign their requests with AWS credentials,
which Nodestro never stores. Where the credentials come from depends on the operating profile:

- **Desktop** (and Nodestro Local): ``default_chain`` is boto3's default credential chain of the user's computer
  (environment, ``~/.aws`` files, SSO, instance or container roles); ``profile`` is a named profile of the shared AWS
  config (``boto3.Session(profile_name=...)``). Only the profile's *name* is stored.
- **Server tenants** (a workspace on handed-in storage): the server's own AWS chain belongs to its operator and is
  never used for a tenant. Bedrock is off for a tenant until the operator assigns it an IAM role
  (:class:`BedrockOperatorConfig`, ``NODESTRO_BEDROCK_TENANT_ROLES``). The server then assumes that role with STS
  (the operator's base credentials, ``ExternalId``, ``RoleSessionName=nodestro-<tenant>``, the regional STS endpoint,
  :data:`ROLE_DURATION_SECONDS`) and hands agents a session of their own that holds only the role's refreshable
  credentials. The tenant never sees the role ARN or the account; its status says only ``operator_role: true``.
- **Exported projects**: boto3's default chain of the target machine, with ``AWS_REGION``/``AWS_DEFAULT_REGION`` or
  the exported region (:func:`nodestro_runtime.providers.environment_connection`).

Custom Bedrock endpoints (``base_url``, VPC endpoints) and Bedrock API keys are not offered: a signed request or a
bearer token must only ever go to AWS's regional endpoint (no server-side request forgery, no credential leakage).

:meth:`~nodestro_runtime.workspace.Workspace.verify_provider` checks a configuration with free calls only
(:func:`verify`): STS ``GetCallerIdentity`` and one read of the Bedrock control plane. It never invokes a model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any, Literal

from .problems import NodestroError

logger = logging.getLogger(__name__)

BedrockAuth = Literal["default_chain", "profile", "operator_role"]
DataResidency = Literal["eu_only"]

#: Region names (``eu-central-1``); the same rule the settings page uses.
REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
#: Named profiles of the shared AWS config: a conservative character set (no paths, no spaces, no quotes).
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.+=,@-]{1,64}$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
_EXTERNAL_ID = re.compile(r"^[\w+=,.@:/-]{2,1224}$")
_SESSION_NAME = re.compile(r"[^\w+=,.@-]")

#: The sign-in methods of each profile: what a desktop user may choose, and the one method of a server tenant.
DESKTOP_AUTH: tuple[BedrockAuth, ...] = ("default_chain", "profile")
SERVER_AUTH: tuple[BedrockAuth, ...] = ("operator_role",)
AUTH_METHODS: tuple[BedrockAuth, ...] = ("default_chain", "profile", "operator_role")
RESIDENCIES: tuple[DataResidency, ...] = ("eu_only",)

#: Lifetime of an assumed tenant role's credentials. botocore refreshes refreshable credentials 15 minutes before
#: they expire, so a lifetime of 15 minutes (STS's minimum) would mean a new AssumeRole call for every request; 30
#: minutes keeps them short-lived and refreshes them about every 15 minutes.
ROLE_DURATION_SECONDS = 1800
#: Timeouts and attempts of the calls Nodestro makes itself (STS, verification): short, at most one retry.
CHECK_CONNECT_TIMEOUT = 5.0
CHECK_READ_TIMEOUT = 10.0
CHECK_MAX_ATTEMPTS = 2

ENV_TENANT_ROLES = "NODESTRO_BEDROCK_TENANT_ROLES"
ENV_DATA_RESIDENCY = "NODESTRO_BEDROCK_DATA_RESIDENCY"
ENV_ALLOWED_REGIONS = "NODESTRO_BEDROCK_ALLOWED_REGIONS"


def eu_regions() -> frozenset[str]:
    """Orkexa's EU regions (the default allowlist of ``eu_only``)."""
    from orkexa.providers.bedrock_residency import EU_REGIONS

    return EU_REGIONS


# ---- settings --------------------------------------------------------------------------------


def check_region(value: str) -> str:
    region = value.strip()
    if not REGION_PATTERN.fullmatch(region):
        raise NodestroError(
            "provider.region_invalid",
            f"Invalid AWS region {region[:40]!r}: use a name like eu-central-1",
            region=region[:40],
        )
    return region


def check_profile_name(value: str) -> str:
    name = value.strip()
    if not PROFILE_PATTERN.fullmatch(name):
        raise NodestroError(
            "provider.profile_name_invalid",
            "Invalid AWS profile name: letters, digits and _ . + = , @ - only (at most 64 characters)",
        )
    return name


def check_regions(regions: tuple[str, ...], data_residency: str | None) -> None:
    """With ``eu_only`` every region must be an EU region (Orkexa's :data:`EU_REGIONS`)."""
    if data_residency == "eu_only":
        outside = sorted(set(regions) - eu_regions())
        if outside:
            raise NodestroError(
                "provider.data_residency_region",
                f"Data residency 'eu_only' allows only EU regions; not allowed: {', '.join(outside)}",
                region=", ".join(outside),
            )


@dataclass(frozen=True)
class BedrockSettings:
    """How agents reach Bedrock: region, sign-in method and data residency (never credentials).

    ``allowed_regions`` is the operator's allowlist on a server (``None``: Orkexa's default for the residency)."""

    region: str | None = None
    auth: BedrockAuth = "default_chain"
    profile_name: str | None = None
    data_residency: DataResidency | None = None
    allowed_regions: tuple[str, ...] | None = None

    def exported(self) -> dict[str, Any]:
        """What an exported project keeps: the region and the residency (never the profile, never a role)."""
        found: dict[str, Any] = {}
        if self.region:
            found["region"] = self.region
        if self.data_residency:
            found["data_residency"] = self.data_residency
        return found


# ---- the operator's policy (server) -----------------------------------------------------------


@dataclass(frozen=True, repr=False)
class TenantRole:
    """The IAM role the operator assigned to one tenant (never shown to the tenant, never in ``repr``)."""

    role_arn: str
    external_id: str

    def __post_init__(self) -> None:
        if not _ROLE_ARN.fullmatch(self.role_arn):
            raise ValueError("role_arn must be an IAM role ARN (arn:aws:iam::<account>:role/<name>)")
        if not _EXTERNAL_ID.fullmatch(self.external_id):
            raise ValueError("external_id must have 2 to 1224 characters of letters, digits and _+=,.@:/-")

    def __repr__(self) -> str:
        return "TenantRole(<operator-assigned>)"

    @property
    def fingerprint(self) -> str:
        """A short hash of the role (to key caches; reveals nothing)."""
        return hashlib.sha256(f"{self.role_arn}\0{self.external_id}".encode()).hexdigest()[:16]


#: ``sts_client_factory(region)``: an STS client with the operator's base credentials (tests inject a stub).
StsClientFactory = Callable[[str], Any]


@dataclass(frozen=True, repr=False)
class BedrockServerPolicy:
    """The operator's Bedrock rules for one tenant of a server (part of its ``WorkspaceStorage``).

    ``role`` ``None``: Bedrock is not available to the tenant. ``data_residency`` ``eu_only`` is forced (the tenant
    cannot relax it); ``allowed_regions`` limits the regions the tenant may choose (``None``: any; with ``eu_only``
    the EU regions)."""

    tenant_id: str
    role: TenantRole | None = None
    data_residency: DataResidency | None = None
    allowed_regions: tuple[str, ...] | None = None
    #: Builds the STS client that assumes the role (default: :func:`operator_sts_client`).
    sts_client_factory: StsClientFactory | None = field(default=None, compare=False)

    def __repr__(self) -> str:
        role = "assigned" if self.role is not None else None
        return (
            f"BedrockServerPolicy(tenant_id={self.tenant_id!r}, role={role!r}, data_residency={self.data_residency!r}, "
            f"allowed_regions={self.allowed_regions!r})"
        )

    def regions(self) -> tuple[str, ...] | None:
        """The regions a tenant may use (the operator's list, or the EU regions with ``eu_only``)."""
        if self.allowed_regions is not None:
            return self.allowed_regions
        if self.data_residency == "eu_only":
            return tuple(sorted(eu_regions()))
        return None


@dataclass(frozen=True, repr=False)
class BedrockOperatorConfig:
    """The operator's Bedrock settings of a server process, read from its environment (:meth:`from_env`).

    ``for_tenant(tenant_id)`` gives the policy that goes into the tenant's ``WorkspaceStorage``."""

    roles: Mapping[str, TenantRole] = field(default_factory=dict)
    data_residency: DataResidency | None = None
    allowed_regions: tuple[str, ...] | None = None
    sts_client_factory: StsClientFactory | None = field(default=None, compare=False)

    def __repr__(self) -> str:
        return (
            f"BedrockOperatorConfig(tenants={len(self.roles)}, data_residency={self.data_residency!r}, "
            f"allowed_regions={self.allowed_regions!r})"
        )

    def for_tenant(self, tenant_id: str) -> BedrockServerPolicy:
        return BedrockServerPolicy(
            tenant_id=tenant_id,
            role=self.roles.get(tenant_id),
            data_residency=self.data_residency,
            allowed_regions=self.allowed_regions,
            sts_client_factory=self.sts_client_factory,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> BedrockOperatorConfig:
        """Read ``NODESTRO_BEDROCK_TENANT_ROLES`` (JSON, or ``..._FILE`` with a path to it),
        ``NODESTRO_BEDROCK_DATA_RESIDENCY`` (``eu_only``) and ``NODESTRO_BEDROCK_ALLOWED_REGIONS`` (comma-separated).

        ``NODESTRO_BEDROCK_TENANT_ROLES`` maps tenant ids to ``{"role_arn": ..., "external_id": ...}``. Nothing is
        set: no tenant can use Bedrock.

        Raises:
            NodestroError: ``profile.invalid`` with ``field`` and ``value`` (never a role ARN or external id).
        """

        def invalid(name: str, reason: str, value: str = "") -> NodestroError:
            return NodestroError("profile.invalid", f"{name}: {reason}", field=name, value=value)

        text = environ.get(ENV_TENANT_ROLES, "").strip()
        path = environ.get(f"{ENV_TENANT_ROLES}_FILE", "").strip()
        if text and path:
            raise invalid(ENV_TENANT_ROLES, f"set either {ENV_TENANT_ROLES} or {ENV_TENANT_ROLES}_FILE, not both")
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise invalid(f"{ENV_TENANT_ROLES}_FILE", f"cannot read the file ({exc.strerror})", path) from None
        roles: dict[str, TenantRole] = {}
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                raise invalid(ENV_TENANT_ROLES, "not valid JSON") from None
            if not isinstance(data, dict):
                raise invalid(
                    ENV_TENANT_ROLES, 'must be a JSON object {"<tenant>": {"role_arn": ..., "external_id": ...}}'
                )
            for tenant, entry in data.items():
                if not isinstance(tenant, str) or not tenant.strip():
                    raise invalid(ENV_TENANT_ROLES, "a tenant id is empty")
                if not isinstance(entry, dict) or set(entry) - {"role_arn", "external_id"}:
                    raise invalid(ENV_TENANT_ROLES, f"tenant {tenant!r}: needs exactly role_arn and external_id")
                try:
                    roles[tenant] = TenantRole(str(entry.get("role_arn") or ""), str(entry.get("external_id") or ""))
                except ValueError as exc:
                    raise invalid(ENV_TENANT_ROLES, f"tenant {tenant!r}: {exc}") from None
        residency_text = environ.get(ENV_DATA_RESIDENCY, "").strip()
        if residency_text not in ("", "eu_only"):
            raise invalid(ENV_DATA_RESIDENCY, "must be eu_only or empty", residency_text[:40])
        residency: DataResidency | None = "eu_only" if residency_text else None
        regions: tuple[str, ...] | None = None
        regions_text = environ.get(ENV_ALLOWED_REGIONS, "").strip()
        if regions_text:
            items = tuple(dict.fromkeys(item.strip() for item in regions_text.split(",") if item.strip()))
            for item in items:
                if not REGION_PATTERN.fullmatch(item):
                    raise invalid(ENV_ALLOWED_REGIONS, f"{item[:40]!r} is not an AWS region name", item[:40])
            try:
                check_regions(items, residency)
            except NodestroError as exc:
                raise invalid(ENV_ALLOWED_REGIONS, str(exc), regions_text[:200]) from None
            regions = items or None
        return cls(roles=roles, data_residency=residency, allowed_regions=regions)


# ---- sessions -------------------------------------------------------------------------------


def _boto3() -> Any:
    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 comes with orkexa[all]
        raise NodestroError(
            "provider.bedrock_not_available", "Amazon Bedrock needs boto3 (pip install 'orkexa[bedrock]')"
        ) from None
    return boto3


def check_config() -> Any:
    """The botocore config of Nodestro's own AWS calls: short timeouts, at most one retry, no configured endpoints."""
    from botocore.config import Config

    return Config(
        connect_timeout=CHECK_CONNECT_TIMEOUT,
        read_timeout=CHECK_READ_TIMEOUT,
        retries={"mode": "standard", "total_max_attempts": CHECK_MAX_ATTEMPTS},
        ignore_configured_endpoint_urls=True,
        user_agent_extra="nodestro",
    )


def sts_endpoint(region: str) -> str:
    """The regional STS endpoint (never the global one, never an endpoint from the environment)."""
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://sts.{region}.{suffix}"


def desktop_session(settings: BedrockSettings) -> Any:
    """A boto3 session from the user's own AWS configuration (default chain or a named profile)."""
    boto3 = _boto3()
    from botocore.exceptions import ProfileNotFound

    profile = settings.profile_name if settings.auth == "profile" else None
    try:
        return boto3.Session(profile_name=profile, region_name=settings.region)
    except ProfileNotFound:
        raise NodestroError(
            "provider.aws_profile_not_found",
            "The AWS profile of the Bedrock settings does not exist in the AWS configuration of this computer",
        ) from None


def operator_sts_client(region: str) -> Any:
    """An STS client with the operator's base credentials (the server's own chain), regional endpoint."""
    boto3 = _boto3()
    return boto3.Session().client("sts", region_name=region, endpoint_url=sts_endpoint(region), config=check_config())


def role_session_name(tenant_id: str) -> str:
    """``nodestro-<tenant>`` within STS's rules (``[\\w+=,.@-]``, at most 64 characters)."""
    name = "nodestro-" + _SESSION_NAME.sub("-", tenant_id)
    if len(name) > 64:
        name = name[:47] + "-" + hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
    return name


class TenantCredentials:
    """The refreshable credentials of one tenant's assumed role in one region.

    Nothing is fetched until a request needs credentials; botocore refreshes them before they expire. One instance
    belongs to one workspace (one tenant) and is never shared with another tenant."""

    def __init__(self, policy: BedrockServerPolicy, region: str) -> None:
        if policy.role is None:  # checked by the caller; kept as a guard
            raise not_available()
        self._role = policy.role
        self._tenant = policy.tenant_id
        self._region = region
        self._factory = policy.sts_client_factory or operator_sts_client
        from botocore.credentials import DeferredRefreshableCredentials

        self.credentials = DeferredRefreshableCredentials(refresh_using=self._fetch, method="nodestro-assume-role")

    def __repr__(self) -> str:
        return f"TenantCredentials(tenant={self._tenant!r}, region={self._region!r})"

    def _fetch(self) -> dict[str, Any]:
        try:
            client = self._factory(self._region)
            response = client.assume_role(
                RoleArn=self._role.role_arn,
                RoleSessionName=role_session_name(self._tenant),
                ExternalId=self._role.external_id,
                DurationSeconds=ROLE_DURATION_SECONDS,
            )
            credentials = response["Credentials"]
            expiration = credentials["Expiration"]
            return {
                "access_key": credentials["AccessKeyId"],
                "secret_key": credentials["SecretAccessKey"],
                "token": credentials["SessionToken"],
                "expiry_time": expiration.isoformat() if isinstance(expiration, datetime) else str(expiration),
            }
        except NodestroError:
            raise
        except Exception as exc:
            code = error_code(exc)
            # The operator's log gets the AWS error code; the tenant only the stable code (no ARN, no account).
            logger.warning("Assuming the Bedrock role of tenant %s failed: %s", self._tenant, aws_error_name(exc))
            raise BedrockRoleError(code) from None

    def session(self) -> Any:
        """A fresh boto3 session that holds only these credentials (never the server's own chain)."""
        boto3 = _boto3()
        import botocore.session

        core = botocore.session.Session()
        # botocore has no public setter for refreshable credentials; this is the documented pattern of
        # boto3/botocore users (the session never falls back to its own credential chain once it is set).
        core._credentials = self.credentials
        core.set_config_variable("region", self._region)
        return boto3.Session(botocore_session=core, region_name=self._region)


class RoleCache:
    """Tenant credentials of one workspace, by role and region (a workspace serves exactly one tenant)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], TenantCredentials] = {}
        self._lock = threading.Lock()

    def get(self, policy: BedrockServerPolicy, region: str) -> TenantCredentials:
        assert policy.role is not None
        key = (policy.tenant_id, policy.role.fingerprint, region)
        with self._lock:
            found = self._items.get(key)
            if found is None:
                found = self._items[key] = TenantCredentials(policy, region)
            return found


# ---- errors ---------------------------------------------------------------------------------


def not_available() -> NodestroError:
    return NodestroError(
        "provider.bedrock_not_available",
        "Amazon Bedrock is not available for this workspace: the server's operator has not assigned an AWS role to it",
        provider="bedrock",
    )


class BedrockRoleError(NodestroError):
    """The tenant's role could not be assumed (code only; the message names no role and no account)."""

    def __init__(self, code: str) -> None:
        super().__init__(code, f"The AWS role of this workspace could not be used ({code})", provider="bedrock")


_IDENTITY_ARN = re.compile(r"arn:aws[a-z-]*:(?:iam|sts)::\d{12}:[^\s\"',;)]+")
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")


def redact_identity(text: str) -> str:
    """Remove IAM/STS identities (role and user ARNs with their account id) from an AWS error text."""
    text = _IDENTITY_ARN.sub("[AWS-IDENTITY]", text)
    return re.sub(r"(?i)(account(?: id)?[ :=]+)\d{12}", r"\1[ACCOUNT]", text)


_CREDENTIALS_MISSING = ("NoCredentialsError", "PartialCredentialsError", "ProfileNotFound", "SSOTokenLoadError")
_ACCESS_DENIED = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException"})
_CREDENTIALS_INVALID = frozenset(
    {
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "SignatureDoesNotMatch",
        "IncompleteSignature",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidSignatureException",
        "MissingAuthenticationTokenException",
    }
)
_REGION_ERRORS = ("UnknownEndpoint", "InvalidRegion", "NoRegionError", "UnknownRegion")
_UNREACHABLE = ("EndpointConnectionError", "ConnectTimeout", "ReadTimeout", "ConnectionClosed", "ProxyConnection")


def aws_error_name(exc: BaseException) -> str:
    """The AWS error code of a ``ClientError`` or the exception's class name (for logs; never the message)."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    code = error.get("Code") if isinstance(error, Mapping) else None
    return str(code) if code else type(exc).__name__


def error_code(exc: BaseException) -> str:
    """A stable Nodestro code for an AWS failure (credentials, permissions, region, network, residency)."""
    if isinstance(exc, NodestroError):
        return exc.code
    from orkexa.errors import DataResidencyError

    if isinstance(exc, DataResidencyError):
        return "provider.data_residency_violation"
    name = aws_error_name(exc)
    kind = type(exc).__name__
    if any(item in kind for item in _CREDENTIALS_MISSING) or "TokenRetrieval" in kind or "SSO" in kind:
        return "provider.aws_credentials_missing"
    if name in _ACCESS_DENIED:
        return "provider.aws_access_denied"
    if name in _CREDENTIALS_INVALID:
        return "provider.aws_credentials_invalid"
    if name in ("ValidationException", "ResourceNotFoundException"):
        return "provider.aws_model_not_found"
    if any(item in kind for item in _REGION_ERRORS):
        return "provider.aws_region_invalid"
    if any(item in kind for item in _UNREACHABLE):
        # A region that does not exist has no endpoint: botocore reports it as a connection error.
        return "provider.aws_unreachable"
    if name in ("ThrottlingException", "Throttling", "TooManyRequestsException"):
        return "provider.aws_rate_limited"
    return "provider.aws_verification_failed"


# ---- verification ---------------------------------------------------------------------------


VerificationState = Literal["not_configured", "configured", "verified", "failed"]


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verify(session: Any, settings: BedrockSettings, model: str | None = None) -> dict[str, Any]:
    """Check the credentials and the region with free calls only; returns ``{state, checked_at, code}``.

    1. STS ``GetCallerIdentity``: the credentials are valid (needs no IAM permission; the identity is not kept).
    2. One read of the Bedrock control plane in the configured region:

       - no ``model``: ``ListFoundationModels(byProvider="anthropic")`` (IAM ``bedrock:ListFoundationModels``);
       - an inference profile (``eu.anthropic...`` or a profile ARN): ``GetInferenceProfile``
         (``bedrock:GetInferenceProfile``, which ``eu_only`` needs anyway); with a data residency, its destination
         regions are checked like before a run;
       - a foundation model id: ``GetFoundationModel`` (``bedrock:GetFoundationModel``).

    Every client has short timeouts and at most one retry; no model is invoked. Errors become stable codes
    (:func:`error_code`); nothing about the AWS identity is returned.
    """
    from orkexa.providers.bedrock_residency import check_destinations, parse_model_id, profile_destinations

    region = settings.region or ""
    config = check_config()
    try:
        allowed = _allowed(settings)
        if allowed is not None and region not in allowed:
            raise NodestroError(
                "provider.data_residency_region", f"The region {region} is outside the allowed regions", region=region
            )
        sts = session.client("sts", region_name=region, endpoint_url=sts_endpoint(region), config=config)
        sts.get_caller_identity()
        control = session.client("bedrock", region_name=region, config=config)
        ref = parse_model_id(model) if model else None
        if ref is None:
            control.list_foundation_models(byProvider="anthropic")
        elif ref.kind in ("inference-profile", "application-inference-profile"):
            response = control.get_inference_profile(inferenceProfileIdentifier=ref.model_id)
            if allowed is not None:
                check_destinations(profile_destinations(response, ref.model_id), allowed, ref.model_id)
        else:
            control.get_foundation_model(modelIdentifier=ref.base_model or ref.model_id)
    except Exception as exc:
        code = error_code(exc)
        logger.info("Bedrock verification failed: %s", aws_error_name(exc))
        return {"state": "failed", "checked_at": now_text(), "code": code}
    return {"state": "verified", "checked_at": now_text(), "code": None}


def _allowed(settings: BedrockSettings) -> frozenset[str] | None:
    if settings.allowed_regions is not None:
        return frozenset(settings.allowed_regions)
    if settings.data_residency == "eu_only":
        return eu_regions()
    return None
