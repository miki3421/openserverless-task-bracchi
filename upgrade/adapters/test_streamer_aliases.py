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

import copy
import unittest
from streamer_aliases import headless_streamer_aliases


def fixture():
    source = {'kind': 'Service', 'metadata': {'namespace': 'nuvolaris', 'name': 'nuvolaris-streamer-api'},
              'spec': {'selector': {'app': 'nuvolaris-streamer-api'},
                       'ports': [{'port': 8080, 'targetPort': 8080}]}}
    alias = copy.deepcopy(source); alias['metadata']['name'] = 'custom-stream-endpoint'
    alias['spec'].update(clusterIP='None', type='ClusterIP')
    alias['spec']['ports'][0]['targetPort'] = 'streamer'
    target = copy.deepcopy(source); target['metadata'] = {'namespace': 'openserverless', 'name': 'openserverless-streamer-api'}
    target['spec']['selector']['app'] = 'openserverless-streamer-api'
    return [source, alias], target


class HeadlessStreamerAliases(unittest.TestCase):
    def test_captured_headless_identity_selects_new_pods_without_mutating_source(self):
        source, target = fixture(); before = copy.deepcopy((source, target))
        result = headless_streamer_aliases(source, target, set())
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['spec']['clusterIP'], 'None')
        self.assertEqual(result[0]['spec']['selector'], target['spec']['selector'])
        self.assertEqual(result[0]['spec']['ports'], target['spec']['ports'])
        self.assertEqual(result[1]['metadata'], {'namespace': 'nuvolaris', 'name': 'custom-stream-endpoint'})
        self.assertEqual(result[1]['spec']['externalName'], 'custom-stream-endpoint.openserverless.svc.cluster.local')
        self.assertEqual((source, target), before)

    def test_no_alias_or_absent_optional_streamer_does_not_add_services(self):
        source, target = fixture()
        self.assertEqual(headless_streamer_aliases(source[:1], target, set()), [])
        self.assertEqual(headless_streamer_aliases([], target, set()), [])

    def test_foreign_namespace_selector_and_ordinary_service_are_not_imported(self):
        for field in ('namespace', 'selector', 'clusterIP'):
            source, target = fixture()
            if field == 'namespace': source[1]['metadata']['namespace'] = 'foreign'
            elif field == 'selector': source[1]['spec']['selector'] = {'app': 'foreign'}
            else: source[1]['spec']['clusterIP'] = '10.0.0.5'
            with self.subTest(field=field):
                self.assertEqual(headless_streamer_aliases(source, target, set()), [])

    def test_original_and_target_selectors_must_be_exact_platform_services(self):
        source, target = fixture()
        with self.assertRaisesRegex(ValueError, 'Unique captured'):
            headless_streamer_aliases(source[1:], target, set())
        source[0]['spec']['selector'] = {'app': 'foreign'}
        with self.assertRaisesRegex(ValueError, 'captured streamer selector'):
            headless_streamer_aliases(source, target, set())
        source, target = fixture(); target['spec']['selector']['extra'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'destination streamer'):
            headless_streamer_aliases(source, target, set())

    def test_occupied_destination_never_overwrites_existing_services(self):
        for namespace in ('nuvolaris', 'openserverless'):
            source, target = fixture()
            with self.subTest(namespace=namespace), self.assertRaisesRegex(ValueError, 'occupied'):
                headless_streamer_aliases(source, target, {(namespace, 'custom-stream-endpoint')})

    def test_duplicate_alias_and_unsupported_network_exposure_are_rejected(self):
        source, target = fixture(); source.append(copy.deepcopy(source[1]))
        with self.assertRaisesRegex(ValueError, 'occupied'):
            headless_streamer_aliases(source, target, set())
        for change in ({'externalIPs': ['192.0.2.1']}, {'type': 'NodePort'},
                       {'publishNotReadyAddresses': True}, {'ports': [{'port': 9090}]},
                       {'ports': [{'port': 8080, 'targetPort': 9090}]}):
            source, target = fixture(); source[1]['spec'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                headless_streamer_aliases(source, target, set())
