#!/usr/bin/env python3
"""Use curl inside the isolated guest to verify OPS splash and SSO initiation."""

from runtime_config import parse_config

import base64
import json
import os
import re
import socket
import subprocess
import urllib.parse


def main():
    """Validate TLS and the generated Microsoft URL without following it."""
    args, cfg = parse_config()
    cfg.require_guest()
    os.umask(0o077)
    root = cfg.workspace_file('web-probe')
    root.mkdir(mode=0o700, exist_ok=True)
    host = cfg.app['host']
    for name in [cfg.app['splash_host'], host]:
        if socket.gethostbyname(name) != '127.0.0.1':
            raise SystemExit('Expected loopback hostname mapping')
    k = [*cfg.guest_kube,
         '--request-timeout=20s', '-n', cfg.namespace]
    secret = json.loads(subprocess.check_output(k + ['get', 'secret',
        cfg.app['tls_secret'], '-o', 'json']))
    cert = root / 'recovered-public-chain.pem'
    cert.write_bytes(base64.b64decode(secret['data']['tls.crt']))
    cert.chmod(0o600)

    def curl(label, url, post=False):
        """Keep raw cookies/state private and return only parsed response data."""
        headers = root / (label + '.headers')
        body = root / (label + '.body')
        command = ['curl', '--noproxy', '*', '--max-time', '45', '--cacert', str(cert),
                   '-sS', '-D', str(headers), '-o', str(body), '-w', '%{http_code}', url]
        if post:
            command += ['-H', 'Content-Type: application/json', '--data', '{}']
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=50)
        for path in (headers, body):
            path.chmod(0o600)
        if result.stdout != '200':
            raise RuntimeError(label + ' returned HTTP ' + result.stdout)
        return body.read_text()

    splash = curl('ops-splash', 'http://' + cfg.app['splash_host'] + '/')
    title = re.search(r'<title>(.*?)</title>', splash, re.S).group(1).strip()
    if 'OpenServerless' not in title:
        raise RuntimeError('Unexpected OPS splash')
    html = curl('application-home', 'https://' + host + '/')
    if 'id="root"' not in html:
        raise RuntimeError('Application HTML missing')
    response = json.loads(curl('application-sso-login', 'https://' + host + cfg.app['login_path'], post=True))
    for unused in range(4):
        if isinstance(response, str):
            response = json.loads(response)
        elif isinstance(response, dict) and 'body' in response:
            response = response['body']
        else:
            break
    url = urllib.parse.urlsplit(response.get('authorizeUrl', ''))
    query = dict(urllib.parse.parse_qsl(url.query))
    if (response.get('authorizationRequired') is not True
            or url.scheme != 'https' or url.hostname != cfg.app['authorization_host']
            or not url.path.endswith('/oauth2/v2.0/authorize')
            or query.get('redirect_uri') != 'https://' + host
            or query.get('response_type') != 'code' or not query.get('state')
            or not query.get('code_challenge') or query.get('code_challenge_method') != 'S256'):
        raise RuntimeError('Microsoft SSO initiation contract not satisfied')
    result = {'opsHttpStatus': 200, 'opsSplashTitle': title, 'applicationHttpsStatus': 200,
              'tlsVerifiedWithRecoveredCa': True, 'ssoLoginHttpStatus': 200,
              'authorizationRequired': True, 'authorizationHost': url.hostname,
              'redirectUri': query['redirect_uri'], 'pkceS256': True, 'statePresent': True,
              'externalAuthorizationUrlFollowed': False, 'externalSsoCompleted': False,
              'guestLoopbackOnly': True, 'result': 'passed'}
    output = root / 'web-sso-acceptance.json'
    output.write_text(json.dumps(result, indent=2) + '\n'); output.chmod(0o600)
    print('Guest TLS and SSO initiation verified; authorization URL not followed.')


if __name__ == '__main__':
    main()
