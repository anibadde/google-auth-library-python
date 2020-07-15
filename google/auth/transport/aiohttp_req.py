# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Transport adapter for Async HTTP (aiohttp)."""

from __future__ import absolute_import

import asyncio
import functools
import logging


import aiohttp
import six


from google.auth import exceptions
from google.auth import transport
from google.auth.transport import requests


_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/appengine.apis",
    "https://www.googleapis.com/auth/userinfo.email",
]

_LOGGER = logging.getLogger(__name__)

# Timeout can be re-defined depending on async requirement. Currently made 60s more than
# sync timeout.
_DEFAULT_TIMEOUT = 180  # in seconds


class _Response(transport.Response):
    """
    Requests transport response adapter.

    Args:
        response (requests.Response): The raw Requests response.
    """

    def __init__(self, response):
        self.response = response

    @property
    def status(self):
        return self.response.status

    @property
    def headers(self):
        return self.response.headers

    @property
    def data(self):
        return self.response.content


class Request(transport.Request):
    """Requests request adapter.

    This class is used internally for making requests using asyncio transports
    in a consistent way. If you use :class:`AuthorizedSession` you do not need
    to construct or use this class directly.

    This class can be useful if you want to manually refresh a
    :class:`~google.auth.credentials.Credentials` instance::

        import google.auth.transport.aiohttp_req
        import aiohttp

        request = google.auth.transport.aiohttp_req.Request()

        credentials.refresh(request)

    Args:
        session (aiohttp.ClientSession): An instance :class: aiohttp.ClientSession used
            to make HTTP requests. If not specified, a session will be created.

    .. automethod:: __call__
    """

    def __init__(self, session=None):
        if not session:
            session = aiohttp.ClientSession()
        self.session = session

    async def __call__(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        timeout=_DEFAULT_TIMEOUT,
        **kwargs
    ):
        """
        Make an HTTP request using aiohttp.

        Args:
            url (str): The URI to be requested.
            method (str): The HTTP method to use for the request. Defaults
                to 'GET'.
            body (bytes): The payload / body in HTTP request.
            headers (Mapping[str, str]): Request headers.
            timeout (Optional[int]): The number of seconds to wait for a
                response from the server. If not specified or if None, the
                requests default timeout will be used.
            kwargs: Additional arguments passed through to the underlying
                requests :meth:`~requests.Session.request` method.

        Returns:
            google.auth.transport.Response: The HTTP response.

        Raises:
            google.auth.exceptions.TransportError: If any exception occurred.
        """
        try:
            _LOGGER.debug("Making request: %s %s", method, url)
            response = await self.session.request(
                method, url, data=body, headers=headers, timeout=timeout, **kwargs
            )
            return _Response(response)

        except aiohttp.ClientError as caught_exc:
            new_exc = exceptions.TransportError(caught_exc)
            six.raise_from(new_exc, caught_exc)

        except asyncio.TimeoutError as caught_exc:
            new_exc = exceptions.TransportError(caught_exc)
            six.raise_from(new_exc, caught_exc)


