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

"""Human-only preparation feedback; never forward commands or private output."""
import re
import threading
import time


def _print(message):
    print(message, flush=True)


class PreparationFeedback:
    def __init__(self, *, enabled=True, interval=20, emit=_print):
        if interval <= 0:
            raise ValueError('positive_feedback_interval_required')
        self.enabled = enabled
        self.interval = interval
        self.emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._phase = None

    def update(self, phase, message):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', phase):
            raise ValueError('invalid_feedback_phase')
        with self._lock:
            now = time.monotonic()
            if self._phase != phase:
                self._started = now
            self._phase = phase
            self._last = now
            if self.enabled:
                self.emit('OPS migrate: ' + phase + ' — ' + message)

    def _waiting(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                now = time.monotonic()
                if self._phase and now - self._last >= self.interval:
                    self.emit('OPS migrate: ' + self._phase +
                              ' — waiting for completion (' +
                              str(int(now - self._started)) + 's elapsed in this phase).')
                    self._last = now

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._waiting, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *error):
        self._stop.set()
        if self._thread:
            self._thread.join()
