# -*- coding: utf-8 -*-
# Copyright 2012-2021 CERN
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Thomas Beermann <thomas.beermann@cern.ch>, 2012-2020
# - Vincent Garonne <vincent.garonne@cern.ch>, 2012-2018
# - Yun-Pin Sun <winter0128@gmail.com>, 2013
# - Mario Lassnig <mario.lassnig@cern.ch>, 2013-2021
# - Cedric Serfon <cedric.serfon@cern.ch>, 2014-2020
# - Ralph Vigne <ralph.vigne@cern.ch>, 2015
# - Joaquín Bogado <jbogado@linti.unlp.edu.ar>, 2015-2018
# - Martin Barisits <martin.barisits@cern.ch>, 2016-2020
# - Tobias Wegner <twegner@cern.ch>, 2017
# - Brian Bockelman <bbockelm@cse.unl.edu>, 2017-2018
# - Robert Illingworth <illingwo@fnal.gov>, 2018
# - Hannes Hansen <hannes.jakob.hansen@cern.ch>, 2018
# - Tomas Javurek <tomas.javurek@cern.ch>, 2019-2020
# - Brandon White <bjwhite@fnal.gov>, 2019
# - Ruturaj Gujar <ruturaj.gujar23@gmail.com>, 2019
# - Eric Vaandering <ewv@fnal.gov>, 2019
# - Jaroslav Guenther <jaroslav.guenther@cern.ch>, 2019-2020
# - Andrew Lister <andrew.lister@stfc.ac.uk>, 2019
# - Eli Chadwick <eli.chadwick@stfc.ac.uk>, 2020
# - Patrick Austin <patrick.austin@stfc.ac.uk>, 2020
# - Benedikt Ziemons <benedikt.ziemons@cern.ch>, 2020-2021
# - Radu Carpa <radu.carpa@cern.ch>, 2021
# - David Población Criado <david.poblacion.criado@cern.ch>, 2021

'''
 Client class for callers of the Rucio system
'''

from __future__ import print_function

import errno
import os
import random
import sys
import time
from os import environ, fdopen, path, makedirs, geteuid
from shutil import move
from tempfile import mkstemp

from dogpile.cache import make_region
from requests import Session, Response
from requests.exceptions import ConnectionError
from requests.status_codes import codes
from six.moves.configparser import NoOptionError, NoSectionError
from six.moves.urllib.parse import urlparse

from rucio import version
from rucio.common import exception
from rucio.common.config import config_get, config_get_bool, config_get_int
from rucio.common.exception import (CannotAuthenticate, ClientProtocolNotSupported,
                                    NoAuthInformation, MissingClientParameter,
                                    MissingModuleException, ServerConnectionException)
from rucio.common.extra import import_extras
from rucio.common.utils import build_url, get_tmp_dir, my_key_generator, parse_response, ssh_sign, setup_logger

EXTRA_MODULES = import_extras(['requests_kerberos'])

if EXTRA_MODULES['requests_kerberos']:
    from requests_kerberos import HTTPKerberosAuth  # pylint: disable=import-error

LOG = setup_logger(module_name=__name__)

REGION = make_region(function_key_generator=my_key_generator).configure(
    'dogpile.cache.memory',
    expiration_time=60,
)

STATUS_CODES_TO_RETRY = [502, 503, 504]
MAX_RETRY_BACK_OFF_SECONDS = 10


def back_off(retry_number, reason):
    """
    Sleep a certain amount of time which increases with the retry count
    :param retry_number: the retry iteration
    :param reason: the reason to backoff which will be shown to the user
    """
    sleep_time = min(MAX_RETRY_BACK_OFF_SECONDS, 0.25 * 2 ** retry_number)
    LOG.warning("Waiting {}s due to reason: {} ".format(sleep_time, reason))
    time.sleep(sleep_time)


@REGION.cache_on_arguments(namespace='host_to_choose')
def choice(hosts):
    """
    Select randomly a host

    :param hosts: Lost of hosts
    :return: A randomly selected host.
    """
    return random.choice(hosts)


