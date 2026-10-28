"""A full-duplex stress of one TLS connection, used as a pgdelta session uses its channel: on each end one thread
writes while another reads, each through the channel's own streams, and every byte is checked against its pattern.

    python experiments/tls_duplex_stress.py [--rounds 100] [--mib 64] [--repo CHECKOUT]

`--repo` loads pgdelta.transport from another checkout, so two versions can be compared. Each round is one TLS
connection over loopback with --mib MiB each way, and prints "ok" with its time, or "hang" (no byte moved for 20 s)
or "FAIL" (a wrong byte, or an error). A process that dies prints nothing more: run it with `python -X faulthandler`
to see the native crash.

Through one ssl.SSLSocket (pgdelta before October 28), OpenSSL's rule that one SSL object is never used by two
threads at once was broken: on macOS, Python 3.14.7 and OpenSSL 3.6.4, the process died of SIGBUS in OpenSSL's BIO
code within 30 rounds of 64 MiB, three times in three tries (rounds 29, 0 and 3). With the TLS state under a lock
(transport._TlsConnection), 100 of 100 rounds passed. The certificates are a throwaway CA's, made with the openssl
command.
"""
import argparse
import hashlib
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CHUNK = 1 << 16
LEAF = ('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n'
        'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n')


def certificates(directory):
    def openssl(*args):
        subprocess.run(['openssl', *args], cwd=directory, check=True, capture_output=True)
    (directory / 'leaf.ext').write_text(LEAF)
    openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout',
            'ca.key', '-out', 'ca.crt', '-days', '2', '-subj', '/CN=ca', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign')
    for name in ('receiver', 'sender'):
        openssl('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout',
                f'{name}.key', '-out', f'{name}.csr', '-subj', f'/CN={name}')
        openssl('x509', '-req', '-in', f'{name}.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-out',
                f'{name}.crt', '-days', '2', '-extfile', 'leaf.ext')


def pattern(end, i):
    return hashlib.blake2b(f'{end}:{i}'.encode(), digest_size=64).digest() * (CHUNK // 64)


def pump(channel, end, peer, chunks, progress):
    def write():
        try:
            for i in range(chunks):
                channel._writer.write(pattern(end, i))
                channel._writer.flush()
                progress['sent'] = i + 1
        except BaseException as exc:
            progress['write_error'] = repr(exc)
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        for i in range(chunks):
            if bytes(channel._read_exact(CHUNK)) != pattern(peer, i):
                progress['corrupt'] = i
                return
            progress['received'] = i + 1
    except BaseException as exc:
        progress['read_error'] = repr(exc)
    writer.join()


def one_round(transport, certs, chunks):
    def context(name, server):
        return transport.tls_context(certs / f'{name}.crt', certs / f'{name}.key', certs / 'ca.crt', server=server)
    listener = transport.listen('127.0.0.1', 0)
    progress, channels = {'a': {}, 'b': {}}, {}

    def serve():
        channels['b'] = transport.accept_channel(listener, tls=context('receiver', True))
        pump(channels['b'], 'b', 'a', chunks, progress['b'])
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    channels['a'] = transport.connect_channel('127.0.0.1', listener.getsockname()[1], tls=context('sender', False))
    client = threading.Thread(target=pump, args=(channels['a'], 'a', 'b', chunks, progress['a']), daemon=True)
    client.start()
    last, moved = None, time.monotonic()
    while client.is_alive() or server.is_alive():
        time.sleep(0.2)
        now = [progress[end].get(key) for end in 'ab' for key in ('sent', 'received')]
        if now != last:
            last, moved = now, time.monotonic()
        elif time.monotonic() - moved > 20:
            for channel in channels.values():
                channel.abort()
            return f'hang {progress}'
    for channel in channels.values():
        channel.close()
    bad = any(key in side for side in progress.values() for key in ('corrupt', 'read_error', 'write_error'))
    return f'FAIL {progress}' if bad else 'ok'


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--rounds', type=int, default=100)
    ap.add_argument('--mib', type=int, default=64)
    ap.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    a = ap.parse_args()
    sys.path.insert(0, a.repo)
    import ssl
    from pgdelta import transport
    print(f'Python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}, pgdelta.transport from {a.repo}', flush=True)
    with tempfile.TemporaryDirectory() as temp:
        certificates(Path(temp))
        failed = 0
        for r in range(a.rounds):
            started = time.monotonic()
            outcome = one_round(transport, Path(temp), a.mib * (1 << 20) // CHUNK)
            failed += outcome != 'ok'
            print(r, outcome, f'{time.monotonic() - started:.2f} s', flush=True)
    print(f'done: {a.rounds} rounds, {failed} not ok', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
