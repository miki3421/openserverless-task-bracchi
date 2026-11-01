# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Recognize authentication denials without treating backend outages as success."""
import re


def authentication_denied(error, *, anonymous=False):
    if error.code in (13, 18):
        return True
    message = str(error)
    return bool(anonymous and error.code == 1 and
                re.search(r'\bSQLSTATE\s+28P01\b', message) and
                'password authentication failed' in message.lower())


def state_identity_preserved(saved, current):
    """Startup may add telemetry state; the native persisted UUID must survive."""
    return bool(isinstance(saved, dict) and isinstance(current, dict) and
                isinstance(saved.get('uuid'), str) and saved['uuid'] and
                current.get('uuid') == saved['uuid'])


def assert_empty_peer_view(database, known_peer_collections):
    """An accepted empty Mongo namespace is not evidence of a peer data read."""
    if database.list_collection_names():
        raise ValueError('Collections visible in the peer namespace')
    for name in known_peer_collections:
        if database[name].find_one({}) is not None:
            raise ValueError('Document readable from a known peer collection')