class BaseClient(object):

    """Main client class for accessing Rucio resources. Handles the authentication."""

    AUTH_RETRIES, REQUEST_RETRIES = 2, 3
    TOKEN_PATH_PREFIX = get_tmp_dir() + '/.rucio_'
    TOKEN_PREFIX = 'auth_token_'
    TOKEN_EXP_PREFIX = 'auth_token_exp_'

    def __init__(self, rucio_host=None, auth_host=None, account=None, ca_cert=None, auth_type=None, creds=None, timeout=600, user_agent='rucio-clients', vo=None):
        """
        Constructor of the BaseClient.
        :param rucio_host: The address of the rucio server, if None it is read from the config file.
        :param rucio_port: The port of the rucio server, if None it is read from the config file.
        :param auth_host: The address of the rucio authentication server, if None it is read from the config file.
        :param auth_port: The port of the rucio authentication server, if None it is read from the config file.
        :param account: The account to authenticate to rucio.
        :param use_ssl: Enable or disable ssl for commucation. Default is enabled.
        :param ca_cert: The path to the rucio server certificate.
        :param auth_type: The type of authentication (e.g.: 'userpass', 'kerberos' ...)
        :param creds: Dictionary with credentials needed for authentication.
        :param user_agent: Indicates the client.
        :param vo: The VO to authenticate into.
        """

        self.host = rucio_host
        self.list_hosts = []
        self.auth_host = auth_host
        self.session = Session()
        self.user_agent = "%s/%s" % (user_agent, version.version_string())  # e.g. "rucio-clients/0.2.13"
        sys.argv[0] = sys.argv[0].split('/')[-1]
        self.script_id = '::'.join(sys.argv[0:2])
        if self.script_id == '':  # Python interpreter used
            self.script_id = 'python'
        try:
            if self.host is None:
                self.host = config_get('client', 'rucio_host')
            if self.auth_host is None:
                self.auth_host = config_get('client', 'auth_host')
        except (NoOptionError, NoSectionError) as error:
            raise MissingClientParameter('Section client and Option \'%s\' cannot be found in config file' % error.args[0])

        try:
            self.trace_host = config_get('trace', 'trace_host')
        except (NoOptionError, NoSectionError):
            self.trace_host = self.host
            LOG.debug('No trace_host passed. Using rucio_host instead')

        self.account = account
        self.vo = vo
        self.ca_cert = ca_cert
        self.auth_type = auth_type
        self.creds = creds
        self.auth_token = None
        self.auth_token_file_path = config_get('client', 'auth_token_file_path', False, None)
        self.headers = {}
        self.timeout = timeout
        self.request_retries = self.REQUEST_RETRIES
        self.token_exp_epoch = None
        self.token_exp_epoch_file = None
        self.auth_oidc_refresh_active = config_get_bool('client', 'auth_oidc_refresh_active', False, False)
        # defining how many minutes before token expires, oidc refresh (if active) should start
        self.auth_oidc_refresh_before_exp = config_get_int('client', 'auth_oidc_refresh_before_exp', False, 20)

        if auth_type is None:
            LOG.debug('No auth_type passed. Trying to get it from the environment variable RUCIO_AUTH_TYPE and config file.')
            if 'RUCIO_AUTH_TYPE' in environ:
                if environ['RUCIO_AUTH_TYPE'] not in ['userpass', 'x509', 'x509_proxy', 'gss', 'ssh', 'saml', 'oidc']:
                    raise MissingClientParameter('Possible RUCIO_AUTH_TYPE values: userpass, x509, x509_proxy, gss, ssh, saml, oidc, vs. ' + environ['RUCIO_AUTH_TYPE'])
                self.auth_type = environ['RUCIO_AUTH_TYPE']
            else:
                try:
                    self.auth_type = config_get('client', 'auth_type')
                except (NoOptionError, NoSectionError) as error:
                    raise MissingClientParameter('Option \'%s\' cannot be found in config file' % error.args[0])

        if self.auth_type == 'oidc':
            if not self.creds:
                self.creds = {}
            # if there are defautl values, check if rucio.cfg does not specify them, otherwise put default
            if 'oidc_refresh_lifetime' not in self.creds or self.creds['oidc_refresh_lifetime'] is None:
                self.creds['oidc_refresh_lifetime'] = config_get('client', 'oidc_refresh_lifetime', False, None)
            if 'oidc_issuer' not in self.creds or self.creds['oidc_issuer'] is None:
                self.creds['oidc_issuer'] = config_get('client', 'oidc_issuer', False, None)
            if 'oidc_audience' not in self.creds or self.creds['oidc_audience'] is None:
                self.creds['oidc_audience'] = config_get('client', 'oidc_audience', False, None)
            if 'oidc_auto' not in self.creds or self.creds['oidc_auto'] is False:
                self.creds['oidc_auto'] = config_get_bool('client', 'oidc_auto', False, False)
            if self.creds['oidc_auto']:
                if 'oidc_username' not in self.creds or self.creds['oidc_username'] is None:
                    self.creds['oidc_username'] = config_get('client', 'oidc_username', False, None)
                if 'oidc_password' not in self.creds or self.creds['oidc_password'] is None:
                    self.creds['oidc_password'] = config_get('client', 'oidc_password', False, None)
            if 'oidc_scope' not in self.creds or self.creds['oidc_scope'] == 'openid profile':
                self.creds['oidc_scope'] = config_get('client', 'oidc_scope', False, 'openid profile')
            if 'oidc_polling' not in self.creds or self.creds['oidc_polling'] is False:
                self.creds['oidc_polling'] = config_get_bool('client', 'oidc_polling', False, False)

        if not self.creds:
            LOG.debug('No creds passed. Trying to get it from the config file.')
            self.creds = {}
            try:
                if self.auth_type in ['userpass', 'saml']:
                    self.creds['username'] = config_get('client', 'username')
                    self.creds['password'] = config_get('client', 'password')
                elif self.auth_type == 'x509':
                    self.creds['client_cert'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_cert'))))
                    if not path.exists(self.creds['client_cert']):
                        raise MissingClientParameter('X.509 client certificate not found: %s' % self.creds['client_cert'])
                    self.creds['client_key'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_key'))))
                    if not path.exists(self.creds['client_key']):
                        raise MissingClientParameter('X.509 client key not found: %s' % self.creds['client_key'])
                    else:
                        perms = oct(os.stat(self.creds['client_key']).st_mode)[-3:]
                        if perms != '400':
                            raise CannotAuthenticate('X.509 authentication selected, but private key (%s) permissions are liberal (required: 400, found: %s)' % (self.creds['client_key'], perms))
                elif self.auth_type == 'x509_proxy':
                    try:
                        self.creds['client_proxy'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'client_x509_proxy'))))
                    except NoOptionError:
                        # Recreate the classic GSI logic for locating the proxy:
                        # - $X509_USER_PROXY, if it is set.
                        # - /tmp/x509up_u`id -u` otherwise.
                        # If neither exists (at this point, we don't care if it exists but is invalid), then rethrow
                        if 'X509_USER_PROXY' in environ:
                            self.creds['client_proxy'] = environ['X509_USER_PROXY']
                        else:
                            fname = '/tmp/x509up_u%d' % geteuid()
                            if path.exists(fname):
                                self.creds['client_proxy'] = fname
                            else:
                                raise MissingClientParameter('Cannot find a valid X509 proxy; not in %s, $X509_USER_PROXY not set, and '
                                                             '\'x509_proxy\' not set in the configuration file.' % fname)
                elif self.auth_type == 'ssh':
                    self.creds['ssh_private_key'] = path.abspath(path.expanduser(path.expandvars(config_get('client', 'ssh_private_key'))))
            except (NoOptionError, NoSectionError) as error:
                if error.args[0] != 'client_key':
                    raise MissingClientParameter('Option \'%s\' cannot be found in config file' % error.args[0])

        rucio_scheme = urlparse(self.host).scheme
        auth_scheme = urlparse(self.auth_host).scheme

        if rucio_scheme != 'http' and rucio_scheme != 'https':
            raise ClientProtocolNotSupported('\'%s\' not supported' % rucio_scheme)

        if auth_scheme != 'http' and auth_scheme != 'https':
            raise ClientProtocolNotSupported('\'%s\' not supported' % auth_scheme)

        if (rucio_scheme == 'https' or auth_scheme == 'https') and ca_cert is None:
            LOG.debug('HTTPS is required, but no ca_cert was passed. Trying to get it from X509_CERT_DIR.')
            self.ca_cert = os.environ.get('X509_CERT_DIR', None)
            if self.ca_cert is None:
                LOG.debug('HTTPS is required, but no ca_cert was passed and X509_CERT_DIR is not defined. Trying to get it from the config file.')
                try:
                    self.ca_cert = path.expandvars(config_get('client', 'ca_cert'))
                except (NoOptionError, NoSectionError):
                    LOG.debug('No ca_cert found in configuration. Falling back to Mozilla default CA bundle (certifi).')
                    self.ca_cert = True

        self.list_hosts = [self.host]

        if account is None:
            LOG.debug('No account passed. Trying to get it from the config file.')
            try:
                self.account = environ['RUCIO_ACCOUNT']
            except KeyError:
                try:
                    self.account = config_get('client', 'account')
                except (NoOptionError, NoSectionError):
                    raise MissingClientParameter('Option \'account\' cannot be found in config file and RUCIO_ACCOUNT is not set.')

        if vo is None:
            LOG.debug('No VO passed. Trying to get it from environment variable RUCIO_VO.')
            try:
                self.vo = environ['RUCIO_VO']
            except KeyError:
                LOG.debug('No VO found. Trying to get it from the config file.')
                try:
                    self.vo = config_get('client', 'vo')
                except (NoOptionError, NoSectionError):
                    LOG.debug('No VO found. Using default VO.')
                    self.vo = 'def'

        # if token file path is defined in the rucio.cfg file, use that file. Currently this prevents authenticating as another user or VO.
        if self.auth_token_file_path:
            self.token_file = self.auth_token_file_path
            self.token_path = '/'.join(self.token_file.split('/')[:-1])
            self.token_exp_epoch_file = self.token_path + '/' + self.TOKEN_EXP_PREFIX + self.account
        else:
            self.token_path = self.TOKEN_PATH_PREFIX + self.account
            if self.vo != 'def':
                self.token_path += '@%s' % self.vo
            self.token_file = self.token_path + '/' + self.TOKEN_PREFIX + self.account
            self.token_exp_epoch_file = self.token_path + '/' + self.TOKEN_EXP_PREFIX + self.account

        self.__authenticate()

        try:
            self.request_retries = int(config_get('client', 'request_retries'))
        except NoOptionError:
            LOG.debug('request_retries not specified in config file. Taking default.')
        except ValueError:
            LOG.debug('request_retries must be an integer. Taking default.')

    def _get_exception(self, headers, status_code=None, data=None):
        """
        Helper method to parse an error string send by the server and transform it into the corresponding rucio exception.

        :param headers: The http response header containing the Rucio exception details.
        :param status_code: The http status code.
        :param data: The data with the ExceptionMessage.

        :return: A rucio exception class and an error string.
        """
        try:
            data = parse_response(data)
        except ValueError:
            data = {}

        exc_cls = 'RucioException'
        exc_msg = 'no error information passed (http status code: %s)' % status_code
        if 'ExceptionClass' in data:
            exc_cls = data['ExceptionClass']
        elif 'ExceptionClass' in headers:
            exc_cls = headers['ExceptionClass']
        if 'ExceptionMessage' in data:
            exc_msg = data['ExceptionMessage']
        elif 'ExceptionMessage' in headers:
            exc_msg = headers['ExceptionMessage']

        if hasattr(exception, exc_cls):
            return getattr(exception, exc_cls), exc_msg
        else:
            return exception.RucioException, "%s: %s" % (exc_cls, exc_msg)

    def _load_json_data(self, response):
        """
        Helper method to correctly load json data based on the content type of the http response.

        :param response: the response received from the server.
        """
        if 'content-type' in response.headers and response.headers['content-type'] == 'application/x-json-stream':
            for line in response.iter_lines():
                if line:
                    yield parse_response(line)
        elif 'content-type' in response.headers and response.headers['content-type'] == 'application/json':
            yield parse_response(response.text)
        else:  # Exception ?
            if response.text:
                yield response.text

    def _send_request(self, url, headers=None, type_='GET', data=None, params=None, stream=False, get_token=False,
                      cert=None, auth=None, verify=None):
        """
        Helper method to send requests to the rucio server. Gets a new token and retries if an unauthorized error is returned.

        :param url: the http url to use.
        :param headers: additional http headers to send.
        :param type_: the http request type to use.
        :param data: post data.
        :param params: (optional) Dictionary or bytes to be sent in the url query string.
        :param get_token: (optional) if it is called from a _get_token function.
        :param cert: (optional) if String, path to the SSL client cert file (.pem). If Tuple, (cert, key) pair.
        :param auth: (optional) auth tuple to enable Basic/Digest/Custom HTTP Auth.
        :param verify: (optional) either a boolean, in which case it controls whether we verify the server’s TLS
                       certificate, or a string, in which case it must be a path to a CA bundle to use.
        :return: the HTTP return body.
        """
        hds = {'X-Rucio-Auth-Token': self.auth_token, 'X-Rucio-Account': self.account, 'X-Rucio-VO': self.vo,
               'Connection': 'Keep-Alive', 'User-Agent': self.user_agent,
               'X-Rucio-Script': self.script_id}

        if headers is not None:
            hds.update(headers)

        if verify is None:
            verify = self.ca_cert

        result = None
        for retry in range(self.AUTH_RETRIES + 1):
            try:
                if type_ == 'GET':
                    result = self.session.get(url, headers=hds, verify=verify, timeout=self.timeout, params=params, stream=True, cert=cert, auth=auth)
                elif type_ == 'PUT':
                    result = self.session.put(url, headers=hds, data=data, verify=verify, timeout=self.timeout)
                elif type_ == 'POST':
                    result = self.session.post(url, headers=hds, data=data, verify=verify, timeout=self.timeout, stream=stream)
                elif type_ == 'DEL':
                    result = self.session.delete(url, headers=hds, data=data, verify=verify, timeout=self.timeout)
                else:
                    return
                if result.status_code in STATUS_CODES_TO_RETRY:
                    back_off(retry, reason='server returned {}'.format(result.status_code))
                    continue
            except ConnectionError as error:
                LOG.error('ConnectionError: ' + str(error))
                if retry > self.request_retries:
                    raise
                continue
            except IOError as error:
                # Handle Broken Pipe
                # While in python3 we can directly catch 'BrokenPipeError', in python2 it doesn't exist.
                if getattr(error, 'errno') != errno.EPIPE:
                    raise
                LOG.error('BrokenPipe: ' + str(error))
                if retry > self.request_retries:
                    raise
                continue

            if result is not None and result.status_code == codes.unauthorized and not get_token:  # pylint: disable-msg=E1101
                self.session = Session()
                self.__get_token()
                hds['X-Rucio-Auth-Token'] = self.auth_token
            else:
                break

        if result is None:
            raise ServerConnectionException
        return result

    def __get_token_userpass(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses username/password.

        :returns: True if the token was successfully received. False otherwise.
        """

        headers = {'X-Rucio-Username': self.creds['username'],
                   'X-Rucio-Password': self.creds['password']}

        url = build_url(self.auth_host, path='auth/userpass')

        result = self._send_request(url, headers=headers, get_token=True)

        if not result:
            # result is either None or not OK.
            if isinstance(result, Response):
                if 'ExceptionClass' in result.headers and result.headers['ExceptionClass']:
                    if 'ExceptionMessage' in result.headers and result.headers['ExceptionMessage']:
                        raise CannotAuthenticate('%s: %s' % (result.headers['ExceptionClass'], result.headers['ExceptionMessage']))
                    else:
                        raise CannotAuthenticate(result.headers["ExceptionClass"])
                elif result.text:
                    raise CannotAuthenticate(result.text)
            LOG.error('Cannot retrieve authentication token!')
            return False

        if result.status_code != codes.ok:  # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        return True

    def __refresh_token_OIDC(self):
        """
        Checks if there is active refresh token and if so returns
        either active token with expiration timestamp or requests a new
        refresh and returns new access token with new expiration timestamp
        and saves these in the token directory.

        :returns: True if the token was successfully received. False otherwise.
        """

        if not self.auth_oidc_refresh_active:
            return False
        if path.exists(self.token_exp_epoch_file):
            with open(self.token_exp_epoch_file, 'r') as token_epoch_file:
                try:
                    self.token_exp_epoch = int(token_epoch_file.readline())
                except:
                    self.token_exp_epoch = None

        if self.token_exp_epoch is None:
            # check expiration time for a new token
            pass
        elif time.time() > self.token_exp_epoch - self.auth_oidc_refresh_before_exp * 60 and time.time() < self.token_exp_epoch:
            # attempt to refresh token
            pass
        else:
            return False

        request_refresh_url = build_url(self.auth_host, path='auth/oidc_refresh')
        refresh_result = self._send_request(request_refresh_url, get_token=True)
        if refresh_result.status_code == codes.ok:
            if 'X-Rucio-Auth-Token-Expires' not in refresh_result.headers or \
                    'X-Rucio-Auth-Token' not in refresh_result.headers:
                print("Rucio Server response does not contain the expected headers.")
                return False
            else:
                new_token = refresh_result.headers['X-Rucio-Auth-Token']
                new_exp_epoch = refresh_result.headers['X-Rucio-Auth-Token-Expires']
                if new_token and new_exp_epoch:
                    LOG.debug("Saving token %s and expiration epoch %s to files" % (str(new_token), str(new_exp_epoch)))
                    # save to the file
                    self.auth_token = new_token
                    self.token_exp_epoch = new_exp_epoch
                    self.__write_token()
                    self.headers['X-Rucio-Auth-Token'] = self.auth_token
                    return True
                LOG.debug("No new token was received, possibly invalid/expired \
                           \ntoken or a token with no refresh token in Rucio DB")
                return False
        else:
            print("Rucio Client did not succeed to contact the \
                   \nRucio Auth Server when attempting token refresh.")
            return False

    def __get_token_OIDC(self):
        """
        First authenticates the user via a Identity Provider server
        (with user's username & password), by specifying oidc_scope,
        user agrees to share the relevant information with Rucio.
        If all proceeds well, an access token is requested from the Identity Provider.
        Access Tokens are not stored in Rucio DB.
        Refresh Tokens are granted only in case no valid access token exists in user's
        local storage, oidc_scope includes 'offline_access'. In such case, refresh token
        is stored in Rucio DB.

        :returns: True if the token was successfully received. False otherwise.
        """
        oidc_scope = str(self.creds['oidc_scope'])
        headers = {'X-Rucio-Client-Authorize-Auto': str(self.creds['oidc_auto']),
                   'X-Rucio-Client-Authorize-Polling': str(self.creds['oidc_polling']),
                   'X-Rucio-Client-Authorize-Scope': str(self.creds['oidc_scope']),
                   'X-Rucio-Client-Authorize-Refresh-Lifetime': str(self.creds['oidc_refresh_lifetime'])}
        if self.creds['oidc_audience']:
            headers['X-Rucio-Client-Authorize-Audience'] = str(self.creds['oidc_audience'])
        if self.creds['oidc_issuer']:
            headers['X-Rucio-Client-Authorize-Issuer'] = str(self.creds['oidc_issuer'])
        if self.creds['oidc_auto']:
            userpass = {'username': self.creds['oidc_username'], 'password': self.creds['oidc_password']}

        result = None
        request_auth_url = build_url(self.auth_host, path='auth/oidc')
        # requesting authorization URL specific to the user & Rucio OIDC Client
        LOG.debug("Initial auth URL request headers %s to files" % str(headers))
        OIDC_auth_res = self._send_request(request_auth_url, headers=headers, get_token=True)
        LOG.debug("Response headers %s and text %s" % (str(OIDC_auth_res.headers), str(OIDC_auth_res.text)))
        # with the obtained authorization URL we will contact the Identity Provider to get to the login page
        if 'X-Rucio-OIDC-Auth-URL' not in OIDC_auth_res.headers:
            print("Rucio Client did not succeed to get AuthN/Z URL from the Rucio Auth Server. \
                                   \nThis could be due to wrongly requested/configured scope, audience or issuer.")
            return False
        auth_url = OIDC_auth_res.headers['X-Rucio-OIDC-Auth-URL']
        if not self.creds['oidc_auto']:
            print("\nPlease use your internet browser, go to:")
            print("\n    " + auth_url + "    \n")
            print("and authenticate with your Identity Provider.")

            headers['X-Rucio-Client-Fetch-Token'] = 'True'
            if self.creds['oidc_polling']:
                timeout = 180
                start = time.time()
                print("In the next 3 minutes, Rucio Client will be polling \
                                           \nthe Rucio authentication server for a token.")
                print("----------------------------------------------")
                while time.time() - start < timeout:
                    result = self._send_request(auth_url, headers=headers, get_token=True)
                    if 'X-Rucio-Auth-Token' in result.headers and result.status_code == codes.ok:
                        break
                    time.sleep(2)
            else:
                print("Copy paste the code from the browser to the terminal and press enter:")
                count = 0
                while count < 3:
                    # Python3 default
                    get_input = input
                    # if Python version <= 2.7 use raw_input
                    if sys.version_info[:2] <= (2, 7):
                        get_input = raw_input  # noqa: F821 pylint: disable=undefined-variable
                    fetchcode = get_input()
                    fetch_url = build_url(self.auth_host, path='auth/oidc_redirect', params=fetchcode)
                    result = self._send_request(fetch_url, headers=headers, get_token=True)
                    if 'X-Rucio-Auth-Token' in result.headers and result.status_code == codes.ok:
                        break
                    else:
                        print("The Rucio Auth Server did not respond as expected. Please, "
                              + "try again and make sure you typed the correct code.")  # NOQA: W503
                        count += 1

        else:
            print("\nAccording to the OAuth2/OIDC standard you should NOT be sharing \n"
                  + "your password with any 3rd party appplication, therefore, \n"  # NOQA: W503
                  + "we strongly discourage you from following this --oidc-auto approach.")  # NOQA: W503
            print("-------------------------------------------------------------------------")
            auth_res = self._send_request(auth_url, get_token=True)
            # getting the login URL and logging in the user
            login_url = auth_res.url
            start = time.time()
            result = self._send_request(login_url, type_='POST', data=userpass)

            # if the Rucio OIDC Client configuration does not match the one registered at the Identity Provider
            # the user will get an OAuth error
            if 'OAuth Error' in result.text:
                LOG.error('Identity Provider does not allow to proceed. Could be due \
                           \nto misconfigured redirection server name of the Rucio OIDC Client.')
                return False
            # In case Rucio Client is not authorized to request information about this user yet,
            # it will automatically authorize itself on behalf of the user.
            if result.url == auth_url:
                form_data = {}
                for scope_item in oidc_scope.split():
                    form_data["scope_" + scope_item] = scope_item
                default_data = {"remember": "until-revoked",
                                "user_oauth_approval": True,
                                "authorize": "Authorize"}
                form_data.update(default_data)
                print('Automatically authorising request of the following info on behalf of user: %s', str(form_data))
                LOG.warning('Automatically authorising request of the following info on behalf of user: %s',
                            str(form_data))
                # authorizing info request on behalf of the user until he/she revokes this authorization !
                result = self._send_request(result.url, type_='POST', data=form_data)

        if not result or 'result' not in locals():
            LOG.error('Cannot retrieve authentication token!')
            return False

        if result.status_code != codes.ok:  # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        if self.auth_oidc_refresh_active:
            LOG.debug("Reseting the token expiration epoch file content.")
            # reset the token expiration epoch file content
            # at new CLI OIDC authentication
            self.token_exp_epoch = None
            file_d, file_n = mkstemp(dir=self.token_path)
            with fdopen(file_d, "w") as f_exp_epoch:
                f_exp_epoch.write(str(self.token_exp_epoch))
            move(file_n, self.token_exp_epoch_file)
            self.__refresh_token_OIDC()
        return True

    def __get_token_x509(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses x509 authentication.

        :returns: True if the token was successfully received. False otherwise.
        """
        client_cert = None
        client_key = None
        if self.auth_type == 'x509':
            url = build_url(self.auth_host, path='auth/x509')
            client_cert = self.creds['client_cert']
            if 'client_key' in self.creds:
                client_key = self.creds['client_key']
        elif self.auth_type == 'x509_proxy':
            url = build_url(self.auth_host, path='auth/x509_proxy')
            client_cert = self.creds['client_proxy']

        if not path.exists(client_cert):
            LOG.error('given client cert (%s) doesn\'t exist' % client_cert)
            return False
        if client_key is not None and not path.exists(client_key):
            LOG.error('given client key (%s) doesn\'t exist' % client_key)

        if client_key is None:
            cert = client_cert
        else:
            cert = (client_cert, client_key)

        result = self._send_request(url, get_token=True, cert=cert)

        # Note a response object for a failed request evaluates to false, so we cannot
        # use "not result" here
        if result is None:
            LOG.error('Internal error: Request for authentication token returned no result!')
            return False

        if result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        return True

    def __get_token_ssh(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses SSH key exchange authentication.

        :returns: True if the token was successfully received. False otherwise.
        """
        headers = {}

        private_key_path = self.creds['ssh_private_key']
        if not path.exists(private_key_path):
            LOG.error('given private key (%s) doesn\'t exist' % private_key_path)
            return False
        if private_key_path is not None and not path.exists(private_key_path):
            LOG.error('given private key (%s) doesn\'t exist' % private_key_path)
            return False

        url = build_url(self.auth_host, path='auth/ssh_challenge_token')

        result = self._send_request(url, get_token=True)

        if not result:
            LOG.error('cannot get ssh_challenge_token')
            return False

        if result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.ssh_challenge_token = result.headers['x-rucio-ssh-challenge-token']
        LOG.debug('got new ssh challenge token \'%s\'' % self.ssh_challenge_token)

        # sign the challenge token with the private key
        with open(private_key_path, 'r') as fd_private_key_path:
            private_key = fd_private_key_path.read()
            signature = ssh_sign(private_key, self.ssh_challenge_token)
            headers['X-Rucio-SSH-Signature'] = signature

        url = build_url(self.auth_host, path='auth/ssh')

        result = self._send_request(url, headers=headers, get_token=True)

        if not result:
            LOG.error('Cannot retrieve authentication token!')
            return False

        if result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        return True

    def __get_token_gss(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses Kerberos authentication.

        :returns: True if the token was successfully received. False otherwise.
        """
        if not EXTRA_MODULES['requests_kerberos']:
            raise MissingModuleException('The requests-kerberos module is not installed.')

        url = build_url(self.auth_host, path='auth/gss')

        result = self._send_request(url, get_token=True, auth=HTTPKerberosAuth())

        if not result:
            LOG.error('Cannot retrieve authentication token!')
            return False

        if result.status_code != codes.ok:   # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['x-rucio-auth-token']
        return True

    def __get_token_saml(self):
        """
        Sends a request to get an auth token from the server and stores it as a class attribute. Uses saml authentication.

        :returns: True if the token was successfully received. False otherwise.
        """
        userpass = {'username': self.creds['username'], 'password': self.creds['password']}
        url = build_url(self.auth_host, path='auth/saml')

        result = None
        SAML_auth_result = self._send_request(url, get_token=True)
        if SAML_auth_result.headers['X-Rucio-Auth-Token']:
            return SAML_auth_result.headers['X-Rucio-Auth-Token']
        SAML_auth_url = SAML_auth_result.headers['X-Rucio-SAML-Auth-URL']
        result = self._send_request(SAML_auth_url, type_='POST', data=userpass, verify=False)
        result = self._send_request(url, get_token=True)

        if not result or 'result' not in locals():
            LOG.error('Cannot retrieve authentication token!')
            return False

        if result.status_code != codes.ok:  # pylint: disable-msg=E1101
            exc_cls, exc_msg = self._get_exception(headers=result.headers,
                                                   status_code=result.status_code,
                                                   data=result.content)
            raise exc_cls(exc_msg)

        self.auth_token = result.headers['X-Rucio-Auth-Token']
        return True

    def __get_token(self):
        """
        Calls the corresponding method to receive an auth token depending on the auth type. To be used if a 401 - Unauthorized error is received.
        """

        LOG.debug('get a new token')
        for retry in range(self.AUTH_RETRIES + 1):
            if self.auth_type == 'userpass':
                if not self.__get_token_userpass():
                    raise CannotAuthenticate('userpass authentication failed for account=%s with identity=%s' % (self.account,
                                                                                                                 self.creds['username']))
            elif self.auth_type == 'x509' or self.auth_type == 'x509_proxy':
                if not self.__get_token_x509():
                    raise CannotAuthenticate('x509 authentication failed for account=%s with identity=%s' % (self.account,
                                                                                                             self.creds))
            elif self.auth_type == 'oidc':
                if not self.__get_token_OIDC():
                    raise CannotAuthenticate('OIDC authentication failed for account=%s' % self.account)

            elif self.auth_type == 'gss':
                if not self.__get_token_gss():
                    raise CannotAuthenticate('kerberos authentication failed for account=%s with identity=%s' % (self.account,
                                                                                                                 self.creds))
            elif self.auth_type == 'ssh':
                if not self.__get_token_ssh():
                    raise CannotAuthenticate('ssh authentication failed for account=%s with identity=%s' % (self.account,
                                                                                                            self.creds))
            elif self.auth_type == 'saml':
                if not self.__get_token_saml():
                    raise CannotAuthenticate('saml authentication failed for account=%s with identity=%s' % (self.account,
                                                                                                             self.creds))
            else:
                raise CannotAuthenticate('auth type \'%s\' not supported' % self.auth_type)

            if self.auth_token is not None:
                self.__write_token()
                self.headers['X-Rucio-Auth-Token'] = self.auth_token
                break

        if self.auth_token is None:
            raise CannotAuthenticate('cannot get an auth token from server')

    def __read_token(self):
        """
        Checks if a local token file exists and reads the token from it.

        :return: True if a token could be read. False if no file exists.
        """
        if not path.exists(self.token_file):
            return False

        try:
            token_file_handler = open(self.token_file, 'r')
            self.auth_token = token_file_handler.readline()
            self.headers['X-Rucio-Auth-Token'] = self.auth_token
        except IOError as error:
            print("I/O error({0}): {1}".format(error.errno, error.strerror))
        except Exception:
            raise
        if self.auth_oidc_refresh_active and self.auth_type == 'oidc':
            self.__refresh_token_OIDC()
        LOG.debug('got token from file')
        return True

    def __write_token(self):
        """
        Write the current auth_token to the local token file.
        """
        # check if rucio temp directory is there. If not create it with permissions only for the current user
        if not path.isdir(self.token_path):
            try:
                LOG.debug('rucio token folder \'%s\' not found. Create it.' % self.token_path)
                makedirs(self.token_path, 0o700)
            except Exception:
                raise

        # if the file exists check if the stored token is valid. If not request a new one and overwrite the file. Otherwise use the one from the file
        try:
            file_d, file_n = mkstemp(dir=self.token_path)
            with fdopen(file_d, "w") as f_token:
                f_token.write(self.auth_token)
            move(file_n, self.token_file)
            if self.auth_type == 'oidc' and self.token_exp_epoch and self.auth_oidc_refresh_active:
                file_d, file_n = mkstemp(dir=self.token_path)
                with fdopen(file_d, "w") as f_exp_epoch:
                    f_exp_epoch.write(str(self.token_exp_epoch))
                move(file_n, self.token_exp_epoch_file)
        except IOError as error:
            print("I/O error({0}): {1}".format(error.errno, error.strerror))
        except Exception:
            raise

    def __authenticate(self):
        """
        Main method for authentication. It first tries to read a locally saved token. If not available it requests a new one.
        """
        if self.auth_type == 'userpass':
            if self.creds['username'] is None or self.creds['password'] is None:
                raise NoAuthInformation('No username or password passed')
        elif self.auth_type == 'oidc':
            if self.creds['oidc_auto'] and (self.creds['oidc_username'] is None or self.creds['oidc_password'] is None):
                raise NoAuthInformation('For automatic OIDC log-in with your Identity Provider username and password are required.')
        elif self.auth_type == 'x509':
            if self.creds['client_cert'] is None:
                raise NoAuthInformation('The path to the client certificate is required')
        elif self.auth_type == 'x509_proxy':
            if self.creds['client_proxy'] is None:
                raise NoAuthInformation('The client proxy has to be defined')
        elif self.auth_type == 'ssh':
            if self.creds['ssh_private_key'] is None:
                raise NoAuthInformation('The SSH private key has to be defined')
        elif self.auth_type == 'gss':
            pass
        elif self.auth_type == 'saml':
            if self.creds['username'] is None or self.creds['password'] is None:
                raise NoAuthInformation('No SAML username or password passed')
        else:
            raise CannotAuthenticate('auth type \'%s\' not supported' % self.auth_type)

        if not self.__read_token():
            self.__get_token()
