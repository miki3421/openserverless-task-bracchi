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

"""Real local Git assembly with the same exclusive logs used by the migrator."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from assets import assemble_target_tasks


class TaskBundleLogs(unittest.TestCase):
    def exercise(self,missing_commit=False):
        with tempfile.TemporaryDirectory(prefix='task bundle ')as temp:
            root=Path(temp);repo=root/'source';repo.mkdir()
            def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
            git('init','--quiet');(repo/'fixture.txt').write_text('synthetic target source\n')
            git('add','fixture.txt');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--quiet','-m','Synthetic target')
            commit=git('rev-parse','HEAD').decode().strip()
            assets=root/'assets';assets.mkdir();(assets/'downloads').mkdir();(assets/'tasks').mkdir()
            logs=root/'logs';logs.mkdir()
            original={name:data for name,data in [('task-revision.stdout.private',b'original source commit\n'),('task-revision.stderr.private',b'original source diagnostic\n')]}
            for name,data in original.items():(logs/name).write_bytes(data)
            def command(label,args,**kwargs):
                with(logs/(label+'.stdout.private')).open('xb')as out,(logs/(label+'.stderr.private')).open('xb')as err:
                    subprocess.run(args,stdout=out,stderr=err,timeout=30,check=True)
                return(logs/(label+'.stdout.private')).read_bytes()
            release={'taskRepository':str(repo),'taskBranch':'feature/pinned-target',
                     'target':{'taskCommit':'0'*40 if missing_commit else commit}}
            if missing_commit:
                with self.assertRaises(subprocess.CalledProcessError):assemble_target_tasks(release,assets,command)
                self.assertFalse((assets/'tasks/target.bundle').exists())
            else:
                bundle=assemble_target_tasks(release,assets,command)
                self.assertEqual(git('bundle','list-heads',str(bundle)).decode().strip(),commit+' refs/heads/feature/pinned-target')
                self.assertEqual(bundle.stat().st_mode&0o777,0o600)
            for name,data in original.items():self.assertEqual((logs/name).read_bytes(),data)

    def test_source_revision_logs_are_preserved_while_pinned_target_bundle_is_created(self):self.exercise()
    def test_unavailable_pin_stops_before_bundle_without_overwriting_source_logs(self):self.exercise(missing_commit=True)


if __name__=='__main__':unittest.main()
