# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read-only qualification selects the configured target, preserving source bindings."""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class TargetQualificationImage(unittest.TestCase):
    def lab(self,configured,actual):
        tree=ast.parse(Path(__file__).with_name('verify_090_data.py').read_text())
        original=next(x for x in tree.body if isinstance(x,ast.ClassDef)and x.name=='Lab')
        methods=[x for x in original.body if isinstance(x,ast.FunctionDef)and x.name in('target','ready')]
        klass=ast.ClassDef(name='Lab',bases=[],keywords=[],body=methods,decorator_list=[])
        pod={'spec':{'containers':[{'name':'openserverless-mongodb','image':actual}]}}
        promotion=SimpleNamespace(plan={'finalWhiskSpec':{'images':{'ferretdb':configured}}},wait_pod=Mock(return_value=pod))
        scope={'promotion':promotion,'copy':copy,'json':json}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[klass],type_ignores=[])),'real-target-qualification','exec'),scope)
        lab=scope['Lab']();lab.plan={'bindings':[{'artifactId':'mongodb-data','targets':[{'pod':'nuvolaris-mongodb-0','container':'nuvolaris-mongodb','image':'ghcr.io/nuvolaris/ferretdb:1.6.0'}]}]}
        return lab,promotion,pod

    def test_digest_target_is_compared_exactly_without_rewriting_original_reference(self):
        pin='ghcr.io/nuvolaris/ferretdb@sha256:'+'a'*64
        lab,promotion,pod=self.lab(pin,pin);source=copy.deepcopy(lab.plan)
        target=lab.target('mongodb-data')
        self.assertEqual(target['image'],'ghcr.io/nuvolaris/ferretdb:1.6.0')
        self.assertEqual(target['qualificationImage'],pin)
        self.assertEqual(lab.ready(target),pod);self.assertEqual(lab.plan,source)
        promotion.wait_pod.assert_called_once_with('openserverless-mongodb-0')

    def test_wrong_digest_or_old_tag_is_not_accepted_as_the_pinned_target(self):
        pin='ghcr.io/nuvolaris/ferretdb@sha256:'+'a'*64
        for actual in ['ghcr.io/nuvolaris/ferretdb:1.6.0','ghcr.io/nuvolaris/ferretdb@sha256:'+'b'*64]:
            lab,_,_=self.lab(pin,actual)
            with self.subTest(actual=actual),self.assertRaisesRegex(ValueError,'Unexpected data qualification image'):lab.ready(lab.target('mongodb-data'))

    def test_existing_tag_based_profiles_still_use_their_exact_target(self):
        image='ghcr.io/nuvolaris/ferretdb:1.6.0';lab,_,pod=self.lab(image,image)
        self.assertEqual(lab.ready(lab.target('mongodb-data')),pod)


if __name__=='__main__':unittest.main()
