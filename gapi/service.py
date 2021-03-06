# Copyright 2007 Robin Gottfried <copyright@kebet.cz>
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
#
__author__ = 'Robin Gottfried <google@kebet.cz>'

import logging
from urllib import urlencode
from google.appengine.api import memcache
from json import loads, dumps
from uuid import uuid1
from copy import deepcopy
from gapi import AUTH_TYPE_V1, AUTH_TYPE_V2
from .gapi_utils import api_fetch
from .oauth2 import TokenRequest
from .exceptions import \
    DailyLimitExceededException, \
    GoogleApiHttpException, \
    InvalidCredentialsException, \
    NotFoundException,  \
    RateLimitExceededException, \
    UnauthorizedException, \
    UnauthorizedUrl
from google.appengine.api.urlfetch_errors import DeadlineExceededError


class Service(object):

    BASE_URL = 'https://www.googleapis.com'

    def __init__(self, service_email, service_key, scope=None, email=None, validate_certificate=True, auth_type=AUTH_TYPE_V2):
        self._email = None
        self._scope = None
        self.scope = scope
        self.email = email
        self.validate_certificate = validate_certificate
        self._service_email = service_email
        self._service_key = service_key
        self.batch_mode = False
        self._batch_items = {}
        self.auth_type = auth_type

    def _get_token(self):
        if self.token is None:
            self.token = TokenRequest(self._service_email, self._service_key, self.scope, self.email, validate_certificate=self.validate_certificate)
        return self.token

    def _get_email(self):
        return self._email

    def _set_email(self, email):
        if self._email != email:
            self.token = None
            self._email = email

    def _del_email(self):
        self._email = None

    email = property(_get_email, _set_email, _del_email)

    def _get_scope(self):
        return self._scope

    def _set_scope(self, scope):
        if self._scope != scope:
            self._scope = scope
            self.token = None

    def _del_scope(self):
        self._scope = None
        self.token = None

    scope = property(_get_scope, _set_scope, _del_scope)

    def _add_batch(self, callback, **kwargs):
        uuid = str(uuid1())
        kwargs['__callback__'] = callback
        self._batch_items[uuid] = kwargs

    def fetch_batch(self):
        if not self._batch_items:
            return
        from .multipart import build_multipart, parse_multipart
        url = 'https://www.googleapis.com/batch'
        method = 'POST'
        headers, payload = build_multipart(self._batch_items)

        responses = parse_multipart(api_fetch(url=url, method=method, headers=headers, payload=payload, validate_certificate=self.validate_certificate))
        for uuid, response in responses.items():
            item = self._batch_items.pop(uuid)
            try:
                response = self._parse_response(response, item['kwargs'])
            except Exception, e:
                response = e
            item['__callback__'](response=response, request=item)

    def _get_oauth1_headers(self, url, method, headers, params):
        from .gdata_minimal.http_core import HttpRequest
        from .gdata_minimal.gauth import TwoLeggedOAuthHmacToken
        request = HttpRequest(url, method, headers)
        token = TwoLeggedOAuthHmacToken(self._service_email, self._service_key, self.email)
        token.modify_request(request)
        headers.update(request.headers)
        params.update(request.uri.query)
        return str(request.uri)

    def fetch(self, url, method='GET', headers={}, payload=None, params={}):
        headers = dict([(key.lower(), value) for key, value in deepcopy(headers).items()])
        params = deepcopy(params)
        if '_callback' in params:
            callback = params.pop('_callback')
        else:
            callback = None
        kwargs = {}
        for k in 'url', 'method', 'headers', 'payload', 'params':
            kwargs[k] = locals()[k]
        if 'content-type' not in headers:
            headers['content-type'] = 'application/json'
        if params:
            url += '?' + urlencode(params)
        if self.auth_type == AUTH_TYPE_V2:
            headers['authorization'] = self._get_token()
        elif self.auth_type == AUTH_TYPE_V1:
            # modifies headers and params as well
            url = self._get_oauth1_headers(url, method, headers, params)
        if payload and headers['content-type'] == 'application/json':
            payload = dumps(payload)
        # logging.debug('Fetching: %s' % url)
        if callback:
            self._add_batch(callback, url=url, method=method, headers=headers, payload=payload, kwargs=kwargs)  # FIXME: kwargs are duplicate argument
            return None
        else:
            response = api_fetch(url=url, method=method, headers=headers, payload=payload, validate_certificate=self.validate_certificate)
            return self._parse_response(response, kwargs)

    def _parse_response(self, result, kwargs):
        if str(result.status_code)[0] != '2':
            if result.status_code == 404:
                raise NotFoundException(result)
            else:
                headers = dict([ (key.lower(), value.lower()) for key, value in result.headers.items()])
                if headers['content-type'].lower().startswith('application/json'):
                    data = loads(result.content)
                    try:
                        error = data['error']
                    except KeyError:
                        error = 'Invalid error response from server, missing error description: %r ...' % result.content[:30]
                else:
                    error = 'Invalid response content-type (%r): %r...' % (headers['content-type'], result.content[:30])
                if str(result.status_code)[0] == '4' and 'errors' in error and error.get('errors', []):
                    reason = error['errors'][0]['reason']
                    if reason == 'unauthorized' and result.status_code == 401:
                        logging.warning('Got "Unathorized" response for %r when calling %r.' % (self.email, kwargs['url']))
                        raise UnauthorizedException(result, kwargs['url'])
                    elif reason == 'dailyLimitExceeded' and result.status_code == 403:
                        memcache.set('off-' + self.token.service_email, 3600)  # mark off for hour
                        logging.info('Daily limit exceeded while getting %r for %r.' % (kwargs['url'], self.email))
                        raise DailyLimitExceededException(result, kwargs['url'])
                    elif (reason == 'rateLimitExceeded' or 'User Rate Limit Exceeded' in result.content) and result.status_code == 403:
                        logging.info('Rate limit exceeded while getting %r for %r.' % (kwargs['url'], self.email))
                        raise RateLimitExceededException(result, kwargs['url'])
                    elif reason == 'authError' and result.status_code == 401:
                        logging.info('Invalid credentials while getting %r for %r.' % (kwargs['url'], self.email))
                        raise InvalidCredentialsException(result, kwargs['url'])
                    if reason == 'push.webhookUrlUnauthorized' and result.status_code == 401:
                        logging.info('Unauthorized webhook url %r (%r).' % (kwargs.get('address', 'n/a'), self.email))
                        raise UnauthorizedUrl(result, kwargs.get('address', 'n/a'))
                    else:
                        raise GoogleApiHttpException(result)
                else:
                    raise GoogleApiHttpException(result)
        else:
            if result.status_code == 204:
                return None
            else:
                if result.headers.get('content-type').startswith('application/json'):
                    return ApiResult(loads(result.content), self.fetch, kwargs)
                else:
                    return result


class ApiResult(dict):

    def __init__(self, data, query, query_kargs):
        self.query = query
        self.query_kwargs = query_kargs
        super(self.__class__, self).__init__(data)

    def get_next_page(self, page_token):
        kwargs = deepcopy(self.query_kwargs)
        kwargs['params']['pageToken'] = page_token
        return self.query(**kwargs)

    def next_page(self):
        page_token = self.get('nextPageToken', None)
        if page_token:
            return self.get_next_page(page_token)

    def iter_pages(self):
        yield self

        page = self.next_page()
        while page:
            yield page
            page = page.next_page()
