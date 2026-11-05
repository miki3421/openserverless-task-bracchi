# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Real quiet work emits bounded human feedback and leaves errors unchanged."""
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedback import PreparationFeedback


class PreparationFeedbackTests(unittest.TestCase):
    def test_quiet_subprocess_has_waiting_feedback_without_private_output(self):
        rows = []; pulse = threading.Event()
        def emit(message):
            rows.append(message)
            if 'waiting for completion' in message: pulse.set()
        with PreparationFeedback(interval=.01, emit=emit) as feedback:
            feedback.update('prepare-image-verification', 'Verify archives before unpacking.')
            result = subprocess.run([sys.executable, '-c',
                "import time;time.sleep(.06);print('private-canary')"], capture_output=True, check=True)
            self.assertTrue(pulse.wait(1))
        self.assertIn(b'private-canary', result.stdout)
        self.assertTrue(any('elapsed in this phase' in row for row in rows))
        self.assertNotIn('private-canary', '\n'.join(rows))
        count = len(rows); time.sleep(.03); self.assertEqual(len(rows), count)

    def test_failure_propagates_and_stops_further_feedback(self):
        rows = []; pulse = threading.Event(); problem = ValueError('synthetic failure')
        def emit(message):
            rows.append(message)
            if 'waiting for completion' in message: pulse.set()
        with self.assertRaises(ValueError) as observed:
            with PreparationFeedback(interval=.01, emit=emit) as feedback:
                feedback.update('prepare-image-export', 'Export images.')
                self.assertTrue(pulse.wait(1)); raise problem
        self.assertIs(observed.exception, problem)
        count = len(rows); time.sleep(.03); self.assertEqual(len(rows), count)

    def test_json_mode_has_no_human_output(self):
        rows = []
        with PreparationFeedback(enabled=False, interval=.01, emit=rows.append) as feedback:
            feedback.update('prepare-input-checksums', 'Bind verified files.')
            time.sleep(.03)
        self.assertEqual(rows, [])

    def test_new_stage_never_gets_an_old_stage_waiting_line(self):
        rows = []; first = threading.Event(); second = threading.Event()
        def emit(message):
            rows.append(message)
            if 'waiting for completion' in message:
                (second if 'prepare-image-cleanup' in message else first).set()
        with PreparationFeedback(interval=.01, emit=emit) as feedback:
            feedback.update('prepare-image-verification', 'Verify archives.')
            self.assertTrue(first.wait(1))
            feedback.update('prepare-image-cleanup', 'Release verified staging.')
            start = next(i for i,row in enumerate(rows) if 'prepare-image-cleanup' in row)
            self.assertTrue(second.wait(1))
        self.assertTrue(all('prepare-image-cleanup' in row for row in rows[start:]))

    def test_invalid_stage_cannot_be_printed_as_feedback(self):
        rows = []
        with self.assertRaisesRegex(ValueError, 'invalid_feedback_phase'):
            PreparationFeedback(emit=rows.append).update('secret=/private/path', 'invalid')
        self.assertEqual(rows, [])
