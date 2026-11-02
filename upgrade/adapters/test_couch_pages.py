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

"""Exercise the real mapping pager without starting recovery workers."""
import ast,hashlib,json,unittest,urllib.parse
from pathlib import Path

source=Path(__file__).with_name('prepare_target_databases.py').read_text()
tree=ast.parse(source)
def pager(documents,max_rows=2,duplicate=False,changed=False,late=False):
    selected=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))and n.name in ['CouchResponseBudgetExceeded','logical_database']]
    scope={'hashlib':hashlib,'json':json,'urllib':__import__('urllib')};exec(compile(ast.Module(body=selected,type_ignores=[]),'actual-pager','exec'),scope)
    calls=[];metadata=0
    def request(method,path):
        nonlocal metadata
        query=urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        if not query:
            metadata+=1
            return 200,{'doc_count':len(documents),'update_seq':'changed'if changed and metadata>1 else'stable'}
        limit=int(query['limit'][0]);last=json.loads(query['startkey'][0])if 'startkey'in query else None
        calls.append((limit,last))
        if limit>(200 if late and last is None else max_rows):raise scope['CouchResponseBudgetExceeded']('synthetic response budget')
        rows=[{'id':d['_id'],'doc':d}for d in documents if last is None or d['_id']>last][:limit]
        if duplicate and last and rows:rows[0]['id']=last
        return 200,{'total_rows':len(documents),'rows':rows}
    scope['request']=request
    return scope['logical_database'],calls

class CouchPages(unittest.TestCase):
    def test_large_attachment_pages_shrink_without_skipping_or_repeating(self):
        docs=[{'_id':f'action-{i:03}','_revisions':{'ids':['revision']},'_attachments':{'action':{'data':'synthetic'}}}for i in range(7)]
        read,calls=pager(docs);consumed=[];result=read('source',lambda rows:consumed.extend(rows))
        self.assertEqual(consumed,docs);self.assertEqual(result['documents'],7)
        expected=hashlib.sha256()
        for d in docs:expected.update(json.dumps({k:v for k,v in d.items()if k!='_revisions'},sort_keys=True,separators=(',',':')).encode()+b'\n')
        self.assertEqual(result['sha256'],expected.hexdigest());self.assertGreater(len(calls),len(docs)//2)
        self.assertTrue(all(c[1]is None for c in calls if c[0]>2))
    def test_later_oversized_page_retries_the_last_consumed_cursor(self):
        docs=[{'_id':f'action-{i:03}'}for i in range(205)]
        read,calls=pager(docs,late=True);consumed=[];result=read('source',lambda rows:consumed.extend(rows))
        self.assertEqual(consumed,docs);self.assertEqual(result['documents'],205)
        self.assertTrue(any(cursor=='action-199'and limit>2 for limit,cursor in calls))
    def test_single_oversized_document_still_fails_closed(self):
        read,calls=pager([{'_id':'large'}],max_rows=0)
        with self.assertRaises(ValueError):read('source')
        self.assertEqual(calls[-1][0],1)
    def test_duplicate_page_or_source_change_is_rejected(self):
        docs=[{'_id':str(i)}for i in range(3)]
        for options in [{'duplicate':True},{'changed':True}]:
            read,_=pager(docs,**options)
            with self.assertRaises(ValueError):read('source')

if __name__=='__main__':unittest.main()
