#!/usr/bin/env python
# -- coding: utf-8 --
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import subprocess
import uuid

from tempfile import NamedTemporaryFile

from django.utils.translation import ugettext as _

from desktop.lib.exceptions_renderable import PopupException
from desktop.lib import export_csvxls
from desktop.lib.rest.http_client import HttpClient, RestException
from desktop.lib.rest import resource

from metadata.conf import OPTIMIZER, get_optimizer_url
from subprocess import CalledProcessError


LOG = logging.getLogger(__name__)


_JSON_CONTENT_TYPE = 'application/json'


def is_optimizer_enabled():
  return get_optimizer_url() and OPTIMIZER.PRODUCT_NAME.get()


class OptimizerApiException(PopupException):
  pass


class OptimizerApi(object):

  UPLOAD = {
    'queries': {
      'headers': ['SQL_ID', 'ELAPSED_TIME', 'SQL_FULLTEXT'],
      'file_headers': """{
    "fileLocation": "%(query_file)s",
    "tenant": "%(tenant)s",
    "fileName": "%(query_file_name)s",
    "sourcePlatform": "%(source_platform)s",
    "colDelim": ",",
    "rowDelim": "\\n",
    "headerFields": [
        {
            "count": 0,
            "coltype": "SQL_ID",
            "use": true,
            "tag": "",
            "name": "SQL_ID"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "ELAPSED_TIME"
        },
        {
            "count": 0,
            "coltype": "SQL_QUERY",
            "use": true,
            "tag": "",
            "name": "SQL_FULLTEXT"
        }
    ]
}"""
    },
    'table_stats': {
        'headers': ['TABLE_NAME', 'NUM_ROWS'],
        'file_headers': """{
    "fileLocation": "%(query_file)s",
    "tenant": "%(tenant)s",
    "fileName": "%(query_file_name)s",
    "sourcePlatform": "%(source_platform)s",
    "colDelim": ",",
    "rowDelim": "\\n",
    "headerFields": [
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "TABLE_NAME"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "NUM_ROWS"
        }
    ]
}"""
    },
    'cols_stats': {
        'headers': ['table_name', 'column_name', 'data_type', 'num_distinct', 'num_nulls', 'avg_col_len'], # Lower case for some reason
        'file_headers': """{
    "fileLocation": "%(query_file)s",
    "tenant": "%(tenant)s",
    "fileName": "%(query_file_name)s",
    "sourcePlatform": "%(source_platform)s",
    "colDelim": ",",
    "rowDelim": "\\n",
    "headerFields": [
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "table_name"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "column_name"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "data_type"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "num_distinct"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "num_nulls"
        },
        {
            "count": 0,
            "coltype": "NONE",
            "use": true,
            "tag": "",
            "name": "avg_col_len"
        }
    ]
}"""
    }
  }

  def __init__(self, api_url=None, product_name=None, product_secret=None, ssl_cert_ca_verify=OPTIMIZER.SSL_CERT_CA_VERIFY.get(), product_auth_secret=None):
    self._api_url = (api_url or get_optimizer_url()).strip('/')
    self._email = OPTIMIZER.EMAIL.get()
    self._email_password = OPTIMIZER.EMAIL_PASSWORD.get()
    self._product_secret = product_secret if product_secret else OPTIMIZER.PRODUCT_SECRET.get()
    self._product_auth_secret = product_auth_secret if product_auth_secret else OPTIMIZER.PRODUCT_AUTH_SECRET.get()
    self._product_name = product_name if product_name else (OPTIMIZER.PRODUCT_NAME.get() or self.get_tenant()['tenant']) # Aka "workload"

    self._client = HttpClient(self._api_url, logger=LOG)
    self._client.set_verify(ssl_cert_ca_verify)

    self._root = resource.Resource(self._client)
    self._token = None


  def _authenticate(self, force=False):
    if self._token is None or force:
      self._token = self.authenticate()['token']

    return self._token


  def _exec(self, command, args):
    data = None
    response = {'status': 'error'}

    try:
      cmd_args = [
          'ccs',
          'navopt',
          '--endpoint-url=%s' % self._api_url,
          command
      ]
      if self._product_secret:
        cmd_args += ['--auth-config', self._product_secret]

      LOG.info(' '.join(cmd_args + args))
      data = subprocess.check_output(cmd_args + args)
    except CalledProcessError, e:
      if command == 'upload' and e.returncode == 1:
        LOG.info('Upload command is successful despite return code of 1: %s' % e.output)
        data = '\n'.join(e.output.split('\n')[3:]) # Beware removing of {"url":...}
      else:
        raise OptimizerApiException(e, title=_('Error while accessing Optimizer'))
    except RestException, e:
      raise OptimizerApiException(e, title=_('Error while accessing Optimizer'))

    if data:
      response = json.loads(data)
      if 'status' not in response:
        response['status'] = 'success'
    return response


  def get_tenant(self, email=None):
    return self._exec('get-tenant', ['--email', email or self._email])


  def create_tenant(self, group):
    return self._exec('create-tenant', ['--user-group', group])


  def authenticate(self):
    try:
      data = {
          'productName': self._product_name,
          'productSecret': self._product_secret,
      }
      return self._root.post('/api/authenticate', data=json.dumps(data), contenttype=_JSON_CONTENT_TYPE)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Optimizer'))


  def delete_workload(self, token, email=None):
    try:
      data = {
          'email': email if email is not None else self._email,
          'token': token,
      }
      return self._root.post('/api/deleteWorkload', data=json.dumps(data), contenttype=_JSON_CONTENT_TYPE)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Optimizer'))


  def get_status(self, token, email=None):
    try:
      data = {
          'email': email if email is not None else self._email,
          'token': token,
      }
      return self._root.post('/api/getStatus', data=json.dumps(data), contenttype=_JSON_CONTENT_TYPE)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Optimizer'))


  def upload(self, data, data_type='queries', source_platform='generic', workload_id=None):
    data_headers = OptimizerApi.UPLOAD[data_type]['file_headers']

    if data_type in ('table_stats', 'cols_stats'):
      data_suffix = '.log'
    else:
      data_suffix = '.csv'

    f_queries_path = NamedTemporaryFile(suffix=data_suffix)
    f_format_path = NamedTemporaryFile(suffix='.json')
    f_queries_path.close()
    f_format_path.close() # Reopened as real file below to work well with the command

    try:
      f_queries = open(f_queries_path.name, 'w+')
      f_format = open(f_format_path.name, 'w+')

      try:
        content_generator = OptimizerDataAdapter(data, data_type=data_type)
        queries_csv = export_csvxls.create_generator(content_generator, 'csv')

        for row in queries_csv:
          f_queries.write(row)

        f_format.write(data_headers % {
            'source_platform': source_platform,
            'tenant': self._product_name,
            'query_file': f_queries.name,
            'query_file_name': os.path.basename(f_queries.name)
        })

      finally:
        f_queries.close()
        f_format.close()

      args = [
          '--cli-input-json', 'file://%s' % f_format.name
      ]
      if workload_id:
        args += ['--workload-id', workload_id]

      return self._exec('upload', args)

    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Optimizer'))
    finally:
      os.remove(f_queries_path.name)
      os.remove(f_format_path.name)


  def upload_status(self, workload_id):
    return self._exec('upload-status', [
        '--tenant', self._product_name,
        '--workload-id', workload_id
    ])


  def top_tables(self, workfloadId=None, database_name='default'):
    return self._exec('get-top-tables', [
        '--tenant', self._product_name,
        '--db-name', database_name.lower()
    ])


  def table_details(self, database_name, table_name):
    return self._exec('get-tables-detail', [
        '--tenant', self._product_name,
        '--db-name', database_name.lower(),
        '--table-name', table_name.lower()
    ])


  def query_compatibility(self, source_platform, target_platform, query):
    return self._exec('get-query-compatible', [
        '--tenant', self._product_name,
        '--source-platform', source_platform,
        '--target-platform', target_platform,
        '--query', query,
    ])


  def query_risk(self, query):
    return self._exec('get-query-risk', [
        '--tenant', self._product_name,
        '--query', query
    ])


  def similar_queries(self, source_platform, query):
    return self._exec('get-similar-queries', [
        '--tenant', self._product_name,
        '--source-platform', source_platform,
        '--query', query
    ])


  def top_filters(self, db_tables=None):
    args = [
        '--tenant', self._product_name,
    ]
    if db_tables:
      args += ['--db-table-list']
      args.extend([db_table.lower() for db_table in db_tables])

    return self._exec('get-top-filters', args)


  def top_aggs(self, db_tables=None):
    args = [
        '--tenant', self._product_name
    ]
    if db_tables:
      args += ['--db-table-list']
      args.extend([db_table.lower() for db_table in db_tables])

    return self._exec('get-top-aggs', args)


  def top_columns(self, db_tables=None):
    args = [
        '--tenant', self._product_name
    ]
    if db_tables:
      args += ['--db-table-list']
      args.extend([db_table.lower() for db_table in db_tables])

    return self._exec('get-top-columns', args)


  def top_joins(self, db_tables=None):
    args = [
        '--tenant', self._product_name,
    ]
    if db_tables:
      args += ['--db-table-list']
      args.extend([db_table.lower() for db_table in db_tables])

    return self._exec('get-top-joins', args)


  def top_databases(self, db_tables=None):
    args = [
        '--tenant', self._product_name,
    ]

    return self._exec('get-top-data-bases', args)


def OptimizerDataAdapter(data, data_type='queries'):
  headers = OptimizerApi.UPLOAD[data_type]['headers']

  if data_type in ('table_stats', 'cols_stats'):
    rows = data
  else:
    if data and len(data[0]) == 3:
      rows = data
    else:
      rows = ([str(uuid.uuid4()), 0.0, q] for q in data)

  yield headers, rows

