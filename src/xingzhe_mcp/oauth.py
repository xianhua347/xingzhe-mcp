"""Account-bound OAuth provider for MCP; protocol handling is supplied by the MCP SDK."""

import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from xingzhe_mcp.config import Settings
from xingzhe_mcp.storage import Store, Transaction
from xingzhe_mcp.xingzhe import Tokens

SCOPE = "activities:read"


class OAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self.store.transaction() as tx:
            data = await tx.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id or not client_info.redirect_uris:
            raise RegistrationError("invalid_client_metadata", "Client and redirect URI required")
        for uri in client_info.redirect_uris:
            parsed = urlsplit(str(uri))
            if (
                parsed.username
                or parsed.password
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (
                        parsed.scheme == "http"
                        and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                    )
                )
            ):
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a loopback HTTP URI")
        async with self.store.transaction() as tx:
            await tx.put("client", client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource not in (None, self.settings.resource):
            raise AuthorizeError("invalid_request", "Unexpected resource")
        if params.scopes and params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", "Only activities:read is supported")
        params.resource = self.settings.resource
        params.scopes = [SCOPE]
        ticket = secrets.token_urlsafe(32)
        async with self.store.transaction() as tx:
            await tx.put(
                "consent",
                ticket,
                {
                    **params.model_dump(mode="json"),
                    "client_id": client.client_id,
                    "client_name": client.client_name or "MCP client",
                },
                expires_at=time.time() + 600,
            )
        return f"{self.settings.origin}/connect?ticket={ticket}"

    async def approve(self, ticket: str, subject: str, tokens: Tokens) -> str:
        async with self.store.transaction() as tx:
            data = await tx.get("consent", ticket)
            if data is None:
                raise ValueError("Consent expired. Start again from your MCP client.")
            params = AuthorizationParams.model_validate(data)
            await tx.lock(f"connection:{subject}")
            await tx.put("connection", subject, tokens.model_dump(), subject=subject)
            code = AuthorizationCode(
                code=secrets.token_urlsafe(32),
                scopes=[SCOPE],
                expires_at=time.time() + 300,
                client_id=data["client_id"],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.settings.resource,
                subject=subject,
            )
            await tx.delete("consent", ticket)
            await tx.put(
                "code",
                code.code,
                code.model_dump(mode="json"),
                expires_at=code.expires_at,
                subject=subject,
            )
            return construct_redirect_uri(
                str(params.redirect_uri), code=code.code, state=params.state
            )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        async with self.store.transaction() as tx:
            data = await tx.get("code", authorization_code)
        return AuthorizationCode.model_validate(data) if data else None

    async def _issue(
        self, tx: Transaction, scopes: list[str], grant_id: str, subject: str, client_id: str
    ) -> OAuthToken:
        now = int(time.time())
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=now + 3600,
            resource=self.settings.resource,
            subject=subject,
        )
        refresh = RefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=access.client_id,
            scopes=scopes,
            expires_at=now + 30 * 86400,
            resource=self.settings.resource,
            subject=subject,
        )
        for kind, token in (("access", access), ("refresh", refresh)):
            await tx.put(
                kind,
                token.token,
                {**token.model_dump(), "grant_id": grant_id},
                expires_at=token.expires_at,
                grant_id=grant_id,
                subject=subject,
            )
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh.token,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self.store.transaction() as tx:
            data = await tx.get("code", authorization_code.code)
            if data is not None and data["client_id"] == client.client_id and data.get("subject"):
                await tx.delete("code", authorization_code.code)
                return await self._issue(
                    tx,
                    authorization_code.scopes,
                    secrets.token_urlsafe(24),
                    data["subject"],
                    data["client_id"],
                )
        # SDK errors are frozen dataclasses; raise outside generator context managers.
        raise TokenError("invalid_grant", "Code expired or already used")

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        async with self.store.transaction() as tx:
            data = await tx.get("refresh", refresh_token)
        return RefreshToken.model_validate(data) if data else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self.store.transaction() as tx:
            data = await tx.get("refresh", refresh_token.token)
            if data is not None and data["client_id"] == client.client_id and data.get("subject"):
                await tx.revoke(data["grant_id"])
                return await self._issue(
                    tx, scopes, data["grant_id"], data["subject"], data["client_id"]
                )
        raise TokenError("invalid_grant", "Refresh token expired or already used")

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self.store.transaction() as tx:
            data = await tx.get("access", token)
        if not data or not data.get("subject") or data["resource"] != self.settings.resource:
            return None
        access = AccessToken.model_validate(data)
        if access.expires_at is None or access.expires_at <= time.time():
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        async with self.store.transaction() as tx:
            data = await tx.get(kind, token.token)
            if data:
                await tx.revoke(data["grant_id"])
