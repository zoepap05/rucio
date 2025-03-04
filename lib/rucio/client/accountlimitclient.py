# -*- coding: utf-8 -*-
# Copyright 2014-2021 CERN
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
# - Martin Barisits <martin.barisits@cern.ch>, 2014-2020
# - Cedric Serfon <cedric.serfon@cern.ch>, 2014
# - Vincent Garonne <vincent.garonne@cern.ch>, 2014-2018
# - Ralph Vigne <ralph.vigne@cern.ch>, 2015
# - Hannes Hansen <hannes.jakob.hansen@cern.ch>, 2018-2019
# - Brandon White <bjwhite@fnal.gov>, 2020
# - Andrew Lister <andrew.lister@stfc.ac.uk>, 2019
# - Eli Chadwick <eli.chadwick@stfc.ac.uk>, 2020
# - Benedikt Ziemons <benedikt.ziemons@cern.ch>, 2021

from json import dumps

from requests.status_codes import codes
from six.moves.urllib.parse import quote_plus

from rucio.client.baseclient import BaseClient
from rucio.client.baseclient import choice
from rucio.common.utils import build_url


class AccountLimitClient(BaseClient):

    """Account limit client class for working with account limits"""

    ACCOUNTLIMIT_BASEURL = 'accountlimits'

    def __init__(self, rucio_host=None, auth_host=None, account=None, ca_cert=None,
                 auth_type=None, creds=None, timeout=600, user_agent='rucio-clients', vo=None):
        super(AccountLimitClient, self).__init__(rucio_host, auth_host, account, ca_cert,
                                                 auth_type, creds, timeout, user_agent, vo=vo)

    def set_account_limit(self, account, rse, bytes_, locality):
        """
        Sets an account limit for a given limit scope.

        :param account: The name of the account.
        :param rse:     The rse name.
        :param bytes_:   An integer with the limit in bytes.
        :param locality: The scope of the account limit. 'local' or 'global'.
        :return:        True if quota was created successfully else False.
        """

        if locality == 'local':
            return self.set_local_account_limit(account, rse, bytes_)
        elif locality == 'global':
            return self.set_global_account_limit(account, rse, bytes_)
        else:
            from rucio.common.exception import UnsupportedOperation
            raise UnsupportedOperation('The provided scope (%s) for the account limit was invalid' % locality)

    def delete_account_limit(self, account, rse, locality):
        """
        Deletes an account limit for a given limit scope.

        :param account: The name of the account.
        :param rse:     The rse name.
        :param locality: The scope of the account limit. 'local' or 'global'.
        :return:        True if quota was created successfully else False.
        """

        if locality == 'local':
            return self.delete_local_account_limit(account, rse)
        elif locality == 'global':
            return self.delete_global_account_limit(account, rse)
        else:
            from rucio.common.exception import UnsupportedOperation
            raise UnsupportedOperation('The provided scope (%s) for the account limit was invalid' % locality)

    def set_local_account_limit(self, account, rse, bytes_):
        """
        Sends the request to set an account limit for an account.

        :param account: The name of the account.
        :param rse:     The rse name.
        :param bytes_:   An integer with the limit in bytes.
        :return:        True if quota was created successfully else False.
        """

        data = dumps({'bytes': bytes_})
        path = '/'.join([self.ACCOUNTLIMIT_BASEURL, 'local', account, rse])
        url = build_url(choice(self.list_hosts), path=path)

        r = self._send_request(url, type_='POST', data=data)

        if r.status_code == codes.created:
            return True
        else:
            exc_cls, exc_msg = self._get_exception(headers=r.headers, status_code=r.status_code, data=r.content)
            raise exc_cls(exc_msg)

    def delete_local_account_limit(self, account, rse):
        """
        Sends the request to remove an account limit.

        :param account: The name of the account.
        :param rse:     The rse name.

        :return: True if quota was removed successfully. False otherwise.
        :raises AccountNotFound: if account doesn't exist.
        """

        path = '/'.join([self.ACCOUNTLIMIT_BASEURL, 'local', account, rse])
        url = build_url(choice(self.list_hosts), path=path)

        r = self._send_request(url, type_='DEL')

        if r.status_code == codes.ok:
            return True
        else:
            exc_cls, exc_msg = self._get_exception(headers=r.headers, status_code=r.status_code, data=r.content)
            raise exc_cls(exc_msg)

    def set_global_account_limit(self, account, rse_expression, bytes_):
        """
        Sends the request to set a global account limit for an account.

        :param account:        The name of the account.
        :param rse_expression: The rse expression.
        :param bytes_:          An integer with the limit in bytes.
        :return:               True if quota was created successfully else False.
        """

        data = dumps({'bytes': bytes_})
        path = '/'.join([self.ACCOUNTLIMIT_BASEURL, 'global', account, quote_plus(rse_expression)])
        url = build_url(choice(self.list_hosts), path=path)

        r = self._send_request(url, type_='POST', data=data)

        if r.status_code == codes.created:
            return True
        else:
            exc_cls, exc_msg = self._get_exception(headers=r.headers, status_code=r.status_code, data=r.content)
            raise exc_cls(exc_msg)

    def delete_global_account_limit(self, account, rse_expression):
        """
        Sends the request to remove a global account limit.

        :param account:        The name of the account.
        :param rse_expression: The rse expression.

        :return: True if quota was removed successfully. False otherwise.
        :raises AccountNotFound: if account doesn't exist.
        """

        path = '/'.join([self.ACCOUNTLIMIT_BASEURL, 'global', account, quote_plus(rse_expression)])
        url = build_url(choice(self.list_hosts), path=path)

        r = self._send_request(url, type_='DEL')

        if r.status_code == codes.ok:
            return True
        else:
            exc_cls, exc_msg = self._get_exception(headers=r.headers, status_code=r.status_code, data=r.content)
            raise exc_cls(exc_msg)