class AuthorizedSession(aiohttp.ClientSession):
    """This is an async implementation of the Authorized Session class. We utilize an
    aiohttp transport instance, and the interface mirrors the google.auth.transport.requests
    Authorized Session class, except for the change in the transport used in the async use case.
    """

    def __init__(
        self,
        credentials,
        refresh_status_codes=transport.DEFAULT_REFRESH_STATUS_CODES,
        max_refresh_attempts=transport.DEFAULT_MAX_REFRESH_ATTEMPTS,
        refresh_timeout=None,
        auth_request=None,
    ):
        super(AuthorizedSession, self).__init__()
        self.credentials = credentials
        self._refresh_status_codes = refresh_status_codes
        self._max_refresh_attempts = max_refresh_attempts
        self._refresh_timeout = refresh_timeout
        self._is_mtls = False
        self._auth_request = auth_request
        self._auth_request_session = None
        self._loop = asyncio.get_event_loop()
        self._refresh_lock = asyncio.Lock()

        if auth_request is None:
            self._auth_request_session = aiohttp.ClientSession()
            auth_request = Request(self._auth_request_session)
            self._auth_request = auth_request

    def configure_mtls_channel(self, client_cert_callback=None):
        """Configure the client certificate and key for SSL connection.

        If client certificate and key are successfully obtained (from the given
        client_cert_callback or from application default SSL credentials), a
        :class:`_MutualTlsAdapter` instance will be mounted to "https://" prefix.

        Args:
            client_cert_callback (Optional[Callable[[], (bytes, bytes)]]):
                The optional callback returns the client certificate and private
                key bytes both in PEM format.
                If the callback is None, application default SSL credentials
                will be used.

        Raises:
            google.auth.exceptions.MutualTLSChannelError: If mutual TLS channel
                creation failed for any reason.

        try:
            import OpenSSL
        except ImportError as caught_exc:
            new_exc = exceptions.MutualTLSChannelError(caught_exc)
            six.raise_from(new_exc, caught_exc)

        try:
            self._is_mtls, cert, key = google.auth.transport._mtls_helper.get_client_cert_and_key(
                client_cert_callback
            )

            if self._is_mtls:
                mtls_adapter = _MutualTlsAdapter(cert, key)
                self.mount("https://", mtls_adapter)
        except (
            exceptions.ClientCertError,
            ImportError,
            OpenSSL.crypto.Error,
        ) as caught_exc:
            new_exc = exceptions.MutualTLSChannelError(caught_exc)
            six.raise_from(new_exc, caught_exc)
        """

    async def request(
        self,
        method,
        url,
        data=None,
        headers=None,
        max_allowed_time=None,
        timeout=_DEFAULT_TIMEOUT,
        **kwargs
    ):

        # Use a kwarg for this instead of an attribute to maintain
        # thread-safety.
        _credential_refresh_attempt = kwargs.pop("_credential_refresh_attempt", 0)
        # Make a copy of the headers. They will be modified by the credentials
        # and we want to pass the original headers if we recurse.
        request_headers = headers.copy() if headers is not None else {}

        # Do not apply the timeout unconditionally in order to not override the
        # _auth_request's default timeout.
        auth_request = (
            self._auth_request
            if timeout is None
            else functools.partial(self._auth_request, timeout=timeout)
        )

        remaining_time = max_allowed_time

        with requests.TimeoutGuard(remaining_time, asyncio.TimeoutError) as guard:
            self.credentials.before_request(auth_request, method, url, request_headers)

        with requests.TimeoutGuard(remaining_time, asyncio.TimeoutError) as guard:
            response = await super(AuthorizedSession, self).request(
                method,
                url,
                data=data,
                headers=request_headers,
                timeout=timeout,
                **kwargs
            )

        remaining_time = guard.remaining_timeout

        if (
            response.status in self._refresh_status_codes
            and _credential_refresh_attempt < self._max_refresh_attempts
        ):

            _LOGGER.info(
                "Refreshing credentials due to a %s response. Attempt %s/%s.",
                response.status,
                _credential_refresh_attempt + 1,
                self._max_refresh_attempts,
            )

            # Do not apply the timeout unconditionally in order to not override the
            # _auth_request's default timeout.
            auth_request = (
                self._auth_request
                if timeout is None
                else functools.partial(self._auth_request, timeout=timeout)
            )

            with requests.TimeoutGuard(remaining_time, asyncio.TimeoutError) as guard:
                async with self._refresh_lock:
                    await self._loop.run_in_executor(
                        None, self.credentials.refresh, auth_request
                    )

            remaining_time = guard.remaining_timeout

            return await self.request(
                method,
                url,
                data=data,
                headers=headers,
                max_allowed_time=remaining_time,
                timeout=timeout,
                _credential_refresh_attempt=_credential_refresh_attempt + 1,
                **kwargs
            )

        await self._auth_request_session.close()

        return response
