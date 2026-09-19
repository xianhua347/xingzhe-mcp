"""Single-owner OAuth provider for MCP; protocol handling is supplied by the MCP SDK."""

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from xingzhe_mcp.config import Settings
from xingzhe_mcp.storage import Store, Transaction

SCOPE = "activities:read"


class OAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id != self.settings.mcp_client_id:
            return None
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=self.settings.mcp_client_secret.get_secret_value(),
            client_name="Xingzhe MCP client",
            scope=SCOPE,
            token_endpoint_auth_method="client_secret_post",
            redirect_uris=[AnyUrl(str(uri)) for uri in self.settings.mcp_redirect_uris],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Configure a client and exact redirect URIs in the environment")

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
                "consent", ticket, params.model_dump(mode="json"), expires_at=time.time() + 600
            )
        return f"{self.settings.origin}/consent?ticket={ticket}"

    async def approve(self, ticket: str) -> str:
        async with self.store.transaction() as tx:
            data = await tx.get("consent", ticket)
            if data is None:
                raise ValueError("Consent expired. Start again from your MCP client.")
            params = AuthorizationParams.model_validate(data)
            code = AuthorizationCode(
                code=secrets.token_urlsafe(32),
                scopes=[SCOPE],
                expires_at=time.time() + 300,
                client_id=self.settings.mcp_client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.settings.resource,
                subject="owner",
            )
            await tx.delete("consent", ticket)
            await tx.put(
                "code", code.code, code.model_dump(mode="json"), expires_at=code.expires_at
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

    async def _issue(self, tx: Transaction, scopes: list[str], grant_id: str) -> OAuthToken:
        now = int(time.time())
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=self.settings.mcp_client_id,
            scopes=scopes,
            expires_at=now + 3600,
            resource=self.settings.resource,
            subject="owner",
        )
        refresh = RefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=access.client_id,
            scopes=scopes,
            expires_at=now + 30 * 86400,
            resource=self.settings.resource,
            subject="owner",
        )
        for kind, token in (("access", access), ("refresh", refresh)):
            await tx.put(
                kind,
                token.token,
                {**token.model_dump(), "grant_id": grant_id},
                expires_at=token.expires_at,
                grant_id=grant_id,
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
            if data is not None and data["client_id"] == client.client_id:
                await tx.delete("code", authorization_code.code)
                return await self._issue(tx, authorization_code.scopes, secrets.token_urlsafe(24))
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
            if data is not None and data["client_id"] == client.client_id:
                await tx.revoke(data["grant_id"])
                return await self._issue(tx, scopes, data["grant_id"])
        raise TokenError("invalid_grant", "Refresh token expired or already used")

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self.store.transaction() as tx:
            data = await tx.get("access", token)
        if not data or data["resource"] != self.settings.resource:
            return None
        if data["client_id"] != self.settings.mcp_client_id:
            return None
        return AccessToken.model_validate(data)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        async with self.store.transaction() as tx:
            data = await tx.get(kind, token.token)
            if data:
                await tx.revoke(data["grant_id"])
